# Automation

Scheduled jobs that keep a project honest without anyone asking.

| Job | When (default) | What it does |
|---|---|---|
| `bin/nightly-guard <project>` | daily 02:00 | Runs the project's steps on a dedicated clone of the shared branch. One GitHub issue while it is red, closed when green; a local notification when red or incomplete. Every run that holds the project lock lands exactly once in `~/.local/state/kitchen/nightly/<project>.jsonl` for `kitchen status`. |
| `bin/weekly-gardener <project>` | Monday 06:00 | One unattended Claude Code run that commits at most one small, verified change on a local branch; the job then publishes it as a PR if it passes its own checks. Skips while the last one is still open. Every run that holds the project lock lands once in `~/.local/state/kitchen/gardener/<project>.jsonl`; a local notification when refused or incomplete. |
| `bin/daily-audit <project>` | daily 04:00 | A churn pass of the `audit-verification` skill on the SHA the nightly guard just proved green, by a sealed Claude Code run like the gardener's. The job hands it the baseline from the kitchen journal and the guard's record, step logs and evidence, then logs the outcome in the journal and publishes at most one PR of prose fixes. Findings go to one GitHub issue per project, commented on while it is open. Every run that holds the project lock lands once in `~/.local/state/kitchen/audit/<project>.jsonl`. |
| `bin/weekly-retro` | Monday 07:00 | Runs the `retro` skill over the week's transcripts and writes a report of proposals. Applies nothing. |

Schedule them with `bin/install-schedule <project>` and `bin/install-schedule --retro`: launchd on macOS, systemd user timers on Linux. `bin/install-schedule --gardener <project>` schedules the gardener alone, for a machine that runs only it. `bin/install-schedule --guard <project>` schedules the nightly guard alone, for a machine whose gardener runs elsewhere. `bin/install-schedule --audit <project> [hour]` schedules the daily audit, on the guard's machine. It has no catch-up: a missed run waits for the next night.

On Linux the gardener and the audit need:

- **A private `/tmp`.** .NET's named mutexes and MSBuild create directories straight in `/tmp`, so the sandboxes must write it. The job refuses to start unless `/tmp` is its own: its systemd unit sets `PrivateTmp=yes`. For a manual run, use `systemd-run --user -p PrivateTmp=yes --wait --pipe bin/weekly-gardener <project>`.
- **An HTTPS `REPO_URL`.** In a user unit, `PrivateTmp` implies a user namespace where root-owned files show as `nobody`, and ssh then refuses its own config ("Bad owner or permissions"). Use an HTTPS URL with a git credential helper such as `gh auth setup-git`.
- **Lingering.** `loginctl enable-linger $USER` keeps the timers running after logout.
- **Single-node MSBuild.** The sandbox blocks Unix sockets, which MSBuild's worker nodes need, so .NET steps take `-m:1`.

## Project config

One file per project at `~/.config/kitchen/automation/<project>.env`, outside this public repo. `<project>` matches the project's folder name so `kitchen status` can find its history.

