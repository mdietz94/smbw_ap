"""SMBW Archipelago Client entry point.

Run via Archipelago Launcher's "SMBW Client" button, or directly:

    python -m apworld.smbw_archipelago.client.main \\
        --connect=localhost:38281 --name=Mario [--password=XXX] \\
        [--switch-host=0.0.0.0] [--switch-port=17777] \\
        [--discovery-port=17776] [--nogui]

The client runs three concurrent async tasks sharing one event loop:

  1. ``LanServer`` on TCP :17777 -- accepts the Switch subsdk.
  2. ``DiscoveryResponder`` on UDP :17776 -- replies to discovery probes.
  3. ``server_loop`` (from CommonClient) -- maintains the AP websocket.

With Kivy enabled (the default when ``stdout`` is a TTY or the Launcher
spawns us), the SMBWManager Kivy app also runs on the asyncio loop via
``ctx.run_gui()``.  Otherwise the bridge is headless and a thin
``run_cli()`` reads stdin for slash commands.

All four exit together when ``ctx.exit_event`` fires (Ctrl-C, GUI close,
or AP `disconnect` package).
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import sys

import Utils
from CommonClient import server_loop, get_base_parser  # type: ignore

from .context import SMBWContext
from .discovery import DEFAULT_DISCOVERY_PORT, DiscoveryResponder
from .lan_server import LanServer
from .state import BridgeState
from .._setup.build_id import expected_mod_source_id


DEFAULT_SWITCH_HOST = "0.0.0.0"
DEFAULT_SWITCH_PORT = 17777


log = logging.getLogger("SMBW")


def _apworld_version() -> str:
    """``__version__`` of the enclosing world package ("0.0.0+dev" on a
    checkout, the tag on a release).  Imported lazily: the package
    ``__init__`` pulls in the whole AP world graph, which the client does
    not otherwise need at import time."""
    try:
        from .. import __version__  # type: ignore[attr-defined]
        return str(__version__)
    except Exception:
        return "unknown"


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """Parse CLI args.  Re-uses Archipelago's `get_base_parser` for the
    standard --connect / --password / --nogui flags, then adds our own."""
    parser = get_base_parser(description="SMBW Archipelago Client (M4).")
    parser.add_argument(
        "--name", default=None,
        help="Slot name to connect as (AP YAML slot).")
    parser.add_argument(
        "--switch-host", default=DEFAULT_SWITCH_HOST,
        help=f"LAN bridge bind host (default: {DEFAULT_SWITCH_HOST}).")
    parser.add_argument(
        "--switch-port", type=int, default=DEFAULT_SWITCH_PORT,
        help=f"LAN bridge TCP port (default: {DEFAULT_SWITCH_PORT}).")
    parser.add_argument(
        "--discovery-port", type=int, default=DEFAULT_DISCOVERY_PORT,
        help=f"UDP discovery port (default: {DEFAULT_DISCOVERY_PORT}).")
    parser.add_argument(
        "--log-level", default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        help="logging level (default: INFO)")
    return parser.parse_args(argv)


async def _main(args: argparse.Namespace) -> int:
    state = BridgeState()
    ctx = SMBWContext(
        server_address=args.connect,
        password=args.password,
        state=state,
    )
    if args.name:
        ctx.auth = args.name

    # Our own identity, logged once so a pasted client log says which
    # apworld was running, and handed to the LAN server so the Switch's
    # Hello mod_ver can be checked against it (stale-subsdk9 warning).
    expected_mod_source = expected_mod_source_id()
    log.info(
        "SMBW Client apworld version %s; expected mod source id: %s",
        _apworld_version(), expected_mod_source or "unknown")

    lan = LanServer(
        state=state,
        expected_mod_source=expected_mod_source,
        on_check_emitted=ctx.handle_check_emitted,
        on_death_reported=ctx.handle_death_reported,
        on_goal_completed=ctx.handle_goal_completed,
        on_gate_entered=ctx.handle_gate_entered,
        badge_mask_provider=ctx._recompute_badge_mask,
        wonder_seed_counts_provider=ctx._recompute_wonder_seed_counts,
        wonder_seed_bits_provider=ctx._recompute_wonder_seed_bits,
        routable_worlds_provider=ctx._recompute_routable_worlds_mask,
        force_cleared_courses_provider=ctx._recompute_force_cleared_mask,
        open_world_royal_seed_provider=ctx._open_world_royal_seed_mask,
        world_unlock_hashes_provider=ctx._world_unlock_hashes,
        itemget_deny_provider=ctx._recompute_itemget_deny_mask,
        unlocked_charas_provider=ctx._recompute_unlocked_chara_mask,
        badge_shop_state_provider=ctx._recompute_badge_shop_state,
        seed_shop_state_provider=ctx._recompute_seed_shop_state,
        badge_shop_text_provider=ctx._recompute_badge_shop_text,
    )
    ctx.lan_server = lan

    await lan.start(host=args.switch_host, port=args.switch_port)

    discovery = DiscoveryResponder(
        tcp_port=args.switch_port,
        port=args.discovery_port,
    )
    await discovery.start()

    log.info(
        "SMBW Client up: AP=%s name=%s LAN=%s:%d discovery=%d gui=%s",
        args.connect, args.name,
        args.switch_host, args.switch_port, args.discovery_port,
        Utils.gui_enabled,
    )

    ctx.server_task = asyncio.create_task(server_loop(ctx), name="ap-server-loop")

    if Utils.gui_enabled:
        ctx.run_gui()
    ctx.run_cli()

    try:
        await ctx.exit_event.wait()
    finally:
        log.info("SMBW Client shutting down")
        try:
            discovery.stop()
        except Exception:
            log.exception("discovery.stop() raised")
        try:
            await lan.stop()
        except Exception:
            log.exception("lan.stop() raised")
        await ctx.shutdown()

    return 0


def launch(*launch_args: str) -> None:
    """Archipelago Launcher entry point.  Called by the apworld's
    ``launch_smbw_client`` Component wrapper.  Also usable as
    ``python -m apworld.smbw_archipelago.client.main``."""
    Utils.init_logging("SMBWClient", exception_logger="Client")
    args = parse_args(list(launch_args))
    # Re-apply log level after Utils.init_logging baselines it -- AP's
    # AutoWorldRegister scan resets root level to WARNING on import.
    logging.getLogger().setLevel(getattr(logging, args.log_level))
    try:
        asyncio.run(_main(args))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    launch(*sys.argv[1:])
