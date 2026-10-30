---
name: second-opinion
description: "Cross-model review of a diff before merge, or of a whole setup in design-review mode: reviewers run on the other model (every reviewer you set in `kitchen models`, in parallel, through T3's delegate_task or their CLI), on two separate axes (standards and the request), and the lead triages every finding into act on, consider, noted or dismissed. Use before merging or opening a PR, when I ask for a review, and always when a change touches auth, tenant scope, sync, data or migrations."
disable-model-invocation: true
---

# second-opinion

The value is a model other than the author looking at the final work. The lead's job is to filter that review, not to forward it.

## 0. Who wrote it

Identify the authoring model before anything else: `Co-Authored-By` trailers in `git log <base>..<head>`, the brief that produced the work, or the delegator's message. Say it in one line.

- You are a different model from the author: you are one reviewer. Apply the lenses from step 2 yourself, one pass per lens, on the same axes, and in step 3 run only the reviewers from `kitchen models` that are not you (another provider or another model); none configured, review alone. Never send it back to the author's model.
- You are the author's model: run the reviewers from `kitchen models` (step 3).
- You were launched as a reviewer by another lead: review and return; launch no reviewers of your own.
- Author unknown: say so and treat it as written by your own model.

## 1. Pin it

Pick the mode:

- **Diff review** (default): resolve the base ref and head SHA with `git rev-parse`; run `git diff --stat <base>...<head>`. A bad ref or an empty diff fails here.
- **Design review**: the whole setup, not a diff (rules, skills, automation, architecture). Pin the commit SHA and the exact list of files or folders in scope.
- **Design+diff**: both at once, when the change only makes sense against the whole setup. Declare it, pin both (range and file list), and report Design findings apart from the diff's Standards and Request findings.

State the intent in two lines and name the originating request: issue, my message, or spec.

## 2. Size the panel

| Work under review | Reviewers |
|---|---|
| Under 50 lines, 1–2 files | Skeptic |
| Up to 200 lines, up to 5 files | Skeptic, Architect |
| Larger, or any design or design+diff review | Skeptic, Architect, Minimalist |

Auth, tenant scope, sync, data and migrations always get the Skeptic, whatever the size, on the other model: the reviewers from `kitchen models`, with their effort and tier.

## 3. Run on the other model

Never use a same-model subagent as a reviewer; that defeats the purpose. The reviewers are the person's own setup: `kitchen models get reviewer --author <claude|codex>` (the author's family) prints one per line. Run every lens on every reviewer, all in parallel, and save each to `$DIR/<lens>-<n>.md`, n being the reviewer's line. Not configured (unless you review as another model, step 0), refused, or any one reviewer unreachable: stop BLOCKED with that error. No other model stands in, and no configured reviewer is skipped.

- **T3 Code** (`delegate_task` is available): one async `delegate_task` per lens and reviewer, role `review`, a distinct `clientRequestId` each. Target: the instance in `orchestrator_capabilities` whose `driverKind` is the provider (`codex`, `pi`, or `claudeAgent` for claude); the model id exactly as that instance lists it (omit it for `default`; an id it does not list is BLOCKED, since Pi runs the nearest model instead); the effort option (`reasoningEffort` on Codex, `effort` on Claude, `thinking` on Pi); the tier (`serviceTier` `default` or `priority` on Codex; `fastMode` on Claude; Pi has none).
- **Claude Code or Codex CLI**: `kitchen models get reviewer --author <a> --command` prints each reviewer's call, in the same order; append `"<prompt>"`, then `-o "$DIR/<lens>-<n>.md"` for codex or `> "$DIR/<lens>-<n>.md"` for claude and pi. The pi call has read tools only, no shell: write the diff to `$DIR/diff.patch`, give it a checkout at the pinned head, and authorize no tests.
- **Anywhere a reviewer's provider cannot be reached** (a cloud session without it): BLOCKED; say which provider was missing.

Build each prompt from [reviewer-prompt.md](reviewer-prompt.md). Before reading, confirm every output file exists and is not empty. A missing review is a gap, not a pass.

## 4. Lead judgment

You hold the full context; the reviewers saw a diff or a file list. Merge each lens's reports first: a finding two reviewers both report is one finding, marked as agreed. For every finding, trace the code: a hypothetical input the callers can never pass is not a finding. Every finding you keep carries:

- its **axis**: Standards, Request or (design and design+diff reviews) Design;
- its **evidence**: measured (the command you ran and what came out) or inferred (the `file:line` you read);
- its **principle**, when one applies: the PRINCIPLES.md id it breaks.

Then place it in exactly one bucket:

- **Act on**: a real, concrete P0 or P1, however many there are. Only these are fixed before merge (chef-mode's review budget).
- **Consider**: real but optional, or a judgment call for me.
- **Noted**: true, not worth action now.
- **Dismissed**: with the reason. This list is a trust mechanism: it lets me overrule you.

If every finding is a nit, the work is probably fine; say so. Be slow to dismiss security and correctness findings, especially when two reviewers agree.

Keep the axes separate. Do not merge or rerank Standards findings against Request findings.

## 5. Merge danger

One line each: two-way or one-way door; blast radius; the one fact the change is safe because of, and how far it is proven (said, pointed at the line, walked the failure, ran it, reproduced it in the app).

For permissions, publication, migrations, or verification changes, test the critical safety invariant across its real boundary or a failure sequence. State what remains unproven. Reuse deterministic gates; do not repeat them without a changed input or unresolved concern.

## Reply

Verdict in one line; author model and review route (direct, reviewers on which CLI, or both); Act on; Consider; Noted; Dismissed; Merge danger; reviewers run (route: delegate_task or CLI; provider, model, effort, tier; output files). Change no code during the review unless I asked you to resolve the findings.

Principles (the kitchen's PRINCIPLES.md): `cross-review`, `evidence`, `no-fallbacks`.
