"""Polaris function — a self-contained Flask blueprint (mounted by the host at /polaris).

A personal journal. An entry is a title, a Markdown body, attachments, and any number of
tags (defined on the /polaris/tags sub-page) — all in this function's own SQLite DB.
Never imports the host.

  bp    : the Flask blueprint
  META  : sidebar/dashboard metadata + the `health` callable the host aggregates
"""
import io
import os
import zipfile

from flask import Blueprint, jsonify, render_template, request, send_file

from . import logic

bp = Blueprint("polaris", __name__, url_prefix="/polaris", template_folder="templates")

# octicons book-16
ICON = (
    '<svg width="16" height="16" viewBox="0 0 16 16"><path d="M0 1.75A.75.75 0 0 1 .75 1h4.253'
    'c1.227 0 2.317.59 3 1.501A3.743 3.743 0 0 1 11.006 1h4.245a.75.75 0 0 1 .75.75v10.5a.75.75 '
    '0 0 1-.75.75h-4.507a2.25 2.25 0 0 0-1.591.659l-.622.621a.75.75 0 0 1-1.06 0l-.622-.621A2.25 '
    '2.25 0 0 0 5.258 13H.75a.75.75 0 0 1-.75-.75Zm7.251 10.324.004-5.073-.002-2.253A2.25 2.25 0 '
    '0 0 0 5.003 2.5H1.5v9h3.757a3.75 3.75 0 0 1 1.994.574ZM8.755 4.75l-.004 7.322a3.752 3.752 0 '
    '0 1 1.992-.572H14.5v-9h-3.495a2.25 2.25 0 0 0-2.25 2.25Z"/></svg>'
)

META = {
    "key": "polaris",
    "label": "Polaris",
    "description": "Your journal — dated entries with attachments, kept locally.",
    "icon": ICON,
    "url": "/polaris/",
    "version": "polaris-1.9.1",
    # Sidebar sub-pages (rendered by base.html's generic subnav loop, collapse-when-active,
    # like the Settings groups). `key` values are what the pages pass as `active`.
    "subnav": [
        {"key": "polaris", "label": "Journal", "url": "/polaris/",
         "icon": '<svg width="16" height="16" viewBox="0 0 16 16"><path d="M11.013 1.427a1.75 1.75 0 0 1 2.474 0l1.086 1.086a1.75 1.75 0 0 1 0 2.474l-8.61 8.61c-.21.21-.47.364-.756.445l-3.251.93a.75.75 0 0 1-.927-.928l.929-3.25c.081-.286.235-.547.445-.758l8.61-8.61Zm.176 4.823L9.75 4.81l-6.286 6.287a.253.253 0 0 0-.064.108l-.558 1.953 1.953-.558a.253.253 0 0 0 .108-.064Zm1.238-3.763a.25.25 0 0 0-.354 0L10.811 3.75l1.439 1.44 1.263-1.263a.25.25 0 0 0 0-.354Z"/></svg>'},
        {"key": "polaris-tags", "label": "Tags", "url": "/polaris/tags",
         "icon": '<svg width="16" height="16" viewBox="0 0 16 16"><path d="M1 7.775V2.75C1 1.784 1.784 1 2.75 1h5.025c.464 0 .91.184 1.238.513l6.25 6.25a1.75 1.75 0 0 1 0 2.474l-5.026 5.026a1.75 1.75 0 0 1-2.474 0l-6.25-6.25A1.752 1.752 0 0 1 1 7.775Zm1.5 0c0 .066.026.13.073.177l6.25 6.25a.25.25 0 0 0 .354 0l5.025-5.025a.25.25 0 0 0 0-.354l-6.25-6.25a.25.25 0 0 0-.177-.073H2.75a.25.25 0 0 0-.25.25ZM6 5a1 1 0 1 1 0 2 1 1 0 0 1 0-2Z"/></svg>'},
    ],
}


@bp.route("/")
def page():
    return render_template("polaris/page.html", active="polaris")


@bp.route("/tags")
def tags_page():
    return render_template("polaris/tags.html", active="polaris-tags")


@bp.route("/api/tags", methods=["GET", "POST"])
def api_tags():
    if request.method == "POST":
        data = request.get_json(silent=True) or {}
        try:
            tag = logic.create_tag(data.get("name", ""), data.get("emoji", ""))
        except ValueError as exc:
            return jsonify(error=str(exc)), 400
        return jsonify(tag=tag)
    return jsonify(tags=logic.list_tags())


