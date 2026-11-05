import os
import re
import sys
import json
import time
import socket
import shutil
import threading
import subprocess
import webbrowser
from pathlib import Path
from http.server import HTTPServer, BaseHTTPRequestHandler
import requests
import yt_dlp

# =====================================================================
# 1. DEFAULT & REMOTE CONFIGURATION (PUT YOUR KEYS HERE)
# =====================================================================
DEFAULT_SPEECHMATICS_KEY = "PASTE_YOUR_SPEECHMATICS_KEY_HERE"
DEFAULT_DEEPGRAM_KEY = "PASTE_YOUR_DEEPGRAM_KEY_HERE"

# Optional: Raw GitHub Gist URL to update keys remotely in the future
REMOTE_CONFIG_URL = "https://gist.githubusercontent.com/0xsegv/fd29b7f8741ff9471dbff60d56fe1140/raw/keys.json"

SETTINGS_FILE = Path.home() / ".arabic_transcriber_config.json"

# Global App State
STATE = {
    "is_running": False,
    "progress": 0,
    "status": "جاهز للبدء. سيتم حفظ الملفات النصية على سطح المكتب (Desktop).",
    "logs": [],
    "last_output_dir": str(Path.home() / "Desktop"),
    "last_heartbeat": time.time(),
}


def add_log(msg: str):
    STATE["logs"].append(msg)


def set_status(msg: str, progress: float | None = None):
    STATE["status"] = msg
    if progress is not None:
        STATE["progress"] = round(progress, 1)


# =====================================================================
# 2. HELPER FUNCTIONS
# =====================================================================
def format_txt_time(seconds: float) -> str:
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
    }
    if SETTINGS_FILE.exists():
        try:
            with open(SETTINGS_FILE, "r", encoding="utf-8") as f:
                settings.update(json.load(f))
        except Exception:
            pass
    return settings


def save_settings(settings: dict):
    try:
        with open(SETTINGS_FILE, "w", encoding="utf-8") as f:
            json.dump(settings, f, ensure_ascii=False, indent=2)
    except Exception:
        pass


def resolve_api_keys(local_settings: dict) -> tuple[str, str]:
    sm_key = local_settings.get("speechmatics_key", "").strip()
    dg_key = local_settings.get("deepgram_key", "").strip()

    remote_sm, remote_dg = "", ""
    if REMOTE_CONFIG_URL.startswith("http"):
        try:
            r = requests.get(REMOTE_CONFIG_URL, timeout=5)
            if r.status_code == 200:
                data = r.json()
                remote_sm = data.get("speechmatics_key", "").strip()
                remote_dg = data.get("deepgram_key", "").strip()
        except Exception:
            pass

    final_sm = sm_key or remote_sm or DEFAULT_SPEECHMATICS_KEY
    final_dg = dg_key or remote_dg or DEFAULT_DEEPGRAM_KEY
    return final_sm, final_dg


