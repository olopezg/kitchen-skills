#!/usr/bin/env bash
# The sealed agent run and its publication, shared by the jobs that run an agent (weekly-gardener, daily-audit).
# Source it after common.sh. The job sets, before calling these functions:
#   AGENT_KIND      gardener or audit: names the agent's branch prefix (<kind>/<date>-<slug>) and the job's log lines
#   WORK_DIR        the run's work dir, the only place the agent writes besides its clone (AGENT_CLONE inside it)
#   JOB_DIR         settings, exports and PR text, outside every path the agent may write
#   SUMMARY_FILE    the PR summary the agent writes
#   AGENT_ENV       extra NAME=value pairs the agent sees (its branch prefix, summary file...)
#   AGENT_DIRS      extra directories the agent reads (its Bash and its Read, Grep and Glob tools)
#   VERIFY_STEPS    name|command steps the job reruns on the final tree before publishing; VERIFY_STEPS_NAME is
#                   the config variable they come from, for messages
#   PUBLISH_LABEL, MAX_CHANGED_LINES, PROTECTED_PATHS (shell patterns never published), EDIT_PATHS (when not
#   empty, the only shell patterns published), PROSE_ONLY (1: only Markdown prose outside fenced code blocks,
#   lib/prose_only.py), RESULT_TAG (the job's one result line)
# and defines result <status> <detail>, refuse <reason> (records refused, returns 1) and stop <reason> (records
# incomplete, exits). Both jobs use the project's sandbox config: GARDENER_SANDBOX_READ/WRITE/DOMAINS/UNIX_SOCKETS
# and GARDENER_ENV_PASS (see README.md).
# Principles: `isolate` (the agent is sealed; nothing it wrote runs unsealed), `prove` (the job reruns the checks).

declare -p GARDENER_SANDBOX_DOMAINS >/dev/null 2>&1 || GARDENER_SANDBOX_DOMAINS=()
declare -p GARDENER_SANDBOX_WRITE >/dev/null 2>&1 || GARDENER_SANDBOX_WRITE=()
declare -p GARDENER_SANDBOX_READ >/dev/null 2>&1 || GARDENER_SANDBOX_READ=()
declare -p GARDENER_ENV_PASS >/dev/null 2>&1 || GARDENER_ENV_PASS=()  # extra variable names the build needs
declare -p GARDENER_SANDBOX_UNIX_SOCKETS >/dev/null 2>&1 || GARDENER_SANDBOX_UNIX_SOCKETS=()
VERIFY_TIMEOUT="${GARDENER_VERIFY_TIMEOUT_SECONDS:-3600}"
SRT="$KITCHEN_AUTOMATION/node_modules/.bin/srt"  # pinned in automation/package.json
RUN_DATE="$(date -u +%Y-%m-%d)"
PLATFORM_READ=()
PLATFORM_WRITE=()

# Claude's environment: everything it needs to edit, build, test and commit; nothing to publish with.
run_sealed() {
  local sealed="$KITCHEN_AUTOMATION/shims/sealed" name keep=()
  mkdir -p "$WORK_DIR/gh-config"
  # The environment is an allowlist: nothing inherited reaches Claude unless named here. HOME and USER let
  # claude read its own login from the macOS keychain; the two auth variables pass only when set (an
  # account that logs in with a token instead), and are then visible to the agent's Bash too.
  # Claude Code re-applies the user's shell startup files to every Bash command (a shell snapshot taken
  # outside the sandbox), which would bring back their exports, functions and aliases, tokens included.
  # AGENT_SHELL (set per platform by sandbox_platform) is zsh with an empty ZDOTDIR on macOS: no user startup
  # file is read.
  mkdir -p "$WORK_DIR/zdotdir"
  for name in HOME USER LOGNAME TMPDIR LANG TERM ANTHROPIC_API_KEY CLAUDE_CODE_OAUTH_TOKEN \
    ${GARDENER_ENV_PASS[@]+"${GARDENER_ENV_PASS[@]}"} $(compgen -e | grep '^LC_' || true); do
    if [ -n "${!name+x}" ]; then keep+=("$name=${!name}"); fi
  done
  env -i "${keep[@]}" \
    "${AGENT_SHELL[@]}" \
    PATH="$sealed:$TOOL_PATH" \
    GH_CONFIG_DIR="$WORK_DIR/gh-config" \
    GIT_SSH_COMMAND="$sealed/refuse" GIT_ASKPASS="$sealed/refuse" SSH_ASKPASS="$sealed/refuse" GIT_TERMINAL_PROMPT=0 \
    GIT_CONFIG_GLOBAL="$WORK_DIR/gitconfig" \
    GIT_CONFIG_COUNT=2 \
    GIT_CONFIG_KEY_0=credential.helper GIT_CONFIG_VALUE_0= \
    GIT_CONFIG_KEY_1=remote.origin.pushurl GIT_CONFIG_VALUE_1="kitchen-sealed://the-$AGENT_KIND-job-publishes" \
    ${AGENT_ENV[@]+"${AGENT_ENV[@]}"} \
    "$@"
}

