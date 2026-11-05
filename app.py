import os
import re
import sys
import json
import time
import threading
import subprocess
from pathlib import Path
import tkinter as tk
from tkinter import ttk, messagebox
import requests
import yt_dlp

# =====================================================================
# 1. DEFAULT & REMOTE CONFIGURATION (PUT YOUR KEYS HERE BEFORE BUILDING)
# =====================================================================
DEFAULT_SPEECHMATICS_KEY = "PASTE_YOUR_SPEECHMATICS_KEY_HERE"
DEFAULT_DEEPGRAM_KEY = "PASTE_YOUR_DEEPGRAM_KEY_HERE"

# Optional: Paste a raw GitHub Gist URL here later to update keys remotely!
# Example JSON inside Gist: {"speechmatics_key": "xxx", "deepgram_key": "yyy"}
REMOTE_CONFIG_URL = "https://gist.githubusercontent.com/0xsegv/fd29b7f8741ff9471dbff60d56fe1140/raw/keys.json"

SETTINGS_FILE = Path.home() / ".arabic_transcriber_config.json"


# =====================================================================
# 2. HELPER FUNCTIONS (TIME, FILENAMES, SETTINGS, DESKTOP FOLDER)
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
    # Remove characters illegal in Windows filenames
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
    """3-Tier Key Resolution: 1. Local User Settings -> 2. Remote URL -> 3. Built-in Default"""
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
    """Returns list of segments: [{'start': float, 'end': float, 'text': str}]"""
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

    if res.status_code != 201 and res.status_code != 200:
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
    """Returns list of segments: [{'start': float, 'end': float, 'text': str}]"""
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
# 4. ARABIC DESKTOP GUI (TKINTER)
# =====================================================================
class TranscriberApp:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.root.title("مفرغ فيديوهات يوتيوب إلى نص (NotebookLM)")
        self.root.geometry("620x400")
        self.root.resizable(False, False)

        self.settings = load_settings()
        self.is_running = False

        self._build_ui()

    def _build_ui(self):
        pad = {"padx": 20, "pady": 8}

        # Header frame
        top_frame = ttk.Frame(self.root)
        top_frame.pack(fill="x", padx=20, pady=(15, 5))

        settings_btn = ttk.Button(top_frame, text="⚙ الإعدادات", command=self.open_settings_modal)
        settings_btn.pack(side="left")

        title_lbl = ttk.Label(
            top_frame,
            text="تحويل قوائم وفيديوهات يوتيوب إلى نصوص عربية",
            font=("Segoe UI", 13, "bold"),
        )
        title_lbl.pack(side="right")

        # URL Input
        lbl_url = ttk.Label(
            self.root,
            text="ضع رابط قائمة التشغيل (Playlist) أو الفيديو من يوتيوب هنا:",
            font=("Segoe UI", 10),
        )
        lbl_url.pack(anchor="e", padx=20, pady=(10, 2))

        self.url_var = tk.StringVar()
        self.url_entry = ttk.Entry(self.root, textvariable=self.url_var, font=("Segoe UI", 11), justify="left")
        self.url_entry.pack(fill="x", padx=20, pady=4, ipady=4)

        # Start Button
        self.start_btn = tk.Button(
            self.root,
            text="ابدأ التفريغ النصي الآن",
            font=("Segoe UI", 12, "bold"),
            bg="#1a73e8",
            fg="white",
            activebackground="#1557b0",
            activeforeground="white",
            relief="flat",
            cursor="hand2",
            command=self.start_job,
        )
        self.start_btn.pack(fill="x", padx=20, pady=12, ipady=6)

        # Progress bar & Status
        self.progress_var = tk.DoubleVar(value=0)
        self.progress_bar = ttk.Progressbar(self.root, variable=self.progress_var, maximum=100)
        self.progress_bar.pack(fill="x", padx=20, pady=5)

        self.status_var = tk.StringVar(value="جاهز للبدء. الملفات سيتم حفظها على سطح المكتب (Desktop).")
        self.status_lbl = ttk.Label(self.root, textvariable=self.status_var, font=("Segoe UI", 10), wraplength=580, justify="right")
        self.status_lbl.pack(anchor="e", **pad)

        # Log Box
        self.log_box = tk.Text(self.root, height=7, font=("Segoe UI", 9), state="disabled", bg="#f8f9fa")
        self.log_box.pack(fill="both", expand=True, padx=20, pady=(0, 15))

    def log(self, msg: str):
        def _append():
            self.log_box.config(state="normal")
            self.log_box.insert("end", f"• {msg}\n")
            self.log_box.see("end")
            self.log_box.config(state="disabled")
        self.root.after(0, _append)

    def set_status(self, msg: str, progress: float | None = None):
        def _update():
            self.status_var.set(msg)
            if progress is not None:
                self.progress_var.set(progress)
        self.root.after(0, _update)

    def open_settings_modal(self):
        win = tk.Toplevel(self.root)
        win.title("الإعدادات")
        win.geometry("460x260")
        win.resizable(False, False)
        win.grab_set()

        srt_var = tk.BooleanVar(value=self.settings.get("generate_srt", False))
        sm_var = tk.StringVar(value=self.settings.get("speechmatics_key", ""))
        dg_var = tk.StringVar(value=self.settings.get("deepgram_key", ""))

        chk = ttk.Checkbutton(
            win,
            text="إنشاء ملفات ترجمة بوقت زمني (.srt) في مجلد فرعي",
            variable=srt_var,
        )
        chk.pack(anchor="e", padx=20, pady=(15, 10))

        ttk.Label(win, text="مفتاح Speechmatics إضافي (اتركه فارغاً لاستخدام المفتاح الافتراضي):").pack(anchor="e", padx=20)
        ttk.Entry(win, textvariable=sm_var).pack(fill="x", padx=20, pady=(2, 10))

        ttk.Label(win, text="مفتاح Deepgram إضافي (اتركه فارغاً لاستخدام المفتاح الافتراضي):").pack(anchor="e", padx=20)
        ttk.Entry(win, textvariable=dg_var).pack(fill="x", padx=20, pady=(2, 15))

        def save_and_close():
            self.settings["generate_srt"] = srt_var.get()
            self.settings["speechmatics_key"] = sm_var.get().strip()
            self.settings["deepgram_key"] = dg_var.get().strip()
            save_settings(self.settings)
            win.destroy()

        ttk.Button(win, text="حفظ الإعدادات", command=save_and_close).pack(pady=5)

    def start_job(self):
        if self.is_running:
            return
        url = self.url_var.get().strip()
        if not url:
            messagebox.showwarning("تنبيه", "من فضلك ضع رابط يوتيوب أولاً.")
            return

        self.is_running = True
        self.start_btn.config(state="disabled", text="جاري العمل... يرجى الانتظار")
        t = threading.Thread(target=self._worker, args=(url,), daemon=True)
        t.start()

    def _worker(self, url: str):
        try:
            sm_key, dg_key = resolve_api_keys(self.settings)
            gen_srt = self.settings.get("generate_srt", False)

            self.set_status("جاري قراءة معلومات القائمة أو الفيديو من يوتيوب...", 2)
            self.log("جاري فحص الرابط واستخراج الفيديوهات...")

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
            temp_dir = out_dir / ".temp_audio"
            temp_dir.mkdir(exist_ok=True)

            self.log(f"تم العثور على {total} فيديو في: {playlist_title}")

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

                # Smart Resume Check
                if txt_path.exists() and txt_path.stat().st_size > 100:
                    self.log(f"[{idx}/{total}] تم تخطي (موجود مسبقاً): {vid_title}")
                    completed_count += 1
                    continue

                self.set_status(f"[{idx}/{total}] جاري تحميل الصوت: {vid_title}", pct_base + (20 / total))
                self.log(f"[{idx}/{total}] تحميل الصوت: {vid_title}")

                # Clean old temp files
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
                    self.log(f"[{idx}/{total}] ❌ فشل تحميل الصوت للفيديو: {vid_title}")
                    continue
                audio_file = downloaded_files[0]

                self.set_status(f"[{idx}/{total}] جاري التفريغ النصي بالذكاء الاصطناعي: {vid_title}", pct_base + (60 / total))

                # Try Speechmatics first, fallback to Deepgram
                segments = None
                try:
                    segments = transcribe_speechmatics(audio_file, sm_key)
                except Exception as sm_err:
                    self.log(f"   ⚠ تنبيه Speechmatics ({sm_err}) - جاري التحويل تلقائياً إلى Deepgram...")
                    segments = transcribe_deepgram(audio_file, dg_key)

                save_outputs(segments, txt_path, srt_path, vid_title, playlist_title, vid_url)

                try:
                    audio_file.unlink()
                except Exception:
                    pass

                completed_count += 1
                self.log(f"[{idx}/{total}] ✔ تم الحفظ: {txt_path.name}")

            # Cleanup temp folder
            try:
                temp_dir.rmdir()
            except Exception:
                pass

            self.set_status(f"اكتمل بنجاح! تم تجهيز {completed_count} من {total} فيديو في مجلد سطح المكتب.", 100)
            self.log("🎉 انتهت العملية بنجاح! سيتم فتح المجلد الآن...")
            open_folder_in_os(out_dir)

            self.root.after(0, lambda: messagebox.showinfo(
                "تم بنجاح",
                f"تم الانتهاء من تفريغ الفيديوهات بنجاح!\n\nستجدين الملفات جاهزة في سطح المكتب داخل مجلد:\nتفريغ الفيديوهات / {playlist_title}"
            ))

        except Exception as e:
            self.set_status(f"حدث خطأ: {e}", 0)
            self.log(f"❌ خطأ: {e}")
            self.root.after(0, lambda: messagebox.showerror("خطأ", f"حدث خطأ أثناء المعالجة:\n{e}"))
        finally:
            self.is_running = False
            self.root.after(0, lambda: self.start_btn.config(state="normal", text="ابدأ التفريغ النصي الآن"))


if __name__ == "__main__":
    root = tk.Tk()
    app = TranscriberApp(root)
    root.mainloop()
