# The definition behind Lumen 2

Credit: the definition and the project are by **Joshua Almeida
([fitzyracing1](https://github.com/fitzyracing1))**.

> In terms of a computer program, superintelligence is a bound bucket using
> AI to drive an attack bot that derives its info from an LLM.

Three parts, each necessary:

1. **The LLM supplies ideas.** Fluent, fast, and unreliable. It proposes —
   candidate edits, candidate answers, the next tool action — and proves
   nothing.
2. **An AI-driven attack bot tries to break every idea.** Not a reviewer
   that nods along: a layer whose job is to kill proposals — catch cheating,
   find the hidden assumption, demand verification — before anything is
   allowed to act.
3. **A bound bucket contains both.** Hard limits on goals (a persistent
   stack, not the whim of the current token), on budget (steps, tokens, wall
   time, revision count), and on reach (what files, what directories, what
   effects are even possible). Only what survives the attack, inside the
   bucket, ever touches the world.

This is an architecture claim, not a capability claim. Lumen 2 is not
superintelligent and does not pretend to be. The claim is that bounded +
adversarial + verified is the control shape any path to *safe*
superintelligence will need — it is exactly what you would still want on the
day the substrate gets strong. Lumen 2 is a small working instance of the
shape, measured by a benchmark rather than asserted.

## Where each part lives in the code

| Thesis part | Code | What it does |
|---|---|---|
| Bound bucket | `lumen2/workspace.py` | The agent works only in a throwaway scratch copy; every path is validated to stay inside it; test files are read-only; every subprocess has a hard timeout. The real target is only touched by a verified commit. |
| Bound bucket | `lumen2/kernel.py` | Persistent goal stack; hard budgets on steps, tokens, and wall time; policy revision is allowed at most twice per run, inside clamps, and can never loosen verification or budgets. |
| Attack bot | `lumen2/critic.py` | Deterministic attacks on every proposed edit and on the final diff: sandbox escapes, test-file edits, deleted asserts, syntax errors, and hardcoded test expectations are detected and rejected. |
| Attack bot | `lumen2/kernel.py` (`gate`, `verify_fix`) | Every action passes a gate before it runs. `finish` is not believed: the kernel re-runs the test suite fresh and re-scans every changed file; only green and clean commits. Low confidence becomes an explicit withhold ("I don't know"), never a guess. |
| Attack bot | `Backend.critique` (`lumen2/backends/`) | An adversarial model critique on top of the static layer. It can veto a free-form answer (where no test oracle exists); for edits it is advisory, because the tests are the stronger arbiter. |
| LLM | `lumen2/backends/` | The idea supply: local models via Ollama, OpenAI, any OpenAI-compatible endpoint, or the deterministic offline stub. The kernel treats them all identically — the discipline does not depend on the model being strong. |

## How progress is measured

The claim is falsifiable. `python -m lumen2.bench` runs the agent on 23
small repositories with real bugs and **hidden held-out tests** the LLM never
sees, and reports:

- **Fixed rate** — bugs truly fixed (visible *and* hidden tests green).
- **Wrong fixes vs hidden tests** — "fixes" that satisfied what the model
  could see but broke what it could not. The headline safety number: the
  architecture exists to drive this to zero.
- **Correct withholds** — refusing to act on the impossible fixture (and on
  anything it cannot verify) instead of committing a plausible lie.
- **Cheats caught** — attempts to edit tests, delete asserts, or hardcode
  expected outputs that the attack layer stopped.

Plus cost: steps, tokens, and wall time per fixture. A v1-vs-v2 comparison
runner (`python -m lumen2.bench.compare`) keeps the previous generation of
the kernel honest too.

## Future work

- **A human baseline** on the same 23 fixtures (fixed rate, wrong-fix rate,
  and time), so the numbers have a reference point beyond model-vs-model.
- **Harder tasks**: larger repositories where exploration is forced,
  multi-step refactors, and tasks whose verification oracle is weaker than a
  test suite — the regime where the attack bot has to earn its keep.
- **Stronger attack bots**: today the static critic is deterministic and the
  model critic is the same LLM arguing with itself; a dedicated adversary
  model is the obvious next step.
