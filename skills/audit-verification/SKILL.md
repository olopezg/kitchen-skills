---
name: audit-verification
description: "Periodic check that a project's verify-<repo> skill still tells the truth: the feature map matches the code, every feature is driven live, and the verifier can still fail. Ends clean, changed, findings, blocked or skipped. Use when I ask to audit verification, or from the daily automation."
disable-model-invocation: true
---

# audit-verification

A feature map starts to rot the moment the app changes, and a verifier that cannot fail is worse than none.

## Outcomes

Say which one:

- **clean**: a full pass covered every feature from source and live; nothing to change. A churn pass that covers its whole scope says **clean (churn)** and lists its features; it claims nothing about the rest.
- **changed**: one PR with proven corrections to the verify skill.
- **findings**: the audit found drift or gaps it could not fix (not authorized, or beyond what the pass may ship); report them with evidence. With fixes authorized, a sealed pass still ships its prose fixes in one PR alongside.
- **blocked**: coverage could not finish or a fix could not ship safely; say exactly why.
- **skipped**: a churn pass found no change since the last audit; it claims no coverage, so never call it clean.

## Scope

A **full** pass covers every feature file. A **churn** pass (the daily run) covers only the features whose code, or whose map file, changed since the last audit's SHA, plus the churn sweep in step 1. The baseline is the latest `audit-verification` entry in the `journal` list of `kitchen status --json --since 30d <repo>` (the text view shows only five entries) whose outcome is clean, clean (churn), changed or skipped, or a sealed pass's `findings churn at <sha> (issue #N)`: its findings live in that issue. Any other blocked or findings audit never moves the baseline: what it left uncovered stays in scope. With no baseline, run a full pass. Say which scope ran and from which baseline.

## Sealed pass

The daily automation (`automation/bin/daily-audit`) runs a churn pass sealed: no network, no Docker, no credentials, so it cannot drive the app, run a control, read the journal or open a PR. Its job runs it only after the nightly guard went green on the audited SHA, and hands it the baseline, the guard's record, step logs and evidence. Afterwards the job logs the outcome, publishes the PR and files findings in the project's one open findings issue (label `audit-verification-findings` by default). In a sealed pass:

- what the guard's proof and control do not cover is a finding, never clean;
- a fix to what runs (a drive block, an assertion, the verifier, a control) needs a live drive before it ships, so it is a finding; only doc drift in prose is fixed, and the job refuses anything else;
- any finding makes the outcome **findings**; its prose fixes still go on its one branch, and the job publishes them as for **changed**.

## Edit scope

Only the verify skill's own folder, and only when fixes are authorized; otherwise the outcome is **findings**. Never product code. When the map describes behavior the app no longer has, it is either doc drift (fix the map) or a product regression (report it; never edit the map to hide it).

## Three things to keep apart

- **Inventory**: what the map says exists (features, routes, entry points).
- **Drives**: what the verifier actually exercises live, and how.
- **Assertions**: what it checks after each drive, and whether that check can go red.

A feature can be in the inventory, driven, and still asserted by nothing. Report the three separately.

## Pass

0. **Locate** `.agents/skills/verify-*/`. None: stop and point to the template in the chef-mode skill, `../chef-mode/playbooks/init/verify/`, and to its init playbook. State the audited profile: environment, build or commit SHA, data set, account or role, and which verifier commands or flags. Every result below holds only for that profile.
1. **Inventory**: the map's README against its files; fix missing, extra, duplicate or dead entries. Sweep the churn since the last audit's SHA (`git log <sha>..HEAD`) for user-facing surfaces missing from the map; require a concrete path before calling one missing.
2. **Source wave**: one read-only worker per feature file in scope, all launched at once. Each answers "how does this feature work for a user?" from source and returns: a summary, the source entry points, likely drift with `file:line` (or none), and one live recipe. Workers never drive the app and never edit. Reconcile: every feature in scope has a summary; spot-check the cited drift; merge the recipes into as few app states as practical.
3. **Drives**: every feature in scope is exercised at least once. If the project's scheduled proof already ran on this exact SHA with the audited profile (same environment, data set, account and verifier flags), cite its record (command, time, result) and its coverage evidence (for example, the ledger of the calls that ran) instead of rerunning it, and drive live only what that proof does not cover; a red record makes the outcome **blocked**. With no such record, or a different profile, run the full proof yourself. Hold three invariants for the whole pass, whatever fails:
   - never drive an instance you have not health-checked since it last surprised you: doctor before the first drive and after any failed drive; where doctor cannot see the fault (a wedged state on a healthy process), reset or relaunch;
   - evidence captured so far survives every cleanup; check it at its named location, do not assume it;
   - nothing a drive started outlives it; clean a failed attempt's residue, and tear down after the last drive, re-proofs included.
   A doctor failure caused by skill drift is drift: fix it within the edit scope and retry once before calling the pass blocked. A sealed pass drives nothing itself: it cites the guard's record, and the rest is a finding. A feature is "unreachable" only with the concrete prerequisite and the route attempted; a prerequisite the map omits is drift.
4. **Assertions**: for each feature, name the check that decides pass or fail. A drive whose only check is "no error" or "exit 0" is a gap.
5. **Can it fail?** If the verifier ships its own control command and the scheduled proof ran it green on this SHA, cite that record and still run one control by hand on a feature in scope. A sealed pass cannot: it cites the record and reports, as a finding, each feature in scope whose assertions changed since the baseline, owed a hand control. Otherwise, in a scratch copy, run both controls for at least one or two features:
   - **negative control**: break one expectation or inject a known fault; the verifier must go red, on that assertion. At least one negative control must nullify the execution or its effect (skip the drive, stub the action to a no-op, drop the write), not only change an expected value: a verifier that never checks the effect stays green when nothing happened;
   - **positive control**: the unbroken copy, same command; it must be green.
   Red on both, or green on both, means the verifier is not measuring the feature. If it stays green under the fault, that is the most important finding of the audit.
6. **Triage**: doc drift, fix it; harness gap, fix it and drive it again live before it ships (a sealed pass reports it as a finding); product gap, report it outside the PR. Without authorization to fix, all of them are findings.
   Never turn a known product defect into a green expectation. If the verifier must keep running past it, mark it as an explicit expected failure (XFAIL) that stays visible in every run and goes red the day it starts passing, with the defect's reference.
7. **Ship or stop**: for **changed**, re-read every changed file, then open one PR. Otherwise no branch, except a sealed pass's prose fixes. Either way, run `kitchen log "audit-verification <outcome> <full|churn> at <audited full sha>"` from the project (add `--status blocked` when blocked) so the next churn pass knows where to start; a sealed pass writes its outcome where its prompt says, and its job logs it and opens the PR. A full pass that ends clean or changed closes the open findings issue with a comment naming the audited SHA (`gh issue close <n> --comment ...`), and says so; nothing else closes it, since a churn pass covers only its scope. Keep run notes in scratch; never commit them.

## Reply

Outcome; scope (full or churn, and the features in it); audited profile; scheduled records cited; inventory drift (fixed or found); features driven and unreachable ones with their prerequisite; features without a real assertion; product gaps and any XFAIL added; and each control run: command, expected, actual.

Principles (the kitchen's PRINCIPLES.md): `prove`, `truthful-state`.
