---
name: handoff
description: "Write a handoff so another agent, tool or session (Claude, Codex, another machine) can continue this work without the conversation. Use when I ask for a handoff, to pass the work on, or for a prompt to paste into another session or machine."
disable-model-invocation: true
---

# handoff

Write it to `${TMPDIR:-/tmp}/handoff-<repo>-<yyyymmdd-hhmm>.md`, never inside the workspace, and print the path. A prompt for another machine, or for me to paste, also goes in full in the reply: the path does not exist there. If I passed arguments, they describe what the next session will do; tailor the handoff to that.

## Contents

1. **Goal**: what we are trying to achieve and how we will know it is done.
2. **State, measured now**: repo path, branch, HEAD SHA, `git status --short` summary, unpushed commits, open PRs (`gh pr list --author @me`), worktrees in use, anything still running.
3. **Done**: what is finished and how each piece was verified (command and result).
4. **Next**: ordered steps. The first one must be runnable without asking anyone anything.
5. **Decisions**: taken (point to `decisions.md` when it exists) and still owed by me.
6. **Traps**: what looks right but is not, and what not to touch.
7. **Verify**: the exact commands that prove the work.
8. **Suggested skills** for the next agent.

Reference specs, PRs, commits and logs by path or URL instead of copying them. Redact every secret.

## For the receiving agent

Before trusting the handoff, re-measure the state block: `git rev-parse HEAD`, `git status`, the PR list. Report any difference before continuing.

Principles (the kitchen's PRINCIPLES.md): `handoff`, `evidence`.
