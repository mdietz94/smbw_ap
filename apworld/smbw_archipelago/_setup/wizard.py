"""Kivy setup wizard window.

Entry point: `run_setup_wizard()`. Surfaced via the `/setup` slash
command in SMBW Client, which spawns this in a new window via
`launch_subprocess` while SMBW Client stays open. Covers both
first-time setup and re-runs.

This is a deliberately minimal Kivy wrapper around the headless
orchestrator in `wizard_cli`. The page structure is one screen:

  - Top: the requested phase list + an "Install missing prereqs"
    checkbox + an "Auto-deploy to Ryujinx" checkbox.
  - Middle: a scrolling log pane that streams every event the
    orchestrator emits.
  - Bottom: "Run" button (kicks off the pipeline in a worker thread)
    and "Close" button.

The page is small on purpose: the headless CLI is the source of truth
and is already tested end-to-end. Anything fancier (per-phase pages,
clickable error remediation links) is a follow-up.

Kivy is imported lazily INSIDE this module — never at apworld-import
time — because AP generation hosts (Linux servers running
`python ap_generate.py`) shouldn't need Kivy installed. Anyone running
the wizard already has Kivy because SMBW Client itself uses it.
"""

from __future__ import annotations

import json
import logging
import threading
from pathlib import Path
from typing import Any

from . import appdata_root, setup_state_path
from .deploy import detect_ryujinx_path
from .wizard_cli import (
    ALL_PHASES, PipelineOptions, run_pipeline,
)

log = logging.getLogger(__name__)


def load_setup_state() -> dict[str, Any]:
    p = setup_state_path()
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        log.warning("setup_state.json unreadable; starting fresh")
        return {}


def save_setup_state(state: dict[str, Any]) -> None:
    p = setup_state_path()
    try:
        p.write_text(json.dumps(state, indent=2), encoding="utf-8")
    except OSError as e:
        log.warning("failed to write setup_state.json: %s", e)


def wizard_log_path() -> Path:
    return appdata_root() / "wizard.log"


def _append_wizard_log(line: str) -> None:
    """Append to %APPDATA%/SMBWArchipelago/wizard.log. Best-effort —
    failures here mustn't bring down the wizard."""
    try:
        with open(wizard_log_path(), "a", encoding="utf-8") as f:
            f.write(line.rstrip("\r\n") + "\n")
    except OSError:
        pass


def _format_event(payload: dict[str, Any]) -> str:
    """Render one orchestrator event as a single log line for the GUI."""
    ts = payload.get("ts", 0.0)
    evt = payload.get("event", "?")
    if evt == "log":
        return f"[t+{ts:6.2f}] {payload.get('line', '')}"
    fields = " ".join(
        f"{k}={v}" for k, v in payload.items()
        if k not in ("event", "ts")
    )
    return f"[t+{ts:6.2f}] {evt} {fields}"


