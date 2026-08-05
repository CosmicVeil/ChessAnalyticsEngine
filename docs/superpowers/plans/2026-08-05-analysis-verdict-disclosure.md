# Analysis Verdict Disclosure Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Deliver a verdict-first native disclosure with the approved `No cheating signal` copy and merge the complete feature into local `main`.

**Architecture:** Keep the API and stored result shape unchanged. The browser renderer converts each model prediction into a visible binary verdict, builds one native `<details>` disclosure per card, and preserves that disclosure's open state when SSE updates rebuild the card. A browser harness exercises the real renderer and native DOM interaction; after verification, a temporary worktree merges the feature into local `main` without moving the user's unstaged training edit.

**Tech Stack:** Flask/Jinja, browser-native ES modules and `<details>`, CSS Grid, Python `unittest`, in-app browser QA.

## Global Constraints

- A true prediction reads `Says cheating`; a false prediction reads `No cheating signal`; a missing prediction reads `Waiting`.
- Percentages appear only inside the expanded disclosure.
- Details explain the 50% decision threshold and state that scores are uncalibrated model scores, not proof, measured accuracy, or calibrated probability.
- An SSE rebuild preserves an existing disclosure's `open` state.
- Do not change API payloads, persistence, Kafka topics, model inference, filtering, or the user's unstaged `ml-model/train_torch.py` changes.
- Merge the complete `feature/more-features` history into local `main`; do not push or delete the feature branch.
- Keep the existing editorial visual system and reduced-motion behavior.

---

### Task 1: Verdict-first result card and native disclosure

**Files:**
- Create: `tests/frontend/analysis_card_browser.html`
- Modify: `tests/web_app/test_pages.py`
- Modify: `webapp/static/js/analysis.js`
- Modify: `webapp/templates/analysis.html`
- Modify: `webapp/static/css/site.css`

**Interfaces:**
- Consumes: the existing `game.scores.{pytorch,xgboost}.{score,prediction}` API shape and `game.scores.agreement` boolean.
- Produces: exported `buildCard(game: object, existing?: HTMLElement): HTMLElement` for the production ledger and real-DOM browser harness.

- [ ] **Step 1: Add the failing page-heading test**

Add this test to `FrontendPageTests` in `tests/web_app/test_pages.py`:

```python
def test_live_ledger_leads_with_verdicts_instead_of_percentages(self):
    response = self.client.get(f"/analyses/{uuid.uuid4()}")

    self.assertEqual(response.status_code, 200)
    self.assertIn(b"PyTorch verdict", response.data)
    self.assertIn(b"XGBoost verdict", response.data)
    self.assertNotIn(b"PyTorch score</span>", response.data)
    self.assertNotIn(b"XGBoost score</span>", response.data)
```

- [ ] **Step 2: Add the real-DOM browser harness**

Create `tests/frontend/analysis_card_browser.html`. Import `buildCard` from `../../webapp/static/js/analysis.js`, render this literal game, and record checks in `document.documentElement.dataset.testStatus`:

