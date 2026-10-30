import datetime
import json
import os
import subprocess
import sys
import unittest
from pathlib import Path
from unittest import mock

from tests.test_kitchen import KITCHEN, KitchenFixture


def git(repo, *args):
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)


def local_ts(days_ago, hour=3, minute=0):
    """A timestamp `days_ago` days back at a local hour, as the nightly writes it."""
    day = datetime.datetime.now().astimezone() - datetime.timedelta(days=days_ago)
    return day.replace(hour=hour, minute=minute, second=0, microsecond=0).isoformat()


def ago(**delta):
    return (datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(**delta)).isoformat(timespec="seconds")


class ProjectFixture(KitchenFixture):
    """Fake notifiers on PATH record what would be shown: a test never pops a real notification."""

    def setUp(self):
        super().setUp()
        self.fakebin = Path(self.tmp.name) / "fakebin"
        self.fakebin.mkdir()
        self.notified = Path(self.tmp.name) / "notified.log"
        for notifier in ("osascript", "notify-send"):
            self.fake(notifier, f'echo "$(basename "$0") $*" >> "{self.notified}"')
        self.extra_env = {"KITCHEN_AGENT": "claude", "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t",
                          "GIT_COMMITTER_EMAIL": "t@t", "PATH": f"{self.fakebin}{os.pathsep}{os.environ['PATH']}"}

    def fake(self, name, body):
        path = self.fakebin / name
        path.write_text(f"#!/usr/bin/env bash\n{body}\n")
        path.chmod(0o755)

    def notifications(self):
        return self.notified.read_text().splitlines() if self.notified.exists() else []

    def make_project(self, name="shop"):
        project = self.home / "work" / name
        project.mkdir(parents=True)
        git(project, "init", "-q", "-b", "main")
        (project / "README.md").write_text("x\n")
        git(project, "add", ".")
        git(project, "commit", "-q", "-m", "init")
        return project

    def list_projects(self, *paths):
        (self.home / "config").mkdir(exist_ok=True)
        (self.home / "config" / "projects.txt").write_text("# projects\n" + "\n".join(str(p) for p in paths) + "\n")

    def configure_base(self, base, project="shop", extra=""):
        (self.home / "config").mkdir(exist_ok=True)
        (self.home / "config" / "integrate.toml").write_text(f'[projects.{project}]\nbase = "{base}"\nchecks = ["true"]\n{extra}')

    def commit(self, project, name, text, message="change"):
        (project / name).write_text(text)
        git(project, "add", name)
        git(project, "commit", "-q", "-m", message)
        return self.rev(project, "HEAD")

    def rev(self, project, ref):
        return subprocess.run(["git", "-C", str(project), "rev-parse", ref], capture_output=True, text=True, check=True).stdout.strip()

    def configure_automation(self, project="shop", text='GUARD_STEPS=("tests|true")\nGARDENER_VERIFY_STEPS=("tests|true")\n'):
        """The project's automation env: its existence configures the nightly, GARDENER_VERIFY_STEPS the gardener."""
        folder = self.home / "config" / "automation"
        folder.mkdir(parents=True, exist_ok=True)
        (folder / f"{project}.env").write_text(text)
        return folder / f"{project}.env"

    def write_record(self, kind, runs, project="shop"):
        folder = self.home / "state" / kind
        folder.mkdir(parents=True, exist_ok=True)
        (folder / f"{project}.jsonl").write_text("".join(json.dumps(r) + "\n" for r in runs))


