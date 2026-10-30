#!/usr/bin/env python3
"""The nightly, gardener and audit histories (`kitchen status` reads the first two): one JSON line per run, rewritten in
place by run_id.

  record.py <file> write <status> <failed_step> <cleanup> <metrics> <sha> <run_id> <log> <started> <steps> [warning...]
  record.py <file> gardener <status> <detail> <run_id> <log> <started> [warning...]
  record.py <file> audit <status> <detail> <sha> <baseline> <run_id> <log> <started> [warning...]
  record.py <file> close-stale <current-run-id>
  record.py <file> mark-survivors <run-id> <pids>

A run writes "running" when it starts and replaces that line when it ends. A "running" line left by a
run that was killed (SIGKILL cannot be trapped) is closed as "incomplete" by the next run that holds
the project lock, since no other run can still be alive then.

A nightly's <steps> are the steps that finished, in order, one line each: `<seconds> <green|red> <name>`; they are
recorded as `steps`, a list of {"name", "seconds", "status"}. Records written before `steps` existed have no such key.
"""
import datetime
import json
import os
import sys

KILLED = "killed before it could finish its record"


def load(path: str) -> list:
    if not os.path.exists(path):
        return []
    entries = []
    for line in open(path, encoding="utf-8").read().splitlines():
        if not line.strip():
            continue
        try:
            entries.append(json.loads(line))
        except json.JSONDecodeError:
            entries.append(line)  # never drop what we cannot read
    return entries


def save(path: str, entries: list) -> None:
    tmp = f"{path}.tmp.{os.getpid()}"
    with open(tmp, "w", encoding="utf-8") as handle:
        for entry in entries:
            handle.write((entry if isinstance(entry, str) else json.dumps(entry)) + "\n")
    os.replace(tmp, path)


def parse_steps(text: str) -> list:
    steps = []
    for line in text.splitlines():
        seconds, status, name = line.split(" ", 2)
        if not seconds.isdigit() or status not in ("green", "red") or not name:
            raise ValueError(f"not a step line: {line!r}")
        steps.append({"name": name, "seconds": int(seconds), "status": status})
    return steps


def main() -> int:
    path, action, *args = sys.argv[1:]
    entries = load(path)
    if action == "close-stale":
        (current,) = args
        for entry in entries:
            if isinstance(entry, dict) and entry.get("status") == "running" and entry.get("run_id") != current:
                entry.update(status="incomplete", failed_step=entry.get("failed_step") or "killed",
                             warnings=[*entry.get("warnings", []), KILLED])
        save(path, entries)
        return 0
    if action == "mark-survivors":
        run_id, pids = args
        for entry in entries:
            if isinstance(entry, dict) and entry.get("run_id") == run_id:
                entry.update(status="incomplete", failed_step=f"survivors {pids}",
                             warnings=[*entry.get("warnings", []),
                                       f"survivors {pids} still held files in the clone after TERM and KILL"])
        save(path, entries)
        return 0
    now = datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")
    if action == "gardener":
        status, detail, run_id, log, started, *warnings = args
        entry = {"ts": now, "started": started, "status": status, "detail": detail or None, "warnings": warnings,
                 "run_id": run_id, "log": log}
    elif action == "audit":
        status, detail, sha, baseline, run_id, log, started, *warnings = args
        entry = {"ts": now, "started": started, "status": status, "detail": detail or None, "sha": sha or None,
                 "baseline": baseline or None, "warnings": warnings, "run_id": run_id, "log": log}
    else:
        status, failed, cleanup, metrics, sha, run_id, log, started, steps, *warnings = args
        entry = {"ts": now, "started": started, "sha": sha or None, "status": status, "failed_step": failed or None,
                 "cleanup": cleanup, "metrics": metrics, "steps": parse_steps(steps), "warnings": warnings,
                 "run_id": run_id, "log": log}
    for index, existing in enumerate(entries):
        if isinstance(existing, dict) and existing.get("run_id") == run_id:
            entries[index] = entry
            break
    else:
        entries.append(entry)
    save(path, entries)
    return 0


if __name__ == "__main__":
    sys.exit(main())