# On Linux both sandboxes put an unreadable device over each dangerous file or directory that does not
# exist yet, so it cannot be created. git add then fails on those devices ("can only add regular files"),
# and the .NET SDK's SourceLink fails reading .gitmodules. So the checkout excludes every masked name
# (srt 0.0.78 and Claude Code 2.1.287; excluding a tracked name changes nothing) and gets an empty,
# excluded .gitmodules, which stays write-protected like the masks.
SANDBOX_MASKED_NAMES=(.gitconfig .gitmodules .bashrc .bash_profile .bash_login .bash_logout .bash_aliases .profile
  .zshrc .zprofile .zshenv .zlogin .zlogout .envrc .ripgreprc .mcp.json .claude.json .npmrc .yarnrc .yarnrc.yml
  .pnp.cjs .pnp.loader.mjs .pnpmfile.cjs bunfig.toml .bunfig.toml .bazelrc .bazelversion .bazeliskrc
  .pre-commit-config.yaml lefthook.yml .lefthook.yml lefthook.yaml .lefthook.yaml gradle-wrapper.properties
  maven-wrapper.properties .devcontainer.json pyrightconfig.json .vscode .idea .claude/commands .claude/agents)
prepare_linux_checkout() {
  local name
  [ "$(uname)" = Linux ] || return 0
  mkdir -p "$1/.git/info"
  for name in "${SANDBOX_MASKED_NAMES[@]}"; do echo "/$name" >> "$1/.git/info/exclude"; done
  [ -e "$1/.gitmodules" ] || : > "$1/.gitmodules"
}

# The agent's own clone at a pinned commit, made fresh from the job's clone (never shared with the nightly guard).
make_agent_clone() { # sha
  git -c core.hooksPath=/dev/null clone --quiet --no-hardlinks --no-checkout "$CLONE_DIR" "$AGENT_CLONE"
  git -C "$AGENT_CLONE" -c core.hooksPath=/dev/null checkout --quiet --detach "$1"
  git -C "$AGENT_CLONE" update-ref "refs/remotes/origin/$BRANCH" "$1"
  local project_hooks
  project_hooks="$(git -C "$CLONE_DIR" config --get kitchen.projectHooksPath || true)"
  [ -z "$project_hooks" ] || git -C "$AGENT_CLONE" config kitchen.projectHooksPath "$project_hooks"
  CLONE_DIR="$AGENT_CLONE" install_push_guard
}

