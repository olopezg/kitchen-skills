#!/usr/bin/env python3
"""What the daily audit reads and writes outside its sandbox: the guard's latest run, the baseline from the kitchen
journal, and the journal entry for the pass (bin/daily-audit).

  audit_state.py guard <nightly-record>
      the latest nightly run, one field per line: status, sha, run_id, failed_step; exit 3 when there is none
  audit_state.py baseline <project>
      the next churn pass's baseline, one per line: its sha and its journal line. It is the newest `audit-verification`
      entry of the last 30 days whose outcome moves it (clean, clean (churn), changed, skipped, or exactly
      `audit-verification findings churn at <sha> (issue #N)`: a sealed pass whose findings landed in the findings
      issue), as the audit-verification skill reads it from `kitchen status --json`; exit 3, saying why, when there
      is none
  audit_state.py log <project> <branch> <sha> <run-id> <status> <message>
      appends the pass's entry to the journal on the project's behalf

<project> is the folder name of exactly one checkout listed in projects.txt, as `kitchen status` matches it. The
entries belong to that checkout's repository, as `kitchen log` run there would write them; the checkout is only read.
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "lib"))
from kitchen import journal  # noqa: E402
from kitchen.common import git_common_dir, git_out, now, parse_since, projects  # noqa: E402

ENTRY = re.compile(r"^audit-verification (clean \(churn\)|clean|changed|findings|blocked|skipped) (full|churn) at ([0-9a-f]{40})\b")
MOVES_BASELINE = ("clean", "clean (churn)", "changed", "skipped")
FILED_FINDINGS = re.compile(r"audit-verification findings churn at ([0-9a-f]{40}) \(issue #[1-9][0-9]*\)")
WINDOW = "30d"


def fail(code: int, reason: str) -> int:
    print(reason, file=sys.stderr)
    return code


def checkout(name: str) -> tuple[str, str] | str:
    """(toplevel, common dir) of the one listed checkout named <name>, or why there is none."""
    matches = [path for path in projects() if path.name == name]
    if len(matches) != 1:
        return f"{len(matches)} checkouts named {name} in projects.txt; the audit needs exactly one"
    toplevel, common = git_out(matches[0], "rev-parse", "--show-toplevel"), git_common_dir(matches[0])
    if not toplevel or not common:
        return f"{matches[0]} is not a git checkout"
    return toplevel, common


def main() -> int:
    action, *args = sys.argv[1:]
    if action == "guard":
        path = Path(args[0])
        runs = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()] if path.is_file() else []
        runs = [run for run in runs if isinstance(run, dict)]
        if not runs:
            return fail(3, f"no nightly guard run recorded in {path}")
        last = runs[-1]
        print("\n".join(str(last.get(key) or "") for key in ("status", "sha", "run_id", "failed_step")))
        return 0
    found = checkout(args[0])
    if isinstance(found, str):
        return fail(2, found)
    toplevel, common = found
    if action == "baseline":
        try:
            entries = journal.read(since=parse_since(WINDOW), project=common, repo=toplevel)
        except RuntimeError as error:
            return fail(2, str(error))
        for entry in reversed(entries):
            message = str(entry.get("message", ""))
            match = ENTRY.match(message)
            if match and match.group(1) in MOVES_BASELINE:
                print(f"{match.group(3)}\n{message}")
                return 0
            filed = FILED_FINDINGS.fullmatch(message)
            if filed:
                print(f"{filed.group(1)}\n{message}")
                return 0
        return fail(3, f"no audit-verification entry that moves the baseline in the last {WINDOW} of the journal")
    if action == "log":
        branch, sha, run_id, status, message = args[1:]
        journal.append({"ts": now().isoformat(timespec="seconds"), "repo": toplevel, "project": common, "branch": branch,
                        "sha": sha[:8], "agent": "kitchen-audit", "session": run_id, "status": status, "message": message})
        return 0
    return fail(2, f"unknown action {action}")


if __name__ == "__main__":
    sys.exit(main())
