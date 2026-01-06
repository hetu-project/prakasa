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
from pynostr.relay import Relay
from pynostr.base_relay import RelayPolicy
from pynostr.message_pool import MessagePool
import tornado.ioloop
from tornado import gen

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
    DEFAULT_LISTEN_KINDS = [INVITE_KIND, CHAT_KIND, REASONING_KIND]

    def __init__(
        self,
        *,
        private_key_nsec: str,
        relays: List[str],
        timeout: int = 6,
        sid: str = "prakasa-main",
        role: str = "node",
        listen_kinds: Optional[List[int]] = None,
    ) -> None:
        self._private_key = PrivateKey.from_nsec(private_key_nsec)
        self.public_key = self._private_key.public_key.bech32()
        # Separate RelayManager for publishing (used by publisher thread)
        self._publish_relay_manager = RelayManager(timeout=timeout)
        self._sid = sid
        self._role = role
        self._relays = relays
        self._timeout = timeout
        self._listen_kinds = listen_kinds if listen_kinds is not None else self.DEFAULT_LISTEN_KINDS

        # Add relays to publish manager
        for r in relays:
            try:
                self._publish_relay_manager.add_relay(r, close_on_eose=False)
            except Exception as exc:
                logger.warning(f"Failed to add Nostr relay {r}: {exc}")

        # Outgoing event queue (for publishing)
        self._publish_queue: queue.Queue[Event] = queue.Queue()

        # Incoming event channel (for external consumers)
        self._event_channel: queue.Queue[Event] = queue.Queue(maxsize=1000)

        # Subscription tracking
        self._subscriptions: dict = {}
        
        # Listener state
        self._listen_io_loop: Optional[tornado.ioloop.IOLoop] = None
        self._listen_relays: List[Relay] = []
        self._listen_message_pool: Optional[MessagePool] = None
        self._listen_callback: Optional[tornado.ioloop.PeriodicCallback] = None
        self._seen_event_ids: set = set()

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
            event = None
            try:
                event = self._publish_queue.get(timeout=0.5)
            except queue.Empty:
                pass  

            if event is not None:
                try:
                    event.sign(self._private_key.hex())
                    self._publish_relay_manager.publish_event(event)
                    
                    logger.info(
                        "Published Nostr event kind=%s id=%s role=%s",
                        getattr(event, "kind", None),
                        getattr(event, "id", None),
                        self._role,
                    )
                except Exception as exc:
                    logger.warning(f"Failed to publish Nostr event: {exc}")


            try:
                self._publish_relay_manager.run_sync()
            except Exception as exc:
                logger.debug("PublishRelayManager.run_sync failed: %s", exc)
    
            if event is not None:
                 # Brief pause only if we published, to allow relay processing
                time.sleep(0.5)
            else:
                # Brief pause to avoid busy loop when idle
                time.sleep(0.1)


    # -------------------------------------------------------------------------
    # Subscribing / Listening
    # -------------------------------------------------------------------------

    def _listener_loop(self) -> None:
        """Background thread: use tornado io_loop to continuously listen for events."""
        # Create a new IOLoop for this thread
        self._listen_io_loop = tornado.ioloop.IOLoop()
        self._listen_message_pool = MessagePool(first_response_only=False)
        policy = RelayPolicy()
        

        current_time = int(time.time())
        filters = FiltersList([Filters(kinds=self._listen_kinds, since=current_time)])
        subscription_id = uuid.uuid1().hex
        
        for relay_url in self._relays:
            try:
                relay = Relay(
                    relay_url, 
                    self._listen_message_pool, 
                    self._listen_io_loop, 
                    policy, 
                    timeout=self._timeout, 
                    close_on_eose=False
                )
                relay.add_subscription(subscription_id, filters)
                self._listen_relays.append(relay)
            except Exception as exc:
                logger.warning(f"Failed to create listener relay for {relay_url}: {exc}")
        
        if not self._listen_relays:
            logger.warning("No listener relays available")
            return
        
        logger.info(
            f"Nostr event listener started for kinds={self._listen_kinds}, sub_id={subscription_id}"
        )
        
        def poll_events():
            """Periodically poll message pool for new events."""
            if self._stop.is_set():
                self._listen_io_loop.stop()
                return
                
            # Check for new events
            while self._listen_message_pool.has_events():
                event_msg = self._listen_message_pool.get_event()
                ev = getattr(event_msg, "event", None)
                
                if ev is not None and ev.id not in self._seen_event_ids:
                    self._seen_event_ids.add(ev.id)
                    try:
                        self._event_channel.put_nowait(ev)
                        logger.debug(f"Listener added event to channel: kind={ev.kind}, id={ev.id[:16]}...")
                    except queue.Full:
                        logger.warning("Nostr event channel full; dropping event")
            
            # Drain EOSE notices
            while self._listen_message_pool.has_eose_notices():
                self._listen_message_pool.get_eose_notice()
        
        @gen.coroutine
        def connect_relays():
            """Connect to all relays."""
            for relay in self._listen_relays:
                try:
                    yield relay.connect()
                    logger.debug(f"Connected to listener relay: {relay.url}")
                except Exception as e:
                    logger.warning(f"Failed to connect to relay {relay.url}: {e}")
        
        # Set up periodic callback to poll for events (every 500ms)
        self._listen_callback = tornado.ioloop.PeriodicCallback(poll_events, 500)
        self._listen_callback.start()
        
        # Spawn the connection in background (non-blocking)
        self._listen_io_loop.spawn_callback(connect_relays)
        
        try:
            # Keep the io_loop running to receive WebSocket messages
            self._listen_io_loop.start()
        except Exception as e:
            logger.warning(f"Listener io_loop error: {e}")
        finally:
            if self._listen_callback:
                self._listen_callback.stop()
            for relay in self._listen_relays:
                try:
                    relay.close()
                except Exception:
                    pass
            logger.debug("Listener loop stopped")

    # -------------------------------------------------------------------------
    # Internal Helpers
    # -------------------------------------------------------------------------

    def _drain_message_pool(self) -> None:
        """Drain ok notices and events from the relay manager's message pool."""
        mp = getattr(self._publish_relay_manager, "message_pool", None)
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
        
        # Stop listener io_loop if running
        if self._listen_io_loop is not None:
            try:
                self._listen_io_loop.add_callback(self._listen_io_loop.stop)
            except Exception:
                pass
        
        try:
            self._publisher_thread.join(timeout=2.0)
        except Exception:
            pass
        try:
            self._listener_thread.join(timeout=2.0)
        except Exception:
            pass
        try:
            self._publish_relay_manager.close()
        except Exception:
            pass
        # Listener relays are closed in _listener_loop finally block
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
    listen_kinds: Optional[List[int]] = None,
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
            listen_kinds=listen_kinds,
        )
    except Exception as exc:
        logging.getLogger(__name__).warning(
            f"Failed to init global Nostr publisher: {exc}"
        )


def get_publisher() -> Optional[NostrPublisher]:
    """Return the process-wide NostrPublisher if initialized, else None."""
    return _GLOBAL_PUBLISHER


__all__ = ["NostrPublisher", "init_global_publisher", "get_publisher"]


