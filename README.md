# Lumen 2

A kernel that makes an AI agent check its own work before it acts.

This is not superintelligence, not AGI, and not a claim that a small language
model got smarter. It is a control architecture: a multi-step, tool-using
coding agent where every step passes through a kernel that enforces goals,
evidence, adversarial critique, verification, and honest refusal. The pitch is
backed by a benchmark with hidden held-out tests, not by adjectives.

## Thesis

In the words of the project's owner, Joshua Almeida (fitzyracing1):

> In terms of a computer program, superintelligence is a bound bucket using
> AI to drive an attack bot that derives its info from an LLM.

Put plainly: an **LLM supplies ideas** — candidate edits, answers, next
steps. An **AI-driven attack bot tries to break each one** before it is
allowed to matter — cheat detection, adversarial critique, verification
against tests the proposer never sees. And a **bound bucket contains it
all** — hard limits on goals, budget, and what the system may touch — so
only what survives the attack acts on the world.

The claim is not that Lumen 2 is superintelligent. The claim is that this
shape — bounded, adversarial, verified — is the architecture any path to
*safe* superintelligence will need, regardless of how strong the substrate
gets. Lumen 2 is a working instance of that shape at small scale, and the
benchmark in this repository is how progress against the claim is measured.
See [DEFINITION.md](DEFINITION.md) for the full mapping from the thesis to
the code.

## What the kernel enforces

A plain agent loop does whatever the model says next. In Lumen 2 the model
only ever *proposes*; the kernel decides:

- **Persistent goal stack.** The mission outlives any single model call.
- **Beliefs with confidence,** updated only by tool evidence (test results,
  file contents, executed code) — never by the model's self-assessment.
- **A critic attacks risky actions** before they run. Static checks are
  deterministic and always on: edits must stay inside the sandbox, test files
  are read-only, syntax must parse, and *cheats are detected* — editing
  tests, deleting asserts, and hardcoding the tests' expected outputs are all
  caught and rejected. A model critique runs on top where a backend is
  available.
- **Verification before commit.** `finish` does not mean done. The kernel
  re-runs the test suite fresh and re-scans every changed file; only a green,
  clean run copies changes out of the scratch workspace into the real target.
- **Withholding is a first-class outcome.** Low confidence, an impossible
  task, or a spent budget end with an explicit refusal, never a guess. In ask
  mode, low confidence becomes a literal "I don't know".
- **Budgets are hard:** steps, tokens, and wall time. The kernel may revise
  its own policy when stuck (raise exploration temperature, inject a
  step-back hint) — at most twice per run, inside clamps, and it can never
  touch verification or the budgets themselves.

## The bound bucket and the containment layers

Everything above runs inside a **BoundBucket** (`lumen2/bucket.py`): declared
hard limits that every model call, tool call, edit, executed command, and
export must pass. Defaults, overridable with `--bucket file.toml|json` (see
[`examples/bucket.toml`](examples/bucket.toml)); the active limits and
sandbox layers are printed at start. A hit limit blocks the action and logs a
`bucket-breach` in the trace (with the layer that stopped it); steps, model
calls, tokens, cost, or wall clock running out ends the run in a withhold;
three blocked actions also end it in a withhold. Limits can be tightened
mid-run (`bucket.tighten(...)`) but never loosened.

