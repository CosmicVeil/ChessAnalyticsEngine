import assert from "node:assert/strict";
import test from "node:test";

globalThis.document = { querySelector: () => null };

const analysis = await import("../../webapp/static/js/analysis.js?node-test=1");

test("score-range validation rejects inverted and non-numeric bounds", () => {
  assert.equal(typeof analysis.validateScoreRange, "function");
  assert.equal(
    analysis.validateScoreRange(new URLSearchParams({ score_min: "0.8", score_max: "0.2" })),
    "Minimum score cannot exceed maximum score.",
  );
  assert.equal(
    analysis.validateScoreRange(new URLSearchParams({ score_min: "not-a-number", score_max: "" })),
    "Minimum score must be a number between zero and one.",
  );
  assert.equal(
    analysis.validateScoreRange(new URLSearchParams({ score_min: "0.2", score_max: "0.8" })),
    null,
  );
});

test("reset reconciliation drops stale cards but preserves fetched and newer live cards", () => {
  const arbiter = analysis.createLoadArbiter();
  const reset = arbiter.beginLoad();
  arbiter.markLive("live-during-request");
  const fetched = new Set(["fresh", "existing"]);

  assert.equal(arbiter.canRemoveGame(reset, "stale", fetched), true);
  assert.equal(arbiter.canRemoveGame(reset, "existing", fetched), false);
  assert.equal(arbiter.canRemoveGame(reset, "live-during-request", fetched), false);

  const newerReset = arbiter.beginLoad();
  assert.equal(arbiter.canRemoveGame(reset, "stale", fetched), false);
  assert.equal(arbiter.canRemoveGame(newerReset, "stale", fetched), true);
});

test("event stream URL resumes strictly after the status cursor", () => {
  assert.equal(typeof analysis.eventStreamUrl, "function");
  assert.equal(
    analysis.eventStreamUrl("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa", 417),
    "/api/analyses/aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa/events?after=417",
  );
});

test("initial game loading never delays or suppresses stream startup", async () => {
  assert.equal(typeof analysis.startLiveSession, "function");
  const calls = [];
  let releaseGames;
  const games = new Promise((resolve) => { releaseGames = resolve; });

  await analysis.startLiveSession({
    terminal: false,
    loadGames: () => {
      calls.push("games");
      return games;
    },
    openStream: () => { calls.push("stream"); },
  });

  assert.deepEqual(calls, ["games", "stream"]);
  releaseGames(false);
});

test("terminal status starts the snapshot but does not open a stream", async () => {
  assert.equal(typeof analysis.startLiveSession, "function");
  const calls = [];

  await analysis.startLiveSession({
    terminal: true,
    loadGames: () => { calls.push("games"); },
    openStream: () => { calls.push("stream"); },
  });

  assert.deepEqual(calls, ["games"]);
});
