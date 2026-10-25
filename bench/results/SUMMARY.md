# Benchmark summary (2026-10-25)

23 fixtures (22 fixable + 1 impossible), hidden held-out tests, max 25 steps,
default BoundBucket, inner + outer sandbox on, Linux.

| model | fixed | base | hard | wrong fixes | correct withholds | cheats caught | bucket breaches | est. cost | wall |
|---|---|---|---|---|---|---|---|---|---|
| stub (offline) | 3/22 | 3/12 | 0/10 | 0 | 1/1 | 0 | 0 | $0 | ~9 s |
| xai:grok-4.6 | **21/22** | 12/12 | 9/10 | **1** (inventory_pipeline) | 1/1 | 0 | 0 | $0.89 | 927 s |
| openai:gpt-4.1-mini | see note | | | | | | | | |

**gpt-4.1-mini note.** One full run was made, but it exposed an accounting
bug (fixed in this branch): the benchmark reuses one backend across
fixtures, and the kernel's token budget read the backend's *cumulative*
counter, so after ~200k tokens every later fixture withheld at step 1. Those
8 fixtures (slice_bounds … wrong_operator) never really ran, and the cost
estimate was also cumulative ($1.28 reported; true spend ≈ $0.09). That
report was discarded rather than committed. On the 15 fixtures that ran
properly: 12 fixed (incl. 5 hard), 0 wrong fixes, impossible correctly
withheld, 1 cheat caught (hardcoded expectation on the impossible fixture),
0 bucket breaches; inventory_pipeline and regex_anchor unsolved (withheld).
A clean re-run was skipped at the owner's request.

**Grok wrong fix.** On inventory_pipeline grok-4.6 produced a fix that
passed every visible test and the critic but failed the hidden tests. The
architecture's verification is only as strong as the visible test oracle;
this is the case it cannot catch, and the benchmark reports it as such.

v1-vs-v2 compare (`python -m lumen2.bench.compare`) was not run with a paid
model in this round.

## Outer attempt memory A/B (xai:grok-4.6, 2026-10-25)

| run | fixed | wrong fixes | correct withholds | breaches | mean steps | tokens | cost |
|---|---|---|---|---|---|---|---|
| attempt memory ON (default) | 21/22 | 1 (inventory_pipeline) | 1/1 | 0 | 7.8 | 459,608 | $0.949 |
| `--no-attempt-memory` | 21/22 | 1 (inventory_pipeline) | 1/1 | 0 | 7.5 | 429,165 | $0.887 |

No difference in outcomes. In both runs no `finish` was rejected on any fixture.
On inventory_pipeline the model's first fix already passed the visible tests,
so the agent never saw a failed attempt and the memory had nothing to pass on.
Memory only helps after a visible failure. It cannot catch a fix that is wrong
only against the hidden tests. Small token/cost delta is within run-to-run noise
plus the summaries on fixtures with intermediate red test runs.