class LogAndStatusTests(ProjectFixture):
    def test_log_records_repo_branch_sha_and_agent(self):
        project = self.make_project()

        result = self.kitchen("log", "migrated the cursor", "--status", "done", cwd=project)

        entry = json.loads((self.home / "state" / "log.jsonl").read_text().splitlines()[-1])
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(Path(entry["repo"]).resolve(), project.resolve())
        self.assertEqual((entry["branch"], entry["agent"], entry["status"]), ("main", "claude", "done"))
        self.assertTrue(entry["sha"])

    def test_log_records_the_session_of_the_agent_that_logged(self):
        project = self.make_project()

        sessions = {"CODEX_SESSION_ID": "codex-123", "CLAUDE_CODE_SESSION_ID": "claude-456"}
        self.extra_env = {**self.extra_env, **sessions, "KITCHEN_AGENT": "codex"}
        self.kitchen("log", "from codex", cwd=project)
        self.extra_env["KITCHEN_AGENT"] = "claude"
        self.kitchen("log", "from claude", cwd=project)

        entries = [json.loads(line) for line in (self.home / "state" / "log.jsonl").read_text().splitlines()]
        self.assertEqual([(e["agent"], e["session"]) for e in entries], [("codex", "codex-123"), ("claude", "claude-456")])

    def test_status_flags_what_an_older_install_left_until_install_moves_it(self):
        project = self.make_project()
        self.list_projects(project)
        old = self.home / ".claude" / "CLAUDE.md"
        old.parent.mkdir(parents=True)
        old.write_text("<!-- Generated by `kitchen install` from somewhere. -->\n")

        before = self.kitchen("status", "--exceptions")
        self.kitchen("install")
        after = self.kitchen("status", "--exceptions")

        self.assertEqual(before.returncode, 1)
        self.assertIn("kitchen  setup      ", before.stdout)
        self.assertIn("CLAUDE.md was written by an older kitchen install", before.stdout)
        self.assertNotIn("kitchen  setup", after.stdout)

    def test_status_shows_blocked_entries_first_and_owed_decisions(self):
        project = self.make_project()
        self.kitchen("log", "step one done", "--status", "done", cwd=project)
        self.kitchen("log", "needs the prod password", "--status", "blocked", cwd=project)
        self.commit(project, "decisions.md", "- [x] 2026-10-01 use uuids later\n- [ ] keep the old route?\n- [ ] drop the table?\n")
        self.configure_base("main")
        self.list_projects(project)

        out = self.kitchen("status").stdout

        self.assertIn("2 entries, 1 blocked", out)
        self.assertLess(out.index("needs the prod password"), out.index("step one done"))
        self.assertIn("2 owed by you", out)

    def test_status_reports_nightly_green_streak_and_failed_step(self):
        project = self.make_project("shop")
        self.configure_automation()
        nightly = self.home / "state" / "nightly"
        nightly.mkdir(parents=True)
        t3 = ago(hours=2)
        runs = [{"ts": local_ts(2), "sha": "a", "status": "red", "failed_step": "tests"}, {"ts": local_ts(1), "sha": "b", "status": "green"},
                {"ts": t3, "sha": "c", "status": "green"}]
        (nightly / "shop.jsonl").write_text("".join(json.dumps(r) + "\n" for r in runs))

        out = self.kitchen("status", str(project)).stdout

        self.assertIn(f"green at {t3} on c", out)  # not ✓: short SHAs and no base leave the distance to the base unknown
        self.assertIn("green streak 2 nights", out)
        self.assertNotIn("overdue", out)

    def test_status_shows_unknown_when_git_status_fails(self):
        project = self.make_project()
        (project / ".git" / "index").write_bytes(b"not an index")

        out = self.kitchen("status", str(project)).stdout

        self.assertIn("dirty: unknown", out)

    def test_status_matches_nightly_by_full_sha_and_shows_its_warnings(self):
        project = self.make_project("shop")
        head = subprocess.run(["git", "-C", str(project), "rev-parse", "HEAD"], capture_output=True, text=True, check=True).stdout.strip()
        self.configure_automation()
        nightly = self.home / "state" / "nightly"
        nightly.mkdir(parents=True)
        run = {"ts": "t1", "sha": head, "status": "green", "warnings": ["cleanup failed", "metrics skipped"]}
        (nightly / "shop.jsonl").write_text(json.dumps(run) + "\n")

        out = self.kitchen("status", str(project)).stdout

        self.assertIn(f"on {head[:8]} (= HEAD)", out)
        self.assertNotIn(head, out)
        self.assertIn("warnings: cleanup failed; metrics skipped", out)

    def test_status_never_matches_a_short_nightly_sha(self):
        project = self.make_project("shop")
        head = subprocess.run(["git", "-C", str(project), "rev-parse", "HEAD"], capture_output=True, text=True, check=True).stdout.strip()
        self.configure_automation()
        nightly = self.home / "state" / "nightly"
        nightly.mkdir(parents=True)
        (nightly / "shop.jsonl").write_text(json.dumps({"ts": "t1", "sha": head[:8], "status": "green"}) + "\n")

        out = self.kitchen("status", str(project)).stdout

        self.assertIn(f"on {head[:8]} ·", out)

    def test_checkpoint_logged_from_a_worktree_shows_under_its_repo(self):
        project = self.make_project("shop")
        worktree = self.home / "work" / "shop-feature"
        git(project, "worktree", "add", "-q", "-b", "feature", str(worktree))
        self.kitchen("log", "worktree step done", "--status", "done", cwd=worktree)

        out = self.kitchen("status", str(project)).stdout

        self.assertIn("worktree step done", out)

    def test_status_never_reports_prs_it_could_not_read_as_none(self):
        project = self.make_project()

        out = self.kitchen("status", str(project)).stdout

        self.assertIn("PRs        unknown:", out)

    def test_status_accepts_a_configured_project_name(self):
        project = self.make_project("shop")
        self.list_projects(project)

        out = self.kitchen("status", "shop", cwd=self.home).stdout

        self.assertIn("shop  main @", out)

    def test_status_reports_a_missing_project_instead_of_skipping_it(self):
        self.list_projects(self.home / "gone")

        out = self.kitchen("status").stdout

        self.assertIn("gone  ✗ path does not exist", out)


    def test_status_reads_decisions_from_the_base_ref_and_names_it(self):
        project = self.make_project("shop")
        git(project, "checkout", "-q", "-b", "dev")
        dev = self.commit(project, "decisions.md", "- [ ] keep the old route?\n- [ ] drop the table?\n")
        git(project, "checkout", "-q", "main")
        self.configure_base("dev")

        out = self.kitchen("status", str(project)).stdout

        self.assertIn(f"decisions  2 owed by you (read from dev @ {dev[:8]})", out)

    def test_status_without_a_base_reports_decisions_not_configured_instead_of_reading_the_checkout(self):
        project = self.make_project("shop")
        (project / "decisions.md").write_text("- [ ] keep the old route?\n")

        out = self.kitchen("status", str(project)).stdout

        self.assertIn("decisions  not configured (no base for 'shop'", out)
        self.assertNotIn("owed", out)

    def test_status_shows_how_far_the_nightly_sha_is_behind_the_base(self):
        project = self.make_project("shop")
        tested = self.rev(project, "HEAD")
        for number in range(3):
            self.commit(project, f"f{number}", "x\n")
        self.configure_base("main")
        self.configure_automation()
        self.write_record("nightly", [{"ts": ago(hours=1), "sha": tested, "status": "green"}])

        out = self.kitchen("status", str(project)).stdout

        self.assertIn("3 behind main", out)

    def test_status_shows_entries_of_no_listed_project_as_unattached(self):
        project = self.make_project("shop")
        other = self.make_project("lab")
        self.kitchen("log", "logged outside any repo", "--status", "blocked", cwd=self.home)
        self.kitchen("log", "logged in an unlisted repo", cwd=other)
        self.kitchen("log", "logged in shop", cwd=project)
        self.list_projects(project)

        out = self.kitchen("status").stdout

        unattached = out[out.index("unattached"):]
        self.assertIn("unattached  2 entries, 1 blocked", unattached)
        self.assertIn("logged outside any repo", unattached)
        self.assertIn("logged in an unlisted repo", unattached)
        self.assertNotIn("logged in shop", unattached)

    def test_status_bounds_the_gh_call_and_prints_unknown_when_it_times_out(self):
        project = self.make_project("shop")
        self.fake("gh", "sleep 10; echo '[]'")

        result = self.kitchen("status", str(project), env_extra={"KITCHEN_GH_TIMEOUT_SECONDS": "1"})

        self.assertIn("PRs        unknown: gh timed out after 1s", result.stdout)
        self.assertNotIn("PRs        none", result.stdout)

    def test_status_counts_the_green_streak_in_nights_and_marks_an_old_nightly_overdue(self):
        project = self.make_project("shop")
        runs = [{"ts": local_ts(4), "sha": "a", "status": "red", "failed_step": "tests"},
                {"ts": local_ts(3), "sha": "b", "status": "green"},
                {"ts": local_ts(2, hour=3), "sha": "c", "status": "green"},
                {"ts": local_ts(2, hour=5), "sha": "c", "status": "green"}]  # a rerun the same night
        self.configure_automation()
        self.write_record("nightly", runs)

        out = self.kitchen("status", str(project)).stdout

        self.assertIn("green streak 2 nights", out)
        self.assertIn("overdue", out)
        self.assertIn("nightly    ✗ green at", out)

    def test_status_shows_the_gardeners_last_result_and_its_age(self):
        project = self.make_project("shop")
        self.configure_automation()
        before = self.kitchen("status", str(project)).stdout
        self.write_record("gardener", [{"ts": ago(days=8), "status": "published", "detail": "https://example.test/pull/1"},
                                       {"ts": ago(hours=2, minutes=5), "status": "refused", "detail": "no independent verification configured"}])

        after = self.kitchen("status", str(project)).stdout

        self.assertIn("gardener   ✗ no record on this host (GARDENER_VERIFY_STEPS in", before)
        self.assertIn("gardener   ✗ refused: no independent verification configured · 2h 5m ago", after)

    def green_project(self):
        project = self.make_project("shop")
        self.fake("gh", "echo '[]'")
        self.configure_base("main")
        self.configure_automation()
        self.write_record("nightly", [{"ts": ago(hours=1), "sha": self.rev(project, "HEAD"), "status": "green"}])
        self.write_record("gardener", [{"ts": ago(days=1), "status": "published", "detail": "https://example.test/pull/1"}])
        self.list_projects(project)
        return project

    def test_status_exceptions_prints_only_non_green_lines_and_exits_1_when_any(self):
        project = self.green_project()
        green = self.kitchen("status", "--exceptions")
        self.write_record("nightly", [{"ts": ago(hours=1), "sha": self.rev(project, "HEAD"), "status": "red", "failed_step": "tests"}])
        self.kitchen("log", "needs the prod password", "--status", "blocked", cwd=self.home)

        red = self.kitchen("status", "--exceptions")

        self.assertEqual((green.returncode, green.stdout), (0, ""), green.stderr)
        self.assertEqual(red.returncode, 1, red.stdout + red.stderr)
        lines = red.stdout.splitlines()
        self.assertTrue(any(line.startswith("shop  nightly    ✗ red") for line in lines), red.stdout)
        self.assertTrue(any(line.startswith("unattached") and "needs the prod password" in line for line in lines), red.stdout)
        for green_line in ("PRs", "gardener", "decisions", "journal window"):
            self.assertNotIn(green_line, red.stdout)

    def test_a_remote_nightly_is_informational_never_green_and_never_an_exception(self):
        self.green_project()
        (self.home / "state" / "nightly" / "shop.jsonl").unlink()  # its records live on the other host
        self.configure_base("main", extra='nightly = "remote:build-host"\n')

        remote = self.kitchen("status", "--exceptions")
        out = self.kitchen("status").stdout
        self.configure_base("main", extra='nightly = "elsewhere"\n')
        malformed = self.kitchen("status", "--exceptions")
        self.configure_base("main")
        local = self.kitchen("status", "--exceptions")

        self.assertEqual((remote.returncode, remote.stdout), (0, ""), remote.stdout + remote.stderr)
        self.assertIn("  nightly    remote (build-host): not read here", out)
        self.assertEqual(malformed.returncode, 1, malformed.stdout)
        self.assertIn("shop  nightly    unknown: nightly = 'elsewhere'", malformed.stdout)
        self.assertEqual(local.returncode, 1, local.stdout)
        self.assertIn("shop  nightly    ✗ no record", local.stdout)

    def test_a_remote_gardener_is_informational_never_green_and_never_an_exception(self):
        self.green_project()
        (self.home / "state" / "gardener" / "shop.jsonl").unlink()
        self.configure_base("main", extra='gardener = "remote:build-host"\n')

        remote = self.kitchen("status", "--exceptions")
        out = self.kitchen("status").stdout
        self.configure_base("main", extra='gardener = "elsewhere"\n')
        malformed = self.kitchen("status", "--exceptions")
        self.configure_base("main")
        local = self.kitchen("status", "--exceptions")

        self.assertEqual((remote.returncode, remote.stdout), (0, ""), remote.stdout + remote.stderr)
        self.assertIn("  gardener   remote (build-host): not read here", out)
        self.assertNotIn("✓", out[out.index("gardener"):].splitlines()[0])
        self.assertEqual(malformed.returncode, 1, malformed.stdout)
        self.assertIn("shop  gardener   unknown: gardener = 'elsewhere'", malformed.stdout)
        self.assertEqual(local.returncode, 1, local.stdout)
        self.assertIn("shop  gardener   ✗ no record on this host", local.stdout)

    def test_a_nightly_whose_sha_is_not_in_the_repo_is_not_green(self):
        self.green_project()
        self.write_record("nightly", [{"ts": ago(hours=1), "sha": "f" * 40, "status": "green"}])

        result = self.kitchen("status", "--exceptions")

        self.assertEqual(result.returncode, 1, result.stdout)
        self.assertIn("shop  nightly    ✗ green", result.stdout)
        self.assertIn("behind base: unknown", result.stdout)

    def test_a_gardener_record_with_an_unreadable_time_is_not_green(self):
        self.green_project()
        self.write_record("gardener", [{"ts": "yesterday", "status": "published", "detail": "https://example.test/pull/1"}])

        result = self.kitchen("status", "--exceptions")

        self.assertEqual(result.returncode, 1, result.stdout)
        self.assertIn("shop  gardener   ✗ published: https://example.test/pull/1 · unknown ago", result.stdout)

    def test_a_local_gardener_record_older_than_a_week_and_a_day_is_overdue_unless_remote(self):
        self.green_project()
        old = [{"ts": ago(days=8, hours=1), "status": "published", "detail": "https://example.test/pull/1"}]
        self.write_record("gardener", [{"ts": ago(days=7, hours=23), "status": "none", "detail": "no gardener branch was committed"}])
        within = self.kitchen("status", "--exceptions")
        self.write_record("gardener", old)
        late = self.kitchen("status", "--exceptions")
        self.configure_base("main", extra='gardener = "remote:build-host"\n')
        remote = self.kitchen("status", "--exceptions")

        self.assertEqual((within.returncode, within.stdout), (0, ""), within.stdout + within.stderr)
        self.assertEqual(late.returncode, 1, late.stdout + late.stderr)
        self.assertIn("shop  gardener   ✗ published: https://example.test/pull/1 · 8d 1h ago"
                      " · overdue: last record 8d 1h ago, cadence weekly plus a day", late.stdout)
        self.assertEqual((remote.returncode, remote.stdout), (0, ""), remote.stdout + remote.stderr)

    AUDIT_ENV = 'GUARD_STEPS=("tests|true")\nGARDENER_VERIFY_STEPS=("tests|true")\nAUDIT_GUARD_STEPS=(prove control)\n'

    def audit_project(self, **record):
        self.green_project()
        env = self.configure_automation(text=self.AUDIT_ENV)
        self.write_audit(**record)
        return env

    def write_audit(self, **record):
        """An older run of the other kind first: status reads the last line, never an earlier one."""
        older = {"ts": ago(days=1, hours=2), "status": "findings" if record.get("status", "clean") in ("clean", "changed", "skipped") else "clean",
                 "detail": "report /old/report.md", "sha": "0" * 40, "baseline": "f" * 40, "warnings": []}
        self.write_record("audit", [older, {"ts": ago(hours=2, minutes=5), "status": "clean", "detail": "report /r/report.md",
                                            "sha": "ab12cd34" + "0" * 32, "baseline": "f" * 40, "warnings": [], **record}])

    def test_status_shows_the_audits_last_outcome_and_only_the_good_ones_are_green(self):
        self.audit_project()
        for outcome in ("clean", "changed", "skipped"):
            with self.subTest(outcome=outcome):
                self.write_audit(status=outcome)

                result = self.kitchen("status", "--exceptions")
                out = self.kitchen("status").stdout

                self.assertEqual((result.returncode, result.stdout), (0, ""), result.stdout + result.stderr)
                self.assertIn(f"  audit      ✓ {outcome} on ab12cd34: report /r/report.md · 2h 5m ago\n", out)
        for outcome in ("findings", "blocked", "refused", "incomplete", "running"):
            with self.subTest(outcome=outcome):
                self.write_audit(status=outcome, warnings=["not notified: see the log"])

                result = self.kitchen("status", "--exceptions")

                self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
                self.assertEqual(result.stdout.splitlines(), [
                    f"shop  audit      ✗ {outcome} on ab12cd34: report /r/report.md · 2h 5m ago · warnings: not notified: see the log"])

    def test_a_refused_audit_without_a_sha_shows_its_reason(self):
        self.audit_project(status="refused", sha=None, detail="no nightly guard run is recorded; the agent did not run")

        result = self.kitchen("status", "--exceptions")

        self.assertEqual(result.stdout.splitlines(),
                         ["shop  audit      ✗ refused: no nightly guard run is recorded; the agent did not run · 2h 5m ago"])

    def test_an_audit_record_older_than_two_days_or_with_an_unreadable_time_is_not_green(self):
        self.audit_project(ts=ago(days=1, hours=23))
        within = self.kitchen("status", "--exceptions")
        within_json = json.loads(self.kitchen("status", "--json").stdout)["projects"][0]["audit"]
        self.write_audit(ts=ago(days=2, hours=1))
        late = self.kitchen("status", "--exceptions")
        late_json = json.loads(self.kitchen("status", "--json").stdout)["projects"][0]["audit"]
        self.write_audit(ts="yesterday")
        unreadable = self.kitchen("status", "--exceptions")

        self.assertEqual((within.returncode, within.stdout), (0, ""), within.stdout + within.stderr)
        self.assertEqual((within_json["status"], within_json["overdue"]), ("clean", False))
        self.assertEqual(late.returncode, 1, late.stdout + late.stderr)
        self.assertEqual(late.stdout.splitlines(), ["shop  audit      ✗ clean on ab12cd34: report /r/report.md · 2d 1h ago"
                                                    " · overdue: last record 2d 1h ago, cadence daily plus a day"])
        self.assertEqual((late_json["status"], late_json["overdue"]), ("clean", True))
        self.assertEqual(unreadable.stdout.splitlines(),
                         ["shop  audit      ✗ clean on ab12cd34: report /r/report.md · unknown ago"])

    def test_an_audit_is_configured_only_by_audit_guard_steps_and_then_needs_its_record(self):
        env = self.audit_project(status="findings")
        for text in (self.AUDIT_ENV.replace("AUDIT_GUARD_STEPS=(prove control)", "AUDIT_GUARD_STEPS=()"),
                     self.AUDIT_ENV.replace("AUDIT_GUARD_STEPS=(prove control)", '# AUDIT_GUARD_STEPS=(prove control)')):
            with self.subTest(text=text):
                self.configure_automation(text=text)

                result = self.kitchen("status", "--exceptions")
                out = self.kitchen("status").stdout

                self.assertEqual((result.returncode, result.stdout), (0, ""), result.stdout + result.stderr)
                self.assertIn(f"  audit      not configured (AUDIT_GUARD_STEPS is empty or unset in {env})", out)
        self.configure_automation(text=self.AUDIT_ENV)
        (self.home / "state" / "audit" / "shop.jsonl").unlink()

        missing = self.kitchen("status", "--exceptions")

        self.assertEqual(missing.returncode, 1, missing.stdout + missing.stderr)
        self.assertEqual(missing.stdout.splitlines(), [f"shop  audit      ✗ no record (AUDIT_GUARD_STEPS in {env})"])

    def test_an_unreadable_integrate_toml_does_not_hide_or_invent_the_audit(self):
        env = self.audit_project(status="findings")
        (self.home / "config" / "integrate.toml").write_text("[projects.shop\n")

        configured = self.kitchen("status", "--exceptions")
        self.configure_automation(text=self.AUDIT_ENV.replace("AUDIT_GUARD_STEPS=(prove control)", "AUDIT_GUARD_STEPS=()"))
        unconfigured = self.kitchen("status").stdout

        self.assertIn("shop  audit      ✗ findings on ab12cd34: report /r/report.md · 2h 5m ago\n", configured.stdout)
        self.assertIn(f"  audit      not configured (AUDIT_GUARD_STEPS is empty or unset in {env})", unconfigured)

    def test_an_unconfigured_nightly_gardener_and_decisions_are_informational_never_green_and_never_exceptions(self):
        project = self.make_project("shop")
        self.fake("gh", "echo '[]'")
        self.list_projects(project)

        result = self.kitchen("status", "--exceptions")
        out = self.kitchen("status").stdout

        self.assertEqual((result.returncode, result.stdout), (0, ""), result.stdout + result.stderr)
        automation = self.home / "config" / "automation"
        self.assertIn(f"  nightly    not configured (no {automation / 'shop.env'})", out)
        self.assertIn(f"  gardener   not configured (no {automation / 'shop.env'}"
                      f" and no gardener in {self.home / 'config' / 'integrate.toml'})", out)
        self.assertIn(f"  audit      not configured (no {automation / 'shop.env'})", out)
        self.assertIn(f"  decisions  not configured (no base for 'shop' in {self.home / 'config' / 'integrate.toml'})", out)
        self.assertNotIn("✓", out)

    def test_a_configured_capability_without_its_evidence_stays_an_exception_next_to_an_unconfigured_project(self):
        project = self.make_project("shop")
        bare = self.make_project("lab")
        self.fake("gh", "echo '[]'")
        env = self.configure_automation()
        self.configure_base("origin/nowhere")
        self.list_projects(project, bare)

        result = self.kitchen("status", "--exceptions")

        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertEqual(result.stdout.splitlines(), [
            f"shop  nightly    ✗ no record (GUARD_STEPS in {env})",
            f"shop  gardener   ✗ no record on this host (GARDENER_VERIFY_STEPS in {env}; if it runs on another host,"
            f" set gardener = \"remote:<host-label>\" in {self.home / 'config' / 'integrate.toml'})",
            "shop  decisions  unknown: cannot resolve origin/nowhere in this repo",
        ])

    def test_records_without_config_are_not_configured_and_with_config_are_green(self):
        self.green_project()
        (self.home / "config" / "automation" / "shop.env").unlink()
        (self.home / "config" / "integrate.toml").unlink()

        unconfigured = self.kitchen("status").stdout
        quiet = self.kitchen("status", "--exceptions")
        self.configure_automation()
        self.configure_base("main")
        configured = self.kitchen("status").stdout

        self.assertIn("  nightly    not configured", unconfigured)
        self.assertIn("  gardener   not configured", unconfigured)
        self.assertIn("  decisions  not configured", unconfigured)
        self.assertNotIn("✓", unconfigured)
        self.assertEqual((quiet.returncode, quiet.stdout), (0, ""), quiet.stdout + quiet.stderr)
        self.assertIn("  nightly    ✓ green", configured)
        self.assertIn("  gardener   ✓ published", configured)
        self.assertIn("  decisions  no decisions.md at main", configured)

    def test_an_empty_guard_steps_leaves_the_nightly_not_configured_on_a_gardener_only_host(self):
        project = self.make_project("shop")
        self.fake("gh", "echo '[]'")
        self.configure_base("main")
        env = self.configure_automation(text='GUARD_STEPS=()\nGARDENER_VERIFY_STEPS=("tests|true")\n')
        self.list_projects(project)

        result = self.kitchen("status", "--exceptions")

        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertEqual(result.stdout.splitlines(), [
            f"shop  gardener   ✗ no record on this host (GARDENER_VERIFY_STEPS in {env}; if it runs on another host,"
            f" set gardener = \"remote:<host-label>\" in {self.home / 'config' / 'integrate.toml'})",
        ])
        self.assertIn(f"  nightly    not configured (GUARD_STEPS is empty or unset in {env})", self.kitchen("status").stdout)

    # What each env leaves, measured with bash as the jobs source it (macOS /bin/bash 3.2, launchd's PATH):
    # env -i HOME=$HOME PATH=/usr/bin:/bin bash --noprofile --norc -c 'set -euo pipefail; source "$1"; set +u;
    #   printf "%s %s" "${#GUARD_STEPS[@]}" "${#GARDENER_VERIFY_STEPS[@]}"' _ <env>
    NIGHTLY_CONFIGURED, NIGHTLY_ABSENT, NIGHTLY_UNKNOWN = ("  nightly    ✗ no record (GUARD_STEPS in",
                                                         "  nightly    not configured (GUARD_STEPS is empty",
                                                         "  nightly    unknown: cannot source")

    def assert_nightly(self, cases):
        project = self.make_project("shop")
        self.list_projects(project)
        for text, expected in cases.items():
            with self.subTest(text=text):
                self.configure_automation(text=text)
                self.assertIn(expected, self.kitchen("status").stdout)

    @staticmethod
    def empty_array_is_unbound():
        """bash before 4.4 (macOS /bin/bash 3.2) stops on "${A[@]}" of an unset array under set -u; bash 4.4 and later
        expand it to nothing. Status and the jobs share this host's bash, so status must report what this bash does."""
        probe = subprocess.run(["bash", "--noprofile", "--norc", "-uc", 'x=("${A[@]}")'], capture_output=True,
                               env={"PATH": "/usr/bin:/bin"})
        return probe.returncode != 0

    def test_the_nightly_is_configured_only_by_a_guard_steps_array_with_a_step_as_bash_reads_it(self):
        configured, absent, unknown = self.NIGHTLY_CONFIGURED, self.NIGHTLY_ABSENT, self.NIGHTLY_UNKNOWN
        old_bash = self.empty_array_is_unbound()
        self.assert_nightly({
            "": absent,                                                                    # 0
            "GUARD_LABEL=\"nightly-guard\"\n": absent,                                    # 0
            '# GUARD_STEPS=("tests|true")\n': absent,                                       # 0
            'GUARD_STEPS=(\n  # "tests|true"\n)\n': absent,                                # 0
            'GUARD_STEPS=("tests|true")\nGUARD_STEPS=()\n': absent,                         # 0
            'GUARD_STEPS=( \\\n)\n': absent,                                              # 0: a line continuation
            "GUARD_STEPS=($(true))\n": absent,                                             # 0
            'GUARD_STEPS=(   # name|command, run in order\n  "build|make build"  # first\n  "tests|make test"\n)\n': configured,  # 2
            'GUARD_STEPS=()\nGUARD_STEPS+=("tests|true")\n': configured,                   # 1
            'GUARD_STEPS=("tests|true")\nGUARD_STEPS+=()\n': configured,                   # 1: += appends
            'export GUARD_STEPS=(\'tests|echo ")"\')\n': configured,                       # 1
            'GUARD_STEPS=("$(printf tests)|true")\n': configured,                           # 1
            'GUARD_STEPS=("tests|make test DIR=$HOME")\n': configured,                      # 1
            "GUARD_STEPS=\n": configured,                                                  # 1: a scalar is one element
            "GUARD_STEPS=(`printf x`)\n": configured,                                      # 1
            'GUARD_STEPS=("${COMMON[@]}" "tests|true")\n': unknown if old_bash else configured,  # bash 3.2: exit 1; 4.4+: 1
            "GUARD_STEPS=($STEPS)\n": unknown,                                             # exit 1: STEPS unbound, every bash
            'GUARD_STEPS=("${COMMON[@]}")\n': unknown if old_bash else absent,               # bash 3.2: exit 1; 4.4+: 0
        })

    def test_the_env_is_read_as_the_jobs_source_it_not_as_text(self):
        shared = self.home / "config" / "shared.env"
        (self.home / "config").mkdir(exist_ok=True)
        shared.write_text('GUARD_STEPS=("tests|true")\nGARDENER_VERIFY_STEPS=("tests|true")\n')
        configured = self.NIGHTLY_CONFIGURED
        self.assert_nightly({
            'GUARD_STEPS=(); GUARD_STEPS+=("tests|true")\n': configured,                    # 1
            'declare -a GUARD_STEPS\nGUARD_STEPS[0]="tests|true"\n': configured,            # 1
            f'source "{shared}"\n': configured,                                             # 1
            'GUARD_STEPS=("tests|true")\nNOTE="a note\nGUARD_STEPS=()\n"\n': configured,    # 1
            'GUARD_STEPS=("tests|true")\nif false; then\n  GUARD_STEPS=()\nfi\n': configured,  # 1
        })
        self.configure_automation(text=f'source "{shared}"\n')
        self.assertIn("  gardener   ✗ no record on this host (GARDENER_VERIFY_STEPS in", self.kitchen("status").stdout)  # 1

    def test_an_env_that_fails_to_source_is_unknown_never_not_configured(self):
        project = self.make_project("shop")
        self.list_projects(project)
        for text in ('GUARD_STEPS=("tests|true")\nGARDENER_VERIFY_STEPS=("tests|true")\nexit 1\n',   # exit 1
                     'GUARD_STEPS=("tests|true")\nGARDENER_VERIFY_STEPS=("tests|true")\nfalse\n',    # exit 1 under set -e
                     'GARDENER_VERIFY_STEPS=("tests|true")\nGUARD_STEPS=(\n  "tests|true"\n'):      # exit 2: syntax error
            with self.subTest(text=text):
                env = self.configure_automation(text=text)

                result = self.kitchen("status", "--exceptions")

                self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
                self.assertIn(f"shop  nightly    unknown: cannot source {env}: bash exited", result.stdout)
                self.assertIn(f"shop  gardener   unknown: cannot source {env}: bash exited", result.stdout)
                self.assertIn(f"shop  audit      unknown: cannot source {env}: bash exited", result.stdout)
                self.assertNotIn("not configured", result.stdout)

    def test_an_env_that_does_not_finish_sourcing_in_time_is_unknown_and_status_returns_promptly(self):
        project = self.make_project("shop")
        self.list_projects(project)
        env = self.configure_automation(text='GUARD_STEPS=("tests|true")\nsleep 30\n')
        started = datetime.datetime.now()

        result = self.kitchen("status", "--exceptions", env_extra={"KITCHEN_ENV_TIMEOUT_SECONDS": "1"})

        self.assertLess((datetime.datetime.now() - started).total_seconds(), 10)
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn(f"shop  nightly    unknown: sourcing {env} took longer than 1s", result.stdout)
        self.assertIn(f"shop  gardener   unknown: sourcing {env} took longer than 1s", result.stdout)

    def test_an_env_without_gardener_verify_steps_configures_the_nightly_but_not_the_gardener(self):
        self.green_project()
        (self.home / "state" / "gardener" / "shop.jsonl").unlink()
        for text in ('GUARD_STEPS=("tests|true")\n# GARDENER_VERIFY_STEPS=("tests|true")\nGARDENER_LABEL="gardener"\n',  # 1 0
                     'GUARD_STEPS=("tests|true")\nGARDENER_VERIFY_STEPS=()\n'):                                       # 1 0
            with self.subTest(text=text):
                self.configure_automation(text=text)

                result = self.kitchen("status", "--exceptions")
                out = self.kitchen("status").stdout

                self.assertEqual((result.returncode, result.stdout), (0, ""), result.stdout + result.stderr)
                self.assertIn("  nightly    ✓ green", out)
                self.assertIn("  gardener   not configured (GARDENER_VERIFY_STEPS is empty or unset in", out)

    @unittest.skipIf(os.geteuid() == 0, "root reads a file without permissions")
    def test_an_unreadable_automation_env_makes_the_gardener_unknown_never_not_configured(self):
        self.green_project()
        env = self.home / "config" / "automation" / "shop.env"
        env.chmod(0)
        try:
            result = self.kitchen("status", "--exceptions")
        finally:
            env.chmod(0o600)

        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn(f"shop  gardener   unknown: cannot read {env}:", result.stdout)
        self.assertNotIn("not configured", result.stdout)

    def test_journal_entries_with_an_unreadable_time_are_counted_never_dropped(self):
        project = self.make_project("shop")
        self.list_projects(project)
        (self.home / "state").mkdir(exist_ok=True)
        entry = {"ts": "not a time", "repo": str(self.home), "project": None, "agent": "claude", "status": "blocked", "message": "clock broke"}
        (self.home / "state" / "log.jsonl").write_text(json.dumps(entry) + "\n")

        out = self.kitchen("status").stdout

        self.assertIn("unattached  1 entries, 1 blocked, 1 with unknown time", out)
        self.assertIn("clock broke", out)

    def test_log_notifies_only_blocked_and_decision_checkpoints_redacted(self):
        project = self.make_project("shop")
        secret = "tok" + "en=" + "abcd1234efgh5678"

        for message, state in (("step one done", "done"), (f"needs the prod {secret}", "blocked"), ("route a or b?", "decision")):
            self.assertEqual(self.kitchen("log", message, "--status", state, cwd=project).returncode, 0)

        sent = self.notifications()
        self.assertEqual(len(sent), 2, sent)
        self.assertIn("blocked", sent[0])
        self.assertIn("[REDACTED]", sent[0])
        self.assertNotIn("abcd1234efgh5678", sent[0])
        self.assertIn("route a or b?", sent[1])


