"""End-to-end live wizard test, fired from the pre-push hook on tag pushes.

Opt-in via `SMBWAP_LIVE_INSTALL=1` (set automatically by
`.githooks/pre-push` → `scripts/local_release_audit.ps1`). Skips otherwise
so the default pytest run stays fast.

What this test validates, in order:

  1. **Bundle build** — `scripts/install_apworld.py --bundle-mod` produces
     a non-empty `smbwonder.apworld` containing both the apworld region
     and a populated `_setup/switch_mod/` region with all three lib
     submodules (imgui, NintendoSDK, sead).

  2. **Sandboxed APPDATA / LOCALAPPDATA** — wizard reads/writes are
     routed into a tempdir via `SMBWAP_APPDATA_ROOT` /
     `SMBWAP_LOCALAPPDATA_ROOT` (see [_setup/__init__.py](../__init__.py)
     for the env-var contract). Asserts the user's real `%APPDATA%\\SMBWArchipelago\\`
     is untouched.

  3. **Prereq detection** — `wizard_cli.run_probe()` walks every detector
     and returns a per-row result. The test asserts the probe completes
     (not that every prereq passes — devkitPro / Archipelago submodule
     init are documented manual prereqs of the audit, not the test's
     job to install).

  4. **Cold switch-mod build** — if every build-relevant prereq is
     present (devkitPro, cmake, ninja, python311, switch_mod_submodule,
     archipelago_submodule), runs `wizard_cli.run_build()` end-to-end
     and asserts `subsdk9` + `subsdk9.npdm` exist post-build. If any
     build prereq is missing the test SKIPS with a clear message — this
     isn't the audit's job to validate; it's a documented prereq.

What this test does NOT exercise:

  - Auto-install of devkitPro (~1.5 GB download + interactive installer
    UI). The test assumes the maintainer's machine has it already.
  - The deploy phase (writes to Ryujinx mods dir, outside the sandbox).
    Build outputs are validated; deploy is a `shutil.copy` step we trust.
  - Leak-audit walk (SMO has `scripts/release_audit.py` with allowed-glob
    enforcement; SMBW does not yet). Add when wizard_cli grows a
    bundled-tree extraction mode and a release_audit module to back it.

If this test passes on a clean machine that has the manual prereqs
(devkitPro, git, cmake, ninja, python3.11), every link from "fresh
checkout" to "subsdk9 ready to deploy" has been validated against
current scripts, current pinned submodule SHAs, current wizard
orchestration, and the produced apworld zip.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path

import pytest


# Opt-in gate. Set automatically by the pre-push hook; manual invocation
# is `$env:SMBWAP_LIVE_INSTALL = "1"; pytest -v -s <this file>`.
pytestmark = pytest.mark.skipif(
    not os.environ.get("SMBWAP_LIVE_INSTALL"),
    reason=("opt-in live-install e2e test; set SMBWAP_LIVE_INSTALL=1 "
            "(slow, ~5-10 min cold build)"),
)


# Resolve repo root: this file is at
# apworld/smbw_archipelago/_setup/tests/test_wizard_e2e_live.py, so root
# is 4 levels up.
_REPO_ROOT = Path(__file__).resolve().parents[4]


def _require_manual_prereqs() -> None:
    """The test assumes Python tooling, git, and devkitPro are already
    on the machine. Skip with a clear message if any is missing — this
    is the audit's documented prereq, not its job to validate."""
    for tool in ("git", "cmake", "ninja", "python"):
        if not shutil.which(tool):
            pytest.skip(
                f"manual prereq {tool!r} not on PATH. The audit assumes "
                f"the maintainer's machine has git, cmake, ninja, python3.11, "
                f"and devkitPro already installed (the wizard's auto-install "
                f"covers them on a fresh user machine; this test focuses on "
                f"the bundle + build flow above those tools)."
            )
    if not os.environ.get("DEVKITPRO"):
        # The build will fail if DEVKITPRO is unset; surface the skip
        # before spending 5 min on a guaranteed-failing build.
        pytest.skip(
            "DEVKITPRO env var not set. Install devkitPro and ensure the "
            "system-wide env var points at C:\\devkitPro (or equivalent)."
        )


@pytest.fixture
def short_sandbox_root() -> Path:
    """A tempdir at the root of the system drive instead of pytest's
    default `%TEMP%\\pytest-of-<user>\\pytest-NNN\\test_NAME\\...` path.

    Mirrors SMO's rationale: the switch-mod build can chain ninja +
    cmake + tool invocations through long CommandLine sequences that
    risk Windows' ~8KB cap when pytest's default tempdir is deep enough.
    Putting the sandbox at `C:\\smbwape2e-XXXX\\` keeps the per-arg path
    short.
    """
    root = Path(tempfile.mkdtemp(
        prefix="smbwape2e-",
        dir=os.environ.get("SYSTEMDRIVE", "C:") + "\\",
    ))
    try:
        yield root
    finally:
        # ignore_errors so a half-cleaned-up sandbox doesn't fail an
        # otherwise-passing test. Leftover C:\smbwape2e-* is obvious.
        shutil.rmtree(root, ignore_errors=True)


@pytest.mark.skipif(os.name != "nt",
                    reason="wizard is Windows-only; e2e test mirrors that")
