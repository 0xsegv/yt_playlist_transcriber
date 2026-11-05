import os
import re
import sys
import json
import time
import socket
import shutil
import ctypes
import threading
import subprocess
import webbrowser
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor
from http.server import HTTPServer, BaseHTTPRequestHandler
import requests
import yt_dlp

# =====================================================================
# 1. CONFIGURATION, GITHUB REPO & REMOTE KEYS
# =====================================================================
APP_VERSION = "2.0.0"

# Put your GitHub username/repo here (e.g. "ahmed123/arabic-transcriber")
GITHUB_REPO = "0xsegv/yt_playlist_transcriber"
GITHUB_BRANCH = "main"

# Leave these empty in your code!
# GitHub Actions will automatically inject REMOTE_CONFIG_URL when building the Windows .exe
DEFAULT_SPEECHMATICS_KEY = ""
DEFAULT_DEEPGRAM_KEY = ""
REMOTE_CONFIG_URL = ""

SETTINGS_FILE = Path.home() / ".arabic_transcriber_config.json"

STATE = {
    "version": APP_VERSION,
    "is_running": False,
    "cancel_requested": False,
    "overall_progress": 0.0,
    "video_progress": 0.0,
    "status": "جاهز للبدء. سيتم حفظ الملفات النصية على سطح المكتب (Desktop).",
    "sub_status": "في انتظار الرابط...",
    "logs": [],
    "last_output_dir": str(Path.home() / "Desktop"),
    "last_heartbeat": time.time(),
}


def add_log(msg: str):
    timestamp = time.strftime("%H:%M:%S")
    STATE["logs"].append(f"[{timestamp}] {msg}")


def set_status(
    main_msg: str | None = None,
    sub_msg: str | None = None,
    overall_pct: float | None = None,
    video_pct: float | None = None,
):
    if main_msg is not None:
        STATE["status"] = main_msg
    if sub_msg is not None:
        STATE["sub_status"] = sub_msg
    if overall_pct is not None:
        STATE["overall_progress"] = round(min(max(overall_pct, 0.0), 100.0), 1)
    if video_pct is not None:
        STATE["video_progress"] = round(min(max(video_pct, 0.0), 100.0), 1)


# =====================================================================
# 2. SYSTEM HELPERS (WINDOWS SLEEP LOCK, TIME, FILENAMES, SETTINGS)
# =====================================================================
def set_keep_awake(keep_awake: bool):
    """Prevents Windows laptops from going to sleep while transcribing a playlist."""
    if sys.platform.startswith("win"):
        try:
            ES_CONTINUOUS = 0x80000000
            ES_SYSTEM_REQUIRED = 0x00000001
            flags = (ES_CONTINUOUS | ES_SYSTEM_REQUIRED) if keep_awake else ES_CONTINUOUS
            ctypes.windll.kernel32.SetThreadExecutionState(flags)
        except Exception:
            pass


def cleanup_old_exe():
    """Removes leftover .old executable after a Windows self-update."""
    if getattr(sys, "frozen", False):
        old_exe = Path(sys.executable).with_suffix(".exe.old")
        if old_exe.exists():
            try:
                old_exe.unlink()
            except Exception:
                pass


def format_txt_time(seconds: float | None) -> str:
    if not seconds or seconds < 0:
        return "00:00"
    m, s = divmod(int(seconds), 60)
    h, m = divmod(m, 60)
    return f"{h:02d}:{m:02d}:{s:02d}" if h > 0 else f"{m:02d}:{s:02d}"


def format_srt_time(seconds: float) -> str:
    ms = int((seconds % 1) * 1000)
    total_sec = int(seconds)
    m, s = divmod(total_sec, 60)
    h, m = divmod(m, 60)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def sanitize_filename(name: str) -> str:
    cleaned = re.sub(r'[\\/*?:"<>|]', "", name)
    cleaned = re.sub(r"\s+", " ", cleaned).strip().rstrip(".")
    return cleaned[:120] if cleaned else "بدون_عنوان"


def get_base_output_dir() -> Path:
    desktop = Path.home() / "Desktop"
    base = (desktop if desktop.exists() else Path.home()) / "تفريغ الفيديوهات"
    base.mkdir(parents=True, exist_ok=True)
    return base


def open_folder_in_os(folder_path: Path):
    try:
        if sys.platform.startswith("win"):
            os.startfile(str(folder_path))
        elif sys.platform.startswith("darwin"):
            subprocess.Popen(["open", str(folder_path)])
        else:
            subprocess.Popen(["xdg-open", str(folder_path)])
    except Exception:
        pass


def load_settings() -> dict:
    settings = {
        "speechmatics_key": "",
        "deepgram_key": "",
        "generate_srt": False,
        "remote_config_url": "",
    }
    if SETTINGS_FILE.exists():
        try:
            with open(SETTINGS_FILE, "r", encoding="utf-8") as f:
                settings.update(json.load(f))
        except Exception:
            pass
    return settings


def save_settings(settings: dict):
    current = load_settings()
    current.update(settings)
    try:
        with open(SETTINGS_FILE, "w", encoding="utf-8") as f:
            json.dump(current, f, ensure_ascii=False, indent=2)
    except Exception:
        pass


