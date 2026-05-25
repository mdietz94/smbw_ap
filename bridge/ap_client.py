"""Archipelago client glue: ``SMBWContext`` subclasses CommonContext
and bridges between the AP MultiServer protocol and the LAN server.

Inbound (AP -> Switch):
  - Badges: maintain a canonical ``_badge_mask`` derived from
    :attr:`items_received` (each badge item -> bit position via
    :mod:`badge_table`).  On every ``ReceivedItems``, recompute the
    mask and push it to the Switch as a ``SetBadgesAbsolute`` (the
    LAN server also re-pushes on HelloMsg and on a ~2 s tick -- AP
    is the sole authority over the badge pool, see
    docs/m4-followups.md and the lan_server module docstring).
  - Royal Seeds: per-item ``GrantHashKeyed`` (container-A path).
  - Items neither table recognizes are logged + dropped (M4 ships
    badges + seeds; M5 will extend with power-ups / characters).

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
from . import royal_seed_table
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

    def _recompute_badge_mask(self) -> int:
        """Walk :attr:`items_received` and compute the absolute owned-
        badge bitmask -- one bit per known badge item.  Items the badge
        table doesn't recognize contribute 0 bits (silent drop, same
        failure mode as the prior per-item grant path).  This is what
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

    def _collect_royal_seed_grants(self) -> list[tuple[int, int]]:
        """Walk :attr:`items_received` and return ``[(hash, value), ...]``
        for every Royal Seed AP item.  Deduplicated by hash (the Switch
        primitive is idempotent for bool writes, but AP may legitimately
        list the same item twice in items_received and there's no value
        in sending the wire message twice).  This is the catch-up batch
        the LAN server replays on every Switch HelloMsg so seeds survive
        Switch reconnect and save/reload -- M4.5."""
        seen: set[int] = set()
        out: list[tuple[int, int]] = []
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
            if not royal_seed_table.is_royal_seed_item(item_name):
                continue
            h = royal_seed_table.hash_for_item(item_name)
            if h is None or h in seen:
                continue
            seen.add(h)
            out.append((h, royal_seed_table.ROYAL_SEED_VALUE))
        return out

    async def _handle_received_items(self, args: dict) -> None:
        items = args.get("items", []) or []

        # Recompute + push the badge mask once per ReceivedItems batch.
        # super().on_package already merged the new items into
        # self.items_received before this coroutine ran, so
        # _recompute_badge_mask sees the new state.
        new_mask = self._recompute_badge_mask()
        if self.lan_server is not None:
            log.info("ReceivedItems: badge mask now 0x%x", new_mask)
            self.lan_server.send_set_badges_absolute(new_mask)
        else:
            log.debug(
                "no lan_server bound; not forwarding badge mask 0x%x",
                new_mask)

        # Per-item routing for non-badge items (Royal Seeds today; M5
        # will extend to power-ups + characters).  Badges are handled by
        # the mask above; we skip them here to avoid double work.
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
                # Already covered by the absolute-mask push above.
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
