"""YouTube downloader — Flask-free logic, wrapping yt-dlp.

Ported from the original stdlib server. Kept self-contained inside the function
package: nothing here imports from the host or other functions.
"""
import os
import queue
import shutil
import threading
import urllib.parse
from datetime import datetime

import yt_dlp

DEFAULT_DOWNLOAD_DIR = "/Users/kai/doc/_my_creations/saved stuffs"
METADATA_FILE = os.path.join(DEFAULT_DOWNLOAD_DIR, "video-links.txt")

os.makedirs(DEFAULT_DOWNLOAD_DIR, exist_ok=True)


def has_ffmpeg():
    return shutil.which("ffmpeg") is not None


def append_metadata(url, title, stem):
    """Append one entry to video-links.txt, matching its existing format:
    `YYYY/MM/DD: <slug> <url> <title>`. A blank line is inserted whenever the
    date changes, so entries stay grouped by day."""
    today = datetime.now().strftime("%Y/%m/%d")
    line = f"{today}: {stem} {url} {title}".rstrip()

    lead = ""
    if os.path.exists(METADATA_FILE) and os.path.getsize(METADATA_FILE) > 0:
        with open(METADATA_FILE, "r", encoding="utf-8") as fh:
            content = fh.read()
        if not content.endswith("\n"):
            lead = "\n"  # finish the current line first
        last_line = next((ln.strip() for ln in reversed(content.splitlines()) if ln.strip()), "")
        head = last_line.split(":", 1)[0].strip()
        is_date = len(head) == 10 and head[4] == "/" and head[7] == "/"
        if is_date and head != today:
            lead += "\n"  # blank line between different days
    with open(METADATA_FILE, "a", encoding="utf-8") as fh:
        fh.write(lead + line + "\n")
    return line


def human_size(num):
    if not num:
        return None
    for unit in ["B", "KB", "MB", "GB", "TB"]:
        if abs(num) < 1024.0:
            return f"{num:3.1f} {unit}"
        num /= 1024.0
    return f"{num:.1f} PB"


def extract_info(url):
    """Return a trimmed, UI-friendly description of every available format."""
    opts = {"quiet": True, "no_warnings": True, "noplaylist": True, "skip_download": True}
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=False)

    formats = []
    for f in info.get("formats", []):
        vcodec = f.get("vcodec")
        acodec = f.get("acodec")
        has_video = vcodec and vcodec != "none"
        has_audio = acodec and acodec != "none"
        if not has_video and not has_audio:
            continue  # storyboards / images

        if has_video and has_audio:
            kind = "both"
        elif has_video:
            kind = "video"
        else:
            kind = "audio"

        size = f.get("filesize") or f.get("filesize_approx")
        formats.append({
            "format_id": f.get("format_id"),
            "ext": f.get("ext"),
            "kind": kind,
            "resolution": f.get("resolution")
            or (f"{f.get('width')}x{f.get('height')}" if f.get("height") else None),
            "height": f.get("height") or 0,
            "fps": f.get("fps"),
            "vcodec": None if vcodec == "none" else vcodec,
            "acodec": None if acodec == "none" else acodec,
            "abr": f.get("abr"),
            "tbr": f.get("tbr"),
            "filesize": size,
            "filesize_human": human_size(size),
            "note": f.get("format_note"),
        })

    order = {"both": 0, "video": 1, "audio": 2}
    formats.sort(key=lambda x: (order.get(x["kind"], 3), -(x["height"] or 0), -(x["tbr"] or 0)))

    return {
        "id": info.get("id"),
        "title": info.get("title"),
        "uploader": info.get("uploader"),
        "duration": info.get("duration"),
        "duration_string": info.get("duration_string"),
        "thumbnail": info.get("thumbnail"),
        "webpage_url": info.get("webpage_url"),
        "formats": formats,
    }


def build_format_string(format_id, kind, audio_only_mp3):
    """Map a chosen format to a yt-dlp format selector."""
    if audio_only_mp3:
        return "bestaudio/best"
    if kind == "video":
        return f"{format_id}+bestaudio/best"  # video-only -> merge with best audio (needs ffmpeg)
    return format_id  # progressive (both) or audio-only as-is


def run_download(*, url, format_id, kind, audio_mp3, date_prefix, write_meta, dest):
    """Run a download in a worker thread, yielding (event, data) tuples for SSE.

    Events: progress, stage, done, error. The caller turns these into SSE frames.
    """
    events = queue.Queue()

    def emit(event, data):
        events.put((event, data))

    def hook(d):
        if d["status"] == "downloading":
            total = d.get("total_bytes") or d.get("total_bytes_estimate")
            emit("progress", {
                "downloaded": d.get("downloaded_bytes"),
                "total": total,
                "percent": (d.get("downloaded_bytes", 0) / total * 100) if total else None,
                "speed": d.get("speed"),
                "eta": d.get("eta"),
                "filename": os.path.basename(d.get("filename") or ""),
            })
        elif d["status"] == "finished":
            emit("stage", {"message": "Download finished, processing/merging…"})

    def worker():
        fmt = build_format_string(format_id, kind, audio_mp3)
        prefix = datetime.now().strftime("%Y%m%d-") if date_prefix else ""
        os.makedirs(dest, exist_ok=True)
        outtmpl = os.path.join(dest, prefix + "%(title)s [%(id)s].%(ext)s")
        opts = {
            "quiet": True,
            "no_warnings": True,
            "noplaylist": True,
            "format": fmt,
            "outtmpl": outtmpl,
            "progress_hooks": [hook],
            "merge_output_format": "mp4",
        }
        if audio_mp3:
            opts["postprocessors"] = [{
                "key": "FFmpegExtractAudio",
                "preferredcodec": "mp3",
                "preferredquality": "192",
            }]
        try:
            with yt_dlp.YoutubeDL(opts) as ydl:
                info = ydl.extract_info(url, download=True)
            req = (info.get("requested_downloads") or [{}])[0]
            filepath = req.get("filepath") or ydl.prepare_filename(info)
            fname = os.path.basename(filepath)

            meta_line = None
            if write_meta:
                try:
                    stem = os.path.splitext(fname)[0]
                    meta_line = append_metadata(info.get("webpage_url") or url, info.get("title") or "", stem)
                except Exception as me:  # noqa: BLE001
                    meta_line = f"⚠️ metadata write failed: {me}"

            emit("done", {
                "filename": fname,
                "path": filepath,
                "dir": dest,
                "url": "/youtube/files/" + urllib.parse.quote(fname) + "?dir=" + urllib.parse.quote(dest),
                "metadata": meta_line,
                "size_human": human_size(os.path.getsize(filepath) if os.path.isfile(filepath) else None),
            })
        except Exception as e:  # noqa: BLE001
            emit("error", {"message": str(e)})
        finally:
            emit("__end__", None)

    threading.Thread(target=worker, daemon=True).start()

    while True:
        event, data = events.get()
        if event == "__end__":
            break
        yield event, data
