"""A bounded, in-memory failure counter used to slow down credential guessing.

Deliberately not a general-purpose rate limiter: it counts only *failed*
authentication attempts, per client, over a sliding window. Successful requests
are never throttled, so a working client cannot be locked out by its own traffic.

State is per-process and lost on restart. That is an accepted limitation — the
service runs as a single uvicorn process on one Raspberry Pi, and the goal is to
make online guessing impractical, not to survive a reboot.
"""

from __future__ import annotations

import threading
import time
from collections import OrderedDict, deque
from collections.abc import Callable
from typing import Final

#: Cap on how many distinct clients are tracked at once. Without a bound, an
#: attacker spraying spoofed sources could grow this dict until the Pi runs out
#: of memory. Oldest entries are evicted first.
DEFAULT_MAX_TRACKED_CLIENTS: Final = 1024


class FailureLimiter:
    """Tracks recent authentication failures per client key."""

    def __init__(
        self,
        *,
        max_failures: int,
        window_seconds: float,
        max_tracked_clients: int = DEFAULT_MAX_TRACKED_CLIENTS,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if max_failures < 1:
            msg = "max_failures must be at least 1"
            raise ValueError(msg)
        if window_seconds <= 0:
            msg = "window_seconds must be positive"
            raise ValueError(msg)

        self._max_failures = max_failures
        self._window = window_seconds
        self._max_clients = max_tracked_clients
        self._clock = clock
        self._failures: OrderedDict[str, deque[float]] = OrderedDict()
        # Route handlers are sync `def`, so Starlette runs them in a threadpool and
        # several requests can reach this object at once. Without the lock, two
        # threads interleaving _prune's delete with record_failure's re-insert can
        # drop a failure — or raise KeyError out of move_to_end.
        self._lock = threading.Lock()

    def _prune(self, client: str) -> deque[float]:
        """Drop expired timestamps for ``client``. Callers must hold ``self._lock``."""
        cutoff = self._clock() - self._window
        timestamps = self._failures.get(client)
        if timestamps is None:
            return deque()
        while timestamps and timestamps[0] <= cutoff:
            timestamps.popleft()
        if not timestamps:
            del self._failures[client]
        return timestamps

    def is_blocked(self, client: str) -> bool:
        """Whether ``client`` has used up its allowance inside the current window."""
        with self._lock:
            return len(self._prune(client)) >= self._max_failures

    def record_failure(self, client: str) -> None:
        with self._lock:
            timestamps = self._prune(client)
            if client not in self._failures:
                self._failures[client] = timestamps
            timestamps.append(self._clock())
            self._failures.move_to_end(client)

            while len(self._failures) > self._max_clients:
                self._evict_one()

    def _evict_one(self) -> None:
        """Drop the least incriminating tracked client. Caller holds ``self._lock``.

        Plain LRU eviction would double as a block eraser: an attacker churning
        through source addresses could push their own blocked entry out of the table
        and get a fresh allowance. Evicting the fewest-failures entry first — oldest
        breaking the tie — means a blocked client is the last thing to be forgotten.
        """
        rank = {client: position for position, client in enumerate(self._failures)}
        victim = min(self._failures, key=lambda client: (len(self._failures[client]), rank[client]))
        del self._failures[victim]

    def reset(self, client: str) -> None:
        """Forget a client's failures — called after it authenticates successfully."""
        with self._lock:
            self._failures.pop(client, None)

    def failure_count(self, client: str) -> int:
        """Failures currently counted against ``client``. Intended for tests and logs."""
        with self._lock:
            return len(self._prune(client))