def fetch_remote_config() -> dict:
    """Fetches keys.json from either baked-in REMOTE_CONFIG_URL or local config file."""
    url = REMOTE_CONFIG_URL if REMOTE_CONFIG_URL.startswith("http") else load_settings().get("remote_config_url", "")
    if url.startswith("http"):
        try:
            sep = "&" if "?" in url else "?"
            r = requests.get(f"{url}{sep}t={int(time.time())}", timeout=6)
            if r.status_code == 200:
                return r.json()
        except Exception:
            pass
    return {}


def resolve_api_keys(local_settings: dict) -> tuple[str, str]:
    sm_key = local_settings.get("speechmatics_key", "").strip()
    dg_key = local_settings.get("deepgram_key", "").strip()

    remote_data = fetch_remote_config()
    remote_sm = remote_data.get("speechmatics_key", "").strip()
    remote_dg = remote_data.get("deepgram_key", "").strip()

    final_sm = sm_key or remote_sm or DEFAULT_SPEECHMATICS_KEY
    final_dg = dg_key or remote_dg or DEFAULT_DEEPGRAM_KEY
    return final_sm, final_dg


# =====================================================================
# 3. 1-CLICK SELF-UPDATER (READS VERSION & EXE DIRECTLY FROM GITHUB)
# =====================================================================
def perform_self_update() -> dict:
    if not GITHUB_REPO or "YOUR_" in GITHUB_REPO:
        return {
            "ok": False,
            "message": f"أنت تستخدم الإصدار ({APP_VERSION}). لم يتم ضبط اسم مستودع GitHub بعد."
        }

    raw_py_url = f"https://raw.githubusercontent.com/{GITHUB_REPO}/{GITHUB_BRANCH}/app.py?t={int(time.time())}"
    exe_url = f"https://github.com/{GITHUB_REPO}/releases/download/latest/ArabicTranscriber.exe"

    try:
        r_py = requests.get(raw_py_url, timeout=10)
        if r_py.status_code != 200:
            return {"ok": False, "message": "تعذر الاتصال بـ GitHub للتحقق من التحديثات."}

        match = re.search(r'APP_VERSION\s*=\s*"([^"]+)"', r_py.text)
        remote_ver = match.group(1).strip() if match else APP_VERSION

        if remote_ver == APP_VERSION:
            return {"ok": True, "message": f"البرنامج محدث بالفعل إلى آخر إصدار ({APP_VERSION})! ✔"}

        # If running as compiled Windows .exe
        if getattr(sys, "frozen", False) and sys.platform.startswith("win"):
            current_exe = Path(sys.executable)
            old_exe = current_exe.with_suffix(".exe.old")
            if old_exe.exists():
                old_exe.unlink()

            r_exe = requests.get(exe_url, timeout=120)
            r_exe.raise_for_status()

            current_exe.rename(old_exe)
            with open(current_exe, "wb") as f:
                f.write(r_exe.content)

            return {
                "ok": True,
                "message": f"تم تحميل الإصدار الجديد ({remote_ver}) بنجاح! 🎉\nأغلق النافذة وافتح البرنامج من جديد."
            }
        else:
            # Running as Python script on Linux / PC
            script_path = Path(__file__).resolve()
            with open(script_path, "wb") as f:
                f.write(r_py.content)

            return {
                "ok": True,
                "message": f"تم تحديث الكود إلى الإصدار ({remote_ver}) بنجاح! 🎉\nأعد تشغيل البرنامج لتطبيق التحديث."
            }
    except Exception as e:
        return {"ok": False, "message": f"فشل التحديث التلقائي: {e}"}


# =====================================================================
# 4. TRANSCRIPTION ENGINES (WITH LIVE TIMER & NETWORK RECOVERY)
# =====================================================================
class QuotaOrAuthError(Exception):
    """Raised when an API key is invalid or out of credits (no point retrying the same provider)."""
    pass


def request_with_retry(method: str, url: str, max_attempts: int = 8, wait_sec: int = 4, **kwargs):
    for attempt in range(1, max_attempts + 1):
        if STATE["cancel_requested"]:
            raise RuntimeError("تم إيقاف العملية بواسطة المستخدم.")
        try:
            return requests.request(method, url, **kwargs)
        except requests.exceptions.RequestException as net_err:
            if attempt == max_attempts:
                raise net_err
            add_log(f"   🌐 انقطاع مؤقت في الشبكة (محاولة {attempt}/{max_attempts})... انتظار {wait_sec}ث")
            time.sleep(wait_sec)