class NotifyTests(unittest.TestCase):
    """One local notifier for every exception; nothing leaves the machine."""

    def notify(self, system, found, returncode=0):
        sys.path.insert(0, str(KITCHEN.parent.parent / "lib"))
        try:
            from kitchen import notify
        finally:
            sys.path.pop(0)
        calls = []

        def run(argv, **kwargs):
            calls.append(argv)
            return subprocess.CompletedProcess(argv, returncode, "", "it broke\n")

        with mock.patch.object(notify.platform, "system", return_value=system), \
                mock.patch.object(notify.shutil, "which", side_effect=lambda name: f"/bin/{name}" if name in found else None), \
                mock.patch.object(notify.subprocess, "run", side_effect=run):
            code, text = notify.notify("kitchen nightly: shop red", "pass" + "word=hunter2hunter2 in the log")
        return code, text, calls

    def test_macos_uses_display_notification_with_the_text_as_arguments(self):
        code, _, calls = self.notify("Darwin", {"osascript"})

        self.assertEqual(code, 0)
        self.assertEqual(calls[0][0], "/bin/osascript")
        self.assertIn("display notification (item 2 of argv) with title (item 1 of argv)", calls[0])
        self.assertEqual(calls[0][-2:], ["kitchen nightly: shop red", "[REDACTED] in the log"])

    def test_linux_uses_notify_send_when_present(self):
        code, _, calls = self.notify("Linux", {"notify-send"})

        self.assertEqual((code, calls), (0, [["/bin/notify-send", "kitchen nightly: shop red", "[REDACTED] in the log"]]))

    def test_without_a_notifier_it_says_so_and_sends_nothing(self):
        code, text, calls = self.notify("Linux", set())

        self.assertEqual((code, calls), (2, []))
        self.assertIn("no notifier available", text)
        self.assertNotIn("hunter2", text)

    def test_a_failing_notifier_is_reported(self):
        code, text, _ = self.notify("Darwin", {"osascript"}, returncode=1)

        self.assertEqual(code, 1)
        self.assertIn("it broke", text)

