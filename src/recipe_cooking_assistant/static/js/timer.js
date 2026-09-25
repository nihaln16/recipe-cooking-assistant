(function () {
  const root = document.getElementById("source-timer");
  if (!root) return;
  const recipeId = root.dataset.recipe;
  const step = root.dataset.step;
  const durationMs = Number(root.dataset.seconds) * 1000;
  const key = "rca-timer:" + recipeId + ":" + step;
  const readout = document.getElementById("timer-readout");
  const primaryBtn = document.getElementById("timer-primary");
  const toggleBtn = document.getElementById("timer-toggle");
  let timerId = null;

  function load() {
    const fresh = {
      durationMs: durationMs,
      remainingMs: durationMs,
      running: false,
      anchorMs: null,
      expired: false,
      started: false,
    };
    try {
      const raw = localStorage.getItem(key);
      if (raw) {
        const saved = JSON.parse(raw);
        if (saved && saved.recipeId === recipeId && String(saved.step) === String(step)) {
          saved.durationMs = durationMs;
          return saved;
        }
      }
    } catch (err) {
      return fresh;
    }
    return fresh;
  }

  function save(state) {
    try {
      localStorage.setItem(
        key,
        JSON.stringify({
          recipeId: recipeId,
          step: step,
          durationMs: state.durationMs,
          remainingMs: state.remainingMs,
          running: state.running,
          anchorMs: state.anchorMs,
          expired: state.expired,
          started: state.started,
        })
      );
    } catch (err) {
      /* storage can be blocked; the readout still updates */
    }
  }

  function remaining(state, now) {
    if (state.running && state.anchorMs != null) {
      return state.remainingMs - Math.max(0, now - state.anchorMs);
    }
    return state.remainingMs;
  }

  function format(ms) {
    const total = Math.max(0, Math.ceil(ms / 1000));
    const hours = Math.floor(total / 3600);
    const minutes = Math.floor((total % 3600) / 60);
    const seconds = total % 60;
    const pad = function (n) { return String(n).padStart(2, "0"); };
    if (hours) return hours + ":" + pad(minutes) + ":" + pad(seconds);
    return minutes + ":" + pad(seconds);
  }

  function render(state) {
    const now = Date.now();
    const left = remaining(state, now);
    if (left <= 0 && (state.running || state.expired)) {
      state.remainingMs = 0;
      state.running = false;
      state.anchorMs = null;
      state.expired = true;
      state.started = true;
      readout.textContent = "Timer finished. " + (root.dataset.range || root.dataset.label);
      readout.setAttribute("aria-live", "assertive");
    } else {
      readout.textContent = format(state.running ? left : state.remainingMs) + " remaining";
      readout.setAttribute("aria-live", "polite");
    }
    const paused = state.started && !state.running && !state.expired;
    primaryBtn.textContent = state.started ? "Reset timer" : "Start timer";
    toggleBtn.textContent = paused ? "Resume" : "Pause";
    toggleBtn.disabled = !paused && !state.running;
    save(state);
    if (state.running && timerId == null) {
      timerId = window.setInterval(function () { render(state); }, 250);
    }
    if (!state.running && timerId != null) {
      window.clearInterval(timerId);
      timerId = null;
    }
  }

  let state = load();
  render(state);

  primaryBtn.addEventListener("click", function () {
    if (!state.started) {
      state.remainingMs = durationMs;
      state.running = true;
      state.anchorMs = Date.now();
      state.expired = false;
      state.started = true;
    } else {
      state.remainingMs = durationMs;
      state.running = false;
      state.anchorMs = null;
      state.expired = false;
      state.started = false;
    }
    render(state);
  });

  toggleBtn.addEventListener("click", function () {
    if (!state.started || state.expired) return;
    if (state.running) {
      state.remainingMs = Math.max(0, remaining(state, Date.now()));
      state.running = false;
      state.anchorMs = null;
    } else if (state.remainingMs > 0) {
      state.running = true;
      state.anchorMs = Date.now();
    }
    render(state);
  });
})();