def transcribe_speechmatics(audio_path: Path, api_key: str, file_mb: float, duration_str: str) -> list[dict]:
    if not api_key or "PASTE_" in api_key:
        raise QuotaOrAuthError("مفتاح Speechmatics غير مضبوط")

    base_url = "https://asr.api.speechmatics.com/v2/jobs"
    headers = {"Authorization": f"Bearer {api_key}"}
    config = {
        "type": "transcription",
        "transcription_config": {
            "language": "ar",
            "operating_point": "enhanced",
        },
    }

    set_status(
        sub_msg=f"☁ جاري رفع الصوت ({file_mb:.1f} MB) إلى Speechmatics...",
        video_pct=45,
    )

    res = None
    for attempt in range(1, 5):
        if STATE["cancel_requested"]:
            raise RuntimeError("تم إيقاف العملية بواسطة المستخدم.")
        try:
            with open(audio_path, "rb") as audio_file:
                files = {
                    "data_file": (audio_path.name, audio_file),
                    "config": (None, json.dumps(config), "application/json"),
                }
                res = requests.post(base_url, headers=headers, files=files, timeout=300)
            break
        except requests.exceptions.RequestException as net_err:
            if attempt == 4:
                raise net_err
            add_log(f"   🌐 انقطاع أثناء رفع الصوت (محاولة {attempt}/4)... انتظار 5ث")
            time.sleep(5)

    if res.status_code in (401, 402, 403):
        raise QuotaOrAuthError(f"انتهى رصيد أو صلاحية مفتاح Speechmatics (HTTP {res.status_code})")
    if res.status_code not in (200, 201):
        raise RuntimeError(f"Speechmatics HTTP {res.status_code}: {res.text[:200]}")

    job_id = res.json()["id"]
    poll_start = time.time()

    while True:
        if STATE["cancel_requested"]:
            raise RuntimeError("تم إيقاف العملية بواسطة المستخدم.")

        elapsed = int(time.time() - poll_start)
        anim_pct = min(92.0, 50.0 + (elapsed * 1.2))
        set_status(
            sub_msg=f"🤖 جاري التفريغ النصي عبر Speechmatics... (مضى {elapsed} ثانية | طول الفيديو: {duration_str})",
            video_pct=anim_pct,
        )

        status_res = request_with_retry(
            "GET",
            f"{base_url}/{job_id}",
            max_attempts=12,
            wait_sec=4,
            headers=headers,
            timeout=45,
        )
        status_res.raise_for_status()
        status = status_res.json()["job"]["status"]
        if status == "done":
            break
        elif status in ("rejected", "deleted", "expired"):
            raise RuntimeError(f"رفض الخادم معالجة الملف: {status}")

        # Sleep in 0.5s ticks so the live stopwatch updates smoothly
        for _ in range(4):
            if STATE["cancel_requested"]:
                break
            time.sleep(0.5)
            elapsed = int(time.time() - poll_start)
            set_status(
                sub_msg=f"🤖 جاري التفريغ النصي عبر Speechmatics... (مضى {elapsed} ثانية | طول الفيديو: {duration_str})"
            )

    set_status(sub_msg="📥 جاري استلام النص العربي وتنسيقه...", video_pct=96)
    trans_res = request_with_retry(
        "GET",
        f"{base_url}/{job_id}/transcript",
        max_attempts=6,
        wait_sec=4,
        headers=headers,
        params={"format": "json-v2"},
        timeout=60,
    )
    trans_res.raise_for_status()
    results = trans_res.json().get("results", [])

    segments = []
    curr_words = []
    seg_start = None
    seg_end = 0.0

    for item in results:
        if item.get("type") not in ("word", "punctuation"):
            continue
        content = item["alternatives"][0]["content"]
        start_t = float(item.get("start_time", seg_end))
        end_t = float(item.get("end_time", start_t + 0.5))

        if seg_start is None:
            seg_start = start_t
        seg_end = max(seg_end, end_t)

        if item.get("type") == "punctuation" and curr_words:
            curr_words[-1] += content
        else:
            curr_words.append(content)

        if content in (".", "؟", "!", "?") or (seg_end - seg_start >= 18):
            segments.append({
                "start": seg_start,
                "end": seg_end,
                "text": " ".join(curr_words).strip()
            })
            curr_words = []
            seg_start = None

    if curr_words and seg_start is not None:
        segments.append({
            "start": seg_start,
            "end": seg_end,
            "text": " ".join(curr_words).strip()
        })

    return segments


def transcribe_deepgram(audio_path: Path, api_key: str, file_mb: float, duration_str: str) -> list[dict]:
    if not api_key or "PASTE_" in api_key:
        raise QuotaOrAuthError("مفتاح Deepgram غير مضبوط")

    set_status(
        sub_msg=f"🤖 جاري الرفع والتفريغ السريع عبر Deepgram ({file_mb:.1f} MB | المدة: {duration_str})...",
        video_pct=60,
    )

    url = "https://api.deepgram.com/v1/listen"
    params = {
        "model": "nova-3",
        "language": "ar-EG",
        "smart_format": "true",
        "utterances": "true",
    }
    headers = {
        "Authorization": f"Token {api_key}",
        "Content-Type": "application/octet-stream",
    }

    res = None
    for attempt in range(1, 5):
        if STATE["cancel_requested"]:
            raise RuntimeError("تم إيقاف العملية بواسطة المستخدم.")
        try:
            with open(audio_path, "rb") as f:
                res = requests.post(url, params=params, headers=headers, data=f, timeout=300)
            break
        except requests.exceptions.RequestException as net_err:
            if attempt == 4:
                raise net_err
            add_log(f"   🌐 انقطاع مؤقت مع Deepgram (محاولة {attempt}/4)... انتظار 5ث")
            time.sleep(5)

    if res.status_code != 200 and res.status_code not in (401, 402, 403):
        params["language"] = "ar"
        with open(audio_path, "rb") as f:
            res = requests.post(url, params=params, headers=headers, data=f, timeout=300)

    if res.status_code != 200:
        raise RuntimeError(f"Deepgram HTTP {res.status_code}: {res.text[:200]}")

    data = res.json()
    utterances = data.get("results", {}).get("utterances", [])
    segments = []
    if utterances:
        for u in utterances:
            segments.append({
                "start": float(u.get("start", 0.0)),
                "end": float(u.get("end", 0.0)),
                "text": u.get("transcript", "").strip(),
            })
    else:
        alt = data["results"]["channels"][0]["alternatives"][0]
        segments.append({
            "start": 0.0,
            "end": 10.0,
            "text": alt.get("transcript", "").strip(),
        })
    return segments