class IntegrateTests(ProjectFixture):
    """A source repo where feature-a and feature-b each pass alone and break together."""

    CHECK = 'if [ -f feature-a ] && [ -f feature-b ]; then echo "a and b collide"; exit 1; fi'

    def setUp(self):
        super().setUp()
        self.source = self.make_project("shop")
        (self.source / "shared.txt").write_text("base\n")
        git(self.source, "add", ".")
        git(self.source, "commit", "-q", "-m", "base")
        for name in ("a", "b"):
            git(self.source, "checkout", "-q", "-b", f"feature-{name}", "main")
            (self.source / f"feature-{name}").write_text(f"{name}\n")
            git(self.source, "add", ".")
            git(self.source, "commit", "-q", "-m", f"add {name}")
        git(self.source, "checkout", "-q", "main")
        self.configure(f"checks = [{json.dumps(self.CHECK)}]")

    def configure(self, body, project="shop"):
        (self.home / "config").mkdir(exist_ok=True)
        (self.home / "config" / "integrate.toml").write_text(f"[projects.{project}]\nbase = \"main\"\n{body}\n")

    def integrate(self, *args, env_extra=None):
        return self.kitchen("integrate", "--repo", str(self.source), *args, env_extra=env_extra)

    def records(self):
        return [json.loads(line) for line in (self.home / "state" / "integrate" / "shop.jsonl").read_text().splitlines()]

    def sha(self, ref):
        return subprocess.run(["git", "-C", str(self.source), "rev-parse", ref], capture_output=True, text=True, check=True).stdout.strip()

    def test_branches_green_alone_and_red_together_fail_together(self):
        alone = [self.integrate(branch) for branch in ("feature-a", "feature-b")]
        together = self.integrate("feature-a", "feature-b")

        self.assertEqual([r.returncode for r in alone], [0, 0], [r.stdout for r in alone])
        self.assertEqual(together.returncode, 1, together.stdout)
        self.assertIn("check failed after feature-b", together.stdout)
        vector = [self.sha("main"), self.sha("feature-a"), self.sha("feature-b")]
        self.assertEqual((self.records()[-1]["vector"], self.records()[-1]["status"]), (vector, "FAIL"))
        self.assertIn(f"[{' '.join(s[:8] for s in vector)}]", together.stdout)

    def test_a_pass_is_bound_to_the_exact_shas_and_a_moved_head_invalidates_it(self):
        self.integrate("feature-a")
        before = self.integrate("feature-a", "--recorded")
        git(self.source, "checkout", "-q", "feature-a")
        (self.source / "more").write_text("x\n")
        git(self.source, "add", ".")
        git(self.source, "commit", "-q", "-m", "more")
        after = self.integrate("feature-a", "--recorded")

        self.assertEqual(before.returncode, 0, before.stdout)
        self.assertEqual(after.returncode, 1, after.stdout)
        self.assertIn("stale: feature-a", after.stdout)

    def test_a_recorded_pass_is_reused_only_while_the_configured_checks_are_unchanged(self):
        self.integrate("feature-a")
        self.configure(f"checks = [{json.dumps(self.CHECK)}, \"true\"]")
        changed = self.integrate("feature-a", "--recorded")
        self.configure(f"checks = [{json.dumps(self.CHECK)}]")
        restored = self.integrate("feature-a", "--recorded")

        self.assertEqual(changed.returncode, 1, changed.stdout)
        self.assertIn("the check configuration changed", changed.stdout)
        self.assertEqual(restored.returncode, 0, restored.stdout)

    def test_without_a_base_integrate_fails_instead_of_using_origin_main(self):
        git(self.source, "update-ref", "refs/remotes/origin/main", self.sha("main"))
        (self.home / "config" / "integrate.toml").write_text(f"[projects.shop]\nchecks = [{json.dumps(self.CHECK)}]\n")

        results = [self.integrate("feature-a"), self.integrate("feature-a", "--recorded")]

        for result in results:
            self.assertEqual(result.returncode, 1, result.stdout)
            self.assertIn("no base for 'shop'", result.stdout)
        self.assertEqual(self.integrate("feature-a", "--base", "main").returncode, 0)

    def test_a_recorded_pass_is_not_reused_once_the_configured_path_changes(self):
        tools = {}
        for name, code in (("good", 0), ("bad", 1)):
            folder = self.home / name
            folder.mkdir()
            (folder / "shop-check").write_text(f"#!/bin/sh\nexit {code}\n")
            (folder / "shop-check").chmod(0o755)
            tools[name] = folder
        self.configure(f'checks = ["shop-check"]\npath = ["{tools["good"]}"]')
        self.assertEqual(self.integrate("feature-a").returncode, 0)
        self.configure(f'checks = ["shop-check"]\npath = ["{tools["bad"]}"]')

        recorded = self.integrate("feature-a", "--recorded")
        real = self.integrate("feature-a")

        self.assertEqual(real.returncode, 1, real.stdout)
        self.assertEqual(recorded.returncode, 1, recorded.stdout)
        self.assertIn("stale: the check configuration changed", recorded.stdout)

    def test_a_merge_conflict_fails_at_that_branch(self):
        for name in ("c1", "c2"):
            git(self.source, "checkout", "-q", "-b", name, "main")
            (self.source / "shared.txt").write_text(f"{name}\n")
            git(self.source, "commit", "-qam", name)
        git(self.source, "checkout", "-q", "main")

        result = self.integrate("c1", "c2")

        self.assertEqual(result.returncode, 1)
        self.assertIn("merge conflict at c2", result.stdout)

    def test_missing_check_config_fails_instead_of_passing(self):
        (self.home / "config" / "integrate.toml").unlink()

        result = self.integrate("feature-a", "--base", "main")

        self.assertEqual(result.returncode, 1)
        self.assertIn("no check commands", result.stdout)

    def test_nothing_it_runs_inherits_git_variables_and_the_source_is_untouched(self):
        self.configure('checks = ["env | grep ^GIT_ && exit 1 || exit 0"]')
        decoy = self.make_project("decoy")
        hook_env = {"GIT_DIR": str(decoy / ".git"), "GIT_INDEX_FILE": str(decoy / ".git" / "index")}
        refs_before = subprocess.run(["git", "-C", str(self.source), "for-each-ref"], capture_output=True, text=True).stdout

        result = self.integrate("feature-a", "feature-b", "--check", "env | grep ^GIT_ && exit 1 || exit 0", env_extra=hook_env)

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        refs_after = subprocess.run(["git", "-C", str(self.source), "for-each-ref"], capture_output=True, text=True).stdout
        self.assertEqual(refs_after, refs_before)
        self.assertEqual(subprocess.run(["git", "-C", str(decoy), "log", "--oneline"], capture_output=True, text=True).stdout.count("\n"), 1)

    def test_pr_heads_are_fetched_into_the_clone_only(self):
        origin = self.home / "origin.git"
        git(self.home, "clone", "-q", "--bare", str(self.source), str(origin))
        git(origin, "update-ref", "refs/pull/7/head", self.sha("feature-a"))
        git(self.source, "remote", "add", "origin", str(origin))

        result = self.integrate("pr:7")

        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertEqual(self.records()[-1]["branches"], [{"ref": "pr:7", "sha": self.sha("feature-a")}])
        self.assertNotIn("pull", subprocess.run(["git", "-C", str(self.source), "for-each-ref"], capture_output=True, text=True).stdout)


