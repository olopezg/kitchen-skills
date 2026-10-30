import datetime
import fcntl
import json
import os
import platform
import random
import re
import shutil
import string
import signal
import subprocess
import sys
import tempfile
import time
import textwrap
import unittest
from pathlib import Path

AUTOMATION = Path(__file__).resolve().parent.parent / "automation"
GIT_ENV = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}


def clean_env(**extra):
    """os.environ without GIT_* (a git hook exports GIT_DIR and GIT_INDEX_FILE), plus a fixed identity."""
    return {**{k: v for k, v in os.environ.items() if not k.startswith("GIT_")}, **GIT_ENV, **extra}


class AutomationFixture(unittest.TestCase):
    """A local origin repo, fake external tools, and throwaway config and state."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.origin, self.bin, self.config, self.state = root / "origin", root / "fakebin", root / "config", root / "state"
        self.calls = root / "calls.log"
        for folder in (self.origin, self.bin, self.config / "automation"):
            folder.mkdir(parents=True)
        env = clean_env()
        for args in (["init", "-q", "-b", "dev"], ["commit", "-q", "--allow-empty", "-m", "init"]):
            subprocess.run(["git", "-C", str(self.origin), *args], check=True, env=env)
        self.fake("gh", 'echo "gh $*" >> "$CALLS"; [ "${GH_FAIL:-0}" = 1 ] && exit 1\n'
                        'case "$1 $2" in "pr list") echo "${OPEN_PRS:-0}";; "pr create") echo "https://example.test/pull/1";;\n'
                        '  "issue list") echo "${OPEN_ISSUE:-}";; "issue create") echo "https://example.test/issues/7";; esac')
        self.fake("claude", 'echo "claude" >> "$CALLS"; exit "${CLAUDE_EXIT:-0}"')
        for notifier in ("osascript", "notify-send"):  # a test never pops a real notification
            self.fake(notifier, 'echo "notify $*" >> "$CALLS"')

    def tearDown(self):
        self.tmp.cleanup()

    def fake(self, name, body):
        path = self.bin / name
        path.write_text(f"#!/usr/bin/env bash\n{body}\n")
        path.chmod(0o755)

    def configure(self, steps, cleanup="true", metrics="", needs_docker=0, project="shop", extra=""):
        step_lines = "\n".join(f'  "{s}"' for s in steps or [])
        (self.config / "automation" / f"{project}.env").write_text(textwrap.dedent(f"""\
            REPO_URL="{self.origin}"
            GH_REPO="owner/shop"
            BRANCH="dev"
            EXTRA_PATH="{self.bin}"
            NEEDS_DOCKER={needs_docker}
            GUARD_LABEL="nightly-guard"
            GARDENER_LABEL="gardener"
            CLEANUP_CMD="{cleanup}"
            METRICS_CMD="{metrics}"
            GARDENER_ENV_PASS=(CALLS CLAUDE_EXIT)
            """) + extra + ("" if steps is None else f"\nGUARD_STEPS=(\n{step_lines}\n)\n"))

    def verify_config(self, steps, extra="", guard_steps=(), **options):
        """srt is a node script: the job finds node only on EXTRA_PATH, as under launchd."""
        node = Path(shutil.which("node")).parent
        self.configure(list(guard_steps), extra=f'EXTRA_PATH="{self.bin}:{node}"\nGARDENER_VERIFY_STEPS=({steps})\n' + extra, **options)

    def configure_publishing(self, extra="", **options):
        """A config the jobs can pass with: neither the gardener nor the guard is green without steps of its own."""
        self.verify_config('"ok|true"', extra, guard_steps=["ok|true"], **options)

    def run_job(self, job, *args, **env):
        full_env = {**clean_env(), "KITCHEN_CONFIG": str(self.config), "KITCHEN_STATE": str(self.state),
                    "KITCHEN_REAL_GH": str(self.bin / "gh"), "CALLS": str(self.calls), "DOCKER_WAIT_TRIES": "1", "DOCKER_WAIT_SECONDS": "0", **env}
        return subprocess.run(["bash", str(AUTOMATION / "bin" / job), *args], env=full_env, capture_output=True, text=True)

    def job_env(self, **env):
        return {**clean_env(), "KITCHEN_CONFIG": str(self.config), "KITCHEN_STATE": str(self.state),
                "KITCHEN_REAL_GH": str(self.bin / "gh"), "CALLS": str(self.calls), **env}

    def start_job(self, job, *args, **env):
        return subprocess.Popen(["bash", str(AUTOMATION / "bin" / job), *args], env=self.job_env(**env),
                                start_new_session=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    def wait_for(self, path, seconds=10):
        end = time.monotonic() + seconds
        while not (path.exists() and path.read_text().strip()) and time.monotonic() < end:
            time.sleep(0.05)
        self.assertTrue(path.exists(), f"{path} never appeared")
        return path.read_text().strip()

    def nightly(self, project="shop"):
        return [json.loads(line) for line in (self.state / "nightly" / f"{project}.jsonl").read_text().splitlines()]

    def calls_log(self):
        return self.calls.read_text() if self.calls.exists() else ""

    def notifications(self):
        return [line for line in self.calls_log().splitlines() if line.startswith("notify ")]

    def gardener(self, project="shop"):
        return [json.loads(line) for line in (self.state / "gardener" / f"{project}.jsonl").read_text().splitlines()]

    def git(self, repo, *args):
        return subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True,
                              env=clean_env()).stdout.strip()

    def origin_branches(self):
        return self.git(self.origin, "for-each-ref", "--format=%(refname:short)", "refs/heads/").splitlines()

    def write_script(self, name, body):
        path = Path(self.tmp.name) / name
        path.write_text(textwrap.dedent(body))
        return path

    def assert_dead(self, pid):
        end = time.monotonic() + 5
        while time.monotonic() < end:
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                return
            time.sleep(0.05)
        self.fail(f"process {pid} is still running")

    def commit_to_origin(self, files):
        for name, (text, mode) in files.items():
            path = self.origin / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text)
            path.chmod(mode)
        self.git(self.origin, "add", "-A")
        self.git(self.origin, "commit", "-q", "-m", "files")


class NightlyGuardTests(AutomationFixture):
    def test_red_run_stops_at_the_first_failure_reports_it_and_records_it(self):
        marker = Path(self.tmp.name) / "never-ran"
        self.configure(["ok|true", "bad|false", f"after|touch {marker}"])

        result = self.run_job("nightly-guard", "shop")

        run = self.nightly()[-1]
        self.assertEqual(result.returncode, 1, result.stdout)
        self.assertEqual((run["status"], run["failed_step"]), ("red", "bad"))
        self.assertFalse(marker.exists())
        self.assertIn("gh issue create", self.calls_log())

    def test_a_run_without_steps_is_a_configuration_failure_never_green(self):
        for name, steps in (("empty", []), ("unset", None)):
            with self.subTest(name):
                self.configure(steps)

                result = self.run_job("nightly-guard", "shop", GUARD_NO_REPORT="1")

                run = self.nightly()[-1]
                self.assertNotEqual(result.returncode, 0, result.stdout)
                self.assertEqual((run["status"], run["failed_step"]), ("incomplete", "config"))
                self.assertIn("no guard step ran", result.stdout)

    def test_an_empty_or_malformed_step_is_a_configuration_failure_never_green(self):
        # `bash -c ""` exits 0: a step without a command would pass without checking anything
        for name, steps in (("empty command", ["build|"]), ("blank command", ["build|   "]), ("no name", ["| true"]),
                            ("empty entry", [""]), ("no separator", ["true"]), ("one bad among good", ["ok|true", "lint|"])):
            with self.subTest(name):
                self.configure(steps)

                result = self.run_job("nightly-guard", "shop", GUARD_NO_REPORT="1")

                run = self.nightly()[-1]
                self.assertNotEqual(result.returncode, 0, result.stdout)
                self.assertEqual((run["status"], run["failed_step"]), ("incomplete", "config"))
                self.assertIn("not name|command with both parts", result.stdout)

    def test_each_step_runs_strict_so_a_hidden_failure_fails_it(self):
        for name, command in (("chained", "false; true"), ("piped", "false | true")):
            with self.subTest(name):
                self.configure([f"{name}|{command}"])

                result = self.run_job("nightly-guard", "shop", GUARD_NO_REPORT="1")

                run = self.nightly()[-1]
                self.assertEqual(result.returncode, 1, result.stdout)
                self.assertEqual((run["status"], run["failed_step"]), ("red", name))

    def test_red_or_incomplete_run_notifies_and_a_green_run_stays_silent(self):
        sent = []
        for steps in (["ok|true"], ["bad|false"], []):
            self.configure(steps)
            self.run_job("nightly-guard", "shop", GUARD_NO_REPORT="1")
            sent.append(self.notifications()[sum(map(len, sent)):])

        self.assertEqual([len(batch) for batch in sent], [0, 1, 1], sent)
        self.assertIn("kitchen nightly: shop red", sent[1][0])
        self.assertIn("kitchen nightly: shop incomplete", sent[2][0])

    def test_green_run_records_metrics_and_cleanup(self):
        self.configure(["ok|true"], metrics="echo '{\\\"tests\\\": 3}'")

        result = self.run_job("nightly-guard", "shop", GUARD_NO_REPORT="1")

        run = self.nightly()[-1]
        history = (self.state / "automation" / "shop" / "history" / "history.jsonl").read_text()
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertEqual((run["status"], run["cleanup"], run["metrics"]), ("green", "ok", "ok"))
        self.assertEqual(json.loads(history)["metrics"], {"tests": 3})

    def test_failed_cleanup_and_metrics_are_recorded_not_hidden(self):
        self.configure(["ok|true"], cleanup="false", metrics="exit 3")

        result = self.run_job("nightly-guard", "shop", GUARD_NO_REPORT="1")

        run = self.nightly()[-1]
        self.assertEqual((run["cleanup"], run["metrics"]), ("failed", "failed"))
        self.assertIn("cleanup FAILED", result.stdout)
        self.assertIn("metrics FAILED", result.stdout)

    def test_green_verdict_keeps_cleanup_and_metrics_failures_as_warnings(self):
        self.configure(["ok|true"], cleanup="false", metrics="exit 3")

        self.run_job("nightly-guard", "shop", GUARD_NO_REPORT="1")

        run = self.nightly()[-1]
        self.assertEqual(run["status"], "green")
        self.assertEqual(run["warnings"], ["cleanup failed", "metrics failed"])

    def test_failing_issue_report_is_a_warning_and_the_run_still_cleans_and_records(self):
        cleaned = Path(self.tmp.name) / "cleaned"
        self.configure(["bad|false"], cleanup=f"touch {cleaned}")

        result = self.run_job("nightly-guard", "shop", GH_FAIL="1")

        runs = self.nightly()
        self.assertEqual(result.returncode, 1, result.stdout)
        self.assertEqual(len(runs), 1)
        self.assertEqual((runs[0]["status"], runs[0]["failed_step"]), ("red", "bad"))
        self.assertIn("issue report failed", runs[0]["warnings"])
        self.assertTrue(cleaned.exists())

    def test_failing_green_report_is_a_warning(self):
        self.configure(["ok|true"])

        result = self.run_job("nightly-guard", "shop", GH_FAIL="1")

        run = self.nightly()[-1]
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertEqual((run["status"], run["warnings"]), ("green", ["issue report failed"]))

    def test_metrics_history_is_mirrored_to_the_gardener_host_after_a_recorded_run(self):
        self.fake("rsync", 'echo "rsync $*" >> "$CALLS"')
        self.configure(["ok|true"], metrics="echo '{\\\"tests\\\": 3}'", extra='HISTORY_MIRROR="gardener-host:state/history/"\n')

        result = self.run_job("nightly-guard", "shop", GUARD_NO_REPORT="1")

        history = self.state / "automation" / "shop" / "history" / "history.jsonl"
        rsync = [line for line in self.calls_log().splitlines() if line.startswith("rsync ")]
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertEqual(len(rsync), 1, self.calls_log())
        self.assertTrue(rsync[0].endswith(f"-- {history} gardener-host:state/history/"), rsync[0])
        self.assertNotIn("warnings", {k for k, v in self.nightly()[-1].items() if v})

    def test_a_failed_history_mirror_is_a_warning_on_a_green_run(self):
        self.fake("rsync", "exit 12")
        self.configure(["ok|true"], metrics="echo '{}'", extra='HISTORY_MIRROR="gardener-host:state/history/"\n')

        result = self.run_job("nightly-guard", "shop", GUARD_NO_REPORT="1")

        run = self.nightly()[-1]
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertEqual((run["status"], run["warnings"]), ("green", ["history mirror failed"]))

    def test_a_hung_history_mirror_is_cut_off_and_the_run_is_still_recorded(self):
        self.fake("rsync", "sleep 30")
        self.configure(["ok|true"], metrics="echo '{}'", extra='HISTORY_MIRROR="gardener-host:state/history/"\n')

        started = time.monotonic()
        result = self.run_job("nightly-guard", "shop", GUARD_NO_REPORT="1", HISTORY_MIRROR_SECONDS="2")

        run = self.nightly()[-1]
        self.assertLess(time.monotonic() - started, 25, "the mirror was not cut off")
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertEqual((run["status"], run["warnings"]), ("green", ["history mirror failed"]))

    def test_a_mirror_destination_that_is_not_host_path_is_refused_not_passed_to_rsync(self):
        self.fake("rsync", 'echo "rsync $*" >> "$CALLS"')
        self.configure(["ok|true"], metrics="echo '{}'", extra='HISTORY_MIRROR="--help"\n')

        result = self.run_job("nightly-guard", "shop", GUARD_NO_REPORT="1")

        run = self.nightly()[-1]
        self.assertNotIn("rsync", self.calls_log())
        self.assertIn("is not host:path", result.stdout)
        self.assertEqual(run["warnings"], ["history mirror failed"])

    def test_no_mirror_when_the_metrics_were_not_recorded(self):
        self.fake("rsync", 'echo "rsync $*" >> "$CALLS"')
        self.configure(["ok|true"], metrics="exit 3", extra='HISTORY_MIRROR="gardener-host:state/history/"\n')

        self.run_job("nightly-guard", "shop", GUARD_NO_REPORT="1")

        self.assertNotIn("rsync", self.calls_log())

    def test_invalid_metrics_json_is_recorded_not_fatal(self):
        self.configure(["ok|true"], metrics="echo not-json")

        result = self.run_job("nightly-guard", "shop", GUARD_NO_REPORT="1")

        run = self.nightly()[-1]
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertEqual((run["status"], run["metrics"], run["warnings"]), ("green", "failed", ["metrics failed"]))

    def test_interrupted_run_is_cleaned_and_recorded_as_incomplete(self):
        cleaned = Path(self.tmp.name) / "cleaned"
        self.configure(["ok|true", "slow|kill -TERM \\$PPID", "after|true"], cleanup=f"touch {cleaned}")

        result = self.run_job("nightly-guard", "shop", GUARD_NO_REPORT="1")

        runs = self.nightly()
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(len(runs), 1)
        self.assertEqual((runs[0]["status"], runs[0]["failed_step"]), ("incomplete", "slow"))
        self.assertTrue(cleaned.exists())

    def test_record_carries_the_full_sha(self):
        self.configure(["ok|true"])

        self.run_job("nightly-guard", "shop", GUARD_NO_REPORT="1")

        self.assertEqual(self.nightly()[-1]["sha"], self.git(self.origin, "rev-parse", "HEAD"))

    def test_issue_body_is_redacted(self):
        token = "ghp_" + "a" * 30  # built at runtime: the repo never holds a credential shape
        assignment = "pass" + "word=" + "hunter2" * 2
        self.configure([f"leak|echo {assignment}; echo {token}; exit 1"])

        self.run_job("nightly-guard", "shop")

        line = next(line for line in self.calls_log().splitlines() if line.startswith("gh issue create"))
        body = Path(line.split("--body-file ")[1].split()[0]).read_text()
        self.assertIn("[REDACTED]", body)
        self.assertNotIn("hunter2hunter2", body)
        self.assertNotIn(token, body)

    def test_project_hooks_keep_working_next_to_the_push_guard(self):
        marks = Path(self.tmp.name)
        self.commit_to_origin({
            "scripts/hooks/pre-commit": (f"#!/bin/sh\ntouch {marks}/pre-commit-ran\n", 0o755),
            "scripts/hooks/pre-push": (f"#!/bin/sh\ncat > {marks}/pre-push-input\n", 0o755),
        })
        self.configure(["hooks|git commit -q --allow-empty -m x && git push -q origin HEAD:refs/heads/kitchen-ok"
                        " && ! git push -q origin HEAD:refs/heads/main"], extra='HOOKS_PATH="scripts/hooks"')

        result = self.run_job("nightly-guard", "shop", GUARD_NO_REPORT="1")

        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertTrue((marks / "pre-commit-ran").exists())
        self.assertIn("refs/heads/kitchen-ok", (marks / "pre-push-input").read_text())
        self.assertIn("kitchen-ok", self.origin_branches())
        self.assertNotIn("main", self.origin_branches())

    def test_hooks_path_set_by_the_project_survives_repeated_installs(self):
        marks = Path(self.tmp.name)
        self.commit_to_origin({"scripts/hooks/pre-commit": (f"#!/bin/sh\ntouch {marks}/pre-commit-ran\n", 0o755)})
        self.configure(["setup|git config core.hooksPath scripts/hooks"])
        self.run_job("nightly-guard", "shop", GUARD_NO_REPORT="1")
        self.configure(["noop|true"])
        self.run_job("nightly-guard", "shop", GUARD_NO_REPORT="1")  # a second install must keep the project's hooks
        self.configure(["commit|git commit -q --allow-empty -m x"])

        result = self.run_job("nightly-guard", "shop", GUARD_NO_REPORT="1")

        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertTrue((marks / "pre-commit-ran").exists())

    def test_inherited_git_dir_never_redirects_the_job(self):
        decoy = Path(self.tmp.name) / "decoy"
        self.git(self.origin, "clone", "-q", str(self.origin), str(decoy))
        before = self.git(decoy, "rev-parse", "HEAD")
        self.configure(["commit|git commit -q --allow-empty -m from-the-guard"])

        result = self.run_job("nightly-guard", "shop", GUARD_NO_REPORT="1",
                              GIT_DIR=str(decoy / ".git"), GIT_INDEX_FILE=str(decoy / ".git" / "index"))

        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertEqual(self.git(decoy, "rev-parse", "HEAD"), before)
        self.assertEqual(self.git(decoy, "config", "--get", "core.bare"), "false")

    def test_sync_runs_no_hook_from_a_tree_left_dirty(self):
        evil = Path(self.tmp.name) / "hook-ran"
        self.commit_to_origin({"scripts/hooks/reference-transaction": ("#!/bin/sh\nexit 0\n", 0o755)})
        self.configure(["ok|true"], extra='HOOKS_PATH="scripts/hooks"')
        self.run_job("nightly-guard", "shop", GUARD_NO_REPORT="1")
        # as if a gardener run had been killed before restoring the tree its agent wrote
        hook = self.state / "automation" / "shop" / "clone" / "scripts" / "hooks" / "reference-transaction"
        hook.write_text(f"#!/bin/sh\ntouch {evil}\n")
        self.commit_to_origin({"later.txt": ("x\n", 0o644)})  # the next fetch updates refs

        result = self.run_job("nightly-guard", "shop", GUARD_NO_REPORT="1")

        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertFalse(evil.exists())

    def test_busy_project_lock_skips_the_run_and_only_logs_it(self):
        marker = Path(self.tmp.name) / "ran"
        self.configure([f"step|touch {marker}"])
        lock = self.state / "automation" / "shop" / "job.lock"
        lock.parent.mkdir(parents=True)

        with open(lock, "w") as held:
            fcntl.flock(held, fcntl.LOCK_EX)
            result = self.run_job("nightly-guard", "shop", GUARD_NO_REPORT="1", LOCK_WAIT_SECONDS="1", LOCK_POLL_SECONDS="0.1")

        self.assertEqual(result.returncode, 75, result.stdout)
        self.assertIn("guard skipped: busy", result.stdout)
        self.assertFalse((self.state / "nightly" / "shop.jsonl").exists())
        self.assertFalse(marker.exists())

    def test_a_busy_skip_never_rewrites_the_record_of_the_run_holding_the_lock(self):
        self.configure(["ok|true"])
        record = self.state / "nightly" / "shop.jsonl"
        record.parent.mkdir(parents=True)
        active = {"ts": "2026-10-05T02:00:00+00:00", "status": "running", "failed_step": None, "run_id": "active"}
        record.write_text(json.dumps(active) + "\n")
        lock = self.state / "automation" / "shop" / "job.lock"
        lock.parent.mkdir(parents=True)

        with open(lock, "w") as held:
            fcntl.flock(held, fcntl.LOCK_EX)
            result = self.run_job("nightly-guard", "shop", GUARD_NO_REPORT="1", LOCK_WAIT_SECONDS="1", LOCK_POLL_SECONDS="0.1")

        self.assertEqual(result.returncode, 75, result.stdout)
        self.assertEqual(self.nightly(), [active])

    def test_missing_docker_fails_the_run(self):
        self.configure(["ok|true"], needs_docker=1)
        self.fake("docker", "exit 1")
        self.fake("open", "exit 1")

        result = self.run_job("nightly-guard", "shop", GUARD_NO_REPORT="1")

        run = self.nightly()[-1]
        self.assertEqual(result.returncode, 1)
        self.assertEqual((run["status"], run["failed_step"]), ("red", "docker"))


    def test_the_record_lists_each_finished_step_with_its_seconds_on_every_path(self):
        self.configure(["fast|true", "slow|sleep 2", "bad|false", "after|true"])

        self.run_job("nightly-guard", "shop", GUARD_NO_REPORT="1")

        steps = self.nightly()[-1]["steps"]
        self.assertEqual([(s["name"], s["status"]) for s in steps], [("fast", "green"), ("slow", "green"), ("bad", "red")])
        self.assertTrue(2 <= steps[1]["seconds"] <= 10, steps)
        self.assertTrue(all(isinstance(s["seconds"], int) for s in steps), steps)

        self.configure(["ok|true", "killed|kill -TERM \\$PPID", "after|true"])  # interrupted: only what finished

        self.run_job("nightly-guard", "shop", GUARD_NO_REPORT="1")

        run = self.nightly()[-1]
        self.assertEqual(run["status"], "incomplete")
        self.assertEqual([(s["name"], s["status"]) for s in run["steps"]], [("ok", "green")])

    def budget_gh(self):
        """A gh whose budget issue (label nightly-guard-budget) is #7 when BUDGET_ISSUE=7; every call is logged."""
        self.fake("gh", 'echo "gh $*" >> "$CALLS"; [ "${GH_FAIL:-0}" = 1 ] && exit 1\n'
                        'case "$*" in "issue list"*"--label nightly-guard-budget"*) echo "${BUDGET_ISSUE:-}";;\n'
                        '  "issue create"*) echo "https://example.test/issues/8";; esac')

    def budget_calls(self):
        return [line for line in self.calls_log().splitlines() if "budget" in line]

    def test_a_step_over_its_budget_warns_and_opens_one_budget_issue_never_changing_the_verdict(self):
        self.budget_gh()
        budgets = 'GUARD_STEP_BUDGETS=("slow|1" "fast|60" "ghost|5" "broken" "fast|010" "fast|")\n'
        self.configure(["fast|true", "slow|sleep 2"], extra=budgets)

        result = self.run_job("nightly-guard", "shop")

        run = self.nightly()[-1]
        self.assertEqual((result.returncode, run["status"]), (0, "green"), result.stdout)
        self.assertEqual([w for w in run["warnings"] if not w.startswith("over budget")],
                         ["step budget for ghost, which is not a step", "malformed step budget: broken (want name|seconds)",
                          "malformed step budget: fast|010 (want name|seconds)", "malformed step budget: fast| (want name|seconds)"])
        over = [w for w in run["warnings"] if w.startswith("over budget")]
        self.assertEqual(len(over), 1, run["warnings"])
        self.assertRegex(over[0], r"^over budget: slow ([2-9]|10)s > 1s$")
        created = [line for line in self.budget_calls() if line.startswith("gh issue create")]
        self.assertEqual(len(created), 1, self.calls_log())
        self.assertIn("--title Nightly guard over its time budget on dev --label nightly-guard-budget", created[0])
        body = Path(created[0].split("--body-file ")[1].split()[0]).read_text()
        self.assertRegex(body, r"\n- slow ([2-9]|10)s > 1s\n")  # the body lists the step over budget

        self.calls.unlink()
        self.run_job("nightly-guard", "shop", BUDGET_ISSUE="7")  # still over: one comment on the open issue

        self.assertTrue(any(line.startswith("gh issue comment 7") for line in self.calls_log().splitlines()), self.calls_log())
        self.assertFalse(any(line.startswith("gh issue create") for line in self.calls_log().splitlines()), self.calls_log())

        self.calls.unlink()
        self.configure(["fast|true", "slow|sleep 2", "bad|false"], extra=budgets)
        self.run_job("nightly-guard", "shop", BUDGET_ISSUE="7")  # red: the budget issue is left alone

        self.assertEqual(self.nightly()[-1]["status"], "red")
        self.assertTrue(any(w.startswith("over budget: slow") for w in self.nightly()[-1]["warnings"]))
        self.assertEqual(self.budget_calls(), [])

        self.configure(["fast|true", "slow|sleep 2"], extra='GUARD_STEP_BUDGETS=("slow|60")\n')
        self.run_job("nightly-guard", "shop", BUDGET_ISSUE="7")  # green within every budget: closed

        self.assertTrue(any(line.startswith("gh issue close 7") for line in self.calls_log().splitlines()), self.calls_log())
        self.assertFalse(any(w.startswith("over budget") for w in self.nightly()[-1]["warnings"]))

    def test_a_failed_budget_issue_call_is_a_warning(self):
        self.budget_gh()
        self.configure(["slow|sleep 2"], extra='GUARD_STEP_BUDGETS=("slow|1")\n')

        result = self.run_job("nightly-guard", "shop", GH_FAIL="1")

        run = self.nightly()[-1]
        self.assertEqual((result.returncode, run["status"]), (0, "green"), result.stdout)
        self.assertIn("budget issue report failed", run["warnings"])

    def test_status_shows_the_steps_and_the_over_budget_warning_of_the_guards_record(self):
        home = Path(self.tmp.name) / "home"
        checkout = Path(self.tmp.name) / "shop"
        home.mkdir()
        subprocess.run(["git", "clone", "-q", str(self.origin), str(checkout)], check=True, env=clean_env())
        (self.config / "projects.txt").write_text(f"{checkout}\n")
        self.configure(["slow|sleep 2"], extra='GUARD_STEP_BUDGETS=("slow|1")\n')
        self.run_job("nightly-guard", "shop", GUARD_NO_REPORT="1")
        self.fake("gh", "echo '[]'")  # status lists the project's pull requests
        env = {**clean_env(), "HOME": str(home), "KITCHEN_CONFIG": str(self.config), "KITCHEN_STATE": str(self.state),
               "KITCHEN_DENYLIST": str(home / "no-denylist.txt"), "PATH": f"{self.bin}{os.pathsep}{os.environ['PATH']}"}
        kitchen = AUTOMATION.parent / "bin" / "kitchen"

        shown = subprocess.run([sys.executable, str(kitchen), "status", str(checkout)], env=env, capture_output=True, text=True)
        report = subprocess.run([sys.executable, str(kitchen), "status", "--json", str(checkout)], env=env, capture_output=True, text=True)

        self.assertRegex(shown.stdout, r"nightly .* warnings: over budget: slow \d+s > 1s", shown.stdout + shown.stderr)
        steps = json.loads(report.stdout)["projects"][0]["nightly"]["steps"]
        self.assertEqual([(s["name"], s["status"]) for s in steps], [("slow", "green")])

    def evidence(self):
        return self.state / "automation" / "shop" / "evidence"

    def test_each_cited_step_keeps_its_own_evidence_and_only_the_newest_runs_keep_theirs(self):
        write = "mkdir -p .verify/evidence && echo {} > .verify/evidence/ledger.jsonl"
        self.configure([f"prove|{write.format('first')}", f"prove-again|{write.format('second')}", "other|true"],
                       extra='AUDIT_GUARD_STEPS=(prove prove-again)\nAUDIT_EVIDENCE_PATHS=(".verify/evidence/" "not/there")\n')

        result = self.run_job("nightly-guard", "shop", GUARD_NO_REPORT="1")

        run = self.nightly()[-1]
        self.assertEqual(run["status"], "green", result.stdout)
        kept = self.evidence() / run["run_id"]
        self.assertEqual(sorted(p.name for p in kept.iterdir()), ["prove", "prove-again"])
        self.assertEqual((kept / "prove" / ".verify" / "evidence" / "ledger.jsonl").read_text(), "first\n")
        self.assertEqual((kept / "prove-again" / ".verify" / "evidence" / "ledger.jsonl").read_text(), "second\n")
        self.assertEqual([w for w in run["warnings"] if w.startswith("evidence")],
                         ["evidence missing after prove: not/there", "evidence missing after prove-again: not/there"])

        for _ in range(4):
            self.run_job("nightly-guard", "shop", GUARD_NO_REPORT="1")
        self.run_job("nightly-guard", "shop", GUARD_ONLY="prove")  # a trial run keeps nothing and evicts nothing

        newest = [r["run_id"] for r in self.nightly()[-3:]]
        self.assertEqual(sorted(p.name for p in self.evidence().iterdir()), sorted(newest))

    def test_trial_run_is_neither_recorded_nor_reported(self):
        self.configure(["build|true", "tests|true"], metrics="echo '{}'")

        result = self.run_job("nightly-guard", "shop", GUARD_ONLY="build")

        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertFalse((self.state / "nightly" / "shop.jsonl").exists())
        self.assertFalse((self.state / "automation" / "shop" / "history" / "history.jsonl").exists())
        self.assertNotIn("gh issue", self.calls_log())


    def background_worker_step(self):
        """A step whose shell records its job's pid and leaves a worker appending to a file."""
        tmp = Path(self.tmp.name)
        files = {"job": tmp / "job.pid", "worker": tmp / "worker.pid", "out": tmp / "worker.out"}
        step = (f"slow|echo \\$PPID > {files['job']}; (while :; do date >> {files['out']}; sleep 0.1; done) & "
                f"echo \\$! > {files['worker']}; sleep 30")
        return step, files

    def test_killing_the_job_stops_its_workers_before_the_lock_is_released(self):
        step, files = self.background_worker_step()
        self.configure([step])
        first = self.start_job("nightly-guard", "shop", GUARD_NO_REPORT="1")
        try:
            job, worker = int(self.wait_for(files["job"])), int(self.wait_for(files["worker"]))
            os.kill(job, signal.SIGKILL)  # only the job's main process, as a crash would
            self.configure(["ok|true"])

            result = self.run_job("nightly-guard", "shop", GUARD_NO_REPORT="1", LOCK_WAIT_SECONDS="30", LOCK_POLL_SECONDS="0.1")
            first.wait(timeout=30)
            self.assert_dead(worker)  # before the cleanup below, which would kill it anyway
            size = files["out"].stat().st_size
            time.sleep(0.3)
            self.assertEqual(files["out"].stat().st_size, size)
        finally:
            for pid in (locals().get("job"), first.pid):
                try:
                    os.killpg(pid, signal.SIGKILL) if pid else None
                except (ProcessLookupError, PermissionError):
                    pass

        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertEqual([r["status"] for r in self.nightly()], ["incomplete", "green"])

    def test_worker_outliving_a_killed_supervisor_keeps_the_project_busy(self):
        step, files = self.background_worker_step()
        self.configure([step])
        first = self.start_job("nightly-guard", "shop", GUARD_NO_REPORT="1")
        try:
            job = int(self.wait_for(files["job"]))
            first.kill()  # the supervisor itself: its job keeps running in its own group
            first.wait()
            self.configure(["ok|true"])
            busy = self.run_job("nightly-guard", "shop", GUARD_NO_REPORT="1", LOCK_WAIT_SECONDS="1", LOCK_POLL_SECONDS="0.1")
        finally:
            try:
                os.killpg(job, signal.SIGKILL)
            except (ProcessLookupError, PermissionError, UnboundLocalError):
                pass
        self.assert_dead(job)

        free = self.run_job("nightly-guard", "shop", GUARD_NO_REPORT="1", LOCK_WAIT_SECONDS="10", LOCK_POLL_SECONDS="0.1")

        self.assertEqual(busy.returncode, 75, busy.stdout)
        self.assertEqual(free.returncode, 0, free.stdout)
        self.assertEqual([r["status"] for r in self.nightly()], ["incomplete", "green"])

    def test_runs_started_in_the_same_second_keep_their_own_records(self):
        self.fake("date", '[ "$2" = "+%Y%m%dT%H%M%SZ" ] && { echo 20260101T000000Z; exit 0; }; exec /bin/date "$@"')
        self.configure(["ok|true"])

        self.run_job("nightly-guard", "shop", GUARD_NO_REPORT="1")
        self.run_job("nightly-guard", "shop", GUARD_NO_REPORT="1")

        runs = self.nightly()
        self.assertEqual([r["status"] for r in runs], ["green", "green"])
        self.assertNotEqual(runs[0]["run_id"], runs[1]["run_id"])

    def test_running_record_is_written_first_and_replaced_at_the_end(self):
        copy = Path(self.tmp.name) / "during.jsonl"
        self.configure([f"peek|cp {self.state}/nightly/shop.jsonl {copy}"])

        self.run_job("nightly-guard", "shop", GUARD_NO_REPORT="1")

        during = [json.loads(line) for line in copy.read_text().splitlines()]
        after = self.nightly()
        self.assertEqual([r["status"] for r in during], ["running"])
        self.assertEqual([r["status"] for r in after], ["green"])
        self.assertEqual(during[0]["run_id"], after[0]["run_id"])

    def test_running_record_left_by_a_killed_run_is_closed_as_incomplete(self):
        (self.state / "nightly").mkdir(parents=True)
        (self.state / "nightly" / "shop.jsonl").write_text(json.dumps({"status": "running", "run_id": "20000101T000000Z"}) + "\n")
        self.configure(["ok|true"])

        self.run_job("nightly-guard", "shop", GUARD_NO_REPORT="1")

        old, new = self.nightly()
        self.assertEqual((old["status"], old["failed_step"]), ("incomplete", "killed"))
        self.assertIn("killed before it could finish its record", old["warnings"])
        self.assertEqual(new["status"], "green")

    def test_hung_gh_call_times_out_and_the_run_still_records(self):
        self.fake("gh", 'echo "gh $*" >> "$CALLS"; exec sleep 20')
        self.configure(["bad|false"])

        started = time.monotonic()
        result = self.run_job("nightly-guard", "shop", GH_TIMEOUT_SECONDS="1")

        self.assertLess(time.monotonic() - started, 12, result.stdout)
        run = self.nightly()[-1]
        self.assertEqual((run["status"], run["warnings"]), ("red", ["issue report failed"]))

    def test_hung_fetch_times_out_and_is_recorded_incomplete(self):
        self.configure(["ok|true"])
        self.run_job("nightly-guard", "shop", GUARD_NO_REPORT="1")  # the clone exists; the next sync fetches
        self.fake("git", f'for arg in "$@"; do [ "$arg" = fetch ] && exec sleep 20; done; exec {shutil.which("git")} "$@"')

        started = time.monotonic()
        result = self.run_job("nightly-guard", "shop", GUARD_NO_REPORT="1", NET_TIMEOUT_SECONDS="1")

        self.assertLess(time.monotonic() - started, 12, result.stdout)
        run = self.nightly()[-1]
        self.assertEqual((run["status"], run["failed_step"]), ("incomplete", "sync"))

    def test_setsid_descendant_holding_the_clone_is_stopped_before_the_lock_is_released(self):
        pidfile = Path(self.tmp.name) / "escaped.pid"
        script = self.write_script("escape.py", """\
            import os, signal, sys, time
            if os.fork() == 0:
                os.setsid()  # leaves the job's process group
                signal.signal(signal.SIGTERM, signal.SIG_IGN)
                held = open("held.txt", "w")  # an open file in the clone
                open(sys.argv[1], "w").write(str(os.getpid()))
                time.sleep(60)
                os._exit(0)
            while not os.path.exists(sys.argv[1]):
                time.sleep(0.01)
            """)
        self.configure([f"escape|python3 {script} {pidfile}"])
        try:
            first = self.run_job("nightly-guard", "shop", GUARD_NO_REPORT="1", KITCHEN_STOP_GRACE_SECONDS="1")
            escaped = int(pidfile.read_text())
            self.assert_dead(escaped)
            second = self.run_job("nightly-guard", "shop", GUARD_NO_REPORT="1", LOCK_WAIT_SECONDS="1", LOCK_POLL_SECONDS="0.1")
        finally:
            if pidfile.exists():
                try:
                    os.kill(int(pidfile.read_text()), signal.SIGKILL)
                except ProcessLookupError:
                    pass

        self.assertEqual((first.returncode, second.returncode), (0, 0), first.stdout + second.stdout)
        self.assertEqual([r["status"] for r in self.nightly()], ["green", "green"])

    def holder_script(self):
        """A daemon: it keeps its pid in argv[1], waits for the clone (argv[2]) and for argv[3] when given, then
        holds a file in the clone."""
        return self.write_script("holder.py", """\
            import os, sys, time
            if os.fork() == 0:
                os.setsid()
                clone, ready = sys.argv[2], sys.argv[3] if len(sys.argv) > 3 else sys.argv[2]
                open(sys.argv[1], "w").write(str(os.getpid()))
                while not (os.path.exists(os.path.join(clone, ".git", "HEAD")) and os.path.exists(ready)):
                    time.sleep(0.01)
                os.chdir(clone)
                held = open(os.path.join(".git", "HEAD"))
                time.sleep(60)
                os._exit(0)
            """)

    def assert_alive(self, pid):
        state = subprocess.run(["ps", "-o", "stat=", "-p", str(pid)], capture_output=True, text=True).stdout.strip()
        self.assertTrue(state and not state.startswith("Z"), f"process {pid} was stopped (ps stat: {state!r})")

    def test_a_process_older_than_the_job_holding_the_clone_is_left_alone(self):
        # Docker Desktop's VM, running before the job, opens the clone's files for the job's containers and keeps them
        # open after the containers are gone (issue #61).
        self.configure(["ok|true"])
        pidfile = Path(self.tmp.name) / "holder.pid"
        clone = self.state / "automation" / "shop" / "clone"
        subprocess.run(["python3", str(self.holder_script()), str(pidfile), str(clone)], check=True)
        try:
            ended = self.run_job("nightly-guard", "shop", GUARD_NO_REPORT="1", KITCHEN_STOP_GRACE_SECONDS="1")
            holder = int(self.wait_for(pidfile))
            self.assert_alive(holder)
            following = self.run_job("nightly-guard", "shop", GUARD_NO_REPORT="1", LOCK_WAIT_SECONDS="1", LOCK_POLL_SECONDS="0.1")
            self.assert_alive(holder)
        finally:
            if pidfile.exists() and pidfile.read_text().strip():
                os.kill(int(pidfile.read_text()), signal.SIGKILL)

        self.assertEqual((ended.returncode, following.returncode), (0, 0), ended.stdout + ended.stderr + following.stdout)
        self.assertEqual([r["status"] for r in self.nightly()], ["green", "green"])

    def test_docker_desktop_started_for_the_job_outlives_it(self):
        # On macOS the job starts Docker Desktop through LaunchServices, not as its descendant; its VM then holds the clone.
        self.configure(["ok|true"], needs_docker=1)
        pidfile, up = Path(self.tmp.name) / "vm.pid", Path(self.tmp.name) / "docker-up"
        clone = self.state / "automation" / "shop" / "clone"
        self.fake("uname", "echo Darwin")
        self.fake("docker", f'[ -f "{up}" ]')
        self.fake("open", f'touch "{up}"; python3 {self.holder_script()} "{pidfile}" "{clone}" </dev/null >/dev/null 2>&1')
        try:
            result = self.run_job("nightly-guard", "shop", GUARD_NO_REPORT="1", KITCHEN_STOP_GRACE_SECONDS="1")
            vm = int(self.wait_for(pidfile))
            self.assert_alive(vm)
        finally:
            if pidfile.exists() and pidfile.read_text().strip():
                os.kill(int(pidfile.read_text()), signal.SIGKILL)

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.nightly()[-1]["status"], "green")

    def test_docker_that_was_not_ready_before_the_job_fails_the_run(self):
        # Docker Desktop that only comes up once the job runs has a VM younger than the job: the sweep would stop it.
        self.configure(["ok|true"], needs_docker=1)
        self.fake("uname", "echo Darwin")
        self.fake("open", "exit 0")
        self.fake("docker", 'case "$PWD" in */shop/clone) exit 0;; esac; exit 1')  # usable only once the job is in its clone

        result = self.run_job("nightly-guard", "shop", GUARD_NO_REPORT="1")

        run = self.nightly()[-1]
        self.assertEqual(result.returncode, 1, result.stdout)
        self.assertEqual((run["status"], run["failed_step"]), ("red", "docker"))

    def test_after_a_supervisor_died_mid_job_its_leftovers_are_not_spared(self):
        self.configure(["ok|true"])
        self.run_job("nightly-guard", "shop", GUARD_NO_REPORT="1")  # the clone and the list of older processes exist
        pidfile, go = Path(self.tmp.name) / "leftover.pid", Path(self.tmp.name) / "go"
        clone = self.state / "automation" / "shop" / "clone"
        # A process of the job whose supervisor died: idle while the next job acquires, then back in the clone.
        subprocess.run(["python3", str(self.holder_script()), str(pidfile), str(clone), str(go)], check=True)
        leftover = int(self.wait_for(pidfile))
        dead = subprocess.Popen(["true"])
        dead.wait()
        (self.state / "automation" / "shop" / "job.lock").write_text(f"{dead.pid}\n")  # the group it left recorded
        self.configure([f"wake|touch {go}; sleep 1"])
        try:
            result = self.run_job("nightly-guard", "shop", GUARD_NO_REPORT="1", KITCHEN_STOP_GRACE_SECONDS="1")
            self.assert_dead(leftover)
        finally:
            try:
                os.kill(leftover, signal.SIGKILL)
            except ProcessLookupError:
                pass

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_survivors_that_cannot_be_stopped_keep_the_project_busy_and_mark_the_run(self):
        pidfile = Path(self.tmp.name) / "respawner.pid"
        script = self.write_script("respawn.py", """\
            import os, sys, time
            if os.fork() == 0:
                os.setsid()
                clone = os.getcwd()
                os.chdir("/")  # the respawner itself holds nothing in the clone
                open(sys.argv[1], "w").write(str(os.getpid()))
                while True:  # every holder killed comes straight back
                    child = os.fork()
                    if child == 0:
                        os.chdir(clone)
                        held = open("held.txt", "w")
                        time.sleep(60)
                        os._exit(0)
                    os.waitpid(child, 0)
            while not os.path.exists(sys.argv[1]):
                time.sleep(0.01)
            time.sleep(0.2)
            """)
        self.configure([f"respawn|python3 {script} {pidfile}"])
        try:
            first = self.run_job("nightly-guard", "shop", GUARD_NO_REPORT="1", KITCHEN_STOP_GRACE_SECONDS="0.5")
            self.configure(["ok|true"])
            busy = self.run_job("nightly-guard", "shop", GUARD_NO_REPORT="1", LOCK_WAIT_SECONDS="1", LOCK_POLL_SECONDS="0.1")
        finally:
            respawner = int(pidfile.read_text())
            os.killpg(respawner, signal.SIGKILL)
        self.assert_dead(respawner)
        time.sleep(0.5)
        free = self.run_job("nightly-guard", "shop", GUARD_NO_REPORT="1", LOCK_WAIT_SECONDS="10", LOCK_POLL_SECONDS="0.1")

        runs = self.nightly()
        self.assertNotEqual(first.returncode, 0, first.stdout)
        self.assertEqual(runs[0]["status"], "incomplete")
        self.assertTrue(runs[0]["failed_step"].startswith("survivors "), runs[0])
        self.assertEqual(busy.returncode, 75, busy.stdout)
        self.assertEqual(free.returncode, 0, free.stdout)
        self.assertEqual([r["status"] for r in runs], ["incomplete", "green"])

