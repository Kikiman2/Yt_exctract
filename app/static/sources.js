"use strict";
// Sources page: add / list / override / refresh / remove subscriptions.
(() => {
  const { h, $, get, post, patch, del, busy, relTime, fullTime } = YT;
  let sources = [];
  let editing = null;

  async function load() {
    try {
      sources = await get("/api/sources");
      render();
    } catch (err) {
      if (err.status !== 401) YT.replace($("#sources-box"), h("div", { class: "alert alert-error", role: "alert", text: "Could not load: " + err.message }));
    }
  }

  function overrideSummary(s) {
    const bits = [];
    if (s.quality) bits.push(s.quality);
    if (s.skip_shorts !== null) bits.push(s.skip_shorts ? "skip shorts" : "keep shorts");
    if (s.skip_live !== null) bits.push(s.skip_live ? "skip live" : "keep live");
    if (s.retention_days !== null) bits.push(s.retention_days ? s.retention_days + " d retention" : "keep forever");
    if (!s.auto_delete_enabled) bits.push("no auto-delete");
    return bits.length ? bits.join(", ") : "global defaults";
  }

  function row(s) {
    const toggle = h("input", {
      type: "checkbox", checked: s.enabled, "aria-label": `Enabled: ${s.title}`,
      onchange: async (e) => {
        const box = e.currentTarget;
        const res = await busy(box, () => patch(`/api/sources/${encodeURIComponent(s.id)}`, { enabled: box.checked }));
        if (res === undefined) box.checked = !box.checked;
        else s.enabled = res.enabled;
      },
    });
    const id = encodeURIComponent(s.id);
    return h(
      "tr",
      { dataset: { sourceId: s.id } },
      h("td", null, h("a", { href: s.url, target: "_blank", rel: "noopener noreferrer", text: s.title || s.url }), h("div", { class: "progress-meta", text: overrideSummary(s) })),
      h("td", null, h("span", { class: "badge " + (s.kind === "playlist" ? "info" : ""), text: s.kind })),
      h("td", { title: fullTime(s.last_checked_at), text: relTime(s.last_checked_at) }, ),
      h("td", { class: "hide-sm" }, s.last_error ? h("span", { class: "error-text", text: s.last_error }) : h("span", { class: "muted", text: "none" })),
      h("td", { class: "num", text: `${s.downloaded_count} / ${s.video_count}`, title: "downloaded / known videos" }),
      h("td", null, toggle),
      h(
        "td",
        { class: "row-actions" },
        h("button", { type: "button", class: "btn-sm", text: "Overrides", "aria-label": `Overrides for ${s.title}`, onclick: () => openEditor(s) }),
        h("button", {
          type: "button", class: "btn-sm", text: "Refresh now", "aria-label": `Refresh ${s.title}`,
          onclick: async (e) => {
            await busy(e.currentTarget, () => post(`/api/sources/${id}/refresh`, {}), (r) => `${r.new} new video${r.new === 1 ? "" : "s"} queued`);
            load();
          },
        }),
        h("button", {
          type: "button", class: "btn-sm btn-danger", text: "Remove", "aria-label": `Remove ${s.title}`,
          onclick: async (e) => {
            if (!confirm(`Remove "${s.title}"? Downloaded files stay on disk.`)) return;
            await busy(e.currentTarget, () => del(`/api/sources/${id}`), "Source removed");
            load();
          },
        })
      )
    );
  }

  function render() {
    $("#source-count").textContent = `(${sources.length})`;
    const box = $("#sources-box");
    if (!sources.length) {
      YT.replace(box, h("p", { class: "muted", text: "No sources yet. Add a channel, playlist or @handle above." }));
      return;
    }
    const heads = ["Source", "Kind", "Last checked", "Last error", "Videos", "Enabled", ""];
    YT.replace(
      box,
      h(
        "table",
        null,
        h("thead", null, h("tr", null, heads.map((t) => h("th", { class: t === "Last error" ? "hide-sm" : null, text: t })))),
        h("tbody", null, sources.map(row))
      )
    );
  }

  const form = $("#override-form");
  const dlg = $("#override-dialog");

  function openEditor(s) {
    editing = s;
    $("#ov-title").textContent = "Overrides: " + (s.title || s.url);
    form.elements.quality.value = s.quality || "";
    form.elements.skip_shorts.value = s.skip_shorts === null ? "" : String(s.skip_shorts);
    form.elements.skip_live.value = s.skip_live === null ? "" : String(s.skip_live);
    form.elements.retention_days.value = s.retention_days === null ? "" : s.retention_days;
    form.elements.auto_delete_enabled.checked = s.auto_delete_enabled;
    $("#ov-error").hidden = true;
    dlg.showModal();
  }

  const triBool = (v) => (v === "" ? null : v === "true");

  form.addEventListener("submit", async (e) => {
    e.preventDefault();
    const days = form.elements.retention_days.value.trim();
    const fields = {
      quality: form.elements.quality.value || null,
      skip_shorts: triBool(form.elements.skip_shorts.value),
      skip_live: triBool(form.elements.skip_live.value),
      retention_days: days === "" ? null : Number(days),
      auto_delete_enabled: form.elements.auto_delete_enabled.checked,
    };
    const err = $("#ov-error");
    err.hidden = true;
    try {
      await patch(`/api/sources/${encodeURIComponent(editing.id)}`, fields);
      dlg.close();
      YT.toast("Overrides saved");
      load();
    } catch (ex) {
      err.textContent = ex.message;
      err.hidden = false;
    }
  });

  $("#add-form").addEventListener("submit", async (e) => {
    e.preventDefault();
    const input = $("#add-url");
    const added = await busy(e.submitter, () => post("/api/sources", { url: input.value.trim() }), (s) => `Added ${s.title || "source"}`);
    if (added) input.value = "";
    load();
  });
  $("#refresh-all-btn").addEventListener("click", async (e) => {
    await busy(e.currentTarget, () => post("/api/refresh", {}), (r) => `${r.new} new video${r.new === 1 ? "" : "s"} queued`);
    load();
  });

  load();
})();
