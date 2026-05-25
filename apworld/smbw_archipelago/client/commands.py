"""Custom slash-commands for the SMBW Client.

Subclass of ClientCommandProcessor so users can type ``/smbw_status``,
``/replay_badges`` etc. in the Kivy command bar (or stdin when running
--nogui).  Each handler returns ``True`` to suppress the default
"unknown command" message.
"""
from __future__ import annotations

import json
import logging
import os
import time

from CommonClient import ClientCommandProcessor  # type: ignore


log = logging.getLogger("SMBW")


# Append-only log of confirmed (bit, name) mappings the user records
# via ``/badge_record``.  Default path is the cwd of the running
# client so the user can find it next to the AP log; can be overridden
# via the SMBW_BADGE_PROBE_LOG env var if running multiple sessions
# from one cwd.
BADGE_PROBE_LOG_PATH = os.environ.get(
    "SMBW_BADGE_PROBE_LOG",
    os.path.join(os.getcwd(), "badge_probe_log.jsonl"))


def _parse_bit_spec(spec: str) -> set[int] | None:
    """Parse a bit-or-range spec like ``7``, ``1-5``, ``1,2,3`` (or
    ``1-5,7,10-12``) into a set of bit positions.  Returns ``None`` if
    the spec is malformed.  Used by `/badge_probe_invalid` and
    `/badge_probe_range`.

    Rejects bits outside [0, 64) and ranges where lo > hi.  Range
    bounds are INCLUSIVE on both ends (``1-5`` = {1,2,3,4,5}) because
    that matches how the user thinks about "bits 1 through 5", even
    though the underlying ``next_unmapped_bit`` ceiling is exclusive.
    """
    out: set[int] = set()
    for part in spec.replace(" ", "").split(","):
        if not part:
            continue
        if "-" in part:
            try:
                lo_s, hi_s = part.split("-", 1)
                lo, hi = int(lo_s, 0), int(hi_s, 0)
            except ValueError:
                return None
            if lo > hi or lo < 0 or hi >= 64:
                return None
            out.update(range(lo, hi + 1))
        else:
            try:
                b = int(part, 0)
            except ValueError:
                return None
            if not (0 <= b < 64):
                return None
            out.add(b)
    return out


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

    # ---- M2.3 badge probe-and-discover commands ----------------------

    def _cmd_badge_probe(self, bit: str = "") -> bool:
        """Set the badge bitfield to a single bit so you can see which
        AP badge that internal_id corresponds to.  Usage:
        ``/badge_probe <bit>`` (decimal or 0xHEX, 0..63).

        Overrides the AP-derived badge mask: the 2 s sync tick keeps
        re-pushing this bit until you clear it with /badge_probe_clear
        or send a different probe.  Note: the badge inventory UI caches
        on scene-enter -- after each /badge_probe you must leave and
        re-enter the badge menu to see the new badge.
        """
        ctx = self.ctx
        if not bit:
            self.output("usage: /badge_probe <bit>  (0..63, decimal or 0xHEX)")
            return True
        try:
            b = int(bit, 0)
        except ValueError:
            self.output(f"ERROR: can't parse bit {bit!r} as integer")
            return True
        if not (0 <= b < 64):
            self.output(f"ERROR: bit {b} out of range [0, 64)")
            return True
        if not hasattr(ctx, "set_badge_probe_mask"):
            self.output("ERROR: context is not an SMBWContext (no probe support)")
            return True
        ctx.set_badge_probe_mask(1 << b)
        self.output(
            f"-> probe bit {b} (mask=0x{1 << b:016x}); "
            f"open badge menu in-game to see which badge it is")
        return True

    def _cmd_badge_probe_mask(self, mask: str = "") -> bool:
        """Set the badge bitfield to an arbitrary multi-bit mask.
        Usage: ``/badge_probe_mask <hex>``.  Useful for sanity-checking
        a candidate set or for confirming a guess at multiple
        positions at once."""
        ctx = self.ctx
        if not mask:
            self.output("usage: /badge_probe_mask <hex>  (e.g. 0x10 or 16)")
            return True
        try:
            m = int(mask, 0)
        except ValueError:
            self.output(f"ERROR: can't parse mask {mask!r} as integer")
            return True
        if not (0 <= m < (1 << 64)):
            self.output(f"ERROR: mask 0x{m:x} out of range [0, 2**64)")
            return True
        if not hasattr(ctx, "set_badge_probe_mask"):
            self.output("ERROR: context is not an SMBWContext (no probe support)")
            return True
        ctx.set_badge_probe_mask(m)
        self.output(f"-> probe mask 0x{m:016x}")
        return True

    def _cmd_badge_probe_range(self, spec: str = "") -> bool:
        """Probe a range or set of bits at once.  The SMBW badge bitfield
        is sparse: ~24 valid bits scattered across [0, 64).  Probing a
        gap range and seeing zero new badges lets you eliminate many
        dead bits with one command.  Usage:
        ``/badge_probe_range 1-7``          # bits 1..7 inclusive
        ``/badge_probe_range 29-45``        # the next big gap
        ``/badge_probe_range 1,3,5,7``      # arbitrary set

        Suggested workflow:
        1. Run /badge_probe_range over a gap.
        2. Open the badge inventory in-game (leave + re-enter the menu
           to defeat the UI cache).
        3. If you see N new badges, narrow with a smaller range to
           identify each.  If you see 0, run /badge_probe_invalid with
           the SAME spec to mark all those bits dead and let
           /badge_probe_next skip them.
        """
        ctx = self.ctx
        if not spec:
            self.output(
                "usage: /badge_probe_range <spec>\n"
                "  spec: bit (7), range (1-7), or list (1,3,5,7)")
            return True
        if not hasattr(ctx, "set_badge_probe_mask"):
            self.output("ERROR: context is not an SMBWContext (no probe support)")
            return True
        bits = _parse_bit_spec(spec)
        if bits is None:
            self.output(f"ERROR: can't parse spec {spec!r}")
            return True
        if not bits:
            self.output("ERROR: empty spec")
            return True
        mask = 0
        for b in bits:
            mask |= (1 << b)
        ctx.set_badge_probe_mask(mask)
        from . import badge_table
        all_count = len(badge_table.all_badge_item_names())
        self.output(
            f"-> probe {len(bits)} bit(s): {sorted(bits)}  "
            f"(mask=0x{mask:016x})\n"
            f"   check badge inventory; if 0 new badges, run "
            f"`/badge_probe_invalid {spec}` to skip them all.\n"
            f"   currently {len(badge_table.mapped_bits())}/{all_count} "
            f"badges mapped.")
        return True

    def _cmd_badge_probe_invalid(self, spec: str = "") -> bool:
        """Mark bit(s) as known-invalid (no badge corresponds to them).
        ``/badge_probe_next`` will skip them.  Use after a
        ``/badge_probe_range`` returned zero badges.  Usage:
        ``/badge_probe_invalid 1-7``
        ``/badge_probe_invalid 20,21,22``
        Runtime-only -- not persisted across client restarts.  Use
        ``/badge_status`` to dump the current set; if you want it
        durable, paste into ``badge_table._BADGES`` or note offline.
        """
        ctx = self.ctx
        if not spec:
            self.output("usage: /badge_probe_invalid <spec>  (e.g. 1-7 or 1,3,5)")
            return True
        if not hasattr(ctx, "_badge_probe_invalid"):
            self.output("ERROR: context is not an SMBWContext (no probe support)")
            return True
        bits = _parse_bit_spec(spec)
        if bits is None:
            self.output(f"ERROR: can't parse spec {spec!r}")
            return True
        if not bits:
            self.output("ERROR: empty spec")
            return True
        from . import badge_table
        # Refuse to mark a bit invalid that's already in _BADGES --
        # that would be a definite contradiction, not just user noise.
        conflicts = bits & badge_table.mapped_bits()
        if conflicts:
            self.output(
                f"ERROR: refusing to mark mapped bits invalid: "
                f"{sorted(conflicts)} are in _BADGES")
            return True
        before = len(ctx._badge_probe_invalid)
        ctx._badge_probe_invalid.update(bits)
        added = len(ctx._badge_probe_invalid) - before
        self.output(
            f"-> marked {added} new bit(s) invalid; total invalid: "
            f"{len(ctx._badge_probe_invalid)}")
        return True

    def _cmd_badge_probe_clear_invalid(self) -> bool:
        """Wipe the runtime known-invalid set.  Use if you've mis-flagged
        bits or want to re-verify."""
        ctx = self.ctx
        if not hasattr(ctx, "_badge_probe_invalid"):
            self.output("ERROR: context is not an SMBWContext")
            return True
        n = len(ctx._badge_probe_invalid)
        ctx._badge_probe_invalid.clear()
        self.output(f"-> cleared {n} invalid bit(s)")
        return True

    def _cmd_badge_probe_clear(self) -> bool:
        """Clear the badge probe override and restore the AP-derived
        badge mask.  The next tick pushes whatever items_received says
        the player owns."""
        ctx = self.ctx
        if not hasattr(ctx, "set_badge_probe_mask"):
            self.output("ERROR: context is not an SMBWContext (no probe support)")
            return True
        ctx.set_badge_probe_mask(None)
        ap_mask = ctx._recompute_badge_mask() if hasattr(ctx, "_recompute_badge_mask") else 0
        self.output(f"-> probe cleared; AP-derived mask=0x{ap_mask:016x}")
        return True

    def _cmd_badge_probe_next(self) -> bool:
        """Probe the next unmapped bit position.  Starts after the
        currently-probed bit (or from 0 if no probe is active) and
        skips bits already in badge_table._BADGES OR in the runtime
        known-invalid set (`/badge_probe_invalid`)."""
        ctx = self.ctx
        if not hasattr(ctx, "set_badge_probe_mask"):
            self.output("ERROR: context is not an SMBWContext (no probe support)")
            return True
        from . import badge_table
        current = ctx._badge_probe_mask if ctx._badge_probe_mask is not None else 0
        # If exactly one bit is set in current, advance from that bit.
        # Otherwise start at -1 to consider bit 0.
        after = -1
        if current and (current & (current - 1)) == 0:
            after = current.bit_length() - 1
        invalid = getattr(ctx, "_badge_probe_invalid", set())
        nxt = badge_table.next_unmapped_bit(after=after, extra_skip=invalid)
        if nxt is None:
            self.output(
                f"no unmapped bits remain in [{after + 1}, 64) -- "
                f"all bits 0..63 either mapped, skipped, or invalid")
            return True
        ctx.set_badge_probe_mask(1 << nxt)
        unmapped = badge_table.unmapped_item_names()
        self.output(
            f"-> probe bit {nxt} (mask=0x{1 << nxt:016x})\n"
            f"   {len(badge_table.mapped_bits())} of "
            f"{len(badge_table.all_badge_item_names())} badges mapped; "
            f"{len(unmapped)} item names still need a bit; "
            f"{len(invalid)} bits marked invalid")
        return True

    def _cmd_badge_record(self, bit: str = "", *name_parts: str) -> bool:
        """Record a confirmed (bit, badge_name) mapping.  Appends to
        ``badge_probe_log.jsonl`` in cwd (or $SMBW_BADGE_PROBE_LOG)
        AND prints the Python tuple to paste into
        ``badge_table._BADGES``.  Usage:
        ``/badge_record <bit> <Badge Name>``  (e.g. ``/badge_record 7 Jet Run Badge``)
        """
        if not bit or not name_parts:
            self.output(
                "usage: /badge_record <bit> <Badge Name>\n"
                "       e.g. /badge_record 7 Jet Run Badge")
            return True
        try:
            b = int(bit, 0)
        except ValueError:
            self.output(f"ERROR: can't parse bit {bit!r} as integer")
            return True
        name = " ".join(name_parts).strip()
        # Best-effort sanity check against items.json.  Warn but don't
        # block -- the user may be probing an item that the apworld
        # doesn't ship yet, or there may be a typo we want surfaced.
        from . import badge_table
        all_names = badge_table.all_badge_item_names()
        if all_names and name not in all_names:
            self.output(
                f"WARNING: {name!r} not in items.json badge list -- "
                f"typo? (continuing anyway)")
        if b in badge_table.mapped_bits():
            existing = badge_table.name_for_internal_id(b)
            self.output(
                f"WARNING: bit {b} is already mapped to {existing!r} in "
                f"_BADGES; new record will SHADOW after merge if name "
                f"differs (you can also just edit _BADGES directly).")
        entry = {
            "bit": b,
            "name": name,
            "ts": int(time.time()),
        }
        try:
            with open(BADGE_PROBE_LOG_PATH, "a", encoding="utf-8") as f:
                f.write(json.dumps(entry) + "\n")
            self.output(f"-> appended to {BADGE_PROBE_LOG_PATH}")
        except OSError as e:
            self.output(f"WARNING: couldn't append to {BADGE_PROBE_LOG_PATH}: {e}")
        # Print the line to paste into _BADGES.
        self.output(f'    ("{name}", {b}, "probe"),')
        return True

    def _cmd_badge_status(self) -> bool:
        """Show badge mapping progress + current probe state."""
        ctx = self.ctx
        from . import badge_table
        all_names = badge_table.all_badge_item_names()
        unmapped_names = badge_table.unmapped_item_names()
        mapped = badge_table.mapped_bits()
        invalid = getattr(ctx, "_badge_probe_invalid", set())
        probe = getattr(ctx, "_badge_probe_mask", None)
        ap_mask = ctx._recompute_badge_mask() if hasattr(ctx, "_recompute_badge_mask") else 0
        self.output(
            f"badges mapped: {len(mapped)}/{len(all_names)} "
            f"(bits: {sorted(mapped)})")
        if unmapped_names:
            self.output(f"item names still unmapped ({len(unmapped_names)}):")
            for n in unmapped_names:
                self.output(f"  - {n}")
        if invalid:
            self.output(
                f"known-invalid bits ({len(invalid)}): "
                f"{sorted(invalid)}")
        nxt = badge_table.next_unmapped_bit(after=-1, extra_skip=invalid)
        self.output(f"next unprobed bit: {nxt if nxt is not None else 'none'}")
        if probe is None:
            self.output("probe: OFF  (using AP-derived mask)")
        else:
            self.output(f"probe: ON   mask=0x{probe:016x}")
        self.output(f"AP-derived mask (would-be active if probe cleared): 0x{ap_mask:016x}")
        self.output(f"probe log path: {BADGE_PROBE_LOG_PATH}")
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
