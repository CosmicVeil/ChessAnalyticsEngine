import csv
from pathlib import Path


DATASET_FIELDNAMES = (
    "game_id",
    "move_number",
    "material_balance",
    "complexity_score",
    "time_delta",
    "rating_diff",
    "GameID",
    "Cheating",
)


def should_store_feature_event(payload: dict) -> bool:
    """Keep move features for training but exclude the game-complete control event."""
    return not payload.get("game_complete", False)


def reset_labeled_dataset(csv_path: Path) -> None:
    """Start each collector run with a fresh CSV and its current fixed schema."""
    csv_path.unlink(missing_ok=True)


def append_labeled_record(csv_path: Path, payload: dict, game_id: str) -> None:
    # Every row uses the same header, even when a new feature is absent in an older event.
    record = {column: payload.get(column, "") for column in DATASET_FIELDNAMES}
    record["GameID"] = game_id
    record["Cheating"] = int("cheating" in game_id.lower())

    write_header = not csv_path.is_file()
    with csv_path.open("a", newline="", encoding="utf-8") as csv_file:
        writer = csv.DictWriter(csv_file, fieldnames=DATASET_FIELDNAMES)
        if write_header:
            writer.writeheader()
        writer.writerow(record)
