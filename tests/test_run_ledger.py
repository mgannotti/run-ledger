"""Tests for run-ledger."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import run_ledger as rl
from scoutkit import read_jsonl
from scoutkit.hashing import GENESIS
from scoutkit.io import EvidenceError


def record(run_id: str, *, status="success", started="2026-07-27T12:00:00Z",
           automation="Alpha", artifacts=("a.json",)) -> dict:
    return {"run_id": run_id, "automation": automation, "started_at": started,
            "ended_at": started, "status": status, "artifacts": list(artifacts)}


def build(tmp_path: Path, records: list[dict]) -> Path:
    ledger = tmp_path / "runs.jsonl"
    for item in records:
        rl.append_entry(ledger, item)
    return ledger


def verify(ledger: Path, **kw):
    args = rl.argparse.Namespace(input=str(ledger), append=None, streak_threshold=kw.get("streak", 3))
    return rl.analyze(args)


def codes(report) -> set[str]:
    return {f.code for f in report.findings}


class TestAppend:
    def test_first_entry_chains_from_genesis(self, tmp_path):
        entry = rl.append_entry(tmp_path / "l.jsonl", record("r1"))
        assert entry["previous"] == GENESIS
        assert entry["sequence"] == 1
        assert len(entry["digest"]) == 64

    def test_sequence_increments(self, tmp_path):
        ledger = build(tmp_path, [record("r1"), record("r2"), record("r3")])
        assert [e["sequence"] for e in read_jsonl(ledger)] == [1, 2, 3]

    def test_each_entry_chains_to_its_predecessor(self, tmp_path):
        entries = read_jsonl(build(tmp_path, [record("r1"), record("r2")]))
        assert entries[1]["previous"] == entries[0]["digest"]

    def test_missing_required_field_is_rejected(self, tmp_path):
        with pytest.raises(EvidenceError):
            rl.append_entry(tmp_path / "l.jsonl", {"automation": "A", "status": "success"})

    def test_appending_never_rewrites_earlier_lines(self, tmp_path):
        ledger = build(tmp_path, [record("r1")])
        first = ledger.read_text(encoding="utf-8").splitlines()[0]
        rl.append_entry(ledger, record("r2"))
        assert ledger.read_text(encoding="utf-8").splitlines()[0] == first


class TestVerification:
    def test_untouched_ledger_is_intact(self, tmp_path):
        report = verify(build(tmp_path, [record("r1"), record("r2")]))
        assert report.summary["chain_intact"] is True
        assert "RL001" not in codes(report)

    def test_edited_entry_breaks_the_chain(self, tmp_path):
        ledger = build(tmp_path, [record("r1"), record("r2", status="failure"), record("r3")])
        lines = ledger.read_text(encoding="utf-8").splitlines()
        payload = json.loads(lines[1])
        payload["status"] = "success"
        lines[1] = json.dumps(payload, sort_keys=True)
        ledger.write_text("\n".join(lines) + "\n", encoding="utf-8")

        report = verify(ledger)
        assert report.summary["chain_intact"] is False
        assert report.verdict == "block"
        broken = [f for f in report.findings if f.code == "RL001"]
        assert broken and "r2" in broken[0].locator

    def test_deleted_entry_breaks_the_chain(self, tmp_path):
        ledger = build(tmp_path, [record("r1"), record("r2"), record("r3")])
        lines = ledger.read_text(encoding="utf-8").splitlines()
        ledger.write_text("\n".join([lines[0], lines[2]]) + "\n", encoding="utf-8")
        assert verify(ledger).summary["chain_intact"] is False

    def test_duplicate_run_id_is_flagged(self, tmp_path):
        assert "RL003" in codes(verify(build(tmp_path, [record("r1"), record("r1")])))

    def test_timestamp_regression_is_flagged(self, tmp_path):
        ledger = build(tmp_path, [
            record("r1", started="2026-07-27T12:00:00Z"),
            record("r2", started="2026-07-27T09:00:00Z"),
        ])
        assert "RL002" in codes(verify(ledger))

    def test_unrecognized_status_is_flagged(self, tmp_path):
        assert "RL004" in codes(verify(build(tmp_path, [record("r1", status="weird")])))

    def test_success_without_artifacts_is_informational(self, tmp_path):
        report = verify(build(tmp_path, [record("r1", artifacts=())]))
        assert "RL005" in codes(report)
        assert report.verdict == "pass"

    def test_empty_ledger_is_handled(self, tmp_path):
        report = verify(tmp_path / "absent.jsonl")
        assert report.summary["entries"] == 0
        assert report.verdict == "pass"


class TestReliability:
    def test_failure_streak_is_detected(self, tmp_path):
        ledger = build(tmp_path, [
            record("r1", status="success", started="2026-07-27T10:00:00Z"),
            record("r2", status="failure", started="2026-07-27T11:00:00Z"),
            record("r3", status="failure", started="2026-07-27T12:00:00Z"),
            record("r4", status="failure", started="2026-07-27T13:00:00Z"),
        ])
        report = verify(ledger)
        assert "RL007" in codes(report)
        streak = next(s for s in report.sections["by_automation"] if s["automation"] == "Alpha")
        assert streak["current_failure_streak"] == 3

    def test_recovery_resets_the_streak(self, tmp_path):
        ledger = build(tmp_path, [
            record("r1", status="failure", started="2026-07-27T10:00:00Z"),
            record("r2", status="failure", started="2026-07-27T11:00:00Z"),
            record("r3", status="failure", started="2026-07-27T12:00:00Z"),
            record("r4", status="success", started="2026-07-27T13:00:00Z"),
        ])
        report = verify(ledger)
        assert "RL007" not in codes(report)

    def test_success_rate_is_computed_per_automation(self, tmp_path):
        ledger = build(tmp_path, [
            record("r1", automation="Alpha", status="success", started="2026-07-27T10:00:00Z"),
            record("r2", automation="Alpha", status="failure", started="2026-07-27T11:00:00Z"),
            record("r3", automation="Beta", status="success", started="2026-07-27T12:00:00Z"),
        ])
        report = verify(ledger)
        rates = {s["automation"]: s["success_rate"] for s in report.sections["by_automation"]}
        assert rates == {"Alpha": 0.5, "Beta": 1.0}
        assert report.summary["automations_tracked"] == 2

    def test_streak_threshold_is_configurable(self, tmp_path):
        ledger = build(tmp_path, [
            record("r1", status="failure", started="2026-07-27T10:00:00Z"),
            record("r2", status="failure", started="2026-07-27T11:00:00Z"),
        ])
        assert "RL007" not in codes(verify(ledger, streak=3))
        assert "RL007" in codes(verify(ledger, streak=2))


class TestCli:
    def test_append_then_verify_via_cli(self, template, tmp_path):
        ledger = tmp_path / "runs.jsonl"
        code = rl.main(["--input", str(ledger),
                        "--append", str(template("run-ledger", "run-record.example.json")),
                        "--outdir", str(tmp_path / "o"), "--quiet"])
        assert code == 0
        assert len(read_jsonl(ledger)) == 1
        payload = json.loads((tmp_path / "o" / "run-ledger.json").read_text(encoding="utf-8"))
        assert payload["summary"]["chain_intact"] is True

    def test_append_of_bad_record_returns_three(self, write, tmp_path):
        bad = write("bad.json", json.dumps({"automation": "A"}))
        assert rl.main(["--input", str(tmp_path / "l.jsonl"), "--append", str(bad),
                        "--outdir", str(tmp_path / "o"), "--quiet"]) == 3