```bash
REPO_URL="git@github.com:owner/repo.git"
GH_REPO="owner/repo"
BRANCH="dev"
EXTRA_PATH="$HOME/.dotnet"            # tools launchd cannot find on its own
NEEDS_DOCKER=1
GUARD_LABEL="nightly-guard"
GARDENER_LABEL="gardener"
GUARD_STEPS=(                          # name|command, run in order in bash -e -o pipefail; the first failure stops the run.
                                       # Required: a run without steps is recorded incomplete, never green
  "build|make build"
  "tests|make test"
)
GUARD_STEP_BUDGETS=("tests|900")       # optional; name|seconds. A step over its budget is a warning, never a red run
CLEANUP_CMD="make stop"                # optional
METRICS_CMD="python3 scripts/metrics.py --json"   # optional; snapshot per run in the history
HISTORY_MIRROR="gardener-host:.local/state/kitchen/automation/<project>/history/"   # optional; rsync history.jsonl there after each recorded run, for a gardener on another host
QUALITY_CMD="python3 scripts/metrics.py"          # optional; what the gardener measures with
BASELINE_FILE="scripts/metrics.json"             # optional
HOOKS_PATH="scripts/hooks"             # optional; the project's hooks folder, kept working in the clone
LOCK_WAIT_SECONDS=3600                 # optional; how long a job waits for the other one before it skips as busy
GARDENER_MAX_CHANGED_LINES=400         # optional; larger gardener diffs are not published
GARDENER_PROTECTED_PATHS=(".github/*")  # optional; shell patterns the gardener may not touch
GARDENER_SANDBOX_DOMAINS=("api.nuget.org") # optional; domains the gardener's Bash may reach (default: none)
GARDENER_SANDBOX_READ=("$HOME/.dotnet") # optional; paths under $HOME its Bash may read (toolchains); the rest of $HOME is denied
GARDENER_SANDBOX_WRITE=("$HOME/.cache/kitchen/gardener/shop") # optional; paths outside the clone its Bash may read and write.
                                       # Give it caches of its own, never ones your own builds use (~/.nuget/packages,
                                       # ~/.npm): a package it plants there would run unsandboxed in your next build.
NUGET_PACKAGES="$HOME/.cache/kitchen/gardener/shop/nuget"  # not exported: reaches only the gardener, via GARDENER_ENV_PASS
GARDENER_ENV_PASS=(DOTNET_ROOT NUGET_PACKAGES)  # optional; extra variable names the agent's build needs
GARDENER_VERIFY_STEPS=(                # required to publish; name|command the job reruns on the final tree before publishing,
  "build|make build"                   # sandboxed like the agent (no Docker unless its socket is allowed)
  "tests|make unit-test"
)
GARDENER_VERIFY_TIMEOUT_SECONDS=3600   # optional; deadline for each verify step
GARDENER_PREPARE_CMD="make restore"   # optional; refreshes the caches the sandbox reads offline, before the agent.
                                       # Runs unsandboxed with the network, in a fresh export of the pinned base (reviewed
                                       # code, as the nightly runs), never on the agent's tree; a failure stops the run
GARDENER_SANDBOX_UNIX_SOCKETS=()       # optional; e.g. the Docker socket. Docker can mount any host path, so
                                       # allowing it reopens what the sandbox closes
GARDENER_MODEL="opus"                  # optional; claude --model for the gardener (unset: Claude Code's default)
GARDENER_EFFORT="high"                 # optional; claude --effort (low, medium, high, xhigh, max)
AUDIT_GUARD_STEPS=(prove control)      # required for the audit; guard steps that drove the app, which the sealed pass cites.
                                       # The latest guard run must be green and have run each of them
AUDIT_EVIDENCE_PATHS=(".verify/evidence") # optional; paths in the guard's clone the guard keeps after each AUDIT_GUARD_STEPS
                                       # step, for the agent; each must exist after each of those steps
AUDIT_EDIT_PATHS=(".agents/skills/verify-*/*") # optional; with AUDIT_VERIFY_STEPS, authorizes prose fixes in these paths
AUDIT_VERIFY_STEPS=("coverage|make coverage") # optional; rerun by the job on a fix before publishing, as GARDENER_VERIFY_STEPS
AUDIT_LABEL="audit-verification"       # optional; the audit PR's label
AUDIT_ISSUE_LABEL="audit-verification-findings" # optional; the label of the one open findings issue
AUDIT_MAX_CHANGED_LINES=200            # optional; larger audit fixes are not published
AUDIT_TIMEOUT_SECONDS=3600             # optional; deadline for the agent; past it the run is incomplete
AUDIT_MODEL="sonnet"                   # optional; claude --model for the audit (unset: Claude Code's default)
AUDIT_EFFORT="medium"                  # optional; claude --effort
GH_TIMEOUT_SECONDS=120                 # optional; deadline for every gh call
NET_TIMEOUT_SECONDS=600                # optional; deadline for clone, fetch and push
```