def run_setup_wizard() -> bool:
    """Open the Kivy wizard window. Blocks until the user closes it.

    Returns True if the pipeline finished successfully end-to-end (so a
    caller could chain a "launch client" step), False otherwise. For
    now the return value is informational; the caller doesn't chain.
    """
    # Subprocess-safe Kivy import: kvui MUST be imported before any
    # `kivy.*` module so its kvui-side `KIVY_DATA_DIR` shim runs first.
    # Matches the order client/gui.py uses.
    import kvui  # noqa: F401

    from kivy.app import App
    from kivy.clock import Clock
    from kivy.uix.boxlayout import BoxLayout
    from kivy.uix.button import Button
    from kivy.uix.checkbox import CheckBox
    from kivy.uix.label import Label
    from kivy.uix.scrollview import ScrollView
    from kivy.uix.textinput import TextInput

    _append_wizard_log("=== wizard start ===")
    saved_state = load_setup_state()

    # Shared state between the UI and the worker thread.
    state: dict[str, Any] = {
        "running": False,
        "ok": False,
        "auto_install": True,
        "deploy_to_ryujinx": True,
    }

    class WizardApp(App):
        def build(self):
            self.title = "SMBW Archipelago — Setup"
            root = BoxLayout(orientation="vertical", padding=12, spacing=8)

            # --- Header
            root.add_widget(Label(
                text="[size=20][b]SMBW Archipelago Setup[/b][/size]",
                markup=True, size_hint_y=None, height=40,
            ))
            root.add_widget(Label(
                text=(
                    "Runs: probe → install missing → junction → build → deploy.\n"
                    "Detailed log at " + str(wizard_log_path())
                ),
                size_hint_y=None, height=48,
                halign="left", valign="top",
            ))

            # --- Options
            opts = BoxLayout(
                orientation="vertical", size_hint_y=None, height=88, spacing=4,
            )

            ai_row = BoxLayout(orientation="horizontal", size_hint_y=None, height=36)
            ai_cb = CheckBox(active=True, size_hint_x=None, width=40)
            def _ai_change(_inst, value):
                state["auto_install"] = bool(value)
            ai_cb.bind(active=_ai_change)
            ai_row.add_widget(ai_cb)
            ai_row.add_widget(Label(
                text="Auto-install missing prerequisites (winget + devkitPro + git submodules)",
                halign="left", valign="middle",
            ))
            opts.add_widget(ai_row)

            ryu_row = BoxLayout(orientation="horizontal", size_hint_y=None, height=36)
            ryu_cb = CheckBox(active=True, size_hint_x=None, width=40)
            def _ryu_change(_inst, value):
                state["deploy_to_ryujinx"] = bool(value)
            ryu_cb.bind(active=_ryu_change)
            ryu_row.add_widget(ryu_cb)
            ryujinx_hint = (
                f"Deploy to Ryujinx (detected: {detect_ryujinx_path()})"
                if detect_ryujinx_path() is not None
                else "Deploy to Ryujinx (no install detected — will fail unless Ryujinx is present)"
            )
            ryu_row.add_widget(Label(
                text=ryujinx_hint, halign="left", valign="middle",
            ))
            opts.add_widget(ryu_row)

            root.add_widget(opts)

            # --- Log pane
            self.log_view = TextInput(
                text="", readonly=True, font_name="RobotoMono-Regular",
                background_color=(0.07, 0.07, 0.09, 1),
                foreground_color=(0.9, 0.9, 0.9, 1),
                cursor_color=(0, 0, 0, 0),
            )
            scroll = ScrollView()
            scroll.add_widget(self.log_view)
            root.add_widget(scroll)

            # --- Buttons
            btn_row = BoxLayout(
                orientation="horizontal", size_hint_y=None, height=48, spacing=8,
            )
            self.run_btn = Button(text="Run setup")
            self.run_btn.bind(on_release=lambda _i: self._start_run())
            self.close_btn = Button(text="Close")
            self.close_btn.bind(on_release=lambda _i: App.get_running_app().stop())
            btn_row.add_widget(self.run_btn)
            btn_row.add_widget(self.close_btn)
            root.add_widget(btn_row)

            return root

        # ----- log helpers -----

        def _append_log(self, line: str) -> None:
            """Append one line to the GUI log pane (UI thread only)."""
            self.log_view.text = self.log_view.text + line + "\n"
            _append_wizard_log(line)

        def _schedule_append(self, line: str) -> None:
            """Thread-safe log append: marshals to UI thread."""
            Clock.schedule_once(lambda _dt, _l=line: self._append_log(_l), 0)

        # ----- pipeline kick-off -----

        def _start_run(self) -> None:
            if state["running"]:
                self._schedule_append("[wizard] already running")
                return
            state["running"] = True
            state["ok"] = False
            self.run_btn.disabled = True
            self.run_btn.text = "Running..."
            self._schedule_append("[wizard] starting pipeline")
            t = threading.Thread(target=self._worker, daemon=True)
            t.start()

        def _worker(self) -> None:
            try:
                opts = PipelineOptions(
                    phases=ALL_PHASES,
                    install_missing=state["auto_install"],
                    deploy_target=("ryujinx" if state["deploy_to_ryujinx"] else "none"),
                )

                def cb(payload: dict[str, Any]) -> None:
                    self._schedule_append(_format_event(payload))

                outcome = run_pipeline(opts, callback=cb)
                state["ok"] = outcome.ok
                summary = (
                    "[wizard] pipeline OK" if outcome.ok
                    else f"[wizard] pipeline FAILED at {outcome.failed_phase}"
                )
                self._schedule_append(summary)
                # Persist a small state file so a future re-run can default
                # to the same deploy target.
                save_setup_state({
                    **saved_state,
                    "deploy_target": opts.deploy_target,
                    "last_ok": outcome.ok,
                })
            except Exception as e:  # pragma: no cover — defensive
                self._schedule_append(f"[wizard] crashed: {type(e).__name__}: {e}")
                log.exception("wizard worker crashed")
            finally:
                state["running"] = False

                def _reset_btn(_dt: float) -> None:
                    self.run_btn.disabled = False
                    self.run_btn.text = "Run setup again"
                Clock.schedule_once(_reset_btn, 0)

    WizardApp().run()
    _append_wizard_log(f"=== wizard end (ok={state['ok']}) ===")
    return bool(state["ok"])