def save_outputs_atomic(
    segments: list[dict],
    txt_path: Path,
    srt_path: Path | None,
    video_title: str,
    playlist_title: str,
    video_url: str,
):
    """Writes to .tmp first and renames atomically so crashes never leave half-written files."""
    tmp_txt = txt_path.with_suffix(".txt.tmp")
    with open(tmp_txt, "w", encoding="utf-8") as f:
        f.write(f"عنوان الفيديو: {video_title}\n")
        f.write(f"القائمة: {playlist_title}\n")
        f.write(f"رابط الفيديو: {video_url}\n")
        f.write("=" * 50 + "\n\n")
        for seg in segments:
            if seg["text"]:
                f.write(f"[{format_txt_time(seg['start'])}] {seg['text']}\n")
    tmp_txt.replace(txt_path)

    if srt_path:
        srt_path.parent.mkdir(parents=True, exist_ok=True)
        tmp_srt = srt_path.with_suffix(".srt.tmp")
        with open(tmp_srt, "w", encoding="utf-8") as f:
            idx = 1
            for seg in segments:
                if not seg["text"]:
                    continue
                start_str = format_srt_time(seg["start"])
                end_str = format_srt_time(max(seg["end"], seg["start"] + 1.0))
                f.write(f"{idx}\n{start_str} --> {end_str}\n{seg['text']}\n\n")
                idx += 1
        tmp_srt.replace(srt_path)


# =====================================================================
# 5. FAST AUDIO DOWNLOADER (VOICE-OPTIMIZED + LIVE PROGRESS HOOK)
# =====================================================================
def download_video_audio(vid_url: str, temp_dir: Path, slot_prefix: str, update_ui: bool = True) -> Path:
    for old_f in temp_dir.glob(f"{slot_prefix}.*"):
        try:
            old_f.unlink()
        except Exception:
            pass

    def progress_hook(d):
        if STATE["cancel_requested"]:
            raise RuntimeError("تم إيقاف العملية بواسطة المستخدم.")
        if not update_ui:
            return
        if d.get("status") == "downloading":
            downloaded = d.get("downloaded_bytes", 0) or 0
            total_b = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
            speed = d.get("speed") or 0
            speed_str = f"{speed / (1024 * 1024):.1f} MB/s" if speed else "..."
            dl_mb = downloaded / (1024 * 1024)

            if total_b > 0:
                tot_mb = total_b / (1024 * 1024)
                pct = (downloaded / total_b) * 100.0
                set_status(
                    sub_msg=f"⬇ جاري تحميل الصوت: {pct:.0f}% ({dl_mb:.1f} MB / {tot_mb:.1f} MB) — السرعة: {speed_str}",
                    video_pct=min(40.0, pct * 0.4),
                )
            else:
                set_status(
                    sub_msg=f"⬇ جاري تحميل الصوت: {dl_mb:.1f} MB — السرعة: {speed_str}",
                    video_pct=20.0,
                )

    # Voice-optimized format: prefers 48-75kbps m4a/webm (~3x smaller & faster than 128k music streams)
    dl_opts = {
        "format": "ba[ext=m4a][abr<=75]/ba[ext=webm][abr<=75]/ba[ext=m4a]/ba[ext=webm]/ba",
        "outtmpl": str(temp_dir / f"{slot_prefix}.%(ext)s"),
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        "retries": 10,
        "fragment_retries": 10,
        "progress_hooks": [progress_hook],
    }
    with yt_dlp.YoutubeDL(dl_opts) as ydl:
        ydl.download([vid_url])

    files = [
        f for f in temp_dir.glob(f"{slot_prefix}.*")
        if not f.name.endswith(".part") and not f.name.endswith(".ytdl")
    ]
    if not files:
        raise FileNotFoundError("لم يتم العثور على ملف الصوت بعد التحميل.")
    return files[0]


