"""Drive the cmake configure + ninja build of switch-mod.

Two steps:

  1. `cmake_configure()` — runs the cmake configure command from
     CLAUDE.md's "Daily dev loop". Idempotent: re-running over an
     existing build/ tree reconfigures cleanly.

  2. `cmake_build()` — runs `cmake --build` (Ninja under the hood).
     Verifies subsdk9 + subsdk9.npdm exist post-build.

Both steps stream stdout+stderr line-by-line through an `on_line`
callback so the wizard renders live progress. Failure surfaces a
BuildResult with `ok=False` plus the full captured log.

The build env is composed deterministically from the wizard's resolved
prereq paths:

  - DEVKITPRO            ← prereqs.resolved_devkitpro_root()
  - PATH (prepended)     ← devkitPro msys2 usr/bin (for `make`, `pacman`-installed tools)
  - CMake binary         ← prereqs.resolved_cmake() (rejects msys2's cmake)
  - Ninja generator      ← bare name "Ninja"; cmake finds the binary
                            from the same PATH we set up

Without this, `cmake --build` would shell out to whatever cmake/ninja
happens to be first on the inherited PATH and frequently miss-resolve
to the msys2 build (see CLAUDE.md "critical gotchas").
"""

from __future__ import annotations

import os
import queue
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from .prereqs import (
    repo_root,
    resolved_cmake,
    resolved_devkitpro_root,
    resolved_ninja_bin,
)

# Suppress per-child console windows under the windowed Launcher
# (Kivy-based parent, no console → Windows spawns a fresh one per child
# that steals focus). No-op on non-Windows.
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)

# Returncode we use for timeout-killed children. 124 follows the
# convention `timeout(1)` uses on POSIX; lifted into a constant so
# tests / callers can match on it without inlining.
TIMEOUT_RETURNCODE = 124

# Wall-clock + stall caps. Wall is generous (a clean configure on a slow
# machine takes ~60s; cold build under devkitA64 can hit 5+ min). Stall
# is the more useful signal — no output for 5 min means cmake/ninja is
# wedged, not slow.
CONFIGURE_WALL_TIMEOUT_S = 300.0      # 5 min
CONFIGURE_STALL_TIMEOUT_S = 120.0     # 2 min
BUILD_WALL_TIMEOUT_S = 900.0          # 15 min
BUILD_STALL_TIMEOUT_S = 300.0         # 5 min

ProgressFn = Callable[[str], None]


@dataclass
class BuildResult:
    """Result of one subprocess step. `log` holds the full streamed
    text — useful when the wizard wants to write a per-failure file
    diagnostic without burdening the user's caller with state."""
    ok: bool
    returncode: int
    log: str = ""


@dataclass
class CMakeOutcome:
    """Aggregated result of cmake configure + build. `outputs` maps
    artifact key → on-disk path when ok=True (empty otherwise)."""
    ok: bool
    step_results: dict[str, BuildResult] = field(default_factory=dict)
    outputs: dict[str, Path] = field(default_factory=dict)


def switch_mod_root(repo: Path | None = None) -> Path:
    repo = repo if repo is not None else repo_root()
    return repo / "switch-mod"


def build_dir(repo: Path | None = None) -> Path:
    return switch_mod_root(repo) / "build"


def toolchain_file(repo: Path | None = None) -> Path:
    return switch_mod_root(repo) / "cmake" / "toolchain.cmake"


def expected_artifacts(repo: Path | None = None) -> dict[str, Path]:
    """The two files `cmake --build` is expected to produce.

    Keys match what `deploy._ryujinx_layout` consumes: `"subsdk9"` and
    `"main.npdm"`. Build emits `subsdk9.npdm`; deploy renames during
    copy. We expose under the deploy-side key so the wizard can hand
    the dict over directly.
    """
    bd = build_dir(repo)
    return {
        "subsdk9": bd / "subsdk9",
        "main.npdm": bd / "subsdk9.npdm",
    }


def _compose_build_env() -> dict[str, str]:
    """Build the env dict every subprocess inherits.

    Composed from:
      - the parent process env (inherit base)
      - DEVKITPRO from prereqs.resolved_devkitpro_root() (falls back to
        env if cache wasn't populated)
      - PATH prepended with $DEVKITPRO\\msys2\\usr\\bin (where pacman
        installs binaries like libnx-headers helpers; devkitA64's own
        bin/ is already discoverable to cmake via toolchain.cmake)
      - PATH prepended with the resolved Ninja bin dir if we have one
      - PYTHONIOENCODING=utf-8 so child python prints don't crash on
        non-ASCII (em-dashes etc.) under cp932 / cp1252 default consoles.
    """
    env = os.environ.copy()
    root = resolved_devkitpro_root() or env.get("DEVKITPRO")
    if root:
        env["DEVKITPRO"] = root
        # devkitPro toolchain.cmake reads $DEVKITPRO via os.getenv-style
        # lookup, so set even if we inherited it already.
        msys2_bin = Path(root) / "msys2" / "usr" / "bin"
        if msys2_bin.is_dir():
            env["PATH"] = str(msys2_bin) + os.pathsep + env.get("PATH", "")
    ninja_bin = resolved_ninja_bin()
    if ninja_bin and ninja_bin not in env.get("PATH", "").split(os.pathsep):
        env["PATH"] = ninja_bin + os.pathsep + env.get("PATH", "")
    env.setdefault("PYTHONIOENCODING", "utf-8")
    return env


