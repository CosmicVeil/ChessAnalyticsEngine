# Analysis Verdict Disclosure Design

## Goal

Make every completed game readable before expansion and make its disclosure control work with mouse, touch, and keyboard input.

## Root cause

The original button toggled the detail element's `hidden` attribute. The author rule `.game-detail { display: flex; }` overrode the browser's default `[hidden] { display: none; }`, so the button updated its state without changing what the user saw.

## Result-card presentation

Each game keeps four source-ordered columns:

1. Game and player metadata.
2. PyTorch verdict.
3. XGBoost verdict.
4. Analysis state.

A completed model cell reads `Says cheating` when its stored prediction is true and `No cheating signal` when false. A pending model reads `Waiting`. The column headings use `PyTorch verdict` and `XGBoost verdict`.

The wording attributes the decision to the model. It does not label either player as a cheater.

## Disclosure interaction

Each card uses one native `<details>` element spanning the full card width. Its `<summary>` reads `Details`. Native disclosure supplies pointer and keyboard behavior without a custom click listener.

When an SSE update rebuilds an existing card, the renderer reads the old disclosure's `open` state and applies it to the replacement. A user who opened a result will not have it collapse because the job counters changed.

Failed and pending games use the same disclosure. Failed games show their structured error; pending games explain that both model workers have not returned yet.

## Expanded content

For each completed model, details show:

- the model score formatted as a percentage;
- whether the score is above or below the 50% decision threshold;
- the resulting `says cheating` or `no cheating signal` decision.

The detail closes with model agreement or disagreement and this interpretation: the percentages are uncalibrated model scores, not measured accuracy, proof, or a calibrated probability that cheating occurred.

For a false prediction, the threshold explanation ends with `The model produced no cheating signal.` This keeps the expanded wording consistent with the collapsed verdict.

## Filtering

Agreement and minimum-score filters continue to use the stored prediction booleans and raw scores. Moving percentages into the disclosure does not change API payloads, filtering, persistence, Kafka topics, or model inference.

## Accessibility and responsive behavior

The summary keeps a visible focus outline and a minimum touch target. The disclosure marker and label show open/closed state without relying on color. On narrow screens, verdict cells remain above the disclosure and the detail content stacks vertically.

## Testing

Automated browser-side behavior tests will render a card with one positive and one negative prediction and verify:

- the collapsed row shows both plain-language verdicts and hides percentages;
- activating `Details` reveals both percentages and their threshold explanations;
- a subsequent update preserves the open state;
- pending and failed cards remain understandable.

The existing page, JavaScript syntax, Python, and Go suites must remain green. A browser pass will verify pointer interaction, keyboard activation, responsive layout, and console output.

## Delivery

After the copy test and full regression suite pass, merge the complete `feature/more-features` history into local `main`. Preserve the user's unstaged `ml-model/train_torch.py` changes throughout the merge and leave them unstaged on `main`.
