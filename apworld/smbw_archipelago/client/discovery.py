"""UDP bridge-discovery responder.

Ported from smo_archipelago/apworld/smo_archipelago/client/discovery.py.
The wire format is intentionally compatible with smo so a single bridge
process could serve both projects on the same LAN (different TCP
sessions, shared UDP responder).  We dropped only smo's ``seed`` field
in the reply -- SMBW doesn't need AP-seed verification at the discovery
layer.

The Switch mod sends a small JSON probe to one of:
  * ``127.0.0.1:17776`` (Ryujinx-on-same-host, tried first)
  * ``255.255.255.255:17776`` (LAN broadcast)
  * baked-in fallback IP ``:17776`` (last resort, from ``BRIDGE_HOST_STRING``)

We bind a UDP socket on ``0.0.0.0:17776``, accept any delivery path,
and unicast a reply telling the Switch where the TCP LanServer is
listening.  The reply's ``host`` field comes from
:func:`net_util.detect_lan_ip` so the Switch always gets a routable
address (even when the probe arrived on loopback / broadcast).

Wire format (newline-terminated UTF-8 JSON, matching the TCP channel):

    probe:  ``{"t":"discover","mod_ver":"<x>"}\\n``
    reply:  ``{"t":"bridge","host":"<ipv4>","port":<tcp_port>}\\n``

Bind failure (port already in use, etc.) is logged at WARN and the
responder no-ops -- the Switch can still TCP-connect directly via the
``BRIDGE_HOST_STRING`` fallback baked at compile time.
"""

from __future__ import annotations

import asyncio
import ctypes
import json
import logging
import socket
import sys
from typing import Callable

from .net_util import detect_lan_ip


log = logging.getLogger("SMBW")


DEFAULT_DISCOVERY_PORT = 17776
MAX_PROBE_BYTES = 512  # probes are tiny; cap defensively


def _disable_udp_connreset_win32(sock: socket.socket) -> None:
    """Disable WSAECONNRESET on a UDP socket via WSAIoctl(SIO_UDP_CONNRESET).

    Python's ``socket.ioctl()`` Win32 whitelist doesn't include
    SIO_UDP_CONNRESET, so we call WSAIoctl directly via ctypes.  Caller
    catches OSError if the call fails -- we don't want a ctypes edge
    case to take down the whole responder.
    """
    SIO_UDP_CONNRESET = 0x9800000C
    ws2 = ctypes.WinDLL("ws2_32")
    LPDWORD = ctypes.POINTER(ctypes.c_ulong)
    ws2.WSAIoctl.argtypes = [
        ctypes.c_void_p,                      # SOCKET s
        ctypes.c_uint32,                      # DWORD dwIoControlCode
        ctypes.c_void_p, ctypes.c_uint32,     # in buf + size
        ctypes.c_void_p, ctypes.c_uint32,     # out buf + size
        LPDWORD,                              # bytes returned
        ctypes.c_void_p, ctypes.c_void_p,     # overlapped + completion
    ]
    ws2.WSAIoctl.restype = ctypes.c_int
    enable = ctypes.c_uint32(0)  # FALSE = suppress ECONNRESET on UDP
    out_size = ctypes.c_ulong(0)
    rc = ws2.WSAIoctl(
        sock.fileno(),
        SIO_UDP_CONNRESET,
        ctypes.byref(enable), 4,
        None, 0,
        ctypes.byref(out_size),
        None, None,
    )
    if rc != 0:
        ws2.WSAGetLastError.restype = ctypes.c_int
        wsa_err = ws2.WSAGetLastError()
        raise OSError(
            wsa_err,
            f"WSAIoctl(SIO_UDP_CONNRESET) failed: WSA error {wsa_err}")


