"""Process-level guards and diagnostics: asyncua array lengths, loop stalls, RSS."""

from __future__ import annotations

import asyncio
import contextvars
import faulthandler
import functools
import logging
import sys
import time
from collections.abc import Callable
from typing import Any

from asyncua.common.utils import Buffer, NotEnoughData
from asyncua.ua import ua_binary

try:
    import resource
except ImportError:  # Windows
    resource = None  # type: ignore[assignment]

logger = logging.getLogger(__name__)

# Server whose task decodes; asyncua's tasks (publish loop) inherit it.
SERVER: contextvars.ContextVar[str] = contextvars.ContextVar("opcua_server", default="-")

STALL_TIMEOUT = 10.0
RSS_HIGH_WATER = 2 << 30

_array_warned: set[str] = set()


class ArrayLengthError(NotEnoughData):
    """A decoded array length exceeds the bytes left in the message."""


def _check_array_length(data: Any, uatype: Any) -> None:
    if not isinstance(data, Buffer) or len(data) < 4:
        return
    length = ua_binary.Primitives.Int32.unpack(data.copy(4))
    remaining = len(data) - 4
    if length <= remaining:
        return
    server = SERVER.get()
    if server not in _array_warned:
        _array_warned.add(server)
        logger.warning(
            "[%s] Rejected a response: %s array length %d exceeds the %d bytes left in "
            "the message - further reports suppressed",
            server,
            getattr(uatype, "name", None) or getattr(uatype, "__name__", uatype),
            length,
            remaining,
        )
    raise ArrayLengthError(f"array length {length} exceeds {remaining} bytes remaining")


def _guarded(factory: Callable[[Any], Callable[[Any], Any]]) -> Callable[[Any], Any]:
    @functools.cache
    def make(uatype: Any) -> Callable[[Any], Any]:
        inner = factory(uatype)

        def deserialize(data: Any) -> Any:
            _check_array_length(data, uatype)
            return inner(data)

        return deserialize

    return make


def _install_array_guard() -> None:
    """Array elements are >= 1 byte on the wire, except Null (0), so a length past the
    bytes left is garbage. asyncua 2.0.1 trusts it: a Null-element array allocates one
    list slot per count (one garbage length froze the loop at 10 GB RSS).
    """
    if getattr(ua_binary, "_zelos_array_guard", False):
        return
    for name in ("_create_uatype_array_deserializer", "_create_list_deserializer"):
        setattr(ua_binary, name, _guarded(getattr(ua_binary, name)))
    # Deserializers built before the patch captured the unguarded factories.
    ua_binary._create_type_deserializer.cache_clear()
    ua_binary._create_dataclass_deserializer.cache_clear()
    ua_binary._zelos_array_guard = True


_install_array_guard()


def peak_rss_bytes() -> int | None:
    """Peak resident set size of this process; None where unavailable (Windows)."""
    if resource is None:
        return None
    peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return peak if sys.platform == "darwin" else peak * 1024  # Linux reports KiB


async def watch_loop(
    describe: Callable[[], str], stall_timeout: float = STALL_TIMEOUT, period: float = 1.0
) -> None:
    """Dump every thread's stack to stderr if the loop blocks past `stall_timeout`;
    WARN when peak RSS crosses RSS_HIGH_WATER, then each doubling. Runs until cancelled.

    `describe`: per-server node and subscription counts for the RSS warning.
    """
    try:
        faulthandler.dump_traceback_later(stall_timeout, repeat=False, file=sys.stderr)
    except (AttributeError, ValueError, OSError) as e:  # stderr without a file descriptor
        logger.warning("Loop stall watchdog unavailable: %s", e)
        return
    logger.info(
        "Loop stall watchdog armed: thread stacks go to stderr if blocked > %.0fs", stall_timeout
    )
    rss_mark = RSS_HIGH_WATER
    try:
        while True:
            before = time.monotonic()
            await asyncio.sleep(period)
            blocked = time.monotonic() - before - period
            if blocked > stall_timeout:
                logger.warning("Event loop was blocked %.1fs; thread stacks dumped above", blocked)
            faulthandler.dump_traceback_later(stall_timeout, repeat=False, file=sys.stderr)
            rss = peak_rss_bytes()
            if rss is not None and rss >= rss_mark:
                logger.warning("Peak RSS %d MB; %s", rss >> 20, describe())
                while rss_mark <= rss:
                    rss_mark *= 2
    finally:
        faulthandler.cancel_dump_traceback_later()