class GardenerTests(AutomationFixture):
    def test_claude_failure_is_the_job_status_and_cleanup_still_runs(self):
        cleaned = Path(self.tmp.name) / "cleaned"
        self.configure_publishing(cleanup=f"touch {cleaned}")

        result = self.run_job("weekly-gardener", "shop", CLAUDE_EXIT="3")

        self.assertEqual(result.returncode, 3, result.stdout)
        self.assertTrue(cleaned.exists())
        self.assertIn("gardener end: status=3", result.stdout)

    def test_skips_while_a_gardener_pr_is_open(self):
        self.configure_publishing()

        result = self.run_job("weekly-gardener", "shop", OPEN_PRS="1")

        self.assertEqual(result.returncode, 0)
        self.assertIn("gardener skipped", result.stdout)
        self.assertNotIn("claude", self.calls_log())

    def test_missing_docker_stops_the_gardener(self):
        self.configure_publishing(needs_docker=1)
        self.fake("docker", "exit 1")
        self.fake("open", "exit 1")

        result = self.run_job("weekly-gardener", "shop")

        self.assertEqual(result.returncode, 1)
        self.assertNotIn("claude", self.calls_log())


    def test_busy_project_lock_skips_the_gardener(self):
        self.configure([])
        lock = self.state / "automation" / "shop" / "job.lock"
        lock.parent.mkdir(parents=True)

        with open(lock, "w") as held:
            fcntl.flock(held, fcntl.LOCK_EX)
            result = self.run_job("weekly-gardener", "shop", LOCK_WAIT_SECONDS="1", LOCK_POLL_SECONDS="0.1")

        self.assertEqual(result.returncode, 75, result.stdout)
        self.assertIn("gardener skipped: busy", result.stdout)
        self.assertNotIn("claude", self.calls_log())

    def test_a_busy_skip_never_masks_the_latest_real_run(self):
        self.configure_publishing()
        record = self.state / "gardener" / "shop.jsonl"
        record.parent.mkdir(parents=True)
        refused = {"ts": "2026-10-05T06:10:00+00:00", "status": "refused", "detail": "possible secret in the diff", "run_id": "active"}
        record.write_text(json.dumps(refused) + "\n")
        lock = self.state / "automation" / "shop" / "job.lock"
        lock.parent.mkdir(parents=True)

        with open(lock, "w") as held:
            fcntl.flock(held, fcntl.LOCK_EX)
            result = self.run_job("weekly-gardener", "shop", LOCK_WAIT_SECONDS="1", LOCK_POLL_SECONDS="0.1")

        self.assertEqual(result.returncode, 75, result.stdout)
        self.assertEqual(self.gardener(), [refused])


    def test_survivors_of_a_gardener_run_mark_the_gardener_record_incomplete(self):
        pidfile = Path(self.tmp.name) / "respawner.pid"
        script = self.write_script("respawn.py", """\
            import os, sys, time
            work = os.path.dirname(os.environ["GARDENER_SUMMARY_FILE"])  # the run's work dir, watched by the supervisor
            if os.fork() == 0:
                os.setsid()
                os.chdir("/")  # the respawner itself holds nothing in the watched dirs
                open(sys.argv[1], "w").write(str(os.getpid()))
                while True:  # every holder killed comes straight back
                    child = os.fork()
                    if child == 0:
                        os.chdir(work)
                        held = open("held.txt", "w")
                        time.sleep(60)
                        os._exit(0)
                    os.waitpid(child, 0)
            while not os.path.exists(sys.argv[1]):
                time.sleep(0.01)
            time.sleep(0.2)
            """)
        self.configure_publishing()
        self.fake("claude", f'echo "claude" >> "$CALLS"; python3 {script} {pidfile}')
        try:
            result = self.run_job("weekly-gardener", "shop", KITCHEN_STOP_GRACE_SECONDS="0.5")
        finally:
            respawner = int(pidfile.read_text())
            os.killpg(respawner, signal.SIGKILL)
        self.assert_dead(respawner)

        runs = self.gardener()
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(len(runs), 1, runs)
        self.assertEqual(runs[0]["status"], "incomplete", runs[0])
        self.assertTrue(any(re.fullmatch(r"survivors [0-9 ]+ still held files in the clone after TERM and KILL", w)
                            for w in runs[0]["warnings"]), runs[0])


