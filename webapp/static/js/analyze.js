import { mountLiveAnalysis } from "./analysis.js";

export function mountSourceTabs(root = document) {
  const buttons = [...root.querySelectorAll("[data-mode-button]")];
  const panels = [...root.querySelectorAll("[data-mode-panel]")];
  const status = root.querySelector("[data-form-status]");
  let mode = buttons.find((button) => button.getAttribute("aria-selected") === "true")?.dataset.modeButton || "file";

  function selectMode(nextMode) {
    mode = nextMode;
    for (const button of buttons) button.setAttribute("aria-selected", String(button.dataset.modeButton === mode));
    for (const button of buttons) button.tabIndex = button.dataset.modeButton === mode ? 0 : -1;
    for (const panel of panels) {
      const selected = panel.dataset.modePanel === mode;
      panel.hidden = !selected;
      panel.classList.toggle("is-active", selected);
    }
    if (status) status.textContent = "";
  }

  function selectByKey(button, key) {
    const current = buttons.indexOf(button);
    const target = key === "Home" ? 0
      : key === "End" ? buttons.length - 1
        : key === "ArrowRight" ? (current + 1) % buttons.length
          : key === "ArrowLeft" ? (current - 1 + buttons.length) % buttons.length
            : null;
    if (target === null) return;
    selectMode(buttons[target].dataset.modeButton);
    buttons[target].focus();
  }

  for (const button of buttons) {
    button.addEventListener("click", () => selectMode(button.dataset.modeButton));
    button.addEventListener("keydown", (event) => {
      if (!["ArrowLeft", "ArrowRight", "Home", "End"].includes(event.key)) return;
      event.preventDefault();
      selectByKey(button, event.key);
    });
  }
  selectMode(mode);
  return { get mode() { return mode; } };
}

const form = document.querySelector("[data-analysis-form]");
if (form) {
  const sourceTabs = mountSourceTabs();
  const status = document.querySelector("[data-form-status]");
  const liveRoot = document.querySelector("[data-live-results]");
  let liveController;

  const fileInput = form.elements.pgn_file;
  const fileLabel = document.querySelector("[data-file-label]");
  const dropZone = document.querySelector("[data-drop-zone]");
  fileInput.addEventListener("change", () => {
    const file = fileInput.files[0];
    fileLabel.textContent = file ? `${file.name} · ${formatBytes(file.size)}` : "Drop a batch file here";
  });
  for (const eventName of ["dragenter", "dragover"]) dropZone.addEventListener(eventName, () => dropZone.classList.add("is-dragging"));
  for (const eventName of ["dragleave", "drop"]) dropZone.addEventListener(eventName, () => dropZone.classList.remove("is-dragging"));

  const textarea = form.elements.pgn_text;
  const pasteCount = document.querySelector("[data-paste-count]");
  textarea.addEventListener("input", () => { pasteCount.textContent = new TextEncoder().encode(textarea.value).length.toLocaleString(); });

  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    status.textContent = "";
    const data = new FormData();
    if (sourceTabs.mode === "file" && fileInput.files[0]) data.append("pgn_file", fileInput.files[0]);
    if (sourceTabs.mode === "paste") data.append("pgn_text", textarea.value);
    if (sourceTabs.mode === "lichess") data.append("lichess_url", form.elements.lichess_url.value);
    form.classList.add("is-submitting");
    form.querySelector("button[type=submit]").disabled = true;
    status.textContent = "Stamping source and opening a durable job…";
    try {
      const response = await fetch("/api/analyses", { method: "POST", body: data });
      const body = await response.json();
      if (!response.ok) throw new Error(body.error?.message || "The source was not accepted.");
      status.textContent = "Accepted. Opening the live ledger…";
      liveController?.close();
      liveRoot.dataset.jobId = body.job.id;
      history.pushState({}, "", body.links.page);
      liveRoot.hidden = false;
      liveRoot.querySelector("[data-job-short]").textContent = body.job.id.slice(0, 8);
      liveRoot.querySelector("[data-source-name]").textContent = "Loading batch…";
      liveRoot.querySelector("[data-connection] span").textContent = "Connecting to live analysis";
      liveController = mountLiveAnalysis(liveRoot, body.job.id);
      liveRoot.scrollIntoView({
        behavior: window.matchMedia("(prefers-reduced-motion: reduce)").matches ? "auto" : "smooth",
        block: "start",
      });
    } catch (error) {
      status.textContent = error.message;
      form.classList.remove("is-submitting");
      form.querySelector("button[type=submit]").disabled = false;
    }
  });
}

window.addEventListener("popstate", () => window.location.reload());

const health = document.querySelector("[data-health]");
if (health) {
  fetch("/api/health")
    .then(async (response) => ({ response, body: await response.json() }))
    .then(({ body }) => {
      health.classList.toggle("is-degraded", body.status !== "ok");
      health.innerHTML = `<span><i></i> ${body.status}</span><span>PostgreSQL ${body.database}</span><span>Kafka ${body.kafka}</span>`;
    })
    .catch(() => { health.classList.add("is-degraded"); health.textContent = "Health check unavailable"; });
}

function formatBytes(value) {
  if (value < 1024) return `${value} B`;
  if (value < 1024 ** 2) return `${(value / 1024).toFixed(1)} KiB`;
  return `${(value / 1024 ** 2).toFixed(1)} MiB`;
}
