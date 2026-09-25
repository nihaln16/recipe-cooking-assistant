(function () {
  var form = document.querySelector(".import-form");
  var sampleForm = document.querySelector(".sample-form");
  if (!form || !sampleForm) return;

  var text = document.getElementById("recipe_text");
  var fileInput = document.getElementById("images");
  var submitBtn = document.getElementById("create-guide");
  var sampleBtn = document.getElementById("sample-recipe");
  var list = document.getElementById("upload-list");
  var count = document.getElementById("upload-count");
  var overlay = document.getElementById("import-loading");
  var statusEl = document.getElementById("import-status");
  var noteEl = document.getElementById("import-loading-note");
  var alertBox = document.getElementById("import-alert");
  var maxImages = Number(form.dataset.maxImages || 6);
  var maxBytes = Number(form.dataset.maxBytes || 0);
  var maxMb = Math.round(maxBytes / (1024 * 1024));
  var allowed = { "image/jpeg": true, "image/png": true, "image/webp": true };
  var files = [];
  var submitting = false;
  var timer = null;
  var reduced = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
  var typeLabel = "JPEG, PNG, or WebP";

  var extractPhrases = [
    "Reading your recipe",
    "Organizing ingredients and steps",
    "Checking for missing details",
    "Preparing your cooking guide",
  ];
  var samplePhrases = [
    "Opening the sample recipe",
    "Preparing the walkthrough",
    "Setting up the cooking guide",
  ];

  function syncButton() {
    var ready = text.value.trim().length > 0 || files.length > 0;
    submitBtn.disabled = submitting || !ready;
    sampleBtn.disabled = submitting;
  }

  function showAlert(message) {
    alertBox.hidden = false;
    alertBox.textContent = "";
    var titleText = "We couldn’t create this recipe yet.";
    var rest = message.replace(/\s+/g, " ").trim();
    if (rest.indexOf(titleText) === 0) rest = rest.slice(titleText.length).trim();
    rest = rest.replace(/Try again$/, "").trim();
    var title = document.createElement("p");
    title.className = "import-alert-title";
    title.textContent = titleText;
    alertBox.appendChild(title);
    if (rest) {
      var body = document.createElement("p");
      body.textContent = rest;
      alertBox.appendChild(body);
    }
    var actions = document.createElement("p");
    actions.className = "import-alert-actions";
    var again = document.createElement("a");
    again.className = "text-action";
    again.href = "#recipe_text";
    again.textContent = "Try again";
    actions.appendChild(again);
    alertBox.appendChild(actions);
  }

  function hideAlert() {
    alertBox.hidden = true;
  }

  function renderFiles() {
    list.textContent = "";
    files.forEach(function (entry, index) {
      var item = document.createElement("li");
      var thumb = document.createElement("img");
      thumb.src = entry.url;
      thumb.alt = "";
      var order = document.createElement("span");
      order.className = "upload-order";
      order.textContent = String(index + 1);
      var name = document.createElement("span");
      name.className = "upload-name";
      name.textContent = entry.file.name;
      var remove = document.createElement("button");
      remove.type = "button";
      remove.className = "text-action";
      remove.textContent = "Remove";
      remove.setAttribute("aria-label", "Remove screenshot " + (index + 1) + ", " + entry.file.name);
      remove.addEventListener("click", function () {
        URL.revokeObjectURL(entry.url);
        files.splice(index, 1);
        renderFiles();
        syncButton();
      });
      item.appendChild(thumb);
      item.appendChild(order);
      item.appendChild(name);
      item.appendChild(remove);
      list.appendChild(item);
    });
    list.hidden = files.length === 0;
    count.textContent =
      files.length + " of " + maxImages + " · " + typeLabel + " · " + maxMb + " MB each";
  }

  fileInput.addEventListener("change", function () {
    var incoming = Array.prototype.slice.call(fileInput.files || []);
    fileInput.value = "";
    var problem = "";
    incoming.forEach(function (file) {
      if (!allowed[file.type]) {
        problem = "Only JPEG, PNG, and WebP images are allowed.";
        return;
      }
      if (maxBytes && file.size > maxBytes) {
        problem = "Each image must be " + maxMb + " MB or smaller.";
        return;
      }
      if (files.length >= maxImages) {
        problem = "You can upload at most " + maxImages + " images per recipe.";
        return;
      }
      files.push({ file: file, url: URL.createObjectURL(file) });
    });
    if (problem) showAlert(problem);
    else hideAlert();
    renderFiles();
    syncButton();
  });

  text.addEventListener("input", syncButton);

  function stopPhrases() {
    if (timer) {
      clearInterval(timer);
      timer = null;
    }
  }

  function showLoading(kind) {
    var phrases = kind === "sample" ? samplePhrases : extractPhrases;
    overlay.hidden = false;
    statusEl.textContent = phrases[0];
    noteEl.hidden = kind === "sample";
    if (reduced) return;
    var index = 0;
    timer = setInterval(function () {
      index = (index + 1) % phrases.length;
      statusEl.textContent = phrases[index];
    }, 2000);
  }

  function hideLoading() {
    stopPhrases();
    overlay.hidden = true;
    submitting = false;
    syncButton();
  }

  function messageFromHtml(html) {
    var doc = new DOMParser().parseFromString(html, "text/html");
    var alertNode = doc.querySelector("[role='alert']");
    if (!alertNode) return "Check that your screenshots contain readable ingredients or directions, then try again. Your text and images are still here.";
    return alertNode.textContent.replace(/\s+/g, " ").trim();
  }

  function send(formEl, kind) {
    if (submitting) return;
    submitting = true;
    submitBtn.disabled = true;
    sampleBtn.disabled = true;
    hideAlert();
    showLoading(kind);
    var body = new FormData(formEl);
    if (kind !== "sample") {
      body.delete("images");
      files.forEach(function (entry) {
        body.append("images", entry.file, entry.file.name);
      });
    }
    fetch(formEl.action, { method: "POST", body: body, redirect: "follow" })
      .then(function (response) {
        var path = "";
        try {
          path = new URL(response.url).pathname;
        } catch (err) {
          path = "";
        }
        if (response.redirected || path.indexOf("/recipes/") === 0) {
          window.location.assign(response.url);
          return;
        }
        return response.text().then(function (html) {
          showAlert(messageFromHtml(html));
          hideLoading();
        });
      })
      .catch(function () {
        showAlert("Check your connection, then try again. Your text and images are still here.");
        hideLoading();
      });
  }

  form.addEventListener("submit", function (event) {
    event.preventDefault();
    if (submitting) return;
    if (!text.value.trim() && files.length === 0) {
      showAlert("Paste recipe text and/or upload screenshots to continue.");
      return;
    }
    send(form, "extract");
  });

  sampleForm.addEventListener("submit", function (event) {
    event.preventDefault();
    if (submitting) return;
    send(sampleForm, "sample");
  });

  window.addEventListener("pageshow", hideLoading);
  syncButton();
})();
