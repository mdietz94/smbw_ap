"""Tests for the headless orchestrator.

We monkeypatch the per-phase functions to avoid touching the network or
the user's machine. The orchestrator's job is sequencing + event
emission + short-circuit-on-failure; we test exactly those.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from apworld.smbw_archipelago._setup import wizard_cli as W


def test_phase_constants_match_all_phases() -> None:
    assert set(W.ALL_PHASES) == {
        W.PHASE_PROBE, W.PHASE_INSTALL, W.PHASE_JUNCTION,
        W.PHASE_BUILD, W.PHASE_DEPLOY,
    }


def test_emit_attaches_event_and_ts() -> None:
    events: list[dict[str, Any]] = []
    import time
    t0 = time.monotonic()
    W._emit(events.append, "hello", t0=t0, key="value")
    assert len(events) == 1
    e = events[0]
    assert e["event"] == "hello"
    assert e["key"] == "value"
    assert "ts" in e


def test_emit_with_none_callback_is_noop() -> None:
    """Phase functions should accept callback=None without crashing."""
    W._emit(None, "test", t0=0.0)


def test_make_json_events_callback(tmp_path: Path) -> None:
    import io
    buf = io.StringIO()
    cb = W.make_json_events_callback(buf)
    cb({"event": "test", "ts": 0.1, "key": "value"})
    cb({"event": "done", "ts": 0.2})
    lines = buf.getvalue().strip().splitlines()
    assert len(lines) == 2
    import json
    parsed = [json.loads(ln) for ln in lines]
    assert parsed[0]["event"] == "test"
    assert parsed[1]["event"] == "done"


def test_run_pipeline_short_circuits_on_probe_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    """When probe fails AND install_missing=False, the pipeline must
    stop immediately (junction/build/deploy never run)."""
    calls: list[str] = []

    def fake_probe(**_kw: Any) -> W.ProbeOutcome:
        calls.append("probe")
        return W.ProbeOutcome(ok=False, results=[], missing_keys=[])

    def fake_install(*_a: Any, **_kw: Any) -> W.InstallOutcome:
        calls.append("install")
        return W.InstallOutcome(ok=True, installed=[], failed=[])

    def fake_junction(**_kw: Any) -> W.JunctionOutcomeWrapper:
        calls.append("junction")
        return W.JunctionOutcomeWrapper(ok=True)

    monkeypatch.setattr(W, "run_probe", fake_probe)
    monkeypatch.setattr(W, "run_install", fake_install)
    monkeypatch.setattr(W, "run_junction", fake_junction)

    outcome = W.run_pipeline(W.PipelineOptions(install_missing=False))
    assert outcome.ok is False
    assert outcome.failed_phase == W.PHASE_PROBE
    assert calls == ["probe"]   # nothing after probe ran


def test_run_pipeline_runs_install_when_authorized(monkeypatch: pytest.MonkeyPatch) -> None:
    """Probe fails but install_missing=True → install runs, and if it
    succeeds the pipeline continues."""
    calls: list[str] = []

    def fake_probe(**_kw: Any) -> W.ProbeOutcome:
        calls.append("probe")
        return W.ProbeOutcome(ok=False, results=[], missing_keys=["git"])

    def fake_install(keys: list[str], **_kw: Any) -> W.InstallOutcome:
        calls.append("install")
        return W.InstallOutcome(ok=True, installed=list(keys), failed=[])

    def fake_junction(**_kw: Any) -> W.JunctionOutcomeWrapper:
        calls.append("junction")
        return W.JunctionOutcomeWrapper(ok=True)

    def fake_build(**_kw: Any) -> W.BuildOutcomeWrapper:
        calls.append("build")
        return W.BuildOutcomeWrapper(
            ok=True,
            outputs={
                "subsdk9": Path("/fake/subsdk9"),
                "main.npdm": Path("/fake/main.npdm"),
            },
        )

    def fake_deploy(*_a: Any, **_kw: Any) -> W.DeployOutcomeWrapper:
        calls.append("deploy")
        return W.DeployOutcomeWrapper(ok=True, target="ryujinx")

    monkeypatch.setattr(W, "run_probe", fake_probe)
    monkeypatch.setattr(W, "run_install", fake_install)
    monkeypatch.setattr(W, "run_junction", fake_junction)
    monkeypatch.setattr(W, "run_build", fake_build)
    monkeypatch.setattr(W, "run_deploy", fake_deploy)

    outcome = W.run_pipeline(W.PipelineOptions(
        install_missing=True, deploy_target="ryujinx",
    ))
    assert outcome.ok is True
    assert calls == ["probe", "install", "junction", "build", "deploy"]


def test_run_pipeline_skips_install_when_no_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    """If probe says everything's fine, install must NOT run even when
    install_missing=True."""
    install_called = []

    monkeypatch.setattr(W, "run_probe", lambda **_kw: W.ProbeOutcome(
        ok=True, results=[], missing_keys=[],
    ))
    monkeypatch.setattr(W, "run_install", lambda *a, **kw: install_called.append(True))
    monkeypatch.setattr(W, "run_junction", lambda **_kw: W.JunctionOutcomeWrapper(ok=True))
    monkeypatch.setattr(W, "run_build", lambda **_kw: W.BuildOutcomeWrapper(
        ok=True, outputs={"subsdk9": Path("/x"), "main.npdm": Path("/y")},
    ))
    monkeypatch.setattr(W, "run_deploy", lambda *a, **kw: W.DeployOutcomeWrapper(
        ok=True, target="none",
    ))

    outcome = W.run_pipeline(W.PipelineOptions(install_missing=True))
    assert outcome.ok is True
    assert install_called == []   # install was NOT called


def test_run_pipeline_short_circuits_on_build_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    """A build failure must prevent deploy from running."""
    deploy_called = []

    monkeypatch.setattr(W, "run_probe", lambda **_kw: W.ProbeOutcome(
        ok=True, results=[], missing_keys=[],
    ))
    monkeypatch.setattr(W, "run_junction", lambda **_kw: W.JunctionOutcomeWrapper(ok=True))
    monkeypatch.setattr(W, "run_build", lambda **_kw: W.BuildOutcomeWrapper(ok=False))
    monkeypatch.setattr(W, "run_deploy", lambda *a, **kw: deploy_called.append(True))

    outcome = W.run_pipeline(W.PipelineOptions(deploy_target="ryujinx"))
    assert outcome.ok is False
    assert outcome.failed_phase == W.PHASE_BUILD
    assert deploy_called == []


def test_run_pipeline_emits_pipeline_start_and_end(monkeypatch: pytest.MonkeyPatch) -> None:
    events: list[dict[str, Any]] = []

    monkeypatch.setattr(W, "run_probe", lambda **_kw: W.ProbeOutcome(
        ok=True, results=[], missing_keys=[],
    ))
    monkeypatch.setattr(W, "run_junction", lambda **_kw: W.JunctionOutcomeWrapper(ok=True))
    monkeypatch.setattr(W, "run_build", lambda **_kw: W.BuildOutcomeWrapper(
        ok=True, outputs={"subsdk9": Path("/x"), "main.npdm": Path("/y")},
    ))
    monkeypatch.setattr(W, "run_deploy", lambda *a, **kw: W.DeployOutcomeWrapper(
        ok=True, target="none",
    ))

    W.run_pipeline(W.PipelineOptions(), callback=events.append)
    evts = [e["event"] for e in events]
    assert "pipeline_start" in evts
    assert "pipeline_end" in evts


def test_run_deploy_skips_when_target_none() -> None:
    """`--deploy-target none` is the CI/test bypass — no copy needed."""
    out = W.run_deploy("none", None, build_outputs={})
    assert out.ok is True


def test_run_deploy_rejects_unknown_target() -> None:
    out = W.run_deploy("not-a-target", None, build_outputs={})
    assert out.ok is False
    assert "unknown deploy target" in out.error


def test_cli_main_parses_phases(monkeypatch: pytest.MonkeyPatch) -> None:
    """`--phases probe` should only run the probe phase."""
    monkeypatch.setattr(W, "run_pipeline", lambda opts, callback=None: W.PipelineOutcome(
        ok=True, phases_run=list(opts.phases),
    ))
    rc = W.main(["--phases", "probe"])
    assert rc == 0


def test_cli_main_rejects_unknown_phase() -> None:
    rc = W.main(["--phases", "made-up-phase"])
    assert rc == 2
