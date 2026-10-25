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
| Bound bucket | `lumen2/bucket.py` (`BoundBucket`) | The first-class bucket. Declared hard limits — allowed paths (scratch workspace only), read-only paths (tests), allowed tools, max steps, max model calls/tokens, wall clock, per-command timeout, max cost for paid APIs, network off, max policy revisions, max edit size, and a confidence floor below which only `withhold` is allowed. Every model call (including the model critic), tool call, edit, executed command, and export passes through it. A hit limit blocks the action and records a `bucket-breach` (with the layer that stopped it) in the trace; hard-budget breaches end the run in a withhold. Limits can be tightened mid-run, never loosened. Defaults, or `--bucket file.toml|json`. |
| Bound bucket | `lumen2/sandbox.py` (`OuterSandbox`, `InnerSandbox`) | Nested containment for executed code: the outer layer is the disposable scratch copy with its own write/network guard, a real-target integrity check (reverts any out-of-band change), and the only export path (verified diff, each file re-checked against the bucket). The inner layer runs every code/test run in its own subprocess and temp copy with timeouts, rlimits, tree-kill, and network/write/spawn guards. Optional Docker/Podman outer layer. |
| Bound bucket | `lumen2/workspace.py` | Path validation (`safe_join`), test files read-only, and the tool surface the agent sees — all execution delegated to the sandbox layers. |
| Bound bucket | `lumen2/kernel.py` | Persistent goal stack; soft budgets on steps, tokens, and wall time inside the bucket's hard ones; policy revision is allowed at most twice per run, inside clamps, asks the bucket first, and can never loosen verification or budgets. |
| Attack bot | `lumen2/critic.py` | Deterministic attacks on every proposed edit and on the final diff: sandbox escapes, test-file edits, deleted asserts, syntax errors, and hardcoded test expectations are detected and rejected. |
| Attack bot | `lumen2/kernel.py` (`gate`, `verify_fix`) | Every action passes a gate before it runs. `finish` is not believed: the kernel re-runs the test suite fresh and re-scans every changed file; only green and clean commits. Low confidence becomes an explicit withhold ("I don't know"), never a guess. |
| Attack bot | `Backend.critique` (`lumen2/backends/`) | An adversarial model critique on top of the static layer. It can veto a free-form answer (where no test oracle exists); for edits it is advisory, because the tests are the stronger arbiter. |
| LLM | `lumen2/backends/` | The idea supply: local models via Ollama, OpenAI, any OpenAI-compatible endpoint, or the deterministic offline stub. The kernel treats them all identically — the discipline does not depend on the model being strong. |

## Containment layers

    Bot (agent + kernel)
      > BoundBucket            lumen2/bucket.py   declared limits, every call
        > outer sandbox        lumen2/sandbox.py  disposable copy, export gate, integrity check, [container]
          > inner sandbox      lumen2/sandbox.py  per-run subprocess + temp copy, rlimits, guards
            > executed code    (tests, run_python)

Each layer checks its own limits independently and logs which layer
stopped an action (`bucket`, `outer`, `inner`). The escape tests in
`tests/test_escape.py` disable the inner layer and show the outer layer
still blocks the write and the network call.

The outer layer also keeps a bounded memory of failed attempts
(`lumen2/attempts.py`; idea by Joshua Almeida). Only kernel/proposer/critic
prompts receive its summary; executed code never can, since it exists only in
the outer process's memory. Size-capped and charged to the bucket; disable
with `--no-attempt-memory`.

## Threat model (honest)

What the layers are for: a model that is wrong, sloppy, or "creative" —
editing tests, writing outside its workspace, phoning home, spinning
forever, spending money. Against that they work, and the tests prove each
block.

What they are not: a security boundary against a determined attacker. The
process guards are Python audit hooks plus rlimits in an ordinary user
process. Native code (ctypes, a C extension, a pre-existing binary run some
other way) can bypass audit hooks; rlimits do not hide the filesystem; on
Windows there are no rlimits or Job Objects yet, only timeouts and
`taskkill /T`. The outer integrity check reverts tampering with the real
target after the fact, but it cannot undo writes elsewhere on disk. A
container (`--container docker|podman`, `--network none`, read-only root,
CPU/memory/pids limits) is materially stronger; a VM stronger still. If you
run untrusted code from an untrusted model, use the container layer or a
throwaway VM.

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
- **Bucket breaches** — actions the bound bucket or a sandbox layer had to
  block (a well-behaved run has zero).
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
