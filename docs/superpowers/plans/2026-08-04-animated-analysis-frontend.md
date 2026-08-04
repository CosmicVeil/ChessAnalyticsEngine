# Animated Analysis Frontend Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Deliver four responsive Flask-rendered pages with layered chess imagery, scroll-driven motion, PGN submission, and replay-safe live analysis updates.

**Architecture:** Jinja templates render a complete semantic baseline. Vanilla ES modules enhance upload progress, motion, filtering, and SSE without replacing server content. Independent RGBA chess-piece assets provide depth while CSS tokens and strict visual rules keep the site away from generic AI dashboard styling.

**Tech Stack:** Flask/Jinja, HTML, CSS, vanilla JavaScript ES modules, native `node --test`, Server-Sent Events, PNG with alpha.

## Global Constraints

- Use warm paper, ink black, vermilion, and acidic green; use no gradients.
- Use square controls, thin rules, board coordinates, and editorial type.
- Exclude glassmorphism, neon purple or blue, glowing blobs, floating orbs, excessive pills, fake terminal decorations, and repeated rounded card grids.
- Keep benchmark figures labeled `local benchmark` and model scores labeled as estimates rather than proof or calibrated probability.
- Use separate transparent PNG layers instead of one baked hero composition.
- Support keyboard navigation, 200 percent zoom, touch targets, and `prefers-reduced-motion`.
- Keep JavaScript progressive; file and paste submission must retain usable HTML structure without scripts.

---

### Task 1: Build the shared shell and page routes

**Files:**
- Create: `webapp/pages.py`
- Create: `webapp/templates/base.html`
- Create: `webapp/templates/partials/_site_header.html`
- Create: `webapp/templates/partials/_site_footer.html`
- Create: `webapp/templates/pages/landing.html`
- Create: `webapp/templates/pages/analyze.html`
- Create: `webapp/templates/pages/analysis.html`
- Create: `webapp/templates/pages/system.html`
- Create: `webapp/static/css/tokens.css`
- Create: `webapp/static/css/base.css`
- Create: `webapp/static/css/components.css`
- Create: `webapp/static/css/pages.css`
- Create: `webapp/static/css/motion.css`
- Create: `tests/webapp/test_pages.py`
- Modify: `webapp/__init__.py`

**Interfaces:**
- Produces `GET /`, `/analyze`, `/analyses/<uuid>`, and `/system`.
- Analysis page receives `JobView`, first `Page[GameView]`, and `latest_event_id`.

- [ ] **Step 1: Write failing page-route tests**

For all four pages, assert status 200, one `<main id="main-content">`, one visible `<h1>`, a skip link, active navigation, and page-specific body classes. Assert rendered analysis HTML contains no spool path or raw PGN text.

- [ ] **Step 2: Run `tests.webapp.test_pages` and confirm missing routes/templates**

- [ ] **Step 3: Implement page blueprint and shared shell**

`base.html` must load CSS in token-to-page order and `app.mjs` with `type="module"`. It includes a skip link, semantic header/navigation, `<main>`, footer, and a restrained connection-status region.

- [ ] **Step 4: Implement tokens and baseline layout**

Define these token families:

```css
:root {
  --paper: #efece2;
  --paper-deep: #d8d2c2;
  --ink: #10110f;
  --ink-muted: #5d5b53;
  --vermilion: #f04f2d;
  --acid: #c8f24b;
  --rule: 1px;
  --content: 86rem;
}
```

Use native serif and monospace stacks, visible focus outlines, `box-sizing: border-box`, square borders, fluid type with bounded `clamp`, and no gradient declarations.

- [ ] **Step 5: Run focused tests and a static anti-cliche scan**

Scan CSS/template files for `linear-gradient`, `radial-gradient`, `backdrop-filter`, purple palette values, and generic `AI-powered` copy; expect no matches.

- [ ] **Step 6: Commit Task 1**

Commit with `feat: add editorial Flask page shell`.

### Task 2: Create and validate isolated transparent chess assets

**Files:**
- Create: `webapp/static/images/pieces/king-ink.png`
- Create: `webapp/static/images/pieces/knight-vermilion.png`
- Create: `webapp/static/images/pieces/rook-ink.png`
- Create: `webapp/static/images/pieces/pawn-acid.png`
- Create: `tests/webapp/test_visual_assets.py`

**Interfaces:**
- Each asset is an RGBA PNG at least 1200 by 1200 pixels with transparent corners.
- Each template use is decorative: empty alt text, `aria-hidden="true"`, and no pointer events.

