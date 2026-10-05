"use strict";
// Settings page: the config form plus cookies / Jellyfin / yt-dlp cards.
(() => {
  const { h, $, get, post, put, del, api, busy, fullTime } = YT;
  const form = $("#settings-form");
  // Exactly the keys of config.DEFAULT_SETTINGS; read-only info keys are never sent back.
  const KEYS = [
    "download_subfolder", "poll_interval_minutes", "quality", "auto_delete_days", "skip_shorts",
    "shorts_max_seconds", "skip_live", "max_concurrent_downloads", "retry_failed_after_hours",
    "max_attempts", "min_free_gb", "write_nfo", "pipeline_paused",
  ];
  const isBool = (el) => el.type === "checkbox";

  function fill(settings) {
    for (const key of KEYS) {
      const el = form.elements[key];
      if (!el || settings[key] === undefined) continue;
      if (isBool(el)) el.checked = settings[key] === true || String(settings[key]).toLowerCase() === "true";
      else el.value = settings[key];
    }
    const jf = settings.jellyfin_configured
      ? `Configured${settings.jellyfin_url ? " at " + settings.jellyfin_url : ""}. Library path: ${settings.library_path || "unknown"}`
      : "Not configured (JELLYFIN_API_KEY is not set).";
    $("#jf-info").textContent = jf;
  }

  function clearErrors() {
    $("#settings-error").hidden = true;
    form.querySelectorAll(".field-error").forEach((e) => {
      e.hidden = true;
      e.textContent = "";
    });
    form.querySelectorAll(".invalid").forEach((e) => e.classList.remove("invalid"));
  }

  // The API reports {detail} as one message; attach it to the field it names, else show it on top.
  function showError(message) {
    const key = KEYS.find((k) => message.includes(k));
    const slot = key && form.querySelector(`[data-error-for="${key}"]`);
    if (slot) {
      slot.textContent = message;
      slot.hidden = false;
      form.elements[key].classList.add("invalid");
      form.elements[key].focus();
    } else {
      const box = $("#settings-error");
      box.textContent = message;
      box.hidden = false;
    }
  }

  form.addEventListener("submit", async (e) => {
    e.preventDefault();
    clearErrors();
    const values = {};
    for (const key of KEYS) {
      const el = form.elements[key];
      values[key] = isBool(el) ? (el.checked ? "true" : "false") : el.value.trim();
    }
    try {
      fill(await put("/api/settings", values));
      YT.toast("Settings saved");
      YT.refreshBanner && YT.refreshBanner();
    } catch (err) {
      showError(err.message);
    }
  });

  async function loadCookies() {
    const box = $("#cookies-status");
    try {
      const c = await get("/api/cookies");
      const lines = [h("span", { class: "badge " + (c.present ? "ok" : ""), text: c.present ? "Present" : "None" }), " ", c.detail || ""];
      if (c.present) {
        lines.push(h("div", { class: "small", text: `${c.cookie_count} cookies, ${c.youtube_cookie_count} for YouTube` }));
        if (c.expires_at) lines.push(h("div", { class: "small", text: "Earliest expiry: " + fullTime(c.expires_at) }));
        if (c.modified_at) lines.push(h("div", { class: "small", text: "Uploaded: " + fullTime(c.modified_at) }));
      }
      YT.replace(box, lines);
      $("#cookies-delete").disabled = !c.present;
    } catch (err) {
      if (err.status !== 401) YT.replace(box, h("span", { class: "error-text", text: err.message }));
    }
  }

  $("#cookies-form").addEventListener("submit", async (e) => {
    e.preventDefault();
    const file = $("#cookies-file").files[0];
    if (!file) return;
    const data = new FormData();
    data.append("file", file);
    const ok = await busy(e.submitter, () => api("POST", "/api/cookies", data), "Cookies uploaded");
    if (ok) $("#cookies-file").value = "";
    loadCookies();
  });
  $("#cookies-delete").addEventListener("click", async (e) => {
    if (!confirm("Delete the stored cookies?")) return;
    await busy(e.currentTarget, () => del("/api/cookies"), "Cookies deleted");
    loadCookies();
  });

  function result(el, res) {
    el.className = "result " + (res.ok ? "ok" : "error");
    el.textContent = res.detail || (res.ok ? "OK" : "Failed");
  }
  for (const [id, url] of [["#jf-test", "/api/jellyfin/test"], ["#jf-create", "/api/jellyfin/create-library"]]) {
    $(id).addEventListener("click", async (e) => {
      const res = await busy(e.currentTarget, () => post(url, {}));
      if (res) result($("#jf-result"), res);
    });
  }

  $("#yt-upgrade").addEventListener("click", async (e) => {
    const res = await busy(e.currentTarget, () => post("/api/ytdlp/upgrade", {}));
    if (!res) return;
    $("#yt-result").className = "result ok";
    $("#yt-result").textContent = `Installed ${res.version}. ${res.note || "Restart the container to use it."}`;
    loadYtdlp();
  });

  async function loadYtdlp() {
    try {
      const hl = await get("/api/health");
      $("#yt-info").textContent = hl.ytdlp.version ? "Running version " + hl.ytdlp.version : hl.ytdlp.detail || "Version unknown";
    } catch (err) {
      if (err.status !== 401) $("#yt-info").textContent = err.message;
    }
  }

  (async () => {
    try {
      fill(await get("/api/settings"));
    } catch (err) {
      if (err.status !== 401) showError("Could not load settings: " + err.message);
    }
  })();
  loadCookies();
  loadYtdlp();
})();
