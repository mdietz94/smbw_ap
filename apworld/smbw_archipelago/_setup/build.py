"""Drive the cmake configure + ninja build of switch-mod (hakkun subsdk).

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

  - CMake binary         ← prereqs.resolved_cmake() (rejects msys2's cmake)
  - PATH (prepended)     ← resolved LLVM bin (clang/clang++ for hakkun's
                           toolchain.cmake), resolved Ninja bin, resolved
                           Python dir (LibHakkun's toolchain.cmake shells
                           out to bare `python3` for setup_libcxx_prepackaged.py).
  - The toolchain file itself is NOT passed as -DCMAKE_TOOLCHAIN_FILE;
    [switch-mod/CMakeLists.txt] sets it via early `set(CMAKE_TOOLCHAIN_FILE
    "${CMAKE_SOURCE_DIR}/sys/cmake/toolchain.cmake")` before `project()`.
    Passing a -D would be either dead (overridden by the in-CMakeLists set)
    or actively wrong (if it pointed somewhere else).
"""

from __future__ import annotations

import os
import queue
import shutil
import subprocess
import sys
import threading
import time
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from . import appdata_root, local_appdata_root
from .patch_hakkun import apply_patches as _apply_hakkun_patches
from .prereqs import (
    is_dev_clone,
    repo_root,
    resolved_cmake,
    resolved_llvm_bin,
    resolved_ninja_bin,
    resolved_python_bin,
)

# Where this module lives on disk.  In a packaged install (.apworld zip
# loaded via zipimport) this is a path WITH the zip-file as a midpoint
# directory -- `Path.is_dir()` returns False for siblings, and cmake
# can't read sources at such paths.  `_extract_bundled_tree()` handles
# the extraction.  In a dev checkout this is just a regular directory.
_SETUP_ROOT = Path(__file__).resolve().parent

# Memoizes the resolved on-disk location of the bundled tree.  Once
# extraction succeeds we never re-extract within the same process.
_extracted_bundled_root: Path | None = None

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


def _find_apworld_zip(setup_root: Path) -> Path | None:
    """Walk up from ``setup_root`` looking for a ``.apworld`` file ancestor.

    Returns the zip path if ``setup_root`` is inside a zip-loaded apworld
    (the production case under the AP Launcher), or None on a dev source
    checkout where ``setup_root`` is a real on-disk directory.
    """
    cur = setup_root
    # Walk up at most ~10 levels; .apworld is normally 2-3 levels above us.
    for _ in range(10):
        if cur.suffix == ".apworld" and cur.is_file():
            return cur
        parent = cur.parent
        if parent == cur:
            return None
        cur = parent
    return None


def _bundled_tree_has_files(root: Path) -> bool:
    """True iff the bundled tree at ``root`` has at least one
    ``switch-mod`` file -- used to reject a zip whose prefix filter
    matched no real entries (truncated or corrupt apworld)."""
    mod = root / "switch-mod"
    try:
        return mod.is_dir() and any(mod.iterdir())
    except OSError:
        return False


