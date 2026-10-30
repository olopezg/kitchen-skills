---
name: retro
description: "Retrospective over my Claude Code and Codex transcripts: find the corrections I had to repeat and propose the strongest fix for each, from code to memory. Proposes only; never applies. Use when I ask for a retro, or from the weekly automation."
disable-model-invocation: true
---

# retro

You are improving the environment the agents work in, not judging one session.

## Steps

1. **Extract.** `kitchen retro --since 7d` prints my prompts from both tools with project, session and time: every Claude account on this machine, Codex, and each host listed in `~/.config/kitchen/retro-sources.txt`. Widen the window when I ask.
   - **Validate provenance.** Its last line is the coverage: included / excluded by reason (notifications, subagents, automations, agent-launched and non-interactive runs, replayed history) / unknown provenance / sources, each with its state. A source that is `unreachable` or `unknown` was not read: name it in the report as a gap, never as zero. Unknown means an agent launched the same text elsewhere and the session cannot tell whether I typed it; unknown prompts are never counted as my corrections. Copy that line into the report. Before counting anything, skim `kitchen retro --since 7d --include-automated` for a non-human message that slipped through (a heartbeat, a delegation notice, a probe) or a human one that was excluded; name each one and leave it out of the counts. A count built on automated messages is a fake count.
2. **Find** what cost me attention: corrections ("no", "otra vez", "te dije", "eso está mal", "outdated"), instructions repeated across sessions, the same question asked twice, agents rebuilding the same script, and mistakes with real damage.
3. **Group** by root cause, not by wording. Keep a group with two or more occurrences, or one with real damage. Propose a fix only for a group an earlier retro report already listed, or one with real damage; a group seen for the first time goes to a **Watch** list with its count, for the next retro to check.
4. **Pick the strongest layer** that would have prevented it:
   1. impossible in code;
   2. guard test, lint or `kitchen check` rule;
   3. hook;
   4. the project's AGENTS.md;
   5. the `chef-mode` skill (how agents work in the kitchen), or my own global rules file for a personal preference;
   6. a skill;
   7. memory.

   A mechanical violation (a banned API, a file location, a command pattern) gets a deterministic check, full stop. Reserve written rules for judgment calls. Repeated manual work (agents rebuilding the same script) gets the script, in the repo.
5. **Prune.** Find rules and skills that change nothing, rules the code now contradicts, and checks that never fire. Read the previous retro report and say whether its accepted proposals stopped the corrections.
6. **Follow up.** Link each repeated failure to its enforcing check and last verified result. Distinguish a missing control from a skipped control or a wrong contract. Remove the rule that caused or now duplicates the failure.

## Output

Write the report to `~/.local/state/kitchen/retro/<yyyy-mm-dd>.md` and summarize it in the reply. When I said read-only (or the run cannot write there), print the full report in the reply instead and write nothing.

Start the report with the coverage line and any provenance corrections.

| # | Problem | Evidence (count, one quote, session) | Layer | Concrete change (file and text or test) | Cost |
|---|---|---|---|---|---|

Then a **Watch** list (first-time groups: count, one quote), a **Prune** list, a **Follow up** list and a **Last retro** check. Never apply a change; each one waits for my approval.

Principles (the kitchen's PRINCIPLES.md): `encode-lessons`, `truthful-state`.