```html
<!doctype html>
<html lang="en" data-test-status="running">
  <head>
    <meta charset="utf-8">
    <link rel="stylesheet" href="../../webapp/static/css/site.css">
    <title>Analysis card browser test</title>
  </head>
  <body>
    <main id="fixture"></main>
    <pre id="report"></pre>
    <script type="module">
      import { buildCard } from "../../webapp/static/js/analysis.js";

      const game = {
        id: "game-1",
        sequence: 1,
        status: "completed",
        white: { name: "Ada", elo: 1800 },
        black: { name: "Grace", elo: 1790 },
        opening: "Sicilian Defense",
        result: "1-0",
        played_at: "2026-08-05T12:00:00+00:00",
        error: null,
        scores: {
          pytorch: { score: 0.81, prediction: true },
          xgboost: { score: 0.375, prediction: false },
          agreement: false
        }
      };
      const failures = [];
      const check = (condition, message) => { if (!condition) failures.push(message); };
      const fixture = document.querySelector("#fixture");
      const card = buildCard(game);
      fixture.append(card);

      check(card.textContent.includes("Says cheating"), "positive verdict missing");
      check(card.textContent.includes("No cheating signal"), "negative verdict missing");
      const disclosure = card.querySelector("details.game-detail");
      check(Boolean(disclosure), "native disclosure missing");
      check(!disclosure.open, "disclosure should start closed");
      check(disclosure.textContent.includes("81%"), "PyTorch percentage missing from details");
      check(disclosure.textContent.includes("37.5%"), "XGBoost percentage missing from details");
      check(disclosure.querySelector(".model-detail").getClientRects().length === 0, "closed details expose percentages");
      disclosure.querySelector("summary").click();
      await new Promise(requestAnimationFrame);
      check(disclosure.open, "summary click did not open details");
      check(disclosure.querySelector(".model-detail").getClientRects().length > 0, "opened details hide percentages");

      const updated = buildCard({ ...game, status: "completed" }, card);
      check(updated.querySelector("details.game-detail").open, "SSE rebuild lost open state");
      check(updated.textContent.includes("50% decision threshold"), "threshold meaning missing");
      check(updated.textContent.includes("uncalibrated model scores"), "score caveat missing");

      document.querySelector("#report").textContent = failures.join("\n") || "PASS";
      document.documentElement.dataset.testStatus = failures.length ? "failed" : "passed";
    </script>
  </body>
</html>
```

- [ ] **Step 3: Run both tests and verify RED**

Run:

```bash
.venv/bin/python -m unittest tests.web_app.test_pages.FrontendPageTests.test_live_ledger_leads_with_verdicts_instead_of_percentages -v
```

Expected: FAIL because the rendered headings still contain `PyTorch score` and `XGBoost score`.

Serve the repository root:

```bash
.venv/bin/python -m http.server 8765 --bind 127.0.0.1
```

Open `http://127.0.0.1:8765/tests/frontend/analysis_card_browser.html` in the in-app browser.

Expected: the harness remains `data-test-status="running"` and the console reports that `analysis.js` does not export `buildCard`.

- [ ] **Step 4: Implement verdict cells and the native disclosure**

In `webapp/static/js/analysis.js`:

1. Export `buildCard`.
2. Read the previous state before `replaceChildren()`:

```javascript
const detailWasOpen = existing?.querySelector("details.game-detail")?.open ?? false;
```

3. Replace both `scoreCell` calls with `verdictCell`:

```javascript
function verdictCell(kind, score) {
  const cell = element("div", `model-verdict ${kind}`);
  const verdict = score
    ? (score.prediction ? "Says cheating" : "No cheating signal")
    : "Waiting";
  cell.append(element("strong", score?.prediction ? "positive" : "", verdict));
  return cell;
}
```

4. Replace the custom button and hidden `div` with a native disclosure:

```javascript
const detail = element("details", "game-detail");
detail.open = detailWasOpen;
detail.append(element("summary", "detail-toggle", "Details"));
const body = element("div", "game-detail-body");
```

5. For each completed model, append a `.model-detail` block containing its name, formatted score, and one of these literal explanations:

```text
This score is at or above the 50% decision threshold. The model says cheating.
This score is below the 50% decision threshold. The model produced no cheating signal.
```

6. Append `Models agree.` or `Models disagree.` and this caveat:

```text
These percentages are uncalibrated model scores, not measured accuracy, proof, or a calibrated probability that cheating occurred.
```

7. Keep pending text as `Awaiting both model workers.` and preserve structured error text for failed games.

In `webapp/templates/analysis.html`, change the ledger heading to `Game-by-game verdicts` and the column labels to `PyTorch verdict` and `XGBoost verdict`.

In `webapp/static/css/site.css`, remove the score-meter rules and style:

