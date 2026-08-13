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

export function validateScoreRange(values) {
  const minimum = parseScoreBound(values.get("score_min"), "Minimum");
  if (minimum.error) return minimum.error;
  const maximum = parseScoreBound(values.get("score_max"), "Maximum");
  if (maximum.error) return maximum.error;
  if (minimum.value !== null && maximum.value !== null && minimum.value > maximum.value) {
    return "Minimum score cannot exceed maximum score.";
  }
  return null;
}

function parseScoreBound(rawValue, label) {
  const raw = String(rawValue ?? "").trim();
  if (raw === "") return { value: null, error: null };
  const value = Number(raw);
  if (!Number.isFinite(value) || value < 0 || value > 1) {
    return { value: null, error: `${label} score must be a number between zero and one.` };
  }
  return { value, error: null };
}

export function eventStreamUrl(jobId, eventCursor) {
  return `/api/analyses/${jobId}/events?after=${eventCursor}`;
}

export function startLiveSession({ terminal, loadGames, openStream }) {
  void loadGames();
  if (!terminal) openStream();
}

export function createLoadArbiter() {
  let latestLoadId = 0;
  let liveRevision = 0;
  const liveGameRevisions = new Map();

  return {
    beginLoad() {
      return Object.freeze({ id: ++latestLoadId, liveRevision });
    },
    invalidate() {
      latestLoadId += 1;
    },
    isCurrent(load) {
      return load.id === latestLoadId;
    },
    markLive(gameId) {
      liveGameRevisions.set(gameId, ++liveRevision);
    },
    canApplyGame(load, gameId) {
      return load.id === latestLoadId
        && (liveGameRevisions.get(gameId) ?? 0) <= load.liveRevision;
    },
    canRemoveGame(load, gameId, fetchedIds) {
      return load.id === latestLoadId
        && !fetchedIds.has(gameId)
        && (liveGameRevisions.get(gameId) ?? 0) <= load.liveRevision;
    },
  };
}

const mountedControllers = new WeakMap();