class ModelsTests(KitchenFixture):
    def test_an_unset_role_fails_with_the_command_that_sets_it(self):
        result = self.kitchen("models", "get", "reviewer", "--author", "claude")

        self.assertEqual(result.returncode, 1)
        self.assertIn("not configured", result.stderr)
        self.assertIn("kitchen models set reviewer --author claude", result.stderr)

    def test_set_then_get_and_the_cli_call(self):
        set_ = self.kitchen("models", "set", "reviewer", "--author", "claude", "--provider", "codex", "--model", "gpt-x",
                            "--effort", "low", "--tier", "standard")
        self.assertEqual(set_.returncode, 0, set_.stderr)

        got = self.kitchen("models", "get", "reviewer", "--author", "claude", "--json")
        call = self.kitchen("models", "get", "reviewer", "--author", "claude", "--command")

        self.assertEqual(json.loads(got.stdout), [{"provider": "codex", "model": "gpt-x", "effort": "low", "tier": "standard"}])
        self.assertEqual(call.stdout.strip(), "codex exec --skip-git-repo-check -s read-only -m gpt-x "
                                              "-c 'model_reasoning_effort=\"low\"' -c 'service_tier=\"default\"'")

    def test_a_reviewer_on_the_authors_own_provider_is_refused(self):
        result = self.kitchen("models", "set", "reviewer", "--author", "codex", "--provider", "codex", "--model", "default",
                              "--effort", "low", "--tier", "standard")

        self.assertEqual(result.returncode, 1)
        self.assertIn("the author's model does not check its own work", result.stderr)
        self.assertFalse((self.home / "config" / "models.toml").exists())

    def test_the_claude_cli_cannot_take_the_fast_tier(self):
        self.kitchen("models", "set", "reviewer", "--author", "codex", "--provider", "claude", "--model", "default",
                     "--effort", "low", "--tier", "fast")

        result = self.kitchen("models", "get", "reviewer", "--author", "codex", "--command")

        self.assertEqual(result.returncode, 1)
        self.assertIn("no flag for the fast tier", result.stderr)

    def test_the_listing_shows_the_reviewer_for_each_author(self):
        self.kitchen("models", "set", "reviewer", "--author", "claude", "--provider", "codex", "--model", "default",
                     "--effort", "low", "--tier", "fast")

        out = self.kitchen("models").stdout

        self.assertIn("reviewer for claude work codex default · effort low · tier fast", out)
        self.assertIn("reviewer for codex work  not configured", out)

    def test_roles_nothing_reads_are_refused(self):
        result = self.kitchen("models", "set", "worker", "--author", "claude", "--provider", "codex", "--model", "default",
                              "--effort", "low", "--tier", "standard")

        self.assertEqual(result.returncode, 2)
        self.assertIn("invalid choice: 'worker'", result.stderr)

    def test_set_refuses_to_drop_tables_it_does_not_read(self):
        config = self.home / "config" / "models.toml"
        config.parent.mkdir(parents=True)
        config.write_text('[worker]\nprovider = "claude"\nmodel = "default"\neffort = "high"\ntier = "standard"\n')

        result = self.kitchen("models", "set", "reviewer", "--author", "claude", "--provider", "codex", "--model", "default",
                              "--effort", "low", "--tier", "standard")

        self.assertEqual(result.returncode, 1)
        self.assertIn("tables kitchen does not read (worker)", result.stderr)
        self.assertIn("[worker]", config.read_text())

    def reviewer(self, action, author, provider, model, tier="standard"):
        return self.kitchen("models", action, "reviewer", "--author", author, "--provider", provider, "--model", model,
                            "--effort", "high", "--tier", tier)

    def pi_listing(self, *rows):
        """A stand-in `pi` on PATH whose `--list-models` prints these provider/model rows in pi's own table."""
        bin_dir = self.home / "pi-bin"
        bin_dir.mkdir(exist_ok=True)
        table = "provider       model                context  max-out  thinking  images\n" + "".join(
            f"{r.split('/')[0]:<15}{r.split('/')[1]:<21}272K     128K     yes       yes\n" for r in rows)
        pi = bin_dir / "pi"
        pi.write_text(f"#!/bin/sh\n[ \"$1\" = --list-models ] && printf '%s' '{table}'\n")
        pi.chmod(0o755)
        return {"PATH": f"{bin_dir}:{os.environ['PATH']}"}

    def test_add_keeps_the_first_reviewer_and_get_gives_both_calls(self):
        self.reviewer("set", "claude", "codex", "gpt-x")

        added = self.reviewer("add", "claude", "pi", "openai-codex/gpt-x")
        got = self.kitchen("models", "get", "reviewer", "--author", "claude", "--json")
        calls = self.kitchen("models", "get", "reviewer", "--author", "claude", "--command",
                             env_extra=self.pi_listing("openai-codex/gpt-x"))
        listing = self.kitchen("models").stdout

        self.assertEqual(added.returncode, 0, added.stderr)
        self.assertEqual([(e["provider"], e["model"]) for e in json.loads(got.stdout)], [("codex", "gpt-x"), ("pi", "openai-codex/gpt-x")])
        self.assertEqual(calls.stdout.splitlines(), [
            "codex exec --skip-git-repo-check -s read-only -m gpt-x -c 'model_reasoning_effort=\"high\"' -c 'service_tier=\"default\"'",
            "pi -p --no-session --no-mcp --tools read,grep,find,ls --model openai-codex/gpt-x --thinking high"])
        self.assertIn("reviewer for claude work codex gpt-x · effort high · tier standard\n"
                      "                           pi openai-codex/gpt-x · effort high · tier standard", listing)

    def test_set_replaces_every_reviewer_with_one(self):
        self.reviewer("set", "claude", "codex", "gpt-x")
        self.reviewer("add", "claude", "pi", "openai-codex/gpt-x")

        self.reviewer("set", "claude", "codex", "gpt-y")

        got = self.kitchen("models", "get", "reviewer", "--author", "claude").stdout
        self.assertEqual(got, "codex gpt-y · effort high · tier standard\n")

    def test_a_pi_reviewer_is_judged_by_its_models_family_not_by_pi(self):
        gpt_on_codex = self.reviewer("add", "codex", "pi", "openai-codex/gpt-x")
        claude_on_codex = self.reviewer("add", "codex", "pi", "claude-bridge/claude-x")

        self.assertEqual(gpt_on_codex.returncode, 1)
        self.assertIn("the author's model does not check its own work", gpt_on_codex.stderr)
        self.assertEqual(claude_on_codex.returncode, 0, claude_on_codex.stderr)

    def test_a_pi_model_whose_family_kitchen_cannot_tell_is_refused(self):
        for model in ("default", "gpt-x", "openrouter/gpt-x"):
            with self.subTest(model=model):
                result = self.reviewer("add", "claude", "pi", model)

                self.assertEqual(result.returncode, 1)
                self.assertIn("so it can tell whose model reviews", result.stderr)
        self.assertFalse((self.home / "config" / "models.toml").exists())

    def test_pi_takes_no_fast_tier(self):
        result = self.reviewer("add", "claude", "pi", "openai-codex/gpt-x", tier="fast")

        self.assertEqual(result.returncode, 1)
        self.assertIn("pi has no service tier", result.stderr)

    def test_the_same_reviewer_twice_is_refused(self):
        self.reviewer("set", "claude", "codex", "gpt-x")

        result = self.reviewer("add", "claude", "codex", "gpt-x")

        self.assertEqual(result.returncode, 1)
        self.assertIn("already runs that provider and model", result.stderr)

    def test_remove_takes_one_reviewer_out_and_asks_which_when_two_share_a_provider(self):
        self.reviewer("set", "claude", "codex", "gpt-x")
        self.reviewer("add", "claude", "codex", "gpt-y")
        self.reviewer("add", "claude", "pi", "openai-codex/gpt-x")

        ambiguous = self.kitchen("models", "remove", "reviewer", "--author", "claude", "--provider", "codex")
        by_model = self.kitchen("models", "remove", "reviewer", "--author", "claude", "--provider", "codex", "--model", "gpt-x")
        by_provider = self.kitchen("models", "remove", "reviewer", "--author", "claude", "--provider", "pi")

        self.assertEqual(ambiguous.returncode, 1)
        self.assertIn("pass --model", ambiguous.stderr)
        self.assertEqual((by_model.returncode, by_provider.returncode), (0, 0), by_model.stderr + by_provider.stderr)
        got = self.kitchen("models", "get", "reviewer", "--author", "claude").stdout
        self.assertEqual(got, "codex gpt-y · effort high · tier standard\n")

    def test_a_file_from_before_lists_is_read_and_add_keeps_its_reviewer(self):
        config = self.home / "config" / "models.toml"
        config.parent.mkdir(parents=True)
        config.write_text('[reviewer.claude]\nprovider = "codex"\nmodel = "gpt-x"\neffort = "high"\ntier = "standard"\n\n'
                          '[reviewer.codex]\nprovider = "claude"\nmodel = "default"\neffort = "high"\ntier = "standard"\n')

        before = self.kitchen("models", "get", "reviewer", "--author", "claude").stdout
        self.reviewer("add", "claude", "pi", "openai-codex/gpt-x")

        self.assertEqual(before, "codex gpt-x · effort high · tier standard\n")
        got = self.kitchen("models", "get", "reviewer", "--author", "claude", "--json")
        self.assertEqual([e["provider"] for e in json.loads(got.stdout)], ["codex", "pi"])
        self.assertEqual(self.kitchen("models", "get", "reviewer", "--author", "codex").stdout, "claude default · effort high · tier standard\n")

    def test_a_pi_reviewer_runs_only_on_an_id_pi_lists_exactly(self):
        self.reviewer("set", "claude", "pi", "openai-codex/gpt-x")

        near = self.kitchen("models", "get", "reviewer", "--author", "claude", "--command",
                            env_extra=self.pi_listing("openai-codex/gpt-x-sol", "claude-bridge/gpt-x"))
        exact = self.kitchen("models", "get", "reviewer", "--author", "claude", "--command",
                             env_extra=self.pi_listing("openai-codex/gpt-x-sol", "openai-codex/gpt-x"))
        no_pi = self.kitchen("models", "get", "reviewer", "--author", "claude", "--command", env_extra={"PATH": "/usr/bin:/bin"})

        self.assertEqual(near.returncode, 1)
        self.assertIn("does not list openai-codex/gpt-x, and pi would run the nearest model", near.stderr)
        self.assertEqual(exact.returncode, 0, exact.stderr)
        self.assertIn("--model openai-codex/gpt-x --thinking high", exact.stdout)
        self.assertEqual(no_pi.returncode, 1)
        self.assertIn("cannot list pi's models", no_pi.stderr)

    def test_a_write_refuses_a_file_it_would_lose_part_of(self):
        config = self.home / "config" / "models.toml"
        config.parent.mkdir(parents=True)
        for body, error in (('reviewer = "bad"\n', "reviewer is not a table"),
                            ('[reviewer]\ncodex = "bad"\n', "reviewer.codex is neither a table nor a list"),
                            ('[[reviewer.codex]]\nprovider = "claude"\n', "reviewer for codex: missing model, effort, tier"),
                            ('[reviewer.gemini]\nprovider = "claude"\n', "tables kitchen does not read (reviewer.gemini)")):
            with self.subTest(body=body):
                config.write_text(body)

                result = self.reviewer("set", "claude", "codex", "gpt-x")

                self.assertEqual(result.returncode, 1)
                self.assertIn(error, result.stderr)
                self.assertEqual(config.read_text(), body)

    def test_doctor_warns_when_a_pi_reviewer_has_no_pi_on_path(self):
        self.reviewer("set", "claude", "pi", "openai-codex/gpt-x")
        env = {"PATH": "/usr/bin:/bin"}

        out = self.kitchen("doctor", env_extra=env).stdout

        self.assertIn("`pi` is not on PATH: a reviewer for claude work runs on it", out)


