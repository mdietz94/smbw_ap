"""SMBW Archipelago bridge entry point.

Run with::

    python -m bridge \\
        --ap-host=localhost --ap-port=38281 --slot=MarioTest \\
        [--password=XXX] \\
        [--archipelago-path="C:/Users/me/Documents/Archipelago"] \\
        [--switch-host=0.0.0.0] [--switch-port=17777] \\
        [--discovery-port=17776] \\
        [--log-level=DEBUG]

The bridge runs three concurrent async tasks:

  1. ``LanServer`` on TCP :17777 -- accepts the Switch subsdk.
  2. ``DiscoveryResponder`` on UDP :17776 -- replies to discovery probes.
  3. ``server_loop`` (from CommonClient) -- maintains the AP websocket.

All three share a single asyncio event loop and a single
:class:`BridgeState` snapshot.  Ctrl-C exits cleanly.

The ``--archipelago-path`` flag is needed when Archipelago isn't
already on PYTHONPATH.  Default is to also try a sibling
``smo_archipelago/vendor/Archipelago`` checkout (matches the typical
dev environment); pass an explicit path if that doesn't work.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import signal
import sys
from pathlib import Path

from .discovery import DEFAULT_DISCOVERY_PORT, DiscoveryResponder
from .lan_server import LanServer
from .state import BridgeState


log = logging.getLogger("bridge")


def _default_archipelago_path() -> Path | None:
    """Look for an Archipelago checkout in well-known spots.

    Order: our own ``vendor/Archipelago`` (the supported path), then the
    sibling ``smo_archipelago/vendor/Archipelago`` (legacy fallback for
    devs who already had it).  Returns ``None`` if neither exists.
    """
    here = Path(__file__).resolve()
    candidates = [
        here.parent.parent / "vendor" / "Archipelago",
        here.parent.parent.parent / "smo_archipelago" / "vendor" / "Archipelago",
    ]
    for c in candidates:
        if (c / "CommonClient.py").is_file():
            return c
    return None


def _setup_archipelago_path(explicit: str | None) -> Path:
    """Insert Archipelago on sys.path before importing CommonClient.

    Order: explicit ``--archipelago-path`` -> default sibling lookup.
    Raises SystemExit with a clear message if neither resolves.
    """
    if explicit:
        p = Path(explicit).resolve()
    else:
        p = _default_archipelago_path()
    if p is None or not (p / "CommonClient.py").is_file():
        sys.stderr.write(
            "error: could not find Archipelago.  Pass --archipelago-path "
            "pointing at a directory containing CommonClient.py "
            "(e.g. .../smo_archipelago/vendor/Archipelago).\n"
        )
        raise SystemExit(2)
    sys.path.insert(0, str(p))
    return p


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog="python -m bridge",
        description="SMBW Archipelago LAN bridge (M4 MVP).",
    )
    p.add_argument(
        "--ap-host", default="localhost",
        help="AP server host (default: localhost)")
    p.add_argument(
        "--ap-port", type=int, default=38281,
        help="AP server port (default: 38281)")
    p.add_argument(
        "--slot", required=True,
        help="AP slot name (matches your YAML)")
    p.add_argument(
        "--password", default=None,
        help="AP server password (if room requires one)")
    p.add_argument(
        "--archipelago-path", default=None,
        help="Path to a checkout containing CommonClient.py.  Defaults "
             "to ../smo_archipelago/vendor/Archipelago if present.")
    p.add_argument(
        "--switch-host", default="0.0.0.0",
        help="LAN server bind host (default: 0.0.0.0)")
    p.add_argument(
        "--switch-port", type=int, default=17777,
        help="LAN server TCP port (default: 17777)")
    p.add_argument(
        "--discovery-port", type=int, default=DEFAULT_DISCOVERY_PORT,
        help=f"UDP discovery port (default: {DEFAULT_DISCOVERY_PORT})")
    p.add_argument(
        "--log-level", default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        help="logging level (default: INFO)")
    return p.parse_args(argv)


class _FlushingStreamHandler(logging.StreamHandler):
    """StreamHandler that flushes on every emit.  Without this, Python's
    block-buffered stdout when the bridge runs backgrounded (output
    redirected to a file) holds INFO/DEBUG lines indefinitely between
    flushes -- ERROR happens to flush more often but not always.
    Witnessed 2026-05-24 M4 first-run: bridge went silent in the log
    file from 21:22 onward even though the process was processing
    events normally."""

    def emit(self, record):  # type: ignore[override]
        super().emit(record)
        try:
            self.flush()
        except Exception:
            pass


def _setup_logging(level: str) -> None:
    fmt = "%(asctime)s %(name)s %(levelname)s %(message)s"
    formatter = logging.Formatter(fmt, datefmt="%H:%M:%S")

    # On-disk handler -- the most reliable sink.  Writes to a fixed path
    # next to the bridge module so the user can `Get-Content -Wait` it
    # regardless of how the bridge process was launched.  Captured pipes
    # via the harness silently buffer INFO/DEBUG levels even with
    # PYTHONUNBUFFERED + per-emit flush; a real disk file bypasses that.
    log_path = Path(__file__).resolve().parent.parent / "bridge.log"
    file_handler = logging.FileHandler(str(log_path), mode="w", encoding="utf-8")
    file_handler.setFormatter(formatter)

    stream_handler = _FlushingStreamHandler()
    stream_handler.setFormatter(formatter)

    # Replace any existing root handlers (an AP-side basicConfig may
    # have run earlier) with ours.  force=True is supported since 3.8.
    logging.basicConfig(
        level=getattr(logging, level),
        handlers=[file_handler, stream_handler],
        force=True,
    )
    # Quiet down noisy AP modules at INFO unless the user picked DEBUG.
    if level != "DEBUG":
        logging.getLogger("websockets").setLevel(logging.WARNING)

    logging.getLogger("bridge").info("logging to %s", log_path)


async def _run(args: argparse.Namespace) -> int:
    # CommonClient + ap_client imports must happen AFTER sys.path setup.
    from CommonClient import server_loop  # type: ignore

    from .ap_client import SMBWContext

    # AP's worlds.AutoWorldRegister scanning at import time resets the
    # root logger level to WARNING (CommonClient.py:1234 has a comment
    # acknowledging this and works around it the same way for the AP
    # TextClient).  Re-apply the user's requested level + re-attach our
    # handlers so INFO/DEBUG from inside the event loop actually shows.
    _setup_logging(args.log_level)

    state = BridgeState()
    server_address = f"{args.ap_host}:{args.ap_port}"
    ctx = SMBWContext(
        server_address=server_address,
        password=args.password,
        state=state,
    )
    ctx.auth = args.slot

    lan = LanServer(
        state=state,
        on_check_emitted=ctx.handle_check_emitted,
        badge_mask_provider=ctx._recompute_badge_mask,
        royal_seed_grants_provider=ctx._collect_royal_seed_grants,
    )
    ctx.lan_server = lan

    await lan.start(host=args.switch_host, port=args.switch_port)

    discovery = DiscoveryResponder(
        tcp_port=args.switch_port,
        port=args.discovery_port,
    )
    await discovery.start()

    # Wire Ctrl-C / SIGTERM into the AP context's exit_event so all
    # three tasks tear down cleanly together.  Windows doesn't support
    # add_signal_handler, so fall back to KeyboardInterrupt.
    loop = asyncio.get_running_loop()
    try:
        loop.add_signal_handler(signal.SIGTERM, ctx.exit_event.set)
        loop.add_signal_handler(signal.SIGINT, ctx.exit_event.set)
    except (NotImplementedError, AttributeError):
        pass  # Windows -- KeyboardInterrupt handles it

    log.info(
        "bridge up: AP=%s slot=%s LAN=%s:%d discovery=%d",
        server_address, args.slot,
        args.switch_host, args.switch_port, args.discovery_port,
    )

    server_loop_task = asyncio.create_task(
        server_loop(ctx), name="ap-server-loop")

    try:
        await ctx.exit_event.wait()
    except KeyboardInterrupt:
        log.info("interrupted; shutting down")
    finally:
        log.info("shutting down")
        server_loop_task.cancel()
        try:
            await server_loop_task
        except (asyncio.CancelledError, Exception):
            pass
        discovery.stop()
        await lan.stop()
        await ctx.shutdown()

    return 0


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    _setup_logging(args.log_level)
    ap_path = _setup_archipelago_path(args.archipelago_path)
    log.info("using Archipelago at %s", ap_path)
    try:
        return asyncio.run(_run(args))
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