def _extract_bundled_tree(
    setup_root: Path | None = None,
    *,
    dst_override: Path | None = None,
) -> Path:
    """Extract the bundled ``switch_mod/`` tree from the apworld zip to
    a real filesystem location and return that location.

    Necessary because:
      - AP loads ``.apworld`` files via Python's zipimporter. Code inside
        the zip imports fine, but ``Path(__file__).parent / "switch_mod"
        / "CMakeLists.txt"`` is a path string with the .apworld ZIP-file
        as a midpoint directory: ``Path.exists()`` returns False and
        cmake can't open files at such paths.
      - cmake reads switch-mod's source tree as a regular filesystem
        layout (CMakeLists.txt, src/, lib/imgui/, lib/sead/, etc.).

    Caches in ``_extracted_bundled_root`` so we only extract once per
    process.  On a dev source checkout where ``setup_root`` is a real
    directory (no ``.apworld`` zip ancestor), the in-place
    ``_SETUP_ROOT`` is returned unchanged.

    Extraction target: ``%APPDATA%/SMBWArchipelago/bundled/``.  The full
    switch-mod source incl. vendored imgui / NintendoSDK / sead is
    several MB; kept out of the AP install dir (which on the official
    installer often requires admin to write to).

    Tests pass ``dst_override`` to redirect into a tempdir so we don't
    touch the user's real %APPDATA%.
    """
    global _extracted_bundled_root
    if _extracted_bundled_root is not None and dst_override is None:
        return _extracted_bundled_root

    root = setup_root if setup_root is not None else _SETUP_ROOT
    apworld_zip = _find_apworld_zip(root)
    if apworld_zip is None:
        # Dev / source checkout -- _SETUP_ROOT IS the real on-disk dir.
        # The bundled switch_mod lives at repo_root() / "switch-mod" in
        # that case; the caller (`switch_mod_root`) knows to look there.
        if dst_override is None:
            _extracted_bundled_root = root
        return root

    dst = dst_override if dst_override is not None else (appdata_root() / "bundled")
    # Marker file records the source-zip mtime so an apworld upgrade
    # (user drops a newer .apworld in) triggers re-extract instead of
    # serving a stale cached copy.
    marker = dst / ".source-zip-mtime"
    src_mtime = apworld_zip.stat().st_mtime

    if marker.exists():
        try:
            cached_mtime = float(marker.read_text(encoding="utf-8").strip())
            if cached_mtime == src_mtime and _bundled_tree_has_files(dst):
                if dst_override is None:
                    _extracted_bundled_root = dst
                return dst
        except (ValueError, OSError):
            pass  # corrupt marker -- re-extract

    # Stale or absent.  Extract to a sibling "<dst>.new" then atomically
    # rename so a mid-extraction crash can't poison the cache: the
    # marker file is the LAST thing written, only after the swap.
    staging = dst.with_name(dst.name + ".new")
    if staging.exists():
        try:
            shutil.rmtree(staging)
        except OSError as e:
            raise RuntimeError(
                f"Could not clear stale bundled-tree staging dir at "
                f"{staging}: {e}. Close any program that might be "
                f"holding files in this folder (Ryujinx, an antivirus "
                f"realtime scan, Explorer windows) and re-run setup."
            ) from e
    try:
        staging.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        raise RuntimeError(
            f"Could not create bundled-tree staging dir at {staging}: "
            f"{e}. Check that %APPDATA% is writable and has free space."
        ) from e

    # Inside the .apworld zip the bundle lives at
    # ``smbwonder/_setup/switch-mod/...`` -- see
    # scripts/install_apworld.py's --bundle-mod path mapping.  We extract
    # the contents of ``smbwonder/_setup/`` to the staging dir so that
    # callers see the same ``<staging>/switch-mod/...`` layout the dev
    # checkout has at ``<repo>/switch-mod/...`` -- the existing
    # ``switch_mod_root(repo) = repo / "switch-mod"`` math then works
    # for both shapes by passing the bundled root as ``repo``.
    prefix = "smbwonder/_setup/"
    current_target: Path | None = None
    try:
        with zipfile.ZipFile(apworld_zip) as zf:
            for info in zf.infolist():
                name = info.filename
                if not name.startswith(prefix):
                    continue
                rel = name[len(prefix):]
                if not rel:
                    continue  # the prefix entry itself
                target = staging / rel
                current_target = target
                if info.is_dir() or name.endswith("/"):
                    target.mkdir(parents=True, exist_ok=True)
                    continue
                target.parent.mkdir(parents=True, exist_ok=True)
                with zf.open(info) as src_f, open(target, "wb") as dst_f:
                    shutil.copyfileobj(src_f, dst_f)
                # Post-write size assertion: a disk-full or AV-induced
                # truncation between read and write completion can leave
                # the destination shorter than expected without raising.
                actual = target.stat().st_size
                if actual != info.file_size:
                    raise RuntimeError(
                        f"bundled tree extraction wrote {actual} bytes "
                        f"for {rel} but the zip entry declares "
                        f"{info.file_size}"
                    )
    except (OSError, zipfile.BadZipFile, RuntimeError) as e:
        try:
            shutil.rmtree(staging, ignore_errors=True)
        except Exception:
            pass
        loc = f" while writing {current_target}" if current_target else ""
        raise RuntimeError(
            f"Failed to extract bundled tree from {apworld_zip.name}"
            f"{loc}: {type(e).__name__}: {e}. Common causes: disk full, "
            f"antivirus blocking the write, or the .apworld zip is "
            f"corrupt. Free space under %APPDATA%, then re-run setup."
        ) from e

    if not _bundled_tree_has_files(staging):
        try:
            shutil.rmtree(staging, ignore_errors=True)
        except Exception:
            pass
        raise FileNotFoundError(
            f"bundled tree extracted from {apworld_zip.name} contains "
            f"no files under switch_mod/. The apworld was likely built "
            f"without `--bundle-mod`, or the zip is truncated -- "
            f"re-download the release and try again."
        )

    # Swap-in.  On Windows a rename across a non-empty target is a hard
    # error, so the rmtree of the old `dst` must come first.
    if dst.exists():
        try:
            shutil.rmtree(dst)
        except OSError as e:
            try:
                shutil.rmtree(staging, ignore_errors=True)
            except Exception:
                pass
            raise RuntimeError(
                f"Could not remove old bundled tree at {dst}: {e}. "
                f"Close any program holding files in that folder "
                f"(Ryujinx, antivirus realtime scan, Explorer window) "
                f"and re-run setup."
            ) from e
    try:
        staging.rename(dst)
    except OSError as e:
        raise RuntimeError(
            f"Could not finalize bundled tree at {dst}: {e}. The "
            f"staged tree at {staging} is intact; you can rename it "
            f"manually or re-run setup."
        ) from e

    try:
        marker.write_text(str(src_mtime), encoding="utf-8")
    except OSError as e:
        raise RuntimeError(
            f"Bundled tree extracted to {dst} but the cache marker "
            f"could not be written: {e}. The next run will re-extract "
            f"unnecessarily -- non-fatal but wastes I/O."
        ) from e
    if dst_override is None:
        _extracted_bundled_root = dst
    return dst