@bp.route("/api/tags/<int:tag_id>", methods=["POST", "DELETE"])
def api_tag(tag_id):
    if request.method == "DELETE":
        if not logic.delete_tag(tag_id):
            return jsonify(error="not found"), 404
        return jsonify(ok=True)
    data = request.get_json(silent=True) or {}
    try:
        # partial update: only the provided fields change
        logic.update_tag(tag_id,
                         name=data.get("name") if "name" in data else None,
                         emoji=data.get("emoji") if "emoji" in data else None)
    except ValueError as exc:
        return jsonify(error=str(exc)), 400
    except KeyError as exc:
        return jsonify(error=exc.args[0]), 404
    return jsonify(ok=True)


@bp.route("/api/entries", methods=["GET", "POST"])
def api_entries():
    if request.method == "POST":
        data = request.get_json(silent=True) or {}
        try:
            entry = logic.save_entry(
                entry_id=data.get("id"),
                date=data.get("date"),
                title=data.get("title", ""),
                body=data.get("body", ""),
                reminder=data.get("reminder", ""),
            )
        except KeyError as exc:
            return jsonify(error=exc.args[0]), 404
        if "tags" in data:  # optional: replace the entry's tag set alongside the save
            entry["tags"] = logic.set_entry_tags(entry["id"], data["tags"])
        return jsonify(entry=entry)
    entries = logic.list_entries(query=(request.args.get("q") or "").strip())
    return jsonify(entries=entries)


@bp.route("/api/entries/<int:entry_id>", methods=["GET", "DELETE"])
def api_entry(entry_id):
    if request.method == "DELETE":
        if not logic.delete_entry(entry_id):
            return jsonify(error="not found"), 404
        return jsonify(ok=True)
    entry = logic.get_entry(entry_id)
    if not entry:
        return jsonify(error="not found"), 404
    return jsonify(entry=entry)


@bp.route("/api/entries/<int:entry_id>/attachments", methods=["POST"])
def api_attach(entry_id):
    f = request.files.get("file")
    if not f or not f.filename:
        return jsonify(error="no file"), 400
    data = f.read()
    if not data:
        return jsonify(error="empty file"), 400
    if len(data) > logic.ATTACH_MAX_BYTES:
        return jsonify(error=f"file exceeds {logic.ATTACH_MAX_BYTES // (1024*1024)}MB"), 413
    try:
        att = logic.add_attachment(entry_id, f.filename, f.mimetype or "", data)
    except KeyError as exc:
        return jsonify(error=exc.args[0]), 404
    return jsonify(attachment=att)


@bp.route("/api/entries/<int:entry_id>/attachments.zip")
def api_attachments_zip(entry_id):
    """Bundle every attachment of an entry into a single download. Filenames are
    basename-only (no stored path escapes the archive) and de-duplicated in place."""
    if not logic.get_entry(entry_id):
        return jsonify(error="not found"), 404
    blobs = logic.attachment_blobs(entry_id)
    if not blobs:
        return jsonify(error="no attachments"), 404
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        seen = {}
        for filename, _mime, data in blobs:
            base = os.path.basename(filename or "") or "file"
            n = seen.get(base, 0)
            seen[base] = n + 1
            if n:                                   # 2nd "photo.png" → "photo (1).png"
                stem, dot, ext = base.rpartition(".")
                base = f"{stem} ({n}){dot}{ext}" if dot else f"{base} ({n})"
            z.writestr(base, data)
    buf.seek(0)
    resp = send_file(buf, mimetype="application/zip", as_attachment=True,
                     download_name=f"polaris-{entry_id}-attachments.zip")
    resp.headers["X-Content-Type-Options"] = "nosniff"
    return resp


@bp.route("/api/attachments/<int:att_id>", methods=["DELETE"])
def api_attachment_delete(att_id):
    if not logic.delete_attachment(att_id):
        return jsonify(error="not found"), 404
    return jsonify(ok=True)


@bp.route("/media/<int:att_id>")
def media(att_id):
    """Serve an attachment. Only known-safe image types render inline; anything else
    downloads, so a stored SVG/HTML can never execute script in this origin."""
    got = logic.get_attachment(att_id)
    if not got:
        return jsonify(error="not found"), 404
    filename, mime, data = got
    inline = mime in logic.INLINE_MIMES
    resp = send_file(io.BytesIO(data), mimetype=mime or "application/octet-stream",
                     as_attachment=not inline, download_name=filename,
                     conditional=False, etag=str(att_id))
    resp.headers["X-Content-Type-Options"] = "nosniff"
    resp.headers["Cache-Control"] = "private, max-age=31536000, immutable"
    return resp


@bp.route("/api/health")
def api_health():
    return jsonify(logic.status())


def health_payload():
    """Function health for the host's aggregated Health page (no network)."""
    return logic.status()


META["health"] = health_payload
