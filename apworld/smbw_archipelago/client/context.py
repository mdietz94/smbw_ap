"""SMBWContext: CommonContext subclass for the SMBW Archipelago client.

Ported from the headless `bridge/ap_client.py`.  Key changes:
  - game name updated to first-class "SMBWonder" (no longer Manual_-prefixed).
  - `make_gui()` returns the Kivy `SMBWManager` subclass so Archipelago
    Launcher spawns a Kivy window with our custom SMBW tab (status on
    the left, log tail on the right).
  - All "SMBW" log lines (lan_server, processor, this module) route
    through the `SMBW` logger, which `SMBWManager` tails in the right
    half of the SMBW tab.

Inbound (AP -> Switch):
  - Badges: maintain a canonical badge mask derived from
    :attr:`items_received`.  On every ``ReceivedItems``, recompute the
    mask and push it to the Switch as a ``SetBadgesAbsolute`` (the LAN
    server also re-pushes on HelloMsg and on a ~2 s tick).
  - Royal Seeds: same idempotent absolute-overwrite pattern -- a 6-bit
    mask of which seeds AP has granted, pushed as
    ``SetRoyalSeedsAbsolute`` on every ReceivedItems + HelloMsg +
    periodic tick.  The Switch loops the 6 container-B bool hashes and
    grants/clears each per the mask, so an in-game palace clear that
    runs ahead of AP gets reverted within seconds.
  - Items neither table recognizes are logged + dropped.

Outbound (Switch -> AP):
  - The LanServer calls :meth:`SMBWContext.handle_check_emitted` for
    every CheckEmitted the processor produces.  We resolve to an AP
    location ID via :mod:`location_table` + the connected
    ``location_names`` and ``send_msgs([{"cmd": "LocationChecks", ...}])``.
"""
from __future__ import annotations

import asyncio
import logging
import math
from typing import Any

from CommonClient import CommonContext  # type: ignore
from NetUtils import ClientStatus  # type: ignore

from . import badge_table
from . import coin_table
from . import powerup_table
from . import royal_seed_table
from . import wonder_seed_table
from . import world_unlock_table
from .commands import SMBWCommandProcessor
from .location_table import (
    lookup_name,
    pr_world_no_to_ap_world,
    shop_badge_location_names,
)
from .protocol import (
    CheckEmitted,
    CheckKind,
    DeathReported,
    GateEntered,
    GateKind,
    GoalCompleted,
)
from .state import BridgeState


log = logging.getLogger("SMBW")


# Must match the new SMBWonderWorld's `game` slug (see
# apworld/smbw_archipelago/Game.py).
GAME_NAME = "Super Mario Bros Wonder"


# The single goal-location name (from apworld locations.json) whose
# completion is signalled by the M3.7 Switch-side Nerve trampoline at
# NSO+0x15b77a8 (SetFlagEnd...AfterClearedLastBoss).  Used to gate
# ``handle_goal_completed`` so the hook only marks AP done when the
# player actually picked the Bowser goal -- otherwise the Bowser kill
# is just a milestone, not victory.  The two other victory locations
# (Special: Wonder Gauntlet - Normal Exit, Special: WONDER? - Normal
# Exit) reach CLIENT_GOAL via ``handle_check_emitted`` matching the
# goal name on a normal LocationCheck.
GOAL_LOCATION_NAME_BOWSER = "BC: Bowser's Rage Stage - Royal Seed"


# Open-world: bit position of the Castle/Bowser route in the routable-
# world mask (see wire.SetRoutableWorldsAbsoluteMsg.CASTLE_BIT and the
# Switch-side kCastleMaskBit).  Worlds W1..W6 occupy bits 0..5.
ROUTABLE_CASTLE_BIT = 8

# Open-world: Petal Isles is AP bucket 6 (= mask bit 6).  We make PI always
# routable so the player can fast-travel to the hub and walk into each open
# world's entrance on foot -- a *normal* (on-foot) first entry plays the
# world's FirstVisitDemo, which moves the Poplins and removes the route
# obstacle (fast-travelling straight into a world skips that demo, leaving
# the obstacle in place; see docs/re-world-intro-demo-2026-06-05.md).
ROUTABLE_PETAL_BIT = 6

# Open-world: World 1 is AP bucket 0 (= mask bit 0).  Unlike W2..W6, W1 is
# NOT walk-connected to Petal Isles, so the walk-in model can never reach it
# -- fast-travel (selecting W1 in the world-travel UI and teleporting to its
# first course) is the ONLY way in.  When W1 is active we therefore set its
# routable bit so the Switch hooks (worldRoutable/worldTravel/courseVisible +
# applyOpenWorldEntry's first-course fill, all keyed on bit 0 == W1) surface
# W1 as a fast-travel destination landing on 1-1.  Done only when W1 is in
# the active set -- W1 is never travelable when it is not active.
ROUTABLE_W1_BIT = 0
# AP world number for World 1 (open_world_active uses 1-based world numbers).
OPEN_WORLD_W1 = 1


# Level-entry gate (kill the player if they sequence-broke into a course
# AP logic gates).  The Switch can't physically block course entry, so
# the bridge waits ``GATE_KILL_DELAY_S`` after entry -- enough grace for
# the player to sit through any unskippable entry dialogue, then pause
# and walk back out -- then drives the DeathLink ``synthKill`` route,
# re-firing every ``GATE_KILL_DELAY_S`` while the player is still inside
# the gated course without the required item.  (10s proved too short:
# some courses open with dialogue the player can't leave from, so the
# kill landed before they ever had control.)
GATE_KILL_DELAY_S = 30.0

# Safety cap on consecutive gate kills for a single course entry.  The
# loop normally stops the moment the player leaves (in_course clears) or
# acquires the item; this cap only matters if some exit path failed to
# fire a course_result -- it bounds a runaway to ~10 minutes rather than
# forever.  Re-entering the course re-arms a fresh loop.
GATE_MAX_CONSECUTIVE_KILLS = 20

# TTL (ms) of each on-Switch overlay countdown notice.  The countdown
# re-sends one notice per second; the TTL is set comfortably longer than
# that interval so the banner never flickers between ticks, and so it
# self-clears on the Switch a couple seconds after the last notice if the
# loop stops abruptly (player left, bridge went quiet) and the explicit
# clear is dropped.
GATE_OVERLAY_TTL_MS = 2500