def _resolve_repo(repo: Path | None) -> Path:
    """When the build helpers are called with ``repo=None``, choose
    between the dev clone (``<git-root>``) and the freshly-extracted
    bundle root (``%APPDATA%/SMBWArchipelago/bundled``) based on whether
    we're running from a packaged install.

    Returned path is suitable to pass to :func:`switch_mod_root` /
    :func:`build_dir` / :func:`expected_artifacts` -- in both shapes
    ``<repo>/switch-mod/CMakeLists.txt`` is the cmake source root.
    """
    if repo is not None:
        return repo
    if is_dev_clone():
        return repo_root()
    return _extract_bundled_tree()


def bundled_switch_mod() -> Path:
    """Return the on-disk ``switch-mod/`` directory for cmake to read.

    Dispatches to the dev tree at ``<repo>/switch-mod`` when we're in
    a git clone, or to the freshly-extracted bundle under
    ``%APPDATA%/SMBWArchipelago/bundled/switch-mod`` otherwise.  Raises
    FileNotFoundError if neither is available -- typically means the
    apworld was packaged without ``--bundle-mod``.
    """
    root = _resolve_repo(None)
    mod = root / "switch-mod"
    if not (mod / "CMakeLists.txt").is_file():
        raise FileNotFoundError(
            f"switch-mod sources not found at {mod}. "
            f"The apworld zip was likely built without `--bundle-mod`. "
            f"Re-download a release build, or run "
            f"`python scripts/install_apworld.py --bundle-mod` from a "
            f"dev clone of the source repo."
        )
    return mod


