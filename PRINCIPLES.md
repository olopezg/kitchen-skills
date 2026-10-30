# Principles

How we work with agents, and why. The rules of the `chef-mode` skill, the guards each project carries, `kitchen check`, `kitchen status` and the init playbook are these principles made concrete. The kitchen is on only in chef mode: an agent reads this file when the owner types `/chef-mode`. When no rule covers your case, decide the way these point.

A kitchen, not a factory: the goal is quality the owner can trust without tasting every dish. The owner is the person whose kitchen it is, the one who runs the agents. Skills, rules, checks and the codebase are the ingredients and the knives, and every chef owns their knives: take ideas, not installers.

Each principle has an id in backticks; a control that enforces one names that id, and `tests/test_principles.py` fails when a control names none. Origins: the conversation between Matt Pocock and poteto on agent workflows (the video), poteto's pstack principles (all 24 are folded in below under their own names), the kitchen deck, and the owner's corrections and incidents, cited by PR.

## 1. Prove it on the real thing (`prove`)

Done means you saw the observable result: the app, the command output, the record. "It compiles", a green proxy or a worker's summary is not proof. A test must fail without the change; its expected values come from an independent source, never recomputed the way the code does. No test beats a tautological one.

- **Why:** a wrong "done" costs more than the check, and a test that cannot fail catches nothing. Verification is what turns a prompt into a loop (the video); agents love tautological tests (the video). The kitchen's own review found controls reporting success they had not earned (PR #22).
- **When:** before you claim anything is done, fixed or safe.
- **Includes (pstack):**
  - *Prove It Works*: check the real artifact; script the check and keep its output so anyone can rerun it.
  - *Test Behavior, Not Implementation*: would the test still pass if every function it imports returned `undefined`? Then it tests nothing. Watch for weak assertions, mock-only tests, self-referential expectations, pinned constants and fixtures asserting fixtures.
  - *Sequence Work into Verifiable Units*: small units, each ending in a check; do not start the next until it is green.
- **Enforced by:** the init playbook (every `bin/check` command run and seen to pass, the commit tier green twice and red on a planted defect in a file it runs); `kitchen integrate`; the nightly guard; the gardener reruns its verify steps before publishing; the `audit-verification` skill, and the daily audit that runs it only on a green guard and never ships a fix nobody drove live; the Skeptic lens of `second-opinion`.

## 2. Unknown is never green (`truthful-state`)

Never invent counts, freshness, progress or success. A read that failed is `unknown`, a skipped check is `skipped`, a run without steps is `incomplete`.