## Nightly record

One JSON line per run: `ts`, `started`, `sha` (full 40 characters), `status`, `failed_step`, `cleanup`, `metrics`, `steps`, `warnings`, `run_id`, `log`. `steps` lists the steps that finished, in order, as `{"name", "seconds", "status"}` (whole seconds, `green` or `red`); a run that stopped early lists only those, and records written before `steps` existed have none. The line is written as `running` when the run holds the lock and replaced when it ends. A run skipped as busy never held the lock, so it only logs: it writes no record, and can neither mask the result of the run that held the lock nor race its writes. A night without a recorded run shows as `overdue` in `kitchen status`.

| `status` | Meaning |
|---|---|
| `green` / `red` | The steps' verdict. Problems around it (`cleanup failed`, `metrics failed`, `issue report failed`) go to `warnings` and never change the verdict. |
| `running` | The run is in progress. |
| `incomplete` | Interrupted, aborted or timed out before a verdict; `failed_step` names the phase (a step, `sync`, `lock`, or `config` when no step ran because `GUARD_STEPS` is empty or unset). A `running` line left by a run killed with SIGKILL is closed as `incomplete` (`failed_step: killed`) by the next run. |

Trial runs (`GUARD_ONLY=...`) are neither recorded nor reported.

**Step budgets.** `GUARD_STEP_BUDGETS` holds `name|seconds` entries, apart from `GUARD_STEPS` because a command may contain `|`. A malformed entry or a name that is not a step is a warning, never a failure. A step that ran longer than its budget adds the warning `over budget: <step> <s>s > <budget>s`, which `kitchen status` shows with the nightly's other warnings, and never changes the verdict. On a green run with a step over budget the guard opens one issue labelled `<GUARD_LABEL>-budget`, or comments on the open one; the next green run with every budgeted step within budget closes it. Red and incomplete runs stopped early and prove nothing about time: they leave that issue alone. Without a valid budget the issue is left as is, so close it by hand when you remove the budgets. A failed issue call is the warning `budget issue report failed`.

**Evidence per step.** After each step named in `AUDIT_GUARD_STEPS`, the guard copies every `AUDIT_EVIDENCE_PATHS` entry from its clone into `~/.local/state/kitchen/automation/<project>/evidence/<run_id>/<step>/<path>`, so a later step that writes the same path cannot overwrite what an earlier one left. A missing path, or one outside the clone, is a warning; so is a copy that fails, and the guard then keeps nothing of that path, so the audit refuses instead of citing part of it. Only the evidence of the newest 3 runs that kept any is kept; trial runs keep none.

## Gardener record

One JSON line per run in `~/.local/state/kitchen/gardener/<project>.jsonl`: `ts`, `started`, `status`, `detail`, `warnings`, `run_id`, `log`, written as `running` when the run holds the lock and replaced when it ends. The same result is the run's one `GARDENER-RESULT` log line. A run skipped as busy never held the lock, so it only logs: it writes no record, and can neither mask the result of the run that held the lock nor race its writes.

| `status` | `detail` |
|---|---|
| `published` | The PR's URL. |
| `none` | Why nothing was published: no gardener branch, or a gardener PR still open. |
| `refused` | Why the job refused to publish, for example `no independent verification configured`. |
| `incomplete` | A prerequisite failed (Docker, srt, node, a private `/tmp`, `GARDENER_PREPARE_CMD`), claude failed, the run was aborted or interrupted, or processes it started survived TERM and KILL (`warnings` names them). A `running` line left by a killed run is closed as `incomplete` by the next one. |