class GardenerPublicationTests(AutomationFixture):
    """The agent only commits locally; the job publishes what passes its checks."""

    def agent(self, script):
        self.fake("claude", 'echo "claude" >> "$CALLS"\n' + script)

    def commit_script(self, branch='"${GARDENER_BRANCH_PREFIX}tidy"', path="notes.txt", lines=3):
        return textwrap.dedent(f"""\
            git checkout -q -b {branch}
            mkdir -p "$(dirname {path})"
            seq 1 {lines} > {path}
            git add {path}
            git commit -q -m "Tidy notes"
            printf '# Tidy the notes\\n\\nWhy and how verified.\\n' > "$GARDENER_SUMMARY_FILE"
            """)

    def test_agent_runs_without_any_way_to_publish(self):
        probe = Path(self.tmp.name) / "probe"
        self.configure_publishing()
        self.agent(textwrap.dedent(f"""\
            gh api repos/owner/shop/pulls/1/merge -X PUT; echo "gh=$?" >> {probe}
            echo "token=${{GH_TOKEN:-unset}} ${{GITHUB_TOKEN:-unset}} real=${{KITCHEN_REAL_GH:-unset}}" >> {probe}
            echo "prompt=$GIT_TERMINAL_PROMPT helper=[$(git config --get credential.helper)]" >> {probe}
            "$GIT_SSH_COMMAND" git@github.com true; echo "ssh=$?" >> {probe}
            git push -q origin HEAD:refs/heads/sneaky; echo "push=$?" >> {probe}
            """))

        result = self.run_job("weekly-gardener", "shop", GH_TOKEN="tok", GITHUB_TOKEN="tok")

        lines = probe.read_text()
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertIn("gh=1", lines)
        self.assertNotIn("gh api", self.calls_log())
        self.assertIn("token=unset unset real=unset", lines)
        self.assertIn("prompt=0 helper=[]", lines)
        self.assertIn("ssh=1", lines)
        self.assertNotIn("push=0", lines)
        self.assertNotIn("sneaky", self.origin_branches())

    def test_agent_bash_runs_in_the_os_sandbox_without_credential_reads(self):
        args = Path(self.tmp.name) / "args"
        tools = Path(self.tmp.name) / "tools"
        self.configure_publishing(extra=f'GARDENER_SANDBOX_DOMAINS=("nuget.org")\nGARDENER_SANDBOX_WRITE=("$HOME/.nuget")\nGARDENER_SANDBOX_READ=("{tools}")')
        self.agent(f'printf "%s\\n" "$@" > {args}; git config --global --list > {args}.git')

        result = self.run_job("weekly-gardener", "shop")

        argv = args.read_text().splitlines()
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertIn("--strict-mcp-config", argv)
        # neither the user's interactive hooks nor the project's checked-in settings load; --settings carries the sandbox
        self.assertEqual(argv[argv.index("--setting-sources") + 1], "local")
        settings = json.loads(Path(argv[argv.index("--settings") + 1]).read_text())
        sandbox = settings["sandbox"]
        self.assertEqual((sandbox["enabled"], sandbox["failIfUnavailable"], sandbox["allowUnsandboxedCommands"]), (True, True, False))
        for path in ("~/.ssh", "~/Library/Keychains", "~/.config/gh", "~/.git-credentials", "~/.netrc", "~/.aws"):
            self.assertIn(path, sandbox["filesystem"]["denyRead"])
        self.assertIn("Read(~/.ssh/**)", settings["permissions"]["deny"])
        self.assertEqual(sandbox["network"]["allowedDomains"], ["nuget.org"])
        self.assertIn(f"{os.environ['HOME']}/.nuget", sandbox["filesystem"]["allowWrite"])
        # reads are an allowlist: the whole home is denied, then exactly the job's paths are re-opened
        self.assertTrue(settings["permissions"]["blockReadsOutsideWorkingDirectories"])
        self.assertIn("~/", sandbox["filesystem"]["denyRead"])
        state = self.state / "automation" / "shop"
        allowed = sandbox["filesystem"]["allowRead"]
        work = Path(argv[argv.index("--settings") + 1]).parent
        self.assertEqual(allowed[:3], [str(work / "clone"), str(work), str(state / "history")])  # its own clone, not the nightly's
        self.assertIn(str(tools), allowed)
        self.assertNotIn(os.environ["HOME"], allowed)
        self.assertNotIn("~", allowed)
        # ~/.gitconfig stays out of reach: the agent's git sees only an identity
        keys = sorted(line.split("=")[0] for line in Path(f"{args}.git").read_text().splitlines())
        self.assertEqual(keys, ["user.email", "user.name"])

    def test_model_and_effort_reach_claude_only_when_set(self):
        args = Path(self.tmp.name) / "args"
        self.agent(f'printf "%s\\n" "$@" > {args}')
        for extra, expected in (("", (None, None)), ('GARDENER_MODEL="opus"\nGARDENER_EFFORT="medium"\n', ("opus", "medium"))):
            with self.subTest(extra=extra):
                self.configure_publishing(extra=extra)

                self.run_job("weekly-gardener", "shop")

                argv = args.read_text().splitlines()
                self.assertEqual(tuple(argv[argv.index(flag) + 1] if flag in argv else None for flag in ("--model", "--effort")), expected)

    def test_agent_environment_is_an_allowlist(self):
        names = Path(self.tmp.name) / "names"
        self.configure_publishing()
        self.agent(f"env | cut -d= -f1 | sort > {names}")

        cloud = "AWS_SESSION_" + "TOKEN"  # built at runtime: the repo never holds a credential-shaped assignment
        self.run_job("weekly-gardener", "shop", REVIEW_PRIVATE_CANARY="inherited", **{cloud: "inherited"})

        seen = set(names.read_text().split())
        self.assertNotIn("REVIEW_PRIVATE_CANARY", seen)
        self.assertNotIn(cloud, seen)
        self.assertNotIn("KITCHEN_REAL_GH", seen)
        allowed = {"HOME", "USER", "LOGNAME", "SHELL", "ZDOTDIR", "TMPDIR", "LANG", "TERM", "PATH", "PWD", "SHLVL", "_", "OLDPWD",
                   "CALLS", "CLAUDE_EXIT", "GH_CONFIG_DIR", "GIT_SSH_COMMAND", "GIT_ASKPASS", "SSH_ASKPASS",
                   "GIT_TERMINAL_PROMPT", "GIT_CONFIG_GLOBAL", "GIT_CONFIG_COUNT", "GIT_CONFIG_KEY_0", "GIT_CONFIG_VALUE_0",
                   "GIT_CONFIG_KEY_1", "GIT_CONFIG_VALUE_1", "GARDENER_BRANCH_PREFIX", "GARDENER_SUMMARY_FILE"}
        self.assertEqual({n for n in seen if not n.startswith("LC_")} - allowed, set())

    @unittest.skipUnless(platform.system() == "Darwin", "zsh with an empty ZDOTDIR is the macOS shell")
    def test_agent_shell_reads_no_user_startup_file(self):
        values = Path(self.tmp.name) / "shell"
        self.configure_publishing()
        self.agent(f'echo "$SHELL $ZDOTDIR" > {values}; ls -A "$ZDOTDIR" | wc -l >> {values}')

        self.run_job("weekly-gardener", "shop")

        shell, zdotdir, entries = values.read_text().split()
        self.assertEqual(shell, "/bin/zsh")
        self.assertTrue(zdotdir.startswith(str(self.state)), zdotdir)
        self.assertEqual(entries, "0")

    @unittest.skipUnless(platform.system() == "Linux", "the owner chose bash for the Linux host")
    def test_agent_shell_on_linux_is_bash(self):
        values = Path(self.tmp.name) / "shell"
        self.configure_publishing()
        self.agent(f'echo "$SHELL ${{ZDOTDIR:-unset}}" > {values}')

        self.run_job("weekly-gardener", "shop")

        self.assertEqual(values.read_text().split(), ["/bin/bash", "unset"])

    def run_each_result(self):
        """published, refused, none, incomplete: one gardener run each. Returns the notifications of each run."""
        sent = []
        for setup, agent, env in ((self.configure_publishing, self.commit_script(), {}),
                                  (lambda: self.configure([]), self.commit_script(), {}),
                                  (self.configure_publishing, "true", {}),
                                  (self.configure_publishing, 'exit "$CLAUDE_EXIT"', {"CLAUDE_EXIT": "3"})):
            setup()
            self.agent(agent)
            self.run_job("weekly-gardener", "shop", **env)
            sent.append(self.notifications()[sum(map(len, sent)):])
        return sent

    def test_each_run_appends_one_gardener_record_with_its_result(self):
        self.run_each_result()

        records = self.gardener()
        self.assertEqual([r["status"] for r in records], ["published", "refused", "none", "incomplete"])
        self.assertEqual(records[0]["detail"], "https://example.test/pull/1")
        self.assertIn("no independent verification configured", records[1]["detail"])
        self.assertEqual(records[2]["detail"], "no gardener branch was committed")
        self.assertIn("claude exited 3", records[3]["detail"])
        self.assertEqual(len({r["run_id"] for r in records}), 4)

    def test_refused_or_incomplete_runs_notify_and_the_others_stay_silent(self):
        sent = self.run_each_result()

        self.assertEqual([len(batch) for batch in sent], [0, 1, 0, 1], sent)
        self.assertIn("kitchen gardener: shop refused", sent[1][0])
        self.assertIn("kitchen gardener: shop incomplete", sent[3][0])

    def test_job_publishes_the_agents_branch_with_the_label_and_summary(self):
        branch = f"gardener/{datetime.datetime.now(datetime.timezone.utc):%Y-%m-%d}-tidy"
        self.configure_publishing()
        self.agent(self.commit_script())

        result = self.run_job("weekly-gardener", "shop")

        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertIn(branch, self.origin_branches())
        pr_create = [line for line in self.calls_log().splitlines() if line.startswith("gh pr create")]
        self.assertEqual(len(pr_create), 1)
        self.assertIn(f"--head {branch}", pr_create[0])
        self.assertIn("--label gardener", pr_create[0])
        self.assertIn("--title Tidy the notes", pr_create[0])
        self.assertIn("GARDENER-RESULT: https://example.test/pull/1", result.stdout)

    def test_only_the_validated_final_tree_is_published_as_one_commit(self):
        branch = f"gardener/{datetime.datetime.now(datetime.timezone.utc):%Y-%m-%d}-tidy"
        self.configure_publishing()
        self.agent(textwrap.dedent("""\
            git checkout -q -b "${GARDENER_BRANCH_PREFIX}tidy"
            mkdir -p .github/workflows && echo "on: push" > .github/workflows/test.yml && echo leaked-value > leaked.txt
            git add -A && git commit -q -m "Add then remove"
            git rm -q .github/workflows/test.yml leaked.txt && seq 1 3 > notes.txt && git add notes.txt
            git commit -q -m "Tidy notes"
            printf '# Tidy the notes\\n\\nWhy.\\n' > "$GARDENER_SUMMARY_FILE"
            """))

        result = self.run_job("weekly-gardener", "shop")

        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertEqual(self.git(self.origin, "rev-list", "--count", f"dev..{branch}"), "1")
        self.assertEqual(self.git(self.origin, "rev-parse", f"{branch}^"), self.git(self.origin, "rev-parse", "dev"))
        self.assertEqual(self.git(self.origin, "ls-tree", "--name-only", branch).splitlines(), ["notes.txt"])
        published = self.git(self.origin, "rev-list", "--objects", "--all")
        self.assertNotIn("leaked.txt", published)
        self.assertNotIn("test.yml", published)

    def test_planted_token_is_refused_and_nothing_is_pushed(self):
        token = "gh" + "p_" + "".join(random.Random(3).choice(string.ascii_letters) for _ in range(36))  # runtime only
        self.configure_publishing()
        self.agent(self.commit_script() + f'echo "export TOKEN={token}" >> notes.txt && git commit -q -am "More notes"\n')

        self.assert_refused(self.run_job("weekly-gardener", "shop"), "possible secret in the diff: notes.txt:4: GitHub token")
        self.assertNotIn(token, self.calls_log())

    def test_planted_random_key_is_refused(self):
        rng = random.Random(11)
        key = "".join(rng.choice(string.ascii_letters + string.digits) for _ in range(40))
        self.configure_publishing()
        self.agent(self.commit_script() + f'echo "client = Client(\\"{key}\\")" >> notes.txt && git commit -q -am "More notes"\n')

        self.assert_refused(self.run_job("weekly-gardener", "shop"), "notes.txt:4: high-entropy string")

    def test_branch_moved_during_publication_cannot_change_what_is_published(self):
        tmp = Path(self.tmp.name)
        started, moved = tmp / "commit-tree.started", tmp / "branch.moved"
        self.configure_publishing()
        self.fake("git", f"""[ "$1" = commit-tree ] && {{ touch {started}; for i in $(seq 1 500); do [ -f {moved} ] && break; sleep .01; done; }}
exec {shutil.which("git")} "$@" """)
        self.agent(self.commit_script() + f"""
safe=$(git rev-parse HEAD)
mkdir -p .github/workflows && echo "on: push" > .github/workflows/unvalidated.yml
git add .github && git commit -q -m "Unvalidated protected tree"
bad=$(git rev-parse HEAD)
git reset -q --hard "$safe"
( for i in $(seq 1 500); do [ -f {started} ] && break; sleep .01; done
  git update-ref "refs/heads/${{GARDENER_BRANCH_PREFIX}}tidy" "$bad"; touch {moved} ) >/dev/null 2>&1 &
""")

        result = self.run_job("weekly-gardener", "shop")

        branch = f"gardener/{datetime.datetime.now(datetime.timezone.utc):%Y-%m-%d}-tidy"
        self.assertTrue(moved.exists(), "the race did not happen")
        published = self.git(self.origin, "ls-tree", "-r", "--name-only", branch).splitlines() if branch in self.origin_branches() else []
        self.assertNotIn(".github/workflows/unvalidated.yml", published, result.stdout)

    def test_hook_files_the_agent_leaves_in_the_tree_never_run_unsealed(self):
        evil = Path(self.tmp.name) / "hook-ran"
        self.commit_to_origin({"scripts/hooks/post-checkout": ("#!/bin/sh\nexit 0\n", 0o755)})
        self.configure_publishing(extra='HOOKS_PATH="scripts/hooks"')
        self.agent(self.commit_script() + f"""
printf '#!/bin/sh\\ntouch {evil}\\n' > scripts/hooks/pre-push && chmod +x scripts/hooks/pre-push
printf '#!/bin/sh\\ntouch {evil}\\n' > scripts/hooks/post-checkout
""")

        published = self.run_job("weekly-gardener", "shop")
        self.configure(["ok|true"], extra='HOOKS_PATH="scripts/hooks"')
        guard = self.run_job("nightly-guard", "shop", GUARD_NO_REPORT="1")  # its sync checks out the agent's tree

        self.assertEqual((published.returncode, guard.returncode), (0, 0), published.stdout + guard.stdout)
        self.assertFalse(evil.exists())

    def test_cleanup_never_runs_code_the_agent_wrote(self):
        tmp = Path(self.tmp.name)
        evil, cleaned = tmp / "evil-ran", tmp / "cleaned"
        self.commit_to_origin({"stop.sh": (f"touch {cleaned}\n", 0o644)})
        self.configure_publishing(cleanup="sh stop.sh")
        self.agent(self.commit_script() + f"echo 'touch {evil}' > stop.sh\n")

        result = self.run_job("weekly-gardener", "shop")

        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertTrue(cleaned.exists())
        self.assertFalse(evil.exists())

    def test_token_split_across_lines_is_refused(self):
        token = "gh" + "p_" + "".join(random.Random(5).choice(string.ascii_letters) for _ in range(36))
        self.configure_publishing()
        self.agent(self.commit_script() + f"""printf 'value = ("{token[:18]}"\\n         "{token[18:]}")\\n' >> notes.txt
git commit -q -am "More notes"
""")

        self.assert_refused(self.run_job("weekly-gardener", "shop"), "GitHub token (across lines)")


    def pr_body(self):
        line = next(line for line in self.calls_log().splitlines() if line.startswith("gh pr create"))
        return Path(line.split("--body-file ")[1].split()[0]).read_text()

    def test_job_reruns_the_verify_steps_on_the_exported_tree_and_lists_them_in_the_pr(self):
        out = Path(self.tmp.name) / "verify-out"
        out.mkdir()
        # a tracked file that matches an ignore rule must still be part of the checkout
        (self.origin / ".gitignore").write_text("*.log\n")
        (self.origin / "keep.log").write_text("tracked anyway\n")
        self.git(self.origin, "add", "-f", ".gitignore", "keep.log")
        self.git(self.origin, "commit", "-q", "-m", "ignored but tracked")
        checkout = f"git rev-parse HEAD^{{tree}} > {out}/tree && git status --porcelain > {out}/status && pwd > {out}/cwd"
        self.verify_config(f'"notes|grep -qx 3 notes.txt" "checkout|{checkout}"', f'GARDENER_SANDBOX_WRITE=("{out}")')
        self.agent(self.commit_script())

        result = self.run_job("weekly-gardener", "shop")

        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertIn("verify: notes", result.stdout)
        self.assertTrue((out / "cwd").read_text().strip().endswith("/verify"))
        branch = f"gardener/{datetime.datetime.now(datetime.timezone.utc):%Y-%m-%d}-tidy"
        self.assertEqual((out / "tree").read_text().strip(), self.git(self.origin, "rev-parse", f"{branch}^{{tree}}"))
        self.assertEqual((out / "status").read_text(), "")
        body = self.pr_body()
        self.assertIn("Job verification: the job reran these steps on exactly this tree, sandboxed by srt", body)
        self.assertIn("- notes: passed\n- checkout: passed", body)

    def test_tree_attributes_cannot_make_the_verify_checkout_fail(self):
        # working-tree-encoding converts on add: re-adding the exported (stored) bytes must not apply it again
        (self.origin / ".gitattributes").write_text("*.ps1 text working-tree-encoding=UTF-16\n")
        (self.origin / "run.ps1").write_bytes("Write-Output 'hi'\n".encode("utf-16"))
        self.git(self.origin, "add", ".gitattributes", "run.ps1")
        self.git(self.origin, "commit", "-q", "-m", "utf-16 script")
        self.verify_config('"build|true"')
        self.agent(self.commit_script())

        result = self.run_job("weekly-gardener", "shop")

        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertIn("- build: passed", self.pr_body())

    @unittest.skipUnless(platform.system() == "Linux", "the sandboxes mask absent dangerous files only on Linux")
    def test_git_and_gitmodules_readers_work_despite_the_sandbox_masks(self):
        seen = Path(self.tmp.name) / "agent-gitmodules"
        self.verify_config("'gitmodules|cat .gitmodules && git add -A --dry-run && git status --porcelain | wc -l | grep -qx 0'")
        self.agent(f'cat .gitmodules && git status --porcelain > {seen}\n' + self.commit_script())

        result = self.run_job("weekly-gardener", "shop")

        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertEqual(seen.read_text(), "")
        self.assertIn("- gitmodules: passed", self.pr_body())
        self.assertNotIn(".gitmodules", self.git(self.origin, "ls-tree", "-r", "--name-only",
                                                 f"gardener/{datetime.datetime.now(datetime.timezone.utc):%Y-%m-%d}-tidy"))

    def test_failing_verify_step_publishes_nothing(self):
        self.verify_config('"build|true" "tests|exit 3"')
        self.agent(self.commit_script())

        self.assert_refused(self.run_job("weekly-gardener", "shop"), "job verification step tests failed (srt exit 0, step exit 3;")

    def test_verify_steps_cannot_read_home_write_outside_or_reach_the_network(self):
        # outside /tmp: on Linux the sandboxes write the run's private /tmp
        (Path.home() / ".cache").mkdir(exist_ok=True)
        home_root = tempfile.TemporaryDirectory(dir=Path.home() / ".cache")
        self.addCleanup(home_root.cleanup)
        home = Path(home_root.name) / "home"
        (home / ".ssh").mkdir(parents=True)
        (home / "private.txt").write_text("canary")
        (home / ".ssh" / "id_test").write_text("canary")
        probe = (  # each escape that works fails the step with its own exit code
            'if cat "$HOME/private.txt"; then exit 10; fi; if cat "$HOME/.ssh/id_test"; then exit 11; fi; '
            'touch "$HOME/planted" 2>/dev/null || true; if curl -s -m 5 https://example.com; then exit 13; fi; exit 0')
        # a write into home must not reach the real one (on Linux it lands in the sandbox's own empty home)
        self.verify_config(f"'probe|{probe}'")
        self.agent(self.commit_script())

        result = self.run_job("weekly-gardener", "shop", HOME=str(home))

        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertIn("- probe: passed", self.pr_body())
        self.assertFalse((home / "planted").exists())

    def test_failures_inside_a_step_fail_it_even_when_its_last_command_succeeds(self):
        for step, recorded in (("false | cat", "1"), ("false; true", "1"), ("kill -TERM $$", "143")):
            with self.subTest(step=step):
                self.tearDown(); self.setUp()
                self.verify_config(f"'check|{step}'")
                self.agent(self.commit_script())
                self.assert_refused(self.run_job("weekly-gardener", "shop"), f"step exit {recorded};")

    def test_a_step_whose_shell_is_killed_never_counts_as_passed(self):
        # srt reports a child killed by a signal as exit 0; the missing exit record still fails the step
        self.verify_config("'check|kill -TERM $PPID'")
        self.agent(self.commit_script())

        self.assert_refused(self.run_job("weekly-gardener", "shop"), "step exit none;")

    def test_attributes_in_the_tree_cannot_hide_files_from_verification(self):
        self.verify_config("'tests|test ! -e broken.flag && test -f .gitattributes'")
        self.agent(self.commit_script() + textwrap.dedent("""\
            printf 'broken.flag export-ignore\\n' > .gitattributes && touch broken.flag
            git add .gitattributes broken.flag && git commit -q -m 'Hide a file from git archive'
            """))

        self.assert_refused(self.run_job("weekly-gardener", "shop"), "job verification step tests failed")

    def test_files_the_agent_plants_in_its_work_dir_cannot_redirect_the_jobs_writes(self):
        outside = Path(self.tmp.name) / "outside.txt"
        outside.write_text("unchanged")
        self.verify_config('"build|true"')
        self.agent(self.commit_script() + textwrap.dedent(f"""\
            wd=$(dirname "$GARDENER_SUMMARY_FILE")
            for name in verify-srt.json body.md message.txt verify.log; do ln -s {outside} "$wd/$name"; done
            """))

        result = self.run_job("weekly-gardener", "shop")

        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertEqual(outside.read_text(), "unchanged")

    def test_git_config_the_agent_writes_never_runs_in_the_job(self):
        ran = Path(self.tmp.name) / "fsmonitor-ran"
        hook = Path(self.tmp.name) / "fsmonitor.sh"
        hook.write_text(f"#!/bin/sh\ntouch {ran}\n")
        hook.chmod(0o755)
        common = Path(self.tmp.name) / "planted-common"
        self.git(self.origin, "init", "-q", "--bare", str(common))
        for key in ("core.fsmonitor", "core.sshCommand", "core.alternateRefsCommand"):
            self.git(common, "config", key, str(hook))
        self.configure_publishing(cleanup="true")
        for redirect in ("", f"printf '%s\\n' '{common}' > .git/commondir\n"):
            with self.subTest(commondir=bool(redirect)):
                self.agent(self.commit_script() + f"git config core.fsmonitor {hook}\ngit config core.sshCommand {hook}\n" + redirect)
                result = self.run_job("weekly-gardener", "shop")
                self.assertIn("GARDENER-RESULT: ", result.stdout)
        guard = self.run_job("nightly-guard", "shop", GUARD_NO_REPORT="1")

        self.assertEqual(guard.returncode, 0, guard.stdout)
        self.assertFalse(ran.exists())
        self.assertEqual(list((self.state / "automation" / "shop" / "gardener").glob("*/clone")), [])  # deleted

    def test_verify_steps_without_srt_stop_the_run_before_the_agent(self):
        copy = Path(self.tmp.name) / "automation"
        shutil.copytree(AUTOMATION, copy, ignore=shutil.ignore_patterns("node_modules"))
        self.verify_config('"build|true"')
        self.agent(self.commit_script())

        result = subprocess.run(["bash", str(copy / "bin" / "weekly-gardener"), "shop"], env=self.job_env(),
                                capture_output=True, text=True)

        self.assertEqual(result.returncode, 1, result.stdout)
        self.assertIn("srt is not installed", result.stdout)
        self.assertNotIn("claude", self.calls_log())

    def test_prepare_runs_on_the_pinned_base_before_the_agent(self):
        out = Path(self.tmp.name) / "prepared"
        self.commit_to_origin({"base.txt": ("from the base\n", 0o644)})
        self.configure_publishing(extra=f'GARDENER_PREPARE_CMD="cat base.txt > {out} && test ! -e .git && echo prepare >> $CALLS"')
        self.agent(self.commit_script())

        result = self.run_job("weekly-gardener", "shop")

        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertEqual(out.read_text(), "from the base\n")
        calls = self.calls_log().splitlines()
        self.assertLess(calls.index("prepare"), calls.index("claude"))

    def test_failed_prepare_stops_the_run_before_the_agent(self):
        self.configure_publishing(extra='GARDENER_PREPARE_CMD="false; true"')
        self.agent(self.commit_script())

        result = self.run_job("weekly-gardener", "shop")

        self.assertEqual(result.returncode, 1, result.stdout)
        self.assertIn("GARDENER_PREPARE_CMD failed", result.stdout)
        self.assertNotIn("claude", self.calls_log())

    def test_without_verify_steps_the_job_refuses_before_the_agent_runs(self):
        self.configure([])
        self.agent(self.commit_script())

        result = self.run_job("weekly-gardener", "shop")

        self.assert_refused(result, "no independent verification configured")
        self.assertNotIn("claude", self.calls_log())
        self.assertEqual([r["status"] for r in self.gardener()], ["refused"])
        self.assertEqual(len(self.notifications()), 1, self.calls_log())

    def test_a_malformed_verify_step_refuses_before_the_agent_runs(self):
        self.verify_config('"build|true" "tests|"', guard_steps=["ok|true"])
        self.agent(self.commit_script())

        result = self.run_job("weekly-gardener", "shop")

        self.assert_refused(result, "not name|command with both parts (tests|)")
        self.assertNotIn("claude", self.calls_log())

    def test_without_verify_steps_the_job_refuses_to_publish(self):
        self.configure([])
        self.agent(self.commit_script())

        self.assert_refused(self.run_job("weekly-gardener", "shop"), "no independent verification configured")

    def test_verify_sandbox_settings_carry_the_agents_boundary(self):
        settings = json.loads(subprocess.run(
            ["python3", str(AUTOMATION / "lib" / "sandbox_settings.py"), "--format", "srt", "--read", "/clone",
             "--write", "/cache", "--domain", "api.nuget.org"], check=True, capture_output=True, text=True).stdout)

        fs, net = settings["filesystem"], settings["network"]
        self.assertEqual(fs["denyRead"][0], "~/")
        for path in ("~/.ssh", "~/Library/Keychains", "~/.config/gh", "~/.codex", "~/.netrc"):
            self.assertIn(path, fs["denyRead"])
            self.assertIn(path, fs["denyWrite"])
        self.assertEqual((fs["allowRead"], fs["allowWrite"]), (["/clone"], ["/cache"]))
        self.assertEqual((net["allowedDomains"], net["deniedDomains"], net["allowUnixSockets"]), (["api.nuget.org"], [], []))

    def assert_refused(self, result, reason):
        self.assertEqual(result.returncode, 1, result.stdout)
        self.assertIn(f"GARDENER-RESULT: refused - ", result.stdout)
        self.assertIn(reason, result.stdout)
        self.assertEqual([b for b in self.origin_branches() if b.startswith("gardener/")], [])
        self.assertNotIn("gh pr create", self.calls_log())

    def test_a_summary_the_agent_links_to_a_host_file_is_not_published(self):
        for name, plant in (("symlink", 'ln -sf "$SECRET" "$GARDENER_SUMMARY_FILE"'),
                            ("hard link", 'rm -f "$GARDENER_SUMMARY_FILE"; ln "$SECRET" "$GARDENER_SUMMARY_FILE"')):
            with self.subTest(name):
                self.tearDown(); self.setUp()
                secret = Path(self.tmp.name) / "host-only.txt"  # outside every path the agent may read
                secret.write_text("# host-only sentinel\n\nnot for a PR\n")
                plant = f"SECRET={secret}; {plant}"
                self.configure_publishing()
                self.agent(self.commit_script() + plant + "\n")

                self.assert_refused(self.run_job("weekly-gardener", "shop"), "is not a regular file of its own")
                self.assertNotIn("host-only sentinel", self.calls_log())

    def test_oversized_diff_is_not_published(self):
        self.configure_publishing(extra="GARDENER_MAX_CHANGED_LINES=5")
        self.agent(self.commit_script(lines=10))

        self.assert_refused(self.run_job("weekly-gardener", "shop"), "the bound is 5")

    def test_protected_path_is_not_published(self):
        self.configure_publishing()
        self.agent(self.commit_script(path=".github/workflows/ci.yml"))

        self.assert_refused(self.run_job("weekly-gardener", "shop"), "protected path .github/workflows/ci.yml")

    def test_branch_with_another_name_is_not_published(self):
        self.configure_publishing()
        self.agent(self.commit_script(branch="gardener/misc"))

        self.assert_refused(self.run_job("weekly-gardener", "shop"), "is not named gardener/")

    def test_branch_without_a_summary_is_not_published(self):
        self.configure_publishing()
        self.agent(self.commit_script() + 'rm "$GARDENER_SUMMARY_FILE"\n')

        self.assert_refused(self.run_job("weekly-gardener", "shop"), "no PR summary")


