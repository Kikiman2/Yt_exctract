"use strict";
// Dashboard: paste-a-link form, queue counters and the video table.
// Refreshes every 3 s, only while the tab is visible (YT.poll).
(() => {
  const { h, $, api, get, post, toast, busy, relTime, fullTime, bytes, duration, progressBar } = YT;
  const STATUSES = ["pending", "downloading", "downloaded", "skipped", "failed", "deleted"];
  let filter = "";
  let queue = null;

  function renderCounters() {
    const box = $("#counters");
    const counts = (queue && queue.counts) || {};
    YT.replace(
      box,
      STATUSES.map((s) =>
        h(
          "button",
          {
            type: "button", class: "counter", dataset: { status: s },
            "aria-pressed": String(filter === s),
            "aria-label": `${counts[s] || 0} ${s}, filter the list`,
            onclick: () => {
              filter = filter === s ? "" : s;
              $("#status-filter").value = filter;
              tick();
            },
          },
          h("span", { class: "counter-value", text: counts[s] || 0 }),
          h("span", { class: "counter-label", text: s })
        )
      )
    );
    const pause = $("#pause-btn");
    pause.textContent = queue && queue.paused ? "Resume downloads" : "Pause downloads";
  }

  function statusCell(v) {
    const parts = [h("span", { class: "badge st-" + v.status, text: v.status })];
    if (v.status === "failed") {
      parts.push(h("div", { class: "error-text", text: v.error || "Download failed" }));
      const retry = v.next_retry_at ? "next retry " + relTime(v.next_retry_at) : "no automatic retry";
      parts.push(h("div", { class: "progress-meta", title: fullTime(v.next_retry_at), text: `attempt ${v.attempts} · ${retry}` }));
    } else if (v.status === "skipped") {
      parts.push(h("div", { class: "skip-text", text: v.error || "Skipped by filter" }));
    }
    return parts;
  }

  function progressCell(v) {
    if (v.status === "downloaded") {
      return h("span", { class: "muted small", text: [bytes(v.file_size), duration(v.duration)].filter(Boolean).join(" · ") });
    }
    const p = v.progress;
    if (v.status !== "downloading" || !p) return null;
    const meta = [p.status === "processing" ? "processing" : p.percent === null ? "starting" : Math.round(p.percent) + "%"];
    if (p.speed) meta.push(bytes(p.speed) + "/s");
    if (p.eta !== null && p.eta !== undefined) meta.push("ETA " + duration(p.eta));
    return [progressBar(p.status === "processing" ? 100 : p.percent), h("div", { class: "progress-meta", text: meta.join(" · ") })];
  }

  async function act(button, fn, ok) {
    await busy(button, fn, ok);
    tick();
  }

  function actionButtons(v) {
    const id = encodeURIComponent(v.video_id);
    const btn = (label, cls, fn, ok) =>
      h("button", { type: "button", class: "btn-sm " + cls, text: label, "aria-label": `${label}: ${v.title}`, onclick: (e) => act(e.currentTarget, fn, ok) });
    const retry = () => post(`/api/videos/${id}/retry`, {});
    const force = () => post(`/api/videos/${id}/retry`, { force: true });
    const out = [];
    if (v.status === "failed") out.push(btn("Retry", "", retry, "Queued again"));
    if (v.status === "skipped" || v.status === "failed") out.push(btn("Download anyway", "", force, "Queued, ignoring filters"));
    if (v.status === "deleted") out.push(btn("Download again", "", force, "Queued again"));
    if (v.status === "downloaded") {
      out.push(
        btn("Delete", "btn-danger", () => {
          if (!confirm(`Delete the file for "${v.title}"?`)) return Promise.resolve();
          return post(`/api/videos/${id}/delete`, {});
        }, "Deleted")
      );
    }
    return out;
  }

  function renderVideos(videos) {
    const box = $("#videos-box");
    $("#video-count").textContent = `(${videos.length})`;
    if (!videos.length) {
      YT.replace(box, h("p", { class: "muted", text: "No videos here yet. Paste a link above or add a source." }));
      return;
    }
    const rows = videos.map((v) =>
      h(
        "tr",
        { dataset: { videoId: v.video_id } },
        h("td", { class: "title" }, h("a", { href: v.url, target: "_blank", rel: "noopener noreferrer", text: v.title || v.video_id })),
        h("td", { class: "hide-sm", text: v.channel_name || "Manual" }),
        h("td", null, statusCell(v)),
        h("td", { class: "progress-cell" }, progressCell(v)),
        h("td", { class: "hide-sm small muted", title: fullTime(v.added_at), text: relTime(v.added_at) }),
        h("td", { class: "row-actions" }, actionButtons(v))
      )
    );
    YT.replace(
      box,
      h(
        "table",
        null,
        h("thead", null, h("tr", null, ["Title", "Channel", "Status", "Progress", "Added", ""].map((t, i) => h("th", { class: i === 1 || i === 4 ? "hide-sm" : null, text: t })))),
        h("tbody", null, rows)
      )
    );
  }

  async function tick() {
    try {
      const qs = new URLSearchParams({ limit: "200" });
      if (filter) qs.set("status", filter);
      const [status, videos] = await Promise.all([get("/api/status"), get("/api/videos?" + qs)]);
      queue = status.queue;
      YT.showQueue(queue);
      renderCounters();
      renderVideos(videos);
    } catch (err) {
      if (err.status !== 401) YT.replace($("#videos-box"), h("div", { class: "alert alert-error", role: "alert", text: "Could not load: " + err.message }));
    }
  }

  $("#add-form").addEventListener("submit", async (e) => {
    e.preventDefault();
    const input = $("#add-url");
    const ok = await busy(e.submitter, () => post("/api/videos", { url: input.value.trim() }), "Link added to the queue");
    if (ok) input.value = "";
    tick();
  });
  $("#status-filter").addEventListener("change", (e) => {
    filter = e.target.value;
    tick();
  });
  $("#pause-btn").addEventListener("click", (e) =>
    act(e.currentTarget, () => post(queue && queue.paused ? "/api/queue/resume" : "/api/queue/pause", {}))
  );
  $("#refresh-btn").addEventListener("click", (e) =>
    act(e.currentTarget, () => post("/api/refresh", {}), (r) => `${r.new} new video${r.new === 1 ? "" : "s"} queued`)
  );
  $("#missing-btn").addEventListener("click", (e) =>
    act(e.currentTarget, () => post("/api/videos/check-missing", {}), (r) => `${r.requeued} missing file${r.requeued === 1 ? "" : "s"} requeued`)
  );

  YT.poll(tick, 3000);
})();