# =====================================================================
# 6. PIPELINED PLAYLIST WORKER (PRE-FETCHES NEXT AUDIO WHILE TRANSCRIBING)
# =====================================================================
def process_playlist_worker(url: str):
    STATE["is_running"] = True
    STATE["cancel_requested"] = False
    STATE["logs"] = []
    set_keep_awake(True)
    playlist_start_time = time.time()

    try:
        settings = load_settings()
        sm_key, dg_key = resolve_api_keys(settings)
        gen_srt = settings.get("generate_srt", False)
        sm_disabled_for_session = False

        set_status(
            main_msg="جاري قراءة معلومات القائمة أو الفيديو من يوتيوب...",
            sub_msg="جاري الاتصال بيوتيوب...",
            overall_pct=2,
            video_pct=5,
        )
        add_log("🔍 جاري فحص الرابط واستخراج الفيديوهات...")

        ydl_extract_opts = {
            "extract_flat": "in_playlist",
            "quiet": True,
            "no_warnings": True,
            "retries": 5,
        }
        with yt_dlp.YoutubeDL(ydl_extract_opts) as ydl:
            info = ydl.extract_info(url, download=False)

        if "entries" in info:
            playlist_title = sanitize_filename(info.get("title") or "قائمة تشغيل يوتيوب")
            entries = [e for e in info["entries"] if e]
        else:
            playlist_title = sanitize_filename(info.get("title") or "فيديو يوتيوب")
            entries = [info]

        total = len(entries)
        if total == 0:
            raise RuntimeError("لم يتم العثور على أي فيديوهات في هذا الرابط.")

        out_dir = get_base_output_dir() / playlist_title
        out_dir.mkdir(parents=True, exist_ok=True)
        STATE["last_output_dir"] = str(out_dir)

        temp_dir = out_dir / ".temp_audio"
        temp_dir.mkdir(exist_ok=True)

        # Build structured task list
        tasks = []
        for idx, entry in enumerate(entries, start=1):
            vid_id = entry.get("id")
            vid_title = sanitize_filename(entry.get("title") or f"فيديو {idx}")
            vid_url = entry.get("url") or entry.get("webpage_url") or f"https://www.youtube.com/watch?v={vid_id}"
            if not vid_url.startswith("http"):
                vid_url = f"https://www.youtube.com/watch?v={vid_id}"
            dur_sec = entry.get("duration")
            dur_str = format_txt_time(dur_sec) if dur_sec else "غير محدد"

            file_prefix = f"{idx:03d} - {vid_title}" if total > 1 else vid_title
            txt_path = out_dir / f"{file_prefix}.txt"
            srt_path = (out_dir / "srt" / f"{file_prefix}.srt") if gen_srt else None

            already_done = txt_path.exists() and txt_path.stat().st_size > 100
            tasks.append({
                "idx": idx,
                "title": vid_title,
                "url": vid_url,
                "dur_str": dur_str,
                "txt_path": txt_path,
                "srt_path": srt_path,
                "done": already_done,
            })

        add_log(f"📋 تم العثور على {total} فيديو في: {playlist_title}")

        completed_count = 0
        prefetch_executor = ThreadPoolExecutor(max_workers=1)
        prefetched_future = None
        prefetched_idx = None

        for i, task in enumerate(tasks):
            if STATE["cancel_requested"]:
                add_log("⏹ تم إيقاف العملية بناءً على طلبك. يمكنك الاستكمال في أي وقت لاحقاً.")
                set_status(
                    main_msg=f"⏹ تم الإيقاف المؤقت (اكتمل {completed_count} من {total} فيديو).",
                    sub_msg="اضغط 'ابدأ' في أي وقت للاستكمال من حيث توقفت.",
                    video_pct=0,
                )
                break

            idx = task["idx"]
            vid_title = task["title"]
            overall_pct = ((idx - 1) / total) * 100.0

            if task["done"]:
                add_log(f"[{idx}/{total}] ⏭ تم تخطي (موجود مسبقاً): {vid_title}")
                completed_count += 1
                set_status(
                    main_msg=f"جاري فحص الفيديوهات ({idx}/{total})...",
                    sub_msg=f"تم تخطي الفيديو الموجود: {vid_title}",
                    overall_pct=(idx / total) * 100.0,
                    video_pct=100.0,
                )
                continue

            set_status(
                main_msg=f"جاري معالجة الفيديو [{idx} من {total}]: {vid_title}",
                overall_pct=overall_pct,
                video_pct=2.0,
            )

            try:
                # 1. Obtain audio (either from background pre-fetch or live download)
                dl_start = time.time()
                if prefetched_future is not None and prefetched_idx == idx:
                    set_status(sub_msg="⚡ جاري استلام الصوت المحمّل مسبقاً في الخلفية...", video_pct=35.0)
                    audio_file = prefetched_future.result()
                else:
                    audio_file = download_video_audio(task["url"], temp_dir, f"slot_{idx}", update_ui=True)
                dl_elapsed = time.time() - dl_start

                file_mb = audio_file.stat().st_size / (1024 * 1024)

                # 2. Kick off background pre-fetch of NEXT needed video while AI transcribes this one!
                prefetched_future = None
                prefetched_idx = None
                for next_task in tasks[i + 1:]:
                    if not next_task["done"]:
                        prefetched_idx = next_task["idx"]
                        prefetched_future = prefetch_executor.submit(
                            download_video_audio,
                            next_task["url"],
                            temp_dir,
                            f"slot_{prefetched_idx}",
                            False,  # Silent background pre-fetch
                        )
                        break

                # 3. Transcribe with Speechmatics (or fallback to Deepgram)
                tr_start = time.time()
                used_engine = "Speechmatics"
                segments = None

                if not sm_disabled_for_session:
                    try:
                        segments = transcribe_speechmatics(audio_file, sm_key, file_mb, task["dur_str"])
                    except QuotaOrAuthError as q_err:
                        sm_disabled_for_session = True
                        add_log(f"   ⚠ {q_err} — سيتم استخدام Deepgram لباقي الفيديوهات تلقائياً.")
                        used_engine = "Deepgram"
                        segments = transcribe_deepgram(audio_file, dg_key, file_mb, task["dur_str"])
                    except Exception as sm_err:
                        if STATE["cancel_requested"]:
                            raise sm_err
                        add_log(f"[{idx}/{total}] ⚠ تنبيه Speechmatics ({sm_err}) — التحويل تلقائياً إلى Deepgram...")
                        used_engine = "Deepgram"
                        segments = transcribe_deepgram(audio_file, dg_key, file_mb, task["dur_str"])
                else:
                    used_engine = "Deepgram"
                    segments = transcribe_deepgram(audio_file, dg_key, file_mb, task["dur_str"])

                tr_elapsed = time.time() - tr_start

                # 4. Save outputs atomically
                save_outputs_atomic(
                    segments,
                    task["txt_path"],
                    task["srt_path"],
                    vid_title,
                    playlist_title,
                    task["url"],
                )

                try:
                    audio_file.unlink()
                except Exception:
                    pass

                completed_count += 1
                set_status(
                    overall_pct=(idx / total) * 100.0,
                    video_pct=100.0,
                )
                add_log(
                    f"[{idx}/{total}] ✔ تم الحفظ ({used_engine}): {task['txt_path'].name} "
                    f"(المدة: {task['dur_str']} | الحجم: {file_mb:.1f}MB | التحميل: {dl_elapsed:.1f}ث | التفريغ: {tr_elapsed:.1f}ث)"
                )

            except Exception as vid_err:
                if STATE["cancel_requested"]:
                    add_log("⏹ تم إيقاف العملية بواسطة المستخدم.")
                    break
                add_log(f"[{idx}/{total}] ❌ تعذر إكمال هذا الفيديو وتم تخطيه ({vid_title}): {vid_err}")
                continue

        prefetch_executor.shutdown(wait=False)
        for leftover in temp_dir.glob("*"):
            try:
                leftover.unlink()
            except Exception:
                pass
        try:
            temp_dir.rmdir()
        except Exception:
            pass

        if not STATE["cancel_requested"]:
            total_elapsed = int(time.time() - playlist_start_time)
            set_status(
                main_msg=f"✅ اكتمل بنجاح! تم تجهيز {completed_count} من {total} فيديو في ({format_txt_time(total_elapsed)}).",
                sub_msg=f"تم حفظ جميع الملفات في سطح المكتب داخل: تفريغ الفيديوهات / {playlist_title}",
                overall_pct=100.0,
                video_pct=100.0,
            )
            add_log(f"🎉 انتهت العملية بالكامل في {format_txt_time(total_elapsed)}! تم فتح مجلد الملفات.")
            open_folder_in_os(out_dir)

    except Exception as e:
        set_status(main_msg=f"❌ حدث خطأ: {e}", sub_msg="تأكد من صحة الرابط أو الاتصال بالإنترنت.", overall_pct=0, video_pct=0)
        add_log(f"❌ خطأ: {e}")
    finally:
        set_keep_awake(False)
        STATE["is_running"] = False
        STATE["cancel_requested"] = False


