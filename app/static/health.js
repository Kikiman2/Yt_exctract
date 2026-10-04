"use strict";
// Health page: component cards plus the events timeline.
(() => {
  const { h, $, get, post, busy, relTime, fullTime } = YT;

  function card(name, ok, detail, extra) {
    return h(
      "article",
      { class: "card " + (ok ? "ok" : "fail") },
      h("h3", null, h("span", { text: name }), h("span", { class: "badge " + (ok ? "ok" : "bad"), text: ok ? "OK" : "Problem" })),
      h("p", { text: detail || "" }),
      extra ? h("p", { text: extra }) : null
    );
  }

  function renderHealth(hl) {
    const ck = hl.cookies || {};
    const cookieDetail = ck.present ? `${ck.cookie_count} cookies (${ck.youtube_cookie_count} YouTube)` : "No cookies uploaded";
    const cards = [
      card("yt-dlp", hl.ytdlp.ok, hl.ytdlp.detail, hl.ytdlp.version ? "Version " + hl.ytdlp.version : ""),
      card("PO token provider", hl.pot_provider.ok, hl.pot_provider.detail),
      // Cookies are optional: absent is informational, only a broken file is a problem.
      card("Cookies", !/invalid|expired/i.test(ck.detail || ""), ck.detail || cookieDetail, ck.present ? cookieDetail + (ck.expires_at ? ", earliest expiry " + fullTime(ck.expires_at) : "") : ""),
      card("Library", hl.library.ok, hl.library.detail, hl.library.path),
      card("Disk", hl.disk.ok, hl.disk.free_gb === null || hl.disk.free_gb === undefined ? "Unknown" : hl.disk.free_gb + " GB free"),
      card("Jellyfin", hl.jellyfin.ok, hl.jellyfin.detail),
    ];
    YT.replace($("#components"), cards);
    $("#checked-at").textContent = hl.checked_at ? "Checked " + relTime(hl.checked_at) : "Not checked yet";
  }

  function renderEvents(events) {
    const list = $("#events");
    if (!events.length) {
      YT.replace(list, h("li", { class: "muted", text: "No events yet." }));
      return;
    }
    YT.replace(
      list,
      events.map((ev) =>
        h(
          "li",
          { class: "level-" + (ev.level || "info") },
          h("span", { class: "ts", title: fullTime(ev.ts), text: relTime(ev.ts) }),
          h("span", { class: "lvl", text: (ev.level || "info") + " · " + ev.kind }),
          h("span", { class: "msg", text: ev.message })
        )
      )
    );
  }

  async function loadEvents() {
    try {
      renderEvents(await get("/api/events?limit=" + encodeURIComponent($("#ev-limit").value)));
    } catch (err) {
      if (err.status !== 401) YT.replace($("#events"), h("li", { class: "error-text", text: "Could not load events: " + err.message }));
    }
  }

  async function loadHealth() {
    try {
      renderHealth(await get("/api/health"));
    } catch (err) {
      if (err.status !== 401) YT.replace($("#components"), h("div", { class: "alert alert-error", role: "alert", text: "Could not load: " + err.message }));
    }
  }

  $("#check-btn").addEventListener("click", async (e) => {
    const hl = await busy(e.currentTarget, () => post("/api/health/check", {}), "Health checked");
    if (hl) renderHealth(hl);
    loadEvents();
  });
  $("#ev-limit").addEventListener("change", loadEvents);

  YT.poll(async () => {
    await Promise.all([loadHealth(), loadEvents()]);
  }, 30000);
})();
