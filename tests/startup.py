# SPDX-License-Identifier: Apache-2.0
"""Wait for launched workers rather than guessing how long EAL startup takes."""
import os
import selectors
import time


def wait_for_output(process, marker, timeout=8):
    output = bytearray()
    deadline = time.monotonic() + timeout
    with selectors.DefaultSelector() as selector:
        selector.register(process.stdout, selectors.EVENT_READ)
        while marker not in output:
            remaining = deadline-time.monotonic()
            if remaining <= 0 or not selector.select(remaining):
                raise AssertionError("DUT startup timed out:\n"+output.decode(errors="replace"))
            chunk = os.read(process.stdout.fileno(), 65536)
            if not chunk:
                raise AssertionError("DUT exited before startup marker:\n"+output.decode(errors="replace"))
            output.extend(chunk)
    return output.decode(errors="replace")