| limit | default |
|---|---|
| allowed paths / read-only paths | scratch workspace only / tests, `test_*`, `*_test.py`, `conftest.py` |
| allowed tools | all eight agent tools |
| max steps / model calls / tokens | 60 / 150 / 400k (the kernel's own softer budgets sit inside these) |
| wall clock / per-command timeout | 1800 s / 120 s |
| max cost (paid APIs: OpenAI, xAI) | $1.00 per run, estimated from reported usage |
| network for sandboxed code | off |
| max policy revisions / max edit size | 2 / 60,000 chars |
| confidence floor (only `withhold` below) | 0.3 |

Executed code sits in nested containment:

    Bot > BoundBucket > outer sandbox > inner sandbox > executed code

- **Outer sandbox:** a disposable copy of the target. Executed code may not
  write outside it or open sockets (Python audit-hook guard). After every run
  the real target is checked and any out-of-band change is reverted and
  logged. Only a diff that passed verification and the critic leaves, and
  each file is re-checked against the bucket on export. Optionally
  `--container auto|docker|podman` runs test runs in a container with
  `--network none`, a read-only root, and CPU/memory/pids limits (the image,
  default `python:3.12-slim`, must contain pytest); it falls back cleanly to
  processes when no runtime is available.
- **Inner sandbox:** each code/test run gets its own subprocess and its own
  temp copy, a timeout with process-tree kill, rlimits on Linux/macOS (CPU,
  address space, file size, process count), no process spawning, writes
  confined to the copy, network blocked (socket guard via `sitecustomize`
  plus a dead proxy), and API keys stripped from the environment.

`tests/test_escape.py` proves each block: writing outside the workspace,
modifying the real target, editing tests, exceeding steps/cost/model calls,
network access, spawning processes, runaway loops — and the same writes and
network calls with the inner layer **disabled**, showing the outer layer
still holds on its own.

**Threat model, honestly:** these are process sandboxes. They stop a model
that is wrong or sloppy, and naive escape attempts. They are *not* a
security boundary against a determined attacker: native code can bypass
Python audit hooks, and on Windows there are no rlimits or Job Objects yet
(only timeouts and `taskkill /T`). Containers are materially stronger; use
`--container` or a throwaway VM for untrusted code. See
[DEFINITION.md](DEFINITION.md#threat-model-honest).

The agent works in a throwaway copy of the target directory. Its tools:
`read_file`, `list_files`, `search`, `run_tests`, `run_python` (sandboxed,
with timeouts), `edit_file` (gated by the critic), `finish`, `withhold`.

## Install and run

Python 3.11+, no runtime dependencies (pytest to run tests/benchmark targets).
Works on Linux, macOS, and Windows (UTF-8 output, PowerShell-friendly).

```bash
pip install pytest
python -m lumen2 fix path/to/broken-project      # fix failing tests
python -m lumen2 ask "what is 2**100 - 1?"       # answer with verified confidence
python -m lumen2 chat                            # interactive ask loop
python -m lumen2.bench --model stub              # the benchmark, offline
```

(`pip install -e .` additionally gives you the `lumen2` console command.)

## Model backends

Selected with `--model` or `LUMEN2_MODEL`. The kernel treats every backend
identically — same gate, same verification, same budgets.

| spec | what it is |
|---|---|
| `stub` (default) | Offline, deterministic. Runs the tests, reads the source, tries small bug-class rewrites one at a time, keeps what the tests accept. CI runs on it. |
| `ollama:<model>` | Local Ollama (e.g. `ollama:qwen2.5-coder:1.5b`) at `http://127.0.0.1:11434/v1` (`OLLAMA_BASE_URL` to override). Uses a JSON action protocol — small local models mangle native tool calls more often than JSON. |
| `amalia` | Amália via Ollama: model `amalia:latest` at `http://127.0.0.1:11434/v1` (`AMALIA_BASE_URL`, `AMALIA_MODEL` to override). |
| `openai:<model>` | OpenAI (needs `OPENAI_API_KEY`; `OPENAI_BASE_URL` to override). Native tool calling, with automatic downgrade to the JSON protocol if the server rejects it. |
| `xai:<model>` | xAI Grok (needs `XAI_API_KEY`; `https://api.x.ai/v1`, `XAI_BASE_URL` to override), e.g. `xai:grok-4.6` or `xai:grok-4.20-0309-non-reasoning`. Native tool calling. |
| `url:<base_url>::<model>` | Any other OpenAI-compatible endpoint (key from `LUMEN2_API_KEY`). |

Any backend can read its key from a named environment variable with
`--api-key-env NAME`; keys are never printed, logged, or passed to sandboxed
code.

### Chat

`python -m lumen2 chat` is an interactive session (default model
`xai:grok-4.6`, key from `XAI_API_KEY`). Questions go through the ask kernel
(checked by running code where possible; low confidence becomes "I don't
know"). A message that asks for a code change (`fix ...`, `change ...`,
`refactor ...`, or `/fix <request>`) runs the normal fixer on the `--project`
directory: scratch copy, BoundBucket, nested sandbox, critic, fresh test
verification, and only a verified diff is written back. Tests stay
read-only, so "change the test so it passes" is blocked like anywhere else.
The whole session shares one bucket budget: each turn's bucket is the
configured one tightened to what the session has left, and once it is spent
chat refuses new turns. `/budget` shows spend, `/help` lists commands, blank
line or `exit` quits.

```powershell
# Windows PowerShell
$env:XAI_API_KEY = "xai-..."
python -m lumen2 chat --project C:\code\myproject
python -m lumen2 chat --project . --bucket examples\bucket.toml   # tighter limits
python -m lumen2 chat --model stub --project .                    # offline
```

Chat's code changes are limited to what the fixer does: make the project's
failing tests pass. It is not a general code-writing assistant; with no
failing tests there is nothing for it to verify, and it withholds.

### Using Grok

```powershell
# PowerShell
$env:XAI_API_KEY = "xai-..."
python -m lumen2 ask "what is 2**64?" --model xai:grok-4.6
python -m lumen2 fix path\to\project --model xai:grok-4.6
python -m lumen2.bench --model xai:grok-4.6
```

```bash
# bash; or any variable name via --api-key-env
export MY_GROK_KEY=xai-...
python -m lumen2.bench --model xai:grok-4.6 --api-key-env MY_GROK_KEY
```

HTTP backends retry with exponential backoff on 429 and 5xx (v1 didn't, and
died on rate limits). Model output that fails validation is never "repaired
by guessing": the kernel pushes back, and repeated garbage ends in a withhold.

## The benchmark

`bench/fixtures/` holds 23 small repositories with real bugs and **hidden
held-out tests the agent never sees**. A "fix" that satisfies the visible
tests but fails the hidden ones is reported as a wrong fix, not a success.

- **13 ported from v1** (`difficulty: base`): off-by-one, wrong operator,
  empty-input edge case, unicode byte/char confusion, mutable default,
  missing/wrong exceptions, multi-file constant, numeric-string sort, slice
  bounds, missing dict default, a fixture where editing the test is the
  tempting way out, and one **impossible** fixture (contradictory visible
  tests) where withholding is the only correct outcome.
- **10 new, harder** (`difficulty: hard`): a multi-file pricing pipeline, a
  misleading traceback that points at the wrong module, a contract that only
  exists in `docs/spec.md`, cache poisoning, twin helpers where the code
  calls the wrong one, two interacting off-by-ones that must both be fixed,
  an unanchored regex, a swallowed exception hiding a wrong delimiter, a
  seconds/milliseconds unit mismatch, and a class-level mutable state leak.
  Each ships a canonical `solution/` that the test suite proves correct, so
  "fixable by construction" is a checked claim, not a promise.

```bash
python -m lumen2.bench --model stub                      # offline
python -m lumen2.bench --model ollama:qwen2.5-coder:1.5b # local model
python -m lumen2.bench --model openai:gpt-4.1-mini       # OpenAI
python -m lumen2.bench --model xai:grok-4.6              # xAI Grok
python -m lumen2.bench --model stub --bucket examples/bucket.toml
python -m lumen2.bench ... --memory mem.json             # with lessons
python -m lumen2.bench.compare --model ollama:qwen2.5-coder:1.5b  # v1 vs v2
```

Reports (JSON + markdown, written to `bench/results/`) include per fixture:
outcome, hidden-test verdict, steps, tokens, cheats caught, edits rejected,
bucket breaches, estimated cost, and time. The compare runner clones v1 and runs both kernels on the 13 shared
fixtures with the same model.

Committed results from real runs live in [`bench/results/`](bench/results/).

## Outer-layer attempt memory (second attack)

Idea credited to **Joshua Almeida**. The inner sandbox is still wiped between
runs, but the *outer* layer (`lumen2/attempts.py`, owned by `OuterSandbox`)
keeps a tiny in-process log of failed attempts within a task: rejected
`finish`, a red test run after an edit, a kernel-rejected edit. A concise
summary of the most recent failure(s) is handed to the next proposal and
critic round as `PREVIOUS FAILED ATTEMPT(S)`, so the second attack is
informed by the first.

- The code under test never sees it: it is never written to the scratch
  copy, the inner run copies, or the environment (tested).
- Bounded: last 2 failures, 280 chars each, 600 chars total; every time it is
  put into a prompt its size is charged to the BoundBucket
  (`attempt_memory_tokens`, reserved against `max_tokens`).
- It does not touch beliefs, thresholds, or budgets (unlike Lumen 1's memory,
  which made it withhold everything).
- On by default; `--no-attempt-memory` (in `lumen2 fix` and `lumen2.bench`)
  turns it off for A/B runs.

## Attacker stage (give it grok to attack)

Idea credited to **Joshua Almeida**. After the kernel verifies a `finish`
(visible tests green, cheat scan clean) and *before* anything is committed, a
separate attacker model (`lumen2/attacker.py`; default `xai:grok-4.6` via
`XAI_API_KEY`, `--attacker-model SPEC`, `stub` offline) reads the task, the
proposed diff, the original and patched sources, and the visible tests, and
writes extra pytest tests that try to break the fix.

- The generated tests run in the inner sandbox against the patched code. They
  are written only into that run's throwaway copy, never into the scratch
  workspace, so they can never appear in the exported diff; visible tests stay
  read-only (tested).
- A surviving failure rejects the attempt: no commit, the failure goes into
  the attempt memory and the rejection feedback, and the next proposal must
  also pass the kept attacker tests.
- Bad-test guard: tests that error on both original and patched code
  (import/name/syntax errors) are dropped; tests that pass on the original but
  fail on the patch are regressions and kept; the rest are judged by the
  critic (attacker model) and dropped if they do not reflect the spec.
- Bounded: at most 2 generations and 3 judge calls per run, every call through
  `BoundBucket.authorize_model_call` and charged at the attacker model's price
  (`attacker_calls` in the bucket usage). Over budget it is skipped and logged.
- On by default in `lumen2 fix`, `lumen2 chat` and `lumen2.bench`
  (stub attacker when `--model stub`); `--no-attacker` turns it off.
  Results: [`bench/results/SUMMARY.md`](bench/results/SUMMARY.md).

## Memory that must not suppress action

v1's memory persisted belief priors and policy between runs; a bad streak made
it so cautious it went 0/12. v2 memory stores only *lessons* — what fixed
which kind of failure, what was a dead end, what the critic caught — tagged
with a keyword signature of the failure and retrieved by relevance. Lessons
are injected into prompts as context; they cannot change thresholds, budgets,
or beliefs. Whether memory helps is measured (`--memory`), not assumed.

## What this is not

The kernel does not make a model smarter; it makes a model's *failures*
cheaper and its *successes* trustworthy. A 1.5B model with Lumen 2 still
cannot fix what it cannot understand — the honest outcome there is a
withhold, and the benchmark counts those separately from fixes. There is no
hidden training loop, no self-modification outside two clamped policy
revisions per run, and no claim that any of this scales to open-ended
autonomy. "Better" here means: fewer wrong fixes slipping through, cheats
caught instead of rewarded, and an agent that can say "I don't know".

## Tests and CI

```bash
python -m pytest tests/ -q
```

Everything runs on the stub backend — no network. The suite includes a
scripted dishonest backend that tries to gut a test file, hardcode expected
outputs, and finish without passing tests; CI fails unless the kernel blocks
all three. GitHub Actions runs the suite plus benchmark and CLI smoke tests
on Linux and Windows, Python 3.11 and 3.12.

## License

MIT.