# What the agent wrote and the job publishes (its PR summary, the audit's report) is read once, without following a
# link, into a path the agent cannot write, and only that copy is used: the agent writes its work dir, so it could
# link the file to any host file. Returns 1 unless <file> is a regular file with a single link.
copy_agent_file() { # file copy
  python3 -c '
import os, stat, sys
fd = os.open(sys.argv[1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
info = os.fstat(fd)
if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
    sys.exit(1)
with os.fdopen(fd, "rb") as source, open(sys.argv[2], "wb") as copy:
    copy.write(source.read())' "$1" "$2" 2>/dev/null
}

# srt runs the job's verification; it is a node script, and EXTRA_PATH must reach node under launchd.
require_srt() {
  [ -x "$SRT" ] || stop "$VERIFY_STEPS_NAME is set and srt is not installed: npm ci --ignore-scripts --prefix $KITCHEN_AUTOMATION"
  PATH="$TOOL_PATH" command -v node >/dev/null || stop "$VERIFY_STEPS_NAME is set and srt needs node, which is not on EXTRA_PATH"
}

# What the sandboxes need beyond the project's paths, per platform: PLATFORM_READ, PLATFORM_WRITE, AGENT_SHELL.
sandbox_platform() {
  case "$(uname)" in
    Darwin) AGENT_SHELL=(SHELL=/bin/zsh ZDOTDIR="$WORK_DIR/zdotdir") ;;
    Linux)
      # .NET's named mutexes and MSBuild create directories straight in /tmp, ignoring TMPDIR, so the
      # sandboxes must write /tmp. That is safe only when /tmp is this run's own: systemd PrivateTmp=yes.
      if ! findmnt -n -o FSROOT --mountpoint /tmp | tail -n 1 | grep -q '^/systemd-private-'; then
        stop "on Linux the $AGENT_KIND needs a private /tmp: run it from its systemd unit, see install-schedule, or with systemd-run --user -p PrivateTmp=yes"
      fi
      mkdir -p /tmp/claude  # srt points TMPDIR here; a fresh private /tmp lacks it
      PLATFORM_WRITE=(/tmp)
      PLATFORM_READ=("$KITCHEN_AUTOMATION/node_modules/@anthropic-ai/sandbox-runtime/vendor/seccomp")  # srt runs its helper inside
      # bash, as the owner chose on 2026-10-04 (no zsh on the Linux host): Claude Code then applies
      # ~/.bashrc, so its exports and PATH reach the agent's Bash. The sandbox still denies the credential
      # paths and the network.
      AGENT_SHELL=(SHELL=/bin/bash) ;;
    *)
      stop "unsupported platform: $(uname)" ;;
  esac
  prepare_linux_checkout "$AGENT_CLONE"
}

# The agent's Claude Code settings, $WORK_DIR/settings.json. Reads are an allowlist: the clone, the run's work dir,
# AGENT_DIRS and the project's tool paths; writes only to the work dir and the configured paths.
write_agent_settings() {
  local path name sandbox_args=(--read "$AGENT_CLONE" --read "$WORK_DIR")
  for path in ${AGENT_DIRS[@]+"${AGENT_DIRS[@]}"}; do sandbox_args+=(--read "$path"); done
  sandbox_args+=(--write "$WORK_DIR")
  for path in ${GARDENER_SANDBOX_READ[@]+"${GARDENER_SANDBOX_READ[@]}"}; do sandbox_args+=(--read "$path"); done
  for path in ${GARDENER_SANDBOX_WRITE[@]+"${GARDENER_SANDBOX_WRITE[@]}"} ${PLATFORM_WRITE[@]+"${PLATFORM_WRITE[@]}"}; do
    sandbox_args+=(--read "$path" --write "$path")
  done
  for name in ${GARDENER_SANDBOX_DOMAINS[@]+"${GARDENER_SANDBOX_DOMAINS[@]}"}; do sandbox_args+=(--domain "$name"); done
  for path in ${GARDENER_SANDBOX_UNIX_SOCKETS[@]+"${GARDENER_SANDBOX_UNIX_SOCKETS[@]}"}; do sandbox_args+=(--socket "$path"); done
  python3 "$KITCHEN_AUTOMATION/lib/sandbox_settings.py" "${sandbox_args[@]}" > "$WORK_DIR/settings.json"
  # ~/.gitconfig is outside the allowlist: the agent's git gets only the identity.
  git config --file "$WORK_DIR/gitconfig" user.name "$(git config user.name || echo "kitchen-$AGENT_KIND")"
  git config --file "$WORK_DIR/gitconfig" user.email "$(git config user.email || echo "kitchen-$AGENT_KIND@localhost")"
}

