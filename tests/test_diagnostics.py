"""Array-length guard on asyncua decoding and the loop stall watchdog."""

from __future__ import annotations

import subprocess
import sys
import textwrap
import time

import pytest
from asyncua import ua
from asyncua.common.utils import Buffer
from asyncua.ua import ua_binary

from zelos_extension_opcua.diagnostics import ArrayLengthError, peak_rss_bytes


@pytest.mark.parametrize(
    ("decode", "prefix"),
    [
        # Variant array of Null: 0 bytes per element, the incident's shape
        (ua_binary.variant_from_binary, b"\x80"),
        # Structure list, e.g. ReadResponse.Results
        (lambda buf: ua_binary.from_binary(list[ua.DataValue], buf), b""),
    ],
    ids=["variant-null-array", "struct-list"],
)
def test_garbage_array_length_rejected_before_allocating(decode, prefix):
    data = Buffer(prefix + (2**31 - 1).to_bytes(4, "little") + b"\x00" * 8)
    rss, start = peak_rss_bytes(), time.monotonic()
    with pytest.raises(ArrayLengthError):
        decode(data)
    assert time.monotonic() - start < 0.5
    if rss is not None:
        assert peak_rss_bytes() - rss < 50 << 20


def test_stall_watchdog_dumps_stacks():
    script = textwrap.dedent(
        """
        import asyncio, logging, time
        from zelos_extension_opcua.diagnostics import watch_loop

        async def main():
            task = asyncio.create_task(watch_loop(lambda: "", stall_timeout=1.0, period=0.1))
            await asyncio.sleep(0.3)
            time.sleep(2)
            await asyncio.sleep(0.3)
            task.cancel()

        logging.basicConfig()
        asyncio.run(main())
        """
    )
    out = subprocess.run(
        [sys.executable, "-c", script], capture_output=True, text=True, timeout=30
    ).stderr
    assert "Timeout (0:00:01)!" in out
    assert "in main" in out
    assert "Event loop was blocked" in out