def _stream_subprocess(
    cmd: list[str],
    *,
    cwd: Path | None = None,
    env: dict[str, str] | None = None,
    on_line: ProgressFn | None = None,
    wall_timeout_s: float | None = None,
    stall_timeout_s: float | None = None,
) -> BuildResult:
    """Run a subprocess, streaming stdout + stderr line-by-line through
    `on_line` and accumulating the full text into the returned `log` for
    failure diagnosis.

    stderr is merged into stdout so cmake's "this file failed to compile"
    interleaves correctly with progress chatter. Two timeouts bound the
    child's lifetime:

      - `wall_timeout_s` — total wall-clock cap. None = no cap.
      - `stall_timeout_s` — max interval between stdout lines. None = no
        cap. Usually the more useful for long builds: "no output for N
        seconds" is a sharper wedge signal than total wall-clock time.

    On timeout the child is SIGTERM'd; if it doesn't exit within 5s, it
    gets SIGKILL'd. The result reports `ok=False` and `returncode=124`.
    """
    log_lines: list[str] = []

    def _emit(line: str) -> None:
        log_lines.append(line)
        if on_line is not None:
            on_line(line)

    _emit(f"[stream] spawning: {cmd}")
    if cwd is not None:
        _emit(f"[stream] cwd: {cwd}")
    if wall_timeout_s is not None:
        _emit(f"[stream] wall timeout: {wall_timeout_s:.0f}s")
    if stall_timeout_s is not None:
        _emit(f"[stream] stall timeout: {stall_timeout_s:.0f}s")

    child_env = dict(env) if env is not None else os.environ.copy()
    child_env.setdefault("PYTHONIOENCODING", "utf-8")

    try:
        proc = subprocess.Popen(
            cmd,
            cwd=str(cwd) if cwd else None,
            env=child_env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
            creationflags=_NO_WINDOW,
        )
    except (FileNotFoundError, OSError) as e:
        msg = f"failed to spawn {cmd[0]}: {e}"
        _emit(msg)
        return BuildResult(ok=False, returncode=127, log=msg)

    _emit(f"[stream] spawned pid={proc.pid}")
    assert proc.stdout is not None

    line_queue: "queue.Queue[str | None]" = queue.Queue()

    def _reader() -> None:
        try:
            for raw in proc.stdout:  # type: ignore[union-attr]
                line_queue.put(raw)
        except Exception as e:  # pragma: no cover - reader thread defensive
            line_queue.put(f"[stream] reader thread crashed: {e}\n")
        finally:
            line_queue.put(None)

    reader = threading.Thread(target=_reader, daemon=True)
    reader.start()

    spawn_ts = time.monotonic()
    last_output_ts = spawn_ts
    timeout_reason: str | None = None

    while True:
        try:
            raw = line_queue.get(timeout=0.5)
        except queue.Empty:
            raw = ""
        if raw is None:
            break
        if raw:
            _emit(raw.rstrip("\r\n"))
            last_output_ts = time.monotonic()

        now = time.monotonic()
        if wall_timeout_s is not None and (now - spawn_ts) > wall_timeout_s:
            timeout_reason = (
                f"wall-clock timeout exceeded ({wall_timeout_s:.0f}s)"
            )
            break
        if stall_timeout_s is not None and (now - last_output_ts) > stall_timeout_s:
            timeout_reason = (
                f"stall timeout exceeded ({stall_timeout_s:.0f}s with no output)"
            )
            break

    if timeout_reason is not None:
        _emit(f"[stream] killing pid={proc.pid}: {timeout_reason}")
        try:
            proc.terminate()
            try:
                proc.wait(timeout=5.0)
            except subprocess.TimeoutExpired:
                _emit(f"[stream] pid={proc.pid} ignored SIGTERM; SIGKILL'ing")
                proc.kill()
                try:
                    proc.wait(timeout=5.0)
                except subprocess.TimeoutExpired:
                    _emit(
                        f"[stream] pid={proc.pid} still running 5s after "
                        f"SIGKILL — orphaning"
                    )
        except (OSError, ProcessLookupError) as e:
            _emit(f"[stream] terminate raised {type(e).__name__}: {e}")
        try:
            while True:
                raw = line_queue.get_nowait()
                if raw is None:
                    break
                _emit(raw.rstrip("\r\n"))
        except queue.Empty:
            pass
        return BuildResult(
            ok=False,
            returncode=TIMEOUT_RETURNCODE,
            log="\n".join(log_lines),
        )

    try:
        rc = proc.wait(timeout=5.0)
    except subprocess.TimeoutExpired:
        _emit(f"[stream] pid={proc.pid} closed stdout but didn't exit within 5s; killing")
        proc.kill()
        rc = TIMEOUT_RETURNCODE

    ok = (rc == 0)
    return BuildResult(ok=ok, returncode=rc, log="\n".join(log_lines))


