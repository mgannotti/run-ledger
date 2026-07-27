---
name: run-ledger
description: Maintain and verify a tamper-evident, hash-chained append-only ledger of every automation run, detecting retroactive edits, deletions, reordering, duplicate run ids, and timestamp regressions, and rolling up reliability and failure streaks per automation. Trigger when the user says "/run-ledger", "log this run", "did anything change my run history", "verify the audit log", "what is my automation success rate", or "which automations keep failing".
---

# Run Ledger

An append-only record where each entry's digest is bound to the one before it.
Editing or removing any earlier entry invalidates every later digest.

## When to use this

Append after every automation run. Verify whenever you need the history to be
trustworthy — audits, incident review, or before drawing conclusions from run counts.

## Inputs

The ledger path (a `.jsonl` file, created on first append) and optionally a run record
to append. A run record requires `run_id`, `automation`, `started_at`, and `status`;
`ended_at`, `artifacts`, `exit_code`, `trigger`, and `notes` are sealed if present.

Valid statuses: `success`, `failure`, `cancelled`, `skipped`, `partial`.

## How to run it

Append and verify in one call:

```
python scripts/run_ledger.py \
  --input out/runs.jsonl \
  --append <run-record.json> \
  --outdir out/run-ledger
```

Verify only — omit `--append`. Adjust the streak sensitivity with
`--streak-threshold N` (default 3).

## What verification catches

- `RL001` **chain broken** — an entry was altered or removed after the fact. Critical,
  and it names the exact sequence position where trust ends.
- `RL002` timestamp regression or negative duration.
- `RL003` duplicate run id, usually a retry logged as a new run.
- `RL004` incomplete record or unrecognized status.
- `RL005` a successful run that produced no artifacts — the silent no-op.
- `RL006` sequence numbers out of order.
- `RL007` consecutive failure streak on an automation that is still enabled.

## How to read the result

`summary.chain_intact` is the integrity answer. `sections.by_automation` gives runs,
successes, failures, success rate, and current failure streak per automation.
`sections.head_digest` is the value to record elsewhere if you want an external anchor.

## Limits — state these when you report

- Integrity is relative to this file. Someone who rewrites the whole ledger from the
  genesis digest forward produces a self-consistent chain. Keep a copy — or just the
  head digest — somewhere the writing process cannot reach.
- The ledger records what it was told. It cannot verify that a reported success was real.

## Guardrails

Append and read only. Existing entries are never rewritten. No network. No cloud writes.
