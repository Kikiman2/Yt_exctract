function formatProgress(video) {
  const p = video.progress;
  if (video.status === "failed") {
    return `<span class="error" title="${video.error || ""}">${video.error || "failed"}</span>`;
  }
  if (video.status === "skipped") {
    return `<span class="skipped-reason" title="${video.error || ""}">${video.error || "skipped"}</span>`;
  }
  if (!p) return "";
  if (p.status === "processing") return "processing…";
  if (typeof p.percent === "number") return `${p.percent.toFixed(1)}%`;
  return "";
}

async function refreshStatus() {
  const table = document.getElementById("videos-table");
  if (!table) return;

  try {
    const res = await fetch("/api/status");
    if (!res.ok) return;
    const data = await res.json();
    const tbody = table.querySelector("tbody");
    tbody.innerHTML = data.videos.map((video) => {
      let actionButton = "";
      if (video.status === "failed" || video.status === "deleted") {
        actionButton = `<form method="post" action="/api/videos/${video.video_id}/retry"><button type="submit">Download</button></form>`;
      } else if (video.status === "skipped") {
        actionButton = `<form method="post" action="/api/videos/${video.video_id}/retry"><input type="hidden" name="force" value="true"><button type="submit">Download anyway</button></form>`;
      } else if (video.status === "downloaded") {
        actionButton = `<form method="post" action="/api/videos/${video.video_id}/delete"><button type="submit">Delete</button></form>`;
      }
      return `
        <tr data-video-id="${video.video_id}">
          <td>${video.title}</td>
          <td>${video.channel_title || "Manual"}</td>
          <td class="status status-${video.status}">${video.status}</td>
          <td class="progress-cell">${formatProgress(video)}</td>
          <td>${actionButton}</td>
        </tr>
      `;
    }).join("");
  } catch (err) {
    // network hiccup, next poll will retry
  }
}

if (document.getElementById("videos-table")) {
  refreshStatus();
  setInterval(refreshStatus, 3000);
}
