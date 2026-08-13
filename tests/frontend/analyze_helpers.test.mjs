import assert from "node:assert/strict";
import test from "node:test";

globalThis.document = { querySelector: () => null };
globalThis.window = { addEventListener() {} };

const analyze = await import("../../webapp/static/js/analyze.js?node-test=1");

test("tab orientation and arrow mapping follow the rendered layout", () => {
  assert.equal(typeof analyze.tabOrientation, "function");
  assert.equal(typeof analyze.tabTargetIndex, "function");
  assert.equal(analyze.tabOrientation(false), "horizontal");
  assert.equal(analyze.tabOrientation(true), "vertical");
  assert.equal(analyze.tabTargetIndex(0, "ArrowRight", 3, "horizontal"), 1);
  assert.equal(analyze.tabTargetIndex(0, "ArrowDown", 3, "horizontal"), null);
  assert.equal(analyze.tabTargetIndex(0, "ArrowDown", 3, "vertical"), 1);
  assert.equal(analyze.tabTargetIndex(0, "ArrowUp", 3, "vertical"), 2);
  assert.equal(analyze.tabTargetIndex(1, "Home", 3, "vertical"), 0);
  assert.equal(analyze.tabTargetIndex(1, "End", 3, "horizontal"), 2);
});

test("dropped-file validation accepts PGN files and rejects unrelated text", () => {
  assert.equal(typeof analyze.isPgnFile, "function");
  assert.equal(analyze.isPgnFile({ name: "round.PGN", type: "text/plain" }), true);
  assert.equal(analyze.isPgnFile({ name: "round", type: "application/x-chess-pgn" }), true);
  assert.equal(analyze.isPgnFile({ name: "notes.txt", type: "text/plain" }), false);
});
