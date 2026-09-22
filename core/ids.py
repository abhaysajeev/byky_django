"""UUID version 7.

Transaction tables use UUID keys; masters keep integers
(design/00-findings.md section 9).

Version 7, not 4. A v4 is entirely random, so every insert lands in a different
place in the index and the table fragments as it grows -- the one real cost of
UUID keys, and the reason teams regret them. A v7 puts a millisecond timestamp
in the high bits, so ids sort in creation order and each insert lands at the end
of the index, the way an auto-increment integer does, while staying globally
unique.

Written here rather than pulled from a package: PostgreSQL 16 has no uuidv7()
(18 does) and Python 3.12 has no uuid.uuid7() (3.14 does). When either arrives,
this goes away.

Layout, per RFC 9562:

    48 bits   milliseconds since the Unix epoch
     4 bits   version (7)
    12 bits   random
     2 bits   variant (0b10)
    62 bits   random
"""

import os
import threading
import time
import uuid

_lock = threading.Lock()
_last_ms = 0
_counter = 0


def uuid7():
    """A time-ordered UUID, unique across machines.

    The 12 random bits next to the timestamp are used as a counter within a
    millisecond, so two ids made in the same millisecond still sort in the
    order they were created. Without that they sort randomly inside each
    millisecond -- harmless for uniqueness, but it gives up part of the index
    locality that is the whole reason for choosing v7.
    """
    global _last_ms, _counter

    with _lock:
        timestamp_ms = int(time.time() * 1000) & 0xFFFFFFFFFFFF
        if timestamp_ms == _last_ms:
            _counter += 1
            if _counter > 0xFFF:          # >4095 in one millisecond: wait for the next
                while timestamp_ms == _last_ms:
                    timestamp_ms = int(time.time() * 1000) & 0xFFFFFFFFFFFF
                _counter = 0
        else:
            _counter = 0
        _last_ms = timestamp_ms
        rand_a = _counter

    rand_b = int.from_bytes(os.urandom(8), "big") & 0x3FFFFFFFFFFFFFFF   # 62 bits

    value = (
        (timestamp_ms << 80)
        | (0x7 << 76)          # version
        | (rand_a << 64)
        | (0b10 << 62)         # variant
        | rand_b
    )
    return uuid.UUID(int=value)