def bundled_switch_mod_available() -> bool:
    """Non-raising probe for prereq checks.  Doesn't trigger extraction.

    Only the cheap detection -- "we have a dev tree" or "there's an
    apworld zip we could extract from" -- so the prereq row stays
    snappy.  The real failure (truncated zip, no ``--bundle-mod``)
    surfaces from :func:`bundled_switch_mod` at build time with a
    precise error.
    """
    if is_dev_clone():
        return (repo_root() / "switch-mod" / "CMakeLists.txt").is_file()
    return _find_apworld_zip(_SETUP_ROOT) is not None


def switch_mod_root(repo: Path | None = None) -> Path:
    """Path to the cmake source directory.

    The original API: ``repo=None`` resolves to the active repo (dev or
    packaged bundle).  Tests pass ``repo=tmp_path`` to point at a
    synthetic layout.
    """
    return _resolve_repo(repo) / "switch-mod"


def build_dir(repo: Path | None = None) -> Path:
    return switch_mod_root(repo) / "build"


def expected_artifacts(repo: Path | None = None) -> dict[str, Path]:
    """The two files `cmake --build` is expected to produce.

    Keys match what `deploy._ryujinx_layout` consumes: `"subsdk9"` and
    `"main.npdm"`. LibHakkun's deploy.cmake step copies both files into
    `build/exefs/` with the correct names — no rename is needed.
    """
    bd = build_dir(repo)
    exefs = bd / "exefs"
    return {
        "subsdk9": exefs / "subsdk9",
        "main.npdm": exefs / "main.npdm",
    }


def _ensure_python3_on_path(py_bin: str, env: dict[str, str]) -> None:
    """Ensure ``python3.exe`` is reachable in the cmake subprocess PATH.

    LibHakkun's toolchain.cmake runs bare ``python3
    sys/tools/setup_libcxx_prepackaged.py`` to download and unpack the
    prebuilt libc++/musl/compiler-rt bundle into ``lib/std/``.  On
    Windows, standard CPython installs ship ``python.exe`` but NOT
    ``python3.exe``.  The Microsoft Store stub that answers ``python3``
    silently exits without writing anything, so ``lib/std/`` is never
    populated and the cmake compiler-check link fails with missing *.a.

    prereqs.ensure_python3_shim() tries to copy ``python.exe →
    python3.exe`` alongside the interpreter, but silently swallows the
    OSError when that directory is write-protected (Program Files install,
    AP-launcher frozen PyInstaller bundle, etc.).

    This function is the second line of defence: if ``python3.exe`` still
    isn't present in the dir we just prepended, we create the shim in
    ``%LOCALAPPDATA%/SMBWArchipelago/bin/`` (always writable) and prepend
    that directory so it shadows any Store stub on the inherited PATH.
    """
    if sys.platform != "win32":
        return
    py_dir = Path(py_bin).parent
    if (py_dir / "python3.exe").is_file():
        return  # shim already in place — nothing to do

    shim_dir = local_appdata_root() / "bin"
    shim = shim_dir / "python3.exe"
    if not shim.is_file():
        try:
            shim_dir.mkdir(parents=True, exist_ok=True)
            shutil.copy2(py_bin, shim)
        except OSError:
            return  # cmake will surface a clear error about python3
    # Relocate-to-front (same reasoning as _compose_build_env.prepend):
    # the shim must shadow any `python3.exe` already on PATH (e.g. a
    # different Python that happens to ship one), not silently lose to it.
    s = str(shim_dir)
    parts = [p for p in env.get("PATH", "").split(os.pathsep)
             if p and p != s]
    env["PATH"] = os.pathsep.join([s, *parts])


