#!/usr/bin/env python3
"""run-ledger — a tamper-evident record of every automated run.

Maintains an append-only, hash-chained JSONL ledger. Each entry binds its own
content to the digest of the entry before it, so any retroactive edit, deletion,
or reordering breaks the chain and is reported. Also rolls up reliability by
automation and flags failure streaks.

Offline. Append and verify only; entries are never rewritten.
"""

from __future__ import annotations

import argparse
import datetime as _dt
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lib"))

from scoutkit import Finding, Report, Severity, append_jsonl, read_json, read_jsonl  # noqa: E402
from scoutkit.cli import run, utc_now  # noqa: E402
from scoutkit.hashing import GENESIS, chain_digest  # noqa: E402
from scoutkit.io import EvidenceError  # noqa: E402

SKILL = "run-ledger"
TITLE = "Run Ledger — automation run integrity and reliability"

REQUIRED_FIELDS = ("run_id", "automation", "started_at", "status")
TERMINAL_STATUSES = frozenset({"success", "failure", "cancelled", "skipped", "partial"})
FAILURE_STATUSES = frozenset({"failure", "cancelled", "partial"})

# Fields covered by the chain digest. Anything outside is annotation, not evidence.
SEALED_FIELDS = ("run_id", "automation", "started_at", "ended_at", "status",
                 "artifacts", "exit_code", "trigger", "notes")


def seal(entry: dict[str, Any]) -> dict[str, Any]:
    """Project an entry down to its sealed fields, for reproducible hashing."""
    return {k: entry.get(k) for k in SEALED_FIELDS if k in entry}


def _parse_ts(value: Any) -> _dt.datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        return _dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def append_entry(ledger_path: Path, record: dict[str, Any]) -> dict[str, Any]:
    """Append one run record, chaining it to the current tail."""
    missing = [f for f in REQUIRED_FIELDS if not record.get(f)]
    if missing:
        raise EvidenceError(f"run record is missing required field(s): {', '.join(missing)}")

    existing = read_jsonl(ledger_path)
    previous = existing[-1]["digest"] if existing else GENESIS
    sequence = len(existing) + 1

    entry = dict(record)
    entry["sequence"] = sequence
    entry["previous"] = previous
    entry["recorded_at"] = utc_now()
    entry["digest"] = chain_digest(previous, seal(entry))
    append_jsonl(ledger_path, entry)
    return entry


def verify(entries: list[dict[str, Any]], report: Report) -> dict[str, Any]:
    """Walk the chain, appending a finding for every integrity defect."""
    previous = GENESIS
    seen_ids: dict[str, int] = {}
    last_start: _dt.datetime | None = None
    intact = True

    def add(code: str, severity: str, title: str, detail: str, rec: str, locator: str) -> None:
        report.add(Finding(code=code, severity=severity, title=title, detail=detail,
                           locator=locator, recommendation=rec))

    for index, entry in enumerate(entries, start=1):
        locator = f"seq {entry.get('sequence', index)} / {entry.get('run_id', '<no id>')}"

        expected = chain_digest(previous, seal(entry))
        if entry.get("digest") != expected:
            intact = False
            add("RL001", Severity.CRITICAL, "Ledger chain broken",
                "The recorded digest does not match the entry content chained to its predecessor. "
                "This entry or an earlier one was altered or removed after the fact.",
                "Treat every entry from here forward as unverified. Preserve the file and investigate.", locator)
        previous = entry.get("digest") or expected

        if entry.get("sequence") != index:
            add("RL006", Severity.HIGH, "Sequence number out of order",
                f"Entry is in position {index} but claims sequence {entry.get('sequence')}.",
                "Entries must be appended in order. Rebuild the ledger from source records.", locator)

        run_id = str(entry.get("run_id") or "")
        if run_id in seen_ids:
            add("RL003", Severity.HIGH, "Duplicate run id",
                f"run_id '{run_id}' already appears at sequence {seen_ids[run_id]}.",
                "Run ids must be unique. A duplicate usually means a retry was logged as a new run.", locator)
        elif run_id:
            seen_ids[run_id] = index

        missing = [f for f in REQUIRED_FIELDS if not entry.get(f)]
        if missing:
            add("RL004", Severity.MEDIUM, "Incomplete run record",
                f"Missing required field(s): {', '.join(missing)}.",
                "Populate every required field at write time; they cannot be reconstructed later.", locator)

        status = str(entry.get("status") or "")
        if status and status not in TERMINAL_STATUSES:
            add("RL004", Severity.MEDIUM, "Unrecognized status",
                f"Status '{status}' is not one of {sorted(TERMINAL_STATUSES)}.",
                "Normalize status values so reliability rollups stay comparable.", locator)

        started = _parse_ts(entry.get("started_at"))
        if started is None:
            add("RL004", Severity.MEDIUM, "Unparseable start timestamp",
                f"started_at '{entry.get('started_at')}' is not ISO-8601.",
                "Write timestamps as ISO-8601 UTC.", locator)
        else:
            if last_start and started < last_start:
                add("RL002", Severity.HIGH, "Timestamp regression",
                    f"This run starts at {started.isoformat()}, before the previous entry at {last_start.isoformat()}.",
                    "An append-only ledger must be chronological. Check for clock skew or a back-dated insert.", locator)
            last_start = max(started, last_start) if last_start else started

        ended = _parse_ts(entry.get("ended_at"))
        if started and ended and ended < started:
            add("RL002", Severity.HIGH, "Negative duration",
                f"ended_at {ended.isoformat()} precedes started_at {started.isoformat()}.",
                "Correct the source record; durations feed cost and reliability reporting.", locator)

        if status == "success" and not entry.get("artifacts"):
            add("RL005", Severity.INFO, "Successful run produced no artifacts",
                "The run reports success but recorded no output.",
                "Confirm the run did real work; silent no-ops are a common failure mode.", locator)

    return {"chain_intact": intact, "entries_verified": len(entries)}