class InventoryTests(ProjectFixture):
    def add_user_skill(self, base, name, body):
        folder = self.home / base / name
        folder.mkdir(parents=True)
        (folder / "SKILL.md").write_text(body)

    def test_inventory_flags_same_skill_with_different_content(self):
        self.add_user_skill(".claude/skills", "deploy", "v1\n")
        self.add_user_skill(".agents/skills", "deploy", "v2\n")

        out = self.kitchen("inventory").stdout

        self.assertIn("drift: deploy has different content", out)

    def test_inventory_does_not_flag_links_to_one_source(self):
        self.add_skill("alpha")
        self.kitchen("install")

        out = self.kitchen("inventory").stdout

        self.assertNotIn("drift: alpha", out)
        self.assertIn("alpha (kitchen)", out)

    def test_inventory_lists_paused_automations_and_disabled_codex_skills(self):
        automation = self.home / ".codex" / "automations" / "brief"
        automation.mkdir(parents=True)
        (automation / "automation.toml").write_text('version = 1\nid = "brief"\nkind = "cron"\nstatus = "PAUSED"\nprompt = "x"\n')
        (self.home / ".codex" / "config.toml").write_text('[[skills.config]]\npath = "/x/pet/SKILL.md"\nenabled = false\n')

        out = self.kitchen("inventory").stdout

        self.assertIn("1 total, 1 paused", out)
        self.assertIn("disabled in Codex config: /x/pet/SKILL.md", out)