# =====================================================================
# 7. ARABIC RTL FRONTEND (WITH DUAL PROGRESS BARS, STOP & SELF-UPDATE)
# =====================================================================
HTML_PAGE = """<!DOCTYPE html>
<html lang="ar" dir="rtl">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>مفرغ فيديوهات يوتيوب إلى نص (NotebookLM)</title>
<style>
  * { box-sizing: border-box; font-family: system-ui, -apple-system, 'Segoe UI', Tahoma, Arial, sans-serif; }
  body {
    margin: 0; padding: 20px;
    background: #f1f5f9; color: #0f172a;
    display: flex; justify-content: center;
  }
  .container {
    width: 100%; max-width: 820px;
    background: #ffffff; border-radius: 16px;
    padding: 26px; box-shadow: 0 10px 30px rgba(0,0,0,0.08);
  }
  .header {
    display: flex; justify-content: space-between; align-items: center; flex-wrap: wrap; gap: 10px;
    border-bottom: 2px solid #f1f5f9; padding-bottom: 14px; margin-bottom: 20px;
  }
  .header h1 { margin: 0; font-size: 1.35rem; color: #0f172a; }
  .version-badge {
    background: #e2e8f0; color: #475569; font-size: 0.8rem;
    padding: 3px 8px; border-radius: 6px; margin-right: 6px;
  }
  .toolbar { display: flex; gap: 8px; flex-wrap: wrap; }
  .btn-secondary {
    background: #f8fafc; color: #1e293b; border: 1px solid #cbd5e1;
    padding: 8px 12px; border-radius: 8px; cursor: pointer; font-weight: 600; font-size: 0.9rem;
    transition: all 0.15s;
  }
  .btn-secondary:hover { background: #e2e8f0; }
  label { display: block; font-weight: 700; margin-bottom: 8px; font-size: 1rem; color: #1e293b; }
  input[type="text"] {
    width: 100%; padding: 13px 16px; font-size: 1rem;
    border: 2px solid #cbd5e1; border-radius: 10px; outline: none;
    direction: ltr; text-align: left;
  }
  input[type="text"]:focus { border-color: #2563eb; }
  .action-row { display: flex; gap: 10px; margin-top: 14px; }
  .btn-primary {
    flex: 1; padding: 14px;
    background: #2563eb; color: #fff; border: none; border-radius: 10px;
    font-size: 1.1rem; font-weight: 700; cursor: pointer; transition: background 0.15s;
  }
  .btn-primary:hover:not(:disabled) { background: #1d4ed8; }
  .btn-primary:disabled { background: #93c5fd; cursor: not-allowed; }
  .btn-stop {
    padding: 14px 22px; background: #ef4444; color: #fff; border: none;
    border-radius: 10px; font-size: 1.05rem; font-weight: 700; cursor: pointer;
    display: none;
  }
  .btn-stop:hover { background: #dc2626; }
  .status-card {
    margin-top: 18px; background: #f8fafc; border: 1px solid #e2e8f0;
    border-radius: 12px; padding: 14px 16px;
  }
  .status-main { font-weight: 700; font-size: 1rem; color: #1e3a8a; margin-bottom: 6px; }
  .status-sub { font-size: 0.92rem; color: #475569; margin-top: 8px; font-weight: 600; }
  .bar-label { display: flex; justify-content: space-between; font-size: 0.82rem; color: #64748b; margin-top: 6px; }
  .progress-wrap {
    margin-top: 4px; background: #e2e8f0; border-radius: 99px;
    height: 12px; overflow: hidden;
  }
  .progress-bar-overall {
    height: 100%; width: 0%; background: linear-gradient(90deg, #2563eb, #10b981);
    transition: width 0.3s ease;
  }
  .progress-bar-video {
    height: 100%; width: 0%; background: #f59e0b;
    transition: width 0.25s ease;
  }
  .log-box {
    margin-top: 16px; background: #0f172a; color: #f8fafc;
    border-radius: 10px; padding: 14px; height: 220px;
    overflow-y: auto; font-size: 0.9rem; line-height: 1.75;
  }
  .log-item { border-bottom: 1px solid #1e293b; padding: 3px 0; }
  .modal-bg {
    display: none; position: fixed; inset: 0; background: rgba(0,0,0,0.45);
    justify-content: center; align-items: center; padding: 16px;
  }
  .modal {
    background: #fff; border-radius: 14px; max-width: 520px; width: 100%;
    padding: 24px; box-shadow: 0 12px 32px rgba(0,0,0,0.2);
  }
  .modal h2 { margin-top: 0; font-size: 1.25rem; }
  .field { margin-bottom: 14px; }
  .checkbox-row { display: flex; align-items: center; gap: 10px; margin-bottom: 16px; font-weight: 600; cursor: pointer; }
  .checkbox-row input { width: 18px; height: 18px; }
</style>
</head>
<body>
<div class="container">
  <div class="header">
    <div>
      <h1>تحويل قوائم وفيديوهات يوتيوب إلى نصوص عربية <span id="verBadge" class="version-badge">v2.0</span></h1>
    </div>
    <div class="toolbar">
      <button class="btn-secondary" onclick="openFolder()">📁 فتح المجلد</button>
      <button class="btn-secondary" onclick="window.open('https://notebooklm.google.com/', '_blank')">🧠 فتح NotebookLM</button>
      <button class="btn-secondary" onclick="checkUpdate()">🔄 تحديث البرنامج</button>
      <button class="btn-secondary" onclick="openSettings()">⚙ الإعدادات</button>
    </div>
  </div>

  <label for="urlInput">ضع رابط قائمة التشغيل (Playlist) أو الفيديو من يوتيوب هنا:</label>
  <input type="text" id="urlInput" placeholder="https://www.youtube.com/playlist?list=..." />

  <div class="action-row">
    <button id="startBtn" class="btn-primary" onclick="startJob()">ابدأ التفريغ النصي الآن</button>
    <button id="stopBtn" class="btn-stop" onclick="stopJob()">⏹ إيقاف</button>
  </div>

  <div class="status-card">
    <div id="statusText" class="status-main">جاهز للبدء.</div>
    <div class="bar-label"><span>التقدم الإجمالي للقائمة</span><span id="overallPctTxt">0%</span></div>
    <div class="progress-wrap"><div id="overallBar" class="progress-bar-overall"></div></div>

    <div id="subStatusText" class="status-sub">في انتظار الرابط...</div>
    <div class="bar-label"><span>تقدم الفيديو الحالي (التحميل والتفريغ)</span><span id="videoPctTxt">0%</span></div>
    <div class="progress-wrap" style="height:8px;"><div id="videoBar" class="progress-bar-video"></div></div>
  </div>

  <div id="logBox" class="log-box"></div>
</div>

<!-- Settings Modal -->
<div id="modalBg" class="modal-bg">
  <div class="modal">
    <h2>⚙ الإعدادات</h2>
    <label class="checkbox-row">
      <input type="checkbox" id="srtCheck" />
      <span>إنشاء ملفات ترجمة بوقت زمني (.srt) في مجلد فرعي</span>
    </label>
    <div class="field">
      <label>مفتاح Speechmatics إضافي (اتركه فارغاً لاستخدام المفتاح الافتراضي):</label>
      <input type="text" id="smKey" placeholder="اختياري..." />
    </div>
    <div class="field">
      <label>مفتاح Deepgram إضافي (اتركه فارغاً لاستخدام المفتاح الافتراضي):</label>
      <input type="text" id="dgKey" placeholder="اختياري..." />
    </div>
    <div style="display:flex; gap:10px; justify-content:flex-end; margin-top:18px;">
      <button class="btn-secondary" onclick="closeSettings()">إلغاء</button>
      <button class="btn-secondary" style="background:#2563eb;color:#fff;" onclick="saveSettings()">حفظ الإعدادات</button>
    </div>
  </div>
</div>

<script>
let lastLogCount = 0;

async function pollState() {
  try {
    const res = await fetch('/api/state');
    const data = await res.json();
    document.getElementById('verBadge').textContent = 'v' + data.version;
    document.getElementById('statusText').textContent = data.status;
    document.getElementById('subStatusText').textContent = data.sub_status;

    document.getElementById('overallBar').style.width = data.overall_progress + '%';
    document.getElementById('overallPctTxt').textContent = data.overall_progress + '%';

    document.getElementById('videoBar').style.width = data.video_progress + '%';
    document.getElementById('videoPctTxt').textContent = data.video_progress + '%';

    const startBtn = document.getElementById('startBtn');
    const stopBtn = document.getElementById('stopBtn');
    startBtn.disabled = data.is_running;
    startBtn.textContent = data.is_running ? 'جاري العمل... يرجى الانتظار' : 'ابدأ التفريغ النصي الآن';
    stopBtn.style.display = data.is_running ? 'block' : 'none';

    if (data.logs.length !== lastLogCount) {
      const box = document.getElementById('logBox');
      box.innerHTML = data.logs.map(l => `<div class="log-item">${l}</div>`).join('');
      box.scrollTop = box.scrollHeight;
      lastLogCount = data.logs.length;
    }
  } catch (e) {}
}

setInterval(pollState, 800);
pollState();

async function startJob() {
  const url = document.getElementById('urlInput').value.trim();
  if (!url) {
    alert('من فضلك ضع رابط يوتيوب أولاً.');
    return;
  }
  lastLogCount = 0;
  await fetch('/api/start', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({url})
  });
  pollState();
}

async function stopJob() {
  await fetch('/api/stop', {method: 'POST'});
}

async function openFolder() {
  await fetch('/api/open_folder', {method: 'POST'});
}

async function checkUpdate() {
  const res = await fetch('/api/update', {method: 'POST'});
  const data = await res.json();
  alert(data.message);
}

async function openSettings() {
  const res = await fetch('/api/settings');
  const s = await res.json();
  document.getElementById('srtCheck').checked = !!s.generate_srt;
  document.getElementById('smKey').value = s.speechmatics_key || '';
  document.getElementById('dgKey').value = s.deepgram_key || '';
  document.getElementById('modalBg').style.display = 'flex';
}

function closeSettings() {
  document.getElementById('modalBg').style.display = 'none';
}

async function saveSettings() {
  const payload = {
    generate_srt: document.getElementById('srtCheck').checked,
    speechmatics_key: document.getElementById('smKey').value.trim(),
    deepgram_key: document.getElementById('dgKey').value.trim()
  };
  await fetch('/api/settings', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify(payload)
  });
  closeSettings();
}
</script>
</body>
</html>
"""


