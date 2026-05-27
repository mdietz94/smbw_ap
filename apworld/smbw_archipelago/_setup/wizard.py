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
from .deploy import detect_ryujinx_path, detect_sd_candidates
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


class BufferedLogStreamer:
    """Coalesces line events from worker threads into batched Kivy updates.

    Why: the cmake/ninja build can stream hundreds of lines/sec. Scheduling
    a separate Clock.schedule_once per line saturates the UI thread — each
    callback reassigns log_view.text, forcing a full retokenize+relayout.
    The window goes "not responding" until the queue drains.  This class
    decouples production (O(1) lock-protected list append on the worker
    thread) from display (one Clock.schedule_interval tick → ONE text
    assignment per 100 ms).

    File writes still happen synchronously on the worker thread so the log
    is durable even if the wizard process dies before the next drain tick.
    """

    def __init__(
        self,
        log_box,
        *,
        max_buffer: int = 2000,
        tail_size: int = 400,
        drain_interval: float = 0.1,
        file_writer=None,
    ) -> None:
        self._log_box = log_box
        self._max_buffer = max_buffer
        self._tail_size = tail_size
        self._drain_interval = drain_interval
        self._file_writer = file_writer
        self._visible: list[str] = []
        self._pending: list[str] = []
        self._lock = threading.Lock()
        self._event = None
        self._start()

    def _start(self) -> None:
        from kivy.clock import Clock as _Clock
        if self._event is None:
            self._event = _Clock.schedule_interval(self._drain, self._drain_interval)

    def on_line(self, line: str) -> None:
        """Thread-safe entry point. Safe to call from any thread."""
        if self._file_writer is not None:
            try:
                self._file_writer(line)
            except Exception:
                pass
        with self._lock:
            self._pending.append(line)

    def _drain(self, _dt) -> None:
        with self._lock:
            if not self._pending:
                return
            batch = self._pending
            self._pending = []
        self._visible.extend(batch)
        if len(self._visible) > self._max_buffer:
            del self._visible[: len(self._visible) - self._max_buffer]
        self._log_box.text = "\n".join(self._visible[-self._tail_size:])


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
    from kivy.uix.spinner import Spinner
    from kivy.uix.textinput import TextInput

    _append_wizard_log("=== wizard start ===")
    saved_state = load_setup_state()

    # Deploy target dropdown labels.  Order matches PipelineOptions'
    # supported values; the wizard's worker maps display label -> key.
    _DEPLOY_LABELS: dict[str, str] = {
        "ryujinx": "Ryujinx",
        "sd":      "SD card",
        "custom":  "Custom folder",
        "none":    "None (skip deploy)",
    }
    _DEPLOY_KEYS: dict[str, str] = {v: k for k, v in _DEPLOY_LABELS.items()}

    # Initial deploy target: honor what setup_state.json remembers from a
    # prior successful run, fall back to "ryujinx" (the previous default).
    initial_target = saved_state.get("deploy_target", "ryujinx")
    if initial_target not in _DEPLOY_LABELS:
        initial_target = "ryujinx"

    # Initial deploy path: target-aware default.  For Ryujinx we feed
    # the auto-detected %APPDATA%\Ryujinx path so the user can see what
    # the wizard will use; for SD we offer the first mounted Switch SD
    # card (Atmosphere layout); custom defaults to empty.
    def _default_deploy_path(target: str) -> str:
        if target == "ryujinx":
            p = detect_ryujinx_path()
            return str(p) if p is not None else ""
        if target == "sd":
            sds = detect_sd_candidates()
            return str(sds[0]) if sds else ""
        return saved_state.get("deploy_path", "") if target == "custom" else ""

    # Shared state between the UI and the worker thread.
    state: dict[str, Any] = {
        "running": False,
        "ok": False,
        "auto_install": True,
        "deploy_target": initial_target,
        "deploy_path": _default_deploy_path(initial_target),
    }

    class WizardApp(App):
        def build(self):
            self.title = "SMBW Archipelago - Setup"
            root = BoxLayout(orientation="vertical", padding=12, spacing=8)

            # --- Header
            root.add_widget(Label(
                text="[size=20][b]SMBW Archipelago Setup[/b][/size]",
                markup=True, size_hint_y=None, height=40,
            ))
            root.add_widget(Label(
                text=(
                    "Runs: probe -> install missing -> junction -> build -> deploy.\n"
                    "Detailed log at " + str(wizard_log_path())
                ),
                size_hint_y=None, height=48,
                halign="left", valign="top",
            ))

            # --- Options
            opts = BoxLayout(
                orientation="vertical", size_hint_y=None, height=140, spacing=4,
            )

            ai_row = BoxLayout(orientation="horizontal", size_hint_y=None, height=36)
            ai_cb = CheckBox(active=True, size_hint_x=None, width=40)
            def _ai_change(_inst, value):
                state["auto_install"] = bool(value)
            ai_cb.bind(active=_ai_change)
            ai_row.add_widget(ai_cb)
            ai_row.add_widget(Label(
                text="Auto-install missing prerequisites (winget + LLVM 19 + git submodules)",
                halign="left", valign="middle",
            ))
            opts.add_widget(ai_row)

            # Deploy target selector: dropdown + path field side-by-side.
            # The path field's editability follows the dropdown -- Ryujinx
            # defaults to auto-detected %APPDATA%\Ryujinx (editable in case
            # the user has a non-default install), SD/Custom require an
            # explicit path, None disables the field entirely.
            deploy_row = BoxLayout(orientation="horizontal", size_hint_y=None, height=36, spacing=8)
            deploy_row.add_widget(Label(
                text="Deploy target:", halign="left", valign="middle",
                size_hint_x=None, width=130,
            ))
            spinner = Spinner(
                text=_DEPLOY_LABELS[state["deploy_target"]],
                values=tuple(_DEPLOY_LABELS.values()),
                size_hint_x=None, width=200,
            )
            self.deploy_path_input = TextInput(
                text=state["deploy_path"],
                multiline=False,
                size_hint_x=1,
            )
            def _deploy_target_change(_inst, value):
                key = _DEPLOY_KEYS.get(value, "none")
                state["deploy_target"] = key
                # Reset the path field to the new target's default. The
                # user can still edit it afterward -- this is just a
                # convenience nudge so switching Ryujinx -> SD doesn't
                # leave a stale Ryujinx path in the box.
                new_default = _default_deploy_path(key)
                self.deploy_path_input.text = new_default
                state["deploy_path"] = new_default
                # "None" target has no meaningful path; lock the field so
                # an accidental keystroke can't surface as a confusing
                # "deploy-path supplied but target=none" warning later.
                self.deploy_path_input.disabled = (key == "none")
            spinner.bind(text=_deploy_target_change)
            self.deploy_path_input.bind(
                text=lambda _i, v: state.update(deploy_path=v))
            self.deploy_path_input.disabled = (state["deploy_target"] == "none")
            deploy_row.add_widget(spinner)
            deploy_row.add_widget(self.deploy_path_input)
            opts.add_widget(deploy_row)

            # Per-target hint line so the user knows what they're committing
            # to before they click Run.
            sd_count = len(detect_sd_candidates())
            hint_lines = [
                "  Ryujinx: writes subsdk9 + main.npdm under <path>/mods/contents/<TITLE_ID>/smbwap/exefs/",
                f"  SD card: writes under <drive>/atmosphere/contents/<TITLE_ID>/exefs/ ({sd_count} card(s) detected)",
                "  Custom folder: same atmosphere/contents/... layout under your chosen folder (useful for offline SD-sync)",
            ]
            opts.add_widget(Label(
                text="\n".join(hint_lines), size_hint_y=None, height=60,
                halign="left", valign="top", font_size="11sp",
            ))

            root.add_widget(opts)

            # --- Log pane
            self.log_view = TextInput(
                text="", readonly=True, font_name="RobotoMono-Regular",
                background_color=(0.07, 0.07, 0.09, 1),
                foreground_color=(0.9, 0.9, 0.9, 1),
                cursor_color=(0, 0, 0, 0),
            )
            self._streamer = BufferedLogStreamer(
                self.log_view, file_writer=_append_wizard_log,
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

        # ----- log helper -----

        def _log(self, line: str) -> None:
            """Thread-safe log append — call from any thread."""
            self._streamer.on_line(line)

        # ----- pipeline kick-off -----

        def _start_run(self) -> None:
            if state["running"]:
                self._log("[wizard] already running")
                return
            state["running"] = True
            state["ok"] = False
            self.run_btn.disabled = True
            self.run_btn.text = "Running..."
            self.close_btn.disabled = True
            self._log("[wizard] starting pipeline")
            t = threading.Thread(target=self._worker, daemon=True)
            t.start()

        def _worker(self) -> None:
            try:
                target = state["deploy_target"]
                path_str = (state.get("deploy_path") or "").strip()
                # Only the SD / Custom / Ryujinx-with-override paths
                # carry a deploy_path through to run_deploy(); "none"
                # ignores it, and Ryujinx with the auto-detected path
                # is fine to leave None so detect_ryujinx_path() runs
                # again at deploy time (catches the user installing
                # Ryujinx between wizard runs).
                deploy_path = Path(path_str) if path_str else None
                if target == "ryujinx" and deploy_path is not None:
                    auto = detect_ryujinx_path()
                    if auto is not None and Path(str(auto)) == deploy_path:
                        deploy_path = None
                opts = PipelineOptions(
                    phases=ALL_PHASES,
                    install_missing=state["auto_install"],
                    deploy_target=target,
                    deploy_path=deploy_path,
                )

                def cb(payload: dict[str, Any]) -> None:
                    self._log(_format_event(payload))

                outcome = run_pipeline(opts, callback=cb)
                state["ok"] = outcome.ok
                summary = (
                    "[wizard] pipeline OK" if outcome.ok
                    else f"[wizard] pipeline FAILED at {outcome.failed_phase}"
                )
                self._log(summary)
                # Persist a small state file so a future re-run can default
                # to the same deploy target + path.
                save_setup_state({
                    **saved_state,
                    "deploy_target": opts.deploy_target,
                    "deploy_path": path_str,
                    "last_ok": outcome.ok,
                })
            except Exception as e:  # pragma: no cover — defensive
                self._log(f"[wizard] crashed: {type(e).__name__}: {e}")
                log.exception("wizard worker crashed")
            finally:
                state["running"] = False

                def _reset_btn(_dt: float) -> None:
                    self.run_btn.disabled = False
                    self.run_btn.text = "Run setup again"
                    self.close_btn.disabled = False
                Clock.schedule_once(_reset_btn, 0)

    WizardApp().run()
    _append_wizard_log(f"=== wizard end (ok={state['ok']}) ===")
    return bool(state["ok"])