```css
.model-verdict strong { font: .72rem/1.3 var(--mono); text-transform: uppercase; }
.model-verdict strong.positive { color: var(--red); }
.game-detail { grid-column: 1 / -1; border-top: 1px dashed var(--rule); }
.game-detail > summary { min-height: 2.75rem; display: flex; align-items: center; justify-content: space-between; cursor: pointer; list-style: none; font: .58rem/1 var(--mono); text-transform: uppercase; }
.game-detail > summary::-webkit-details-marker { display: none; }
.game-detail > summary::after { content: "+"; font-size: 1rem; }
.game-detail[open] > summary::after { content: "−"; }
.game-detail-body { padding: 1rem 0 0 4.2rem; display: grid; grid-template-columns: 1fr 1fr; gap: 1rem 2rem; font: .62rem/1.5 var(--mono); text-transform: uppercase; }
.game-detail-note { grid-column: 1 / -1; }
```

At the narrow breakpoint, set `.game-detail-body` to one column with no left padding.

- [ ] **Step 5: Verify GREEN in the focused tests**

Run:

```bash
.venv/bin/python -m unittest tests.web_app.test_pages -v
node --check webapp/static/js/analysis.js
```

Expected: all page tests pass and JavaScript syntax is valid.

Reload the browser harness. Expected:

- `document.documentElement.dataset.testStatus` equals `passed`;
- the report reads `PASS`;
- clicking and keyboard-activating `Details` opens the native disclosure;
- the open disclosure stays open after `buildCard` updates the existing card;
- the console contains no errors.

- [ ] **Step 6: Run full regression verification**

Run:

```bash
.venv/bin/python -m unittest discover -s tests -v
GOCACHE=/private/tmp/chessanalytics-go-cache go -C go-backend test -count=1 ./...
node --check webapp/static/js/main.js
node --check webapp/static/js/analyze.js
node --check webapp/static/js/analysis.js
git diff --check
```

Expected: 107 or more Python tests pass with only service-gated skips, Go tests pass, all JavaScript syntax checks pass, and `git diff --check` is silent.

- [ ] **Step 7: Commit only the disclosure change**

```bash
git add tests/frontend/analysis_card_browser.html tests/web_app/test_pages.py webapp/static/js/analysis.js webapp/templates/analysis.html webapp/static/css/site.css
git commit -m "fix: make model verdict details interactive"
```

Confirm that `git status --short` still lists the user's `ml-model/train_torch.py` modification as unstaged.

---

### Task 2: Refine the negative verdict copy

**Files:**
- Modify: `tests/frontend/analysis_card_browser.html`
- Modify: `webapp/static/js/analysis.js`

**Interfaces:**
- Consumes: `buildCard(game, existing)` and the existing false `score.prediction` value.
- Produces: the collapsed verdict `No cheating signal` and expanded explanation `The model produced no cheating signal.`

- [ ] **Step 1: Change the browser assertions first**

In `tests/frontend/analysis_card_browser.html`, replace the negative-verdict assertion:

```javascript
check(
  card.querySelector(".model-verdict.xgb")?.textContent === "No cheating signal",
  "negative verdict missing",
);
```

After the existing `const updated = buildCard(...)` line, add:

```javascript
check(
  updated.textContent.includes("The model produced no cheating signal."),
  "negative threshold explanation missing",
);
check(!updated.textContent.includes("Doesn't say cheating"), "old negative copy remains");
```

Increment the module and stylesheet cache-buster query from `browser-test=1` to `browser-test=2` so the browser loads the pre-fix renderer during the RED run.

- [ ] **Step 2: Run the browser harness and verify RED**

Serve the repository root:

```bash
.venv/bin/python -m http.server 8765 --bind 127.0.0.1
```

Open `http://127.0.0.1:8765/tests/frontend/analysis_card_browser.html?copy-red=1`.

Expected: `data-test-status="failed"`; the report lists `negative verdict missing`, `negative threshold explanation missing`, and `old negative copy remains`.

- [ ] **Step 3: Make the two production copy changes**

In `verdictCell` within `webapp/static/js/analysis.js`, use:

```javascript
const verdict = score
  ? (score.prediction ? "Says cheating" : "No cheating signal")
  : "Waiting";
```

In `modelDetail`, use this false-prediction explanation:

```text
This score is below the 50% decision threshold. The model produced no cheating signal.
```

