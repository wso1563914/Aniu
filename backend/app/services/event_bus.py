"""Per-run event bus for streaming agent progress to subscribers.

Each run has zero or more subscribers; every published event fan-outs to all
live subscriber queues. Subscribers are thread-safe `queue.Queue` instances so
a FastAPI SSE endpoint running in a worker thread can block on `get()` while
the agent executes in another thread.
"""

from __future__ import annotations

import logging
import queue
import threading
import time
from typing import Any, Iterator

logger = logging.getLogger(__name__)

# Events dropped if a subscriber is slower than this many pending items.
_MAX_SUBSCRIBER_BACKLOG = 2000
# Seconds the replay buffer keeps events after a run finishes (lets late
# subscribers catch up after reconnect).
_REPLAY_TTL_SECONDS = 60.0
# Max events kept in the replay buffer per run.
_MAX_REPLAY_EVENTS = 500


class _RunChannel:
    __slots__ = ("run_id", "subscribers", "history", "finished_at", "lock", "sequence")

    def __init__(self, run_id: int) -> None:
        self.run_id = run_id
        self.subscribers: list[queue.Queue[dict[str, Any] | None]] = []
        self.history: list[dict[str, Any]] = []
        self.finished_at: float | None = None
        self.lock = threading.Lock()
        self.sequence = 0


class EventBus:
    def __init__(self) -> None:
        self._channels: dict[int, _RunChannel] = {}
        self._global_lock = threading.Lock()

    def _get_or_create(self, run_id: int) -> _RunChannel:
        with self._global_lock:
            channel = self._channels.get(run_id)
            if channel is None:
                channel = _RunChannel(run_id)
                self._channels[run_id] = channel
            return channel

    def _get(self, run_id: int) -> _RunChannel | None:
        with self._global_lock:
            return self._channels.get(run_id)

    def publish(self, run_id: int, event_type: str, data: dict[str, Any] | None = None) -> None:
        payload: dict[str, Any] = {
            "type": event_type,
            "run_id": run_id,
            "ts": time.time(),
        }
        if data:
            payload.update(data)

        channel = self._get_or_create(run_id)
        with channel.lock:
            channel.sequence += 1
            payload.setdefault("sequence", channel.sequence)
            channel.history.append(payload)
            if len(channel.history) > _MAX_REPLAY_EVENTS:
                del channel.history[: len(channel.history) - _MAX_REPLAY_EVENTS]
            subscribers = list(channel.subscribers)

        for sub in subscribers:
            try:
                if sub.qsize() >= _MAX_SUBSCRIBER_BACKLOG:
                    logger.warning(
                        "event_bus subscriber backlog exceeded for run_id=%s, dropping",
                        run_id,
                    )
                    continue
                sub.put_nowait(payload)
            except Exception:  # pragma: no cover - defensive
                logger.exception("event_bus publish to subscriber failed")

        if event_type in {"completed", "failed"}:
            with channel.lock:
                channel.finished_at = time.time()
            self._maybe_expire_finished()

    def subscribe(self, run_id: int) -> tuple[queue.Queue[dict[str, Any] | None], list[dict[str, Any]]]:
        channel = self._get_or_create(run_id)
        sub: queue.Queue[dict[str, Any] | None] = queue.Queue()
        with channel.lock:
            snapshot = list(channel.history)
            channel.subscribers.append(sub)
        return sub, snapshot

    def unsubscribe(self, run_id: int, sub: queue.Queue[dict[str, Any] | None]) -> None:
        channel = self._get(run_id)
        if channel is None:
            return
        with channel.lock:
            try:
                channel.subscribers.remove(sub)
            except ValueError:
                pass

    def is_finished(self, run_id: int) -> bool:
        channel = self._get(run_id)
        if channel is None:
            return False
        with channel.lock:
            return channel.finished_at is not None

    def _maybe_expire_finished(self) -> None:
        now = time.time()
        with self._global_lock:
            expired = [
                rid
                for rid, ch in self._channels.items()
                if ch.finished_at is not None
                and now - ch.finished_at > _REPLAY_TTL_SECONDS
                and not ch.subscribers
            ]
            for rid in expired:
                self._channels.pop(rid, None)

    def stream(self, run_id: int, *, stop_event: threading.Event | None = None) -> Iterator[dict[str, Any]]:
        """Blocking generator yielding events for `run_id` until the run ends."""
        sub, snapshot = self.subscribe(run_id)
        try:
            for event in snapshot:
                yield event
            # If a snapshot already contained the terminal event, end now.
            if snapshot and snapshot[-1].get("type") in {"completed", "failed"}:
                return
            while True:
                if stop_event is not None and stop_event.is_set():
                    return
                try:
                    event = sub.get(timeout=15.0)
                except queue.Empty:
                    # Heartbeat to keep proxies from closing idle connections.
                    yield {"type": "heartbeat", "run_id": run_id, "ts": time.time()}
                    continue
                if event is None:
                    return
                yield event
                if event.get("type") in {"completed", "failed"}:
                    return
        finally:
            self.unsubscribe(run_id, sub)


event_bus = EventBus()


def make_emitter(run_id: int):
    """Factory returning an `emit(type, **data)` callable bound to a run."""

    def emit(event_type: str, **data: Any) -> None:
        try:
            event_bus.publish(run_id, event_type, data or None)
        except Exception:  # pragma: no cover - never let telemetry break the run
            logger.exception("emit failed for run_id=%s type=%s", run_id, event_type)

    return emit
