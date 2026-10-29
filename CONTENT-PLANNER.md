# Weekly Instagram content planner

`ig_content_planner.py` turns the existing analytics and queue into a seven-day
editorial review roadmap. It writes **only** `ig_content_plan.json` and
`ig_content_plan.md`. It does not generate media, call Meta, publish, edit either
queue, change posting frequency, or require an AI key or paid service.

## Run locally

```sh
python -m unittest -v test_ig_content_planner
python ig_content_planner.py
# Reproduce a dated plan exactly:
python ig_content_planner.py --start-date 2026-10-29
# Optional isolated outputs:
python ig_content_planner.py --input-dir . --output-dir /tmp/amiees-plan --start-date 2026-10-29
```

Python 3.9+ and IANA timezone data are needed. GitHub runs Python 3.13 on Ubuntu.
No third-party Python package is required on that runner. Windows Python needs
access to the `America/New_York` timezone database.

Without `--start-date`, the first review day is tomorrow in America/New_York.
All calculations are deterministic for identical input bytes and start date.
There is no random sampling, network access or wall-clock timestamp in the output.
Queue release times without offsets are UTC, as specified by this repository.
Local dates and offsets are calculated with timezone rules, including the November
clock change.

## Inputs and schedule protection

- Required: `ig_learning.json`, `ig_analytics.json`, `ig_queue.csv`.
- Upcoming Nature Series posts come directly from queued `ig-nature-*` rows.
  No separately maintained calendar is needed.
- Optional: `ig_state.json`, read only, resolves published rows that analytics
  has not collected yet. Without it, unresolved rows stay reserved.
- Held rows never supply candidates. Candidates need a queued product and existing
  image references. These are creative candidates, not a live inventory check.
- Protect products scheduled from seven days before the review week through
  fourteen days after its last day, plus unresolved overdue entries. Recently
  published products also have a seven-day quiet period.
- Protect classified concepts and exact/fuzzy opening hooks in that schedule.
  Explicit `concept` or `content_angle` columns are honored when present. Current
  Nature Series rows are categorized as nature-fact stories. Without explicit
  labels, other concepts use documented keyword rules in the script. This is
  bounded lexical matching, not full semantic understanding.
- Every proposed product and concept is used at most once per plan. If there are
  fewer than seven safe ideas, the remaining days are observation days. The planner
  never relaxes protection to fill a quota, even for a controlled follow-up.

The plan dates are **review dates**, not approved extra posting slots. The daily
Nature Series is already booked. An editor must choose or defer a test and decide
how it fits before any future queue change. Do not append all seven ideas as extra
daily posts.

## Conservative evidence rules

The planner reads learning context but recalculates evidence from raw analytics
counts. It deliberately does not copy upstream winner labels, format rankings or
posting-time recommendations. It prefers retests with mature comparators, then
uses a stable weekly product rotation for exploration. Tiny rate differences and
likes do not rank products.

- One distinct published media ID is one sample. Daily history entries are not
  extra samples. Only the latest metric snapshot is used for evidence.
- A comparator needs a known snapshot at least 72 hours after publication.
  Missing timestamps and future snapshots do not qualify.
- Missing counts remain unknown. Measured zeros stay zero. Views never stand in
  for reach. Sum of post reach is explicitly not a unique audience total.
- Confidence is low unless there are at least three mature, same-format posts
  for that product, each with reach of 100 or more and at least ten measured
  primary actions across those posts. Even then, confidence is only medium and
  descriptive. The planner never awards high confidence or declares a winner.
- Data older than two days relative to the plan start, future-dated data, and
  undated data keep confidence low. Learning/analytics mismatch is disclosed.
- Behavioral metrics use the raw action count divided by reach, with both raw
  counts retained. If that format has no measured action metric with known reach,
  the objective falls back to establishing reach; it does not claim behavioral
  learning. Reach-only fallback confidence always remains low.

For a controlled follow-up, the plan names the historical reference and changes
only the opening caption line. Product/variant, exact images/order, format,
remaining caption, CTA/offer and posting-time window must remain fixed. Verify
the exact reference assets before approval. An exploratory test is labeled as
such and is not presented as a controlled comparison.

Historical lifetime counts are context, not a matched-age control. Capture control
and variant results at 72 hours. Review a direction only after at least three
comparable posts per condition, reach of at least 100 for each and ten primary
actions per condition for action metrics. These are cautious editorial guardrails,
not a power calculation or proof of significance. Several planning cycles may be
needed. Low reach means keep learning, not automatically post more or spend more.

## Workflow

`.github/workflows/instagram-content-planner.yml` runs Sundays at **23:17 UTC**
(7:17 PM EDT / 6:17 PM EST), after the existing daily analytics schedule. Its default
review week is Monday through Sunday. Manual dispatch supports an optional start
date. Changes to planner code, tests or its workflow also trigger verification and
generation on main; pull requests run tests with read-only permissions.

The generation job has repository contents write permission and commits only the
two generated plan files. No publishing token is passed to it. Existing analytics,
Instagram publication and Pinterest workflows are unchanged. Output-only commits
do not match this workflow's push path filter.

If another job advances main, the disposable runner fetches and regenerates from
the latest inputs before retrying a normal, non-forced push, up to three times.
Failures remain failed runs; they never trigger a content submission. The readable
plan is also included in the Actions job summary.

## Review checklist

Check stock, price, product fidelity, exact media, rights, caption claims and the
destination before activating any idea. No proposed test has publishing approval.
After a few reviewed cycles, generation can be connected as a separate change.
