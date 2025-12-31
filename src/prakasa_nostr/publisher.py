"""
Lightweight asynchronous Nostr publisher and event listener.

Usage pattern:
    from prakasa_nostr import init_global_publisher, get_publisher

    # Initialize once at startup
    init_global_publisher(privkey_nsec, relays=[...])

    # Publish events
    pub = get_publisher()
    if pub is not None:
        event = ...
        pub.publish_event(event)

    # Consume received events from channel
    event_channel = pub.get_event_channel()
    while True:
        ev = event_channel.get(timeout=5)
        print(ev)

Publishing and listening are done on background threads to avoid blocking hot paths.
"""

from __future__ import annotations

import logging
import queue
import threading
import time
import uuid
from typing import List, Optional

from pynostr.event import Event
from pynostr.filters import Filters, FiltersList
from pynostr.key import PrivateKey
from pynostr.relay_manager import RelayManager

from parallax_utils.logging_config import get_logger
from prakasa_nostr.events import INVITE_KIND, CHAT_KIND, REASONING_KIND

logger = get_logger("prakasa_nostr.publisher")


class NostrPublisher:
    """Async Nostr event publisher and listener backed by `RelayManager` and worker threads.

    Responsibilities:
    - Publish events asynchronously via a background thread
    - Subscribe to events and put them into a thread-safe channel for external consumers
    """

    # Default event kinds to subscribe
    DEFAULT_LISTEN_KINDS = [1, INVITE_KIND, CHAT_KIND, REASONING_KIND]

    def __init__(
        self,
        *,
        private_key_nsec: str,
        relays: List[str],
        timeout: int = 6,
        sid: str = "prakasa-main",
        role: str = "node",
    ) -> None:
        self._private_key = PrivateKey.from_nsec(private_key_nsec)
        self._relay_manager = RelayManager(timeout=timeout)
        self._sid = sid
        self._role = role

        # Add relays
        for r in relays:
            try:
                self._relay_manager.add_relay(r)
            except Exception as exc:
                logger.warning(f"Failed to add Nostr relay {r}: {exc}")

        # Outgoing event queue (for publishing)
        self._publish_queue: queue.Queue[Event] = queue.Queue()

        # Incoming event channel (for external consumers)
        self._event_channel: queue.Queue[Event] = queue.Queue(maxsize=1000)

        # Subscription tracking
        self._subscriptions: dict = {}

        # Stop signal for background threads
        self._stop = threading.Event()

        # Start publisher thread
        self._publisher_thread = threading.Thread(
            target=self._publisher_loop,
            name=f"NostrPublisher-{role}",
            daemon=True,
        )
        self._publisher_thread.start()

        # Start event listener thread
        self._listener_thread = threading.Thread(
            target=self._listener_loop,
            name=f"NostrListener-{role}",
            daemon=True,
        )
        self._listener_thread.start()

        logger.info(
            "Initialized NostrPublisher (role=%s, sid=%s, relays=%d)",
            role,
            sid,
            len(relays),
        )

    # -------------------------------------------------------------------------
    # Properties
    # -------------------------------------------------------------------------

    @property
    def sid(self) -> str:
        return self._sid

    @property
    def role(self) -> str:
        return self._role

    # -------------------------------------------------------------------------
    # Event Channel (for external consumers)
    # -------------------------------------------------------------------------

    def get_event_channel(self) -> queue.Queue[Event]:
        """Return the event channel (queue) for external consumers to get nostr events."""
        return self._event_channel

    # -------------------------------------------------------------------------
    # Publishing
    # -------------------------------------------------------------------------

    def publish_event(self, event: Event) -> None:
        """Enqueue an event for async publishing."""
        try:
            self._publish_queue.put_nowait(event)
        except queue.Full:
            logger.warning("NostrPublisher queue full; dropping event")

    def _publisher_loop(self) -> None:
        """Background loop that signs and publishes events."""
        while not self._stop.is_set():
            try:
                event: Event = self._publish_queue.get(timeout=0.5)
            except queue.Empty:
                continue

            try:
                event.sign(self._private_key.hex())
                self._relay_manager.publish_event(event)

                # Flush pending network ops
                try:
                    self._relay_manager.run_sync()
                except Exception as exc:
                    logger.debug("RelayManager.run_sync failed: %s", exc)

                # Brief pause to allow messages to be processed by relays
                time.sleep(0.5)

                # Drain ok notices and events from message pool
                self._drain_message_pool()

                logger.debug(
                    "Published Nostr event kind=%s id=%s role=%s",
                    getattr(event, "kind", None),
                    getattr(event, "id", None),
                    self._role,
                )
            except Exception as exc:
                logger.warning(f"Failed to publish Nostr event: {exc}")

    # -------------------------------------------------------------------------
    # Subscribing / Listening
    # -------------------------------------------------------------------------

    def subscribe_events(self, kinds: List[int], limit: int = 100) -> str:
        """Subscribe to nostr events of given kinds. Returns subscription_id."""
        filters = FiltersList([Filters(kinds=kinds, limit=limit)])
        sub_id = uuid.uuid1().hex
        self._relay_manager.add_subscription_on_all_relays(sub_id, filters)
        self._relay_manager.run_sync()
        self._subscriptions[sub_id] = (filters, [])
        logger.info(f"Subscribed to nostr events: kinds={kinds}, sub_id={sub_id}")
        return sub_id

    def unsubscribe_events(self, sub_id: str) -> None:
        """Unsubscribe from nostr events by subscription_id."""
        try:
            self._relay_manager.close_subscription_on_all_relays(sub_id)
            self._relay_manager.run_sync()
            self._subscriptions.pop(sub_id, None)
            logger.info(f"Unsubscribed nostr events: sub_id={sub_id}")
        except Exception as exc:
            logger.warning(f"Failed to unsubscribe nostr events: {exc}")

    def _listener_loop(self) -> None:
        """Background thread: poll relay_manager for new events and put into event_channel."""
        sub_id = None
        try:
            sub_id = self.subscribe_events(self.DEFAULT_LISTEN_KINDS, limit=100)
        except Exception as exc:
            logger.warning(f"Failed to subscribe to default nostr events: {exc}")
            return

        logger.info(
            f"Nostr event listener started for kinds={self.DEFAULT_LISTEN_KINDS}, sub_id={sub_id}"
        )

        while not self._stop.is_set():
            events = self._poll_events(sub_id)
            for ev in events:
                try:
                    self._event_channel.put_nowait(ev)
                except queue.Full:
                    logger.warning("Nostr event channel full; dropping event")
            time.sleep(2)

    def _poll_events(self, sub_id: Optional[str] = None) -> List[Event]:
        """Poll and return received events for a subscription (or all if sub_id=None)."""
        events = []
        mp = getattr(self._relay_manager, "message_pool", None)
        if mp is None:
            return events

        try:
            while mp.has_events():
                event_msg = mp.get_event()
                ev = getattr(event_msg, "event", None)
                if ev is not None:
                    msg_sub_id = getattr(event_msg, "subscription_id", None)
                    if sub_id is None or msg_sub_id == sub_id:
                        events.append(ev)
        except Exception:
            pass

        return events

    # -------------------------------------------------------------------------
    # Internal Helpers
    # -------------------------------------------------------------------------

    def _drain_message_pool(self) -> None:
        """Drain ok notices and events from the relay manager's message pool."""
        mp = getattr(self._relay_manager, "message_pool", None)
        if mp is None:
            return

        # Drain OK notices
        try:
            while getattr(mp, "has_ok_notices", lambda: False)():
                ok_msg = mp.get_ok_notice()
                logger.debug("Nostr ok notice: %s", ok_msg)
        except Exception:
            pass

        # Drain events (for publisher thread, just log them)
        try:
            while getattr(mp, "has_events", lambda: False)():
                event_msg = mp.get_event()
                ev = getattr(event_msg, "event", None)
                if ev is not None:
                    try:
                        logger.debug("Nostr received event: %s", ev.to_dict())
                    except Exception:
                        logger.debug("Nostr received event (non-dict): %s", ev)
                else:
                    logger.debug("Nostr received message: %s", event_msg)
        except Exception:
            pass

    # -------------------------------------------------------------------------
    # Lifecycle
    # -------------------------------------------------------------------------

    def shutdown(self) -> None:
        """Stop all background threads and close relay connections."""
        self._stop.set()
        try:
            self._publisher_thread.join(timeout=2.0)
        except Exception:
            pass
        try:
            self._listener_thread.join(timeout=2.0)
        except Exception:
            pass
        try:
            self._relay_manager.close()
        except Exception:
            pass
        logger.info("NostrPublisher shutdown complete")


# =============================================================================
# Global Publisher Singleton
# =============================================================================

_GLOBAL_PUBLISHER: Optional[NostrPublisher] = None


def init_global_publisher(
    private_key_nsec: str,
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
    if not private_key_nsec:
        logger.warning("Empty Nostr private key provided; publisher not initialized")
        return

    try:
        _GLOBAL_PUBLISHER = NostrPublisher(
            private_key_nsec=private_key_nsec,
            relays=relays,
            sid=sid,
            role=role,
        )
    except Exception as exc:
        logging.getLogger(__name__).warning(
            f"Failed to init global Nostr publisher: {exc}"
        )


def get_publisher() -> Optional[NostrPublisher]:
    """Return the process-wide NostrPublisher if initialized, else None."""
    return _GLOBAL_PUBLISHER


__all__ = ["NostrPublisher", "init_global_publisher", "get_publisher"]


