"use strict";
// Users page (admin): list accounts, add one, change role, enable/disable,
// reset a password, delete.
(() => {
  const { h, $, get, post, patch, del, busy, fullTime } = YT;
  let me = null;

  async function load() {
    try {
      const [users, who] = await Promise.all([get("/api/users"), me ? Promise.resolve(me) : get("/api/me")]);
      me = who;
      render(users);
    } catch (err) {
      if (err.status !== 401) YT.replace($("#users-box"), h("div", { class: "alert alert-error", role: "alert", text: "Could not load: " + err.message }));
    }
  }

  async function act(button, fn, ok) {
    await busy(button, fn, ok);
    load();
  }

  function row(u) {
    const self = me && u.id === me.id;
    const btn = (label, cls, fn, ok) =>
      h("button", { type: "button", class: "btn-sm " + cls, text: label, "aria-label": `${label}: ${u.username}`, onclick: (e) => act(e.currentTarget, fn, ok) });
    const actions = [
      btn("Reset password", "", () => {
        const pw = prompt(`New temporary password for ${u.username} (min. 8 characters):`);
        if (pw === null) return Promise.resolve();
        return patch(`/api/users/${u.id}`, { password: pw });
      }, "Password reset"),
    ];
    if (!self) {
      actions.push(btn(u.role === "admin" ? "Make user" : "Make admin", "", () => patch(`/api/users/${u.id}`, { role: u.role === "admin" ? "user" : "admin" }), "Role changed"));
      actions.push(btn(u.disabled ? "Enable" : "Disable", "", () => patch(`/api/users/${u.id}`, { disabled: !u.disabled }), u.disabled ? "Enabled" : "Disabled"));
      actions.push(
        btn("Delete", "btn-danger", () => {
          if (!confirm(`Delete the account "${u.username}"? Their downloaded videos stay in the library.`)) return Promise.resolve();
          return del(`/api/users/${u.id}`);
        }, "Deleted")
      );
    }
    return h(
      "tr",
      null,
      h("td", null, u.username, self ? h("span", { class: "muted small", text: " (you)" }) : null),
      h("td", null, h("span", { class: "badge", text: u.role })),
      h("td", null, u.disabled ? h("span", { class: "badge st-failed", text: "disabled" }) : h("span", { class: "badge st-downloaded", text: "active" })),
      h("td", { class: "hide-sm small muted", text: String(u.request_count) }),
      h("td", { class: "hide-sm small muted", title: u.created_at, text: fullTime(u.created_at) }),
      h("td", { class: "row-actions" }, h("div", { class: "row-actions-inner" }, actions))
    );
  }

  function render(users) {
    $("#user-count").textContent = `(${users.length})`;
    YT.replace(
      $("#users-box"),
      h(
        "table",
        null,
        h("thead", null, h("tr", null, ["User", "Role", "Status", "Links", "Created", ""].map((t, i) => h("th", { class: i === 3 || i === 4 ? "hide-sm" : null, text: t })))),
        h("tbody", null, users.map(row))
      )
    );
  }

  $("#user-form").addEventListener("submit", async (e) => {
    e.preventDefault();
    const form = e.currentTarget;
    const data = Object.fromEntries(new FormData(form));
    const ok = await busy(e.submitter, () => post("/api/users", data), (r) => `Added ${r.username}`);
    if (ok) form.reset();
    load();
  });

  load();
})();
