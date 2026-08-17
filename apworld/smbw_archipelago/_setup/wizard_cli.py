"""Headless orchestrator for the SMBW setup pipeline.

The Kivy wizard (`wizard.py`) drives the same five phases through user-
clicked pages: **probe** (prereqs) → **install** (silent installers
for missing tools) → **junction** (create the apworld dev-junction into
vendor/Archipelago/custom_worlds) → **build** (cmake configure + ninja
build of switch-mod) → **deploy** (copy artifacts to Ryujinx).

This module exposes the *same* sequencing as a stateless, callback-driven
API so pytest / CI / a future packaged headless installer can drive the
whole pipeline without booting Kivy. Each phase is one function that:

  - Takes its parameters as explicit arguments (no module globals).
  - Streams progress through a single `EventCallback` so the caller can
    surface logs in whatever shape it wants (JSON Lines on stdout for CI,
    Kivy widget updates for the GUI, captured `list[dict]` for tests).
  - Returns a typed `*Outcome` dataclass with `ok: bool`. A failed phase
    short-circuits the pipeline; the orchestrator never silently swallows
    errors.

The module also serves as a
`python -m apworld.smbw_archipelago._setup.wizard_cli` entry point. With
`--json-events` each event becomes one JSON object on stdout
(line-buffered, so a tail-f or a `pytest`-captured subprocess sees
events live). Without the flag, events are rendered as human-readable
log lines for terminal use.

Kivy is NEVER imported here — wizard.py stays the only module that
touches it.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Sequence


# ---------------------------------------------------------------------------
# Event stream
# ---------------------------------------------------------------------------

EventCallback = Callable[[dict[str, Any]], None]


PHASE_PROBE = "probe"
PHASE_INSTALL = "install"
PHASE_JUNCTION = "junction"
PHASE_BUILD = "build"
PHASE_DEPLOY = "deploy"

ALL_PHASES: tuple[str, ...] = (
    PHASE_PROBE,
    PHASE_INSTALL,
    PHASE_JUNCTION,
    PHASE_BUILD,
    PHASE_DEPLOY,
)


def _emit(cb: EventCallback | None, event: str, *, t0: float, **fields: Any) -> None:
    """Build an event dict and hand it to the callback (if any).

    Centralized so every emitted event carries the same `event` + `ts`
    shape and the t0 anchor stays consistent across the pipeline.
    """
    if cb is None:
        return
    payload: dict[str, Any] = {
        "event": event,
        "ts": round(time.monotonic() - t0, 6),
    }
    payload.update(fields)
    cb(payload)


# ---------------------------------------------------------------------------
# Per-phase outcome types
# ---------------------------------------------------------------------------

@dataclass
class ProbeOutcome:
    ok: bool
    results: list[Any]            # list[prereqs.PrereqResult]
    missing_keys: list[str]

    def blocking(self, *, installable_fixes: bool = False) -> list[Any]:
        """Failed prereqs that must stop the pipeline before `build`.

        Warn-only rows (Ryujinx) never block. When `installable_fixes` is
        True the caller intends to run the install phase, so rows the
        wizard can auto-install are not counted as blockers — whatever
        remains is something only the user can fix (on Linux: cmake,
        ninja, LLVM 19, Python, git — all `auto_installable=False`
        because we won't drive apt/dnf/pacman).
        """
        return [
            r for r in self.results
            if not r.ok and not r.warn_only
            and not (installable_fixes and r.auto_installable)
        ]


@dataclass
class InstallOutcome:
    ok: bool
    installed: list[str]
    failed: list[str]


@dataclass
class JunctionOutcomeWrapper:
    ok: bool
    action: str = ""
    target: str = ""
    source: str = ""
    message: str = ""


@dataclass
class BuildOutcomeWrapper:
    ok: bool
    step_results: dict[str, Any] = field(default_factory=dict)
    outputs: dict[str, Path] = field(default_factory=dict)


@dataclass
class DeployOutcomeWrapper:
    ok: bool
    target: str
    files: list[tuple[Path, Path]] = field(default_factory=list)
    error: str = ""


@dataclass
class PipelineOutcome:
    ok: bool
    phases_run: list[str]
    failed_phase: str | None = None
    probe: ProbeOutcome | None = None
    install: InstallOutcome | None = None
    junction: JunctionOutcomeWrapper | None = None
    build: BuildOutcomeWrapper | None = None
    deploy: DeployOutcomeWrapper | None = None


# ---------------------------------------------------------------------------
# Per-phase orchestrators
# ---------------------------------------------------------------------------

def run_probe(
    *,
    callback: EventCallback | None = None,
    t0: float | None = None,
) -> ProbeOutcome:
    """Run every prereq detector and surface per-row results."""
    from .prereqs import all_ok, check_all, missing_auto_installable

    anchor = t0 if t0 is not None else time.monotonic()
    _emit(callback, "phase_start", phase=PHASE_PROBE, t0=anchor)
    results = check_all()
    for r in results:
        _emit(
            callback, "prereq",
            t0=anchor,
            key=r.key,
            name=r.name,
            ok=r.ok,
            detail=r.detail,
            warn_only=r.warn_only,
            auto_installable=r.auto_installable,
        )
    ok = all_ok(results)
    missing = missing_auto_installable(results)
    _emit(callback, "phase_end", phase=PHASE_PROBE, t0=anchor, ok=ok)
    return ProbeOutcome(ok=ok, results=list(results), missing_keys=missing)


def run_install(
    keys: Sequence[str],
    *,
    preflight: bool = True,
    callback: EventCallback | None = None,
    t0: float | None = None,
) -> InstallOutcome:
    """Run installers for the given prereq keys in INSTALL_ORDER sequence.

    `preflight=True` runs internet + winget probes once before the first
    installer. A failed preflight short-circuits with ok=False.
    """
    from .installers import (
        INSTALLERS, INSTALL_ORDER, check_internet, check_winget, install_many,
    )

    anchor = t0 if t0 is not None else time.monotonic()
    _emit(callback, "phase_start", phase=PHASE_INSTALL, t0=anchor, keys=list(keys))

    ordered = [k for k in INSTALL_ORDER if k in set(keys)]
    unknown = [k for k in keys if k not in set(INSTALL_ORDER)]
    for k in unknown:
        _emit(callback, "log", phase=PHASE_INSTALL, t0=anchor,
              line=f"[wizard_cli] no installer registered for {k!r}; skipping")

    if not ordered:
        _emit(callback, "phase_end", phase=PHASE_INSTALL, t0=anchor, ok=True)
        return InstallOutcome(ok=True, installed=[], failed=[])

    if preflight:
        _emit(callback, "log", phase=PHASE_INSTALL, t0=anchor,
              line="[wizard_cli] preflight: checking internet...")
        r = check_internet(
            lambda line: _emit(callback, "log",
                               phase=PHASE_INSTALL, t0=anchor, line=line))
        _emit(callback, "preflight",
              t0=anchor, kind="internet", ok=r.ok, detail=r.detail)
        if not r.ok:
            _emit(callback, "phase_end", phase=PHASE_INSTALL, t0=anchor, ok=False)
            return InstallOutcome(ok=False, installed=[], failed=list(ordered))
        winget_keys = {"git", "cmake", "ninja", "python311"}
        if any(k in winget_keys for k in ordered):
            _emit(callback, "log", phase=PHASE_INSTALL, t0=anchor,
                  line="[wizard_cli] preflight: checking winget...")
            r = check_winget(
                lambda line: _emit(callback, "log",
                                   phase=PHASE_INSTALL, t0=anchor, line=line))
            _emit(callback, "preflight",
                  t0=anchor, kind="winget", ok=r.ok, detail=r.detail)
            if not r.ok:
                _emit(callback, "phase_end",
                      phase=PHASE_INSTALL, t0=anchor, ok=False)
                return InstallOutcome(ok=False, installed=[], failed=list(ordered))

    installed: list[str] = []
    failed: list[str] = []
    for key in ordered:
        fn = INSTALLERS.get(key)
        if fn is None:
            _emit(callback, "log", phase=PHASE_INSTALL, t0=anchor,
                  line=f"[wizard_cli] no installer fn for {key!r}; skipping")
            continue
        _emit(callback, "install_start", t0=anchor, key=key)
        result = fn(
            lambda line, _k=key: _emit(
                callback, "log",
                phase=PHASE_INSTALL, t0=anchor, key=_k, line=line))
        _emit(callback, "install_end",
              t0=anchor, key=key, ok=result.ok,
              returncode=result.returncode, detail=result.detail)
        if result.ok:
            installed.append(key)
        else:
            failed.append(key)
            break

    ok = not failed
    _emit(callback, "phase_end", phase=PHASE_INSTALL, t0=anchor, ok=ok)
    return InstallOutcome(ok=ok, installed=installed, failed=failed)


def run_junction(
    *,
    callback: EventCallback | None = None,
    t0: float | None = None,
) -> JunctionOutcomeWrapper:
    """Create the vendor/Archipelago/custom_worlds/smbw_archipelago junction.

    Skipped when we're not running from a dev clone: there is no
    ``vendor/Archipelago/`` for the junction to live in, and the apworld
    must already be discoverable some other way (pip-installed,
    extracted into a stock AP ``custom_worlds/``) or the wizard could
    not have launched at all.  Reporting ok=True here lets a packaged-
    install user run probe/build/deploy without seeing a spurious
    junction failure.
    """
    from .junction import install_junction
    from .prereqs import is_dev_clone

    anchor = t0 if t0 is not None else time.monotonic()
    _emit(callback, "phase_start", phase=PHASE_JUNCTION, t0=anchor)

    if not is_dev_clone():
        msg = "skipped: not a dev clone (apworld is already discoverable by the running Archipelago)"
        _emit(callback, "junction_result",
              t0=anchor, ok=True, action="skipped",
              target="", source="", message=msg)
        _emit(callback, "phase_end", phase=PHASE_JUNCTION, t0=anchor, ok=True)
        return JunctionOutcomeWrapper(
            ok=True, action="skipped", target="", source="", message=msg,
        )

    result = install_junction()
    _emit(callback, "junction_result",
          t0=anchor, ok=result.ok, action=result.action,
          target=str(result.target), source=str(result.source),
          message=result.message)
    _emit(callback, "phase_end", phase=PHASE_JUNCTION, t0=anchor, ok=result.ok)
    return JunctionOutcomeWrapper(
        ok=result.ok,
        action=result.action,
        target=str(result.target),
        source=str(result.source),
        message=result.message,
    )


def run_build(
    *,
    skip_configure_if_ready: bool = True,
    bridge_host: str | None = None,
    callback: EventCallback | None = None,
    t0: float | None = None,
) -> BuildOutcomeWrapper:
    """Run cmake configure (skipped if cache exists) + ninja build.

    Pre-warms prereq detectors that populate `resolved_*` caches the
    build subprocess reads (LLVM, cmake, ninja, Python). When PROBE was
    run earlier in the same pipeline the caches are already populated
    and the warm is a fast no-op; when `--phases build` is run alone
    the warm makes sure the build env is properly composed.

    `bridge_host`, when set, forwards `-DBRIDGE_HOST_STRING=<addr>` to
    cmake so the Switch bridge-discovery sweep seeds the right /24 for
    play on a non-192.168.1.x network. It triggers a reconfigure only
    when the value differs from the one already in the cmake cache —
    `run_build_phase` owns that call and logs which way it went.
    """
    from .build import run_build_phase
    from .prereqs import (
        check_all, resolved_cmake, resolved_llvm_bin, resolved_ninja_bin,
    )

    anchor = t0 if t0 is not None else time.monotonic()
    _emit(callback, "phase_start", phase=PHASE_BUILD, t0=anchor)

    needs_warm = (
        resolved_cmake() == "cmake"
        or resolved_llvm_bin() is None
        or resolved_ninja_bin() is None
    )
    if needs_warm:
        _emit(callback, "log", phase=PHASE_BUILD, t0=anchor,
              line="[wizard_cli] prewarming prereq detectors so the build "
                   "subprocess gets the wizard-verified toolchain pinned")
        check_all()

    if bridge_host:
        # Don't claim a reconfigure here — run_build_phase decides, and
        # skips when the cached seed already matches. Saying "forcing
        # reconfigure" unconditionally is what made a wedged build dir
        # (cache present, build.ninja missing) read as a cmake bug: the
        # log promised a configure that the next line never spawned.
        _emit(callback, "log", phase=PHASE_BUILD, t0=anchor,
              line=f"[wizard_cli] bridge-discovery sweep seed requested: "
                   f"{bridge_host}")

    outcome = run_build_phase(
        on_line=lambda line: _emit(callback, "log",
                                   phase=PHASE_BUILD, t0=anchor, line=line),
        skip_configure_if_ready=skip_configure_if_ready,
        bridge_host=bridge_host,
    )
    for step_name, step_result in outcome.step_results.items():
        _emit(callback, "build_step", t0=anchor, step=step_name,
              ok=step_result.ok, returncode=step_result.returncode)
    if outcome.ok:
        _emit(callback, "build_outputs", t0=anchor,
              files={k: str(v) for k, v in outcome.outputs.items()})
    _emit(callback, "phase_end", phase=PHASE_BUILD, t0=anchor, ok=outcome.ok)
    return BuildOutcomeWrapper(
        ok=outcome.ok,
        step_results=dict(outcome.step_results),
        outputs=dict(outcome.outputs),
    )


DEPLOY_TARGETS: tuple[str, ...] = ("ryujinx", "sd", "custom", "none")


def run_deploy(
    target: str,
    target_path: Path | None,
    build_outputs: dict[str, Path],
    *,
    callback: EventCallback | None = None,
    t0: float | None = None,
) -> DeployOutcomeWrapper:
    """Copy build outputs to the chosen target."""
    from .deploy import (
        deploy_to_custom_folder, deploy_to_ryujinx, deploy_to_sd,
        detect_ryujinx_path,
    )

    anchor = t0 if t0 is not None else time.monotonic()
    _emit(callback, "phase_start", phase=PHASE_DEPLOY, t0=anchor,
          target=target,
          target_path=str(target_path) if target_path else None)

    if target == "none":
        _emit(callback, "phase_end", phase=PHASE_DEPLOY, t0=anchor, ok=True)
        return DeployOutcomeWrapper(ok=True, target="none (skipped)")

    if target == "ryujinx":
        resolved = target_path or detect_ryujinx_path()
        if resolved is None or not Path(resolved).is_dir():
            err = (
                f"Ryujinx folder not found "
                f"(passed={target_path!r}, auto={detect_ryujinx_path()!r}). "
                f"Pass --deploy-path explicitly or install Ryujinx."
            )
            _emit(callback, "phase_end", phase=PHASE_DEPLOY, t0=anchor,
                  ok=False, error=err)
            return DeployOutcomeWrapper(ok=False, target="ryujinx", error=err)
        result = deploy_to_ryujinx(Path(resolved), build_outputs)
    elif target == "sd":
        if target_path is None:
            err = "--deploy-path is required for sd target"
            _emit(callback, "phase_end", phase=PHASE_DEPLOY, t0=anchor,
                  ok=False, error=err)
            return DeployOutcomeWrapper(ok=False, target="sd", error=err)
        if not target_path.exists():
            err = f"SD card path does not exist: {target_path}"
            _emit(callback, "phase_end", phase=PHASE_DEPLOY, t0=anchor,
                  ok=False, error=err)
            return DeployOutcomeWrapper(ok=False, target="sd", error=err)
        result = deploy_to_sd(target_path, build_outputs)
    elif target == "custom":
        if target_path is None:
            err = "--deploy-path is required for custom target"
            _emit(callback, "phase_end", phase=PHASE_DEPLOY, t0=anchor,
                  ok=False, error=err)
            return DeployOutcomeWrapper(ok=False, target="custom", error=err)
        if not target_path.parent.exists():
            err = f"Custom folder parent does not exist: {target_path.parent}"
            _emit(callback, "phase_end", phase=PHASE_DEPLOY, t0=anchor,
                  ok=False, error=err)
            return DeployOutcomeWrapper(ok=False, target="custom", error=err)
        target_path.mkdir(parents=True, exist_ok=True)
        result = deploy_to_custom_folder(target_path, build_outputs)
    else:
        err = f"unknown deploy target {target!r} (expected one of {DEPLOY_TARGETS})"
        _emit(callback, "phase_end", phase=PHASE_DEPLOY, t0=anchor,
              ok=False, error=err)
        return DeployOutcomeWrapper(ok=False, target=target, error=err)

    _emit(callback, "deploy_result",
          t0=anchor, ok=result.ok, target=result.target,
          file_count=len(result.files), error=result.error)
    _emit(callback, "phase_end", phase=PHASE_DEPLOY, t0=anchor, ok=result.ok)
    return DeployOutcomeWrapper(
        ok=result.ok, target=result.target,
        files=list(result.files), error=result.error,
    )


# ---------------------------------------------------------------------------
# Pipeline orchestrator
# ---------------------------------------------------------------------------

@dataclass
class PipelineOptions:
    phases: tuple[str, ...] = ALL_PHASES
    deploy_target: str = "none"
    deploy_path: Path | None = None
    install_missing: bool = False
    install_preflight: bool = True
    skip_configure_if_ready: bool = True
    # Optional bridge-discovery sweep seed. When set, the build phase
    # forwards `-DBRIDGE_HOST_STRING=<addr>` so the Switch sweeps the
    # right /24 on a non-192.168.1.x play network.
    bridge_host: str | None = None


def run_pipeline(
    opts: PipelineOptions,
    *,
    callback: EventCallback | None = None,
) -> PipelineOutcome:
    """Run the requested phases in order; short-circuit on first failure."""
    t0 = time.monotonic()
    _emit(callback, "pipeline_start", t0=t0, phases=list(opts.phases))

    outcome = PipelineOutcome(ok=True, phases_run=[])

    probe: ProbeOutcome | None = None
    if PHASE_PROBE in opts.phases:
        probe = run_probe(callback=callback, t0=t0)
        outcome.probe = probe
        outcome.phases_run.append(PHASE_PROBE)
        # A failed probe stops here unless every failing row is something
        # the install phase can actually fix. The old test was just
        # `not probe.ok and not opts.install_missing`, so with the GUI's
        # auto-install checkbox on (the default) ANY probe failure fell
        # through: install skipped the rows it couldn't handle with
        # "no missing auto-installable prereqs", and the pipeline marched
        # into `build` with no cmake and the wrong LLVM. The user saw a
        # cmake linker error instead of "install cmake and LLVM 19".
        will_install = opts.install_missing and PHASE_INSTALL in opts.phases
        blocking = probe.blocking(installable_fixes=will_install)
        if not probe.ok and (blocking or not will_install):
            for r in blocking:
                _emit(callback, "prereq_blocking", t0=t0,
                      key=r.key, name=r.name, detail=r.detail,
                      install_url=r.install_url, note=r.note)
            if blocking:
                _emit(callback, "log", phase=PHASE_PROBE, t0=t0,
                      line=(
                          "[wizard_cli] stopping before the remaining phases: "
                          + ", ".join(r.name for r in blocking)
                          + " must be installed by hand first (the wizard "
                            "only auto-installs system tools on Windows)"
                      ))
            outcome.ok = False
            outcome.failed_phase = PHASE_PROBE
            _emit(callback, "pipeline_end", t0=t0, ok=False,
                  failed_phase=PHASE_PROBE,
                  blocking=[r.key for r in blocking])
            return outcome

    if PHASE_INSTALL in opts.phases:
        if not opts.install_missing:
            _emit(callback, "phase_skip", phase=PHASE_INSTALL, t0=t0,
                  reason="install_missing not requested")
        elif probe is None:
            _emit(callback, "phase_skip", phase=PHASE_INSTALL, t0=t0,
                  reason="probe phase not run; nothing to install")
        elif not probe.missing_keys:
            _emit(callback, "phase_skip", phase=PHASE_INSTALL, t0=t0,
                  reason="no missing auto-installable prereqs")
        else:
            inst = run_install(
                probe.missing_keys,
                preflight=opts.install_preflight,
                callback=callback, t0=t0,
            )
            outcome.install = inst
            outcome.phases_run.append(PHASE_INSTALL)
            if not inst.ok:
                outcome.ok = False
                outcome.failed_phase = PHASE_INSTALL
                _emit(callback, "pipeline_end", t0=t0, ok=False,
                      failed_phase=PHASE_INSTALL)
                return outcome

    if PHASE_JUNCTION in opts.phases:
        jn = run_junction(callback=callback, t0=t0)
        outcome.junction = jn
        outcome.phases_run.append(PHASE_JUNCTION)
        if not jn.ok:
            outcome.ok = False
            outcome.failed_phase = PHASE_JUNCTION
            _emit(callback, "pipeline_end", t0=t0, ok=False,
                  failed_phase=PHASE_JUNCTION)
            return outcome

    if PHASE_BUILD in opts.phases:
        bd = run_build(
            skip_configure_if_ready=opts.skip_configure_if_ready,
            bridge_host=opts.bridge_host,
            callback=callback, t0=t0,
        )
        outcome.build = bd
        outcome.phases_run.append(PHASE_BUILD)
        if not bd.ok:
            outcome.ok = False
            outcome.failed_phase = PHASE_BUILD
            _emit(callback, "pipeline_end", t0=t0, ok=False,
                  failed_phase=PHASE_BUILD)
            return outcome

    if PHASE_DEPLOY in opts.phases:
        if outcome.build is not None:
            outputs = outcome.build.outputs
        else:
            from .build import expected_artifacts
            outputs = expected_artifacts()
            missing = [k for k, p in outputs.items() if not p.is_file()]
            if missing:
                err = (
                    f"deploy phase requested but no build outputs found: "
                    f"missing {', '.join(missing)}. Run the build phase first."
                )
                _emit(callback, "phase_skip", phase=PHASE_DEPLOY, t0=t0,
                      reason=err)
                outcome.ok = False
                outcome.failed_phase = PHASE_DEPLOY
                _emit(callback, "pipeline_end", t0=t0, ok=False,
                      failed_phase=PHASE_DEPLOY)
                return outcome
        dp = run_deploy(
            opts.deploy_target, opts.deploy_path, outputs,
            callback=callback, t0=t0,
        )
        outcome.deploy = dp
        outcome.phases_run.append(PHASE_DEPLOY)
        if not dp.ok:
            outcome.ok = False
            outcome.failed_phase = PHASE_DEPLOY
            _emit(callback, "pipeline_end", t0=t0, ok=False,
                  failed_phase=PHASE_DEPLOY)
            return outcome

    _emit(callback, "pipeline_end", t0=t0, ok=outcome.ok,
          phases_run=outcome.phases_run)
    return outcome


# ---------------------------------------------------------------------------
# Callback adapters
# ---------------------------------------------------------------------------

def make_json_events_callback(stream=None) -> EventCallback:
    """Return a callback that writes one JSON object per event to `stream`."""
    s = stream if stream is not None else sys.stdout

    def emit(payload: dict[str, Any]) -> None:
        s.write(json.dumps(payload, default=str) + "\n")
        s.flush()
    return emit


def make_text_callback(stream=None) -> EventCallback:
    """Return a callback that renders events as human-readable log lines."""
    s = stream if stream is not None else sys.stdout

    def emit(payload: dict[str, Any]) -> None:
        ts = payload.get("ts", 0.0)
        if payload.get("event") == "log":
            line = payload.get("line", "")
            s.write(f"[t+{ts:.3f}] {line}\n")
        else:
            fields = " ".join(
                f"{k}={v!r}" for k, v in payload.items()
                if k not in ("event", "ts")
            )
            evt = payload.get("event")
            s.write(f"[t+{ts:.3f}] {evt} {fields}\n")
        s.flush()
    return emit


# ---------------------------------------------------------------------------
# CLI entry
# ---------------------------------------------------------------------------

def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="python -m apworld.smbw_archipelago._setup.wizard_cli",
        description=(
            "Headless SMBW Archipelago setup pipeline. Runs the same "
            "probe → install → junction → build → deploy sequence the "
            "Kivy wizard drives, but with no UI and a JSON-event stream "
            "suitable for CI."
        ),
    )
    p.add_argument("--json-events", action="store_true",
                   help="Emit one JSON object per event on stdout.")
    p.add_argument("--phases", default=",".join(ALL_PHASES),
                   help=f"Comma-separated phase subset. Choices: {','.join(ALL_PHASES)}.")
    p.add_argument("--deploy-target", choices=DEPLOY_TARGETS, default="none",
                   help="Deploy destination. Default: none (skip).")
    p.add_argument("--deploy-path", type=Path, default=None,
                   help="Deploy destination path. Optional for ryujinx.")
    p.add_argument("--auto-install", action="store_true",
                   help="Auto-install missing prereqs before later phases.")
    p.add_argument("--no-install-preflight", action="store_true",
                   help="Skip the internet + winget preflight (testing only).")
    p.add_argument("--force-configure", action="store_true",
                   help="Re-run cmake configure even if the build dir is ready.")
    p.add_argument("--bridge-host", default=None, metavar="IPV4",
                   help="Seed the Switch bridge-discovery /24 sweep with this "
                        "address (forwarded as -DBRIDGE_HOST_STRING). Use when "
                        "playing on a subnet other than 192.168.1.x; the value "
                        "only needs to be SOME address on the target /24. Pass "
                        "'auto' to use this machine's own LAN IP. Reconfigures "
                        "when the seed differs from the cached one.")
    return p


def main(argv: list[str] | None = None) -> int:
    """Headless CLI entry. Returns 0 on success, 1 on failure."""
    args = _build_parser().parse_args(argv)
    requested = tuple(p.strip() for p in args.phases.split(",") if p.strip())
    unknown = [p for p in requested if p not in ALL_PHASES]
    if unknown:
        sys.stderr.write(
            f"unknown phase(s): {','.join(unknown)}. "
            f"Valid: {','.join(ALL_PHASES)}\n"
        )
        return 2

    bridge_host = args.bridge_host.strip() if args.bridge_host else None
    if bridge_host:
        try:
            from ..client.net_util import is_plausible_ipv4, lan_subnet_seed
        except ImportError:  # apworld loaded as a top-level package
            from smbw_archipelago.client.net_util import (  # type: ignore
                is_plausible_ipv4, lan_subnet_seed,
            )
        if bridge_host.lower() == "auto":
            bridge_host = lan_subnet_seed() or None
            if bridge_host is None:
                sys.stderr.write(
                    "--bridge-host auto: no routable LAN IP detected "
                    "(only loopback); falling back to the compiled-in default\n"
                )
        elif not is_plausible_ipv4(bridge_host):
            sys.stderr.write(
                f"--bridge-host {bridge_host!r} is not a valid dotted-quad "
                f"IPv4 address (e.g. 10.0.0.5) or 'auto'\n"
            )
            return 2

    opts = PipelineOptions(
        phases=requested,
        deploy_target=args.deploy_target,
        deploy_path=args.deploy_path,
        install_missing=args.auto_install,
        install_preflight=not args.no_install_preflight,
        skip_configure_if_ready=not args.force_configure,
        bridge_host=bridge_host,
    )
    callback = (
        make_json_events_callback() if args.json_events
        else make_text_callback()
    )
    outcome = run_pipeline(opts, callback=callback)
    return 0 if outcome.ok else 1


if __name__ == "__main__":
    sys.exit(main())