# The sealed Claude Code run, in the agent's clone. Returns claude's exit code; 124 when it outlived <timeout>.
# <model> and <effort> pass as --model and --effort when set; unset, Claude Code's own defaults apply.
# --setting-sources local: the user's settings (their interactive hooks, e.g. the kitchen guards, which block
# legitimate cleanup inside the clone) and the project's checked-in settings (hooks from the tree under work)
# do not load; the OS sandbox from --settings is the boundary here, and --settings always applies.
run_agent() { # prompt timeout-seconds-or-empty model-or-empty effort-or-empty [extra-allowed-tool...]
  local prompt="$1" timeout="$2" model="$3" effort="$4" bound=() choice=()
  shift 4
  [ -z "$timeout" ] || bound=(python3 "$KITCHEN_AUTOMATION/lib/bounded.py" "$timeout")
  [ -z "$model" ] || choice+=(--model "$model")
  [ -z "$effort" ] || choice+=(--effort "$effort")
  (cd "$AGENT_CLONE" && run_sealed ${bound[@]+"${bound[@]}"} claude -p "$prompt" \
    ${choice[@]+"${choice[@]}"} \
    --setting-sources local \
    --settings "$WORK_DIR/settings.json" --strict-mcp-config \
    --permission-mode acceptEdits \
    --allowedTools "Bash" "Read" "Edit" "Write" "Grep" "Glob" "$@" \
    --disallowedTools "Bash(gh:*)" "Bash(git push:*)" \
    --add-dir ${AGENT_DIRS[@]+"${AGENT_DIRS[@]}"} "$WORK_DIR" \
    --output-format text </dev/null)
}

# Nothing the agent wrote runs unsealed: the project's cleanup command runs in a fresh export of the pinned base
# from the job's own clone, and the agent's clone is deleted without running git in it. Old runs go after 90 days.
cleanup_agent_run() { # ran-agent(0|1)
  if [ "$1" = 1 ] && [ -n "${CLEANUP_CMD:-}" ]; then
    if python3 "$KITCHEN_AUTOMATION/lib/export_tree.py" "$CLONE_DIR" "$BASE_SHA^{tree}" "$JOB_DIR/cleanup" >/dev/null; then
      (cd "$JOB_DIR/cleanup" && bash -c "$CLEANUP_CMD") > "$LOG_DIR/$AGENT_KIND-$RUN_ID-cleanup.log" 2>&1 ||
        log "cleanup FAILED: something the $AGENT_KIND started may still be running (log $LOG_DIR/$AGENT_KIND-$RUN_ID-cleanup.log)"
    else
      log "cleanup FAILED: could not export $BASE_SHA to run it"
    fi
  fi
  rm -rf "$AGENT_CLONE" "$JOB_DIR/prepare" "$JOB_DIR/cleanup" "$JOB_DIR/verify" "$JOB_DIR/publish.git"
  find "$LOG_DIR" -name "$AGENT_KIND-*" -mtime +90 -delete
  find "$STATE_DIR/$AGENT_KIND" "$STATE_DIR/$AGENT_KIND-job" -mindepth 1 -maxdepth 1 -mtime +90 -exec rm -rf {} + 2>/dev/null
}