- [ ] **Step 1: Write failing PNG structure tests**

Read the PNG signature and IHDR with the standard library. Assert all four paths exist, width and height meet the floor, color type equals `6` for RGBA, file size stays below 4 MiB, and template references carry decorative semantics.

- [ ] **Step 2: Run the asset test and confirm missing files**

- [ ] **Step 3: Generate four consistent linocut pieces through the image-generation workflow**

Generate one piece per request on a flat removable chroma background, remove the key through the approved helper, validate alpha corners and subject coverage, then copy the final files to the exact paths. Prompts specify no text, watermark, glow, rectangular background, cast shadow, or floor plane.

- [ ] **Step 4: Inspect each asset against paper and ink surfaces**

Use the local image viewer for edges and composition. Regenerate only the failed piece or retry chroma removal with edge contraction when a fringe remains.

- [ ] **Step 5: Run the asset tests and commit**

Commit with `feat: add isolated chess motion assets`.

### Task 3: Build the landing and system stories

**Files:**
- Modify: `webapp/templates/pages/landing.html`
- Modify: `webapp/templates/pages/system.html`
- Modify: `webapp/static/css/pages.css`
- Modify: `webapp/static/css/components.css`
- Modify: `tests/webapp/test_pages.py`

**Interfaces:**
- Landing primary CTA points to `/analyze`; secondary CTA points to `/system`.
- System page names the five existing Kafka topics and shows separated model lanes.

- [ ] **Step 1: Add failing copy and structure tests**

Require the hero copy `Every game leaves a statistical trail`, both CTA URLs, all four benchmark values, one `local benchmark` qualifier per value, and a non-guarantee note. Require the system stages `PGN ingestion`, `Go move features`, `Game aggregation`, `PyTorch`, `XGBoost`, and `Joined result`.

- [ ] **Step 2: Build the landing composition**

Use separate `[data-scroll-layer][data-depth]` elements for board coordinates, knight, rook, and ledger labels. Add a compact result specimen and a ruled benchmark ledger rather than feature cards.

- [ ] **Step 3: Build the system composition**

Create one `[data-scroll-scene]` with five numbered stages. Split PyTorch and XGBoost into parallel lanes and join them at `chess-model-predictions`, then show `chess-predictions` flowing to Flask. Explain L2 normalization, sigmoid, saved XGBoost min/range, and the score disclaimer in page copy.

- [ ] **Step 4: Run page tests and commit**

Commit with `feat: tell the streaming analysis story`.

### Task 4: Build the PGN analysis desk

**Files:**
- Modify: `webapp/templates/pages/analyze.html`
- Create: `webapp/static/js/app.mjs`
- Create: `webapp/static/js/analyze.mjs`
- Modify: `webapp/static/css/pages.css`
- Modify: `webapp/static/css/components.css`
- Modify: `tests/webapp/test_pages.py`

**Interfaces:**
- Submits multipart fields `pgn_file`, `pgn_text`, or `lichess_url` to `POST /api/analyses`.
- Reads upload limits and health URL from safe `data-*` attributes.

- [ ] **Step 1: Write failing semantic form tests**

Require a numbered radio fieldset, associated labels, file input, textarea, URL input, error-summary target, progress element, submit button, source-limit copy, privacy copy, and a normal form action/method.

- [ ] **Step 2: Implement progressive source switching**

`analyze.mjs` enables only controls for the active mode, supports keyboard file selection and drag/drop, checks file size without reading file content, and counts pasted UTF-8 bytes with `TextEncoder`.

- [ ] **Step 3: Implement upload progress and responses**

Use `XMLHttpRequest` with `FormData` so `xhr.upload.onprogress` can update a native `<progress>`. On HTTP 202 redirect to `analysis_url`. Focus the error summary on failure. Keep the Lichess URL when the API returns `connector_unavailable`.

- [ ] **Step 4: Add health behavior**

Poll `/api/health`. Database failure disables submission. Kafka degradation leaves submission enabled and explains that accepted jobs remain queued.

- [ ] **Step 5: Run page tests and commit**

Commit with `feat: add PGN upload and paste desk`.

### Task 5: Implement replay-safe live analysis state

