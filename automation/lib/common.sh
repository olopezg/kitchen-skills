#!/usr/bin/env bash
# Shared helpers for kitchen automation jobs. Usage: source common.sh <project> nightly|gardener|audit (the job's own record)
# Config: ${KITCHEN_CONFIG:-~/.config/kitchen}/automation/<project>.env (private, never in this repo)
# State:  ${KITCHEN_STATE:-~/.local/state/kitchen}/automation/<project>/, and nightly/, gardener/ and audit/<project>.jsonl
set -euo pipefail
# Jobs only ever work on their own clone; a GIT_DIR inherited from a git hook would redirect them.
unset GIT_DIR GIT_WORK_TREE GIT_INDEX_FILE GIT_OBJECT_DIRECTORY GIT_ALTERNATE_OBJECT_DIRECTORIES GIT_COMMON_DIR GIT_PREFIX GIT_NAMESPACE

KITCHEN_AUTOMATION="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PROJECT="${1:?usage: <job> <project>}"
CONFIG_ROOT="${KITCHEN_CONFIG:-$HOME/.config/kitchen}"
STATE_ROOT="${KITCHEN_STATE:-$HOME/.local/state/kitchen}"
CONFIG="$CONFIG_ROOT/automation/$PROJECT.env"
[ -f "$CONFIG" ] || { echo "no config: $CONFIG" >&2; exit 2; }
# shellcheck source=/dev/null
source "$CONFIG"

STATE_DIR="$STATE_ROOT/automation/$PROJECT"
CLONE_DIR="$STATE_DIR/clone"
LOG_DIR="$STATE_DIR/logs"
HISTORY_DIR="$STATE_DIR/history"
NIGHTLY_RECORD="$STATE_ROOT/nightly/$PROJECT.jsonl"
GARDENER_RECORD="$STATE_ROOT/gardener/$PROJECT.jsonl"
AUDIT_RECORD="$STATE_ROOT/audit/$PROJECT.jsonl"
mkdir -p "$LOG_DIR" "$HISTORY_DIR" "$(dirname "$NIGHTLY_RECORD")" "$(dirname "$GARDENER_RECORD")" "$(dirname "$AUDIT_RECORD")"
case "${2:-}" in  # the record this job's run writes, where the supervisor marks its survivors
  nightly) JOB_RECORD="$NIGHTLY_RECORD" ;;
  gardener) JOB_RECORD="$GARDENER_RECORD" ;;
  audit) JOB_RECORD="$AUDIT_RECORD" ;;
  *) echo "common.sh: the job must name its record, nightly, gardener or audit (got '${2:-}')" >&2; exit 2 ;;
esac

# launchd starts with a bare PATH. The shims go first so the gh wrapper wins;
# the wrapper calls the real gh found without the shims.
TOOL_PATH="${EXTRA_PATH:-}:$HOME/.local/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"
export KITCHEN_REAL_GH="${KITCHEN_REAL_GH:-$(PATH="$TOOL_PATH" command -v gh || true)}"
export PATH="$KITCHEN_AUTOMATION/shims:$TOOL_PATH"

# A step is name|command with both parts non-blank. An empty command would pass for free: `bash -c ""`
# exits 0, so a step list holding one proves nothing. Prints each malformed entry, one per line.
malformed_steps() { # entries...
  local entry name cmd
  for entry in "$@"; do
    if [[ "$entry" != *"|"* ]]; then printf '%s\n' "${entry:-<empty>}"; continue; fi
    name="${entry%%|*}"; cmd="${entry#*|}"
    if [ -z "${name//[[:space:]]/}" ] || [ -z "${cmd//[[:space:]]/}" ]; then printf '%s\n' "$entry"; fi
  done
}

