(function () {
  const form = document.getElementById("cook-guide-form");
  if (!form) return;
  const status = document.getElementById("cook-guide-status");

  function thread() {
    let log = form.parentElement.querySelector(".cook-thread");
    if (!log) {
      log = document.createElement("div");
      log.className = "cook-thread";
      log.setAttribute("role", "log");
      log.setAttribute("aria-label", "Conversation for this step");
      form.parentElement.insertBefore(log, form);
    }
    return log;
  }

  function addMessage(role, text) {
    const article = document.createElement("article");
    article.className = role === "assistant" ? "cook-msg cook-msg-ai" : "cook-msg cook-msg-user";
    const label = document.createElement("p");
    label.className = "cook-msg-label";
    label.textContent = role === "assistant" ? "AI guidance" : "You";
    const body = document.createElement("p");
    body.textContent = text;
    article.append(label, body);
    thread().append(article);
  }

  function showError(message) {
    let alert = form.parentElement.querySelector(".alert-error");
    if (!message) {
      if (alert) alert.remove();
      return;
    }
    if (!alert) {
      alert = document.createElement("div");
      alert.className = "alert alert-error";
      alert.setAttribute("role", "alert");
      form.parentElement.insertBefore(alert, form);
    }
    alert.textContent = message;
  }

  form.addEventListener("submit", function (event) {
    event.preventDefault();
    if (form.dataset.submitting === "1") return;
    form.dataset.submitting = "1";
    form.setAttribute("aria-busy", "true");
    if (status) status.textContent = "Sending…";
    const submitter = event.submitter;
    const body = new FormData(form);
    if (submitter && submitter.name) body.set(submitter.name, submitter.value);
    const buttons = form.querySelectorAll("button");
    buttons.forEach(function (button) { button.disabled = true; });
    fetch(form.action, {
      method: "POST",
      body: body,
      headers: { Accept: "application/json" },
      credentials: "same-origin",
    })
      .then(function (response) {
        return response.json().then(function (data) {
          return { status: response.status, data: data };
        });
      })
      .then(function (result) {
        const data = result.data || {};
        if (data.form_token) {
          const token = form.querySelector('input[name="form_token"]');
          if (token) token.value = data.form_token;
        }
        if (data.redirect) {
          window.location.assign(data.redirect);
          return;
        }
        if (data.user) addMessage("user", data.user);
        if (data.guidance) addMessage("assistant", data.guidance);
        if (data.ok && data.user) {
          const field = document.getElementById("cook-question");
          if (field && (!submitter || submitter.name !== "quick_action")) field.value = "";
          showError("");
        } else if (data.error) {
          showError(data.error);
          if (data.draft && document.getElementById("cook-question")) {
            document.getElementById("cook-question").value = data.draft;
          }
        }
        if (status) status.textContent = "";
      })
      .catch(function () {
        showError("Cooking guidance is unavailable right now. Your recipe and this step are unchanged. Try again.");
        if (status) status.textContent = "";
      })
      .finally(function () {
        form.dataset.submitting = "0";
        form.removeAttribute("aria-busy");
        buttons.forEach(function (button) { button.disabled = false; });
      });
  });
})();