def cmake_configure(
    *,
    repo: Path | None = None,
    on_line: ProgressFn | None = None,
) -> BuildResult:
    """`cmake -S switch-mod -B switch-mod/build -G Ninja
       -DCMAKE_TOOLCHAIN_FILE=switch-mod/cmake/toolchain.cmake`

    Resolved cmake binary comes from prereqs (rejects msys2's cmake).
    Build dir is created if missing.
    """
    repo = repo if repo is not None else repo_root()
    src = switch_mod_root(repo)
    bd = build_dir(repo)
    tc = toolchain_file(repo)

    if not src.is_dir():
        return BuildResult(False, 1, f"switch-mod root missing: {src}")
    if not tc.is_file():
        return BuildResult(False, 1, f"toolchain file missing: {tc}")

    bd.mkdir(parents=True, exist_ok=True)

    cmd = [
        resolved_cmake(),
        "-S", str(src),
        "-B", str(bd),
        "-G", "Ninja",
        f"-DCMAKE_TOOLCHAIN_FILE={tc}",
    ]
    env = _compose_build_env()
    return _stream_subprocess(
        cmd,
        env=env,
        on_line=on_line,
        wall_timeout_s=CONFIGURE_WALL_TIMEOUT_S,
        stall_timeout_s=CONFIGURE_STALL_TIMEOUT_S,
    )


def cmake_build(
    *,
    repo: Path | None = None,
    on_line: ProgressFn | None = None,
) -> BuildResult:
    """`cmake --build switch-mod/build`.

    Runs in the configured build/ dir; cmake routes to Ninja, which
    parallelizes across CPUs by default. Output verification happens
    in the calling `run_build_phase()` so caller can distinguish "build
    succeeded but artifacts missing" from "build failed".
    """
    repo = repo if repo is not None else repo_root()
    bd = build_dir(repo)
    if not bd.is_dir():
        return BuildResult(
            False, 1,
            f"build dir missing: {bd} (run cmake_configure first)",
        )

    cmd = [resolved_cmake(), "--build", str(bd)]
    env = _compose_build_env()
    return _stream_subprocess(
        cmd,
        env=env,
        on_line=on_line,
        wall_timeout_s=BUILD_WALL_TIMEOUT_S,
        stall_timeout_s=BUILD_STALL_TIMEOUT_S,
    )


def run_build_phase(
    *,
    repo: Path | None = None,
    on_line: ProgressFn | None = None,
    skip_configure_if_ready: bool = True,
) -> CMakeOutcome:
    """End-to-end build orchestrator.

    Skips cmake_configure if `build/CMakeCache.txt` already exists and
    `skip_configure_if_ready=True` (the dev re-build case). Always runs
    cmake_build.

    Verifies both artifacts exist + are non-empty after build; treats a
    successful build with missing artifacts as a failure so the wizard
    surfaces it rather than the deploy phase blowing up later.
    """
    repo = repo if repo is not None else repo_root()
    bd = build_dir(repo)
    step_results: dict[str, BuildResult] = {}

    cache = bd / "CMakeCache.txt"
    if not (skip_configure_if_ready and cache.is_file()):
        cfg = cmake_configure(repo=repo, on_line=on_line)
        step_results["configure"] = cfg
        if not cfg.ok:
            return CMakeOutcome(ok=False, step_results=step_results)

    bld = cmake_build(repo=repo, on_line=on_line)
    step_results["build"] = bld
    if not bld.ok:
        return CMakeOutcome(ok=False, step_results=step_results)

    artifacts = expected_artifacts(repo)
    missing: list[str] = []
    for key, path in artifacts.items():
        try:
            if not path.is_file() or path.stat().st_size == 0:
                missing.append(f"{key}={path}")
        except OSError:
            missing.append(f"{key}={path}")
    if missing:
        msg = f"build returned 0 but artifacts missing/empty: {', '.join(missing)}"
        if on_line is not None:
            on_line(msg)
        return CMakeOutcome(
            ok=False,
            step_results=step_results,
            outputs={},
        )

    return CMakeOutcome(ok=True, step_results=step_results, outputs=artifacts)