# The job's own proof, independent of what the agent says it ran. The validated tree is written from the
# objects into $JOB_DIR/verify and each VERIFY_STEPS command runs there under srt with the agent's
# boundary: home denied except the configured tool and cache paths, writes only to the export, an exit-code
# dir and GARDENER_SANDBOX_WRITE, network only to GARDENER_SANDBOX_DOMAINS, and the agent's environment
# allowlist minus the claude login. The command runs in bash -e -o pipefail and records its exit code; srt
# reports a child killed by a signal as success, so a step passes only with srt's 0 AND a recorded 0.
# Sets VERIFY_REPORT for the PR body.
verify_tree() {
  local tree="$1" vdir="$JOB_DIR/verify" spec name cmd rc recorded var path keep=() args passed=""
  # The agent's own report is not evidence: without steps of its own the job publishes nothing.
  [ ${#VERIFY_STEPS[@]} -gt 0 ] || refuse "no independent verification configured ($VERIFY_STEPS_NAME is empty)" || return 1
  [ -z "$(malformed_steps "${VERIFY_STEPS[@]}")" ] || refuse "$VERIFY_STEPS_NAME has a malformed step" || return 1
  python3 "$KITCHEN_AUTOMATION/lib/export_tree.py" "$GIT_DIR" "$tree" "$vdir" >/dev/null ||
    refuse "could not export the validated tree" || return 1
  git config --file "$JOB_DIR/gitconfig" user.name "kitchen-$AGENT_KIND-verify"
  git config --file "$JOB_DIR/gitconfig" user.email "kitchen-$AGENT_KIND@localhost"
  # A git checkout of exactly that tree, for project checks that run git. add -f: a tracked file can match an
  # ignore rule. info/attributes overrides the tree's .gitattributes, so add stores the exported bytes as they
  # are (no eol or working-tree-encoding conversion, no filter). The comparison below proves the result.
  (cd "$vdir" && env -u GIT_DIR GIT_CONFIG_GLOBAL="$JOB_DIR/gitconfig" GIT_CONFIG_NOSYSTEM=1 sh -c \
    'git init -q -b kitchen-verify && echo "* -text -eol -working-tree-encoding -filter -ident" > .git/info/attributes &&
     git add -A -f && git -c core.hooksPath=/dev/null commit -q -m "kitchen: validated tree"') \
    >> "$JOB_DIR/verify.log" 2>&1 || refuse "could not make a git checkout of the validated tree" || return 1
  [ "$(git -C "$vdir" --git-dir="$vdir/.git" rev-parse 'HEAD^{tree}')" = "$tree" ] ||
    refuse "the verification checkout does not hold the validated tree" || return 1
  prepare_linux_checkout "$vdir"
  mkdir "$JOB_DIR/exit"
  args=(--format srt --read "$vdir" --write "$vdir" --write "$JOB_DIR/exit")
  for path in ${GARDENER_SANDBOX_READ[@]+"${GARDENER_SANDBOX_READ[@]}"} ${PLATFORM_READ[@]+"${PLATFORM_READ[@]}"}; do
    args+=(--read "$path")
  done
  for path in ${GARDENER_SANDBOX_WRITE[@]+"${GARDENER_SANDBOX_WRITE[@]}"} ${PLATFORM_WRITE[@]+"${PLATFORM_WRITE[@]}"}; do
    args+=(--read "$path" --write "$path")
  done
  for path in ${GARDENER_SANDBOX_DOMAINS[@]+"${GARDENER_SANDBOX_DOMAINS[@]}"}; do args+=(--domain "$path"); done
  for path in ${GARDENER_SANDBOX_UNIX_SOCKETS[@]+"${GARDENER_SANDBOX_UNIX_SOCKETS[@]}"}; do args+=(--socket "$path"); done
  python3 "$KITCHEN_AUTOMATION/lib/sandbox_settings.py" "${args[@]}" > "$JOB_DIR/verify-srt.json" ||
    refuse "could not write the verification sandbox settings" || return 1
  for var in HOME USER LOGNAME LANG TERM ${GARDENER_ENV_PASS[@]+"${GARDENER_ENV_PASS[@]}"} $(compgen -e | grep '^LC_' || true); do
    if [ -n "${!var+x}" ]; then keep+=("$var=${!var}"); fi
  done
  for spec in "${VERIFY_STEPS[@]}"; do
    name="${spec%%|*}"
    cmd="${spec#*|}"
    log "verify: $name"
    rm -f "$JOB_DIR/exit/rc"
    rc=0
    # The command and the exit file travel as variables: srt joins positional arguments without quoting.
    (cd "$vdir" && bounded "$VERIFY_TIMEOUT" env -i ${keep[@]+"${keep[@]}"} \
      PATH="$KITCHEN_AUTOMATION/shims/sealed:$TOOL_PATH" GIT_CONFIG_GLOBAL="$JOB_DIR/gitconfig" GIT_CONFIG_NOSYSTEM=1 \
      KITCHEN_STEP="$cmd" KITCHEN_STEP_EXIT="$JOB_DIR/exit/rc" \
      "$SRT" -s "$JOB_DIR/verify-srt.json" -c 'bash -e -o pipefail -c "$KITCHEN_STEP"; echo $? > "$KITCHEN_STEP_EXIT"') \
      >> "$JOB_DIR/verify.log" 2>&1 || rc=$?
    recorded="$(cat "$JOB_DIR/exit/rc" 2>/dev/null || echo none)"
    if [ "$rc" != 0 ] || [ "$recorded" != 0 ]; then
      tail -n 20 "$JOB_DIR/verify.log"
      refuse "job verification step $name failed (srt exit $rc, step exit $recorded; log $JOB_DIR/verify.log)"
      return 1
    fi
    passed+="- $name: passed"$'\n'
  done
  VERIFY_REPORT="Job verification: the job reran these steps on exactly this tree, sandboxed by srt $("$SRT" --version):"$'\n'"$passed"
}

# Deterministic publication of what the agent committed, from the base BASE_SHA. Returns 1 when it refuses or
# fails; sets PUBLISHED_URL when it opened the PR, and leaves it empty when there was no branch to publish.
# Every check and the published commit use SHAs pinned once (BASE_SHA before Claude ran, the branch's
# commit and tree right here), never ref names: a leftover agent process could move a ref mid-publish.
# Git runs with hooks off: the working tree (where project hooks live) was writable by the agent.
publish() {
  local listing br head tree ahead added deleted path pattern allowed changed=0 title
  PUBLISHED_URL=""
  # for-each-ref only reads refs: no hook, filter or fsmonitor of the agent's clone can run.
  listing="$(git --git-dir="$AGENT_CLONE/.git" for-each-ref --format='%(objectname) %(refname:short)' "refs/heads/$AGENT_KIND/")" ||
    refuse "cannot list the branches of the agent's clone" || return 1
  [ -n "$listing" ] || return 0
  [ "$(printf '%s\n' "$listing" | wc -l | tr -d ' ')" = 1 ] || refuse "more than one $AGENT_KIND branch: $(echo $listing)" || return 1
  head="${listing%% *}"
  br="${listing#* }"
  [[ "$head" =~ ^[0-9a-f]{40}$ ]] || refuse "unexpected object name for $br" || return 1
  # The job's own bare repository: the agent's objects are copied in (alternates, repack, then the alternates
  # are dropped) and every object is hashed again by fsck, so what is checked is what gets pushed.
  git init --quiet --bare "$GIT_DIR" || refuse "cannot create $GIT_DIR" || return 1
  printf '%s\n' "$AGENT_CLONE/.git/objects" "$CLONE_DIR/.git/objects" > "$GIT_DIR/objects/info/alternates"
  git update-ref refs/kitchen/agent "$head" && git update-ref refs/kitchen/base "$BASE_SHA" &&
    git repack --quiet -a -d && rm "$GIT_DIR/objects/info/alternates" && git fsck --no-dangling --no-progress ||
    refuse "could not import the agent's objects for $br" || return 1
  git remote add origin "$REPO_URL"
  tree="$(git rev-parse --verify --quiet "$head^{tree}")" || refuse "cannot read the tree of $br" || return 1
  local name_re="^$AGENT_KIND/${RUN_DATE}-[a-z0-9][a-z0-9-]*$"
  [[ "$br" =~ $name_re ]] || refuse "branch $br is not named $AGENT_KIND/$RUN_DATE-<slug>" || return 1
  git merge-base --is-ancestor "$BASE_SHA" "$head" || refuse "$br does not start from the job's base ${BASE_SHA:0:12}" || return 1
  ahead="$(git rev-list --count "$BASE_SHA..$head")"
  [ "$ahead" -ge 1 ] || refuse "$br has no commits ahead of its base" || return 1
  while IFS=$'\t' read -r -d '' added deleted path; do
    [ "$added" = - ] && { refuse "binary change in $path"; return 1; }
    changed=$((changed + added + deleted))
    for pattern in ${PROTECTED_PATHS[@]+"${PROTECTED_PATHS[@]}"}; do
      # shellcheck disable=SC2254
      case "$path" in $pattern) refuse "$br touches protected path $path ($pattern)"; return 1 ;; esac
    done
    if [ ${#EDIT_PATHS[@]} -gt 0 ]; then
      allowed=0
      for pattern in "${EDIT_PATHS[@]}"; do
        # shellcheck disable=SC2254
        case "$path" in $pattern) allowed=1 ;; esac
      done
      [ "$allowed" = 1 ] || { refuse "$br touches $path, outside the paths it may edit (${EDIT_PATHS[*]})"; return 1; }
    fi
  done < <(git diff --no-renames --numstat -z "$BASE_SHA" "$tree")
  [ "$changed" -le "$MAX_CHANGED_LINES" ] || refuse "$br changes $changed lines; the bound is $MAX_CHANGED_LINES" || return 1
  if [ "${PROSE_ONLY:-0}" = 1 ]; then
    local code
    code="$(python3 "$KITCHEN_AUTOMATION/lib/prose_only.py" "$GIT_DIR" "$BASE_SHA" "$tree")" ||
      { refuse "$br changes more than prose: $(printf '%s' "$code" | tr '\n' ';' | cut -c1-300)"; return 1; }
  fi
  # Second layer, independent of the sandbox: no credential-looking content in what would be published.
  local secrets
  secrets="$(git diff -U0 --no-color "$BASE_SHA" "$tree" | python3 "$KITCHEN_AUTOMATION/lib/scan_diff.py")" ||
    { refuse "possible secret in the diff: $(printf '%s' "$secrets" | tr '\n' ';' | cut -c1-300)"; return 1; }
  [ -s "$SUMMARY_FILE" ] || refuse "no PR summary at $SUMMARY_FILE" || return 1
  local summary="$JOB_DIR/summary.md"
  copy_agent_file "$SUMMARY_FILE" "$summary" || refuse "the PR summary at $SUMMARY_FILE is not a regular file of its own" || return 1
  title="$(grep -m1 -v '^[[:space:]]*$' "$summary" | sed -E 's/^#+[[:space:]]*//' | cut -c1-120 | redact)" || refuse "could not read the PR title" || return 1
  [ -n "$title" ] || refuse "empty PR title in $SUMMARY_FILE" || return 1
  awk 'seen { print; next } /[^[:space:]]/ { seen = 1 }' "$summary" | redact > "$JOB_DIR/body.md" ||
    refuse "could not redact the PR summary" || return 1
  verify_tree "$tree" || return 1
  printf '\n---\n%s\n' "$VERIFY_REPORT" >> "$JOB_DIR/body.md"

  # One commit: the validated tree, on the pinned base, with the agent's messages (redacted) and author.
  local commit
  if [ "$ahead" = 1 ]; then
    git log -1 --format=%B "$head" | redact > "$JOB_DIR/message.txt"
  else
    { printf '%s\n\n' "$title"; git log --reverse --format='* %B' "$BASE_SHA..$head"; } | redact > "$JOB_DIR/message.txt"
  fi || refuse "could not build the commit message" || return 1
  commit="$(GIT_AUTHOR_NAME="$(git log -1 --format=%an "$head")" GIT_AUTHOR_EMAIL="$(git log -1 --format=%ae "$head")" \
    git commit-tree "$tree" -p "$BASE_SHA" -F "$JOB_DIR/message.txt")" || refuse "could not build the commit" || return 1
  [ "$(git rev-parse "$commit^{tree}")" = "$tree" ] || refuse "the built commit does not hold the validated tree" || return 1

  # The refspec is built here and can only name refs/heads/<kind>/...; no hook from the tree runs.
  bounded "$NET_TIMEOUT" git -c core.hooksPath=/dev/null push --quiet origin "$commit:refs/heads/$br" ||
    refuse "push of $br failed" || return 1
  PUBLISHED_URL="$(gh pr create -R "$GH_REPO" --base "$BRANCH" --head "$br" --label "$PUBLISH_LABEL" \
    --title "$title" --body-file "$JOB_DIR/body.md")" || refuse "gh pr create failed for pushed branch $br" || return 1
  log "$RESULT_TAG: $PUBLISHED_URL ($changed changed lines in one commit, squashed from $ahead)"
}