log() { printf '%s %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$*"; }

# A deadline for anything that waits on the network (gh goes through the bounded gh shim).
bounded() { python3 "$KITCHEN_AUTOMATION/lib/bounded.py" "$@"; }
NET_TIMEOUT="${NET_TIMEOUT_SECONDS:-600}"

# Problems that did not change the verdict but must not hide: logged now, recorded with the run.
WARNINGS=()
warn() { WARNINGS+=("$1"); log "WARNING: $1"; }

# Secrets out of anything that leaves the machine (stdin to stdout).
redact() { python3 "$KITCHEN_AUTOMATION/lib/redact.py"; }

# A local notification for an exception (never call it on green): redacted, shown on this machine only.
# Returns non-zero, saying why on stderr, when it was not shown, also when the platform has no notifier.
notify() { python3 "$KITCHEN_AUTOMATION/lib/notify.py" "$1" "$2"; }

# One open GitHub issue per label and title: a comment on it while it is open, else a new one. The body travels as a
# file (an argument is capped far below GitHub's body limit). Sets ISSUE_NUMBER; returns 1 when a gh call failed or
# gh printed no issue URL, and the caller records the warning.
open_issue_number() { # label title
  gh issue list -R "$GH_REPO" --label "$1" --state open --search "in:title \"$2\"" --json number --jq '.[0].number // empty'
}
report_issue() { # label color label-description title body-file
  local existing url
  ISSUE_NUMBER=""
  gh label create "$1" -R "$GH_REPO" --color "$2" --description "$3" >/dev/null 2>&1 || true
  existing="$(open_issue_number "$1" "$4")" || return 1
  if [ -n "$existing" ]; then
    gh issue comment "$existing" -R "$GH_REPO" --body-file "$5" >/dev/null || return 1
    ISSUE_NUMBER="$existing"
    log "commented on issue #$existing"
  else
    url="$(gh issue create -R "$GH_REPO" --title "$4" --label "$1" --body-file "$5")" || return 1
    [[ "${url##*/}" =~ ^[1-9][0-9]*$ ]] || { log "gh issue create printed no issue URL: ${url:0:200}"; return 1; }
    ISSUE_NUMBER="${url##*/}"
    log "opened issue #$ISSUE_NUMBER: $4"
  fi
}

# A dedicated clone at the tip of $BRANCH; the developer's checkout is never touched.
# Destructive (reset and clean): call it only while holding the project lock. Hooks stay off until the
# tree is back at origin/$BRANCH: the gardener's agent could write files in the previous working tree.
sync_clone() {
  if [ ! -d "$CLONE_DIR/.git" ]; then
    bounded "$NET_TIMEOUT" git clone --quiet "$REPO_URL" "$CLONE_DIR"
  fi
  bounded "$NET_TIMEOUT" git -C "$CLONE_DIR" -c core.hooksPath=/dev/null fetch --quiet --prune origin
  git -C "$CLONE_DIR" -c core.hooksPath=/dev/null checkout --quiet --force --detach "origin/$BRANCH"
  git -C "$CLONE_DIR" -c core.hooksPath=/dev/null reset --quiet --hard "origin/$BRANCH"
  git -C "$CLONE_DIR" -c core.hooksPath=/dev/null clean --quiet -fdx -e node_modules
  install_push_guard
}

# Automation may push its own branches, never the shared ones. The project's own hooks keep working:
# core.hooksPath points at a kitchen folder that forwards every hook of the project's hooks folder and
# puts the pre-push guard in front of the project's own pre-push. The project's folder is HOOKS_PATH in
# the config, else a hooks path the project set in the clone since the last install, else the one
# remembered by an earlier install (kitchen.projectHooksPath), else .git/hooks. Installing twice is a no-op.
install_push_guard() {
  local git_dir hooks_dir current project_hooks hook name
  git_dir="$(git -C "$CLONE_DIR" rev-parse --absolute-git-dir)"
  hooks_dir="$git_dir/kitchen-hooks"
  current="$(git -C "$CLONE_DIR" config --get core.hooksPath || true)"
  if [ -n "${HOOKS_PATH:-}" ]; then
    project_hooks="$HOOKS_PATH"
  elif [ -n "$current" ] && [ "$current" != "$hooks_dir" ]; then
    project_hooks="$current"
  else
    project_hooks="$(git -C "$CLONE_DIR" config --get kitchen.projectHooksPath || true)"
    [ -n "$project_hooks" ] || project_hooks="$git_dir/hooks"
  fi
  git -C "$CLONE_DIR" config kitchen.projectHooksPath "$project_hooks"
  case "$project_hooks" in /*) ;; *) project_hooks="$CLONE_DIR/$project_hooks" ;; esac
  rm -rf "$hooks_dir"
  mkdir -p "$hooks_dir"
  for hook in "$project_hooks"/*; do
    [ -f "$hook" ] || continue
    name="$(basename "$hook")"
    case "$name" in *.sample|pre-push) continue ;; esac
    printf '#!/usr/bin/env bash\nhook=%q\n[ -x "$hook" ] || exit 0\nexec "$hook" "$@"\n' "$hook" > "$hooks_dir/$name"
    chmod +x "$hooks_dir/$name"
  done
  cat > "$hooks_dir/pre-push" <<HOOK
#!/usr/bin/env bash
input="\$(cat)"
while read -r local_ref local_sha remote_ref remote_sha; do
  case "\$remote_ref" in
    refs/heads/dev|refs/heads/main|refs/heads/master|$(printf %q "refs/heads/$BRANCH"))
      echo "kitchen automation: refusing to push to \$remote_ref" >&2; exit 1 ;;
  esac
done <<< "\$input"
hook=$(printf %q "$project_hooks/pre-push")
[ -x "\$hook" ] || exit 0
if [ -n "\$input" ]; then printf '%s\n' "\$input"; fi | "\$hook" "\$@"
HOOK
  chmod +x "$hooks_dir/pre-push"
  git -C "$CLONE_DIR" config core.hooksPath "$hooks_dir"
}

# Starts Docker Desktop on macOS when it is not running. Called before the supervisor lists the processes that
# predate the job: started from inside the job, Docker's VM would count as the job's own and be killed when the
# job ends (issue #61). It only starts Docker and notes in KITCHEN_DOCKER whether it was ready before the job
# began; the job's docker phase (docker_ready) decides.
start_docker() {
  export KITCHEN_DOCKER=unavailable
  if docker info >/dev/null 2>&1; then KITCHEN_DOCKER=ready; return 0; fi
  [ "$(uname)" = Darwin ] || return 0
  log "docker not running; starting Docker Desktop"
  if ! open -a Docker >/dev/null 2>&1; then log "open -a Docker failed"; return 0; fi
  for _ in $(seq 1 "${DOCKER_WAIT_TRIES:-60}"); do
    if docker info >/dev/null 2>&1; then KITCHEN_DOCKER=ready; return 0; fi
    sleep "${DOCKER_WAIT_SECONDS:-5}"
  done
}

# Never pretends: returns 1, and says so, when Docker is not usable. A Docker that only became usable after the
# job began does not count: its VM may be younger than the job, and the supervisor would stop it with the job.
docker_ready() {
  if [ "$DOCKER_BEFORE_JOB" != ready ]; then
    log "docker was not usable before the job started"
    return 1
  fi
  docker info >/dev/null 2>&1 && return 0
  log "docker is not available"
  return 1
}

# The nightly history that `kitchen status` reads: one line per run, replaced in place by run_id.
record_nightly() { # status failed_step cleanup metrics sha run_id log started steps [warning...]
  python3 "$KITCHEN_AUTOMATION/lib/record.py" "$NIGHTLY_RECORD" write "$@"
}

# The gardener's history: one line per run, its result published / none / refused / incomplete (or running).
record_gardener() { # status detail run_id log started [warning...]
  python3 "$KITCHEN_AUTOMATION/lib/record.py" "$GARDENER_RECORD" gardener "$@"
}

# The daily audit's history: one line per run, its outcome (clean, changed, findings, blocked, skipped) or refused /
# incomplete (or running).
record_audit() { # status detail sha baseline run_id log started [warning...]
  python3 "$KITCHEN_AUTOMATION/lib/record.py" "$AUDIT_RECORD" audit "$@"
}

# Closes "running" lines left by runs that were killed; only called while holding the project lock.
close_stale_records() { # current-run-id [record, default the nightly's]
  python3 "$KITCHEN_AUTOMATION/lib/record.py" "${2:-$NIGHTLY_RECORD}" close-stale "$1"
}

# Every job runs under lib/supervise.py: one job at a time per project (the guard, the gardener and the audit share
# the clone that sync_clone resets), in its own process group that is stopped as a whole before the lock
# is released. The supervisor re-runs this script with KITCHEN_LOCK_STATE=held, or =busy when the lock
# stayed taken for LOCK_WAIT_SECONDS (default 3600); the job then only logs the skip: no job writes its
# record without holding the lock.
# The supervisor also refuses to release the project while any process of the job still has files open in
# the clone or the agents' work dirs (see lib/supervise.py); such survivors mark this run's line in its own record
# ($JOB_RECORD) incomplete.
if [ -z "${KITCHEN_LOCK_STATE:-}" ]; then
  [ "${NEEDS_DOCKER:-0}" != 1 ] || start_docker
  export KITCHEN_RUN_ID="$(date -u +%Y%m%dT%H%M%SZ)-$$"
  exec python3 "$KITCHEN_AUTOMATION/lib/supervise.py" "$STATE_DIR/job.lock" "${LOCK_WAIT_SECONDS:-3600}" "${LOCK_POLL_SECONDS:-5}" \
    --watch "$CLONE_DIR" --watch "$STATE_DIR/gardener" --watch "$STATE_DIR/gardener-job" \
    --watch "$STATE_DIR/audit" --watch "$STATE_DIR/audit-job" --record "$JOB_RECORD" --run-id "$KITCHEN_RUN_ID" \
    -- "$BASH" "$0" "$PROJECT"
fi
LOCK_STATE="$KITCHEN_LOCK_STATE"
RUN_ID="$KITCHEN_RUN_ID"
DOCKER_BEFORE_JOB="${KITCHEN_DOCKER:-}"
unset KITCHEN_LOCK_STATE KITCHEN_RUN_ID KITCHEN_DOCKER
