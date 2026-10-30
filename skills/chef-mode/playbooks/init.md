# init: set one repo up for agents

The owner chooses the guardrails; you build and prove them on a branch, in one pull request the owner merges. Per repo, never automatic: only the repo you were pointed at. The guards you add run in this repo only.

Copy these steps into your todo list.

## 0. Where

You need the kitchen on this machine: `kitchen` on PATH and this playbook's starting points next to it. Without it, install it first (the README's one-line prompt), or stop BLOCKED and say so.

Work in a worktree on a new branch `kitchen/init` from the shared base: in T3 Code `t3_thread_launch` with a worktree `workspaceStrategy`, elsewhere `git worktree add -b kitchen/init <path> origin/<base>`. Never in the owner's checkout. Unknown base: ask; never guess.

## 1. Read what is there; write nothing

- The stacks and their manifests, and the commands the team really runs: CI workflows, package scripts, Makefile, README.
- Hooks: `git config core.hooksPath`, `.githooks/`, `.husky/`, pre-commit config; a secret scanner.
- Agent files: `AGENTS.md`, `CLAUDE.md`, `.claude/settings.json`, `.codex/`, `decisions.md`, `.agents/skills/verify-*`, `bin/check`.
- `.gitignore` rules that would hide a new file (a .NET `bin/` rule hides `bin/check`).
- The owner's kitchen config, if it lists this repo: `~/.config/kitchen/integrate.toml` (checks the owner already trusts) and `projects.txt`.
- Branch protection, read-only: `gh api repos/<owner>/<repo>/branches/<base>/protection`.

## 2. Ask once

One message of numbered questions. For each: what is there today, one sentence on what yes does, your recommendation. Leave out a piece that already exists and works; say so in one line instead.

1. **Guards.** `kitchen guards .` copies `deny-no-verify`, `deny-shared-push` and `deny-recursive-rm` into `.kitchen/hooks/` and runs them before every shell command, from `.claude/settings.json` and `.codex/hooks.json`. Recommend yes.
2. **Check contract.** `bin/check` with the tiers `commit` (seconds to a minute), `integrate` (everything a merge needs), `nightly` and `verify-tree`, and `--list`; from [check.sh](init/check.sh), filled with commands this repo really runs. Recommend yes.
3. **Pre-commit hook.** `.githooks/pre-commit` from [pre-commit.sh](init/pre-commit.sh): a secret scan on the staged changes, then `bin/check commit`. Recommend yes when the commit tier takes under a minute.
4. **Verify method.** `.agents/skills/verify-<repo>/` from [verify/](init/verify/), with a feature map and a guard test that fails when an entry point is missing from it. Recommend yes when the repo has a UI, an API or a CLI to drive.
5. **AGENTS.md.** A `## Verify` section naming `bin/check` and the verify skill, and the line `Autonomy: propose` or `Autonomy: merge` (the rungs are in chef-mode's Doors and the trust ladder), followed by the sentence "It applies in chef mode (`/chef-mode`); outside it, open the pull request and leave the merge to the owner." Recommend `propose` until the owner has seen the guardrails work.
6. **decisions.md.** From [decisions.md](init/decisions.md), with each one-way door below as an owed `- [ ]`. Recommend yes when it is missing.
7. **The owner's kitchen** (personal config, never committed). Append the repo path to `~/.config/kitchen/projects.txt` (`kitchen status`, the nightly guard), and a `[projects.<folder>]` table with `base` and `checks = ["bin/check integrate"]` to `~/.config/kitchen/integrate.toml` (`kitchen integrate`). Append only; a file you cannot parse is reported, not rewritten.
8. **Quality baseline** (optional). Today: whatever the repo measures (file sizes, layer violations, coverage, lint counts), or nothing. If yes: a script that prints those numbers as JSON and a committed baseline file; in the owner's `~/.config/kitchen/automation/<repo>.env`, `METRICS_CMD` records a snapshot after each nightly run, and `QUALITY_CMD` and `BASELINE_FILE` let the weekly gardener propose small changes that improve them, regenerating the baseline in the same change. A ratchet test that only lets a number shrink makes it a gate. Recommend yes only when the owner wants the numbers to move; a baseline nobody reads is noise.

One-way doors are printed, never run: a ruleset or branch protection that requires the check on the shared base, with the exact `gh api` command. They go in the pull request body as numbered decisions.

## 3. Build what was answered yes

- `bin/check`: every command is one you ran in this repo and saw pass; nothing guessed stays in. When `integrate.toml` already lists checks for the repo, start from those. Keep the commit tier fast; put the full build and the slow suites in `integrate`.
- Long runs print progress; never sit silent for minutes.
- A file `.gitignore` hides gets a `!` exception in `.gitignore`, not `git add -f`.
- Guards: `kitchen guards .`, then `kitchen guards . --check` must print OK.

## 4. Prove it

Prove each piece the owner said yes to; a piece they declined has nothing to prove.

1. Check contract: `bin/check commit` green twice in a row, from a clean tree.
2. Check contract, negative control: plant a defect in a file the commit tier actually runs (a test it executes, in a language its commands cover), run `bin/check commit`, see it fail, revert the defect. Still green means the tier does not cover that file: fix the tier or pick a file it runs. Never report done on a control that stayed green.
3. Check contract: `bin/check integrate` green once.
4. Pre-commit hook: `git config core.hooksPath .githooks`, stage the planted defect and run `git commit`: the hook must refuse it. Revert.
5. Guards: each one blocks its probe in this repo: pipe `{"tool_name":"Bash","cwd":"<repo>","tool_input":{"command":"git push origin HEAD:<base>"}}` into `.kitchen/hooks/deny-shared-push` and see exit 2 (likewise `git commit --no-verify -m x` for `deny-no-verify`, `rm -rf /x` for `deny-recursive-rm`).

Keep each command and its last lines of output for the pull request.

## 5. Hand it over

Commit on `kitchen/init`, push the branch, open the pull request to the base. Its body: what was added and why, the evidence from step 4 (both green runs, the red control and its revert, the guard probes), the one-way doors as numbered decisions with their commands, and two lines for the owner: Claude Code loads the guards in a session started at the repo root; in Codex, trust the project and open `/hooks` once to approve them. The owner merges this pull request, whatever the rung.

Report PASS, ISSUES or BLOCKED with that evidence, and the decisions the owner owes.
