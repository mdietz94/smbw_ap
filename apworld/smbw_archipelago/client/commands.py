"""Custom slash-commands for the SMBW Client.

Subclass of ClientCommandProcessor so users can type ``/smbw_status``,
``/replay_badges`` etc. in the Kivy command bar (or stdin when running
--nogui).  Each handler returns ``True`` to suppress the default
"unknown command" message.
"""
from __future__ import annotations

import logging

from CommonClient import ClientCommandProcessor  # type: ignore


log = logging.getLogger("SMBW")


class SMBWCommandProcessor(ClientCommandProcessor):
    """Adds SMBW-specific commands to the standard AP /commands."""

    def _cmd_smbw_status(self) -> bool:
        """Print bridge + Switch + AP connection summary."""
        ctx = self.ctx
        state = getattr(ctx, "bridge_state", None)
        lan = getattr(ctx, "lan_server", None)
        switch_connected = bool(getattr(lan, "_client_writer", None)) if lan else False
        course = state.current_course if state else None
        course_desc = (
            f"world={course.world_no} course={course.course_no} "
            f"stage_key={course.stage_key}"
        ) if course else "(none)"
        emitted = state.count_emitted() if state else 0
        deaths = state.death_count if state else 0
        goal_complete = bool(state.goal_complete) if state else False
        mask = ctx._recompute_badge_mask() if hasattr(ctx, "_recompute_badge_mask") else 0
        self.output(f"AP slot: {ctx.auth or '(unset)'}  seed: {ctx.seed_name or '(unset)'}")
        self.output(f"Switch:  {'connected' if switch_connected else 'disconnected'}")
        self.output(f"Course:  {course_desc}")
        self.output(f"Emitted checks: {emitted}    Deaths: {deaths}")
        self.output(
            "Goal:    "
            + ("COMPLETE (final Bowser defeated)" if goal_complete else "not yet"))
        self.output(f"Badge mask: 0x{mask:x}")
        return True

    def _cmd_replay_badges(self) -> bool:
        """Force a SetBadgesAbsolute push to the Switch with the current
        AP-known badge mask.  Useful after a Switch reconnect to verify
        the sync without waiting for the 2 s tick."""
        ctx = self.ctx
        lan = getattr(ctx, "lan_server", None)
        if lan is None or not hasattr(ctx, "_recompute_badge_mask"):
            self.output("ERROR: lan_server not wired")
            return True
        mask = ctx._recompute_badge_mask()
        lan.send_set_badges_absolute(mask)
        self.output(f"-> set_badges_absolute bits=0x{mask:x}")
        return True

    def _cmd_replay_seeds(self) -> bool:
        """Replay every Royal Seed grant to the Switch from the current
        items_received set.  Useful for testing after a save-reload."""
        ctx = self.ctx
        lan = getattr(ctx, "lan_server", None)
        if lan is None:
            self.output("ERROR: lan_server not wired")
            return True
        from . import royal_seed_table
        sent = 0
        for it in ctx.items_received:
            item_id = getattr(it, "item", None)
            if item_id is None and isinstance(it, dict):
                item_id = it.get("item")
            if item_id is None:
                continue
            try:
                item_name = ctx.item_names.lookup_in_game(int(item_id))
            except Exception:
                continue
            if not royal_seed_table.is_royal_seed_item(item_name):
                continue
            h = royal_seed_table.hash_for_item(item_name)
            if h is None:
                continue
            lan.send_grant_hash_keyed(h, royal_seed_table.ROYAL_SEED_VALUE)
            sent += 1
        self.output(f"-> replayed {sent} Royal Seed grants")
        return True