- [ ] **Step 4: Verify GREEN and run regressions**

Reload the browser harness with `?copy-green=1`.

Expected: `data-test-status="passed"`, the report reads `PASS`, pointer and keyboard activation still open the disclosure, and the console contains no errors.

Run:

```bash
.venv/bin/python -m unittest discover -s tests -v
GOCACHE=/private/tmp/chessanalytics-go-cache go -C go-backend test -count=1 ./...
node --check webapp/static/js/main.js
node --check webapp/static/js/analyze.js
node --check webapp/static/js/analysis.js
git diff --check
```

Expected: 107 or more Python tests pass with only service-gated skips, Go tests pass, JavaScript syntax is valid, and `git diff --check` is silent.

- [ ] **Step 5: Commit the copy revision**

```bash
git add tests/frontend/analysis_card_browser.html webapp/static/js/analysis.js
git commit -m "fix: refine negative model verdict"
```

Confirm `ml-model/train_torch.py` remains unstaged.

---

### Task 3: Merge the complete feature into local main

**Files:**
- Merge: `feature/more-features` into `main`
- Preserve: `ml-model/train_torch.py` as an unstaged user modification

**Interfaces:**
- Consumes: the fully verified `feature/more-features` branch and local `main`.
- Produces: local `main` containing the complete feature history and the unchanged user working-tree edit.

- [ ] **Step 1: Record the protected edit and preflight the merge**

Run from the original workspace:

```bash
git diff --binary -- ml-model/train_torch.py | shasum -a 256
git status --short
git merge-tree cb3044d3420ccae279427b04a768cd5ea7ae6e40 main feature/more-features
```

Record the hash. Status must list only ` M ml-model/train_torch.py`. The merge-tree output has one known add/add conflict, `README.md`; no application file conflicts are allowed.

- [ ] **Step 2: Create a temporary main worktree**

Confirm the task-specific path is unused, then let Git create the worktree:

```bash
test ! -e /private/tmp/chess-analytics-main-merge-20260805
git worktree add /private/tmp/chess-analytics-main-merge-20260805 main
```

If the first command fails, stop rather than reusing or deleting the existing path. The original workspace and its unstaged file remain untouched.

- [ ] **Step 3: Merge with the documented README resolution**

In the temporary main worktree, run:

```bash
git merge --no-ff -X theirs feature/more-features -m "Merge branch 'feature/more-features'"
git status --short
```

`-X theirs` resolves the known add/add `README.md` conflict in favor of the feature branch's comprehensive Flask, PostgreSQL, preprocessing, and operations documentation. Expected status: clean. If any other conflict appears, stop without resolving it.

- [ ] **Step 4: Verify the merged main tree**

Run the Python interpreter from the original environment against the temporary worktree:

```bash
/Users/mohandixit/Documents/GitHub/ChessAnalyticsEngine/.venv/bin/python -m unittest discover -s tests -v
GOCACHE=/private/tmp/chessanalytics-go-cache go -C go-backend test -count=1 ./...
node --check webapp/static/js/main.js
node --check webapp/static/js/analyze.js
node --check webapp/static/js/analysis.js
git diff --check
```

Expected: 107 or more Python tests pass with only service-gated skips, Go tests pass, JavaScript syntax is valid, and the merged worktree is clean.

- [ ] **Step 5: Remove the temporary worktree and switch the original workspace**

From the original repository, remove only the newly created clean worktree:

```bash
git worktree remove /private/tmp/chess-analytics-main-merge-20260805
git worktree prune
git switch main
```

Do not stash, stage, or commit `ml-model/train_torch.py`. If `git switch main` refuses, stop and leave the current workspace unchanged.

- [ ] **Step 6: Prove the user edit and main history are intact**

Run:

```bash
git diff --binary -- ml-model/train_torch.py | shasum -a 256
git status --short
git branch --show-current
git log --oneline -5
```

The before/after hashes must match, status must list only ` M ml-model/train_torch.py`, the branch must be `main`, and the log must show the merge commit containing the copy revision. Keep `feature/more-features`; do not push either branch.
