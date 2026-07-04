"""YouTube downloader — Flask-free logic, wrapping yt-dlp.

Ported from the original stdlib server. Kept self-contained inside the function
package: nothing here imports from the host or other functions.
"""
import os
import queue
import shutil
import subprocess
import threading
import urllib.parse
from datetime import datetime

import yt_dlp

# Codecs QuickTime Player can decode. 4K YouTube is only VP9/AV1, so a 2160p download
# needs re-encoding to HEVC before QuickTime will open it (see transcode_to_quicktime).
QUICKTIME_VCODECS = {"h264", "hevc"}

# Machine-local download dir. Overridable per-machine via MAGI_YOUTUBE_DIR so the
# unified host can boot on the mini (user wklin3) without reaching for a path under
# /Users/kai. NOTE: importing this module must never touch the filesystem — a crash
# here takes the whole host down. The dir is created lazily at download time
# (os.makedirs(dest, ...) in run_download), so there is no import-time makedirs.
DEFAULT_DOWNLOAD_DIR = os.environ.get(
    "MAGI_YOUTUBE_DIR", "/Users/kai/doc/_my_creations/saved stuffs"
)
METADATA_NAME = "video-links.txt"

# The host injects a resolver (the env-scoped `youtube_download_dir` setting) at startup;
# left None when the function runs standalone. See current_download_dir().
_dir_resolver = None


def set_download_dir_resolver(fn):
    """Let the host supply the active download dir (e.g. an env-scoped setting). `fn` is
    called with no args and may return a path or a falsy value (→ fall back)."""
    global _dir_resolver
    _dir_resolver = fn


def current_download_dir():
    """The active download directory. Precedence: host-injected resolver (the env-scoped
    setting) → MAGI_YOUTUBE_DIR env / hardcoded default (both baked into
    DEFAULT_DOWNLOAD_DIR). Never touches the filesystem (importing must stay side-effect free)."""
    if _dir_resolver is not None:
        try:
            chosen = (_dir_resolver() or "").strip()
        except Exception:  # noqa: BLE001
            chosen = ""
        if chosen:
            return chosen
    return DEFAULT_DOWNLOAD_DIR


def metadata_file(dest_dir=None):
    """Path to video-links.txt — it lives in the folder the videos land in."""
    return os.path.join(dest_dir or current_download_dir(), METADATA_NAME)


def has_ffmpeg():
    return shutil.which("ffmpeg") is not None


def has_hevc_encoder():
    """True if ffmpeg exposes Apple's hardware HEVC encoder (hevc_videotoolbox) — what the
    QuickTime-compatible re-encode uses. Cheap, cached-free; safe to call from health."""
    if not has_ffmpeg():
        return False
    try:
        out = subprocess.run(
            ["ffmpeg", "-hide_banner", "-encoders"],
            capture_output=True, text=True, timeout=15,
        )
        return "hevc_videotoolbox" in (out.stdout or "")
    except Exception:  # noqa: BLE001
        return False


def _video_codec(path):
    """First video stream's codec name (lowercase) via ffprobe, or '' if unknown."""
    try:
        out = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "v:0",
             "-show_entries", "stream=codec_name", "-of", "default=nw=1:nk=1", path],
            capture_output=True, text=True, timeout=30,
        )
        return (out.stdout or "").strip().lower()
    except Exception:  # noqa: BLE001
        return ""