class RetroTests(ProjectFixture):
    """Fixtures follow the real transcript formats: Claude Code project JSONL and Codex rollout JSONL."""

    def claude_event(self, text, ts="2099-01-01T10:00:00Z", **extra):
        event = {"type": "user", "timestamp": ts, "cwd": "/work/shop", "entrypoint": "claude-desktop",
                 "promptSource": "sdk", "origin": {"kind": "human"}, "message": {"content": text}}
        event.update(extra)
        return event

    def write_claude(self, events, session="s1", folder="-work-shop"):
        path = self.home / ".claude" / "projects" / folder
        path.mkdir(parents=True, exist_ok=True)
        (path / f"{session}.jsonl").write_text("".join(json.dumps(e) + "\n" for e in events))

    def write_codex(self, name, source, texts, **meta):
        folder = self.home / ".codex" / "sessions" / "2099" / "01" / "01"
        folder.mkdir(parents=True, exist_ok=True)
        payload = {"id": name, "cwd": "/work/api", "originator": "Codex Desktop", "source": source, "thread_source": "user", **meta}
        lines = [{"type": "session_meta", "timestamp": "2099-01-01T09:00:00Z", "payload": payload}]
        for entry in texts:
            text, client = (entry, True) if isinstance(entry, str) else entry
            item = {"type": "UserMessage", "id": f"u-{len(lines)}", "content": [{"type": "text", "text": text}]}
            if client:
                item["client_id"] = f"c-{len(lines)}"
            lines.append({"timestamp": "2099-01-01T10:00:00Z", "type": "event_msg", "payload": {"type": "item_completed", "thread_id": name, "item": item}})
        (folder / f"rollout-{name}.jsonl").write_text("".join(json.dumps(line, separators=(",", ":")) + "\n" for line in lines))

    def retro(self, *args):
        return self.kitchen("retro", "--since", "1d", "--json", *args, cwd=self.home)

    def texts(self, *args):
        return [p["text"] for p in json.loads(self.retro(*args).stdout)]

    def test_claude_keeps_human_prompts_and_drops_injected_turns(self):
        self.write_claude([
            self.claude_event("no, eso está mal otra vez"),
            self.claude_event([{"type": "tool_result", "content": "output"}]),
            self.claude_event("<system-reminder>context</system-reminder>"),
            self.claude_event("Context handoff (manual_context): moved"),
            self.claude_event("Act as the research sub-agent for this task. Do X"),
            self.claude_event("old prompt", ts="2000-01-01T00:00:00Z"),
        ])

        self.assertEqual(self.texts("--tool", "claude"), ["no, eso está mal otra vez"])

    def test_claude_excludes_by_provenance_not_wording(self):
        launched = "Without using any tools, answer in one line: which language do you reply in?"
        self.write_claude([
            self.claude_event("dale, sigue"),
            self.claude_event("Background task finished", origin={"kind": "task-notification"}, promptSource="system"),
            self.claude_event("You are the weekly gardener. Run unattended.", entrypoint="sdk-cli", origin=None),
            self.claude_event("Delegated task node:delegated-task:abc reached a terminal state. Use task_status.", origin=None),
            self.claude_event("replayed prompt", uuid="u-replay"),
            {"type": "assistant", "uuid": "a-replay", "parentUuid": "u-replay", "requestId": "req_syn_0001", "message": {"content": []}},
        ])
        self.write_claude([
            {"type": "queue-operation", "operation": "enqueue", "timestamp": "2099-01-01T10:00:00Z", "content": launched},
            self.claude_event(launched, entrypoint="sdk-ts", origin=None, cwd="/tmp"),
        ], session="probe", folder="-tmp")
        self.write_claude([
            {"type": "assistant", "timestamp": "2099-01-01T09:59:00Z", "message": {"content": [
                {"type": "tool_use", "name": "Bash", "input": {"command": f"cd /tmp && claude -p '{launched}' --model x"}}]}},
        ], session="parent", folder="-work-lead")

        result = self.kitchen("retro", "--since", "1d", "--tool", "claude", cwd=self.home)

        self.assertEqual(self.texts("--tool", "claude"), ["dale, sigue"])
        self.assertEqual(result.stdout.splitlines()[-1],
                         "1 prompts included · 5 excluded (notification 2, agent-launched 1, non-interactive 1, replayed 1) · 0 unknown provenance"
                         " · sources: claude ~/.claude ok")

    def test_claude_excludes_a_t3_thread_an_agent_launched(self):
        brief = "This thread starts a discussion with the owner about the kitchen. Work in your worktree."
        self.write_claude([
            {"type": "queue-operation", "operation": "enqueue", "timestamp": "2099-01-01T10:00:00Z", "content": brief},
            self.claude_event(brief, entrypoint="sdk-ts", origin=None, cwd="/wt/docs"),
            self.claude_event("listame los 24 principios", entrypoint="sdk-ts", origin=None, cwd="/wt/docs", ts="2099-01-01T10:05:00Z"),
        ], session="child", folder="-wt-docs")
        self.write_claude([
            {"type": "assistant", "timestamp": "2099-01-01T09:59:56Z", "cwd": "/scratch", "message": {"content": [
                {"type": "tool_use", "name": "mcp__t3-code__t3_thread_launch",
                 "input": {"title": "Principles", "workspaceStrategy": {"type": "worktree"}, "message": brief}}]}},
        ], session="parent", folder="-scratch")

        result = self.kitchen("retro", "--since", "1d", "--tool", "claude", cwd=self.home)

        self.assertEqual(self.texts("--tool", "claude"), ["listame los 24 principios"])
        self.assertIn("excluded (agent-launched 1)", result.stdout.splitlines()[-1])

    def test_a_command_that_only_mentions_claude_p_launches_nothing(self):
        brief = "This thread starts a discussion with the owner about the kitchen. Work in your worktree."
        self.write_claude([
            self.claude_event(brief, entrypoint="sdk-ts", origin=None, cwd="/wt/docs"),
            self.claude_event("listame los principios", entrypoint="sdk-ts", origin=None, cwd="/wt/docs", ts="2099-01-01T10:05:00Z"),
        ], session="thread", folder="-wt-docs")
        heredoc = f"python3 - <<'EOF'\nLAUNCH = 'claude -p'\nBRIEF = '{brief}'\nEOF"
        self.write_claude([
            {"type": "assistant", "timestamp": "2099-01-01T10:20:00Z", "cwd": "/elsewhere", "message": {"content": [
                {"type": "tool_use", "name": "Bash", "input": {"command": heredoc}}]}},
        ], session="editor", folder="-elsewhere")

        result = self.kitchen("retro", "--since", "1d", "--tool", "claude", cwd=self.home)

        self.assertEqual(self.texts("--tool", "claude"), [brief, "listame los principios"])
        self.assertIn("0 unknown provenance", result.stdout.splitlines()[-1])

    def test_unknown_provenance_covers_only_the_launched_first_prompt(self):
        launched = "Without using any tools, answer in one line: which language do you reply in?"
        self.write_claude([
            self.claude_event(launched, entrypoint="sdk-ts", origin=None, cwd="/tmp"),
            self.claude_event("y en inglés?", entrypoint="sdk-ts", origin=None, cwd="/tmp", ts="2099-01-01T10:03:00Z"),
        ], session="probe", folder="-tmp")
        self.write_claude([
            {"type": "assistant", "timestamp": "2099-01-01T08:00:00Z", "cwd": "/srv", "message": {"content": [
                {"type": "tool_use", "name": "Bash", "input": {"command": f"claude -p '{launched}'"}}]}},
        ], session="parent", folder="-srv")

        result = self.kitchen("retro", "--since", "1d", "--tool", "claude", cwd=self.home)

        self.assertEqual(self.texts("--tool", "claude"), ["y en inglés?"])
        self.assertIn("1 unknown provenance", result.stdout.splitlines()[-1])

    def test_a_short_prompt_launched_with_claude_p_is_agent_launched(self):
        self.write_claude([self.claude_event("/wait-what", entrypoint="sdk-ts", origin=None, cwd="/tmp")], session="probe", folder="-tmp")
        self.write_claude([
            {"type": "assistant", "timestamp": "2099-01-01T09:59:30Z", "cwd": "/srv", "message": {"content": [
                {"type": "tool_use", "name": "Bash", "input": {"command": 'cd /tmp && claude -p "/wait-what" --model x 2>&1 | head -4'}}]}},
        ], session="parent", folder="-srv")

        result = self.kitchen("retro", "--since", "1d", "--tool", "claude", cwd=self.home)

        self.assertIn("excluded (agent-launched 1)", result.stdout.splitlines()[-1])

    def test_a_plural_t3_notice_is_a_notification(self):
        self.write_claude([
            self.claude_event("Delegated tasks node:a, node:b reached terminal states. Use task_status with each taskId to read the results.",
                              entrypoint="sdk-ts", origin=None),
            self.claude_event("dale, sigue", entrypoint="sdk-ts", origin=None, ts="2099-01-01T10:01:00Z"),
        ])

        self.assertEqual(self.texts("--tool", "claude"), ["dale, sigue"])

    def test_t3_result_question_pull_request_and_restart_notices_are_notifications(self):
        # both arrive with no origin and promptSource "sdk", exactly like a prompt typed in T3 (measured in real transcripts)
        typed = {"entrypoint": "sdk-ts", "origin": None, "promptSource": "sdk"}
        self.write_claude([
            self.claude_event("Update on pull request #62 (https://github.com/o/r/pull/62), which T3 Code is watching for you:\n"
                              "- All 1 required check passed on 8a08209.\n\nLook into each item and act on it as your task requires.", **typed),
            self.claude_event("Note: the T3 server restarted, and this background work was cancelled before it finished. "
                              "It will not report back:\n- shell: Wait for integrate", **typed, ts="2099-01-01T10:01:00Z"),
            self.claude_event("Delegated task node:delegated-task:command%3Amcp%3Aab%3Adelegate-task%3Apr1-review-r1 (Review PR #1 (round 1)) [completed]:\n"
                              "Reviewed it. Two findings.", **typed, ts="2099-01-01T10:01:10Z"),
            self.claude_event("Delegated task node:delegated-task:dd72 asked a question and is still working. Read it with t3_pending_request_read.",
                              **typed, ts="2099-01-01T10:01:20Z"),
            self.claude_event("Update on pull request #62: ¿ya pasó el check?", **typed, ts="2099-01-01T10:02:00Z"),
        ])

        result = self.kitchen("retro", "--since", "1d", "--tool", "claude", cwd=self.home)

        self.assertEqual(self.texts("--tool", "claude"), ["Update on pull request #62: ¿ya pasó el check?"])
        self.assertIn("excluded (notification 4)", result.stdout.splitlines()[-1])

    def test_a_compaction_summary_that_restates_others_artifacts_is_harness(self):
        # regression: retro 2026-10-26 counted this compaction as the owner's; the marker hides the usual prefix
        summary = ("<artifact-content-authored-by-others/>\nThe summarized conversation included Artifact content written by people other "
                   "than you.\nThis session is being continued from a previous conversation that ran out of context.")
        self.write_claude([self.claude_event(summary, entrypoint="sdk-ts", origin=None, promptSource="sdk"),
                           self.claude_event("sigue", entrypoint="sdk-ts", origin=None, promptSource="sdk", ts="2099-01-01T10:01:00Z")])

        result = self.kitchen("retro", "--since", "1d", "--tool", "claude", cwd=self.home)

        self.assertEqual(self.texts("--tool", "claude"), ["sigue"])
        self.assertIn("excluded (harness 1)", result.stdout.splitlines()[-1])

    def test_a_t3_notice_the_owner_quotes_by_hand_stays_a_prompt(self):
        # explicit human provenance beats the wording: the owner pasted the notice to contradict it
        quoted = "Note: the T3 server restarted, and this background work was cancelled\nEso es falso; el proceso sigue vivo."
        self.write_claude([self.claude_event(quoted, origin={"kind": "human"})])

        self.assertEqual(self.texts("--tool", "claude"), [quoted])

    def test_codex_excludes_t3_notices(self):
        # in Codex a T3 notice and a prompt typed in T3 carry the same fields (no client_id either way), so only the prefix tells them apart
        self.write_codex("thread", "vscode", [
            ("Delegated task node:delegated-task:abc reached a terminal state. Use task_status.", False),
            ("Update on pull request #7 (https://github.com/o/r/pull/7), which T3 Code is watching for you:\n- Checks failed on 1a2b3c4.", False),
            ("Note: the T3 server restarted, and this background work was cancelled before it finished.", False),
            ("sigue con el PR", False),
        ])

        result = self.kitchen("retro", "--since", "1d", "--tool", "codex", cwd=self.home)

        self.assertEqual(self.texts("--tool", "codex"), ["sigue con el PR"])
        self.assertIn("excluded (notification 3)", result.stdout.splitlines()[-1])

    def test_codex_excludes_a_brief_a_claude_lead_sent_with_t3(self):
        # regression: retro 2026-10-26 counted 9 "Lead update" briefs as the owner's; in Codex they carry no client_id, like a typed prompt
        brief = "Lead update 2: read UPDATE-2.md. You can now run read-only queries on the live model yourself."
        self.write_codex("worker", "vscode", [(brief, False), ("y qué encontraste?", False)])
        self.write_claude([
            {"type": "assistant", "timestamp": "2099-01-01T09:59:50Z", "cwd": "/work/lead", "message": {"content": [
                {"type": "tool_use", "name": "mcp__t3-code__t3_thread_send",
                 "input": {"threadId": "worker", "message": brief, "mode": "queue"}}]}},
        ], session="lead", folder="-work-lead")

        result = self.kitchen("retro", "--since", "1d", "--tool", "codex", cwd=self.home)

        self.assertEqual(self.texts("--tool", "codex"), ["y qué encontraste?"])
        self.assertIn("excluded (agent-launched 1)", result.stdout.splitlines()[-1])

    def test_codex_keeps_an_owner_prompt_that_only_resembles_an_agent_send(self):
        # regression: review of #76; a phrase the lead sent to another thread, or a notice the owner pasted in Codex, stays the owner's
        quoted = "Note: the T3 server restarted, and this background work was cancelled\nThat is incorrect; investigate why."
        self.write_codex("owner", "vscode", [("Run the checks again", False), (quoted, True)])
        self.write_claude([
            {"type": "assistant", "timestamp": "2099-01-01T09:59:50Z", "cwd": "/work/lead", "message": {"content": [
                {"type": "tool_use", "name": "mcp__t3-code__t3_thread_send",
                 "input": {"threadId": "other", "message": "Please Run the checks again in the worker and report any failures."}}]}},
        ], session="lead", folder="-work-lead")

        self.assertEqual(self.texts("--tool", "codex"), ["Run the checks again", quoted])

    def test_codex_excludes_a_short_message_a_claude_lead_sent(self):
        self.write_codex("worker", "vscode", [("sigue", False)])
        self.write_claude([
            {"type": "assistant", "timestamp": "2099-01-01T09:59:50Z", "cwd": "/work/lead", "message": {"content": [
                {"type": "tool_use", "name": "mcp__t3-code__t3_thread_send", "input": {"threadId": "worker", "message": "sigue"}}]}},
        ], session="lead", folder="-work-lead")

        self.assertEqual(self.texts("--tool", "codex"), [])

    def test_codex_unwraps_requests_and_drops_delegated_and_guardian_sessions(self):
        self.write_codex("human", "vscode", ["## Context:\nfiles\n## My request for Codex:\nsube a dev", "Act as the review sub-agent for this task. Review it"])
        self.write_codex("guardian", {"subagent": {"other": "guardian"}}, ["approve this command?"], thread_source="guardian_review")

        self.assertEqual(self.texts("--tool", "codex"), ["sube a dev"])

    def test_codex_excludes_heartbeats_exec_runs_and_subagent_threads(self):
        heartbeat = "<heartbeat>\n  <automation_id>phone-progress</automation_id>\n  <instructions>check it</instructions>\n</heartbeat>"
        self.write_codex("thread", "vscode", ["pon un monitor cada 5 min", (heartbeat, False), "ya le di a restore"])
        self.write_codex("probe", "exec", ["Without using any tools, say hi"], originator="codex_exec")
        self.write_codex("child", "vscode", ["explore the repo"], thread_source="subagent")

        result = self.kitchen("retro", "--since", "1d", "--tool", "codex", cwd=self.home)

        self.assertEqual(self.texts("--tool", "codex"), ["pon un monitor cada 5 min", "ya le di a restore"])
        self.assertTrue(result.stdout.endswith("2 prompts included · 3 excluded (subagent 1, automation 1, non-interactive 1) · 0 unknown provenance"
                                                    " · sources: codex ~/.codex ok\n"), result.stdout)

    def test_include_automated_shows_excluded_messages_with_their_reason(self):
        heartbeat = "<heartbeat>\n  <automation_id>phone-progress</automation_id>\n</heartbeat>"
        self.write_codex("thread", "vscode", ["listo", (heartbeat, False)])

        prompts = json.loads(self.retro("--include-automated").stdout)

        self.assertEqual([(p["text"][:11], p["excluded"]) for p in prompts], [("listo", None), ("<heartbeat>", "automation")])

    def test_json_output_keeps_coverage_off_stdout(self):
        self.write_claude([self.claude_event("dale")])

        result = self.retro()

        self.assertEqual(json.loads(result.stdout)[0]["text"], "dale")
        self.assertIn("1 prompts included · 0 excluded (none) · 0 unknown provenance", result.stderr)

    def test_secrets_are_redacted(self):
        self.write_claude([self.claude_event("use token=abc123 please")])

        self.assertEqual(self.texts(), ["use [REDACTED] please"])

    def test_match_filters_prompts(self):
        self.write_claude([self.claude_event("dale"), self.claude_event("te dije que no")])

        self.assertEqual(self.texts("--match", "te dije"), ["te dije que no"])

    def remote_sources(self, *lines):
        (self.home / "config").mkdir(exist_ok=True)
        (self.home / "config" / "retro-sources.txt").write_text("# where else my sessions live\n" + "\n".join(lines) + "\n")

    def fake_ssh(self, body):
        """Stands in for ssh: records its arguments, then runs body. A test never opens a connection."""
        self.fake("fake-ssh", f'printf "%s\\n" "$@" >> "{self.home}/ssh-args"\n{body}')
        return {"KITCHEN_SSH": str(self.fakebin / "fake-ssh")}

    def test_every_claude_account_is_read_and_attributed(self):
        # regression: retro read only ~/.claude, so a second account's sessions (about a quarter of the owner's prompts) never counted
        self.write_claude([self.claude_event("sube a dev")])
        second = self.home / ".claude-personal-2" / "projects" / "-work-api"
        second.mkdir(parents=True)
        (second / "s2.jsonl").write_text(json.dumps(self.claude_event("eso está mal", ts="2099-01-01T11:00:00Z")) + "\n")

        result = self.retro()

        self.assertEqual([(p["text"], p["source"]) for p in json.loads(result.stdout)],
                         [("sube a dev", "claude ~/.claude"), ("eso está mal", "claude ~/.claude-personal-2")])
        self.assertIn("2 prompts included", result.stderr)
        self.assertIn("· sources: claude ~/.claude ok, claude ~/.claude-personal-2 ok", result.stderr)

    def test_a_launch_from_one_account_excludes_the_session_it_starts_in_the_other(self):
        brief = "This thread starts a discussion with the owner about the kitchen. Work in your worktree."
        child = self.home / ".claude-personal-2" / "projects" / "-wt-docs"
        child.mkdir(parents=True)
        (child / "child.jsonl").write_text(json.dumps(self.claude_event(brief, entrypoint="sdk-ts", origin=None, cwd="/wt/docs")) + "\n")
        self.write_claude([
            {"type": "assistant", "timestamp": "2099-01-01T09:59:56Z", "cwd": "/scratch", "message": {"content": [
                {"type": "tool_use", "name": "mcp__t3-code__t3_thread_launch", "input": {"title": "Docs", "message": brief}}]}},
        ], session="lead", folder="-scratch")

        prompts = json.loads(self.retro("--include-automated").stdout)

        self.assertEqual([(p["text"], p["excluded"]) for p in prompts], [(brief, "agent-launched")])

    def test_an_unreachable_remote_source_is_unknown_in_the_coverage_line(self):
        self.write_claude([self.claude_event("dale")])
        self.remote_sources("ssh far-host")

        result = self.kitchen("retro", "--since", "1d", cwd=self.home, env_extra=self.fake_ssh('echo "connect: no route" >&2; exit 255'))

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(result.stdout.endswith("· sources: claude ~/.claude ok, ssh far-host unreachable\n"), result.stdout)

    def test_a_remote_source_that_cannot_run_kitchen_is_unknown(self):
        self.remote_sources("ssh far-host maxim")

        result = self.kitchen("retro", "--since", "1d", cwd=self.home,
                              env_extra=self.fake_ssh('echo "zsh:1: command not found: kitchen" >&2; exit 127'))

        self.assertIn("ssh far-host maxim unknown (exit 127: zsh:1: command not found: kitchen)", result.stdout.splitlines()[-1])

    def test_a_remote_source_is_merged_and_classified_where_it_lives(self):
        self.write_claude([self.claude_event("aquí", ts="2099-01-01T10:00:00Z")])
        self.remote_sources("ssh far-host maxim")
        remote = [{"ts": "2099-01-01T09:00:00+00:00", "tool": "claude", "project": "/w/api", "session": "r1", "text": "allá", "excluded": None,
                   "source": "claude ~/.claude"},
                  {"ts": "2099-01-01T11:00:00+00:00", "tool": "codex", "project": "/w/api", "session": "r2", "text": "<heartbeat>",
                   "excluded": "automation", "source": "codex ~/.codex"}]
        body = (f"cat <<'EOF'\n{json.dumps(remote)}\nEOF\n"
                "echo '1 prompts included · 1 excluded (automation 1) · 0 unknown provenance · sources: claude ~/.claude ok, codex ~/.codex ok' >&2")

        result = self.kitchen("retro", "--since", "1d", "--json", "--include-automated", cwd=self.home, env_extra=self.fake_ssh(body))

        self.assertEqual([(p["text"], p["source"], p["excluded"]) for p in json.loads(result.stdout)],
                         [("allá", "ssh far-host maxim: claude ~/.claude", None), ("aquí", "claude ~/.claude", None),
                          ("<heartbeat>", "ssh far-host maxim: codex ~/.codex", "automation")])
        self.assertIn("2 prompts included · 1 excluded (automation 1) · 0 unknown provenance · sources: claude ~/.claude ok, "
                      "ssh far-host maxim ok [claude ~/.claude ok, codex ~/.codex ok]", result.stderr)
        args = (self.home / "ssh-args").read_text().splitlines()
        self.assertEqual(args[:6], ["-o", "BatchMode=yes", "-o", "ConnectTimeout=10", "-l", "maxim"])
        self.assertEqual(args[6], "far-host")
        self.assertIn("kitchen retro --json --include-automated --home-only --since 1d --tool all", args[7])

    def test_ssh_noise_after_the_remote_coverage_line_does_not_hide_a_gap(self):
        # regression: review of #78; only the last stderr line was read, so a remote gap behind ssh noise showed as plain "ok"
        self.remote_sources("ssh far-host", "ssh mute-host")
        body = ('if [ "$5" = mute-host ]; then echo "[]"; exit 0; fi\n'
                "echo '[]'\n"
                "echo '0 prompts included · 0 excluded (none) · 0 unknown provenance · sources: claude ~/.claude unknown (Permission denied)' >&2\n"
                "echo 'Connection to far-host closed.' >&2")

        result = self.kitchen("retro", "--since", "1d", cwd=self.home, env_extra=self.fake_ssh(body))

        self.assertIn("ssh far-host ok [claude ~/.claude unknown (Permission denied)], ssh mute-host unknown (no coverage line from the remote)",
                      result.stdout.splitlines()[-1])

    def test_home_only_reads_no_listed_source(self):
        # what a remote source runs, so two machines that list each other do not recurse
        self.write_claude([self.claude_event("dale")])
        self.remote_sources("ssh far-host")

        result = self.kitchen("retro", "--since", "1d", "--home-only", cwd=self.home, env_extra=self.fake_ssh("exit 255"))

        self.assertTrue(result.stdout.endswith("· sources: claude ~/.claude ok\n"), result.stdout)
        self.assertFalse((self.home / "ssh-args").exists())


if __name__ == "__main__":
    unittest.main()
