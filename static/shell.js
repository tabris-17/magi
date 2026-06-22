/* =====================================================================
   magi shell — theme + mobile drawer + appearance + common settings sync
   Loaded on every page (host + mounted functions).

   Theme persistence: the host DB (data/magi.db, via /api/settings) is the
   source of truth for common settings; localStorage is a no-flash cache used
   by the pre-paint script. On load we reconcile the cache with the DB; on
   change we write through to the DB. The version label is filled from the
   same endpoint.
   ===================================================================== */
(function () {
  "use strict";

  const THEME_KEY = "magi-theme";
  const $ = (id) => document.getElementById(id);

  function getThemePref() { return localStorage.getItem(THEME_KEY) || "dark"; }
  function resolveTheme(pref) {
    return pref === "system"
      ? (matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light")
      : pref;
  }
  function applyTheme(pref, persist) {
    localStorage.setItem(THEME_KEY, pref);                 // no-flash cache
    const resolved = resolveTheme(pref);
    document.documentElement.setAttribute("data-theme", resolved);
    const meta = document.querySelector('meta[name="theme-color"]');
    if (meta) meta.setAttribute("content", resolved === "dark" ? "#0d1117" : "#ffffff");
    markCards(pref);
    if (persist) {
      fetch("/api/settings", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ key: "theme", value: pref }),
      }).catch(() => {});                                   // best-effort write-through
    }
  }
  // Re-resolve when the OS theme changes and the user is on "system".
  matchMedia("(prefers-color-scheme: dark)").addEventListener("change", () => {
    if (getThemePref() === "system") applyTheme("system", false);
  });

  // ---- appearance cards (only on /settings) ----
  function markCards(pref) {
    document.querySelectorAll("[data-theme-value]").forEach(
      (c) => c.classList.toggle("selected", c.getAttribute("data-theme-value") === pref));
  }
  function initAppearance() {
    const cards = document.querySelectorAll("[data-theme-value]");
    if (!cards.length) return;
    markCards(getThemePref());
    cards.forEach((btn) =>
      btn.addEventListener("click", () => applyTheme(btn.getAttribute("data-theme-value"), true)));
  }

  // ---- reconcile cache + version label from the host DB ----
  function syncFromServer() {
    fetch("/api/settings", { cache: "no-store" })
      .then((r) => r.json())
      .then((d) => {
        const v = $("magiVersion");
        if (v && d.version) v.textContent = d.version;
        const dbTheme = d.settings && d.settings.theme;
        if (dbTheme && dbTheme !== getThemePref()) applyTheme(dbTheme, false);  // cache was stale
      })
      .catch(() => {});
  }

  // ---- mobile drawer ----
  function openDrawer() { $("magiSidebar").classList.add("open"); $("magiBackdrop").classList.add("open"); }
  function closeDrawer() { $("magiSidebar").classList.remove("open"); $("magiBackdrop").classList.remove("open"); }

  document.addEventListener("DOMContentLoaded", () => {
    const menuBtn = $("magiMenuBtn"), backdrop = $("magiBackdrop");
    if (menuBtn) menuBtn.addEventListener("click", openDrawer);
    if (backdrop) backdrop.addEventListener("click", closeDrawer);
    initAppearance();
    syncFromServer();
  });

  window.magi = { applyTheme, getThemePref, closeDrawer };
})();
