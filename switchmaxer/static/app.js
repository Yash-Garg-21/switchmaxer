document.querySelectorAll("[data-autosave]").forEach((control) => {
  control.addEventListener("change", () => control.form.requestSubmit());
});