def transcode_to_quicktime(path, duration=None, emit=None):
    """Re-encode `path` in place to HEVC (hardware-accelerated hevc_videotoolbox) so
    QuickTime Player can open it. No-op if the video is already H.264/HEVC (nothing to
    fix). On any failure the ORIGINAL file is kept — the download itself already
    succeeded, so a failed re-encode should degrade to "playable elsewhere", not error.

    `duration` (seconds) drives the progress percentage; `emit(event, data)` streams
    `recode` progress events to the SSE client. Returns `path` (same filename either way).
    """
    codec = _video_codec(path)
    if codec in QUICKTIME_VCODECS:
        return path  # already QuickTime-compatible — don't waste a lossy re-encode
    if emit:
        emit("recode", {"message": f"Re-encoding {codec or 'video'} → HEVC for QuickTime…",
                        "percent": 0})
    tmp = os.path.splitext(path)[0] + ".qt-tmp.mp4"
    cmd = [
        "ffmpeg", "-y", "-i", path,
        "-c:v", "hevc_videotoolbox", "-q:v", "60", "-tag:v", "hvc1",  # hvc1 tag = QuickTime-friendly
        "-c:a", "aac", "-b:a", "192k",                                # Opus/AAC → AAC (QuickTime audio)
        "-movflags", "+faststart",
        "-progress", "pipe:1", "-nostats", "-loglevel", "error",
        tmp,
    ]
    proc = None
    try:
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        total = float(duration or 0)
        for line in proc.stdout:  # -progress emits key=value lines periodically
            if emit and total and line.startswith("out_time="):
                ts = line.split("=", 1)[1].strip()  # HH:MM:SS.microseconds
                try:
                    h, m, s = ts.split(":")
                    pct = max(0.0, min(100.0, (int(h) * 3600 + int(m) * 60 + float(s)) / total * 100))
                    emit("recode", {"percent": pct})
                except ValueError:
                    pass  # "N/A" before the first frame
        proc.wait()
        if proc.returncode == 0 and os.path.isfile(tmp) and os.path.getsize(tmp) > 0:
            os.replace(tmp, path)  # same filename, now HEVC → QuickTime opens it
            return path
        err = (proc.stderr.read() if proc.stderr else "") or f"ffmpeg exit {proc.returncode}"
        raise RuntimeError(err.strip()[:200])
    except Exception as e:  # noqa: BLE001
        if emit:
            emit("stage", {"message": f"⚠️ HEVC re-encode failed ({e}); kept the original file"})
        return path
    finally:
        if proc and proc.stderr:
            proc.stderr.close()
        if os.path.exists(tmp):
            try:
                os.remove(tmp)
            except OSError:
                pass


def append_metadata(url, title, stem, dest_dir=None):
    """Append one entry to video-links.txt (in the download dir), matching its existing
    format: `YYYY/MM/DD: <slug> <url> <title>`. A blank line is inserted whenever the
    date changes, so entries stay grouped by day."""
    meta_file = metadata_file(dest_dir)
    today = datetime.now().strftime("%Y/%m/%d")
    line = f"{today}: {stem} {url} {title}".rstrip()

    lead = ""
    if os.path.exists(meta_file) and os.path.getsize(meta_file) > 0:
        with open(meta_file, "r", encoding="utf-8") as fh:
            content = fh.read()
        if not content.endswith("\n"):
            lead = "\n"  # finish the current line first
        last_line = next((ln.strip() for ln in reversed(content.splitlines()) if ln.strip()), "")
        head = last_line.split(":", 1)[0].strip()
        is_date = len(head) == 10 and head[4] == "/" and head[7] == "/"
        if is_date and head != today:
            lead += "\n"  # blank line between different days
    with open(meta_file, "a", encoding="utf-8") as fh:
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


def run_download(*, url, format_id, kind, audio_mp3, date_prefix, write_meta, dest, quicktime=False):
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
            # Big single-file DASH streams (e.g. 2160p, 1+ GiB) make YouTube drop the
            # connection mid-transfer → "N bytes read, M more expected" (IncompleteRead).
            # Pull each stream in bounded ranged chunks so a dropped connection only loses
            # the current chunk and resumes at the right offset instead of aborting the whole
            # download. On a flaky path YouTube can drop nearly every chunk, so a small retry
            # budget gets exhausted on one bad chunk — retry indefinitely (matching yt-dlp's
            # `--retries infinite`); socket_timeout bounds each stalled read so a retry always
            # makes progress rather than hanging silently. Verified: full 1.09 GiB 2160p pull.
            "http_chunk_size": 10 * 1024 * 1024,  # 10 MiB
            "retries": float("inf"),
            "fragment_retries": float("inf"),
            "file_access_retries": 10,
            "socket_timeout": 30,
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

            # Optional: re-encode VP9/AV1 (e.g. 4K, which QuickTime can't decode) to HEVC so
            # QuickTime opens it. In place, same filename; a no-op for already-H.264/HEVC files.
            if quicktime and not audio_mp3 and has_ffmpeg():
                filepath = transcode_to_quicktime(filepath, duration=info.get("duration"), emit=emit)
            fname = os.path.basename(filepath)

            meta_line = None
            if write_meta:
                try:
                    stem = os.path.splitext(fname)[0]
                    meta_line = append_metadata(info.get("webpage_url") or url, info.get("title") or "", stem, dest_dir=dest)
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
