# A measurable definition of progress toward superintelligence

## The standard definition

Nick Bostrom defines superintelligence as "any intellect that greatly exceeds the cognitive performance of humans in virtually all domains of interest" (Superintelligence, 2014). In short: an intellect that greatly exceeds the best humans in nearly every domain.

## Where Lumen stands

Lumen is not superintelligence. It is a small control loop wrapped around ordinary language models. It does not exceed the best humans at anything we have measured. Claiming otherwise would be marketing, not science.

## A testable yardstick

"Superintelligence" is only useful as a goal if progress toward it can be measured. We propose this: on the same tasks given to the best humans, a system moves toward superintelligence when it

1. **Solves more.** It completes a larger share of the tasks correctly.
2. **Makes fewer confident mistakes.** A confident mistake is a wrong fix: a change that passes the visible tests but fails hidden, held-out tests. Fewer of these means the system is not just gaming the checks it can see.
3. **Knows when it doesn't know.** On tasks that cannot be solved as stated, it correctly withholds instead of committing a fake answer.

All three matter. A system that solves more but cheats more, or never admits a task is impossible, is not getting smarter in a way anyone should trust.

## How the Lumen benchmark measures each

The Lumen benchmark is a set of small buggy code fixtures. Each has visible tests the system can run and hidden tests it never sees.

- **Solves more:** the fixed rate, meaning the share of fixable fixtures where the patch passes both visible and hidden tests.
- **Fewer confident mistakes:** the wrong-fix count, meaning patches that pass the visible tests but fail the hidden ones. Attempts to edit the tests instead of the code are also caught and counted.
- **Knows when it doesn't know:** an impossible fixture whose tests contradict each other. The correct behavior is to withhold. Committing a patch there is scored as wrong.

## Next steps

Today the benchmark compares models against each other, not against people, and the tasks are small. To turn this into a public scoreboard, the next steps are:

1. **A human baseline.** Have skilled engineers take the same fixtures under the same rules, so every score can be read against the best humans.
2. **Harder tasks.** Add larger, multi-file, real-world bugs and more impossible cases, so the ceiling is far above current systems.

Only when a system beats the human baseline on all three measures, across many domains, would the word superintelligence start to apply.