- **Why:** the owner acts on what the screen says. A `0` that means "could not read" hides the exact failure the screen exists to show: `status` once turned a failed `git status` into `dirty: 0` (PR #22), and a product dashboard showed `0` instead of "unknown" for failed requests.
- **When:** every report, every status line, every automation you write.
- **Includes (pstack):**
  - *Explain the Number*: before trusting a measured number, name what limits it and what else it could be measuring. Ask "why not double?" Keep run count and spread next to it.
- **Enforced by:** `status` prints `unknown`, never `0` or `none`; the nightly records `incomplete` and is never green without steps; `integrate --recorded` reuses a PASS only on the same SHAs and check digest; `retro` prints its coverage line; `second-opinion` treats a missing review as a gap.

## 3. No fallbacks; fail loud (`no-fallbacks`)

When the intended operation fails, surface the failure. No alternative path, substitute value or degraded mode, and no other model standing in for the configured one. If you find an existing fallback, report where it is, when it fires and what it hides; change it only with the owner's approval.

- **Why:** a fallback turns a failure into a quiet wrong answer that you find much later, in the wrong place, and the system claims it worked. The owner's own rule, from repeated corrections.
- **When:** error handling, defaults, retries, "just in case" branches, model or tool substitutions, compatibility layers.
- **Includes (pstack):**
  - *Outcome-Oriented Execution*: optimize for the verified end state, not for smooth intermediate states; temporary compatibility code becomes permanent debt.
  - *Migrate Callers Then Delete Legacy APIs*: move every caller and delete the old path in the same change; no dual paths. If that removes behavior someone relies on, or the API is published, it is a one-way door (`doors`).
- **Enforced by:** every hook blocks with exit 2 when it cannot read its input or parse the command; `integrate` has no default base; `install` refuses a file it cannot parse or does not manage; `kitchen models` fails, with no default, when a role is not configured. In project code: rule only.

## 4. Fix the cause, not the symptom (`root-cause`)

Reproduce first, red. Form hypotheses that can be falsified. Fix the cause, then the same pattern elsewhere. No silencing guards.

- **Why:** symptom fixes pile up and the bug comes back somewhere else; agents use a comment as an excuse for a patch. The deck's bug flow: red repro, failing test, minimal fix.
- **When:** anything broken, failing, flaky or slow whose cause is not known.
- **Includes (pstack):**
  - *Fix Root Causes*: ask why until you reach the cause; grep for the pattern and fix every instance; for "fails after restart", suspect stale state first.
  - *Attack the Premise*: when two fixes that share a premise fail the same gate, write the premise down and test it before a third fix.
- **Enforced by:** the `find-the-cause` skill. Otherwise rule only.

## 5. A repeated correction is an environment defect (`encode-lessons`)

If the owner has to say it twice, the environment is wrong, not the session. Fix it at the strongest layer that would have prevented it: impossible in code, then a test or lint, then a hook, then a project AGENTS.md rule, then a skill, then memory. A mechanical rule gets a machine check; prose is for judgment calls.

- **Why:** "If someone always trips in the same place, you fix the floor; you don't ask them to be careful" (poteto, in the deck). Correct the environment, not the one agent (the video). Three rules that used to be text are now hooks (PR #31).
- **When:** every correction, every review miss, every incident.
- **Includes (pstack):**
  - *Encode Lessons in Structure*: the second time you write the same instruction, turn it into a check and delete the text.
  - *Build the Lever*: for repeated or bulk work, build the script, codemod or generator and put it in the diff; "trust me" becomes "run this".
- **Enforced by:** the `retro` skill and the weekly retro job; attack corpora in `tests/corpus/` (a miss found in review becomes a bypass case); `kitchen check`; `tests/test_principles.py`.

## 6. Spend the owner's attention only on decisions (`owner-attention`)

The owner's attention is the scarcest resource in the kitchen. On reversible work, do it and show the result; do not ask. Controls run and read themselves and surface only exceptions, in one line. When you need the owner, lead with the decision, number it, and give your recommendation so they can reply "1 yes, 2 B".

- **Why:** the owner was the bottleneck: dozens of "how is it going?" and "push it to dev" in one week (the deck), 18 merges approved by hand. "Where am I the bottleneck?" and "sample instead of block" (the video). A control the owner has to read is a cost, not a safeguard: a control must buy autonomy (PR #22).
- **When:** planning, reporting, designing any check or automation.
- **Includes (pstack):**
  - *Never Block on the Human*: proceed on reversible work and let the owner correct afterwards. pstack lets external actions proceed; the kitchen does not: one-way doors (`doors`) and anything sent to people or published still wait for the owner's explicit request.
- **Enforced by:** the kitchen stays off until the owner types `/chef-mode`, and `install` writes nothing every session reads; `status --exceptions` (prints only non-green, exits 1); notifications only for red, incomplete, blocked or decision; the nightly opens one issue while red and closes it when green; the daily audit files its findings in one issue per project; the init playbook asks the owner once, only about what the repo cannot answer. Reply format: rule only.

## 7. Know your doors (`doors`)

A two-way door is cheap to revert: walk through it. A one-way door is not: schema migrations, data deletion, auth and tenant scope, published contracts and installers, production deploys, force-pushes and history rewrites on shared branches, removing behavior someone relies on. For a one-way door, sketch two structurally different options and get the owner's approval before executing. A two-way door goes as far as the project's rung on the trust ladder, set by the owner in its AGENTS.md: `propose` (the owner merges), `merge` (the agent merges its own verified, reviewed pull requests) or `ship` (it also lands stacks unattended and deploys outside production).

- **Why:** speed is safe only where mistakes are cheap, and autonomy on two-way doors is what the owner can grant. Two-way and one-way PRs came up in the video; the deck set the policy: two-way goes verify, second opinion, merge, and the owner samples; one-way goes two designs, approval, a specific verification. The owner chose the ladder (2026-10-06): more autonomy where trust is earned, so the agent merges what is not critical once a project has its guardrails.
- **When:** before any action you could not undo with one command.
- **Includes (pstack):**
  - *Exhaust the Design Space*: the options for a one-way door must differ in shape; "a second flavor of the first shape does not count".
- **Enforced by:** the guards each project carries, `deny-shared-push`, `deny-no-verify`, `deny-recursive-rm` (move to the Trash instead); the rung rules of `chef-mode`; the init playbook prints one-way doors such as rulesets as decisions, never runs them, and the owner merges its pull request; the gardener only opens a PR, within a line budget and outside protected paths; `kitchen check` blocks private data in this public repo.

## 8. Say how you know (`evidence`)

Label every claim: measured (you ran it; say what and what came out), inferred (you read code or docs; say where) or a guess. Never hand the owner a check you could have run yourself.

- **Why:** the owner calibrates trust on the label; an inference presented as a measurement is how wrong decisions get made with confidence. The labels compress the deck's evidence ladder: reproduced it in the app, ran it, showed it cannot happen, pointed at the line, said it.
- **When:** every report, review finding and handoff.
- **Enforced by:** `second-opinion` findings carry measured or inferred evidence and the principle they touch; the init pull request carries each command and its output. Otherwise rule only.

## 9. Small, owned, short enough to read (`small-owned`)

Prefer the smallest change that does the job. Skills are workflows, not manuals: mechanical steps go into scripts, detail into referenced files. Keep the context window for the work: send bulky reading to workers and keep their conclusions. Few dependencies, all pinned.

- **Why:** what an agent does not read, it does not follow. Move the deterministic parts into code and leave only judgment in the skill; skills get smaller as models improve (the video). "If a rule is enough, it is not a skill" (the deck). The kitchen replaced a large suite with a few hundred owned lines (PR #21).
- **When:** writing a skill, rule or tool; choosing a dependency; deciding what to load.
- **Includes (pstack):**
  - *Laziness Protocol*: the most result from the least code; if answering a question means tracing more than three files or layers, flatten it.
  - *Subtract Before You Add*: remove complexity before building; no speculative guards; delete stubs with nothing in them.
  - *Minimize Reader Load*: can a new reader answer "where does X come from?" and "what can change X?" in under 30 seconds?
  - *Guard the Context Window*: route bulk to workers; keep summaries, not raw payloads.
- **Enforced by:** `kitchen check` (`SKILL.md` at most 150 lines, one-line descriptions, folder name equals `name`); the Minimalist lens of `second-opinion`. Standard library only: rule only.

## 10. Separate before you share (`isolate`)

Parallel work runs in separate worktrees or clones, one writer each. Shared state is integrated at the end, on exact SHAs, in a disposable place. Operations converge: running twice leaves the same result.

- **Why:** instructions are not concurrency control, and a rerun should never become a debugging session. Measured here: `GIT_*` variables leaking from a worktree wrote junk commits and `core.bare=true` into the shared repo (PR #22).
- **When:** delegating, scheduling jobs, writing installers and state files.
- **Includes (pstack):**
  - *Separate Before Serializing Shared State*: remove the shared write target first; serialize only when sharing is a real invariant.
  - *Make Operations Idempotent*: what happens if it runs twice, or crashed halfway? If the answer depends on leftover state, add reconciliation.
- **Enforced by:** `kitchen integrate` (disposable clone, exact SHA vector); the automation project lock; `install` and `kitchen guards` rerun byte-identical, and the guards run only in the project that carries them; the init playbook works in its own worktree; tests use a throwaway repo and `HOME` and drop `GIT_*`.

## 11. Another model checks the work, on a budget (`cross-review`)

The author's model does not review its own work. Risky diffs (auth, tenant scope, sync, data, migrations) always go to the other model. Which model, at what effort and tier, is each person's setup in `kitchen models`, never written here. Two rounds per PR: one open review, then one pass that only reruns the first round's repros. Only P0/P1 the lead confirmed are fixed; the rest is listed in the report. Never upgrade ISSUES, BLOCKED or missing evidence to PASS.

- **Why:** a different model sees what the author is blind to ("the other model reviews", the deck). The budget is the owner's call (PR #22): open-ended rounds kept finding new blockers instead of converging. Three-model panels at maximum effort cost too much for small and medium work (the deck), so the reviewers are configured once in `kitchen models`: one unless the owner adds more, and each added one runs every lens (owner, 2026-10-12).
- **When:** before merge, and whenever a worker reports back.
- **Enforced by:** the `second-opinion` skill, with the reviewers taken from each person's `kitchen models` setup; it stops BLOCKED when any of them cannot be reached; `kitchen models` refuses a reviewer whose model is the author's family. Budget and effort: rule only.

## 12. Write it where the next agent will find it (`handoff`)

Every brief stands alone: goal, scope, exact branch or SHA, how to verify, and a report of PASS, ISSUES or BLOCKED. Decisions that should outlive the session go to the project's `decisions.md`; check it before asking the owner again. Autonomous runs leave checkpoints with `kitchen log`.

- **Why:** the next agent has no memory of this conversation, and asking a decision already made spends the owner's attention twice. Past transcripts are the process materialized (the video); the `retro` skill mines them.
- **When:** delegating, ending a session, taking a decision.
- **Enforced by:** the init playbook (`decisions.md`, and a `## Verify` section in the project's AGENTS.md); `status` shows owed decisions, checkpoints, and what an older install left in the global setup; the `handoff` skill.

## 13. Make the right thing the easy thing (`design`)

Shape the code so the wrong thing is hard to write. Get the data and the scaffolding right before the logic, put the domain in structures rather than scattered conditionals, and validate at the edges so the inside can trust its types.

- **Why:** agents take the shortcut the code allows; "how do I make the easy thing the right thing?" and "there's really only one way to do something" (the video). The codebase is the agents' memory: every copy of a bad pattern makes the next one likelier.
- **When:** designing features, data, APIs and modules in a project. The project's AGENTS.md and lint config carry the specifics.
- **Includes (pstack):**
  - *Foundational Thinking*: data structures and scaffolding (CI, lint, tests, shared types) first; ask "what if another actor changes this at the same time?"
  - *Redesign from First Principles*: fold a new requirement in as if it had been there from day one, through every type, doc and example.
  - *Model the Domain*: a state machine, union or registry instead of an if/else that grows a branch per feature, or two booleans that must stay in sync.
  - *Boundary Discipline*: validate where data enters the system; keep business logic pure.
  - *Type System Discipline*: make illegal states unrepresentable; if you can write a comment explaining when a combination of fields is valid, use a sum type.
  - *Experience First*: fewer, finished features over many rough ones; the next maintainer and the library's importer are users too.
- **Enforced by:** rule only. Each project encodes its own in types and lint (`encode-lessons`).