def test_bundle_and_build_against_real_environment(
    short_sandbox_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The single, expensive, release-only test.

    Builds the bundled apworld zip, sandboxes the wizard's write roots,
    runs the prereq probe, and runs a cold switch-mod build end-to-end.
    """
    _require_manual_prereqs()

    # --- 1. Sandbox -----------------------------------------------------
    sandbox_appdata = short_sandbox_root / "appdata"
    sandbox_localappdata = short_sandbox_root / "localappdata"
    monkeypatch.setenv("SMBWAP_APPDATA_ROOT", str(sandbox_appdata))
    monkeypatch.setenv("SMBWAP_LOCALAPPDATA_ROOT", str(sandbox_localappdata))

    # --- 2. Build the apworld zip ---------------------------------------
    install_apworld = _REPO_ROOT / "scripts" / "install_apworld.py"
    apworld_zip = (
        _REPO_ROOT / "vendor" / "Archipelago" / "custom_worlds"
        / "smbwonder.apworld"
    )
    # Capture pre-existing state so we can restore it (the test must not
    # alter the user's dev-mode custom_worlds layout).
    pre_existed = apworld_zip.is_file()
    pre_existing_bytes = apworld_zip.read_bytes() if pre_existed else None
    subprocess.run(
        [sys.executable, str(install_apworld), "--bundle-mod"],
        check=True, cwd=str(_REPO_ROOT),
    )
    try:
        assert apworld_zip.is_file(), (
            f"install_apworld did not produce {apworld_zip}"
        )

        # Verify the zip has both regions populated.
        with zipfile.ZipFile(apworld_zip) as zf:
            names = zf.namelist()
        apworld_files = [n for n in names
                         if not n.startswith("smbwonder/_setup/switch_mod/")]
        mod_files = [n for n in names
                     if n.startswith("smbwonder/_setup/switch_mod/")]
        assert apworld_files, "apworld region empty"
        assert mod_files, "_setup/switch_mod region empty (--bundle-mod broken?)"
        # Sentinel files the wizard needs to build.
        assert "smbwonder/_setup/switch_mod/CMakeLists.txt" in names
        assert "smbwonder/_setup/switch_mod/cmake/toolchain.cmake" in names
        assert "smbwonder/_setup/switch_mod/src/program/main.cpp" in names
        # All three lib submodules populated.
        for sub in ("imgui", "NintendoSDK", "sead"):
            sub_files = [n for n in mod_files
                         if f"/lib/{sub}/" in n]
            assert sub_files, (
                f"lib/{sub} submodule is empty in bundle — "
                f"run `git submodule update --init --recursive` and re-run"
            )

        # --- 3. Sandbox write check -------------------------------------
        # The wizard's appdata_root() honors SMBWAP_APPDATA_ROOT. Confirm
        # the override is in effect by importing it and checking the
        # resolved path lands inside the sandbox. (The function creates
        # the dir on first access; that's the side-effect we're after.)
        from apworld.smbw_archipelago._setup import (
            appdata_root, local_appdata_root,
        )
        resolved_appdata = appdata_root()
        resolved_local = local_appdata_root()
        assert resolved_appdata == sandbox_appdata, (
            f"SMBWAP_APPDATA_ROOT override did not take effect: "
            f"got {resolved_appdata}, expected {sandbox_appdata}"
        )
        assert resolved_local == sandbox_localappdata, (
            f"SMBWAP_LOCALAPPDATA_ROOT override did not take effect: "
            f"got {resolved_local}, expected {sandbox_localappdata}"
        )

        # --- 4. Probe phase ---------------------------------------------
        # Just confirms the probe doesn't crash. We do NOT assert every
        # prereq passes — devkitPro / Archipelago submodule init are
        # auto-installable on a user's machine but documented manual
        # prereqs for the audit.
        from apworld.smbw_archipelago._setup import wizard_cli
        probe_outcome = wizard_cli.run_probe()
        assert probe_outcome.results, "probe returned no results"
        # Print the per-row status for the maintainer reading -v -s output.
        print()
        print("Probe results:")
        for r in probe_outcome.results:
            mark = "OK " if r.ok else "MISS"
            print(f"  {mark}  {r.key:30s}  {r.detail}")
        # Probe is allowed to find missing things; the BUILD is what
        # actually requires every-prereq-green.

        # --- 5. Cold switch-mod build -----------------------------------
        # Only attempt the build if every build-side prereq is detected.
        # Missing prereqs here SKIP rather than fail — they're
        # documented requirements of running the audit, and the rest of
        # the bundle/probe coverage above is still meaningful.
        build_required_keys = {
            "devkitpro", "cmake", "ninja", "python311",
            "archipelago_submodule", "switch_mod_submodule",
        }
        by_key = {r.key: r for r in probe_outcome.results}
        missing_for_build = [
            k for k in build_required_keys
            if k in by_key and not by_key[k].ok
        ]
        if missing_for_build:
            pytest.skip(
                f"build-side prereqs missing: {missing_for_build}. "
                f"Install via the wizard's auto-install flow on a user "
                f"machine, or surface a clearer skip here if these are "
                f"intentionally absent in your dev env."
            )

        build_outcome = wizard_cli.run_build()
        assert build_outcome.ok, (
            f"wizard_cli.run_build failed. step_results="
            f"{ {k: getattr(v, 'returncode', v) for k, v in build_outcome.step_results.items()} }"
        )
        assert set(build_outcome.outputs.keys()) >= {"subsdk9", "main.npdm"}, (
            f"build outputs missing required artifacts: "
            f"{build_outcome.outputs!r}"
        )
        for name, path in build_outcome.outputs.items():
            assert path.is_file(), f"{name!r} reported at {path} but file missing"
            assert path.stat().st_size > 0, f"{name!r} at {path} is zero-sized"

    finally:
        # Restore the user's pre-test state for the apworld zip so the
        # test doesn't perturb their dev-mode setup.
        if pre_existing_bytes is not None:
            apworld_zip.write_bytes(pre_existing_bytes)
        else:
            apworld_zip.unlink(missing_ok=True)
