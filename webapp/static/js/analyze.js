import { mountLiveAnalysis } from "./analysis.js";

export function tabOrientation(isMobile) {
  return isMobile ? "vertical" : "horizontal";
}

export function tabTargetIndex(current, key, count, orientation) {
  if (key === "Home") return 0;
  if (key === "End") return count - 1;
  const nextKey = orientation === "vertical" ? "ArrowDown" : "ArrowRight";
  const previousKey = orientation === "vertical" ? "ArrowUp" : "ArrowLeft";
  if (key === nextKey) return (current + 1) % count;
  if (key === previousKey) return (current - 1 + count) % count;
  return null;
}

export function isPgnFile(file) {
  if (!file) return false;
  return /\.pgn$/i.test(file.name)
    || file.type === "application/x-chess-pgn";
}

export function mountSourceTabs(
  root = document,
  mobileQuery = window.matchMedia("(max-width: 640px)"),
) {
  const tablist = root.querySelector("[role=tablist]");
  const buttons = [...root.querySelectorAll("[data-mode-button]")];
  const panels = [...root.querySelectorAll("[data-mode-panel]")];
  const status = root.querySelector("[data-form-status]");
  let mode = buttons.find((button) => button.getAttribute("aria-selected") === "true")?.dataset.modeButton || "file";

  function syncOrientation() {
    tablist?.setAttribute("aria-orientation", tabOrientation(mobileQuery.matches));
  }

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
    const target = tabTargetIndex(
      current,
      key,
      buttons.length,
      tabOrientation(mobileQuery.matches),
    );
    if (target === null) return;
    selectMode(buttons[target].dataset.modeButton);
    buttons[target].focus();
  }

  for (const button of buttons) {
    button.addEventListener("click", () => selectMode(button.dataset.modeButton));
    button.addEventListener("keydown", (event) => {
      if (tabTargetIndex(
        buttons.indexOf(button),
        event.key,
        buttons.length,
        tabOrientation(mobileQuery.matches),
      ) === null) return;
      event.preventDefault();
      selectByKey(button, event.key);
    });
  }
  syncOrientation();
  mobileQuery.addEventListener("change", syncOrientation);
  selectMode(mode);
  return {
    get mode() { return mode; },
    close() { mobileQuery.removeEventListener("change", syncOrientation); },
  };
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
  const updateFileLabel = () => {
    const file = fileInput.files[0];
    fileLabel.textContent = file ? `${file.name} · ${formatBytes(file.size)}` : "Drop a batch file here";
  };
  fileInput.addEventListener("change", updateFileLabel);
  for (const eventName of ["dragenter", "dragover"]) {
    dropZone.addEventListener(eventName, (event) => {
      event.preventDefault();
      if (event.dataTransfer) event.dataTransfer.dropEffect = "copy";
      dropZone.classList.add("is-dragging");
    });
  }
  dropZone.addEventListener("dragleave", () => dropZone.classList.remove("is-dragging"));
  dropZone.addEventListener("drop", (event) => {
    event.preventDefault();
    dropZone.classList.remove("is-dragging");
    const file = event.dataTransfer?.files?.[0];
    if (!isPgnFile(file)) {
      status.textContent = "Drop one PGN file with a .pgn extension.";
      return;
    }
    const transfer = new DataTransfer();
    transfer.items.add(file);
    fileInput.files = transfer.files;
    updateFileLabel();
    status.textContent = "";
  });

  const textarea = form.elements.pgn_text;
  const pasteCount = document.querySelector("[data-paste-count]");
  textarea.addEventListener("input", () => { pasteCount.textContent = new TextEncoder().encode(textarea.value).length.toLocaleString(); });

  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    status.textContent = "";
    const data = new FormData();
    data.append("engine", form.elements.engine.value);
    data.append("csrf_token", form.elements.csrf_token.value);
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
      form.classList.remove("is-submitting");
      form.querySelector("button[type=submit]").disabled = false;
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