`kitchen status` reads only this machine's record. The gardener counts as configured when the project's env leaves at least one `GARDENER_VERIFY_STEPS` step (or `integrate.toml` has a `gardener` key); then a missing record is an exception, `no record on this host`. Without either, the line says `not configured`, never green and never an exception. The same holds for the nightly, configured by at least one `GUARD_STEPS` step; a host that runs only the gardener sets `GUARD_STEPS=()`. `kitchen status` counts them by sourcing the env as the jobs do (bash, `set -euo pipefail`, `PATH=/usr/bin:/bin`, no stdin, `KITCHEN_ENV_TIMEOUT_SECONDS`, default 5), so the env runs there too. An env that fails to source or misses the deadline is `unknown`, never `not configured`. Where the gardener or the nightly runs on another host, set `gardener = "remote:<host-label>"` or `nightly = "remote:<host-label>"` in the project's `integrate.toml` entry: that line then says `remote (<host-label>): not read here`, never green and never an exception, and `kitchen integrate` ignores the key.

## Audit record

One JSON line per run in `~/.local/state/kitchen/audit/<project>.jsonl`: `ts`, `started`, `status`, `detail`, `sha` (the audited SHA, the guard's), `baseline`, `warnings`, `run_id`, `log`, written as `running` when the run holds the lock and replaced when it ends; the same result is the run's one `AUDIT-RESULT` log line. `kitchen status` shows the last line as `audit`: green for `clean`, `changed` or `skipped`, an exception for any other status, and `overdue` past two days (daily, with no catch-up, plus a day of grace: the time is the run's end, which moves with its length and the lock wait). The audit counts as configured when the env leaves at least one `AUDIT_GUARD_STEPS` step, which the job requires; then a missing record is an exception, `no record`. A host that does not run the audit sets `AUDIT_GUARD_STEPS=()` or leaves it unset: the line says `not configured`, never green and never an exception.

| `status` | Meaning |
|---|---|
| `clean`, `changed`, `findings`, `blocked`, `skipped` | The pass's outcome, as the skill defines it. `changed` carries the PR's URL, `findings` the report's path, the findings issue and, when its prose fixes were published, the PR's URL, the others the report's path. A red or incomplete latest guard run is `blocked` and the agent does not run; an audited SHA equal to the baseline is `skipped`, also without the agent. Each outcome the agent reached, and each skip, is also logged in the kitchen journal as `audit-verification <outcome> churn at <sha>`, on the project's behalf, where the next churn pass finds its baseline. |
| `refused` | The job refused an input or the publication: no baseline in the journal (run a full pass by hand), a guard run without one of `AUDIT_GUARD_STEPS`, a guard clone no longer at the guard's SHA, an evidence path the guard did not keep after one of those steps, a `changed` fix the publication refused, or a committed fix under an outcome of `clean` or `skipped`. A refused fix, or `changed` without authorization, is logged in the journal as `findings`, so the drift stays in the next pass's scope. |
| `incomplete` | Claude failed or outlived `AUDIT_TIMEOUT_SECONDS`, wrote no valid outcome or no report, a prerequisite failed, or the run was aborted or killed. Nothing is logged in the journal. |

The boundary is the gardener's, made stricter. The agent runs in the same sandbox (`lib/sealed.sh`, the `GARDENER_SANDBOX_READ`, `GARDENER_SANDBOX_WRITE` and `GARDENER_ENV_PASS` settings), in its own clone at the audited SHA, with the Agent tool for the skill's source wave. `GARDENER_SANDBOX_DOMAINS` and `GARDENER_SANDBOX_UNIX_SOCKETS` never reach it or its verification: no network but the model API, and no Docker socket. It has no Docker: running the agent's harness code live would need Docker outside the sandbox, which can mount any host path. So the job never drives a fix live. It publishes only Markdown prose outside fenced code blocks (`lib/prose_only.py`) within `AUDIT_EDIT_PATHS`, after the gardener's checks (branch name `audit/<date>-<slug>`, size, `.github/*`, secret scan) and `AUDIT_VERIFY_STEPS` rerun under srt; a fix to a drive block, an assertion or the verifier is a finding. A `findings` pass publishes its prose fixes with the same checks; a refused publication is a warning and the outcome stays `findings`. The agent gets the evidence the guard kept after each cited step, at `guard/evidence/<step>/<path>` in its work dir. Its clone is made from the guard's clone, which must still be at the guard's SHA, so schedule the audit after the guard and before the gardener.

Findings go to one GitHub issue per project, labelled `AUDIT_ISSUE_LABEL`: opened when none is open, else commented on. The body names the outcome, audited SHA, baseline and PR, then the report, redacted and cut under GitHub's limit with a pointer to the full report. The baseline moves past a `findings` pass only when its report landed in the issue: its journal line is then `audit-verification findings churn at <sha> (issue #N)`; a failed report is a warning and the baseline stays. The job never closes the issue, since a churn pass covers only its scope: a full pass by hand that ends clean or changed closes it. `blocked`, `refused` and `incomplete` never move the baseline.

## Notifications

`lib/notify.py` (wrapping `lib/kitchen/notify.py`) shows a local notification for exceptions only: a red or incomplete nightly, a refused or incomplete gardener run, an audit that ends findings, blocked, refused or incomplete, and `kitchen log --status blocked|decision`. Green runs stay silent. macOS uses `osascript` (`display notification`); Linux uses `notify-send` when it is installed. Without a notifier it logs `no notifier available` and the run records the warning `not notified`: the absence shows, and nothing is sent anywhere else. The text is redacted with the patterns `kitchen retro` uses. There is no external webhook.

On Linux a notification reaches only the session bus of the user the timers run as. If that user has no notification server there, for example because the desktop is logged in as another user, `notify-send` fails with `The name is not activatable` and every exception run records `not notified`. `kitchen doctor` asks the timers' bus for its notification server, without showing anything, and warns when none answers.

## Guarantees

- Jobs work in their own clone; your checkout is never touched. One job at a time per project: each job runs under `lib/supervise.py`, which holds the project lock and runs the job in its own process group. When the job ends, however it ends, the group is stopped, then every process that still has its working directory or an open file in the clone or the gardener's work dirs (found with `lsof`, which also catches descendants that left the group with `setsid`) gets TERM, then KILL. Processes that were already running when the job started are not the job's and are left alone, such as Docker Desktop's VM, which keeps bind-mounted files open after its containers are gone; that is also why a job that needs Docker starts it before the job starts, and fails its docker phase when Docker only became usable after that. If any survive, the run's line in its own record (the nightly's or the gardener's) is marked `incomplete`, with `failed_step: survivors <pids>` and a warning naming them, and the project stays busy until they exit. Do not open a shell or editor inside the automation clone while a job runs: the job stops it.
- Every gh call and every clone, fetch and push has a deadline, so a hung network call cannot hold a run or its record.
- A pre-push hook in the clone refuses pushes to `dev`, `main`, `master` and `$BRANCH`. The project's own hooks (`HOOKS_PATH`, or the hooks path the project configured in the clone) keep running next to it.
- The gardener's agent runs sealed. Its environment is an allowlist (`env -i` plus `HOME`, `USER`, `LOGNAME`, `SHELL`, `TMPDIR`, `LANG`, `LC_*`, `TERM`, the job's own variables, `GARDENER_ENV_PASS`, and `ANTHROPIC_API_KEY`/`CLAUDE_CODE_OAUTH_TOKEN` only when set, because claude then needs them to log in). Claude Code re-applies the user's shell startup files to every Bash command, which would bring back their exports, functions and aliases. On macOS the shell is therefore zsh with an empty `ZDOTDIR`. On Linux it is bash, an owner's choice for a host without zsh: `~/.bashrc` then reaches the agent's Bash, and only the sandbox keeps credentials and the network out. Toolchain variables the build needs go in `GARDENER_ENV_PASS`. No `gh` (a shim refuses every call), `GIT_SSH_COMMAND` and askpass refuse, git credential helpers cleared, `origin`'s push URL refused, no MCP servers.
- Its reads are an allowlist. Claude Code's `permissions.blockReadsOutsideWorkingDirectories` makes Read, Grep and Glob refuse anything outside the working directories, and its Bash runs in Claude Code's OS sandbox (`lib/sandbox_settings.py`; Seatbelt on macOS, bubblewrap with its own network namespace on Linux; `failIfUnavailable`, no unsandboxed escape) with all of `$HOME` denied and only the clone, the run's work dir, the metrics history and `GARDENER_SANDBOX_READ`/`GARDENER_SANDBOX_WRITE` re-opened. Credential locations stay denied even inside those, and git sees an identity-only config instead of `~/.gitconfig`. Bash writes only to the clone, the work dir and `GARDENER_SANDBOX_WRITE`; network only to `GARDENER_SANDBOX_DOMAINS`. The claude process itself stays outside, so its login and the model API work.
- It commits on `gardener/<date>-<slug>` and writes a PR summary. The job validates the branch's final tree (the only gardener branch, starts from `origin/$BRANCH`, has commits, within `GARDENER_MAX_CHANGED_LINES`, none of `GARDENER_PROTECTED_PATHS`, and no added line that looks like a credential: kitchen redaction patterns, known token shapes, or long random-looking strings, see `lib/scan_diff.py`) and publishes exactly that tree as ONE new commit on `origin/$BRANCH`, never the agent's history. Every check and the commit use SHAs pinned once (the base before Claude ran, the branch's commit and tree), never ref names that a leftover process could move.
- Before publishing, the job reruns `GARDENER_VERIFY_STEPS` itself on exactly the validated tree, written from the git objects by `lib/export_tree.py` (no `.gitattributes` such as `export-ignore`, no filters, no hooks, no symlink followed). It is committed into a fresh git repository, for checks that run git, and the job refuses unless that commit holds exactly the validated tree. On Linux, both sandboxes put an unreadable device over each dangerous file that does not exist yet (`.gitmodules`, shell rc files, `.vscode`...). So the agent's clone and the verify checkout exclude those names from git, and get an empty `.gitmodules`, which the .NET SDK reads. Each step runs under `srt` (`@anthropic-ai/sandbox-runtime`, the runtime behind Claude Code's sandbox, pinned in `package.json`; `bin/install-schedule` installs it) with the agent's boundary from `lib/sandbox_settings.py --format srt` and its environment allowlist without the claude login, in `bash -e -o pipefail`. A step passes only when srt exits 0 and the step recorded exit 0 (srt reports a child killed by a signal as 0). A failing, killed or hung step publishes nothing, and an empty `GARDENER_VERIFY_STEPS` publishes nothing either (`GARDENER-RESULT: refused - no independent verification configured`): the agent's own report is not evidence. The job refuses that before the agent runs, and `verify_tree` refuses it again at publication. The PR body ends with the steps that passed. `srt` is a node script: `EXTRA_PATH` must reach `node` under launchd.
- Nothing the agent wrote runs unsealed afterwards, and no git command of the job reads the agent's git config or hooks. The agent works in a fresh clone of its own (`gardener/<run>/clone`, deleted at the end; the nightly's clone is never handed to it). The job lists its branch with `for-each-ref`, copies the objects into a bare repository of its own and re-hashes them with `git fsck`, then validates, verifies and pushes from there. Its settings, exports and PR text live in `gardener-job/<run>/`, which the agent cannot write, so nothing it plants in its work dir redirects the job's writes. The project's cleanup command runs in a fresh export of the pinned base.
- What content scanning can and cannot do: it catches accidental leaks (a token pasted into a file, also when split across lines). Encoded or deliberately disguised exfiltration (base64, hex, pieces spread around) is stopped only by the boundary: sandboxed reads that never reach the secrets, and an environment that never holds them. Otherwise it logs one `GARDENER-RESULT: refused - <reason>` line and exits 1.
- Log lines posted to GitHub issues and the gardener's PR text are redacted with the same secret patterns as `kitchen retro`.
- Nothing degrades silently: missing Docker fails the run, and a failed cleanup, metrics step or issue report is logged and recorded as a warning.