**Files:**
- Create: `webapp/static/js/analysis-state.mjs`
- Create: `webapp/static/js/analysis-stream.mjs`
- Modify: `webapp/templates/pages/analysis.html`
- Create: `webapp/templates/partials/_game_row.html`
- Modify: `webapp/static/css/pages.css`
- Create: `tests/web_js/analysis-state.test.mjs`
- Modify: `tests/webapp/test_pages.py`

**Interfaces:**
- Produces JS functions `reduceAnalysisState`, `matchesFilters`, `upsertGameInSequence`, and `formatModelScore`.
- Consumes SSE events `job.updated` and `game.updated`.
- Consumes paginated `GET /api/analyses/<uuid>/games`.

- [ ] **Step 1: Write failing native Node reducer tests**

Cover replayed events, source-order insertion, update-in-place by game ID, status filters, agreement filters, inclusive score range, terminal states, and `0.8437 -> "84.37%"` formatting. Run with `node --test tests/web_js/analysis-state.test.mjs`.

- [ ] **Step 2: Implement pure state helpers**

`upsertGameInSequence` returns a new array sorted by numeric sequence. `reduceAnalysisState` ignores event IDs at or below `lastEventId` and preserves unrelated games.

- [ ] **Step 3: Build the server-rendered analysis baseline**

Render counts, progress rail, job status, first 50 game rows, filters, load-more control, a single polite live region, score disclaimer, `<noscript>` refresh link, and embedded JSON containing only `JobView`, `GameView`, and latest event ID.

- [ ] **Step 4: Implement SSE enhancement**

Open the stream with `?after=<latest_event_id>`, update existing DOM in place, insert by sequence, deduplicate by ID, and close on completed, partial, or failed jobs. Show a reconnecting mark after five seconds. Native EventSource handles later `Last-Event-ID` headers.

- [ ] **Step 5: Implement filters and pagination**

Request pages of at most 50 rows using the approved query parameters. Apply `content-visibility: auto` to loaded rows.

- [ ] **Step 6: Run Node and Flask tests and commit**

Commit with `feat: stream game analysis into the browser`.

### Task 6: Add the shared motion system and reduced-motion behavior

**Files:**
- Create: `webapp/static/js/motion.mjs`
- Modify: `webapp/static/js/app.mjs`
- Modify: `webapp/static/css/motion.css`
- Create: `tests/web_js/motion-math.test.mjs`

**Interfaces:**
- Produces `initMotion(root=document)`, `computeSceneProgress(rect, viewportHeight)`, and `animateNumber(element, target, options)`.
- Recognizes `[data-motion="reveal"]`, `[data-scroll-scene]`, `[data-scroll-layer][data-depth]`, and `[data-number-roll]`.

- [ ] **Step 1: Write failing motion math tests**

Require progress clamping to zero and one, monotonic progress through a scene, immediate number assignment in reduced-motion mode, and cancellable number rolls.

- [ ] **Step 2: Implement one observer and one animation loop**

Use `IntersectionObserver` to add active scenes and one `requestAnimationFrame` loop to write CSS custom properties. Change only transform and opacity. Remove `will-change` when a scene exits.

- [ ] **Step 3: Implement the reduced-motion branch**

Under `prefers-reduced-motion: reduce`, disable parallax, sticky stage travel, number interpolation, game-row travel, and score-meter sweeps. Apply status and score values without delay.

- [ ] **Step 4: Run Node tests, scan for scroll listeners outside the motion module, and commit**

Commit with `feat: add layered scroll motion`.

### Task 7: Perform browser visual and accessibility verification

**Files:**
- Modify only files with verified visual defects.
- Create: `docs/frontend-verification.md`

**Interfaces:**
- Verifies 1440 by 1000, 768 by 1024, and 390 by 844 viewports plus 200 percent zoom.

- [ ] **Step 1: Start Flask with PostgreSQL and seeded page states**

Create test jobs for empty, parsing, analyzing, model agreement, disagreement, failed game, partial job, reconnect, and terminal states through public repository/service interfaces.

- [ ] **Step 2: Inspect all pages with the browser-control skill**

Check layout, scroll layers, transparent edges on paper and ink, focus order, keyboard-only use, touch targets, heading hierarchy, horizontal overflow, console errors, and live-region restraint.

- [ ] **Step 3: Emulate reduced motion and repeat the interaction path**

Confirm immediate updates and the absence of parallax, travel, rolls, and sweeps.

- [ ] **Step 4: Fix only reproduced defects and rerun covering tests**

- [ ] **Step 5: Record the verification matrix and commit**

Commit with `test: verify animated analysis frontend`.
