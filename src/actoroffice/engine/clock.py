"""A clock of awake time for one run (SI-38 BR-5).

On Windows `time.monotonic` keeps counting while the machine sleeps: after a night asleep a run looked stalled
for 4,414 s and a command looked over its limit. The run's loops (command guard every second, watchdog every few
seconds) read this clock often, so a jump much longer than their period means the process did not run: the whole
jump is left out, and every duration measured with this clock counts again from the moment the machine woke.
"""

from __future__ import annotations

import time
from collections.abc import Callable

SLEEP_GAP_S = 60.0


class AwakeClock:
    def __init__(self, raw: Callable[[], float] = time.monotonic, gap_s: float = SLEEP_GAP_S) -> None:
        self.raw = raw
        self.gap_s = gap_s
        self.slept = 0.0  # seconds left out so far
        self.unreported = 0.0  # slept but not yet told in the activity stream (the watchdog reports it)
        self._last = raw()

    def now(self) -> float:
        t = self.raw()
        jump = t - self._last
        self._last = t
        if jump > self.gap_s:
            self.slept += jump
            self.unreported += jump
        return t - self.slept

    def take_unreported(self) -> float:
        slept, self.unreported = self.unreported, 0.0
        return slept
