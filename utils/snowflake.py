import time
import threading

from config import SNOWFLAKE_MACHINE_ID

# Custom epoch (2024-01-01T00:00:00Z, ms) - keeps the 41-bit timestamp from
# running out for ~69 years from this point, instead of from 1970.
_EPOCH_MS = 1704067200000

_MACHINE_ID_BITS = 10
_SEQUENCE_BITS = 12
_MAX_MACHINE_ID = (1 << _MACHINE_ID_BITS) - 1
_MAX_SEQUENCE = (1 << _SEQUENCE_BITS) - 1


class SnowflakeGenerator:
    """
    id = (timestamp_ms - EPOCH) << 22 | machine_id << 12 | sequence

    One instance per process. Safe for concurrent use within that process
    (guarded by a lock); machine_id must be unique across processes/replicas
    for the guarantee to hold cluster-wide.
    """

    def __init__(self, machine_id: int):
        if not (0 <= machine_id <= _MAX_MACHINE_ID):
            raise ValueError(f"machine_id must be between 0 and {_MAX_MACHINE_ID}")
        self._machine_id = machine_id
        self._lock = threading.Lock()
        self._last_timestamp_ms = -1
        self._sequence = 0

    def next_id(self) -> int:
        with self._lock:
            timestamp_ms = self._current_millis()

            if timestamp_ms < self._last_timestamp_ms:
                # Clock moved backwards (NTP adjustment). Refusing to hand out
                # an id is safer than risking a duplicate.
                raise RuntimeError("Clock moved backwards - refusing to generate id")

            if timestamp_ms == self._last_timestamp_ms:
                self._sequence = (self._sequence + 1) & _MAX_SEQUENCE
                if self._sequence == 0:
                    # Exhausted this millisecond's sequence space - spin to the next one
                    timestamp_ms = self._wait_for_next_millis(timestamp_ms)
            else:
                self._sequence = 0

            self._last_timestamp_ms = timestamp_ms

            return (
                ((timestamp_ms - _EPOCH_MS) << (_MACHINE_ID_BITS + _SEQUENCE_BITS))
                | (self._machine_id << _SEQUENCE_BITS)
                | self._sequence
            )

    @staticmethod
    def _current_millis() -> int:
        return time.time_ns() // 1_000_000

    def _wait_for_next_millis(self, current_ms: int) -> int:
        next_ms = self._current_millis()
        while next_ms <= current_ms:
            next_ms = self._current_millis()
        return next_ms


# Single shared generator for this process - every id (user/chat/message)
# is drawn from it, so they're all globally unique and roughly time-sortable.
_generator = SnowflakeGenerator(SNOWFLAKE_MACHINE_ID)


def next_id() -> int:
    return _generator.next_id()
