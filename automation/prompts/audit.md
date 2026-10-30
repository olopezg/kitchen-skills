You run the daily verification audit for this repository. You run unattended: nobody answers questions.

Follow the audit-verification skill (its file is in the run facts) as a **churn** pass from the baseline in the run facts, on the audited SHA your clone is checked out at. This is the skill's sealed scheduled pass:

- Your Bash runs in a sandbox with no network, no Docker and no credentials: you cannot launch, drive or control the app. The nightly guard already drove it on exactly the audited SHA. Cite its record, its step logs and the evidence the job copied (run facts) for steps 3 and 5. What they do not cover is a finding, never clean.
- The job read the baseline from the kitchen journal and logs your outcome there. Do not run `kitchen`, `gh` or `git push`; they are not available.
- Step 2's source wave: launch the read-only workers with the Agent tool, all at once, one per feature file in scope.
- Fixes: the run facts say whether they are authorized. When they are not, every drift is a finding and you commit nothing. When they are, you may fix doc drift only: Markdown prose in the paths the run facts list, outside fenced code blocks. The job refuses to publish anything else. A harness gap (a drive block, an assertion, the verifier's code, a control) must be driven live before it ships, and you cannot drive: report it as a finding.
- **changed**: every drift you found was doc drift, and all of it is fixed on your branch. Anything else you found is a finding, and the outcome is **findings**: when fixes are authorized, still commit the doc fixes you can make on your one branch, and list both in the report, the fixes on the branch and the findings left; when they are not, commit nothing and list the doc fixes you would have made among the findings.
- Never edit product code. Keep scratch notes in the work dir from the run facts, never in the clone.

Outputs, all in the work dir:

1. The report file: the skill's Reply, in Markdown. It is the evidence the owner reads; label each claim measured, inferred or cited.
2. The outcome file: exactly one word, `clean`, `changed`, `findings`, `blocked` or `skipped` (`skipped`: nothing in the churn touches a feature or the map).
3. Only for `changed`, or `findings` with doc fixes: one branch named as the run facts say, created from your current HEAD, with your commits, and the PR summary file (first line: the PR title; then what drifted, what you fixed and how you checked it).

Finish by printing one line: `AUDIT-OUTCOME: <word>`.
