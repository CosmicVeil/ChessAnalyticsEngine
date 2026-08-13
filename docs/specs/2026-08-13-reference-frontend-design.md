# Reference Frontend Redesign

## Goal

Rebuild the Flask presentation layer to match the supplied dark navy chess-analysis references while keeping the existing upload API, durable job state, Kafka pipeline, model workers, and live SSE result flow intact.

## Route and page model

- `/` is the primary Home experience. It contains the PGN source controls and the live result ledger in one continuous page.
- `/analyses/<job_id>` renders the same Home shell with that job selected and starts its live result stream.
- `/analyze` redirects to `/` so old bookmarks remain useful.
- `/system` becomes the Information page.
- The navigation exposes Home, Information, and the project's GitHub link. It marks the current page and keeps a visible keyboard focus state.

## Home experience

The Home page uses a near-black navy canvas, thin blue-gray borders, restrained violet highlights, transparent chess-piece PNGs, and compact rounded panels. The hero centers the message “Live model scores from your PGN” above a three-tab source card:

- Upload PGN keeps drag-and-drop and file selection.
- Paste PGN keeps the byte counter and text input.
- Lichess Study remains visibly “Coming soon” and preserves the current unavailable-connector response.

Submitting a valid file or pasted PGN updates the browser URL to the returned `/analyses/<job_id>` path and mounts the live result view without navigating to a visually separate page. A direct visit or refresh of that URL restores the same job.

The result area is always visible as the destination for incoming games. Before submission it presents an honest empty state, never fabricated model output. During analysis it preserves:

- source-ordered, game-by-game insertion;
- total, queued, analyzing, completed, and failed counters;
- connection and batch state;
- status, agreement, and model-score filtering;
- incremental pagination;
- verdict-first rows (“Says cheating”, “No cheating signal”, or “Waiting”);
- native clickable Details disclosures containing both percentages and their threshold meaning;
- the disclaimer that scores are uncalibrated signals, not proof of misconduct.

## Information experience

The Information page uses a two-column composition. The left column contains the product overview and four substantive cards: site purpose, live-update behavior, interpretation warning, and project/contact links. The right column is a sticky visual narrative constructed from compact interface previews and a dotted progress spine.

As the user scrolls:

- left cards rise and fade into place with a short stagger;
- right preview cards slide from the right, fade in, and settle with small alternating rotations;
- progress markers activate in sequence;
- chess-piece cutouts move at a subtler depth than the content.

Motion is tied to viewport progress through `requestAnimationFrame`, not a timer. `prefers-reduced-motion: reduce` disables parallax, scroll transforms, pulsing, and transition delays while leaving all content visible.

## Visual constraints

- Use the existing high-resolution RGBA PNG chess assets with their transparent isolation intact.
- Do not add generated photography, stock art, gradients, glassmorphism/backdrop blur, oversized decorative slogans, or fake analytics.
- Depth comes from solid-color layers, borders, restrained shadows, transparency, and the isolated PNG pieces.
- Use a system sans-serif stack; no external font or JavaScript dependency is required.
- Maintain usable contrast, keyboard operation, semantic tabs/disclosures, live-region messaging, and responsive layouts down to narrow phones.

## Architecture impact

There is no core inference architecture change. Flask remains a separate web adapter and still submits to `POST /api/analyses`; PostgreSQL remains the durable job/event store; the ingestor and PyTorch/XGBoost workers retain their existing contracts; the browser still consumes status, paginated games, and SSE events from the current endpoints. Changes are limited to page routing, templates, CSS, browser-side composition, and frontend tests.

## Verification

- Flask page tests verify the combined route model, required source controls, information-page animation hooks, and preserved live ledger copy.
- Browser fixture tests verify verdict-first rows, interactive Details, score explanations, and live state behavior.
- Static checks retain the existing bans on gradient/backdrop-filter shortcuts and confirm transparent PNG assets.
- The focused frontend test suite and full non-integration Python suite must pass.
