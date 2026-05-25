"""SMBWContext: CommonContext subclass for the SMBW Archipelago client.

Ported from the headless `bridge/ap_client.py`.  Key changes:
  - game name updated to first-class "SMBWonder" (no longer Manual_-prefixed).
  - `make_gui()` returns the Kivy `SMBWManager` subclass so Archipelago
    Launcher spawns a Kivy window with our custom Status + log tabs.
  - All "SMBW" log lines (lan_server, processor, this module) route
    through the `SMBW` logger which `SMBWManager.logging_pairs`
    surfaces as a dedicated tab.

Inbound (AP -> Switch):
  - Badges: maintain a canonical badge mask derived from
    :attr:`items_received`.  On every ``ReceivedItems``, recompute the
    mask and push it to the Switch as a ``SetBadgesAbsolute`` (the LAN
    server also re-pushes on HelloMsg and on a ~2 s tick).
  - Royal Seeds: per-item ``GrantHashKeyed`` (container-A path).
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
from typing import Any

from CommonClient import CommonContext  # type: ignore
from NetUtils import ClientStatus  # type: ignore

from . import badge_table
from . import royal_seed_table
from .commands import SMBWCommandProcessor
from .location_table import lookup_name
from .protocol import CheckEmitted
from .state import BridgeState


log = logging.getLogger("SMBW")


# Must match the new SMBWonderWorld's `game` slug (see
# apworld/smbw_archipelago/Game.py).
GAME_NAME = "SMBWonder"


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

    def _recompute_badge_mask(self) -> int:
        """Walk :attr:`items_received` and compute the absolute owned-badge
        bitmask -- one bit per known badge item.  Items the badge table
        doesn't recognize contribute 0 bits (silent drop).  This is what
        the LAN server pushes to the Switch as the authoritative badge
        set on HelloMsg, on a periodic tick, and after every
        ReceivedItems."""
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

    async def _handle_received_items(self, args: dict) -> None:
        items = args.get("items", []) or []

        new_mask = self._recompute_badge_mask()
        if self.lan_server is not None:
            log.info("ReceivedItems: badge mask now 0x%x", new_mask)
            self.lan_server.send_set_badges_absolute(new_mask)
        else:
            log.debug(
                "no lan_server bound; not forwarding badge mask 0x%x",
                new_mask)

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
                continue
            if royal_seed_table.is_royal_seed_item(item_name):
                hash_ = royal_seed_table.hash_for_item(item_name)
                if hash_ is None:
                    log.warning(
                        "royal_seed_table inconsistency: "
                        "is_royal_seed_item(%r)=True but no hash", item_name)
                    continue
                log.info(
                    "item received: %r (id=%s) -> grant_hash_keyed "
                    "hash=0x%08x value=%d",
                    item_name, item_id, hash_,
                    royal_seed_table.ROYAL_SEED_VALUE)
                if self.lan_server is None:
                    log.debug("no lan_server bound; cannot forward grant")
                    continue
                self.lan_server.send_grant_hash_keyed(
                    hash_, royal_seed_table.ROYAL_SEED_VALUE)
            else:
                log.info(
                    "received unhandled item %r (id=%s); ignoring "
                    "(no table entry)", item_name, item_id)

    # ---- Outbound: LanServer's CheckEmitted callback ------------------

    async def handle_check_emitted(self, check: CheckEmitted) -> None:
        """Translate a CheckEmitted to an AP LocationChecks message."""
        name = lookup_name(check)
        if name is None:
            log.info(
                "no AP location name for kind=%s stage_key=%d (table needs "
                "extending)",
                check.kind.value, check.stage_key)
            return
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