class DailyAuditTests(AutomationFixture):
    """The audit stands on the guard's green run of the same SHA, runs a sealed agent, then logs and publishes itself."""

    PROVE = "prove|mkdir -p .verify/evidence && echo held > .verify/evidence/ledger.jsonl"
    MAP = "# Map\n\nReaders: list them.\n\n```bash\n$V api GET /readers --expect 200\n```\n"

    def setUp(self):
        super().setUp()
        self.checkout = Path(self.tmp.name) / "shop"  # the owner's checkout, named like the project; only read
        subprocess.run(["git", "clone", "-q", str(self.origin), str(self.checkout)], check=True, env=clean_env())
        (self.config / "projects.txt").write_text(f"{self.checkout}\n")

    def audit_config(self, guard_steps=(PROVE, "control|true"), cited="prove control", extra=""):
        node = Path(shutil.which("node")).parent  # srt, for the job's verification of a fix
        self.configure(list(guard_steps), extra=f'EXTRA_PATH="{self.bin}:{node}"\nAUDIT_GUARD_STEPS=({cited})\n'
                                                f'AUDIT_EVIDENCE_PATHS=(".verify/evidence")\n' + extra)

    FIXES = 'AUDIT_EDIT_PATHS=("docs/*")\nAUDIT_VERIFY_STEPS=("map|grep -q \'every reader\' docs/map.md")\n'

    def log_baseline(self, sha, outcome="changed full"):
        self.log_raw(f"audit-verification {outcome} at {sha}")

    def log_raw(self, message):
        entry = {"ts": datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds"), "repo": str(self.checkout),
                 "project": str((self.checkout / ".git").resolve()), "status": "note", "message": message}
        self.state.mkdir(parents=True, exist_ok=True)
        with open(self.state / "log.jsonl", "a") as handle:
            handle.write(json.dumps(entry) + "\n")

    def journal(self):
        path = self.state / "log.jsonl"
        return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []

    def audits(self):
        return [json.loads(line) for line in (self.state / "audit" / "shop.jsonl").read_text().splitlines()]

    def churn(self):
        """A baseline on the first commit, then one change on dev: the churn of the next pass."""
        self.log_baseline(self.git(self.origin, "rev-parse", "dev"))
        self.commit_to_origin({"docs/map.md": (self.MAP, 0o644)})
        return self.git(self.origin, "rev-parse", "dev")

    def agent(self, outcome="findings", script=""):
        self.fake("claude", 'echo "claude" >> "$CALLS"\n' + script +
                  f'\nprintf "# Audit\\n\\nOutcome: {outcome}\\n" > "$AUDIT_REPORT_FILE"\necho {outcome} > "$AUDIT_OUTCOME_FILE"\n')

    def guard_then_audit(self, **env):
        guard = self.run_job("nightly-guard", "shop", GUARD_NO_REPORT="1")
        self.assertEqual(guard.returncode in (0, 1), True, guard.stdout)
        return self.run_job("daily-audit", "shop", **env)

    def test_a_red_guard_blocks_the_pass_and_the_agent_never_runs(self):
        sha = self.churn()
        self.audit_config(guard_steps=(self.PROVE, "control|false"))
        self.agent()

        result = self.guard_then_audit()

        self.assertEqual(result.returncode, 0, result.stdout)
        run = self.audits()[-1]
        self.assertEqual((run["status"], run["sha"]), ("blocked", sha))
        self.assertIn("is red on", run["detail"])
        self.assertIn("(failed: control)", run["detail"])
        self.assertNotIn("claude", self.calls_log())
        self.assertEqual(len(self.journal()), 1)  # only the baseline: a pass that never ran logs nothing
        self.assertIn("kitchen audit: shop blocked", self.notifications()[-1])

    def test_a_clone_moved_off_the_guards_sha_is_refused(self):
        guarded = self.churn()
        self.audit_config()
        self.agent()
        self.run_job("nightly-guard", "shop", GUARD_NO_REPORT="1")
        self.commit_to_origin({"later.txt": ("merged after the guard\n", 0o644)})
        clone = self.state / "automation" / "shop" / "clone"  # what the next sync does to the guard's clone
        self.git(clone, "fetch", "-q", "origin")
        self.git(clone, "checkout", "-q", "--detach", "origin/dev")
        moved = self.git(clone, "rev-parse", "HEAD")

        result = self.run_job("daily-audit", "shop")

        self.assertEqual(result.returncode, 1, result.stdout)
        self.assertIn(f"AUDIT-RESULT: refused - the guard proved {guarded[:12]} but its clone is at {moved}", result.stdout)
        self.assertNotIn("claude", self.calls_log())
        self.assertEqual(self.audits()[-1]["status"], "refused")

    def test_no_change_since_the_baseline_is_skipped_without_the_agent(self):
        self.log_baseline(self.git(self.origin, "rev-parse", "dev"))
        self.audit_config()
        self.agent()

        result = self.guard_then_audit()

        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertEqual(self.audits()[-1]["status"], "skipped")
        self.assertNotIn("claude", self.calls_log())
        self.assertEqual(self.notifications(), [])
        sha = self.git(self.origin, "rev-parse", "dev")  # the checkpoint keeps the baseline inside the journal window
        self.assertEqual([e["message"] for e in self.journal()], [f"audit-verification changed full at {sha}",
                                                                  f"audit-verification skipped churn at {sha}"])

    def test_a_guard_run_without_a_cited_step_is_refused(self):
        self.churn()
        self.audit_config(guard_steps=(self.PROVE,))
        self.agent()

        result = self.guard_then_audit()

        self.assertEqual(result.returncode, 1, result.stdout)
        self.assertIn("did not run step control", result.stdout)
        self.assertNotIn("claude", self.calls_log())

    def test_without_a_baseline_the_job_refuses_instead_of_a_full_pass(self):
        self.commit_to_origin({"docs/map.md": (self.MAP, 0o644)})
        self.audit_config()
        self.agent()

        result = self.guard_then_audit()

        self.assertEqual(result.returncode, 1, result.stdout)
        self.assertIn("no churn baseline", result.stdout)
        self.assertIn("run a full audit-verification pass by hand", result.stdout)
        self.assertNotIn("claude", self.calls_log())

    def test_the_agent_runs_sealed_with_the_guards_evidence_and_the_configured_model(self):
        tmp = Path(self.tmp.name)
        sha = self.churn()
        baseline = self.git(self.origin, "rev-parse", "dev~1")
        self.audit_config(extra='AUDIT_MODEL="sonnet"\nAUDIT_EFFORT="low"\nGARDENER_SANDBOX_DOMAINS=("nuget.org")\n'
                                'GARDENER_SANDBOX_UNIX_SOCKETS=("/var/run/docker.sock")\n')
        self.agent(script=f"""printf "%s\\n" "$@" > {tmp}/args; printf '%s' "$2" > {tmp}/prompt
work=$(dirname "$AUDIT_REPORT_FILE")
cat "$work/guard/evidence/prove/.verify/evidence/ledger.jsonl" > {tmp}/ledger
ls "$work/guard/logs" > {tmp}/logs
head -1 "$work/skill/SKILL.md" > {tmp}/skill
git rev-parse HEAD > {tmp}/head
echo "${{KITCHEN_REAL_GH:-unset}}" > {tmp}/gh""")

        result = self.guard_then_audit()

        self.assertEqual(result.returncode, 0, result.stdout)
        argv = (tmp / "args").read_text().splitlines()
        prompt = (tmp / "prompt").read_text()
        self.assertEqual((argv[argv.index("--model") + 1], argv[argv.index("--effort") + 1]), ("sonnet", "low"))
        self.assertIn("Agent", argv)  # the skill's source wave
        settings = json.loads(Path(argv[argv.index("--settings") + 1]).read_text())
        self.assertEqual((settings["sandbox"]["enabled"], settings["sandbox"]["allowUnsandboxedCommands"]), (True, False))
        self.assertIn("~/", settings["sandbox"]["filesystem"]["denyRead"])
        # stricter than the gardener's: no package feed, no Docker socket, whatever the project grants the gardener
        self.assertEqual((settings["sandbox"]["network"]["allowedDomains"], settings["sandbox"]["network"]["allowUnixSockets"]), ([], []))
        self.assertIn(f"audited SHA (your clone's HEAD): {sha}", prompt)
        self.assertIn(f"churn from the baseline {baseline}", prompt)
        self.assertIn("fixes authorized: no: AUDIT_EDIT_PATHS is empty", prompt)
        self.assertEqual((tmp / "head").read_text().strip(), sha)
        self.assertEqual((tmp / "ledger").read_text(), "held\n")
        self.assertTrue(any(name.endswith("-prove.log") for name in (tmp / "logs").read_text().split()))
        self.assertTrue(any(name.endswith("-control.log") for name in (tmp / "logs").read_text().split()))
        self.assertEqual((tmp / "skill").read_text().strip(), "---")
        self.assertEqual((tmp / "gh").read_text().strip(), "unset")

    def test_each_proof_step_hands_the_agent_its_own_evidence_even_when_a_later_step_rewrites_it(self):
        tmp = Path(self.tmp.name)
        self.churn()
        again = "prove-again|mkdir -p .verify/evidence && echo rewritten > .verify/evidence/ledger.jsonl"
        self.audit_config(guard_steps=(self.PROVE, again), cited="prove prove-again")
        self.agent(script=f"""work=$(dirname "$AUDIT_REPORT_FILE"); printf '%s' "$2" > {tmp}/prompt
cat "$work/guard/evidence/prove/.verify/evidence/ledger.jsonl" > {tmp}/first
cat "$work/guard/evidence/prove-again/.verify/evidence/ledger.jsonl" > {tmp}/second""")

        result = self.guard_then_audit()

        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertEqual(((tmp / "first").read_text(), (tmp / "second").read_text()), ("held\n", "rewritten\n"))
        self.assertIn("/guard/evidence/<step>/<path> (paths: .verify/evidence)", (tmp / "prompt").read_text())

    def test_a_cited_step_without_its_kept_evidence_is_refused(self):
        self.churn()
        self.audit_config()
        self.agent()
        self.run_job("nightly-guard", "shop", GUARD_NO_REPORT="1")
        run_id = self.nightly()[-1]["run_id"]
        shutil.rmtree(self.state / "automation" / "shop" / "evidence" / run_id / "control")

        result = self.run_job("daily-audit", "shop")

        self.assertEqual(result.returncode, 1, result.stdout)
        self.assertIn("AUDIT-RESULT: refused - the guard's evidence .verify/evidence after step control is missing", result.stdout)
        self.assertNotIn("claude", self.calls_log())

    @unittest.skipIf(os.geteuid() == 0, "root reads an unreadable file")
    def test_a_partial_evidence_copy_is_not_kept_so_the_audit_refuses(self):
        self.churn()
        partial = ("prove|mkdir -p .verify/evidence && echo held > .verify/evidence/a.txt && echo x > .verify/evidence/b.txt"
                   " && chmod 000 .verify/evidence/b.txt")
        self.audit_config(guard_steps=(partial, "control|chmod 600 .verify/evidence/b.txt"))
        self.agent()

        result = self.guard_then_audit()

        guard = self.nightly()[-1]
        self.assertEqual(guard["status"], "green")
        self.assertIn("evidence of prove not kept: copying .verify/evidence failed", guard["warnings"])
        self.assertEqual(result.returncode, 1, result.stdout)
        self.assertIn("AUDIT-RESULT: refused - the guard's evidence .verify/evidence after step prove is missing", result.stdout)
        self.assertNotIn("claude", self.calls_log())

    def test_a_pass_logs_its_outcome_and_a_clean_one_moves_the_baseline(self):
        sha = self.churn()
        self.audit_config()
        # findings whose issue report failed (gh down) do not move the baseline, so the clean pass still runs
        for outcome, journal_line, env in (("findings", f"audit-verification findings churn at {sha}", {"GH_FAIL": "1"}),
                                           ("clean", f"audit-verification clean (churn) churn at {sha}", {})):
            with self.subTest(outcome=outcome):
                self.agent(outcome)

                result = self.guard_then_audit(**env)

                self.assertEqual(result.returncode, 0, result.stdout)
                entry = self.journal()[-1]
                self.assertEqual(entry["message"], journal_line)
                self.assertEqual(entry["project"], str((self.checkout / ".git").resolve()))  # status lists it under the project
                self.assertEqual(self.audits()[-1]["status"], outcome)
        self.assertEqual(len([n for n in self.notifications() if "kitchen audit" in n]), 1)  # findings, not clean

        again = self.run_job("daily-audit", "shop")  # the clean pass is the new baseline: nothing left to audit

        self.assertEqual(self.audits()[-1]["status"], "skipped", again.stdout)
        self.assertEqual(self.calls_log().count("claude"), 2)

    def test_a_pass_without_an_outcome_or_past_its_deadline_is_incomplete(self):
        self.churn()
        self.audit_config()
        for name, fake, env, reason in (("no outcome", 'echo "claude" >> "$CALLS"', {}, "no valid outcome"),
                                        ("timeout", 'echo "claude" >> "$CALLS"; sleep 30', {"AUDIT_TIMEOUT_SECONDS": "1"}, "timed out after 1s")):
            with self.subTest(name):
                self.fake("claude", fake)

                result = self.guard_then_audit(**env)

                self.assertEqual(result.returncode, 1, result.stdout)
                self.assertEqual(self.audits()[-1]["status"], "incomplete")
                self.assertIn(reason, self.audits()[-1]["detail"])
                self.assertEqual(len(self.journal()), 1)

    def fix_script(self, line="Readers: list every reader.", at="Readers: list them."):
        text = self.MAP.replace(at, line)
        return textwrap.dedent(f"""\
            git checkout -q -b "${{AUDIT_BRANCH_PREFIX}}map"
            cat > docs/map.md <<'MAP'
            {textwrap.indent(text, '            ').strip()}
            MAP
            git commit -q -am "docs: correct the map"
            printf '# Correct the map\\n\\nDrift and how it was checked.\\n' > "$AUDIT_SUMMARY_FILE"
            """)

    def test_a_changed_pass_publishes_a_prose_fix_through_the_gardeners_validation(self):
        sha = self.churn()
        self.audit_config(extra=self.FIXES)
        self.agent("changed", self.fix_script())

        result = self.guard_then_audit()

        branch = f"audit/{datetime.datetime.now(datetime.timezone.utc):%Y-%m-%d}-map"
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertEqual(self.git(self.origin, "rev-parse", f"{branch}^"), sha)
        pr = [line for line in self.calls_log().splitlines() if line.startswith("gh pr create")]
        self.assertEqual(len(pr), 1)
        self.assertIn("--label audit-verification", pr[0])
        body = Path(pr[0].split("--body-file ")[1].split()[0]).read_text()
        self.assertIn("- map: passed", body)  # the job reran AUDIT_VERIFY_STEPS on the tree, as for the gardener
        self.assertEqual(self.audits()[-1]["status"], "changed")
        self.assertEqual(self.journal()[-1]["message"], f"audit-verification changed churn at {sha}")

    def test_a_fix_the_gardeners_validation_or_the_prose_rule_refuses_is_logged_as_findings(self):
        token = "gh" + "p_" + "".join(random.Random(7).choice(string.ascii_letters) for _ in range(36))  # runtime only
        for name, script, reason in (
                ("inside a drive block", self.fix_script("$V api GET /readers --expect 204", "$V api GET /readers --expect 200"),
                 "changes more than prose: docs/map.md:6: inside a fenced code block"),
                ("inside a blockquoted drive block", self.fix_script("> ```bash\n> $V api GET /readers --expect 204\n> ```"),
                 "changes more than prose: docs/map.md:3: inside a fenced code block"),
                ("outside the edit paths", self.fix_script() + "echo note > notes.md && git add notes.md && git commit -qm more\n",
                 "touches notes.md, outside the paths it may edit"),
                ("a planted token", self.fix_script() + f"echo 'key {token}' >> docs/map.md && git commit -qam more\n",
                 "possible secret in the diff")):
            with self.subTest(name):
                self.tearDown(); self.setUp()
                sha = self.churn()
                self.audit_config(extra=self.FIXES)
                self.agent("changed", script)

                result = self.guard_then_audit()

                self.assertEqual(result.returncode, 1, result.stdout)
                self.assertIn(f"AUDIT-RESULT: refused - ", result.stdout)
                self.assertIn(reason, result.stdout)
                self.assertEqual([b for b in self.origin_branches() if b.startswith("audit/")], [])
                self.assertNotIn("gh pr create", self.calls_log())
                self.assertEqual(self.journal()[-1]["message"], f"audit-verification findings churn at {sha}")

    def test_a_clean_outcome_with_a_committed_fix_is_refused_and_logged_as_findings(self):
        sha = self.churn()
        self.audit_config(extra=self.FIXES)
        self.agent("clean", self.fix_script())

        result = self.guard_then_audit()

        self.assertEqual(result.returncode, 1, result.stdout)
        self.assertIn("outcome clean, but the agent committed audit/", result.stdout)
        self.assertNotIn("gh pr create", self.calls_log())
        self.assertEqual(self.journal()[-1]["message"], f"audit-verification findings churn at {sha}")

    def test_changed_without_authorization_is_refused_and_logged_as_findings(self):
        for name, extra, env in (("no edit paths", "", {}), ("an audit PR still open", self.FIXES, {"OPEN_PRS": "1"})):
            with self.subTest(name):
                self.tearDown(); self.setUp()
                sha = self.churn()
                self.audit_config(extra=extra)
                self.agent("changed", self.fix_script())

                result = self.guard_then_audit(**env)

                self.assertEqual(result.returncode, 1, result.stdout)
                self.assertIn("fixes were not authorized", result.stdout)
                self.assertNotIn("gh pr create", self.calls_log())
                self.assertEqual(self.journal()[-1]["message"], f"audit-verification findings churn at {sha}")

    def issue_body(self):
        line = next(line for line in self.calls_log().splitlines() if line.startswith(("gh issue create", "gh issue comment")))
        return Path(line.split("--body-file ")[1].split()[0]).read_text()

    def test_findings_ship_their_prose_fixes_and_land_in_one_issue_that_moves_the_baseline(self):
        sha = self.churn()
        baseline = self.git(self.origin, "rev-parse", "dev~1")
        self.audit_config(extra=self.FIXES)
        self.agent("findings", self.fix_script())

        result = self.guard_then_audit()

        branch = f"audit/{datetime.datetime.now(datetime.timezone.utc):%Y-%m-%d}-map"
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertEqual(self.git(self.origin, "rev-parse", f"{branch}^"), sha)  # published as for changed
        pr = [line for line in self.calls_log().splitlines() if line.startswith("gh pr create")]
        self.assertEqual(len(pr), 1)
        self.assertIn("- map: passed", Path(pr[0].split("--body-file ")[1].split()[0]).read_text())
        run = self.audits()[-1]
        self.assertEqual(run["status"], "findings")
        self.assertIn("report ", run["detail"])
        self.assertIn("PR https://example.test/pull/1", run["detail"])
        self.assertIn("issue #7", run["detail"])
        created = [line for line in self.calls_log().splitlines() if line.startswith("gh issue create")]
        self.assertEqual(len(created), 1)
        self.assertIn("--label audit-verification-findings", created[0])
        body = self.issue_body()
        for fact in ("**findings**", sha, baseline, "https://example.test/pull/1", "Outcome: findings"):
            self.assertIn(fact, body)
        self.assertEqual(self.journal()[-1]["message"], f"audit-verification findings churn at {sha} (issue #7)")
        self.assertNotIn("AUDIT-RESULT: refused", result.stdout)

        again = self.run_job("daily-audit", "shop")  # the filed findings are the new baseline

        self.assertEqual(self.audits()[-1]["status"], "skipped", again.stdout)
        self.assertEqual(self.calls_log().count("claude"), 1)

    def test_an_open_findings_issue_gets_a_comment_and_the_job_never_closes_it(self):
        sha = self.churn()
        self.audit_config(extra='AUDIT_ISSUE_LABEL="audit-findings"\n')
        self.agent("findings")

        result = self.guard_then_audit(OPEN_ISSUE="12")

        self.assertEqual(result.returncode, 0, result.stdout)
        calls = self.calls_log()
        self.assertIn("gh issue list -R owner/shop --label audit-findings --state open", calls)
        self.assertIn("gh issue comment 12 ", calls)
        self.assertNotIn("gh issue create", calls)
        self.assertNotIn("gh issue close", calls)
        self.assertEqual(self.journal()[-1]["message"], f"audit-verification findings churn at {sha} (issue #12)")

    def test_a_fix_the_publication_refuses_under_findings_is_a_warning_and_the_findings_still_land(self):
        sha = self.churn()
        self.audit_config(extra=self.FIXES)
        self.agent("findings", self.fix_script("$V api GET /readers --expect 204", "$V api GET /readers --expect 200"))

        result = self.guard_then_audit()

        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertEqual(result.stdout.count("AUDIT-RESULT:"), 1, result.stdout)
        run = self.audits()[-1]
        self.assertEqual(run["status"], "findings")
        self.assertNotIn("PR ", run["detail"])
        self.assertTrue(any(w.startswith("not published: ") and "changes more than prose" in w for w in run["warnings"]), run)
        self.assertEqual([b for b in self.origin_branches() if b.startswith("audit/")], [])
        self.assertNotIn("gh pr create", self.calls_log())
        self.assertEqual(self.journal()[-1]["message"], f"audit-verification findings churn at {sha} (issue #7)")
        body = self.issue_body()  # the issue says the fixes did not ship, and why
        self.assertIn("- prose fixes: not published", body)
        self.assertIn("changes more than prose", body)

    def test_findings_whose_issue_report_failed_leave_the_baseline_and_warn(self):
        sha = self.churn()
        self.audit_config()
        self.agent("findings")

        result = self.guard_then_audit(GH_FAIL="1")

        self.assertEqual(result.returncode, 0, result.stdout)
        run = self.audits()[-1]
        self.assertEqual(run["status"], "findings")
        self.assertIn("findings issue report failed: the baseline does not move", run["warnings"])
        self.assertEqual(self.journal()[-1]["message"], f"audit-verification findings churn at {sha}")

        again = self.run_job("daily-audit", "shop")  # the same churn is audited again

        self.assertEqual(self.audits()[-1]["status"], "findings", again.stdout)
        self.assertEqual(self.calls_log().count("claude"), 2)

    def test_the_findings_issue_body_is_redacted_and_cut_under_githubs_limit(self):
        token = "ghp_" + "a" * 30  # built at runtime: the repo never holds a credential shape
        self.churn()
        self.audit_config()
        self.agent("findings", f'python3 -c \'print("drift line\\n" * 8000)\' > "$AUDIT_REPORT_FILE.big"\n')
        report = 'printf "# Audit\\n\\nOutcome: findings\\n" > "$AUDIT_REPORT_FILE"'
        claude = (self.bin / "claude").read_text()
        (self.bin / "claude").write_text(claude.replace(report, report + f'\necho "key {token}" >> "$AUDIT_REPORT_FILE"'
                                                        '\ncat "$AUDIT_REPORT_FILE.big" >> "$AUDIT_REPORT_FILE"'))

        result = self.guard_then_audit()

        self.assertEqual(result.returncode, 0, result.stdout)
        body = self.issue_body()
        self.assertLessEqual(len(body), 65536)
        self.assertNotIn(token, body)
        self.assertIn("[REDACTED]", body)
        self.assertIn("Truncated: the full report is at", body)

    def test_a_report_the_agent_links_to_a_host_file_never_leaves_the_machine(self):
        secret = Path(self.tmp.name) / "host-only.txt"  # outside every path the agent may read
        secret.write_text("host-only sentinel\n")
        self.churn()
        self.audit_config()
        for name, plant in (("symlink", f'ln -sf {secret} "$AUDIT_REPORT_FILE"'),
                            ("hard link", f'rm -f "$AUDIT_REPORT_FILE"; ln {secret} "$AUDIT_REPORT_FILE"')):
            with self.subTest(name):
                self.agent("findings")
                claude = (self.bin / "claude").read_text()
                (self.bin / "claude").write_text(claude.replace('echo findings > "$AUDIT_OUTCOME_FILE"',
                                                                f'echo findings > "$AUDIT_OUTCOME_FILE"\n{plant}'))

                result = self.guard_then_audit()

                self.assertEqual(result.returncode, 1, result.stdout)
                self.assertEqual(self.audits()[-1]["status"], "incomplete")
                self.assertIn("not a regular file of its own", self.audits()[-1]["detail"])
                self.assertNotIn("gh issue", self.calls_log())
                self.assertEqual(len(self.journal()), 1)  # only the baseline

    def test_only_a_findings_line_that_names_its_issue_moves_the_baseline(self):
        sha = self.git(self.origin, "rev-parse", "dev")
        for message, moves in ((f"findings churn at {sha} (issue #3)", True), (f"findings churn at {sha}", False),
                               (f"findings churn at {sha} (issue #3) and more", False), (f"findings full at {sha} (issue #3)", False),
                               (f"findings churn at {sha} (issue #0)", False), (f"blocked churn at {sha} (issue #3)", False)):
            with self.subTest(message):
                self.tearDown(); self.setUp()
                self.log_raw(f"audit-verification {message}")

                result = subprocess.run(["python3", str(AUTOMATION / "lib" / "audit_state.py"), "baseline", "shop"],
                                        env=self.job_env(), capture_output=True, text=True)

                self.assertEqual(result.returncode, 0 if moves else 3, result.stderr)
                if moves:
                    self.assertEqual(result.stdout.splitlines()[0], sha)

    def test_busy_project_lock_skips_the_audit_and_records_nothing(self):
        self.audit_config()
        lock = self.state / "automation" / "shop" / "job.lock"
        lock.parent.mkdir(parents=True)

        with open(lock, "w") as held:
            fcntl.flock(held, fcntl.LOCK_EX)
            result = self.run_job("daily-audit", "shop", LOCK_WAIT_SECONDS="1", LOCK_POLL_SECONDS="0.1")

        self.assertEqual(result.returncode, 75, result.stdout)
        self.assertIn("audit skipped: busy", result.stdout)
        self.assertFalse((self.state / "audit" / "shop.jsonl").exists())


