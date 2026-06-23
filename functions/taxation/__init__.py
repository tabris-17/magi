"""Taxation function — a self-contained Flask blueprint.

Look up the RBA daily exchange rate (A$1 = USD/GBP/HKD) for a given date — for converting
foreign income to AUD at tax time. Exposes:
  bp    : the Flask blueprint (mounted by the host under /taxation)
  META  : sidebar/dashboard metadata + optional health/settings_section the host reads
"""
from flask import Blueprint, jsonify, render_template, request
from markupsafe import escape

from . import logic

bp = Blueprint("taxation", __name__, url_prefix="/taxation", template_folder="templates")

ICON = (
    '<svg width="16" height="16" viewBox="0 0 16 16"><path d="M1.75 1h12.5c.966 0 1.75.784 '
    '1.75 1.75v10.5A1.75 1.75 0 0 1 14.25 15H1.75A1.75 1.75 0 0 1 0 13.25V2.75C0 1.784.784 1 '
    '1.75 1ZM1.5 2.75v10.5c0 .138.112.25.25.25h12.5a.25.25 0 0 0 .25-.25V2.75a.25.25 0 0 '
    '0-.25-.25H1.75a.25.25 0 0 0-.25.25ZM4 5.25A.75.75 0 0 1 4.75 4.5h6.5a.75.75 0 0 1 0 '
    '1.5h-6.5A.75.75 0 0 1 4 5.25Zm0 3A.75.75 0 0 1 4.75 7.5h6.5a.75.75 0 0 1 0 1.5h-6.5A.75.75 '
    '0 0 1 4 8.25Zm0 3a.75.75 0 0 1 .75-.75h3.5a.75.75 0 0 1 0 1.5h-3.5a.75.75 0 0 1-.75-.75Z"/></svg>'
)

META = {
    "key": "taxation",
    "label": "Taxation",
    "description": "RBA daily FX rates (A$1 = USD/GBP/HKD) for any date — for AUD tax conversions.",
    "icon": ICON,
    "url": "/taxation/",
    "version": "tax-1.0.0",
}


@bp.route("/")
def page():
    return render_template("taxation/page.html", active="taxation")


@bp.route("/api/rates")
def api_rates():
    date = (request.args.get("date") or "").strip()
    if not date:
        return jsonify(error="Missing date (YYYY-MM-DD)"), 400
    try:
        return jsonify(logic.rates_for(date))
    except ValueError as e:
        return jsonify(error=str(e)), 400
    except Exception as e:  # noqa: BLE001
        return jsonify(error=f"Could not load RBA data: {e}"), 502


@bp.route("/api/refresh", methods=["POST"])
def api_refresh():
    try:
        logic.refresh()
        return jsonify(ok=True, **logic.status())
    except Exception as e:  # noqa: BLE001
        return jsonify(ok=False, error=str(e)), 502


@bp.route("/api/health")
def api_health():
    return jsonify(logic.status())


# ---- function contract: health + settings_section the host aggregates ----------------

def health_payload():
    """Function health for the host's aggregated Health page (no network — cache snapshot)."""
    return logic.status()


def settings_section():
    """Editable RBA source URL, persisted in the host DB (key `taxation_rba_url`) via the
    shared /api/settings endpoint — the host owns the storage; we own the presentation."""
    url = escape(logic.current_rba_url())
    html = f"""
<p class="lead">The source spreadsheet the Taxation function downloads and parses
(RBA historical daily exchange rates). Stored in magi's settings DB.</p>
<div class="tax-setting">
  <input type="url" id="taxRbaUrl" class="tax-url-input" value="{url}" spellcheck="false"
         autocapitalize="off" placeholder="https://www.rba.gov.au/…/2023-current.xls" />
  <button class="btn-env-save" type="button" id="taxRbaSave">Save</button>
  <span class="env-status" id="taxRbaStatus" aria-live="polite"></span>
</div>
<script>
  (function () {{
    var input = document.getElementById("taxRbaUrl");
    var btn = document.getElementById("taxRbaSave");
    var status = document.getElementById("taxRbaStatus");
    function save() {{
      btn.disabled = true; status.textContent = "saving…";
      fetch("/api/settings", {{
        method: "POST", headers: {{ "Content-Type": "application/json" }},
        body: JSON.stringify({{ key: "taxation_rba_url", value: input.value.trim() }}),
      }})
        .then(function (r) {{ return r.json().then(function (d) {{ return {{ ok: r.ok, d: d }}; }}); }})
        .then(function (res) {{ status.textContent = res.ok ? "saved ✓" : (res.d.error || "error"); }})
        .catch(function () {{ status.textContent = "error"; }})
        .finally(function () {{ btn.disabled = false; setTimeout(function () {{ status.textContent = ""; }}, 2500); }});
    }}
    btn.addEventListener("click", save);
    input.addEventListener("keydown", function (e) {{ if (e.key === "Enter") save(); }});
  }})();
</script>
"""
    return {"id": "taxation", "label": "Taxation", "html": html}


META["health"] = health_payload
META["settings_section"] = settings_section