class AppHandler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        pass

    def _send_json(self, data: dict, status=200):
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        STATE["last_heartbeat"] = time.time()
        if self.path == "/":
            body = HTML_PAGE.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif self.path == "/api/state":
            self._send_json(STATE)
        elif self.path == "/api/settings":
            self._send_json(load_settings())
        else:
            self.send_error(404)

    def do_POST(self):
        STATE["last_heartbeat"] = time.time()
        length = int(self.headers.get("Content-Length", 0))
        raw = self.rfile.read(length).decode("utf-8") if length > 0 else "{}"
        data = json.loads(raw) if raw else {}

        if self.path == "/api/start":
            if not STATE["is_running"]:
                url = data.get("url", "").strip()
                t = threading.Thread(target=process_playlist_worker, args=(url,), daemon=True)
                t.start()
            self._send_json({"ok": True})
        elif self.path == "/api/stop":
            if STATE["is_running"]:
                STATE["cancel_requested"] = True
                set_status(sub_msg="⏹ جاري إيقاف العملية بأمان...")
            self._send_json({"ok": True})
        elif self.path == "/api/settings":
            save_settings(data)
            self._send_json({"ok": True})
        elif self.path == "/api/open_folder":
            target = Path(STATE.get("last_output_dir", str(get_base_output_dir())))
            open_folder_in_os(target if target.exists() else get_base_output_dir())
            self._send_json({"ok": True})
        elif self.path == "/api/update":
            res = perform_self_update()
            self._send_json(res)
        else:
            self.send_error(404)


