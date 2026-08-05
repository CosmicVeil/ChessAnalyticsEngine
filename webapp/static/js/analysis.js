export function jobConnectionState(status) {
  const normalized = status === "analyzing" ? "live" : status;
  if (normalized === "parsing") {
    return { label: "Reading PGN", terminal: false, failed: false };
  }
  if (normalized === "live") {
    return { label: "Live analysis", terminal: false, failed: false };
  }
  if (["completed", "partial", "failed"].includes(normalized)) {
    return {
      label: `Batch ${normalized}`,
      terminal: true,
      failed: normalized === "failed",
    };
  }
  return {
    label: "Waiting for ingestion",
    terminal: false,
    failed: false,
  };
}

const root = document.querySelector("[data-job-id]");
if (root) {
  const jobId = root.dataset.jobId;
  const ledger = document.querySelector("[data-game-ledger]");
  const empty = document.querySelector("[data-empty]");
  const connection = document.querySelector("[data-connection]");
  const filters = document.querySelector("[data-filters]");
  const loadMore = document.querySelector("[data-load-more]");
  const cards = new Map();
  let nextCursor = null;
  let eventSource;
  let latestJobStatus = "queued";

  function renderJobConnection(status = latestJobStatus) {
    const state = jobConnectionState(status);
    connection.classList.toggle("is-offline", state.failed);
    connection.querySelector("span").textContent = state.label;
    return state;
  }

  function updateJob(job) {
    latestJobStatus = job.status;
    document.querySelector("[data-source-name]").textContent = job.source_name;
    for (const [key, value] of Object.entries(job.counts)) {
      const target = document.querySelector(`[data-count-key="${key}"]`);
      if (target) rollNumber(target, Number(target.textContent.replace(/\D/g, "")) || 0, value);
    }
    const terminal = job.counts.completed + job.counts.failed;
    document.querySelector("[data-progress]").style.transform = `scaleX(${job.counts.total ? terminal / job.counts.total : 0})`;
    const connectionState = renderJobConnection();
    if (connectionState.terminal) {
      eventSource?.close();
    }
  }

  function upsertGame(game) {
    const existing = cards.get(game.id);
    const card = buildCard(game, existing);
    if (!existing) {
      const following = [...ledger.children].find((node) => Number(node.dataset.sequence) > game.sequence);
      ledger.insertBefore(card, following || null);
      cards.set(game.id, card);
      requestAnimationFrame(() => card.classList.remove("is-new"));
    }
    empty.hidden = cards.size > 0;
    applyVisibility(card);
  }

  function applyVisibility(card) {
    const data = new FormData(filters);
    const status = data.get("status");
    const agreement = data.get("agreement");
    const minimum = Number(data.get("score_min"));
    const score = card.dataset.score === "" ? null : Number(card.dataset.score);
    card.hidden = (status !== "all" && card.dataset.status !== status)
      || (agreement !== "all" && card.dataset.agreement !== agreement)
      || (data.get("score_min") !== "" && (score === null || score < minimum));
  }

  async function loadStatus() {
    const response = await fetch(`/api/analyses/${jobId}`);
    if (!response.ok) throw new Error("Analysis job not found");
    updateJob((await response.json()).job);
  }

  async function loadGames(reset = false) {
    if (reset) { cards.clear(); ledger.replaceChildren(); nextCursor = null; }
    const params = new URLSearchParams(new FormData(filters));
    params.set("limit", "50");
    if (nextCursor) params.set("cursor", nextCursor);
    const response = await fetch(`/api/analyses/${jobId}/games?${params}`);
    if (!response.ok) return;
    const page = await response.json();
    page.items.forEach(upsertGame);
    nextCursor = page.next_cursor;
    loadMore.hidden = !page.has_more;
    ledger.setAttribute("aria-busy", "false");
    empty.hidden = cards.size > 0;
  }

  filters.addEventListener("change", () => loadGames(true));
  filters.addEventListener("input", () => cards.forEach(applyVisibility));
  loadMore.addEventListener("click", () => loadGames());

  Promise.all([loadStatus(), loadGames(true)]).then(() => {
    if (renderJobConnection().terminal) return;
    eventSource = new EventSource(`/api/analyses/${jobId}/events`);
    eventSource.addEventListener("open", () => renderJobConnection());
    eventSource.addEventListener("error", () => {
      if (jobConnectionState(latestJobStatus).terminal) {
        renderJobConnection();
        return;
      }
      connection.classList.add("is-offline");
      connection.querySelector("span").textContent = "Reconnecting to live analysis";
    });
    eventSource.addEventListener("job.updated", (event) => updateJob(JSON.parse(event.data).job));
    eventSource.addEventListener("game.updated", (event) => {
      const payload = JSON.parse(event.data);
      updateJob(payload.job);
      upsertGame(payload.game);
    });
  }).catch((error) => {
    connection.classList.add("is-offline");
    connection.querySelector("span").textContent = error.message;
  });

  function rollNumber(target, from, to) {
    if (window.matchMedia("(prefers-reduced-motion: reduce)").matches || from === to) { target.textContent = to; return; }
    const start = performance.now();
    const tick = (now) => {
      const progress = Math.min(1, (now - start) / 350);
      target.textContent = Math.round(from + (to - from) * progress);
      if (progress < 1) requestAnimationFrame(tick);
    };
    requestAnimationFrame(tick);
  }
}