export function mountLiveAnalysis(root, jobId) {
  mountedControllers.get(root)?.close();

  root.dataset.jobId = jobId;
  const ledger = root.querySelector("[data-game-ledger]");
  const empty = root.querySelector("[data-empty]");
  const connection = root.querySelector("[data-connection]");
  const filters = root.querySelector("[data-filters]");
  const filterStatus = root.querySelector("[data-filter-status]");
  const loadMore = root.querySelector("[data-load-more]");
  const cards = new Map();
  const loadArbiter = createLoadArbiter();
  let nextCursor = null;
  let eventSource;
  let latestJobStatus = "queued";
  let closed = false;

  function renderJobConnection(status = latestJobStatus) {
    const state = jobConnectionState(status);
    connection.classList.toggle("is-offline", state.failed);
    connection.querySelector("span").textContent = state.label;
    return state;
  }

  function updateJob(job) {
    latestJobStatus = job.status;
    root.querySelector("[data-source-name]").textContent = job.source_name;
    for (const [key, value] of Object.entries(job.counts)) {
      const target = root.querySelector(`[data-count-key="${key}"]`);
      if (target) rollNumber(target, Number(target.textContent.replace(/\D/g, "")) || 0, value);
    }
    const terminal = job.counts.completed + job.counts.failed;
    root.querySelector("[data-progress]").style.transform = `scaleX(${job.counts.total ? terminal / job.counts.total : 0})`;
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
    const maximum = Number(data.get("score_max"));
    const score = card.dataset.score === "" ? null : Number(card.dataset.score);
    card.hidden = (status !== "all" && card.dataset.status !== status)
      || (agreement !== "all" && card.dataset.agreement !== agreement)
      || (data.get("score_min") !== "" && (score === null || score < minimum))
      || (data.get("score_max") !== "" && (score === null || score > maximum));
  }

  async function loadStatus() {
    const response = await fetch(`/api/analyses/${jobId}`);
    if (!response.ok) throw new Error(await apiErrorMessage(response, "Analysis job not found"));
    const body = await response.json();
    if (closed) return 0;
    updateJob(body.job);
    return Number.isInteger(body.event_cursor) && body.event_cursor >= 0
      ? body.event_cursor
      : 0;
  }

  async function loadGames(reset = false) {
    const load = loadArbiter.beginLoad();
    const filterValues = new FormData(filters);
    const validationError = validateScoreRange(filterValues);
    if (validationError) {
      if (loadArbiter.isCurrent(load)) {
        ledger.setAttribute("aria-busy", "false");
        filterStatus.textContent = validationError;
      }
      return false;
    }
    ledger.setAttribute("aria-busy", "true");
    filterStatus.textContent = "";
    const params = new URLSearchParams(filterValues);
    params.set("limit", "50");
    if (!reset && nextCursor) params.set("cursor", nextCursor);
    try {
      const response = await fetch(`/api/analyses/${jobId}/games?${params}`);
      if (!response.ok) {
        throw new Error(await apiErrorMessage(response, "Games could not be loaded."));
      }
      const page = await response.json();
      if (closed || !loadArbiter.isCurrent(load)) return false;
      const fetchedIds = new Set(page.items.map((game) => game.id));
      page.items.forEach((game) => {
        if (loadArbiter.canApplyGame(load, game.id)) upsertGame(game);
      });
      if (reset) {
        for (const [gameId, card] of cards) {
          if (!loadArbiter.canRemoveGame(load, gameId, fetchedIds)) continue;
          card.remove();
          cards.delete(gameId);
        }
      }
      nextCursor = page.next_cursor;
      loadMore.hidden = !page.has_more;
      cards.forEach(applyVisibility);
      ledger.setAttribute("aria-busy", "false");
      filterStatus.textContent = "";
      empty.hidden = cards.size > 0;
      return true;
    } catch (error) {
      if (!closed && loadArbiter.isCurrent(load)) {
        ledger.setAttribute("aria-busy", "false");
        filterStatus.textContent = error.message;
      }
      return false;
    }
  }

  const handleFilterChange = () => { void loadGames(true); };
  const handleFilterInput = () => {
    loadArbiter.invalidate();
    ledger.setAttribute("aria-busy", "false");
    filterStatus.textContent = validateScoreRange(new FormData(filters)) || "";
  };
  const handleLoadMore = () => { void loadGames(); };

  filters.addEventListener("change", handleFilterChange);
  filters.addEventListener("input", handleFilterInput);
  loadMore.addEventListener("click", handleLoadMore);

  loadStatus().then((eventCursor) => {
    if (closed) return;
    startLiveSession({
      terminal: renderJobConnection().terminal,
      loadGames: () => loadGames(true),
      openStream: () => {
        eventSource = new EventSource(eventStreamUrl(jobId, eventCursor));
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
          loadArbiter.markLive(payload.game.id);
          upsertGame(payload.game);
        });
      },
    });
  }).catch((error) => {
    if (closed) return;
    ledger.setAttribute("aria-busy", "false");
    filterStatus.textContent = error.message;
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

  const controller = {
    close() {
      if (closed) return;
      closed = true;
      eventSource?.close();
      filters.removeEventListener("change", handleFilterChange);
      filters.removeEventListener("input", handleFilterInput);
      loadMore.removeEventListener("click", handleLoadMore);
      if (mountedControllers.get(root) === controller) mountedControllers.delete(root);
    },
  };
  mountedControllers.set(root, controller);
  return controller;
}

async function apiErrorMessage(response, fallback) {
  try {
    const body = await response.json();
    return body.error?.message || fallback;
  } catch {
    return fallback;
  }
}

const root = document.querySelector("[data-job-id]");
if (root?.dataset.jobId) mountLiveAnalysis(root, root.dataset.jobId);

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
