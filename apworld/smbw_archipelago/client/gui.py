"""Kivy UI for the SMBW Client.

THIS MODULE PULLS KIVY.  Never import it from anywhere that runs at
apworld load time -- generation hosts may not have a display server.
Only ``SMBWContext.make_gui()`` reaches it, and that's only called from
``CommonContext.run_gui()`` inside the Launcher subprocess.

Subclasses CommonClient's GameManager, which provides:
  - top bar: server-address input + Connect button + thin progress bar
  - log tab: "Archipelago" (AP/Client-side logger output)
  - "Hints" tab (built-in)
  - bottom bar: command prompt for slash-commands

We add ONE custom tab ("SMBW") split 50/50 horizontally:
  * left  -- at-a-glance bridge state (Switch connection, current
             course, emitted checks count, deaths, badge mask, recent
             items received)
  * right -- UILog tailing logger ``"SMBW"`` (lan_server, processor,
             context all log under that name)

Why a single combined tab instead of two:
  Mirrors smo_archipelago's Odyssey tab.  Keeps state and diagnostics
  in one eye-line while debugging, and -- because the SMBW logger is
  NOT listed in ``logging_pairs`` -- kvui doesn't synthesize an "All"
  tab that would otherwise interleave verbose bridge logs with the
  AP/Client output.

Polling: 1.5 s for the status panel.  Human-speed state changes
(course-clear, badge grant) read fine at that cadence and leave plenty
of frame budget for Kivy.
"""
from __future__ import annotations

import logging
import typing

# kvui MUST be imported before any kivy.* module -- kvui asserts
# `"kivy" not in sys.modules` at module top for frozen-build compat.
from kvui import GameManager, UILog  # type: ignore

from kivy.clock import Clock  # type: ignore
from kivy.uix.boxlayout import BoxLayout  # type: ignore
from kivy.uix.label import Label  # type: ignore
from kivy.uix.scrollview import ScrollView  # type: ignore

if typing.TYPE_CHECKING:  # pragma: no cover
    from .context import SMBWContext


_REFRESH_INTERVAL_SEC = 1.5