export function buildCard(game, existing) {
  const detailWasOpen = existing?.querySelector("details.game-detail")?.open ?? false;
  const card = existing || element("article", "game-card is-new");
  card.dataset.sequence = game.sequence;
  card.dataset.status = game.status;
  const scoreValues = game.scores ? [game.scores.pytorch.score, game.scores.xgboost.score] : [];
  card.dataset.score = scoreValues.length ? Math.max(...scoreValues) : "";
  card.dataset.agreement = game.scores ? (game.scores.agreement ? "agree" : "disagree") : "pending";
  card.replaceChildren();

  const identity = element("div", "game-id");
  identity.append(element("span", "game-sequence", String(game.sequence).padStart(3, "0")));
  const players = element("div", "game-players");
  players.append(playerLine(game.white), playerLine(game.black));
  players.append(element("div", "game-meta", [game.opening, game.result, game.played_at?.slice(0, 10)].filter(Boolean).join(" · ") || "Headers unavailable"));
  identity.append(players);
  card.append(identity);
  card.append(verdictCell("pytorch", "PyTorch", game.scores?.pytorch));
  card.append(verdictCell("xgb", "XGBoost", game.scores?.xgboost));

  const state = element("div", "game-state");
  state.append(element("span", `state-label ${game.status}`, game.status));
  card.append(state);

  const detail = element("details", "game-detail");
  detail.open = detailWasOpen;
  detail.append(element("summary", "detail-toggle", "Details"));
  const body = element("div", "game-detail-body");
  if (game.error) {
    body.append(element("p", "game-detail-note", `${game.error.code}: ${game.error.message}`));
  }
  if (game.scores) {
    body.append(
      modelDetail("PyTorch", game.scores.pytorch),
      modelDetail("XGBoost", game.scores.xgboost),
      element("p", "game-detail-note", game.scores.agreement ? "Models agree." : "Models disagree."),
      element("p", "game-detail-note score-caveat", "These percentages are uncalibrated model scores, not measured accuracy, proof, or a calibrated probability that cheating occurred."),
    );
  }
  if (!body.children.length) {
    body.append(element("p", "game-detail-note", "Awaiting both model workers."));
  }
  detail.append(body);
  card.append(detail);
  return card;
}

function playerLine(player) {
  const line = element("div", "player-line");
  line.append(element("span", "", player.name || "Unknown player"));
  line.append(element("small", "", player.elo ?? "—"));
  return line;
}

function verdictCell(kind, modelName, score) {
  const cell = element("div", `model-verdict ${kind}`);
  const verdict = score
    ? (score.prediction ? "Says cheating" : "No cheating signal")
    : "Waiting";
  cell.setAttribute("aria-label", `${modelName}: ${verdict}`);
  cell.append(element("strong", score?.prediction ? "positive" : "", verdict));
  return cell;
}

function modelDetail(modelName, score) {
  const detail = element("section", "model-detail");
  detail.append(
    element("h3", "", modelName),
    element("strong", "model-detail-score", percent(score.score)),
    element(
      "p",
      "",
      score.prediction
        ? "This score is at or above the 50% decision threshold. The model says cheating."
        : "This score is below the 50% decision threshold. The model produced no cheating signal.",
    ),
  );
  return detail;
}

function element(tag, className = "", text = "") {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== "") node.textContent = text;
  return node;
}

function percent(value) {
  return new Intl.NumberFormat(undefined, { style: "percent", maximumFractionDigits: 1 }).format(value);
}
