# Analysis Cleanup Utility Design

## Purpose

Add a local-development utility at `docker_help/clear_analysis_state.sh`. It clears abandoned live-analysis work so the user can restart the application and upload a fresh PGN. It preserves terminal job history and does not change model inference or Kafka topic contracts.

## Safety contract

The utility exits before changing data when the ingestion worker or Go feature extractor is running. It also verifies that the `chess-state-managers` Kafka consumer group has no active member. Before it prompts for `CLEAR`, the utility previews each unfinished job and its spool path, displays the Kafka group and lag, and freezes the exact identities of matching spool files. A `--yes` flag supports deliberate non-interactive use.

The utility treats `queued`, `parsing`, and `live` jobs as unfinished. It preserves `completed`, `partial`, and `failed` jobs.

## Cleanup flow

1. Resolve the repository root and require Docker Compose, PostgreSQL, and Redpanda.
2. Refuse to run while ingestion or Go extraction is active.
3. Query and display unfinished jobs, game counts, spool paths, and the Kafka group and lag; freeze the confirmed spool identities without moving them.
4. Require confirmation after the operator has seen that complete destructive impact.
5. Advance every `chess-moves` partition commit for `chess-state-managers` to the end, including previously uncommitted partitions, then describe the commits and require provably zero lag per partition.
6. In one PostgreSQL maintenance transaction, lock the confirmed job rows, recheck their status and optional stale filter, temporarily suppress foreign-key triggers, and manually delete unmatched web results, job events, model results, games, and the still-eligible unfinished jobs in child-first order. This avoids the current slow cascade without changing the schema.
7. Validate that the transaction created no orphaned rows and report targets skipped because they raced to a terminal or otherwise ineligible state.
8. Delete only frozen regular spool identities associated with job IDs actually returned by the committed job deletion.
9. Print remaining `completed`, `partial`, and `failed` counts plus their total and instructions for restarting the application.

If any database command fails, PostgreSQL rolls back the transaction and the utility keeps every spool file. If Kafka seeking fails, the utility does not touch PostgreSQL.

## Interface

```text
docker_help/clear_analysis_state.sh [--yes] [--stale-minutes MINUTES]
```

Without `--stale-minutes`, the utility deletes every unfinished job after confirming that the app processes have stopped. Supplying `--stale-minutes` limits deletion to unfinished jobs whose lease has expired and whose last update is older than the selected threshold.

## Tests

Python `unittest` tests run the shell utility against fake `docker` and `pgrep` commands. Tests cover help output, active-worker refusal, confirmation refusal, Kafka failure ordering, SQL scope, and safe spool-path handling. A shell syntax check runs as part of the focused suite.

## Architecture impact

The change adds one operational script, its tests, and usage documentation. It does not change Flask, PostgreSQL migrations, Go feature extraction, model preprocessing, inference, or Kafka message schemas.