class SMBWContext(CommonContext):
    """CommonContext that ties the LAN server to the AP server.

    Construction is two-phase: the context is created early so the AP
    auth loop can start; the ``lan_server`` reference is wired in by
    :mod:`.main` after both objects exist.  Until then item-grant
    routing is silently dropped (no Switch is connected yet anyway).
    """

    game = GAME_NAME
    items_handling = 0b111  # all items via AP (no local progression)
    command_processor = SMBWCommandProcessor

    def __init__(
        self,
        server_address: str | None,
        password: str | None,
        state: BridgeState,
    ) -> None:
        super().__init__(server_address, password)
        self.bridge_state = state
        self.lan_server: Any = None  # wired post-construction
        self._location_name_to_id: dict[str, int] = {}
        self._item_name_to_id: dict[str, int] = {}
        self._sent_loc_ids: set[int] = set()

        # M3.8 DeathLink -- off by default; flipped on by slot_data
        # ``death_link`` truthy when the AP server delivers it on
        # Connected.  Gates both outbound (report_death sends Bounce iff
        # enabled) and inbound (on_deathlink forwards to Switch iff
        # enabled, belt-and-braces over CommonClient's tag-check).
        self.deathlink_enabled: bool = False

        # ``ten_coin_sanity`` slot_data toggle -- gates the outbound
        # TEN_COIN refund.  When OFF, the player's 10-coin blocks aren't
        # AP locations: the game gives +10 purple coins normally and the
        # bridge must NOT subtract them (no AP item is coming back to
        # net the refund out).  When ON, every 10-coin block is an AP
        # location, the +10 is purely a side-effect of the in-game
        # animation, and the refund makes AP the sole authority over
        # what the block actually awards.  Default ON to match the yaml
        # ``DefaultOnToggle`` -- if slot_data is missing the key entirely
        # (e.g. seed generated by an older apworld), we err toward the
        # documented option default.  The inbound ``"10 Coin"`` grant
        # is intentionally NOT gated: in a multiworld a peer with sanity
        # ON could ship a 10 Coin item to a slot with sanity OFF, and
        # the recipient should still receive the +10.
        self.ten_coin_sanity_enabled: bool = True

        # M3.7 goal-option respect (apworld fill_slot_data ships the
        # player's chosen goal as the resolved location name).  None
        # until ``Connected`` populates it; ``handle_goal_completed``
        # uses it to gate the Bowser-Nerve hook and
        # ``handle_check_emitted`` uses it to convert a goal-location
        # LocationCheck into a StatusUpdate(CLIENT_GOAL).  The dedup
        # flag stops the same session from sending CLIENT_GOAL twice
        # (e.g. Bowser kill firing both the M3.7 hook and the
        # PALACE_CLEAR check path).
        self.goal_location_name: str | None = None
        self._goal_status_sent: bool = False

        # M2.3 badge probe override -- when not None, ``_recompute_badge_mask``
        # returns this instead of the AP-derived mask.  Lets a developer
        # cycle through unmapped internal_ids (`/badge_probe <bit>`) to
        # discover which AP badge each bit corresponds to.  The 2 s
        # badge_sync tick keeps re-pushing the probe mask, so the badge
        # stays visible in-game until the override is cleared.
        self._badge_probe_mask: int | None = None

        # Power-up pickup gating (2026-06-10).  ``powerup_gating`` is set
        # from slot_data on Connected -- True for seeds where the Power-Up
        # items are real pool items (no longer precollected).  When True,
        # ``_recompute_itemget_deny_mask`` denies every gated power-up
        # pickup whose AP item hasn't been received yet (receiving the
        # item unlocks the ability to collect that power-up in-level).
        # ``_itemget_deny_override`` is the `/deny_powerups` debug
        # override -- when not None it wins over the AP-derived mask
        # (mirror of ``_badge_probe_mask``).
        self.powerup_gating: bool = False
        self._itemget_deny_override: int | None = None

        # Level-entry gate.  ``entry_gating_enabled`` defaults ON (the
        # feature is the request); an apworld can ship a
        # ``level_entry_gating`` slot_data key to turn it off per-seed.
        # ``_gate_kill_task`` holds the in-flight delayed-kill loop for
        # the course the player is currently inside (None when idle); it
        # is replaced/cancelled on every new gated entry and on shutdown.
        self.entry_gating_enabled: bool = True
        # Badge-specific sub-toggle.  Defaults OFF: badge-level entry
        # gate-kills are disabled everywhere unless explicitly turned on.
        # When False, BADGE-kind entry gates never arm (and any in-flight
        # badge kill self-stops within one countdown tick via
        # ``_gate_check_stop``) -- but the final-Bowser ROYAL_SEEDS gate is
        # unaffected.  Master ``entry_gating_enabled`` still wins (off there
        # disables everything).  Set from slot_data
        # ``badge_level_entry_gating`` (also defaults False) and
        # live-toggleable via the ``/badge_gate_kills on`` command.
        self.badge_entry_gating_enabled: bool = False
        self._gate_kill_task: asyncio.Task[None] | None = None

        # M2.3 known-invalid bits the user has confirmed (via
        # `/badge_probe_invalid`) don't correspond to any AP badge.
        # `/badge_probe_next` skips these so the user doesn't re-probe
        # dead bits.  Runtime-only -- not persisted across client
        # restarts; for permanent records add to ``badge_table._BADGES``
        # or paste the snippet from `/badge_status` somewhere durable.
        self._badge_probe_invalid: set[int] = set()

        # Open-world mode (2026-06).  ``open_world`` flips on when the
        # apworld ships a non-empty ``open_world_active`` list in
        # slot_data; ``open_world_active`` holds the active world numbers
        # (1-6) and ``palaces_required`` the Royal-Seed threshold that
        # unlocks Bowser.  ``_bowser_opened`` latches once the threshold
        # is met so we flag the Castle route routable exactly once (then
        # keep re-asserting on reconnect via the providers).  It only
        # opens the route + satisfies the death-gate; the win condition is
        # actually beating Bowser, same as vanilla.
        self.open_world: bool = False
        self.open_world_active: list[int] = []
        self.palaces_required: int = 0
        self._bowser_opened: bool = False

    # ---- AP lifecycle overrides ---------------------------------------

    async def server_auth(self, password_requested: bool = False) -> None:
        if password_requested and not self.password:
            log.warning(
                "AP server requested a password but none configured "
                "(set one in the connect command or pass --password)")
        await self.get_username()
        await self.send_connect()

    def on_package(self, cmd: str, args: dict) -> None:
        super().on_package(cmd, args)
        asyncio.create_task(self._handle_ap_package(cmd, args))

    async def _handle_ap_package(self, cmd: str, args: dict) -> None:
        if cmd == "Connected":
            log.info(
                "AP connected: slot=%s player=%s seed=%s",
                self.auth, self.slot, self.seed_name)
            self._rebuild_reverse_maps_from_context()
            # M3.8 -- pick up the player's per-slot DeathLink choice from
            # slot_data and toggle the connection tag.  Slot_data wins
            # over any local default; this is the only place the YAML's
            # ``death_link`` setting takes effect.  ``update_death_link``
            # mutates ``self.tags`` and sends a ConnectUpdate when
            # changed mid-session.
            slot_data = args.get("slot_data") or {}
            dl = bool(slot_data.get("death_link"))
            if dl != self.deathlink_enabled:
                log.info(
                    "DeathLink: %s (from slot_data)",
                    "ENABLED" if dl else "disabled")
            self.deathlink_enabled = dl
            try:
                await self.update_death_link(dl)
            except Exception:
                log.exception("update_death_link(%s) failed", dl)
            # Default ON to match the yaml DefaultOnToggle if the key is
            # missing entirely (older apworld at gen time).
            tcs = bool(slot_data.get("ten_coin_sanity", True))
            if tcs != self.ten_coin_sanity_enabled:
                log.info(
                    "ten_coin_sanity: %s (from slot_data)",
                    "ENABLED" if tcs else "disabled")
            self.ten_coin_sanity_enabled = tcs

            # M3.7 -- pick up the player's chosen goal location from
            # slot_data.  The apworld's fill_slot_data resolves the
            # integer ``goal`` option to its location name, which the
            # bridge then uses to gate both the Bowser-Nerve hook and
            # the per-check CLIENT_GOAL path.  An older apworld that
            # only ships the integer drops to None here and we fall
            # back to the prior unconditional behavior with a warning.
            goal_name = slot_data.get("goal_location_name")
            if isinstance(goal_name, str) and goal_name:
                if goal_name != self.goal_location_name:
                    log.info("goal location: %r (from slot_data)", goal_name)
                self.goal_location_name = goal_name
            else:
                log.warning(
                    "slot_data missing 'goal_location_name'; "
                    "goal hook will fire on Bowser regardless of "
                    "the player's chosen goal option")
                self.goal_location_name = None
            # Level-entry gating toggle.  Default ON if slot_data omits
            # the key (older apworld, or one that hasn't surfaced the
            # option) -- the feature is the request.  The kill loop
            # re-reads this flag each iteration, so flipping it off mid-
            # session stops any in-flight kill within one delay window.
            eg = bool(slot_data.get("level_entry_gating", True))
            if eg != self.entry_gating_enabled:
                log.info(
                    "level_entry_gating: %s (from slot_data)",
                    "ENABLED" if eg else "disabled")
            self.entry_gating_enabled = eg
            # Badge-specific sub-toggle (default OFF; badge gate-kills are
            # disabled everywhere unless the slot opts in or the player runs
            # ``/badge_gate_kills on``).  The Royal-Seed/Bowser gate is
            # governed only by ``level_entry_gating`` above and is unaffected.
            beg = bool(slot_data.get("badge_level_entry_gating", False))
            if beg != self.badge_entry_gating_enabled:
                log.info(
                    "badge_level_entry_gating: %s (from slot_data)",
                    "ENABLED" if beg else "disabled")
            self.badge_entry_gating_enabled = beg

            # Power-up pickup gating.  Default OFF when the key is
            # missing (seed generated before the Power-Up items entered
            # the pool) -- those seeds precollect the power-ups and must
            # keep vanilla pickups.  Push immediately so the gate engages
            # even if the connect-time ReceivedItems batch is empty.
            pg = bool(slot_data.get("powerup_gating"))
            if pg != self.powerup_gating:
                log.info(
                    "powerup_gating: %s (from slot_data)",
                    "ENABLED" if pg else "disabled")
            self.powerup_gating = pg
            if self.lan_server is not None:
                self.lan_server.send_set_itemget_deny(
                    self._recompute_itemget_deny_mask())

            # Open-world mode.  The apworld injects ``open_world_active``
            # (the random active world set) and ``palaces_required`` into
            # slot_data; a non-empty list turns the mode on.  Reset the
            # one-shot Bowser latch -- the ReceivedItems that follows
            # Connected re-evaluates the threshold and re-opens if met.
            active = slot_data.get("open_world_active") or []
            self.open_world_active = [int(n) for n in active if isinstance(n, int)]
            self.open_world = bool(self.open_world_active)
            self.palaces_required = int(slot_data.get("palaces_required") or 0)
            self._bowser_opened = False
            if self.open_world:
                log.info(
                    "open-world: active worlds=%s palaces_required=%d",
                    self.open_world_active, self.palaces_required)
                # Push routable worlds and the world-unlock hash batch so
                # courses appear on fresh saves before any item arrives.
                # Both are idempotent and replay on HelloMsg.
                if self.lan_server is not None:
                    self.lan_server.send_set_routable_worlds(
                        self._recompute_routable_worlds_mask())
                    self.lan_server.send_apply_world_unlock(
                        *self._world_unlock_hashes())

            # Badge-shop AP ownership: push the shop masks now that the
            # reverse maps + checked_locations (carried by Connected) are
            # available, so a returning player's already-bought badges show
            # sold-out and AP-granted-but-unbought ones show purchasable
            # before they walk into a shop.
            self._push_badge_shop_state()

            # AP shop-text: scout the shop-badge checks so the shop detail
            # panel can show what each purchase would send.  The LocationInfo
            # reply is handled below and pushes the per-badge text.
            await self._scout_shop_badge_locations()

            # Tell the AP server the player is in-game so item routing
            # starts flowing.  ClientStatus.CLIENT_PLAYING.
            try:
                await self.send_msgs([{
                    "cmd": "StatusUpdate",
                    "status": ClientStatus.CLIENT_PLAYING,
                }])
            except Exception:
                log.exception("StatusUpdate(CLIENT_PLAYING) failed")
            return

        if cmd == "DataPackage":
            self._rebuild_reverse_maps_from_context()
            return

        if cmd == "ReceivedItems":
            await self._handle_received_items(args)
            return

        if cmd == "RoomUpdate":
            # ``checked_locations`` may grow (our own checks confirmed, or
            # another slot's collect) -- re-push the badge-shop sold mask so
            # a freshly-obtained shop badge flips to sold-out.  super()
            # already merged the update into self.checked_locations.
            if "checked_locations" in args:
                self._push_badge_shop_state()
            return

        if cmd == "LocationInfo":
            # Reply to our shop-badge LocationScouts (super() already merged
            # the scouted items into self.locations_info).  Push the per-badge
            # AP-check text so the shop detail panel reflects what each
            # purchase would send.
            text = self._recompute_badge_shop_text()
            log.info("shop-text: LocationInfo -> %d badge texts: %s",
                     len(text), text)
            self._push_badge_shop_text(force=True)
            return

    def _rebuild_reverse_maps_from_context(self) -> None:
        """Build name->id reverse maps from CommonContext's id->name tables.
        Idempotent; called on Connected and on DataPackage."""
        try:
            loc_id_to_name = self.location_names[self.game]
            item_id_to_name = self.item_names[self.game]
        except Exception:
            log.exception("could not read location_names/item_names for %s", self.game)
            return
        self._location_name_to_id = {
            name: cid for cid, name in loc_id_to_name.items()
        }
        self._item_name_to_id = {
            name: cid for cid, name in item_id_to_name.items()
        }
        log.info(
            "reverse maps built: %d locations, %d items for %s",
            len(self._location_name_to_id),
            len(self._item_name_to_id),
            self.game,
        )

    def _recompute_itemget_deny_mask(self) -> int:
        """Compute the Switch ItemGet deny mask: every gated power-up bit
        whose AP item has NOT been received.  0 == vanilla pickups.

        Returns 0 unless this seed shuffles power-ups
        (``powerup_gating`` from slot_data), so old seeds and the
        pre-Connected window stay vanilla.  Precollected power-ups
        (old seeds) also arrive in the connect-time ReceivedItems batch,
        so they clear their bits naturally even if the marker were set.

        `/deny_powerups` override: if ``_itemget_deny_override`` is not
        None, return it instead (mirror of the badge probe override).
        """
        if self._itemget_deny_override is not None:
            return self._itemget_deny_override
        if not self.powerup_gating:
            return 0
        mask = powerup_table.GATED_MASK
        for it in self.items_received:
            item_id = getattr(it, "item", None)
            if item_id is None and isinstance(it, dict):
                item_id = it.get("item")
            if item_id is None:
                continue
            try:
                item_name = self.item_names.lookup_in_game(int(item_id))
            except Exception:
                continue
            bit = powerup_table.deny_bit_for_item(item_name)
            if bit is None:
                continue
            mask &= ~(1 << bit)
        return mask

    def set_itemget_deny_override(self, mask: int | None) -> None:
        """`/deny_powerups` entry point.  Set or clear the deny-mask
        override and immediately push it to the Switch.  Passing ``None``
        restores AP-authoritative mode (the mask recomputes from
        items_received + slot_data)."""
        self._itemget_deny_override = mask
        lan = self.lan_server
        if lan is not None:
            lan.send_set_itemget_deny(self._recompute_itemget_deny_mask())

    def _recompute_badge_shop_state(self) -> tuple[int, int]:
        """Compute the AP-authoritative Poplin badge-shop masks the Switch
        applies to the shop display (see ``wire.SetBadgeShopStateMsg``).

        ``managed`` = every badge sold at a Poplin shop (an AP shop check):
        AP owns those rows' display state regardless of the in-game
        owned/purchased bits.  ``sold`` = the managed badges whose
        "<Badge> Obtained" location is already obtained -- server-confirmed
        (:attr:`checked_locations`) OR sent this session
        (:attr:`_sent_loc_ids`, optimistic so a just-bought badge shows
        sold-out immediately rather than flickering back to purchasable
        during the AP round-trip).

        ``managed`` needs no AP-id resolution (it's the static set of shop
        badge internal_ids), so it's correct even before the DataPackage
        reverse maps exist; ``sold`` resolves each badge's location name to
        its AP id and stays empty until the maps are built (a Connected /
        periodic-tick push then corrects it).  This is the
        ``badge_shop_state_provider`` the LAN server replays on HelloMsg +
        the 2 s tick.
        """
        managed = 0
        sold = 0
        obtained = set(self.checked_locations) | self._sent_loc_ids
        for internal_id, loc_name in shop_badge_location_names().items():
            managed |= (1 << internal_id)
            loc_id = self._location_name_to_id.get(loc_name)
            if loc_id is not None and loc_id in obtained:
                sold |= (1 << internal_id)
        return managed, sold

    def _push_badge_shop_state(self) -> None:
        """Recompute + push the badge-shop masks to the Switch now.  No-op
        when no Switch client is bound (the LAN server replays on the next
        HelloMsg / tick)."""
        lan = self.lan_server
        if lan is None:
            return
        managed, sold = self._recompute_badge_shop_state()
        lan.send_set_badge_shop_state(managed, sold)

    # ---- AP shop-text (scouted-check display) -------------------------

    def _shop_badge_location_ids(self) -> list[int]:
        """AP location ids of the shop-badge checks present in THIS slot, for
        scouting.  Filtered to :attr:`server_locations` (the locations the
        server actually has for us) -- the DataPackage name->id table carries
        every location the apworld can define, but a given seed may omit some
        (badges not shuffled, category excluded, ...).  Scouting an id the
        server doesn't have raises a fatal ``KeyError`` server-side and drops
        the connection, so we must only request real ones.  Empty until the
        Connected packet populates ``server_locations``."""
        known = self.server_locations
        ids: list[int] = []
        for loc_name in shop_badge_location_names().values():
            loc_id = self._location_name_to_id.get(loc_name)
            if loc_id is not None and loc_id in known:
                ids.append(loc_id)
        return ids

    def _format_shop_check_text(self, net_item: Any) -> str:
        """Build the per-badge detail text from a scouted location's item:
        ``"<player>: <item>"`` (the item the purchase would send, and to
        whom).  Resolves the item name in its owning slot's game and the
        receiving player's name, with robust fallbacks."""
        player = int(getattr(net_item, "player", 0) or 0)
        item_id = int(getattr(net_item, "item", 0) or 0)
        player_name = self.player_names.get(player, f"Player {player}")
        item_name: str | None = None
        try:
            item_name = self.item_names.lookup_in_slot(item_id, player)
        except Exception:
            try:
                item_name = self.item_names.lookup_in_game(item_id)
            except Exception:
                item_name = None
        if not item_name:
            item_name = f"item {item_id}"
        if player == self.slot:
            return f"Your {item_name}"
        return f"{player_name}'s {item_name}"

    def _recompute_badge_shop_text(self) -> dict[int, str]:
        """``{badge_internal_id: text}`` for every shop badge whose AP check
        has been scouted (present in :attr:`locations_info`).  The Switch
        shows this in the badge-shop detail panel.  Empty until the connect-
        time LocationScouts response arrives; the ``badge_shop_text_provider``
        the LAN server replays on HelloMsg + the 2 s tick."""
        out: dict[int, str] = {}
        for internal_id, loc_name in shop_badge_location_names().items():
            loc_id = self._location_name_to_id.get(loc_name)
            if loc_id is None:
                continue
            net_item = self.locations_info.get(loc_id)
            if net_item is None:
                continue
            try:
                out[internal_id] = self._format_shop_check_text(net_item)
            except Exception:
                log.exception(
                    "failed to format shop-check text for badge %d (loc %d)",
                    internal_id, loc_id)
        return out

    def _push_badge_shop_text(self, *, force: bool = False) -> None:
        """Push the AP shop-text table to the Switch now.  No-op without a
        bound Switch client (the LAN server replays on HelloMsg / tick)."""
        lan = self.lan_server
        if lan is not None:
            lan._push_badge_shop_text_now(force=force)

    async def _scout_shop_badge_locations(self) -> None:
        """LocationScouts the shop-badge checks (no hint) so we can show what
        each purchase would send.  Awaited from the Connected handler; the
        LocationInfo reply is handled in :meth:`_handle_ap_package`."""
        loc_ids = self._shop_badge_location_ids()
        if not loc_ids:
            log.warning(
                "shop-text: no shop-badge location ids resolved yet; "
                "skipping LocationScouts")
            return
        try:
            await self.send_msgs([{
                "cmd": "LocationScouts",
                "locations": loc_ids,
                "create_as_hint": 0,
            }])
            log.info("shop-text: scouting %d shop-badge locations: %s",
                     len(loc_ids), loc_ids)
        except Exception:
            log.exception("LocationScouts(shop badges) send failed")

    def _recompute_badge_mask(self) -> int:
        """Walk :attr:`items_received` and compute the absolute owned-badge
        bitmask -- one bit per known badge item.  Items the badge table
        doesn't recognize contribute 0 bits (silent drop).  This is what
        the LAN server pushes to the Switch as the authoritative badge
        set on HelloMsg, on a periodic tick, and after every
        ReceivedItems.

        M2.3 probe override: if ``self._badge_probe_mask`` is not None,
        return it instead of the AP-derived mask.  Drives the
        `/badge_probe` command loop where a developer sets a single bit
        to see which badge it corresponds to in-game.
        """
        if self._badge_probe_mask is not None:
            return self._badge_probe_mask
        mask = 0
        for it in self.items_received:
            item_id = getattr(it, "item", None)
            if item_id is None and isinstance(it, dict):
                item_id = it.get("item")
            if item_id is None:
                continue
            try:
                item_name = self.item_names.lookup_in_game(int(item_id))
            except Exception:
                continue
            bit = badge_table.grant_internal_id_for_item(item_name)
            if bit is None:
                continue
            mask |= (1 << bit)
        return mask

    def set_badge_probe_mask(self, mask: int | None) -> None:
        """M2.3 probe-and-discover entry point.  Set or clear the badge
        probe override and immediately push a SetBadgesAbsolute so the
        Switch reflects the new mask without waiting for the next tick.

        Passing ``None`` restores AP-authoritative mode (the badge mask
        recomputes from items_received).  Passing 0 sends an empty mask
        (useful for confirming the override is active).
        """
        self._badge_probe_mask = mask
        lan = self.lan_server
        if lan is not None and hasattr(lan, "_push_badge_sync_now"):
            lan._push_badge_sync_now()

    def _recompute_wonder_seed_counts(self) -> list[int]:
        """Walk :attr:`items_received` and return cumulative Wonder Seed
        counts bucketed by world.  Returns a list of length
        ``wonder_seed_table.WORLD_COUNT`` (8 buckets:
        W1/W2/W3/W4/W5/W6/Petal Isles/Special).  Idempotent and
        side-effect-free; the bridge pushes the tuple as
        ``SetWonderSeedCountsMsg`` on HelloMsg, on every ReceivedItems,
        and on the periodic 2 s tick.  The Switch reads container-A
        hash ``0x9f5ead3c`` (current world index) and applies
        ``counts[bucket]`` via ``probe::pushWonderSeedOverride`` --
        making AP the sole authority over Wonder Seed gating.

        Items the table doesn't recognize contribute to nothing
        (silent drop), matching the rest of the table layer's fallback
        semantics."""
        counts = [0] * wonder_seed_table.WORLD_COUNT
        for it in self.items_received:
            item_id = getattr(it, "item", None)
            if item_id is None and isinstance(it, dict):
                item_id = it.get("item")
            if item_id is None:
                continue
            try:
                item_name = self.item_names.lookup_in_game(int(item_id))
            except Exception:
                continue
            idx = wonder_seed_table.world_index_for_item(item_name)
            if idx is None:
                continue
            counts[idx] += 1
        return counts

    def _recompute_wonder_seed_bits(self) -> tuple[int, int]:
        """Walk :attr:`items_received` and return the absolute 128-bit
        per-course Wonder Seed bitfield as a ``(bits_lo, bits_hi)``
        tuple of two u64s.  Bridge pushes this via
        :class:`SetWonderSeedsAbsoluteMsg`; Switch overwrites container-C
        hash ``0x60458608`` to match on every ReceivedItems / HelloMsg /
        periodic tick.  (2026-05-29)

        Bit derivation: 16 bits per world bucket.  World w occupies
        bits ``[w*16, w*16+15]``.  For each world with cumulative AP
        count ``N_w`` (capped at 16), set the lowest ``N_w`` bits in
        that range.  This is a DETERMINISTIC AP-derived bitfield that
        the Switch persistence-test workflow can verify against the
        saved file; bit positions are NOT semantically tied to specific
        in-game courses yet (refining the bit-to-course mapping
        requires additional RE of the FUN_71003D4110 Murmur3 course-
        name -> course-index lookup).

        Returns ``(0, 0)`` when no Wonder Seeds are owned; that is
        valid input to the Switch primitive and means "AP knows of no
        seeds -- clobber the bitfield to empty".  16 bits per bucket
        accommodates up to 16 seeds per world; vanilla SMBW worlds run
        up to ~17 seeds (W6 has ~34 across multiple branches), so a
        future refinement may either widen each bucket or remap to
        course indices proper."""
        counts = self._recompute_wonder_seed_counts()
        BITS_PER_WORLD = 16
        bits = 0
        for w, count in enumerate(counts):
            capped = min(count, BITS_PER_WORLD)
            if capped <= 0:
                continue
            bucket_mask = (1 << capped) - 1
            bits |= bucket_mask << (w * BITS_PER_WORLD)
        bits_lo = bits & ((1 << 64) - 1)
        bits_hi = (bits >> 64) & ((1 << 64) - 1)
        return (bits_lo, bits_hi)

    def _recompute_royal_seed_mask(self) -> int:
        """Walk :attr:`items_received` and return the absolute 6-bit
        Royal Seed mask -- one bit per world (bit 0 = W1, ..., bit 5 =
        W6).  Idempotent and side-effect-free.  Items the table doesn't
        recognize contribute 0 bits (silent drop), matching the badge
        path.

        The bridge pushes this as ``SetRoyalSeedsAbsoluteMsg`` on
        HelloMsg, on every ReceivedItems, and on the periodic 2 s tick.
        The Switch loops the 6 container-B bool hashes (see
        ``royal_seed_table.ROYAL_SEED_HASHES`` for the canonical
        order, mirrored Switch-side) and writes
        ``(mask >> bit) & 1`` to each -- granting AP-granted seeds and
        clearing any seed the player obtained in-game without AP."""
        mask = 0
        for it in self.items_received:
            item_id = getattr(it, "item", None)
            if item_id is None and isinstance(it, dict):
                item_id = it.get("item")
            if item_id is None:
                continue
            try:
                item_name = self.item_names.lookup_in_game(int(item_id))
            except Exception:
                continue
            bit = royal_seed_table.bit_for_item(item_name)
            if bit is None:
                continue
            mask |= (1 << bit)
        return mask

    def _recompute_routable_worlds_mask(self) -> int:
        """Open-world: return the AP-authoritative routable-world mask --
        bit N-1 set per active world N (bit 0 = W1 ... bit 5 = W6), plus
        the Castle bit once Bowser is unlocked.  Returns 0 when open-world
        is inactive, which the Switch hook treats as a no-op (vanilla
        world-unlock behavior).  Side-effect-free; wired as the LanServer
        ``routable_worlds_provider`` so it replays on HelloMsg + tick."""
        if not self.open_world:
            return 0
        # Walk-in model (2026-06-05): ONLY Petal Isles (the hub) is made
        # routable.  The player fast-travels to PI -- which the Switch
        # pre-unlocks (all nodes + route gates) -- and walks into each active
        # world's entrance ON FOOT.  A normal on-foot entry plays that world's
        # FirstVisitDemo, which clears its route obstacle (fast-travelling
        # straight into a world skips the demo, leaving the obstacle), so the
        # world is left otherwise VANILLA: not in the fast-travel mask, not
        # node-filled, its internal seed-bar gates intact.  Castle stays
        # routable once Bowser is unlocked so the final fight is reachable.
        mask = (1 << ROUTABLE_PETAL_BIT)
        # World 1 EXCEPTION (2026-06-09): W1 is not walk-connected to Petal
        # Isles, so the walk-in path can never reach it.  When W1 is active we
        # add its routable bit so it shows up as a fast-travel destination and
        # teleports the player to 1-1 (the Switch first-course fill +
        # worldRoutable/worldTravel/courseVisible hooks all key on bit 0 == W1).
        # Only when active -- W1 is never travelable otherwise.
        if OPEN_WORLD_W1 in self.open_world_active:
            mask |= (1 << ROUTABLE_W1_BIT)
        if self._bowser_opened:
            mask |= (1 << ROUTABLE_CASTLE_BIT)
        return mask

    def _open_world_royal_seed_mask(self) -> int | None:
        """Royal Seeds are NEVER pushed to the Switch.  The in-game Royal Seed
        state stays purely vanilla -- the player collects them by clearing
        palaces, which triggers the cutscenes / world-map UI naturally -- and
        the AP-granted Royal Seed count is used ONLY by the final-level
        death-gate (whether the player is kicked out of Bowser's stage; read
        from ``_recompute_royal_seed_mask``).  This provider is therefore
        retired (2026-06-05) and always returns ``None``; it is kept as a no-op
        so the LanServer wiring stays unchanged."""
        return None

    def _world_unlock_hashes(self) -> tuple[tuple[int, ...], tuple[int, ...]]:
        """Open-world: return the (int_hashes, bool_hashes) world/course
        unlock pair to send to the Switch, split by GameDataList category
        (Int -> grantContainerACounter, Bool -> grantContainerBBool).
        Returns empty tuples when open-world is inactive so the LAN server
        skips the send.  Wired as the LanServer
        ``world_unlock_hashes_provider`` so the unlock replays on HelloMsg.
        The table is static (derived from a fresh→100%-save diff 2026-06-03)
        and independent of which worlds are active -- we unlock all worlds'
        course state and rely on the death-gate + AP logic to enforce which
        courses the player may actually complete."""
        if not self.open_world:
            return ((), ())
        return (world_unlock_table.WORLD_UNLOCK_INT_HASHES,
                world_unlock_table.WORLD_UNLOCK_BOOL_HASHES)

    async def _handle_received_items(self, args: dict) -> None:
        items = args.get("items", []) or []

        new_mask = self._recompute_badge_mask()
        new_seed_counts = self._recompute_wonder_seed_counts()
        # Royal Seeds are deliberately NOT pushed to the Switch any more
        # -- the vanilla game owns Royal Seed state (world-map UI,
        # final-castle access).  AP enforcement of "don't enter Bowser
        # without the seeds" is handled by the level-entry death-gate
        # instead, which reads the AP-granted count below.
        new_royal_seed_mask = self._recompute_royal_seed_mask()
        new_ws_bits_lo, new_ws_bits_hi = self._recompute_wonder_seed_bits()
        if self.lan_server is not None:
            log.info(
                "ReceivedItems: badge mask now 0x%x, royal_seed mask 0x%x "
                "(not pushed; vanilla-owned), wonder_seed counts=%s, "
                "wonder_seed bits=(0x%x, 0x%x)",
                new_mask, new_royal_seed_mask, new_seed_counts,
                new_ws_bits_lo, new_ws_bits_hi)
            self.lan_server.send_set_badges_absolute(new_mask)
            # Idempotent absolute-overwrite: AP is the sole authority
            # over the per-world Wonder Seed counts (same pattern as
            # badges).  Switch routes via current-world hash 0x9f5ead3c.
            self.lan_server.send_set_wonder_seed_counts(new_seed_counts)
            # 2026-05-29 -- AP-authoritative per-course Wonder Seed
            # bitfield (container-C hash 0x60458608).  Same idempotent
            # absolute-overwrite pattern -- Switch overwrites all 128
            # bits to match.
            self.lan_server.send_set_wonder_seeds_absolute(
                new_ws_bits_lo, new_ws_bits_hi)
            # Power-up pickup gating: re-derive the deny mask -- a newly
            # received Power-Up item clears its bit, unlocking that
            # pickup type in-level within a frame of the Switch applying
            # it.  No-ops to 0 when this seed doesn't gate power-ups.
            self.lan_server.send_set_itemget_deny(
                self._recompute_itemget_deny_mask())
            # Open-world: once the player holds enough AP Royal Seeds, flag the
            # Castle route routable so the player can travel to Bowser.  Royal
            # Seeds are NOT pushed to the Switch -- the in-game seed state is
            # left vanilla.  One-shot; the routable provider re-asserts on
            # reconnect.  This only opens the *route* (early access is fine --
            # they just get bounced at the door if unqualified); it does NOT
            # win the seed or satisfy the death-gate.
            #
            # The GOAL is NOT signalled here.  Open-world now mirrors vanilla:
            # the win condition is actually DEFEATING Bowser (the
            # GameGoalReached Nerve -> handle_goal_completed path, or the
            # goal-location check), not merely holding the seeds.  And the
            # final-level death-gate (see _gate_requirement_met) independently
            # requires ALL SIX AP Royal Seeds plus palaces_required palaces
            # cleared in-game before it lets the player face Bowser.
            if self.open_world and not self._bowser_opened:
                owned = bin(new_royal_seed_mask).count("1")
                if owned >= self.palaces_required:
                    self._bowser_opened = True
                    log.info(
                        "open-world: %d/%d AP Royal Seeds held -> Castle "
                        "routable (seeds NOT pushed; vanilla-owned; beat "
                        "Bowser to win, gated on all 6 seeds + %d palaces)",
                        owned, self.palaces_required, self.palaces_required)
                    self.lan_server.send_set_routable_worlds(
                        self._recompute_routable_worlds_mask())
        else:
            log.debug(
                "no lan_server bound; not forwarding badge mask 0x%x / "
                "wonder_seed counts=%s / wonder_seed bits=(0x%x, 0x%x)",
                new_mask, new_seed_counts,
                new_ws_bits_lo, new_ws_bits_hi)

        for it in items:
            item_id = it.get("item") if isinstance(it, dict) else None
            if item_id is None:
                item_id = getattr(it, "item", None)
            if item_id is None:
                log.warning("ReceivedItems entry without item id: %r", it)
                continue
            try:
                item_name = self.item_names.lookup_in_game(int(item_id))
            except Exception as e:
                log.warning("can't resolve item id %r: %s", item_id, e)
                continue
            if badge_table.is_badge_item(item_name):
                log.debug(
                    "badge item received: %r (id=%s) -- covered by "
                    "SetBadgesAbsolute push", item_name, item_id)
                # NOTE: we no longer auto-resolve the "<Badge> Obtained"
                # shop location on grant.  The Switch badge-shop override
                # (SetBadgeShopState) now keeps an AP-granted-but-unbought
                # shop badge PURCHASABLE -- despite its owned bit being set
                # so Mario can equip it -- so the player completes the shop
                # check by actually buying it (the purchase-commit hook
                # fires BADGE_ACQUIRED).  See _recompute_badge_shop_state.
                continue
            if wonder_seed_table.is_wonder_seed_item(item_name):
                log.debug(
                    "wonder seed item received: %r (id=%s) -- covered by "
                    "SetWonderSeedCounts push", item_name, item_id)
                continue
            if royal_seed_table.is_royal_seed_item(item_name):
                # Royal Seeds are vanilla-owned: nothing to push, and we
                # no longer auto-resolve the palace location on receipt.
                # The player re-enters the palace and the natural
                # PALACE_CLEAR (koopajr_result) fires the AP check.
                log.debug(
                    "royal seed item received: %r (id=%s) -- vanilla-owned, "
                    "no push, palace check fires on natural clear",
                    item_name, item_id)
                continue
            if coin_table.is_coin_item(item_name):
                grant = coin_table.grant_for_item(item_name)
                if grant is None:
                    log.warning(
                        "coin_table inconsistency: "
                        "is_coin_item(%r)=True but no grant", item_name)
                    continue
                hash_, delta = grant
                log.info(
                    "item received: %r (id=%s) -> increment_hash_keyed "
                    "hash=0x%08x delta=%d",
                    item_name, item_id, hash_, delta)
                if self.lan_server is None:
                    log.debug("no lan_server bound; cannot forward grant")
                    continue
                self.lan_server.send_increment_hash_keyed(hash_, delta)
            else:
                log.info(
                    "received unhandled item %r (id=%s); ignoring "
                    "(no table entry)", item_name, item_id)

    # ---- DeathLink ----------------------------------------------------

    def on_deathlink(self, data: dict[str, Any]) -> None:
        """CommonContext dispatches this when an AP DeathLink Bounce
        arrives for a slot tagged ``DeathLink``.  Forward to the Switch
        as a KillMsg unless we sourced it ourselves (CommonClient already
        gates by timestamp; this is belt-and-braces).
        """
        super().on_deathlink(data)
        if not self.deathlink_enabled:
            return
        source = str(data.get("source") or "")
        if source and source == self._own_player_name():
            log.debug("on_deathlink: ignoring self-sourced bounce")
            return
        cause = str(data.get("cause") or "")
        if self.lan_server is None:
            log.info(
                "[deathlink in] source=%s cause=%s (no Switch bound; dropping)",
                source, cause)
            return
        log.info("[deathlink in] source=%s cause=%s -> Switch", source, cause)
        self.lan_server.send_kill(source=source, cause=cause)

    async def handle_death_reported(self, death: DeathReported) -> None:
        """LanServer dispatches this when the processor emits a
        DeathReported (a Switch-side DEATH_DETECTED that survived the
        discriminator).  We delegate to CommonContext's ``send_death``
        which composes the Bounce with our slot name as ``source`` and
        updates ``last_death_link`` (so an echo back doesn't re-trigger
        ``on_deathlink``)."""
        if not self.deathlink_enabled:
            log.debug(
                "handle_death_reported(seq=%d): deathlink disabled, dropping",
                death.seq)
            return
        try:
            await self.send_death("mario_died")
            log.info(
                "-> AP Bounce DeathLink seq=%d source=%s",
                death.seq, self._own_player_name() or "?")
        except Exception:
            log.exception("send_death failed for seq=%d", death.seq)

    # ---- Level-entry gate ---------------------------------------------

    async def handle_gate_entered(self, ev: GateEntered) -> None:
        """LanServer dispatches this when the processor sees the player
        enter a course AP logic gates (a badge-granting course, or the
        final Bowser stage).  If the player lacks the required item, arm
        a delayed-kill loop that bounces them out via the DeathLink
        ``synthKill`` route after a grace window.

        A new gated entry always supersedes any prior pending kill (only
        one course is active at a time).
        """
        self._cancel_gate_kill()

        if not self.entry_gating_enabled:
            return

        if ev.gate_kind == GateKind.BADGE and not self.badge_entry_gating_enabled:
            log.info(
                "entered badge-gated stage_key=0x%08x (req=%d) but badge "
                "entry-gating is disabled -- not arming a kill",
                ev.stage_key & 0xFFFFFFFF, ev.requirement)
            return

        # Open-world: don't bounce the player out of a badge course that
        # isn't part of this seed's logic.  The worlds you aren't playing
        # (and Petal Isles / the Special world) still have their course
        # state unlocked on the Switch, so the player can walk into a
        # badge-granting course whose badge AP never places -- gating it
        # would be an unsatisfiable lock on content they don't need.
        if not self._gate_course_in_logic(ev):
            log.info(
                "entered gated stage_key=0x%08x (%s req=%d) in a world "
                "outside this open-world seed's logic (world_no=%d); "
                "not gating",
                ev.stage_key & 0xFFFFFFFF, ev.gate_kind.value,
                ev.requirement, ev.world_no)
            return

        # Deliberately NOT gated on being connected to AP: a player who
        # disconnects (or never connects) must NOT be able to sequence-
        # break into gated content offline.  When not connected,
        # items_received reflects the last-known AP state (empty on a
        # fresh start), so an offline player with no recorded item is
        # treated as not owning it -- and gets bounced.  Reconnecting
        # and receiving the item satisfies the requirement on the next
        # loop iteration.
        if self._gate_requirement_met(ev):
            log.info(
                "entered gated stage_key=0x%08x (%s req=%d); requirement "
                "satisfied -- no kill",
                ev.stage_key & 0xFFFFFFFF, ev.gate_kind.value, ev.requirement)
            return

        log.warning(
            "entered gated stage_key=0x%08x (%s req=%d) WITHOUT the required "
            "item; arming delayed kill (%.0fs grace, re-arms while inside)",
            ev.stage_key & 0xFFFFFFFF, ev.gate_kind.value, ev.requirement,
            GATE_KILL_DELAY_S)
        self._gate_kill_task = asyncio.create_task(
            self._gate_kill_loop(ev), name="smbw-gate-kill")

    def _gate_course_in_logic(self, ev: GateEntered) -> bool:
        """Open-world only: is the badge course the player just entered
        part of *this* seed's logic?

        In open-world mode the worlds you are not playing are stripped
        from the item/location pool (their Wonder Seeds are precollected
        so their counters show full), yet every world's course state stays
        unlocked on the Switch -- so the player can wander into an inactive
        world (or Petal Isles / the Special world) and walk into a badge-
        granting course whose badge AP never places.  Bouncing them out is
        pointless: the requirement can never be satisfied and they don't
        need to clear it.  Treat a BADGE gate as in-logic only when its
        course maps to one of the active numbered worlds; PI/Castle/Special
        (``pr_world_no_to_ap_world`` -> ``None``) and inactive numbered
        worlds are out of logic.

        Always ``True`` outside open-world mode (every world is in logic)
        and for non-badge gates -- the final-Bowser ROYAL_SEEDS gate must
        always fire."""
        if not self.open_world:
            return True
        if ev.gate_kind != GateKind.BADGE:
            return True
        ap_world = pr_world_no_to_ap_world(ev.world_no)
        return ap_world is not None and ap_world in self.open_world_active

    def _palace_location_ids(self) -> set[int]:
        """AP location IDs for the six numbered-world palace clears (W1..W6;
        NOT the final Bowser stage).  Derived from the Royal-Seed table's
        palace stage_keys via the location table, resolved to server IDs
        through the reverse map built on Connected/DataPackage.  Empty until
        that map exists."""
        ids: set[int] = set()
        for sk in royal_seed_table.PALACE_STAGE_KEYS:
            name = lookup_name(
                CheckEmitted(kind=CheckKind.PALACE_CLEAR, stage_key=sk))
            if name is None:
                continue
            loc_id = self._location_name_to_id.get(name)
            if loc_id is not None:
                ids.add(loc_id)
        return ids

    def _palaces_cleared_in_game(self) -> int:
        """How many of the six numbered-world palaces the player has actually
        cleared in their own game.

        Measured by counting palace-clear AP locations present in
        :attr:`checked_locations` -- the server persists that set, so it
        survives reconnects (unlike a session-local counter) and, crucially,
        reflects *genuine in-game palace clears*: receiving a Royal Seed
        *item* from AP does NOT auto-check the palace location (see the Royal
        Seed branch of ``_handle_received_items``), so the only way a palace
        location lands here is the player physically clearing it."""
        return len(self._palace_location_ids() & set(self.checked_locations))

    def _gate_requirement_met(self, ev: GateEntered) -> bool:
        """True iff the player satisfies the gate's requirement.

        BADGE: own the AP badge whose container-C internal_id is
        ``ev.requirement``.  ROYAL_SEEDS (final Bowser): hold all
        ``ev.requirement`` AP Royal Seeds AND -- in open-world -- have
        actually cleared ``palaces_required`` palaces in your own game.
        (Open-world force-opens the Castle route and AP can route Royal Seed
        items to you without you ever playing a palace, so the in-game
        palace clears are an anti-cheese second condition; standard mode
        leaves palace-clearing to the vanilla castle gate.)  An unknown gate
        kind fails *open* (treated as satisfied) so a future gate kind never
        kills players on an older client."""
        if ev.gate_kind == GateKind.BADGE:
            return bool((self._recompute_badge_mask() >> ev.requirement) & 1)
        if ev.gate_kind == GateKind.ROYAL_SEEDS:
            owned = bin(self._recompute_royal_seed_mask()).count("1")
            if owned < ev.requirement:
                return False
            if (self.open_world
                    and self._palaces_cleared_in_game() < self.palaces_required):
                return False
            return True
        log.warning(
            "unknown gate kind %r for stage_key=0x%08x; treating as satisfied",
            ev.gate_kind, ev.stage_key & 0xFFFFFFFF)
        return True

    def _gate_kill_cause(self, ev: GateEntered) -> str:
        """Human-readable ``cause`` string for the KillMsg (the Switch
        logs it; truncated to the wire cap downstream)."""
        if ev.gate_kind == GateKind.ROYAL_SEEDS:
            if self.open_world:
                return (
                    f"Locked: need all {ev.requirement} AP Royal Seeds and "
                    f"{self.palaces_required} palaces cleared to face Bowser")
            return f"Locked: need {ev.requirement} Royal Seeds to face Bowser"
        # BADGE gate: ev.requirement is the container-C internal_id, which
        # is exactly the key badge_table maps to the AP item name.  Name
        # the specific badge when we know it; fall back to the generic
        # phrasing for an unmapped bit (badge_table is incomplete -- see
        # its _BADGES list).
        name = badge_table.name_for_internal_id(ev.requirement)
        if name is not None:
            return f"Locked: missing the {name} for this challenge"
        return "Locked: missing the AP badge for this challenge"

    def _gate_check_stop(self, ev: GateEntered) -> str | None:
        """Return a human-readable reason the gate-kill loop should STOP,
        or ``None`` if it should keep counting down toward a kill.  Mirrors
        the precondition checks the loop made inline before it grew a
        per-second overlay countdown."""
        if not self.entry_gating_enabled:
            return "gating disabled"
        if ev.gate_kind == GateKind.BADGE and not self.badge_entry_gating_enabled:
            return "badge gating disabled"
        if not self.bridge_state.is_in_course():
            return "player left the course"
        cur = self.bridge_state.current_course
        if cur is None or cur.stage_key != ev.stage_key:
            return "player moved to a different course"
        if self._gate_requirement_met(ev):
            return "requirement now satisfied"
        if self.lan_server is None:
            return "no Switch bound"
        return None

    def _gate_overlay_text(self, ev: GateEntered, seconds_left: int) -> str:
        """The on-Switch overlay banner for a pending gate kill: a fixed
        "Level not in logic" headline, the live countdown, and the same
        human-readable detail the KillMsg carries."""
        return (
            "Level not in logic\n"
            f"Bouncing you out in {seconds_left}s\n"
            f"{self._gate_kill_cause(ev)}"
        )

    def _push_gate_overlay(self, ev: GateEntered, seconds_left: int) -> None:
        """Surface the countdown banner on the Switch overlay (forces it
        visible even while connected).  No-op if no Switch is bound."""
        if self.lan_server is None:
            return
        self.lan_server.send_overlay_notice(
            text=self._gate_overlay_text(ev, seconds_left),
            ttl_ms=GATE_OVERLAY_TTL_MS)

    def _clear_gate_overlay(self) -> None:
        """Clear any active overlay countdown banner (idempotent)."""
        if self.lan_server is not None:
            self.lan_server.send_overlay_notice(text="", ttl_ms=0)

    async def _gate_countdown(self, ev: GateEntered) -> bool:
        """Count the grace window down one second at a time, pushing an
        on-Switch overlay notice each tick so the player sees *why* they're
        about to be bounced.  Returns ``True`` if the loop should STOP (a
        precondition lapsed mid-countdown -- the overlay is cleared in that
        case), or ``False`` once the full window elapsed and the caller
        should fire the kill."""
        sk = ev.stage_key & 0xFFFFFFFF
        remaining = float(GATE_KILL_DELAY_S)
        while remaining > 0:
            reason = self._gate_check_stop(ev)
            if reason is not None:
                log.info("gate kill stage_key=0x%08x: %s; stopping", sk, reason)
                self._clear_gate_overlay()
                return True
            self._push_gate_overlay(ev, max(1, math.ceil(remaining)))
            step = 1.0 if remaining > 1.0 else remaining
            await asyncio.sleep(step)
            remaining -= step
        return False

    async def _gate_kill_loop(self, ev: GateEntered) -> None:
        """Count down the grace window (surfacing an on-Switch overlay
        countdown), then kill -- repeating every window while the player is
        still inside the gated course without the item.  Stops the moment
        any precondition lapses (left the course, moved courses, acquired
        the item, gating disabled, no Switch bound) or the safety cap is
        reached."""
        cause = self._gate_kill_cause(ev)
        sk = ev.stage_key & 0xFFFFFFFF
        kills = 0
        try:
            while True:
                if await self._gate_countdown(ev):
                    return
                # Re-check after the final countdown second so a precondition
                # that lapsed in that last second still aborts the kill.
                reason = self._gate_check_stop(ev)
                if reason is not None:
                    log.info("gate kill stage_key=0x%08x: %s; stopping",
                             sk, reason)
                    self._clear_gate_overlay()
                    return
                self.lan_server.send_kill(source="SMBW Gate", cause=cause)
                kills += 1
                log.warning("gate kill #%d sent for stage_key=0x%08x (%s)",
                            kills, sk, cause)
                if kills >= GATE_MAX_CONSECUTIVE_KILLS:
                    log.warning(
                        "gate kill stage_key=0x%08x: hit safety cap (%d); "
                        "stopping (re-arms on re-entry)",
                        sk, GATE_MAX_CONSECUTIVE_KILLS)
                    self._clear_gate_overlay()
                    return
        except asyncio.CancelledError:
            raise
        finally:
            self._clear_gate_overlay()
            if self._gate_kill_task is asyncio.current_task():
                self._gate_kill_task = None

    def _cancel_gate_kill(self) -> None:
        """Cancel any in-flight gate-kill loop (idempotent)."""
        task = self._gate_kill_task
        self._gate_kill_task = None
        if task is not None and not task.done():
            task.cancel()

    async def shutdown(self) -> None:  # type: ignore[override]
        """Cancel the gate-kill loop before the base shutdown so a
        sleeping task doesn't outlive the event loop."""
        self._cancel_gate_kill()
        await super().shutdown()

    # ---- Game-completion goal -----------------------------------------

    async def handle_goal_completed(self, goal: GoalCompleted) -> None:
        """LanServer dispatches this when the processor emits a
        GoalCompleted (Switch fired the SetFlagEnd...AfterClearedLastBoss
        Nerve at NSO+0x15b77a8 = Mario defeated final Bowser for the
        first time on this save).

        Sends a ``StatusUpdate`` with ``ClientStatus.CLIENT_GOAL`` only
        if the player's chosen goal is the Bowser one -- otherwise this
        Nerve is just a milestone and the actual goal (Wonder Gauntlet
        / WONDER?) is reached via ``handle_check_emitted`` matching the
        goal location name on a regular CheckEmitted.  Pre-slot_data
        fallback (no goal_location_name received): assume Bowser so
        legacy apworlds still goal-complete on Bowser kill.
        """
        if (self.goal_location_name is not None
                and self.goal_location_name != GOAL_LOCATION_NAME_BOWSER):
            log.info(
                "GameGoalReached Nerve fired (seq=%d) but goal is %r "
                "-- not signalling CLIENT_GOAL on Bowser kill",
                goal.seq, self.goal_location_name)
            return
        await self._send_client_goal(reason=f"GameGoalReached seq={goal.seq}")

    async def _send_client_goal(self, reason: str) -> None:
        """Send the StatusUpdate(CLIENT_GOAL) at most once per session.

        Both the M3.7 Bowser-Nerve path and the goal-location
        CheckEmitted path funnel through here; the AP server tolerates
        repeats but the bridge logs would be confusing if both fired
        for the same Bowser-goal seed.
        """
        if self._goal_status_sent:
            log.debug("CLIENT_GOAL already sent this session; "
                      "ignoring duplicate trigger (%s)", reason)
            return
        try:
            await self.send_msgs([{
                "cmd": "StatusUpdate",
                "status": ClientStatus.CLIENT_GOAL,
            }])
            self._goal_status_sent = True
            log.info("-> AP StatusUpdate(CLIENT_GOAL) (%s)", reason)
        except Exception:
            log.exception("StatusUpdate(CLIENT_GOAL) failed (%s)", reason)

    def _own_player_name(self) -> str:
        """Best-effort lookup of our own player name in the AP slot
        roster.  CommonContext populates ``player_names`` on Connected;
        before that, fall back to the configured ``auth`` slot name.
        Used for the self-ping guard in ``on_deathlink``."""
        try:
            return self.player_names[self.slot]
        except Exception:
            return self.auth or ""

    # ---- Outbound: LanServer's CheckEmitted callback ------------------

    async def handle_check_emitted(self, check: CheckEmitted) -> None:
        """Translate a CheckEmitted to an AP LocationChecks message."""
        # M2.3 probe-mode guard: each /badge_probe overwrites the
        # container-C bitfield, which the Switch's diff-on-overwrite
        # path then interprets as the player picking up the *previous*
        # probe's bit.  Suppress BADGE_ACQUIRED checks while a probe is
        # active so we don't spuriously tick off real AP locations
        # during exploration.  Clear the probe before genuine play
        # (`/badge_probe_clear`) to re-enable badge check emission.
        if (check.kind == CheckKind.BADGE_ACQUIRED
                and self._badge_probe_mask is not None):
            log.info(
                "badge probe active (mask=0x%x); suppressing AP "
                "LocationCheck for BADGE_ACQUIRED internal_id=%d",
                self._badge_probe_mask, check.stage_key)
            return
        name = lookup_name(check)
        if name is None:
            # A BADGE_ACQUIRED check for a course-granted badge has no AP
            # location by design (only shop badges are checks) -- drop it
            # quietly rather than nagging about a missing table entry.
            if (check.kind == CheckKind.BADGE_ACQUIRED
                    and badge_table.name_for_internal_id(check.stage_key)
                    is not None):
                log.debug(
                    "badge bit %d (%s) is course-granted, not an AP check; "
                    "dropping", check.stage_key,
                    badge_table.name_for_internal_id(check.stage_key))
                return
            log.info(
                "no AP location name for kind=%s stage_key=%d (table needs "
                "extending)",
                check.kind.value, check.stage_key)
            return
        # M3.7 goal-option respect: if this check is the player's chosen
        # goal location, send CLIENT_GOAL.  Done before the loc_id
        # lookup because the apworld wipes the goal location's address
        # in create_regions, so the regular LocationCheck path would
        # log a spurious warning and drop the message.  CLIENT_GOAL is
        # the authoritative signal; the LocationCheck attempt below is
        # best-effort.
        if (self.goal_location_name is not None
                and name == self.goal_location_name):
            log.info(
                "goal-location check emitted (%r); signalling CLIENT_GOAL",
                name)
            await self._send_client_goal(
                reason=f"goal-location check {name!r}")
        loc_id = self._location_name_to_id.get(name)
        if loc_id is None:
            log.warning(
                "AP server has no id for location %r -- DataPackage missing "
                "or game name mismatch (we declared game=%s)",
                name, self.game)
            return
        if loc_id in self._sent_loc_ids:
            log.debug("AP location %r (id=%d) already sent; skipping",
                      name, loc_id)
            return
        self._sent_loc_ids.add(loc_id)
        # Badge-shop AP ownership: a just-emitted shop-badge check means the
        # row should flip to SOLD OUT now.  Push optimistically off
        # ``_sent_loc_ids`` so it doesn't flicker back to purchasable during
        # the AP round-trip (the periodic tick / RoomUpdate would catch it
        # within ~2 s otherwise).
        if check.kind == CheckKind.BADGE_ACQUIRED:
            self._push_badge_shop_state()
        # TEN_COIN refund: the game already added 10 to the player's
        # flower_coin counter when the block was hit; AP is the sole
        # authority over what each 10-coin block actually grants, so
        # we subtract 10 here.  If the inbound item happens to be a
        # "10 Coin" routed back to this slot, the inbound +10 cancels
        # this -10 for a net +10 (player sees the same UX as vanilla);
        # if AP scattered the item to another slot, the refund stands
        # and the player gets the actual item rather than free coins.
        # Paired with the dedup above so we refund at most once per
        # in-game pickup per session.
        #
        # Gated on ``ten_coin_sanity``: when OFF, 10-coin blocks aren't
        # AP locations and no inbound item is coming back to net the
        # refund out -- subtracting here would just drain the player's
        # purple coins for free.
        if (check.kind == CheckKind.TEN_COIN
                and self.ten_coin_sanity_enabled):
            grant = coin_table.grant_for_item("10 Coin")
            if grant is not None and self.lan_server is not None:
                hash_, delta = grant
                log.info(
                    "TEN_COIN check fired -> increment_hash_keyed "
                    "hash=0x%08x delta=%d (refund in-game pickup)",
                    hash_, -delta)
                self.lan_server.send_increment_hash_keyed(hash_, -delta)
        try:
            await self.send_msgs([
                {"cmd": "LocationChecks", "locations": [loc_id]},
            ])
            log.info("-> AP LocationChecks %r (id=%d)", name, loc_id)
        except Exception:
            log.exception(
                "send_msgs(LocationChecks=[%d]) failed; will not retry",
                loc_id)
            self._sent_loc_ids.discard(loc_id)

    # ---- GUI plumbing -------------------------------------------------

    def make_gui(self):
        """Hook called by CommonContext.run_gui() to discover the
        Kivy manager class.  Lazy-imported so headless / --nogui runs
        never touch Kivy."""
        from .gui import SMBWManager
        return SMBWManager
