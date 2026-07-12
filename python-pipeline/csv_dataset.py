import csv
from pathlib import Path


def append_labeled_record(csv_path: Path, payload: dict, game_id: str) -> None:
    record = dict(payload)
    record["GameID"] = game_id
    record["Cheating"] = int("cheating" in game_id.lower())

    write_header = not csv_path.is_file()
    with csv_path.open("a", newline="", encoding="utf-8") as csv_file:
        writer = csv.DictWriter(csv_file, fieldnames=record.keys())
        if write_header:
            writer.writeheader()
        writer.writerow(record)