def _compose_build_env() -> dict[str, str]:
    """Build the env dict every subprocess inherits.

    Composed from:
      - the parent process env (inherit base)
      - PATH prepended with the resolved LLVM `bin/` dir. LibHakkun's
        [switch-mod/sys/cmake/toolchain.cmake] hardcodes the compilers
        to bare `clang` / `clang++`, so this is what makes cmake find
        the wizard-verified LLVM 19.1.x rather than whatever else is
        first on the inherited PATH.
      - PATH prepended with the resolved Python dir. LibHakkun's
        toolchain.cmake shells out to bare `python3 sys/tools/setup_libcxx_prepackaged.py`
        when its prepackaged libc++/musl/compiler-rt bundle is missing.
        Without `python3` on PATH, that resolves to the Microsoft Store
        stub and silently no-ops; prereqs.ensure_python3_shim() drops a
        `python3.exe` copy alongside, but only if the dir is on PATH.
      - PATH prepended with the resolved Ninja bin dir if we have one.
      - PYTHONIOENCODING=utf-8 so child python prints don't crash on
        non-ASCII (em-dashes etc.) under cp932 / cp1252 default consoles.
    """
    env = os.environ.copy()

    def prepend(dir_path: str) -> None:
        # Relocate-to-front, not skip-if-present. The earlier version
        # no-op'd when `dir_path` was already anywhere on the inherited
        # PATH — which silently broke the whole point of resolving a
        # specific interpreter/toolchain. Concrete failure: a dev box
        # with Python 3.14 first on PATH and the wizard-resolved 3.12
        # (via `py -3.12`) later on PATH. The wizard installs lz4 /
        # pyelftools into the resolved 3.12, but because 3.12's dir was
        # already on PATH the prepend was skipped, so cmake's POST_BUILD
        # bare `python elf2nso.py` / `deploy.py` resolved to the 3.14
        # that's first on PATH — which lacks lz4 — and the build died with
        # `ModuleNotFoundError: No module named 'lz4'` right after linking.
        # Removing any existing occurrence and re-prepending makes the
        # resolved dir authoritatively win, so the Python the build runs
        # is the same one the wizard installed the deps into.
        if not dir_path:
            return
        parts = [p for p in env.get("PATH", "").split(os.pathsep)
                 if p and p != dir_path]
        env["PATH"] = os.pathsep.join([dir_path, *parts])

    # Order matters: tail-most prepend wins on PATH lookup. We want LLVM
    # first (its `clang.exe` is what cmake invokes by bare name), then
    # Ninja, then Python (least likely to collide with anything else).
    py_bin = resolved_python_bin()
    if py_bin:
        prepend(str(Path(py_bin).parent))
        _ensure_python3_on_path(py_bin, env)
    ninja_bin = resolved_ninja_bin()
    if ninja_bin:
        prepend(ninja_bin)
    llvm_bin = resolved_llvm_bin()
    if llvm_bin:
        prepend(llvm_bin)

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
    bridge_host: str | None = None,
) -> BuildResult:
    """`cmake -S switch-mod -B switch-mod/build -G Ninja`

    Resolved cmake binary comes from prereqs (rejects msys2's cmake).
    Build dir is created if missing.

    `bridge_host`, when given, is forwarded as
    `-DBRIDGE_HOST_STRING="<addr>"`. That seeds the Switch's
    bridge-discovery /24 sweep (src/ap/ApDiscovery.cpp) so a player on a
    subnet other than 192.168.1.0/24 can still be found. The value only
    needs to be SOME address on the target /24. CMake caches the define,
    so a later configure without `bridge_host` keeps the last value;
    callers that want a guaranteed-fresh value force a reconfigure.

    Note: no `-DCMAKE_TOOLCHAIN_FILE` arg. [switch-mod/CMakeLists.txt]
    sets the toolchain itself via early
    `set(CMAKE_TOOLCHAIN_FILE "${CMAKE_SOURCE_DIR}/sys/cmake/toolchain.cmake")`
    before `project()`. The in-CMakeLists assignment wins over any -D we'd
    pass, so passing one is at best dead, at worst misleading.
    """
    repo = _resolve_repo(repo)
    src = switch_mod_root(repo)
    bd = build_dir(repo)

    if not src.is_dir():
        return BuildResult(False, 1, f"switch-mod root missing: {src}")
    # LibHakkun has no top-level CMakeLists.txt — sys/cmake/module.cmake is
    # the file `switch-mod/CMakeLists.txt:62` does `include(...)` against.
    sys_module = src / "sys" / "cmake" / "module.cmake"
    if not sys_module.is_file():
        return BuildResult(
            False, 1,
            f"LibHakkun submodule missing: {sys_module} (run "
            f"`git submodule update --init switch-mod/sys` from the repo root)",
        )

    # Patch upstream LibHakkun in-place. Without this, Python 3.14 users
    # crash at cmake POST_BUILD time inside `sys/tools/nso.py` (the no-arg
    # `NsoHeader()` ctor relies on a Struct.__new__ shape that 3.14
    # removed). See patch_hakkun.py for the full why.
    _apply_hakkun_patches(src, on_line=on_line)

    bd.mkdir(parents=True, exist_ok=True)

    cmd = [
        resolved_cmake(),
        "-S", str(src),
        "-B", str(bd),
        "-G", "Ninja",
    ]
    if bridge_host:
        cmd.append(f"-DBRIDGE_HOST_STRING={bridge_host}")
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
    repo = _resolve_repo(repo)
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


