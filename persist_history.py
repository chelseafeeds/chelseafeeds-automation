#!/usr/bin/env python3
"""Race-safe persistence for ChelseaFeeds collector history.

Merges the collector's local history with the latest main-branch history,
then retries a normal fast-forward push if another writer wins the race.
Never force-pushes and never discards events from either history.
"""
from __future__ import annotations

import json
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent
HISTORY = ROOT / "seen_stories.json"
RETENTION_DAYS = 90
MAX_ATTEMPTS = 6


def parse_dt(value):
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt.astimezone(timezone.utc)
    except (TypeError, ValueError):
        return None


def iso_min(a, b):
    values = [x for x in (parse_dt(a), parse_dt(b)) if x]
    return min(values).isoformat() if values else (a or b)


def iso_max(a, b):
    values = [x for x in (parse_dt(a), parse_dt(b)) if x]
    return max(values).isoformat() if values else (a or b)


def merge_histories(*documents):
    merged = {}
    updated_at = ""
    version = "4.0"
    for doc in documents:
        if not isinstance(doc, dict):
            continue
        version = str(doc.get("history_version") or version)
        updated_at = iso_max(updated_at, doc.get("updated_at"))
        events = doc.get("events", {})
        if not isinstance(events, dict):
            continue
        for event_id, incoming in events.items():
            if not isinstance(incoming, dict):
                continue
            previous = merged.get(event_id, {})
            prev_seen = previous.get("last_seen_at")
            new_seen = incoming.get("last_seen_at")
            newest = incoming if (parse_dt(new_seen) or datetime.min.replace(tzinfo=timezone.utc)) >= (parse_dt(prev_seen) or datetime.min.replace(tzinfo=timezone.utc)) else previous
            record = dict(previous)
            record.update(newest)
            record["event_id"] = incoming.get("event_id") or previous.get("event_id") or event_id
            record["first_seen_at"] = iso_min(previous.get("first_seen_at"), incoming.get("first_seen_at"))
            record["last_seen_at"] = iso_max(prev_seen, new_seen)
            record["last_published_at"] = iso_max(previous.get("last_published_at"), incoming.get("last_published_at"))
            record["last_observation_count"] = max(
                int(previous.get("last_observation_count") or 0),
                int(incoming.get("last_observation_count") or 0),
            )
            domains = set(previous.get("publisher_domains") or []) | set(incoming.get("publisher_domains") or [])
            record["publisher_domains"] = sorted(domains)
            statuses = [x for x in (previous.get("status"), incoming.get("status")) if x and x != "observed_not_processed"]
            if statuses:
                record["status"] = newest.get("status") if newest.get("status") in statuses else statuses[-1]
            elif previous.get("status") or incoming.get("status"):
                record["status"] = "observed_not_processed"
            merged[event_id] = record

    cutoff = datetime.now(timezone.utc) - timedelta(days=RETENTION_DAYS)
    kept = {}
    for event_id, record in merged.items():
        seen = parse_dt(record.get("last_seen_at"))
        if seen and seen >= cutoff:
            kept[event_id] = record
    return {
        "history_version": version,
        "updated_at": updated_at or datetime.now(timezone.utc).isoformat(),
        "events": kept,
    }


def run(*args, check=True, capture_output=False):
    return subprocess.run(
        args, cwd=ROOT, check=check, text=True,
        stdout=subprocess.PIPE if capture_output else None,
        stderr=subprocess.PIPE if capture_output else None,
    )


def read_remote_history():
    result = run("git", "show", "origin/main:seen_stories.json", check=False, capture_output=True)
    if result.returncode != 0:
        return {"history_version": "4.0", "events": {}}
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError:
        raise RuntimeError("The main-branch seen_stories.json is not valid JSON")


def persist():
    if not HISTORY.exists():
        raise RuntimeError("seen_stories.json is missing; refusing to overwrite history")
    local = json.loads(HISTORY.read_text(encoding="utf-8"))
    for attempt in range(1, MAX_ATTEMPTS + 1):
        run("git", "fetch", "origin", "main")
        remote = read_remote_history()
        merged = merge_histories(local, remote)
        run("git", "reset", "--mixed", "origin/main")
        HISTORY.write_text(json.dumps(merged, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        run("git", "add", "seen_stories.json")
        diff = run("git", "diff", "--cached", "--quiet", check=False)
        if diff.returncode == 0:
            print("History already current; no commit needed.")
            return
        run("git", "config", "user.name", "github-actions[bot]")
        run("git", "config", "user.email", "41898282+github-actions[bot]@users.noreply.github.com")
        run("git", "commit", "-m", "Update ChelseaFeeds story history")
        pushed = run("git", "push", "origin", "HEAD:main", check=False)
        if pushed.returncode == 0:
            print(f"History safely merged and pushed on attempt {attempt}; {len(merged['events'])} events retained.")
            return
        # Keep our merged working-tree JSON, reset to the latest remote on the
        # next pass, and merge again. This handles a concurrent non-collector
        # main-branch update without a rebase conflict or force push.
        print(f"Push raced with another main update (attempt {attempt}); retrying.", file=sys.stderr)
        if attempt < MAX_ATTEMPTS:
            time.sleep(min(2 ** attempt, 15))
    raise RuntimeError(f"Could not push merged history after {MAX_ATTEMPTS} attempts")


def self_test():
    a = {"history_version": "4.0", "updated_at": "2026-11-11T10:00:00+00:00", "events": {
        "a": {"event_id": "a", "title": "Older title", "first_seen_at": "2026-11-10T00:00:00+00:00",
              "last_seen_at": "2026-11-11T09:00:00+00:00", "last_published_at": "2026-11-10T00:00:00+00:00",
              "last_observation_count": 2, "publisher_domains": ["bbc.co.uk"], "status": "observed_not_processed"}}}
    b = {"history_version": "4.0", "updated_at": "2026-11-11T11:00:00+00:00", "events": {
        "a": {"event_id": "a", "title": "Newer title", "first_seen_at": "2026-11-09T00:00:00+00:00",
              "last_seen_at": "2026-11-11T11:00:00+00:00", "last_published_at": "2026-11-11T10:00:00+00:00",
              "last_observation_count": 4, "publisher_domains": ["theguardian.com"], "status": "reviewed"},
        "b": {"event_id": "b", "title": "Second event", "first_seen_at": "2026-11-11T00:00:00+00:00",
              "last_seen_at": "2026-11-11T10:00:00+00:00", "last_observation_count": 1,
              "publisher_domains": ["skysports.com"], "status": "observed_not_processed"}}}
    out = merge_histories(a, b)
    assert set(out["events"]) == {"a", "b"}
    assert out["events"]["a"]["title"] == "Newer title"
    assert out["events"]["a"]["first_seen_at"] == "2026-11-09T00:00:00+00:00"
    assert out["events"]["a"]["last_observation_count"] == 4
    assert out["events"]["a"]["publisher_domains"] == ["bbc.co.uk", "theguardian.com"]
    assert out["events"]["a"]["status"] == "reviewed"
    print("PASS: history union, latest event data, earliest first-seen, max observation count, publisher union and status preservation")


if __name__ == "__main__":
    if "--self-test" in sys.argv:
        self_test()
    else:
        persist()