class _ResponderProtocol(asyncio.DatagramProtocol):
    def __init__(
        self,
        tcp_port: int,
        get_lan_ip: Callable[[], str] = detect_lan_ip,
    ) -> None:
        self._tcp_port = tcp_port
        self._get_lan_ip = get_lan_ip
        self._transport: asyncio.DatagramTransport | None = None
        # Cache the LAN IP for the responder's lifetime.  detect_lan_ip
        # opens a UDP socket per call; the value doesn't change between
        # bridge restarts.
        self._lan_ip = self._get_lan_ip()

    def connection_made(self, transport: asyncio.BaseTransport) -> None:
        self._transport = transport  # type: ignore[assignment]

    def datagram_received(self, data: bytes, addr: tuple) -> None:
        log.info("probe received from %s (%d bytes)", addr, len(data))
        if len(data) > MAX_PROBE_BYTES:
            log.debug("oversized probe (%d bytes) from %s; ignoring",
                      len(data), addr)
            return
        try:
            msg = json.loads(data.decode("utf-8", errors="replace"))
        except Exception:
            log.warning("malformed JSON from %s: %r", addr, data[:80])
            return
        if not isinstance(msg, dict) or msg.get("t") != "discover":
            log.debug("probe from %s wasn't t=discover: %r", addr, msg)
            return
        reply = {
            "t": "bridge",
            "host": self._lan_ip,
            "port": self._tcp_port,
        }
        payload = (json.dumps(reply, separators=(",", ":")) + "\n").encode("utf-8")
        try:
            assert self._transport is not None
            self._transport.sendto(payload, addr)
            log.info(
                "replied to %s (host=%s port=%d)",
                addr, self._lan_ip, self._tcp_port)
        except Exception:
            log.exception("sendto failed (addr=%s)", addr)

    def error_received(self, exc: Exception) -> None:
        # Per-datagram errors aren't fatal; just log at debug.
        log.debug("datagram error: %r", exc)


class DiscoveryResponder:
    """UDP bridge-discovery responder.  One per bridge process."""

    def __init__(
        self,
        tcp_port: int,
        bind_host: str = "0.0.0.0",
        port: int = DEFAULT_DISCOVERY_PORT,
        get_lan_ip: Callable[[], str] = detect_lan_ip,
    ) -> None:
        self._tcp_port = tcp_port
        self._bind_host = bind_host
        self._port = port
        self._get_lan_ip = get_lan_ip
        self._transport: asyncio.DatagramTransport | None = None
        self._protocol: _ResponderProtocol | None = None

    async def start(self) -> bool:
        """Bind the UDP socket and start listening.  Returns ``True`` on
        success, ``False`` on bind failure (logged at WARN).

        On failure the bridge keeps running -- the Switch can still
        TCP-connect directly via ``BRIDGE_HOST_STRING``.

        We create + bind the raw socket ourselves (rather than passing
        ``local_addr=`` to ``create_datagram_endpoint``) for two reasons:

        1. The Windows-only ``WSAECONNRESET`` ioctl below requires a
           raw ``socket.socket`` -- asyncio's TransportSocket wrapper
           doesn't expose ioctl.
        2. ``SO_REUSEADDR`` lets a previous crashed process release the
           port without TIME_WAIT, and must be set before bind.
        """
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.bind((self._bind_host, self._port))
        except OSError as e:
            sock.close()
            log.warning(
                "failed to bind UDP %s:%d (%s) -- auto-discovery disabled "
                "this session; the Switch must use the baked-in "
                "BRIDGE_HOST_STRING fallback to find us.",
                self._bind_host, self._port, e,
            )
            return False

        # Windows-only WSAECONNRESET suppression.  Without this, when
        # our reply sendto() hits an already-closed ephemeral port (which
        # Ryujinx tears down between probe and reply on every reconnect
        # cycle), Windows returns WSAECONNRESET on the next recv and
        # asyncio's DatagramTransport surfaces it via error_received,
        # silently dropping subsequent inbound datagrams.  Real hardware
        # on a normal LAN rarely trips this, but Ryujinx in the dev loop
        # does on every reconnect.
        if sys.platform == "win32":
            try:
                _disable_udp_connreset_win32(sock)
            except OSError as e:
                log.warning(
                    "failed to disable WSAECONNRESET (%s) -- the UDP "
                    "socket may stop accepting probes after the first "
                    "ICMP-unreachable bounce.", e,
                )
            except Exception as e:
                log.warning(
                    "WSAIoctl ctypes call raised (%r) -- WSAECONNRESET "
                    "poisoning hazard is not suppressed this session.", e,
                )

        loop = asyncio.get_running_loop()
        try:
            transport, protocol = await loop.create_datagram_endpoint(
                lambda: _ResponderProtocol(self._tcp_port, self._get_lan_ip),
                sock=sock,
            )
        except Exception as e:
            sock.close()
            log.warning("create_datagram_endpoint failed: %r", e)
            return False
        self._transport = transport
        self._protocol = protocol  # type: ignore[assignment]
        log.info(
            "listening on UDP %s:%d (replies advertise TCP %s:%d)",
            self._bind_host, self._port,
            getattr(self._protocol, "_lan_ip", "?"), self._tcp_port,
        )
        return True

    def stop(self) -> None:
        if self._transport is not None:
            try:
                self._transport.close()
            except Exception:
                pass
            self._transport = None
        self._protocol = None
