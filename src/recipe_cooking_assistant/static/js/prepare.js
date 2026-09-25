(function () {
  document.querySelectorAll(".prep-row form").forEach(function (form) {
    form.addEventListener("submit", function (event) {
      event.preventDefault();
      if (form.dataset.submitting === "1") return;
      form.dataset.submitting = "1";
      const button = form.querySelector("button");
      const box = form.querySelector(".check-box");
      const checked = form.querySelector('input[name="checked"]');
      fetch(form.action, {
        method: "POST",
        body: new FormData(form),
        headers: { Accept: "application/json" },
        credentials: "same-origin",
      })
        .then(function (response) { return response.json(); })
        .then(function (data) {
          if (!data.ok) return;
          const on = !!data.checked;
          checked.value = on ? "0" : "1";
          button.setAttribute("aria-pressed", on ? "true" : "false");
          const label = button.getAttribute("aria-label") || "";
          const name = label.replace(/^Mark (not )?ready:\s*/, "");
          button.setAttribute("aria-label", (on ? "Mark not ready: " : "Mark ready: ") + name);
          box.textContent = on ? "✓" : "";
        })
        .finally(function () {
          form.dataset.submitting = "0";
        });
    });
  });
})();
