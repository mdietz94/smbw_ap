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
             course, emitted checks count, deaths, per-world Wonder/
             Royal Seed table, owned badges)
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
import os.path
import typing

# kvui MUST be imported before any kivy.* module -- kvui asserts
# `"kivy" not in sys.modules` at module top for frozen-build compat.
from kvui import GameManager, UILog  # type: ignore

from kivy import kivy_data_dir  # type: ignore
from kivy.clock import Clock  # type: ignore
from kivy.core.text import LabelBase  # type: ignore
from kivy.metrics import dp  # type: ignore
from kivy.uix.boxlayout import BoxLayout  # type: ignore
from kivy.uix.label import Label  # type: ignore
from kivy.uix.scrollview import ScrollView  # type: ignore

from . import badge_table, royal_seed_table, wonder_seed_table

if typing.TYPE_CHECKING:  # pragma: no cover
    from .context import SMBWContext


_REFRESH_INTERVAL_SEC = 1.5


# Per-world labels for the at-a-glance Wonder/Royal Seed table.  Indexed
# by ``wonder_seed_table`` bucket (0..7).  Royal Seeds only exist for
# W1..W6 (bucket 0..5); Petal Isles and Special World rows show a
# placeholder dash in the Royal column.
_WORLD_LABELS = (
    "W1 Pipe-Rock Plateau",
    "W2 Fluff-Puff Peaks",
    "W3 Shining Falls",
    "W4 Sunbaked Desert",
    "W5 Fungi Mines",
    "W6 Deep Magma Bog",
    "Petal Isles",
    "Special World",
)


# Register Kivy's bundled monospace font under a short alias so the
# per-world seed table can use [font=RobotoMono] markup to keep columns
# lined up under Kivy's proportional default font.  Mirrors the same
# trick smo_archipelago uses for its per-kingdom moons table -- see
# that module for the long-form rationale on why resource_find fails
# at module-import time.  _MONO_OK gates the [font=...] markup so a
# custom Kivy build that strips the bundled fonts degrades to the
# proportional default instead of crashing.
_MONO_FONT_PATH = os.path.join(kivy_data_dir, "fonts", "RobotoMono-Regular.ttf")
_MONO_OK = os.path.isfile(_MONO_FONT_PATH)
if _MONO_OK:
    LabelBase.register(name="RobotoMono", fn_regular=_MONO_FONT_PATH)


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
        """Vertical box of labels updated by `_refresh_status`.

        Header lines (Switch / AP / course / checks / deaths / goal)
        are short single-line labels; the per-world seed table and the
        owned-badges list are multi-line labels rendered with
        [font=RobotoMono] markup so their columns line up.
        """
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
        self._lbl_openworld = self._mk_label(inner, "", multiline=True)
        self._lbl_seeds = self._mk_label(inner, "", multiline=True)
        self._lbl_badges = self._mk_label(inner, "", multiline=True)

        scroll.add_widget(inner)
        outer.add_widget(scroll)
        return outer

    def _mk_label(self, parent, text: str, *, multiline: bool = False):
        lbl = Label(
            text=text,
            size_hint_y=None,
            text_size=(None, None),
            halign="left",
            valign="top",
            markup=True,
        )
        if multiline:
            # Grow height with content so the seed table and badge list
            # never clip; the ScrollView above scrolls the union of all
            # rows once their combined height exceeds the viewport.
            lbl.bind(texture_size=lambda inst, sz: setattr(
                inst, "height", sz[1] + dp(4)))
        else:
            lbl.height = dp(24)
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
            f"slot=[b]{ctx.auth or '?'}[/b]"
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

        if getattr(ctx, "open_world", False):
            active = getattr(ctx, "open_world_active", []) or []
            worlds = ", ".join(f"W{n}" for n in active) if active else "(none)"
            pr = int(getattr(ctx, "palaces_required", 0) or 0)
            self._lbl_openworld.text = (
                f"[b]Open-world[/b] - play: [color=80ff80]{worlds}[/color]\n"
                f"[i]walk in from Petal Isles[/i]  "
                f"({pr} palace{'s' if pr != 1 else ''} -> Bowser)"
            )
        else:
            self._lbl_openworld.text = ""

        self._lbl_seeds.text = _format_seed_table(ctx)
        self._lbl_badges.text = _format_badge_list(ctx)


def _format_seed_table(ctx: "SMBWContext") -> str:
    """Per-world Wonder Seed + Royal Seed table.

    Layout mirrors smo_archipelago's per-kingdom moons table: name
    column left-justified to the widest world label, count column
    right-justified, Royal Seed column shows ``[Y]`` / ``[ ]`` / ``-``
    (the last for Petal Isles / Special which have no Royal Seed).
    """
    try:
        seeds = ctx._recompute_wonder_seed_counts()
    except Exception:
        seeds = [0] * wonder_seed_table.WORLD_COUNT
    try:
        royal_mask = ctx._recompute_royal_seed_mask()
    except Exception:
        royal_mask = 0

    name_w = max(len(n) for n in _WORLD_LABELS)
    count_w = max(len(str(c)) for c in seeds) if seeds else 1

    rows: list[str] = []
    for i, name in enumerate(_WORLD_LABELS):
        count = seeds[i] if i < len(seeds) else 0
        if i < royal_seed_table.WORLD_COUNT:
            owned = bool(royal_mask & (1 << i))
            royal = "[Y]" if owned else "[ ]"
        else:
            royal = " - "
        label = f"{name}:".ljust(name_w + 1)
        rows.append(f"  {label} {count:>{count_w}}   {royal}")
    table = "\n".join(rows)
    header = "[b]Seeds by world[/b]    [i]Wonder / Royal[/i]"
    body = f"[font=RobotoMono]{table}[/font]" if _MONO_OK else table
    return f"{header}\n{body}"


def _format_badge_list(ctx: "SMBWContext") -> str:
    """Owned-badge list, sorted alphabetically by AP item name.

    Reads the bridge's canonical badge mask (same as what's pushed to
    the Switch) and reverse-looks-up each set bit via
    ``badge_table.name_for_internal_id``.  Bits without a known
    mapping are shown as ``bit N`` so probe-mode results stay visible.
    """
    try:
        mask = ctx._recompute_badge_mask()
    except Exception:
        mask = 0
    if mask == 0:
        return "[b]Badges owned[/b]\n  [i](none yet)[/i]"
    names: list[str] = []
    for bit in range(64):
        if not (mask & (1 << bit)):
            continue
        name = badge_table.name_for_internal_id(bit)
        names.append(name if name is not None else f"bit {bit}")
    names.sort()
    body = "\n".join(f"  - {n}" for n in names)
    return f"[b]Badges owned[/b] ([b]{len(names)}[/b])\n{body}"
