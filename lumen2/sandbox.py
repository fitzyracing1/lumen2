"""Nested containment for executed code.

    Bot > BoundBucket > outer sandbox > inner sandbox > executed code

* **Outer sandbox** (``OuterSandbox``): the disposable scratch copy of the
  target (``Workspace``). The agent never sees the real target path. Every
  executed process carries an outer write guard confined to the scratch
  copy, and every run is followed by an integrity check of the real target
  (any change is reverted and recorded). Only a verified diff leaves, via
  ``export`` — after the critic's verification and a per-file bucket check.
  When Docker/Podman is available and requested, test runs happen in a
  container (``--network none``, read-only root, CPU/memory limits).
* **Inner sandbox** (``InnerSandbox``): each code or test run gets its own
  subprocess in its own temp copy of the scratch workspace, with a timeout,
  POSIX rlimits (CPU, address space, file size, process count), process-tree
  kill on timeout (process group on POSIX, ``taskkill /T`` on Windows), a
  network guard (Python audit hook on socket operations + dead proxy env),
  a write guard confined to the temp copy, and no process spawning.

Each layer checks independently. Blocks are reported as
``LUMEN2-BLOCK[<layer>]`` and collected into breach records with the layer
that stopped the action.

Honest limits: the guards are Python audit hooks and rlimits in an ordinary
process. They stop accidents and naive attacks by model-written code. They
are not a security boundary against a determined attacker (native code via
ctypes, a C extension, or a pre-existing binary can bypass audit hooks).
A container is a materially stronger outer layer.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

IGNORED = ("__pycache__", ".pytest_cache", ".git", ".venv", "node_modules",
           ".mypy_cache", ".lumen2-tmp")
BLOCK_RE = re.compile(r"LUMEN2-BLOCK\[(\w+)\]:? ?([^\n'\"]*)")

# sitecustomize installed into every sandboxed interpreter. Guards come from
# LUMEN2_GUARDS (JSON list, innermost first). Python-level only.
GUARD_SOURCE = r'''
import json as _j, os as _o, sys as _s
_G = _j.loads(_o.environ.get("LUMEN2_GUARDS", "[]"))
_LOG = _o.environ.get("LUMEN2_BLOCK_LOG", "")
_ALLOW = ("/dev/null", "/dev/tty", "nul", "NUL")
def _norm(p):
    try:
        return _o.path.normcase(_o.path.realpath(_o.fsdecode(p)))
    except Exception:
        return ""
def _inside(p, root):
    return p == root or p.startswith(root.rstrip(_o.sep) + _o.sep)
def _block(layer, what):
    msg = "LUMEN2-BLOCK[%s]: %s" % (layer, what)
    if _LOG:
        try:
            _f = _orig_open(_LOG, "a", encoding="utf-8"); _f.write(msg + "\n"); _f.close()
        except Exception:
            pass
    raise PermissionError(msg)
import builtins as _b, io as _io
_orig_open = _io.open
for _g in _G:
    _g["root"] = _norm(_g["root"])
def _check_write(path, what):
    p = _norm(path)
    if not p or p in _ALLOW or (_LOG and p == _norm(_LOG)):
        return
    for _g in _G:
        if _g.get("writes", True) and not _inside(p, _g["root"]):
            _block(_g["layer"], "%s outside %s: %s" % (what, _g["layer"], p))
_W = ("w", "a", "x", "+")
def _hook(event, args):
    if event == "open":
        path, mode, flags = (list(args) + [None, None, 0])[:3]
        if isinstance(path, int) or path is None:
            return
        writing = (isinstance(mode, str) and any(c in mode for c in _W)) or \
            (isinstance(flags, int) and flags & (_o.O_WRONLY | _o.O_RDWR | _o.O_CREAT | getattr(_o, "O_APPEND", 0)))
        if writing:
            _check_write(path, "write")
    elif event in ("os.remove", "os.rmdir", "os.mkdir", "os.truncate",
                   "os.chmod", "os.symlink", "os.link", "shutil.rmtree",
                   "shutil.copyfile", "os.utime"):
        if args and not isinstance(args[0], int):
            target = args[1] if event in ("os.symlink", "os.link", "shutil.copyfile") and len(args) > 1 else args[0]
            _check_write(target, event)
    elif event in ("os.rename", "os.replace"):
        for a in args[:2]:
            if not isinstance(a, int):
                _check_write(a, event)
    elif event in ("socket.connect", "socket.connect_ex", "socket.bind",
                   "socket.getaddrinfo", "socket.gethostbyname", "socket.sendto"):
        for _g in _G:
            if not _g.get("network", False):
                _block(_g["layer"], "network access (%s)" % event)
    elif event in ("subprocess.Popen", "os.system", "os.exec", "os.posix_spawn",
                   "os.spawn", "os.fork", "os.forkpty", "pty.spawn"):
        for _g in _G:
            if not _g.get("spawn", False):
                _block(_g["layer"], "process spawn (%s)" % event)
    elif event == "ctypes.dlopen":
        pass  # documented bypass vector; not blocked (would break CPython)
if _G:
    _s.addaudithook(_hook)
'''


@dataclass
class RunResult:
    returncode: int = -1
    stdout: str = ""
    stderr: str = ""
    timed_out: bool = False
    blocks: list = field(default_factory=list)   # [{"layer", "detail"}]
    layer_note: str = ""


def _text(v) -> str:
    if v is None:
        return ""
    return v.decode("utf-8", "replace") if isinstance(v, bytes) else str(v)


def _kill_tree(proc: subprocess.Popen) -> None:
    try:
        if os.name == "nt":
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                           capture_output=True, timeout=10)
        else:
            os.killpg(proc.pid, signal.SIGKILL)
    except (OSError, subprocess.SubprocessError):
        pass
    try:
        proc.kill()
    except OSError:
        pass


def _rlimit_preexec(cpu: int, mem_mb: int, fsize_mb: int, nproc: int):
    def apply():
        import resource
        os.setsid()
        for name, val in (("RLIMIT_CPU", cpu),
                          ("RLIMIT_AS", mem_mb * 1024 * 1024),
                          ("RLIMIT_FSIZE", fsize_mb * 1024 * 1024),
                          ("RLIMIT_NPROC", nproc)):
            res = getattr(resource, name, None)
            if res is None or not val:
                continue
            try:
                soft, hard = resource.getrlimit(res)
                cap = val if hard == resource.RLIM_INFINITY else min(val, hard)
                resource.setrlimit(res, (cap, hard))
            except (ValueError, OSError):
                pass
    return apply


def container_runtime(choice: str) -> str | None:
    """Return 'docker'/'podman' if requested and usable, else None."""
    if choice in (None, "", "off"):
        return None
    names = ("docker", "podman") if choice == "auto" else (choice,)
    for name in names:
        if shutil.which(name) is None:
            continue
        try:
            ok = subprocess.run([name, "info"], capture_output=True,
                                timeout=15).returncode == 0
        except (OSError, subprocess.SubprocessError):
            ok = False
        if ok:
            return name
    return None


class InnerSandbox:
    """One subprocess per run, in its own temp copy, with limits and guards."""

    def __init__(self, bucket=None, enabled: bool = True):
        self.bucket = bucket
        self.enabled = enabled

    @property
    def limits(self):
        from lumen2.bucket import Limits
        return self.bucket.limits if self.bucket is not None else Limits()

    def describe(self) -> str:
        if not self.enabled:
            return "inner: DISABLED"
        bits = ["temp copy per run", "timeout + tree kill",
                "network guard" if not self.limits.network else "network ALLOWED",
                "write guard", "no spawn"]
        if os.name == "posix":
            bits.append("rlimits(cpu,as,fsize,nproc)")
        else:
            bits.append("rlimits n/a on Windows (timeout + taskkill /T only)")
        return "inner: " + ", ".join(bits)


class OuterSandbox:
    """Disposable scratch copy + outer guards + verified-diff export."""

    def __init__(self, root: Path, target: Path | None, bucket=None,
                 inner: InnerSandbox | None = None,
                 sandbox_dir: Path | None = None,
                 attempt_memory: bool = True):
        from lumen2.attempts import AttemptMemory
        # Outer-only, in-process log of failed attempts; never on disk.
        self.attempts = AttemptMemory(enabled=attempt_memory)
        self.root = Path(root).resolve()
        # The whole disposable directory (scratch copy + inner run copies).
        self.sandbox_dir = Path(sandbox_dir or self.root.parent).resolve()
        self.target = Path(target).resolve() if target else None
        self.bucket = bucket
        self.inner = inner if inner is not None else InnerSandbox(bucket)
        self.runtime = container_runtime(self.limits.container)
        self.blocks: list[dict] = []
        self._target_snapshot = self._snapshot_target()

    @property
    def limits(self):
        from lumen2.bucket import Limits
        return self.bucket.limits if self.bucket is not None else Limits()

    def describe(self) -> str:
        outer = ["outer: disposable scratch copy", "write guard",
                 "target integrity check", "verified-diff export"]
        if self.runtime:
            outer.append(f"{self.runtime} for tests (--network none, "
                         f"read-only root, cpu/mem limits)")
        elif self.limits.container != "off":
            outer.append(f"container '{self.limits.container}' requested "
                         f"but unavailable: falling back to processes")
        return ("[sandbox] layers: bucket > " + ", ".join(outer) + " > "
                + self.inner.describe() + " > executed code")

    # -- target integrity ---------------------------------------------------------

    def _snapshot_target(self) -> dict:
        snap = {}
        if self.target is None or not self.target.is_dir():
            return snap
        for p in self.target.rglob("*"):
            if p.is_file() and not any(part in IGNORED for part in p.parts):
                try:
                    snap[p.relative_to(self.target).as_posix()] = p.read_bytes()
                except OSError:
                    pass
        return snap

    def check_target_integrity(self) -> list[str]:
        """Revert any change to the real target made outside export()."""
        if self.target is None:
            return []
        now = self._snapshot_target()
        bad = sorted(set(now) ^ set(self._target_snapshot)
                     | {k for k in now if now[k] != self._target_snapshot.get(k)})
        for rel in bad:
            path = self.target / rel
            if rel in self._target_snapshot:
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(self._target_snapshot[rel])
            else:
                try:
                    path.unlink()
                except OSError:
                    pass
            self._block("outer", f"real target modified outside export: {rel} (reverted)")
        return bad

    def _block(self, layer: str, detail: str) -> None:
        rec = {"layer": layer, "detail": detail[:300]}
        self.blocks.append(rec)
        if self.bucket is not None:
            self.bucket.breach(layer, "sandbox", detail)

    # -- execution ---------------------------------------------------------------------

    def run(self, argv: list[str], timeout: float | None = None,
            kind: str = "python") -> RunResult:
        lim = self.limits
        timeout = min(timeout or lim.command_timeout, lim.command_timeout)
        inner = self.inner.enabled
        tmp = Path(tempfile.mkdtemp(prefix="inner-", dir=str(self.sandbox_dir)))
        try:
            if inner:
                cwd = tmp / "run"
                shutil.copytree(self.root, cwd,
                                ignore=shutil.ignore_patterns(*IGNORED))
            else:
                cwd = self.root
            site = tmp / "site"
            site.mkdir()
            (site / "sitecustomize.py").write_text(GUARD_SOURCE, encoding="utf-8")
            scratch_tmp = cwd / ".lumen2-tmp"
            scratch_tmp.mkdir(exist_ok=True)
            block_log = tmp / "blocks.log"
            guards = []
            if inner:
                guards.append({"layer": "inner", "root": str(cwd),
                               "network": lim.network, "spawn": False})
            guards.append({"layer": "outer", "root": str(self.sandbox_dir),
                           "network": lim.network, "spawn": True})
            env = dict(os.environ)
            for key in list(env):
                if key.upper().endswith("_API_KEY") or key.upper() in (
                        "GH_TOKEN", "GITHUB_TOKEN"):
                    env.pop(key)   # model-written code never sees secrets
            env.update({
                "PYTHONIOENCODING": "utf-8", "PYTHONUTF8": "1",
                "PYTHONDONTWRITEBYTECODE": "1",
                "PYTHONPATH": os.pathsep.join(
                    [str(site)] + ([env["PYTHONPATH"]] if env.get("PYTHONPATH") else [])),
                "LUMEN2_GUARDS": json.dumps(guards),
                "LUMEN2_BLOCK_LOG": str(block_log),
                "TMPDIR": str(scratch_tmp), "TEMP": str(scratch_tmp),
                "TMP": str(scratch_tmp),
            })
            if not lim.network:
                dead = "http://127.0.0.1:9"
                env.update({"http_proxy": dead, "https_proxy": dead,
                            "HTTP_PROXY": dead, "HTTPS_PROXY": dead,
                            "ALL_PROXY": dead, "all_proxy": dead,
                            "no_proxy": "", "NO_PROXY": ""})
            if self.runtime and kind == "tests":
                result = self._run_container(argv, cwd, timeout)
            else:
                result = self._run_process(argv, cwd, env, timeout, inner)
            if block_log.is_file():
                seen = set()
                for line in block_log.read_text(encoding="utf-8").splitlines():
                    m = BLOCK_RE.search(line)
                    if m and line not in seen:
                        seen.add(line)
                        self._block(m.group(1), m.group(2))
                        result.blocks.append({"layer": m.group(1),
                                              "detail": m.group(2)})
            for rel in self.check_target_integrity():
                result.blocks.append({"layer": "outer",
                                      "detail": f"target modified: {rel}"})
            if result.timed_out:
                self._block("inner" if inner else "outer",
                            f"command timed out after {timeout}s (tree killed)")
            return result
        finally:
            if inner:
                shutil.rmtree(tmp, ignore_errors=True)
            else:
                shutil.rmtree(tmp, ignore_errors=True)
                shutil.rmtree(self.root / ".lumen2-tmp", ignore_errors=True)

    def _run_process(self, argv, cwd, env, timeout, inner) -> RunResult:
        lim = self.limits
        kwargs: dict = {}
        if os.name == "posix":
            if inner:
                kwargs["preexec_fn"] = _rlimit_preexec(
                    int(min(lim.cpu_seconds, timeout + 1)), lim.mem_mb,
                    lim.file_size_mb, lim.max_procs)
            else:
                kwargs["start_new_session"] = True
        else:
            kwargs["creationflags"] = getattr(subprocess,
                                              "CREATE_NEW_PROCESS_GROUP", 0)
        try:
            proc = subprocess.Popen(argv, cwd=str(cwd), env=env,
                                    stdout=subprocess.PIPE,
                                    stderr=subprocess.PIPE, **kwargs)
        except OSError as exc:
            return RunResult(stderr=f"could not start: {exc}")
        try:
            out, err = proc.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            _kill_tree(proc)
            out, err = proc.communicate()
            return RunResult(proc.returncode or -9, _text(out), _text(err),
                             timed_out=True)
        return RunResult(proc.returncode, _text(out), _text(err))

    def _run_container(self, argv, cwd, timeout) -> RunResult:
        lim = self.limits
        inner_argv = ["python"] + argv[1:]
        cmd = [self.runtime, "run", "--rm", "--network", "none", "--read-only",
               "--tmpfs", "/tmp", "--cpus", "1", "--memory", f"{lim.mem_mb}m",
               "--pids-limit", str(lim.max_procs), "-e", "PYTHONDONTWRITEBYTECODE=1",
               "-v", f"{cwd}:/work", "-w", "/work", lim.container_image] + inner_argv
        try:
            proc = subprocess.run(cmd, capture_output=True, timeout=timeout + 30)
        except subprocess.TimeoutExpired as exc:
            return RunResult(-9, _text(exc.stdout), _text(exc.stderr), timed_out=True)
        except OSError as exc:
            return RunResult(stderr=f"container failed: {exc}")
        return RunResult(proc.returncode, _text(proc.stdout), _text(proc.stderr),
                         layer_note=self.runtime)

    # -- export --------------------------------------------------------------------------

    def export(self, changed: dict[str, str]) -> list[str]:
        """Copy a verified diff to the real target, file by file, each one
        re-checked against the bucket. Deletions are never propagated."""
        from lumen2.workspace import safe_join
        if self.target is None:
            return []
        applied = []
        for rel, content in changed.items():
            src = safe_join(self.root, rel)
            dest = safe_join(self.target, rel)
            if src is None or dest is None or not src.is_file():
                if dest is None:
                    self._block("outer", f"export path escapes target: {rel}")
                continue
            if self.bucket is not None and self.bucket.check_export(rel, content):
                continue
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_text(content, encoding="utf-8", newline="\n")
            applied.append(rel)
        self._target_snapshot = self._snapshot_target()
        return applied
