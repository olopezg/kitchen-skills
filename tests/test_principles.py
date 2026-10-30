"""Every control names the principle it enforces, and every name is a principle in PRINCIPLES.md.

A control is a guard, a scheduled job, a CLI module or a skill. They are found by
globbing, so a new one is held to this suite the moment it lands; a helper that enforces nothing is excused by name
in NOT_CONTROLS, with the reason.
"""
import re
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
NOT_CONTROLS = {
    "hooks/shellparse.py": "the parser the guards share",
    "automation/bin/install-schedule": "writes launchd and systemd units; the jobs it schedules are the controls",
    "lib/kitchen/__init__.py": "package marker",
    "lib/kitchen/checkplan.py": "picks which test modules a commit runs",
    "lib/kitchen/common.py": "paths and git helpers",
    "lib/kitchen/environment.py": "tool and schedule probes that doctor prints",
    "lib/kitchen/inventory.py": "lists what the agents can see",
    "lib/kitchen/notify.py": "sends a local notification for the controls that decide to",
}
CANDIDATES = [REPO / "bin" / "kitchen", *REPO.glob(".github/workflows/*.yml"), *REPO.glob("hooks/*"), *REPO.glob("skills/*/SKILL.md"), *REPO.glob("automation/bin/*"), *REPO.glob("lib/kitchen/*.py")]
CONTROLS = sorted(p for p in CANDIDATES if p.is_file() and p.name != "README.md" and str(p.relative_to(REPO)) not in NOT_CONTROLS)
HEADING = re.compile(r"^## \d+\. .+? \(`([a-z-]+)`\)\s*$", re.MULTILINE)
DECLARATION = re.compile(r"Principles(?: \([^)]*\))?:\s*(.+)")
NAME = re.compile(r"`([a-z][a-z-]*)`")


CHEF_MODE = REPO / "skills" / "chef-mode" / "SKILL.md"


def declared(path: Path) -> list[str]:
    match = DECLARATION.search(path.read_text(encoding="utf-8"))
    return NAME.findall(match.group(1)) if match else []


def enforced_by_a_control(controls) -> set[str]:
    """chef-mode names every principle to switch them on; it enforces none, so it cannot vouch for one."""
    return {n for path in controls if path != CHEF_MODE for n in declared(path)}


class Principles(unittest.TestCase):
    def setUp(self):
        self.ids = HEADING.findall((REPO / "PRINCIPLES.md").read_text(encoding="utf-8"))

    def test_principles_parse_with_unique_ids(self):
        self.assertGreaterEqual(len(self.ids), 13, "PRINCIPLES.md headings must read `## N. Title (`id`)`")
        self.assertEqual(len(self.ids), len(set(self.ids)))

    def test_every_control_names_known_principles(self):
        for path in CONTROLS:
            with self.subTest(control=str(path.relative_to(REPO))):
                self.assertTrue(path.is_file(), "a listed control is missing")
                names = declared(path)
                self.assertTrue(names, "no `Principles:` line naming at least one principle")
                self.assertEqual([n for n in names if n not in self.ids], [], "names a principle PRINCIPLES.md does not define")

    def test_every_excused_helper_still_exists(self):
        self.assertEqual([p for p in NOT_CONTROLS if not (REPO / p).is_file()], [], "drop a gone helper from NOT_CONTROLS")

    def test_the_reviewer_effort_lives_only_in_kitchen_models(self):
        # one source of truth: a level written here would contradict what each person sets with `kitchen models`
        level = re.compile(r"\b(low|medium|high|xhigh|max)\s+(reasoning\s+)?effort\b", re.IGNORECASE)
        for rel in ("PRINCIPLES.md", "skills/chef-mode/SKILL.md", "skills/second-opinion/SKILL.md", "skills/second-opinion/reviewer-prompt.md"):
            with self.subTest(file=rel):
                text = (REPO / rel).read_text(encoding="utf-8")
                self.assertEqual([m.group(0) for m in level.finditer(text)], [], "set it in `kitchen models` instead")

    def test_chef_mode_names_every_principle(self):
        # chef-mode is the one place the kitchen's rules switch on; a principle it leaves out never reaches an agent
        self.assertEqual(sorted(declared(CHEF_MODE)), sorted(self.ids))

    def test_chef_mode_asks_each_decision_for_its_door_on_one_line(self):
        # the owner's Hermes seat profiles read the label from the coordinator's own line: without it they never
        # treat a decision as two-way («quien calla otorga», hermes-config 2026-10-30)
        reporting = CHEF_MODE.read_text(encoding="utf-8").split("## Reporting", 1)[1].split("\n## ", 1)[0]
        for label in ("`(two-way)`", "`(one-way)`", "`(dos vías)`", "`(una vía)`", "one line"):
            with self.subTest(label=label):
                self.assertIn(label, reporting)

    def test_chef_mode_alone_vouches_for_no_principle(self):
        # regression: while chef-mode counted as a control, the check below could never fail (audit, 2026-10-26)
        self.assertEqual(enforced_by_a_control([CHEF_MODE]), set())

    def test_chef_mode_reads_this_principles_file(self):
        link = REPO / "skills" / "chef-mode" / "PRINCIPLES.md"
        self.assertTrue(link.is_symlink(), "a copy would drift: link it to the repo's PRINCIPLES.md")
        self.assertEqual(link.resolve(), (REPO / "PRINCIPLES.md").resolve())

    def test_every_principle_is_enforced_or_says_rule_only(self):
        text = (REPO / "PRINCIPLES.md").read_text(encoding="utf-8")
        sections = dict(zip(self.ids, re.split(r"^## \d+\. ", text, flags=re.MULTILINE)[1:]))
        cited = enforced_by_a_control(CONTROLS)
        for pid in self.ids:
            with self.subTest(principle=pid):
                enforced = re.search(r"^- \*\*Enforced by:\*\* (.+)$", sections[pid], re.MULTILINE)
                self.assertTrue(enforced, "no `Enforced by:` line")
                self.assertTrue(pid in cited or enforced.group(1).startswith("rule only"),
                                "no control names this principle, and its `Enforced by:` does not start with `rule only`")


if __name__ == "__main__":
    unittest.main()