def launch_app_window(url: str):
    if sys.platform.startswith("win"):
        try:
            subprocess.Popen(["cmd", "/c", "start", "", "msedge", f"--app={url}", "--window-size=900,740"])
            return
        except Exception:
            pass
    else:
        for browser_bin in ("chromium", "brave", "google-chrome-stable", "microsoft-edge"):
            if shutil.which(browser_bin):
                try:
                    subprocess.Popen([browser_bin, f"--app={url}", "--window-size=900,740"])
                    return
                except Exception:
                    pass
    webbrowser.open(url)


def main():
    cleanup_old_exe()
    STATE["last_output_dir"] = str(get_base_output_dir())

    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()

    server = HTTPServer(("127.0.0.1", port), AppHandler)
    url = f"http://127.0.0.1:{port}"
    print(f"App running at: {url}")

    threading.Thread(target=server.serve_forever, daemon=True).start()
    launch_app_window(url)

    try:
        while True:
            time.sleep(2)
            idle_seconds = time.time() - STATE["last_heartbeat"]
            if not STATE["is_running"] and idle_seconds > 15:
                print("Window closed. Shutting down cleanly.")
                break
    except KeyboardInterrupt:
        pass
    finally:
        set_keep_awake(False)
        server.shutdown()


if __name__ == "__main__":
    main()
