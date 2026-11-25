document.querySelectorAll("[data-autosave]").forEach((control) => {
  control.addEventListener("change", () => control.form.requestSubmit());
});

document.querySelectorAll("[data-refresh-sources]").forEach((form) => {
  const button = form.querySelector("button");
  const label = button.textContent.trim();
  form.addEventListener("submit", () => {
    button.disabled = true;
    button.textContent = "Refreshing…";
    form.setAttribute("aria-busy", "true");
  });
  window.addEventListener("pageshow", () => {
    button.disabled = false;
    button.textContent = label;
    form.removeAttribute("aria-busy");
  });
});
