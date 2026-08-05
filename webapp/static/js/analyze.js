const form = document.querySelector("[data-analysis-form]");
if (form) {
  const buttons = [...document.querySelectorAll("[data-mode-button]")];
  const panels = [...document.querySelectorAll("[data-mode-panel]")];
  const status = document.querySelector("[data-form-status]");
  let mode = "file";

  function selectMode(nextMode) {
    mode = nextMode;
    for (const button of buttons) button.setAttribute("aria-selected", String(button.dataset.modeButton === mode));
    for (const panel of panels) {
      const selected = panel.dataset.modePanel === mode;
      panel.hidden = !selected;
      panel.classList.toggle("is-active", selected);
    }
    status.textContent = "";
  }
  buttons.forEach((button) => button.addEventListener("click", () => selectMode(button.dataset.modeButton)));

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
    if (mode === "file" && fileInput.files[0]) data.append("pgn_file", fileInput.files[0]);
    if (mode === "paste") data.append("pgn_text", textarea.value);
    if (mode === "lichess") data.append("lichess_url", form.elements.lichess_url.value);
    form.classList.add("is-submitting");
    form.querySelector("button[type=submit]").disabled = true;
    status.textContent = "Stamping source and opening a durable job…";
    try {
      const response = await fetch("/api/analyses", { method: "POST", body: data });
      const body = await response.json();
      if (!response.ok) throw new Error(body.error?.message || "The source was not accepted.");
      status.textContent = "Accepted. Moving to the live ledger…";
      window.location.assign(body.links.page);
    } catch (error) {
      status.textContent = error.message;
      form.classList.remove("is-submitting");
      form.querySelector("button[type=submit]").disabled = false;
    }
  });
}

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