class SMBWManager(GameManager):
    """Custom GameManager: one "SMBW" tab combining status + log tail."""

    base_title = "Archipelago SMBW Client"

    # Single entry on purpose.  kvui's GameManager.build() only
    # synthesizes the leftmost "All" tab when ``len(logging_pairs) > 1``;
    # keeping just the Client/Archipelago pair here suppresses that tab
    # so SMBW-logger output stays out of the AP-protocol log view.  The
    # "SMBW" logger is rendered manually in the right half of the SMBW
    # tab via UILog (see ``build``).
    logging_pairs = [
        ("Client", "Archipelago"),
    ]

    ctx: "SMBWContext"

    def __init__(self, ctx: "SMBWContext"):
        super().__init__(ctx)
        self._smbw_log: UILog | None = None

    def build(self):
        container = super().build()
        # SMBW tab: horizontal 50/50 split.
        #   Left  -- at-a-glance bridge state (Switch / AP connection,
        #            course, checks, deaths, badge mask, recent items).
        #   Right -- UILog tailing logger "SMBW".  Constructing UILog
        #            attaches a LogtoUI handler to the passed logger, so
        #            records auto-tail here without needing logging_pairs
        #            (which would also pull them into a synthesized "All"
        #            tab next to the Client/Archipelago output).
        smbw_split = BoxLayout(orientation="horizontal", spacing=4)

        status_panel = self._build_status_panel()
        status_panel.size_hint_x = 0.5
        smbw_split.add_widget(status_panel)

        self._smbw_log = UILog(logging.getLogger("SMBW"))
        self._smbw_log.size_hint_x = 0.5
        smbw_split.add_widget(self._smbw_log)

        self.add_client_tab("SMBW", smbw_split)
        Clock.schedule_interval(self._refresh_status, _REFRESH_INTERVAL_SEC)
        return container

    # ---- Status tab construction --------------------------------------

    def _build_status_panel(self):
        """Vertical box of labels updated by `_refresh_status`."""
        outer = BoxLayout(orientation="vertical", padding=8, spacing=6)
        scroll = ScrollView(do_scroll_x=False, do_scroll_y=True)
        inner = BoxLayout(orientation="vertical", size_hint_y=None, spacing=4)
        inner.bind(minimum_height=inner.setter("height"))

        self._lbl_switch = self._mk_label(inner, "Switch: ?")
        self._lbl_ap = self._mk_label(inner, "AP: ?")
        self._lbl_course = self._mk_label(inner, "Course: (none)")
        self._lbl_checks = self._mk_label(inner, "Emitted checks: 0")
        self._lbl_deaths = self._mk_label(inner, "Deaths: 0")
        self._lbl_goal = self._mk_label(inner, "Goal: not yet")
        self._lbl_badges = self._mk_label(inner, "Badge mask: 0x0")
        self._lbl_items = self._mk_label(
            inner, "Items received: 0",
            multiline=True,
        )

        scroll.add_widget(inner)
        outer.add_widget(scroll)
        return outer

    def _mk_label(self, parent, text: str, *, multiline: bool = False):
        lbl = Label(
            text=text,
            size_hint_y=None,
            height=24 if not multiline else 200,
            text_size=(None, None),
            halign="left",
            valign="top",
            markup=True,
        )
        # Bind text_size to width so wrapping kicks in on resize.
        def _resize(_inst, w):
            lbl.text_size = (w, None)
        parent.bind(width=_resize)
        parent.add_widget(lbl)
        return lbl

    # ---- Periodic refresh ---------------------------------------------

    def _refresh_status(self, _dt: float) -> None:
        ctx = self.ctx
        state = getattr(ctx, "bridge_state", None)
        lan = getattr(ctx, "lan_server", None)

        switch_connected = bool(getattr(lan, "_client_writer", None)) if lan else False
        self._lbl_switch.text = (
            "Switch: [color=00ff00]connected[/color]"
            if switch_connected
            else "Switch: [color=ff8080]disconnected[/color]"
        )

        ap_state = "connected" if ctx.server and not ctx.server.socket.closed else "disconnected" \
            if ctx.server else "disconnected"
        self._lbl_ap.text = (
            f"AP: {ap_state}  "
            f"slot=[b]{ctx.auth or '?'}[/b]  "
            f"seed={ctx.seed_name or '?'}"
        )

        if state and state.current_course:
            c = state.current_course
            self._lbl_course.text = (
                f"Course: W{c.world_no}-{c.course_no} "
                f"(stage_key={c.stage_key})"
            )
        else:
            self._lbl_course.text = "Course: (not in a course)"

        emitted = state.count_emitted() if state else 0
        deaths = state.death_count if state else 0
        goal_complete = bool(state.goal_complete) if state else False
        self._lbl_checks.text = f"Emitted checks: [b]{emitted}[/b]"
        self._lbl_deaths.text = f"Deaths: {deaths}"
        self._lbl_goal.text = (
            "Goal: [color=00ff00]complete (final Bowser defeated)[/color]"
            if goal_complete
            else "Goal: not yet"
        )

        try:
            mask = ctx._recompute_badge_mask()
        except Exception:
            mask = 0
        self._lbl_badges.text = f"Badge mask: [b]0x{mask:x}[/b]"

        items = getattr(ctx, "items_received", None) or []
        item_lines = [f"Items received: [b]{len(items)}[/b]"]
        for it in items[-12:]:
            try:
                item_id = getattr(it, "item", None) or (
                    it.get("item") if isinstance(it, dict) else None
                )
                name = ctx.item_names.lookup_in_game(int(item_id)) if item_id else "?"
                item_lines.append(f"  - {name}")
            except Exception:
                continue
        self._lbl_items.text = "\n".join(item_lines)
