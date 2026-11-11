#!/usr/bin/env python3
"""Record completed editorial outcomes so hourly collection does not regenerate duplicates."""
from __future__ import annotations
import argparse
import json
from datetime import datetime, timezone
from pathlib import Path

HISTORY = Path("seen_stories.json")

TERMINAL = {
    "draft_created_unpublished": "draft_created_unpublished",
    "rejected_quality_gate": "rejected_quality_gate",
}

def update(report_path: Path):
    if not HISTORY.exists():
        raise RuntimeError("seen_stories.json is missing; refusing to create incomplete history")
    history = json.loads(HISTORY.read_text(encoding="utf-8"))
    report = json.loads(report_path.read_text(encoding="utf-8"))
    events = history.setdefault("events", {})
    stamp = datetime.now(timezone.utc).isoformat()
    changed = 0
    for result in report.get("results", []):
        event_id = str(result.get("event_id") or "").strip()
        status = result.get("processing_status")
        if not event_id or status not in TERMINAL:
            continue
        record = events.get(event_id)
        if not isinstance(record, dict):
            # A candidate absent from history is an integrity error; do not fabricate a history record.
            raise RuntimeError(f"Event {event_id} is missing from seen_stories.json")
        record["status"] = TERMINAL[status]
        record["last_processed_at"] = stamp
        record["last_editorial_outcome"] = status
        if result.get("headline"):
            record["last_processed_headline"] = str(result["headline"])[:240]
        changed += 1
    history["updated_at"] = stamp
    HISTORY.write_text(json.dumps(history, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"Recorded {changed} terminal editorial outcomes; transient AI/evidence failures remain eligible for retry.")

def self_test():
    import tempfile
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        global HISTORY
        old = HISTORY
        HISTORY = root / "seen_stories.json"
        HISTORY.write_text(json.dumps({"events": {"a": {"event_id":"a","status":"observed_not_processed"},
                                                    "b": {"event_id":"b","status":"observed_not_processed"}}}))
        report = root / "report.json"
        report.write_text(json.dumps({"results":[
            {"event_id":"a","processing_status":"draft_created_unpublished","headline":"A"},
            {"event_id":"b","processing_status":"draft_generation_failed","headline":"B"}]}))
        update(report)
        saved = json.loads(HISTORY.read_text())
        assert saved["events"]["a"]["status"] == "draft_created_unpublished"
        assert saved["events"]["b"]["status"] == "observed_not_processed"
        HISTORY = old
    print("PASS: terminal outcomes persisted; transient failures remain retryable")

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--report", default="output/editorial_report.json")
    parser.add_argument("--self-test", action="store_true")
    args = parser.parse_args()
    if args.self_test:
        self_test()
    else:
        update(Path(args.report))
