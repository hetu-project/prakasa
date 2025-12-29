"""
Lightweight asynchronous Nostr publisher used by scheduler and workers.

Usage pattern:
    from prakasa_nostr import init_global_publisher, get_publisher

    init_global_publisher(privkey_hex, relays=[...])
    pub = get_publisher()
    if pub is not None:
        event = ...
        pub.publish_event(event)

Publishing is done on a background thread to avoid blocking hot paths.
"""

from __future__ import annotations

import logging
import queue
import threading
from typing import List, Optional

from pynostr.event import Event
from pynostr.key import PrivateKey
from pynostr.relay_manager import RelayManager

from parallax_utils.logging_config import get_logger


logger = get_logger("prakasa_nostr.publisher")


class NostrPublisher:
    """Async Nostr event publisher backed by `RelayManager` and a worker thread."""

    def __init__(
        self,
        *,
        private_key_hex: str,
        relays: List[str],
        timeout: int = 6,
        sid: str = "prakasa-main",
        role: str = "node",
    ) -> None:
        self._private_key = PrivateKey(bytes.fromhex(private_key_hex))
        self._relay_manager = RelayManager(timeout=timeout)
        for r in relays:
            try:
                self._relay_manager.add_relay(r)
            except Exception as exc:  # defensive: a bad relay must not crash startup
                logger.warning(f"Failed to add Nostr relay {r}: {exc}")

        self._sid = sid
        self._role = role
        self._queue: "queue.Queue[Event]" = queue.Queue()
        self._stop = threading.Event()
        self._thread = threading.Thread(
            target=self._worker_loop, name=f"NostrPublisher-{role}", daemon=True
        )
        self._thread.start()
        logger.info(
            "Initialized NostrPublisher (role=%s, sid=%s, relays=%d)",
            role,
            sid,
            len(relays),
        )

    @property
    def sid(self) -> str:
        return self._sid

    @property
    def role(self) -> str:
        return self._role

    def publish_event(self, event: Event) -> None:
        """Enqueue an event for async publishing."""
        try:
            self._queue.put_nowait(event)
        except queue.Full:
            logger.warning("NostrPublisher queue full; dropping event")

    def _worker_loop(self) -> None:
        """Background loop that signs and publishes events."""
        while not self._stop.is_set():
            try:
                event: Event = self._queue.get(timeout=0.5)
            except queue.Empty:
                continue

            try:
                event.sign(self._private_key.hex())
                self._relay_manager.publish_event(event)
                logger.debug(
                    "Published Nostr event kind=%s id=%s role=%s",
                    getattr(event, "kind", None),
                    getattr(event, "id", None),
                    self._role,
                )
            except Exception as exc:
                logger.warning(f"Failed to publish Nostr event: {exc}")

    def shutdown(self) -> None:
        """Stop the worker thread and close relays."""
        self._stop.set()
        try:
            self._thread.join(timeout=2.0)
        except Exception:
            pass
        try:
            self._relay_manager.close()
        except Exception:
            pass


_GLOBAL_PUBLISHER: Optional[NostrPublisher] = None


def init_global_publisher(
    private_key_hex: str,
    relays: Optional[List[str]] = None,
    *,
    sid: str = "prakasa-main",
    role: str = "node",
) -> None:
    """
    Initialize the process-wide NostrPublisher.

    Safe to call multiple times; later calls are ignored once initialized.
    """
    global _GLOBAL_PUBLISHER
    if _GLOBAL_PUBLISHER is not None:
        return

    relays = relays or []
    if not private_key_hex:
        logger.warning("Empty Nostr private key provided; publisher not initialized")
        return

    try:
        _GLOBAL_PUBLISHER = NostrPublisher(
            private_key_hex=private_key_hex,
            relays=relays,
            sid=sid,
            role=role,
        )
    except Exception as exc:
        # Do not break main process if nostr fails; just log and continue.
        logging.getLogger(__name__).warning(f"Failed to init global Nostr publisher: {exc}")


def get_publisher() -> Optional[NostrPublisher]:
    """Return the process-wide NostrPublisher if initialized, else None."""
    return _GLOBAL_PUBLISHER


__all__ = ["NostrPublisher", "init_global_publisher", "get_publisher"]


