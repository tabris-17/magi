"""YouTube downloader function — a self-contained Flask blueprint.

Exposes:
  bp    : the Flask blueprint (mounted by the host under /youtube)
  META  : sidebar/dashboard metadata the host reads to build the shell
"""
import json
import os
import urllib.parse

from flask import (
    Blueprint, Response, abort, jsonify, render_template, request,
    send_file, stream_with_context,
)

from . import logic

bp = Blueprint("youtube", __name__, url_prefix="/youtube", template_folder="templates")

ICON = (
    '<svg width="16" height="16" viewBox="0 0 16 16"><path d="M16 3.75v8.5a.75.75 0 0 '
    '1-1.136.643L11 10.575v.675A1.75 1.75 0 0 1 9.25 13h-7.5A1.75 1.75 0 0 1 0 '
    '11.25v-6.5C0 3.784.784 3 1.75 3h7.5c.966 0 1.75.784 1.75 1.75v.675l3.864-2.318A.75.75 '
    '0 0 1 16 3.75Z"/></svg>'
)

META = {
    "key": "youtube",
    "label": "YouTube Downloader",
    "description": "Fetch every available format and download videos or audio locally.",
    "icon": ICON,
    "url": "/youtube/",
    "version": "yd-1.0.0",
}


@bp.route("/")
def page():
    return render_template("youtube/page.html", active="youtube")


@bp.route("/api/health")
def health():
    return jsonify(
        ffmpeg=logic.has_ffmpeg(),
        default_dir=logic.DEFAULT_DOWNLOAD_DIR,
        metadata_file=logic.METADATA_FILE,
    )


@bp.route("/api/info")
def info():
    url = (request.args.get("url") or "").strip()
    if not url:
        return jsonify(error="Missing url"), 400
    try:
        return jsonify(logic.extract_info(url))
    except Exception as e:  # noqa: BLE001
        return jsonify(error=str(e)), 500


@bp.route("/api/download")
def download():
    a = request.args
    url = (a.get("url") or "").strip()
    if not url:
        return jsonify(error="Missing url"), 400
    dest = os.path.expanduser((a.get("dest") or logic.DEFAULT_DOWNLOAD_DIR).strip() or logic.DEFAULT_DOWNLOAD_DIR)
    params = dict(
        url=url,
        format_id=(a.get("format_id") or "").strip(),
        kind=(a.get("kind") or "both").strip(),
        audio_mp3=(a.get("audio_mp3") or "0") == "1",
        date_prefix=(a.get("date_prefix") or "1") == "1",
        write_meta=(a.get("metadata") or "1") == "1",
        dest=dest,
    )

    def sse():
        for event, data in logic.run_download(**params):
            yield f"event: {event}\ndata: {json.dumps(data)}\n\n"

    return Response(
        stream_with_context(sse()),
        mimetype="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@bp.route("/files/<path:name>")
def files(name):
    safe = os.path.basename(urllib.parse.unquote(name))
    base_dir = os.path.expanduser(request.args.get("dir", logic.DEFAULT_DOWNLOAD_DIR))
    full = os.path.join(base_dir, safe)
    if os.path.isfile(full):
        return send_file(full, as_attachment=True, download_name=safe)
    abort(404)