# =====================================================================
# 3. TRANSCRIPTION ENGINES (SPEECHMATICS PRIMARY + DEEPGRAM FALLBACK)
# =====================================================================
def transcribe_speechmatics(audio_path: Path, api_key: str) -> list[dict]:
    if not api_key or "PASTE_" in api_key:
        raise ValueError("مفتاح Speechmatics غير مضبوط")

    base_url = "https://asr.api.speechmatics.com/v2/jobs"
    headers = {"Authorization": f"Bearer {api_key}"}
    config = {
        "type": "transcription",
        "transcription_config": {
            "language": "ar",
            "operating_point": "enhanced",
        },
    }

    with open(audio_path, "rb") as audio_file:
        files = {
            "data_file": (audio_path.name, audio_file),
            "config": (None, json.dumps(config), "application/json"),
        }
        res = requests.post(base_url, headers=headers, files=files, timeout=300)

    if res.status_code not in (200, 201):
        raise RuntimeError(f"Speechmatics HTTP {res.status_code}: {res.text[:200]}")

    job_id = res.json()["id"]

    while True:
        status_res = requests.get(f"{base_url}/{job_id}", headers=headers, timeout=60)
        status_res.raise_for_status()
        status = status_res.json()["job"]["status"]
        if status == "done":
            break
        elif status in ("rejected", "deleted", "expired"):
            raise RuntimeError(f"Speechmatics job failed: {status}")
        time.sleep(3)

    trans_res = requests.get(
        f"{base_url}/{job_id}/transcript",
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


def transcribe_deepgram(audio_path: Path, api_key: str) -> list[dict]:
    if not api_key or "PASTE_" in api_key:
        raise ValueError("مفتاح Deepgram غير مضبوط")

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

    with open(audio_path, "rb") as f:
        res = requests.post(url, params=params, headers=headers, data=f, timeout=300)

    if res.status_code != 200:
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


def save_outputs(
    segments: list[dict],
    txt_path: Path,
    srt_path: Path | None,
    video_title: str,
    playlist_title: str,
    video_url: str,
):
    with open(txt_path, "w", encoding="utf-8") as f:
        f.write(f"عنوان الفيديو: {video_title}\n")
        f.write(f"القائمة: {playlist_title}\n")
        f.write(f"رابط الفيديو: {video_url}\n")
        f.write("=" * 50 + "\n\n")
        for seg in segments:
            if seg["text"]:
                f.write(f"[{format_txt_time(seg['start'])}] {seg['text']}\n")

    if srt_path:
        srt_path.parent.mkdir(parents=True, exist_ok=True)
        with open(srt_path, "w", encoding="utf-8") as f:
            idx = 1
            for seg in segments:
                if not seg["text"]:
                    continue
                start_str = format_srt_time(seg["start"])
                end_str = format_srt_time(max(seg["end"], seg["start"] + 1.0))
                f.write(f"{idx}\n{start_str} --> {end_str}\n{seg['text']}\n\n")
                idx += 1


# =====================================================================
# 4. BACKGROUND WORKER
# =====================================================================
def process_playlist_worker(url: str):
    STATE["is_running"] = True
    STATE["logs"] = []
    try:
        settings = load_settings()
        sm_key, dg_key = resolve_api_keys(settings)
        gen_srt = settings.get("generate_srt", False)

        set_status("جاري قراءة معلومات القائمة أو الفيديو من يوتيوب...", 3)
        add_log("جاري فحص الرابط واستخراج الفيديوهات...")

        ydl_extract_opts = {
            "extract_flat": "in_playlist",
            "quiet": True,
            "no_warnings": True,
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

        add_log(f"تم العثور على {total} فيديو في: {playlist_title}")

        completed_count = 0
        for idx, entry in enumerate(entries, start=1):
            vid_id = entry.get("id")
            vid_title = sanitize_filename(entry.get("title") or f"فيديو {idx}")
            vid_url = entry.get("url") or entry.get("webpage_url") or f"https://www.youtube.com/watch?v={vid_id}"
            if not vid_url.startswith("http"):
                vid_url = f"https://www.youtube.com/watch?v={vid_id}"

            file_prefix = f"{idx:03d} - {vid_title}" if total > 1 else vid_title
            txt_path = out_dir / f"{file_prefix}.txt"
            srt_path = (out_dir / "srt" / f"{file_prefix}.srt") if gen_srt else None

            pct_base = ((idx - 1) / total) * 100

            if txt_path.exists() and txt_path.stat().st_size > 100:
                add_log(f"[{idx}/{total}] ⏭ تم تخطي (موجود مسبقاً): {vid_title}")
                completed_count += 1
                set_status(f"[{idx}/{total}] تم تخطي الفيديو الموجود: {vid_title}", (idx / total) * 100)
                continue

            set_status(f"[{idx}/{total}] جاري تحميل الصوت: {vid_title}", pct_base + (25 / total))
            add_log(f"[{idx}/{total}] ⬇ جاري تحميل الصوت: {vid_title}")

            for old_f in temp_dir.glob("*"):
                try:
                    old_f.unlink()
                except Exception:
                    pass

            dl_opts = {
                "format": "ba[ext=m4a]/ba[ext=webm]/ba",
                "outtmpl": str(temp_dir / "audio.%(ext)s"),
                "noplaylist": True,
                "quiet": True,
                "no_warnings": True,
            }
            with yt_dlp.YoutubeDL(dl_opts) as ydl:
                ydl.download([vid_url])

            downloaded_files = list(temp_dir.glob("audio.*"))
            if not downloaded_files:
                add_log(f"[{idx}/{total}] ❌ فشل تحميل الصوت للفيديو: {vid_title}")
                continue
            audio_file = downloaded_files[0]

            set_status(f"[{idx}/{total}] جاري التفريغ النصي بالذكاء الاصطناعي: {vid_title}", pct_base + (65 / total))

            try:
                segments = transcribe_speechmatics(audio_file, sm_key)
            except Exception as sm_err:
                add_log(f"[{idx}/{total}] ⚠ تنبيه Speechmatics ({sm_err}) — التحويل تلقائياً إلى Deepgram...")
                segments = transcribe_deepgram(audio_file, dg_key)

            save_outputs(segments, txt_path, srt_path, vid_title, playlist_title, vid_url)

            try:
                audio_file.unlink()
            except Exception:
                pass

            completed_count += 1
            add_log(f"[{idx}/{total}] ✔ تم الحفظ بنجاح: {txt_path.name}")

        try:
            temp_dir.rmdir()
        except Exception:
            pass

        set_status(f"✅ اكتمل بنجاح! تم تجهيز {completed_count} من {total} فيديو في مجلد سطح المكتب.", 100)
        add_log("🎉 انتهت العملية بالكامل! تم فتح مجلد الملفات على سطح المكتب.")
        open_folder_in_os(out_dir)

    except Exception as e:
        set_status(f"❌ حدث خطأ: {e}", 0)
        add_log(f"❌ خطأ: {e}")
    finally:
        STATE["is_running"] = False


# =====================================================================
# 5. ARABIC RTL FRONTEND (HTML/CSS + LOCAL SERVER)
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
    margin: 0; padding: 24px;
    background: #f4f6fb; color: #1f2937;
    display: flex; justify-content: center;
  }
  .container {
    width: 100%; max-width: 780px;
    background: #ffffff; border-radius: 16px;
    padding: 28px; box-shadow: 0 8px 24px rgba(0,0,0,0.07);
  }
  .header {
    display: flex; justify-content: space-between; align-items: center;
    border-bottom: 2px solid #f0f2f5; padding-bottom: 16px; margin-bottom: 22px;
  }
  .header h1 { margin: 0; font-size: 1.45rem; color: #111827; }
  .btn-secondary {
    background: #eef2ff; color: #3730a3; border: 1px solid #c7d2fe;
    padding: 8px 14px; border-radius: 8px; cursor: pointer; font-weight: 600; font-size: 0.95rem;
  }
  .btn-secondary:hover { background: #e0e7ff; }
  label { display: block; font-weight: 600; margin-bottom: 8px; font-size: 1.05rem; }
  input[type="text"] {
    width: 100%; padding: 14px 16px; font-size: 1.05rem;
    border: 2px solid #d1d5db; border-radius: 10px; outline: none;
    direction: ltr; text-align: left;
  }
  input[type="text"]:focus { border-color: #2563eb; }
  .btn-primary {
    width: 100%; margin-top: 16px; padding: 15px;
    background: #2563eb; color: #fff; border: none; border-radius: 10px;
    font-size: 1.15rem; font-weight: 700; cursor: pointer; transition: background 0.15s;
  }
  .btn-primary:hover:not(:disabled) { background: #1d4ed8; }
  .btn-primary:disabled { background: #93c5fd; cursor: not-allowed; }
  .progress-wrap {
    margin-top: 22px; background: #e5e7eb; border-radius: 99px;
    height: 16px; overflow: hidden;
  }
  .progress-bar {
    height: 100%; width: 0%; background: linear-gradient(90deg, #2563eb, #10b981);
    transition: width 0.3s ease;
  }
  .status-text {
    margin-top: 12px; font-weight: 600; font-size: 1rem; color: #1e40af;
  }
  .log-box {
    margin-top: 16px; background: #111827; color: #f9fafb;
    border-radius: 10px; padding: 14px; height: 210px;
    overflow-y: auto; font-size: 0.95rem; line-height: 1.7;
  }
  .log-item { border-bottom: 1px solid #1f2937; padding: 3px 0; }
  /* Modal */
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
    <h1>تحويل قوائم وفيديوهات يوتيوب إلى نصوص عربية</h1>
    <div style="display:flex; gap:8px;">
      <button class="btn-secondary" onclick="openFolder()">📁 فتح المجلد</button>
      <button class="btn-secondary" onclick="openSettings()">⚙ الإعدادات</button>
    </div>
  </div>

  <label for="urlInput">ضع رابط قائمة التشغيل (Playlist) أو الفيديو من يوتيوب هنا:</label>
  <input type="text" id="urlInput" placeholder="https://www.youtube.com/playlist?list=..." />

  <button id="startBtn" class="btn-primary" onclick="startJob()">ابدأ التفريغ النصي الآن</button>

  <div class="progress-wrap">
    <div id="progressBar" class="progress-bar"></div>
  </div>
  <div id="statusText" class="status-text">جاهز للبدء.</div>

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
    document.getElementById('statusText').textContent = data.status;
    document.getElementById('progressBar').style.width = data.progress + '%';
    const btn = document.getElementById('startBtn');
    btn.disabled = data.is_running;
    btn.textContent = data.is_running ? 'جاري العمل... يرجى الانتظار' : 'ابدأ التفريغ النصي الآن';

    if (data.logs.length !== lastLogCount) {
      const box = document.getElementById('logBox');
      box.innerHTML = data.logs.map(l => `<div class="log-item">${l}</div>`).join('');
      box.scrollTop = box.scrollHeight;
      lastLogCount = data.logs.length;
    }
  } catch (e) {}
}

setInterval(pollState, 1000);
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

async function openFolder() {
  await fetch('/api/open_folder', {method: 'POST'});
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
        pass  # Keep console clean

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
        elif self.path == "/api/settings":
            save_settings(data)
            self._send_json({"ok": True})
        elif self.path == "/api/open_folder":
            target = Path(STATE.get("last_output_dir", str(get_base_output_dir())))
            open_folder_in_os(target if target.exists() else get_base_output_dir())
            self._send_json({"ok": True})
        else:
            self.send_error(404)


def launch_app_window(url: str):
    """Launches a standalone desktop-style app window if Edge/Chrome/Brave/Chromium is available, else default browser."""
    if sys.platform.startswith("win"):
        try:
            subprocess.Popen(["cmd", "/c", "start", "", "msedge", f"--app={url}", "--window-size=860,680"])
            return
        except Exception:
            pass
    else:
        for browser_bin in ("chromium", "brave", "google-chrome-stable", "microsoft-edge"):
            if shutil.which(browser_bin):
                try:
                    subprocess.Popen([browser_bin, f"--app={url}", "--window-size=860,680"])
                    return
                except Exception:
                    pass
    webbrowser.open(url)


def main():
    STATE["last_output_dir"] = str(get_base_output_dir())

    # Bind to a free local port
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()

    server = HTTPServer(("127.0.0.1", port), AppHandler)
    url = f"http://127.0.0.1:{port}"
    print(f"App running at: {url}")

    threading.Thread(target=server.serve_forever, daemon=True).start()
    launch_app_window(url)

    # Keep alive while window is open or a transcription job is running
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
        server.shutdown()


if __name__ == "__main__":
    main()
