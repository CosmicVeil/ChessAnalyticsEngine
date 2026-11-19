"""Fill the dataset's Stockfish columns by analysing the source PGNs locally.

The dataset's game IDs encode where each game came from:
``"<White> vs. <Black> #N Clean"`` is game N of ``data/sample.pgn`` and
``"... #N Cheating"`` is game N of ``data/AI_sample.pgn``, the order in which
``python-pipeline/simulator.py`` streamed them.

Every position is analysed once with a fixed node budget. Results go to a
JSON-lines cache, one line per game, so an interrupted run resumes where it
stopped. The final step backs up the original CSV and writes the filled one.

Usage (from ml-model/):
    ../.venv/bin/python add_engine_features.py            # analyse + merge
    ../.venv/bin/python add_engine_features.py --workers 6
"""

import argparse
import json
import multiprocessing
import os
import re
import shutil
import sys
import time
from pathlib import Path

import chess
import chess.engine
import chess.pgn
import pandas as pd

from engine_features import (
    ANALYSIS_NODES,
    checkmate_evaluation,
    clamp_centipawns,
    move_features,
)


ROOT = Path(__file__).resolve().parents[1]
DATASET = Path(__file__).with_name("chess_dataset.csv")
BACKUP = Path(__file__).with_name("chess_dataset_without_engine.csv")
CACHE = Path(__file__).with_name("engine_features_cache.jsonl")
SOURCES = {"Clean": ROOT / "data" / "sample.pgn", "Cheating": ROOT / "data" / "AI_sample.pgn"}
ENGINE_COLUMNS = ["centipawn_loss", "evaluation", "win_chance", "mate"]
GAME_ID_PATTERN = re.compile(r"#(\d+) (Clean|Cheating)$")

_engine = None


def _start_engine(stockfish_path: str) -> None:
    global _engine
    _engine = chess.engine.SimpleEngine.popen_uci(stockfish_path)
    _engine.configure({"Threads": 1, "Hash": 16})


def _evaluate(board: chess.Board) -> tuple[int, int | None]:
    if board.is_checkmate():
        return checkmate_evaluation(board.turn == chess.WHITE), 0
    score = _engine.analyse(board, chess.engine.Limit(nodes=ANALYSIS_NODES))["score"].white()
    mate = score.mate()
    return clamp_centipawns(score.score(), mate), mate


def analyse_game(job: tuple[str, list[str]]) -> tuple[str, list[dict]]:
    """Return the engine columns for every move of one game (UCI move list)."""
    game_id, moves = job
    board = chess.Board()
    before, _ = _evaluate(board)
    rows = []
    for uci in moves:
        white_moved = board.turn == chess.WHITE
        board.push_uci(uci)
        after, mate = _evaluate(board)
        rows.append(move_features(before, after, mate, white_moved))
        before = after
    return game_id, rows


def wanted_games(dataframe: pd.DataFrame) -> dict[str, dict[int, tuple[str, int]]]:
    """Map each source to {game number: (dataset game ID, dataset move count)}."""
    counts = dataframe.groupby("GameID", sort=False)["move_number"].count()
    wanted = {kind: {} for kind in SOURCES}
    for game_id, count in counts.items():
        match = GAME_ID_PATTERN.search(game_id)
        if match:
            wanted[match.group(2)][int(match.group(1))] = (game_id, int(count))
    return wanted


def read_jobs(wanted, done: set[str]):
    """Stream the PGNs and yield (game_id, uci moves) for games still to analyse."""
    for kind, path in SOURCES.items():
        targets = wanted[kind]
        if not targets:
            continue
        last = max(targets)
        with path.open(encoding="utf-8", errors="replace") as handle:
            for number in range(1, last + 1):
                game = chess.pgn.read_game(handle)
                if game is None:
                    break
                if number not in targets:
                    continue
                expected_id, expected_moves = targets[number]
                if expected_id in done:
                    continue
                moves = [move.uci() for move in game.mainline_moves()]
                actual_id = (
                    f'{game.headers.get("White")} vs. {game.headers.get("Black")} '
                    f"#{number} {kind}"
                )
                # Skip anything that does not line up exactly with the dataset.
                if actual_id != expected_id or len(moves) != expected_moves:
                    print(f"skipping mismatched game {expected_id}", file=sys.stderr)
                    continue
                yield expected_id, moves


def load_cache() -> dict[str, list[dict]]:
    results = {}
    if CACHE.exists():
        with CACHE.open(encoding="utf-8") as handle:
            for line in handle:
                if line.strip():
                    record = json.loads(line)
                    results[record["game_id"]] = record["moves"]
    return results


def analyse(dataframe: pd.DataFrame, workers: int, stockfish_path: str) -> None:
    wanted = wanted_games(dataframe)
    done = set(load_cache())
    total = sum(len(games) for games in wanted.values())
    print(f"{len(done)} of {total} games already analysed", flush=True)
    started, finished = time.time(), 0
    with multiprocessing.get_context("spawn").Pool(
        workers, initializer=_start_engine, initargs=(stockfish_path,)
    ) as pool, CACHE.open("a", encoding="utf-8") as cache:
        for game_id, rows in pool.imap_unordered(
            analyse_game, read_jobs(wanted, done), chunksize=4
        ):
            cache.write(json.dumps({"game_id": game_id, "moves": rows}) + "\n")
            finished += 1
            if finished % 500 == 0:
                cache.flush()
                rate = finished / (time.time() - started)
                remaining = (total - len(done) - finished) / rate / 60
                print(f"{len(done) + finished}/{total} games, ~{remaining:.0f} min left", flush=True)


def merge(dataframe: pd.DataFrame) -> None:
    results = load_cache()
    missing = set(dataframe["GameID"]) - set(results)
    if missing:
        print(f"dropping {len(missing)} games without engine analysis", flush=True)
        dataframe = dataframe[~dataframe["GameID"].isin(missing)].copy()
    dataframe = dataframe.sort_values(["GameID", "move_number"], kind="stable")
    values = {column: [] for column in ENGINE_COLUMNS}
    for game_id, group in dataframe.groupby("GameID", sort=False):
        rows = results[game_id]
        for move_number in group["move_number"]:
            row = rows[int(move_number) - 1]
            for column in ENGINE_COLUMNS:
                values[column].append(row[column])
    for column in ENGINE_COLUMNS:
        dataframe[column] = values[column]
    if not BACKUP.exists():
        shutil.copy2(DATASET, BACKUP)
        print(f"backed up the original dataset to {BACKUP.name}")
    dataframe.sort_index().to_csv(DATASET, index=False)
    print(f"wrote {len(dataframe)} rows with engine features to {DATASET.name}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workers", type=int, default=max(1, (os.cpu_count() or 2) - 1))
    parser.add_argument("--stockfish", default=shutil.which("stockfish") or "stockfish")
    parser.add_argument("--merge-only", action="store_true")
    args = parser.parse_args()
    # The backup holds the untouched rows once a merge has happened.
    source = BACKUP if BACKUP.exists() else DATASET
    dataframe = pd.read_csv(source)
    if not args.merge_only:
        analyse(dataframe, args.workers, args.stockfish)
    merge(dataframe)


if __name__ == "__main__":
    main()
