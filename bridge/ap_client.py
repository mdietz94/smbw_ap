"""Archipelago client glue: ``SMBWContext`` subclasses CommonContext
and bridges between the AP MultiServer protocol and the LAN server.

Inbound (AP -> Switch):
  - ``ReceivedItems`` -> resolve each item id to a name -> look up in
    :mod:`badge_table` -> ``LanServer.send_grant_badge`` -> Switch.
  - Items the table doesn't recognize are logged + dropped (M4 ships
    badges only; M5 will extend with power-ups / characters etc.).

Outbound (Switch -> AP):
  - The LanServer calls :meth:`SMBWContext.handle_check_emitted` for
    every CheckEmitted the processor produces.  We resolve to an AP
    location ID via :mod:`location_table` + the connected
    ``location_names`` and ``send_msgs([{"cmd": "LocationChecks", ...}])``.

Headless: this module does NOT subclass any Kivy GUI helpers.  CommonContext's
``run_gui`` is a no-op when not subclassed -- the bridge runs entirely
in a single asyncio event loop.

Importing this module requires Archipelago's ``CommonClient`` to be on
``sys.path``.  The bridge entry point (:mod:`bridge.__main__`) handles
that via ``--archipelago-path`` before importing this module.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

# These imports require Archipelago on sys.path.  bridge.__main__
# ensures that before importing us; if you're importing ap_client
# directly from a script, set up sys.path first.
from CommonClient import CommonContext  # type: ignore
from NetUtils import ClientStatus  # type: ignore

from . import badge_table
from .location_table import lookup_name
from .protocol import CheckEmitted
from .state import BridgeState


log = logging.getLogger(__name__)


# Must match ``manual_smbwonder_zim/Game.py``: ``game_name = "Manual_" +
# game_table["game"] + "_" + game_table["creator"]`` (data/game.json has
# game="SMBWonder" and creator="Zim").
GAME_NAME = "Manual_SMBWonder_Zim"


class SMBWContext(CommonContext):
    """CommonContext that ties the LAN server to the AP server.

    Construction is two-phase: the context is created early so the AP
    auth loop can start; the ``lan_server`` reference is wired in by
    :mod:`bridge.__main__` after both objects exist.  Until then
    item-grant routing is silently dropped, which is fine because no
    Switch is connected anyway.
    """

    # CommonContext reads this to populate the Connect package's
    # ``game`` field and to gate datapackage downloads.
    game = GAME_NAME
    items_handling = 0b111  # all items via AP (no local progression)

    def __init__(
        self,
        server_address: str | None,
        password: str | None,
        state: BridgeState,
    ) -> None:
        super().__init__(server_address, password)
        self.bridge_state = state
        self.lan_server: Any = None  # set post-construction; LanServer
        # Reverse name->id map for our own game.  CommonContext stores
        # id->name; we build the inverse on DataPackage receipt.
        self._location_name_to_id: dict[str, int] = {}
        self._item_name_to_id: dict[str, int] = {}
        # Track which AP location ids the bridge has already shipped on
        # this AP session so a re-fire (e.g. PlayReport replay after a
        # Switch reconnect) doesn't generate redundant LocationChecks
        # messages.  BridgeState dedups by (kind, stage_key); this
        # complements it at the AP-id level for defense in depth.
        self._sent_loc_ids: set[int] = set()

    # ---- AP lifecycle overrides ---------------------------------------

    async def server_auth(self, password_requested: bool = False) -> None:
        if password_requested and not self.password:
            log.warning(
                "AP server requested a password but none configured "
                "(pass --password)")
        await self.get_username()
        await self.send_connect()

    def on_package(self, cmd: str, args: dict) -> None:
        # super() runs first so CommonContext's bookkeeping
        # (items_received, checked_locations, item_names, ...) is up to
        # date before our handlers read it.
        super().on_package(cmd, args)
        asyncio.create_task(self._handle_ap_package(cmd, args))

    async def _handle_ap_package(self, cmd: str, args: dict) -> None:
        if cmd == "Connected":
            log.info(
                "AP connected: slot=%s player=%s seed=%s",
                self.auth, self.slot, self.seed_name)
            # CommonContext already has the location_names + item_names
            # populated by this point (either from a fresh DataPackage
            # or from on-disk cache).  Build our name->id reverse maps
            # from those instead of relying on a fresh DataPackage
            # event firing -- when the server's package matches our
            # cached checksum, no DataPackage event arrives and our
            # reverse map would stay empty (witnessed 2026-05-24:
            # 'no AP id for location' WARN on every CheckEmitted).
            self._rebuild_reverse_maps_from_context()
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
            # Server pushed a fresh package -- super() already merged
            # it into self.location_names / self.item_names; just refresh
            # our reverse maps from there.
            self._rebuild_reverse_maps_from_context()
            return

        if cmd == "ReceivedItems":
            await self._handle_received_items(args)
            return

    def _rebuild_reverse_maps_from_context(self) -> None:
        """Build name->id reverse maps from CommonContext's id->name
        tables.  Idempotent; called on Connected and on DataPackage."""
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

    async def _handle_received_items(self, args: dict) -> None:
        items = args.get("items", []) or []
        for it in items:
            item_id = it.get("item") if isinstance(it, dict) else None
            if item_id is None:
                # NetworkItem dataclass or some other shape; ducktype it.
                item_id = getattr(it, "item", None)
            if item_id is None:
                log.warning("ReceivedItems entry without item id: %r", it)
                continue
            try:
                item_name = self.item_names.lookup_in_game(int(item_id))
            except Exception as e:
                log.warning("can't resolve item id %r: %s", item_id, e)
                continue
            if not badge_table.is_badge_item(item_name):
                log.info(
                    "received non-badge item %r (id=%s); ignoring (M4 only "
                    "handles badges)", item_name, item_id)
                continue
            internal_id = badge_table.grant_internal_id_for_item(item_name)
            if internal_id is None:
                # is_badge_item returned True so this shouldn't happen,
                # but belt-and-braces.
                log.warning(
                    "badge_table inconsistency: is_badge_item(%r)=True but "
                    "no internal_id available", item_name)
                continue
            log.info(
                "item received: %r (id=%s) -> grant_badge internal_id=%d",
                item_name, item_id, internal_id)
            if self.lan_server is None:
                log.debug("no lan_server bound; cannot forward grant")
                continue
            self.lan_server.send_grant_badge(internal_id)

    # ---- Outbound: LanServer's CheckEmitted callback ------------------

    async def handle_check_emitted(self, check: CheckEmitted) -> None:
        """Translate a CheckEmitted to an AP LocationChecks message.

        Logs and drops if the location isn't in our table yet, or if the
        AP server hasn't sent us a DataPackage that maps the name.  Both
        cases are normal during early M4 (the W1-only table will be
        extended; the AP server pushes the datapackage right after
        Connect).
        """
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
            # Drop from sent set so a retry succeeds if connectivity
            # comes back.
            self._sent_loc_ids.discard(loc_id)
