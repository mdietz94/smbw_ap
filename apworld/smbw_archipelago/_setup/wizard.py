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
import os
import sys
import threading
from pathlib import Path
from typing import Any

from . import appdata_root, setup_state_path
from .deploy import (
    detect_ryujinx_path,
    detect_sd_candidates,
    ryujinx_primary_default,
)
from .wizard_cli import (
    ALL_PHASES, PipelineOptions, run_pipeline,
)
from ..client.net_util import is_plausible_ipv4, lan_subnet_seed

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
            # Prefer the existing install location; fall back to the
            # platform default so the user always sees where deploy
            # *would* write (the grayed-out field would otherwise be
            # blank if Ryujinx isn't installed yet).
            p = detect_ryujinx_path() or ryujinx_primary_default()
            return str(p) if p is not None else ""
        if target == "sd":
            sds = detect_sd_candidates()
            return str(sds[0]) if sds else ""
        return saved_state.get("deploy_path", "") if target == "custom" else ""

    # Initial bridge-discovery sweep seed. Priority:
    #   1. the env var `/setup_ip <addr>` exports (explicit override),
    #   2. this machine's own LAN IP — the smart default: the Switch then
    #      sweeps the same /24 the PC client lives on, so a player on any
    #      subnet is found with no typing,
    #   3. setup_state.json from a prior run (covers the case where LAN
    #      detection yields only loopback),
    #   4. blank (= the compiled-in 192.168.1.x default).
    # Ryujinx-on-same-host still resolves via loopback regardless of this.
    initial_bridge_host = (
        os.environ.get("SMBW_SETUP_BRIDGE_HOST", "").strip()
        or lan_subnet_seed()
        or saved_state.get("bridge_host", "")
    )

    # Shared state between the UI and the worker thread.
    state: dict[str, Any] = {
        "running": False,
        "ok": False,
        "auto_install": True,
        "deploy_target": initial_target,
        "deploy_path": _default_deploy_path(initial_target),
        "bridge_host": initial_bridge_host,
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
                orientation="vertical", size_hint_y=None, height=180, spacing=4,
            )

            ai_row = BoxLayout(orientation="horizontal", size_hint_y=None, height=36)
            ai_cb = CheckBox(active=True, size_hint_x=None, width=40)
            def _ai_change(_inst, value):
                state["auto_install"] = bool(value)
            ai_cb.bind(active=_ai_change)
            ai_row.add_widget(ai_cb)
            ai_label = (
                "Auto-install missing prerequisites (winget + LLVM 19 + git submodules)"
                if sys.platform == "win32"
                else "Auto-install missing prerequisites (git submodules + pip deps; system tools shown for manual install)"
            )
            ai_row.add_widget(Label(
                text=ai_label,
                halign="left", valign="middle",
            ))
            opts.add_widget(ai_row)

            # Deploy target selector: dropdown + path field + Browse
            # side-by-side. The path field is grayed out unless the
            # target is "Custom folder" -- Ryujinx and SD show the
            # auto-detected path read-only so the user can see what
            # the deploy step will use, but accidental keystrokes
            # can't push them off the canonical path. Only "Custom"
            # unlocks both the text field and the Browse button.
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
            browse_btn = Button(text="Browse...", size_hint_x=None, width=110)

            def _apply_path_editability() -> None:
                """Sync the path field + Browse button enabled-state to
                the currently selected deploy target."""
                editable = state["deploy_target"] == "custom"
                self.deploy_path_input.disabled = not editable
                browse_btn.disabled = not editable

            def _deploy_target_change(_inst, value):
                key = _DEPLOY_KEYS.get(value, "none")
                state["deploy_target"] = key
                # Reset the path field to the new target's default
                # so switching Ryujinx -> SD doesn't leave a stale
                # path in the box. Custom preserves whatever the user
                # last typed / browsed to (via setup_state.json).
                new_default = _default_deploy_path(key)
                self.deploy_path_input.text = new_default
                state["deploy_path"] = new_default
                _apply_path_editability()
            spinner.bind(text=_deploy_target_change)
            self.deploy_path_input.bind(
                text=lambda _i, v: state.update(deploy_path=v))

            def _open_folder_picker(_btn) -> None:
                """Tk's askdirectory is the cleanest cross-platform folder
                picker -- Kivy's FileChooserListView is file-oriented and
                awkward for picking dirs. Tk is stdlib so no extra dep.
                Mirrors the smo_archipelago wizard pattern."""
                try:
                    import tkinter
                    import tkinter.filedialog
                    tkroot = tkinter.Tk()
                    tkroot.withdraw()
                    # Always-on-top so it isn't hidden behind the Kivy window.
                    tkroot.attributes("-topmost", True)
                    initial = self.deploy_path_input.text or ""
                    chosen = tkinter.filedialog.askdirectory(
                        title="Select custom deploy folder",
                        parent=tkroot,
                        initialdir=initial if initial else None,
                    )
                    tkroot.destroy()
                    if chosen:
                        self.deploy_path_input.text = chosen
                        state["deploy_path"] = chosen
                except Exception as e:
                    self._log(f"folder picker failed: {e!r}")
            browse_btn.bind(on_release=_open_folder_picker)

            _apply_path_editability()
            deploy_row.add_widget(spinner)
            deploy_row.add_widget(self.deploy_path_input)
            deploy_row.add_widget(browse_btn)
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

            # Bridge IP (subnet seed). Auto-filled with this PC's LAN IP so
            # the Switch sweeps the same /24 the client lives on. Edit it if
            # you'll play on a different network than the one you're setting
            # up from; clear it to fall back to the compiled-in default.
            bridge_row = BoxLayout(orientation="horizontal", size_hint_y=None, height=36, spacing=8)
            bridge_row.add_widget(Label(
                text="Bridge IP:", halign="left", valign="middle",
                size_hint_x=None, width=130,
            ))
            self.bridge_host_input = TextInput(
                text=state["bridge_host"],
                hint_text="auto-detected from this PC's LAN IP; any IP on your play /24",
                multiline=False,
                size_hint_x=1,
            )
            self.bridge_host_input.bind(
                text=lambda _i, v: state.update(bridge_host=v))
            bridge_row.add_widget(self.bridge_host_input)
            opts.add_widget(bridge_row)

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
            self._btn_row = BoxLayout(
                orientation="horizontal", size_hint_y=None, height=48, spacing=8,
            )
            self.run_btn = Button(text="Run setup")
            self.run_btn.bind(on_release=lambda _i: self._start_run())
            self.close_btn = Button(text="Close")
            self.close_btn.bind(on_release=lambda _i: App.get_running_app().stop())
            self._btn_row.add_widget(self.run_btn)
            self._btn_row.add_widget(self.close_btn)
            self._root = root
            root.add_widget(self._btn_row)

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

                # Bridge IP is optional. Blank → leave the compiled-in
                # default. A non-blank value is validated here so a typo
                # surfaces in the log rather than silently producing a
                # build that sweeps the wrong /24.
                bridge_host = (state.get("bridge_host") or "").strip()
                if bridge_host and not is_plausible_ipv4(bridge_host):
                    self._log(
                        f"[wizard] Bridge IP {bridge_host!r} is not a valid "
                        f"IPv4 address (e.g. 10.0.0.5); ignoring it and using "
                        f"the compiled-in default")
                    bridge_host = ""

                opts = PipelineOptions(
                    phases=ALL_PHASES,
                    install_missing=state["auto_install"],
                    deploy_target=target,
                    deploy_path=deploy_path,
                    bridge_host=bridge_host or None,
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
                    "bridge_host": bridge_host,
                    "last_ok": outcome.ok,
                })
            except Exception as e:  # pragma: no cover — defensive
                self._log(f"[wizard] crashed: {type(e).__name__}: {e}")
                log.exception("wizard worker crashed")
            finally:
                state["running"] = False

                def _reset_btn(_dt: float) -> None:
                    self.close_btn.disabled = False
                    if state["ok"]:
                        # Replace button row with a success banner + Close only.
                        self._btn_row.clear_widgets()
                        self._root.remove_widget(self._btn_row)
                        success_label = Label(
                            text="[size=18][b][color=44cc44]Installation successful.[/color][/b][/size]",
                            markup=True,
                            size_hint_y=None,
                            height=48,
                        )
                        close_only_row = BoxLayout(
                            orientation="horizontal", size_hint_y=None, height=48, spacing=8,
                        )
                        close_btn2 = Button(text="Close")
                        close_btn2.bind(on_release=lambda _i: App.get_running_app().stop())
                        close_only_row.add_widget(close_btn2)
                        self._root.add_widget(success_label)
                        self._root.add_widget(close_only_row)
                    else:
                        self.run_btn.disabled = False
                        self.run_btn.text = "Run setup again"
                Clock.schedule_once(_reset_btn, 0)

    WizardApp().run()
    _append_wizard_log(f"=== wizard end (ok={state['ok']}) ===")
    return bool(state["ok"])
