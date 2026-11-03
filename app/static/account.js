"use strict";
// Account page: change your own password.
(() => {
  const { $, post, busy } = YT;

  $("#password-form").addEventListener("submit", async (e) => {
    e.preventDefault();
    const form = e.currentTarget;
    const err = $("#pw-error");
    err.hidden = true;
    const data = Object.fromEntries(new FormData(form));
    if (data.new_password !== data.repeat) {
      err.textContent = "The new passwords don't match.";
      err.hidden = false;
      return;
    }
    const ok = await busy(
      e.submitter,
      () => post("/api/me/password", { current_password: data.current_password, new_password: data.new_password }),
      "Password changed"
    );
    if (ok) form.reset();
  });
})();