def rollup(entries: list[dict[str, Any]], report: Report, *, streak_threshold: int) -> dict[str, Any]:
    """Per-automation reliability, plus failure-streak detection."""
    by_automation: dict[str, dict[str, Any]] = {}
    for entry in entries:
        name = str(entry.get("automation") or "<unknown>")
        bucket = by_automation.setdefault(name, {"runs": 0, "success": 0, "failure": 0, "statuses": []})
        bucket["runs"] += 1
        status = str(entry.get("status") or "unknown")
        bucket["statuses"].append(status)
        if status == "success":
            bucket["success"] += 1
        elif status in FAILURE_STATUSES:
            bucket["failure"] += 1

    summaries: list[dict[str, Any]] = []
    for name, bucket in sorted(by_automation.items()):
        runs = bucket["runs"]
        rate = round(bucket["success"] / runs, 4) if runs else 0.0

        streak = 0
        for status in reversed(bucket["statuses"]):
            if status in FAILURE_STATUSES:
                streak += 1
            else:
                break
        if streak >= streak_threshold:
            report.add(Finding(
                code="RL007", severity=Severity.HIGH, title="Consecutive failure streak",
                detail=f"'{name}' has failed {streak} run(s) in a row and is still enabled.",
                locator=name,
                recommendation="Disable the automation and run failure-postmortem on the most recent failure.",
            ))
        summaries.append({
            "automation": name, "runs": runs, "success": bucket["success"],
            "failure": bucket["failure"], "success_rate": rate, "current_failure_streak": streak,
        })
    return {"by_automation": summaries}


def analyze(args: argparse.Namespace) -> Report:
    ledger_path = Path(args.input)
    report = Report(skill=SKILL, subject=ledger_path.name)

    if args.append:
        record = read_json(args.append)
        if not isinstance(record, dict):
            raise EvidenceError("--append expects a JSON object describing one run")
        entry = append_entry(ledger_path, record)
        report.note(f"Appended run {entry['run_id']} at sequence {entry['sequence']}.")

    entries = read_jsonl(ledger_path)
    if not entries:
        report.note(f"Ledger {ledger_path} is empty or absent; nothing to verify.")
        report.summary = {"entries": 0, "chain_intact": True}
        report.decide_verdict()
        return report

    integrity = verify(entries, report)
    reliability = rollup(entries, report, streak_threshold=args.streak_threshold)

    statuses: dict[str, int] = {}
    for entry in entries:
        key = str(entry.get("status") or "unknown")
        statuses[key] = statuses.get(key, 0) + 1
    successes = statuses.get("success", 0)

    report.sections = {**integrity, **reliability, "status_counts": statuses,
                       "head_digest": entries[-1].get("digest")}
    report.summary = {
        "entries": len(entries),
        "chain_intact": integrity["chain_intact"],
        "automations_tracked": len(reliability["by_automation"]),
        "overall_success_rate": round(successes / len(entries), 4),
        "first_run": entries[0].get("started_at"),
        "last_run": entries[-1].get("started_at"),
    }
    report.note("Integrity is relative to this file. Keep a copy off the machine that writes it.")
    report.decide_verdict()
    return report


def _extend(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--append", metavar="RECORD.json",
                        help="Append this run record to the ledger before verifying.")
    parser.add_argument("--streak-threshold", type=int, default=3,
                        help="Consecutive failures before raising a streak finding (default: 3).")


def main(argv: list[str] | None = None) -> int:
    return run(argv, skill=SKILL, title=TITLE, analyze=analyze, extend=_extend,
               description="Append to and verify a tamper-evident ledger of automation runs.")


if __name__ == "__main__":
    raise SystemExit(main())
