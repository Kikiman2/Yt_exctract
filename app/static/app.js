"use strict";
// Shared helpers for every page. No framework, no build step.
// Rule: every dynamic value reaches the DOM through textContent / attribute
// setters (see h()) or esc(); never through raw innerHTML.

const YT = (() => {
  class ApiError extends Error {
    constructor(message, status) {
      super(message);
      this.status = status;
    }
  }

  const ESC = { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" };
  // For the rare place that must build an HTML string; h() is preferred.
  function esc(value) {
    return String(value === null || value === undefined ? "" : value).replace(/[&<>"']/g, (c) => ESC[c]);
  }

  function formatDetail(detail) {
    if (typeof detail === "string") return detail;
    if (Array.isArray(detail)) {
      return detail
        .map((d) => {
          const loc = Array.isArray(d.loc) ? d.loc.filter((x) => x !== "body").join(".") : "";
          return (loc ? loc + ": " : "") + (d.msg || JSON.stringify(d));
        })
        .join("; ");
    }
    return detail && typeof detail === "object" ? JSON.stringify(detail) : "";
  }

  // body may be a plain object (sent as JSON) or FormData (sent as multipart).
  async function api(method, url, body) {
    const opts = { method, headers: { Accept: "application/json" }, credentials: "same-origin" };
    if (body instanceof FormData) {
      opts.body = body;
    } else if (body !== undefined) {
      opts.headers["Content-Type"] = "application/json";
      opts.body = JSON.stringify(body);
    }
    let res;
    try {
      res = await fetch(url, opts);
    } catch (err) {
      throw new ApiError("Network error: the server could not be reached", 0);
    }
    if (res.status === 401) {
      location.href = "/login?next=" + encodeURIComponent(location.pathname + location.search);
      throw new ApiError("Not logged in", 401);
    }
    let data = null;
    if ((res.headers.get("content-type") || "").includes("application/json")) {
      try {
        data = await res.json();
      } catch (err) {
        data = null;
      }
    }
    if (!res.ok) {
      throw new ApiError((data && formatDetail(data.detail)) || `${res.status} ${res.statusText || "error"}`, res.status);
    }
    return data;
  }

  const get = (url) => api("GET", url);
  const post = (url, body) => api("POST", url, body === undefined ? {} : body);
  const put = (url, body) => api("PUT", url, body);
  const patch = (url, body) => api("PATCH", url, body);
  const del = (url) => api("DELETE", url);

  // h("div", {class: "x", onclick: fn, dataset: {id: 1}}, "text", child, [more])
  function h(tag, props, ...children) {
    const el = document.createElement(tag);
    if (props) {
      for (const [k, v] of Object.entries(props)) {
        if (v === undefined || v === null || v === false) continue;
        if (k === "class") el.className = v;
        else if (k === "text") el.textContent = v;
        else if (k === "dataset") Object.assign(el.dataset, v);
        else if (k.startsWith("on") && typeof v === "function") el.addEventListener(k.slice(2), v);
        else if (["checked", "disabled", "value", "hidden", "selected"].includes(k)) el[k] = v;
        else el.setAttribute(k, v === true ? "" : String(v));
      }
    }
    append(el, children);
    return el;
  }

  function append(el, children) {
    for (const c of children) {
      if (c === null || c === undefined || c === false) continue;
      if (Array.isArray(c)) append(el, c);
      else if (c instanceof Node) el.appendChild(c);
      else el.appendChild(document.createTextNode(String(c)));
    }
  }

  function replace(el, ...children) {
    el.replaceChildren();
    append(el, children);
  }

  const $ = (sel, root) => (root || document).querySelector(sel);

  function toast(message, isError) {
    const box = document.getElementById("toasts");
    if (!box) return;
    const t = h("div", { class: "toast" + (isError ? " error" : ""), role: isError ? "alert" : "status" }, message);
    box.appendChild(t);
    setTimeout(() => t.remove(), isError ? 9000 : 3500);
  }

  // Run an async action from a button: disable it meanwhile, toast the outcome.
  async function busy(button, fn, okMessage) {
    if (button) {
      button.disabled = true;
      button.classList.add("busy");
    }
    try {
      const result = await fn();
      if (okMessage) toast(typeof okMessage === "function" ? okMessage(result) : okMessage);
      return result;
    } catch (err) {
      toast(err.message || String(err), true);
      return undefined;
    } finally {
      if (button) {
        button.disabled = false;
        button.classList.remove("busy");
      }
    }
  }

  function relTime(iso) {
    if (!iso) return "never";
    const d = new Date(iso);
    if (isNaN(d)) return String(iso);
    const s = Math.round((Date.now() - d.getTime()) / 1000);
    const abs = Math.abs(s);
    let txt;
    if (abs < 60) txt = abs + "s";
    else if (abs < 3600) txt = Math.round(abs / 60) + "m";
    else if (abs < 86400) txt = Math.round(abs / 3600) + "h";
    else txt = Math.round(abs / 86400) + "d";
    return s < 0 ? "in " + txt : txt + " ago";
  }

  function fullTime(iso) {
    if (!iso) return "";
    const d = new Date(iso);
    return isNaN(d) ? String(iso) : d.toLocaleString();
  }

  function bytes(n) {
    if (n === null || n === undefined) return "";
    const units = ["B", "KB", "MB", "GB", "TB"];
    let v = Number(n);
    let i = 0;
    while (v >= 1024 && i < units.length - 1) {
      v /= 1024;
      i++;
    }
    return (i === 0 ? v : v.toFixed(v >= 100 ? 0 : 1)) + " " + units[i];
  }

  function duration(sec) {
    if (sec === null || sec === undefined) return "";
    sec = Math.max(0, Math.round(sec));
    const hh = Math.floor(sec / 3600);
    const mm = Math.floor((sec % 3600) / 60);
    const ss = sec % 60;
    const p = (n) => String(n).padStart(2, "0");
    return hh ? `${hh}:${p(mm)}:${p(ss)}` : `${mm}:${p(ss)}`;
  }

  function progressBar(percent) {
    const pct = Math.max(0, Math.min(100, Math.round(percent || 0)));
    return h(
      "div",
      { class: "progress", role: "progressbar", "aria-label": "Download progress", "aria-valuemin": "0", "aria-valuemax": "100", "aria-valuenow": String(pct), title: pct + "%" },
      h("span", { style: "width:" + pct + "%" })
    );
  }

  // Poll while the tab is visible; returns a function that polls immediately.
  function poll(fn, ms) {
    let running = false;
    const tick = async () => {
      if (running) return;
      running = true;
      try {
        await fn();
      } finally {
        running = false;
      }
    };
    setInterval(() => {
      if (!document.hidden) tick();
    }, ms);
    document.addEventListener("visibilitychange", () => {
      if (!document.hidden) tick();
    });
    tick();
    return tick;
  }

  // Global banner: "Downloads paused: <reasons>" from a StatusJson/QueueJson.
  function showQueue(queue) {
    const el = document.getElementById("status-banner");
    if (!el) return;
    const paused = queue && queue.paused;
    el.hidden = !paused;
    el.textContent = paused ? "Downloads paused: " + ((queue.pause_reasons || []).join("; ") || "paused") : "";
  }

  // Pages that already fetch /api/status call showQueue themselves; the rest poll here.
  async function refreshBanner() {
    try {
      showQueue((await get("/api/status")).queue);
    } catch (err) {
      /* The banner is best-effort; page content reports API errors. */
    }
  }

  document.addEventListener("click", (e) => {
    const btn = e.target.closest("[data-close]");
    const dlg = btn && btn.closest("dialog");
    if (dlg) dlg.close();
  });

  const hasOwnStatus = document.body.classList.contains("page-dashboard");
  if (document.getElementById("status-banner") && !hasOwnStatus && !document.body.classList.contains("page-login")) {
    poll(refreshBanner, 15000);
  }

  return { ApiError, esc, refreshBanner, api, get, post, put, patch, del, h, replace, append, $, toast, busy, relTime, fullTime, bytes, duration, progressBar, poll, showQueue };
})();