class ProseOnlyTests(unittest.TestCase):
    """Which lines of a Markdown file the audit's prose gate keeps as code: every line a fence may hold."""

    def code_lines(self, text):
        import importlib.util
        spec = importlib.util.spec_from_file_location("prose_only", AUTOMATION / "lib" / "prose_only.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return sorted(module.code_lines(textwrap.dedent(text)))

    def test_fences_count_at_the_top_level_and_nested_in_quotes_and_lists(self):
        self.assertEqual(self.code_lines("""\
            prose
            ```bash
            drive
            ```
            > ```
            > drive
            > ```
            - ```
              drive
              ```
            prose
            """), [2, 3, 4, 5, 6, 7, 8, 9, 10])

    def test_a_line_that_only_looks_like_a_closing_fence_keeps_the_block_open(self):
        # inside a quoted fence, "> - ```" is content, not the quote's closing fence (round 2 of the review)
        self.assertEqual(self.code_lines("""\
            > ```bash
            > - ```
            > drive
            > ```
            prose
            """), [1, 2, 3, 4])
        # four spaces of indentation inside a top-level fence is content too
        self.assertEqual(self.code_lines("```\n    ```\ndrive\n```\nprose\n"), [1, 2, 3, 4])


@unittest.skipUnless(platform.system() == "Darwin", "launchd is the macOS schedule")
class MacScheduleTests(AutomationFixture):
    def test_scheduled_jobs_run_at_standard_priority_not_throttled_as_background(self):
        self.configure([])
        self.fake("launchctl", 'echo "launchctl $*" >> "$CALLS"')
        self.fake("npm", 'echo "npm $*" >> "$CALLS"')
        home = Path(self.tmp.name) / "home"
        env = {**self.job_env(), "HOME": str(home), "PATH": f"{self.bin}:{os.environ['PATH']}"}

        result = subprocess.run(["bash", str(AUTOMATION / "bin" / "install-schedule"), "shop"], env=env,
                                capture_output=True, text=True)

        self.assertEqual(result.returncode, 0, result.stderr)
        plists = sorted((home / "Library" / "LaunchAgents").glob("*.plist"))
        self.assertEqual([p.name for p in plists], ["com.kitchen.shop.nightly-guard.plist", "com.kitchen.shop.weekly-gardener.plist"])
        for plist in plists:
            text = plist.read_text()
            self.assertIn("<key>ProcessType</key><string>Standard</string>", text, plist.name)
            self.assertNotIn("Background", text, plist.name)


    def test_guard_alone_schedules_only_the_nightly_guard_and_installs_no_srt(self):
        self.configure([])
        self.fake("launchctl", 'echo "launchctl $*" >> "$CALLS"')
        self.fake("npm", 'echo "npm $*" >> "$CALLS"')
        home = Path(self.tmp.name) / "home"
        env = {**self.job_env(), "HOME": str(home), "PATH": f"{self.bin}:{os.environ['PATH']}"}

        result = subprocess.run(["bash", str(AUTOMATION / "bin" / "install-schedule"), "--guard", "shop", "3"], env=env,
                                capture_output=True, text=True)

        self.assertEqual(result.returncode, 0, result.stderr)
        plists = sorted((home / "Library" / "LaunchAgents").glob("*.plist"))
        self.assertEqual([p.name for p in plists], ["com.kitchen.shop.nightly-guard.plist"])
        self.assertIn("<key>Hour</key><integer>3</integer>", plists[0].read_text())
        self.assertNotIn("npm", self.calls_log())

@unittest.skipUnless(platform.system() == "Linux", "systemd user timers are the Linux schedule")
class LinuxScheduleTests(AutomationFixture):
    def install(self, *args, linger="yes"):
        self.fake("loginctl", f'echo "loginctl $*" >> "$CALLS"; echo {linger}')
        self.fake("systemctl", 'echo "systemctl $*" >> "$CALLS"')
        self.fake("npm", 'echo "npm $*" >> "$CALLS"')
        home = Path(self.tmp.name) / "home"
        env = {**self.job_env(), "HOME": str(home), "XDG_CONFIG_HOME": str(home / ".config"),
               "PATH": f"{self.bin}:{os.environ['PATH']}"}
        result = subprocess.run(["bash", str(AUTOMATION / "bin" / "install-schedule"), *args], env=env,
                                capture_output=True, text=True)
        return result, home / ".config" / "systemd" / "user"

    def test_gardener_alone_gets_a_weekly_timer_and_a_private_tmp(self):
        self.configure([])

        result, units = self.install("--gardener", "shop", "1", "6")

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(sorted(p.name for p in units.iterdir()),
                         ["kitchen-shop-weekly-gardener.service", "kitchen-shop-weekly-gardener.timer"])
        service = (units / "kitchen-shop-weekly-gardener.service").read_text()
        self.assertIn("PrivateTmp=yes", service)
        self.assertIn(f"ExecStart=/bin/bash {AUTOMATION}/bin/weekly-gardener shop", service)
        self.assertIn("OnCalendar=Mon *-*-* 06:00:00", (units / "kitchen-shop-weekly-gardener.timer").read_text())
        self.assertIn("systemctl --user enable --now kitchen-shop-weekly-gardener.timer", self.calls_log())

    def test_audit_gets_a_daily_timer_after_the_guard_without_catch_up(self):
        self.configure([])

        result, units = self.install("--audit", "shop")

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(sorted(p.name for p in units.iterdir()), ["kitchen-shop-daily-audit.service", "kitchen-shop-daily-audit.timer"])
        service = (units / "kitchen-shop-daily-audit.service").read_text()
        self.assertIn("PrivateTmp=yes", service)
        self.assertIn(f"ExecStart=/bin/bash {AUTOMATION}/bin/daily-audit shop", service)
        timer = (units / "kitchen-shop-daily-audit.timer").read_text()
        self.assertIn("OnCalendar=*-*-* 04:00:00", timer)
        self.assertNotIn("Persistent", timer)  # a missed run waits for the next one

    def test_refuses_a_path_systemd_would_read_differently(self):
        self.configure([])
        odd = Path(self.tmp.name) / "kitchen 100%"
        shutil.copytree(AUTOMATION, odd / "automation", ignore=shutil.ignore_patterns("node_modules"))
        self.fake("loginctl", "echo yes")
        self.fake("systemctl", 'echo "systemctl $*" >> "$CALLS"')
        self.fake("npm", "true")
        (odd / "automation" / "node_modules" / ".bin").mkdir(parents=True)
        (odd / "automation" / "node_modules" / ".bin" / "srt").write_text("#!/bin/sh\necho 0.0.78\n")
        (odd / "automation" / "node_modules" / ".bin" / "srt").chmod(0o755)
        home = Path(self.tmp.name) / "home"
        env = {**self.job_env(), "HOME": str(home), "XDG_CONFIG_HOME": str(home / ".config"),
               "PATH": f"{self.bin}:{os.environ['PATH']}"}

        result = subprocess.run(["bash", str(odd / "automation" / "bin" / "install-schedule"), "--gardener", "shop"],
                                env=env, capture_output=True, text=True)

        self.assertEqual(result.returncode, 2, result.stdout)
        self.assertIn("cannot pass", result.stderr)
        self.assertNotIn("enable", self.calls_log())

    def test_refuses_without_lingering(self):
        self.configure([])

        result, units = self.install("--gardener", "shop", linger="no")

        self.assertEqual(result.returncode, 2)
        self.assertIn("loginctl enable-linger", result.stderr)
        self.assertFalse(units.exists())


class ShimTests(AutomationFixture):
    def test_gh_shim_blocks_merging_and_passes_everything_else(self):
        env = {**clean_env(), "KITCHEN_REAL_GH": str(self.bin / "gh"), "CALLS": str(self.calls)}
        shim = str(AUTOMATION / "shims" / "gh")

        merge = subprocess.run([shim, "pr", "merge", "7"], env=env, capture_output=True, text=True)
        view = subprocess.run([shim, "pr", "view", "7"], env=env, capture_output=True, text=True)

        self.assertEqual(merge.returncode, 1)
        self.assertIn("blocked", merge.stderr)
        self.assertEqual(view.returncode, 0)
        self.assertEqual(self.calls_log().strip(), "gh pr view 7")


if __name__ == "__main__":
    unittest.main()


class SuperviseStopGroupTests(unittest.TestCase):
    """The supervisor stops a finished job's process group, and must not crash when the OS refuses a signal."""

    def load(self):
        import importlib.util
        spec = importlib.util.spec_from_file_location("supervise", AUTOMATION / "lib" / "supervise.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        module.DRAIN = 0
        return module

    def test_a_group_that_refuses_the_signal_does_not_crash_the_supervisor(self):
        supervise = self.load()
        sent = []

        def killpg(pgid, sig):
            sent.append(sig)
            if sig == 0:
                if len([s for s in sent if s != 0]) >= 2:  # gone once TERM and KILL were attempted
                    raise ProcessLookupError
                return
            raise PermissionError(1, "Operation not permitted")

        original = supervise.os.killpg
        supervise.os.killpg = killpg
        try:
            supervise.stop_group(4242, 0.01)  # raised PermissionError before the fix
        finally:
            supervise.os.killpg = original

        self.assertIn(signal.SIGTERM, sent)
        self.assertIn(signal.SIGKILL, sent)  # escalation continued past the refused TERM