def cached_bridge_host(repo: Path | None = None) -> str | None:
    """Read the ``BRIDGE_HOST_STRING`` value already baked into
    ``build/CMakeCache.txt``, or None if absent/unreadable.

    Lets :func:`run_build_phase` tell "the seed changed, reconfigure" from
    "same seed, keep the fast rebuild path" — so a normal `/setup` that
    always carries the auto-detected LAN seed doesn't force a redundant
    reconfigure every run.
    """
    cache = build_dir(repo) / "CMakeCache.txt"
    try:
        text = cache.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    for line in text.splitlines():
        # CMakeCache.txt format: `BRIDGE_HOST_STRING:STRING=<value>`.
        if line.startswith("BRIDGE_HOST_STRING:"):
            _, _, val = line.partition("=")
            return val.strip()
    return None


def run_build_phase(
    *,
    repo: Path | None = None,
    on_line: ProgressFn | None = None,
    skip_configure_if_ready: bool = True,
    bridge_host: str | None = None,
) -> CMakeOutcome:
    """End-to-end build orchestrator.

    Skips cmake_configure if `build/CMakeCache.txt` already exists and
    `skip_configure_if_ready=True` (the dev re-build case). Always runs
    cmake_build.

    When `bridge_host` is set the configure step still runs unless the
    cache already carries that exact seed — the `-DBRIDGE_HOST_STRING`
    override has to reach cmake to land in the cache (and re-trigger
    compilation of the discovery TU). Matching the cached value lets a
    normal `/setup` (which now always passes the auto-detected LAN seed)
    keep the fast rebuild path when the seed hasn't changed.

    Verifies both artifacts exist + are non-empty after build; treats a
    successful build with missing artifacts as a failure so the wizard
    surfaces it rather than the deploy phase blowing up later.
    """
    repo = _resolve_repo(repo)
    bd = build_dir(repo)
    step_results: dict[str, BuildResult] = {}

    cache = bd / "CMakeCache.txt"
    if bridge_host:
        # Reconfigure unless the cache already holds this exact seed.
        need_configure = (
            not skip_configure_if_ready
            or cached_bridge_host(repo) != bridge_host
        )
    else:
        need_configure = not (skip_configure_if_ready and cache.is_file())
    if need_configure:
        cfg = cmake_configure(repo=repo, on_line=on_line, bridge_host=bridge_host)
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
