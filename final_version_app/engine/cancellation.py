"""Cooperative cancellation primitives shared by turns and tool runtimes."""

from __future__ import annotations

from threading import Event, Lock
from typing import Optional


class TurnCancelledError(RuntimeError):
    """Internal control-flow exception raised at safe cancellation points."""


class CancellationToken:
    """Thread-safe cancellation signal with a human-readable reason.

    Python cannot safely kill arbitrary threads. The runtime therefore uses
    cooperative cancellation: the model loop and every managed tool check this
    token before expensive work. Process-backed tools can later translate the
    same signal into a real process termination.
    """

    def __init__(self) -> None:
        self._event = Event()
        self._reason: Optional[str] = None
        self._lock = Lock()

    @property
    def is_cancelled(self) -> bool:
        return self._event.is_set()

    @property
    def reason(self) -> Optional[str]:
        with self._lock:
            return self._reason

    def cancel(self, reason: Optional[str] = None) -> bool:
        """Set the signal once and return whether this call changed its state."""

        with self._lock:
            if self._event.is_set():
                return False
            self._reason = reason or "Turn interrupted."
            self._event.set()
            return True

    def raise_if_cancelled(self) -> None:
        if self._event.is_set():
            raise TurnCancelledError(self.reason or "Turn interrupted.")
