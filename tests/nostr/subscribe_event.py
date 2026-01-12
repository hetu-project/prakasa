import sys
from pathlib import Path
import queue
from datetime import datetime
import logging

# Add src directory to Python path
project_root = Path(__file__).parent.parent.parent
sys.path.insert(0, str(project_root / "src"))

# Set up logging to see debug messages
logging.basicConfig(
    level=logging.DEBUG,
    format='%(asctime)s [%(levelname)s] %(name)s: %(message)s',
    datefmt='%H:%M:%S'
)

from prakasa_nostr import init_global_publisher, get_publisher
from pynostr.key import PrivateKey
from prakasa_nostr.events import DINF_TASK_PUBLISH_KIND, DINF_EVALUATION_KIND, DINF_TASK_ASSIGN_KIND, DINF_TASK_RESULT_KIND, DINF_WORKLOAD_PROOF_KIND, DINF_SETTLEMENT_KIND

listen_kinds_list = [DINF_TASK_PUBLISH_KIND, DINF_TASK_ASSIGN_KIND, DINF_TASK_RESULT_KIND, DINF_WORKLOAD_PROOF_KIND, DINF_SETTLEMENT_KIND, DINF_EVALUATION_KIND]

pk = PrivateKey()
privkey_nsec = pk.bech32()  # Your hex-encoded Nostr private key here

relays = ["wss://nostr.parallel.hetu.org:8443"]
print(f"\nInitializing subscriber...")
print(f"Public key: {pk.public_key.hex()}")
print(f"Subscribe since: {int(__import__('time').time())} ({datetime.now().strftime('%H:%M:%S')})")
print()
# Initialize once at startup
init_global_publisher(privkey_nsec, relays=relays, listen_kinds=listen_kinds_list)

# Get publisher
pub = get_publisher()
if pub is None:
    print("Failed to initialize Nostr publisher")
    sys.exit(1)

print(f"Listening for Nostr events with kinds: {listen_kinds_list}")
print(f"Relays: {relays}")
print("Press Ctrl+C to exit\n")
print("Waiting for events...")

# Consume received events from channel
event_channel = pub.get_event_channel()
heart_beat_counter = 0
while True:
    try:
        ev = event_channel.get(timeout=5)
        # Parse timestamp to readable format
        event_time = datetime.fromtimestamp(ev.created_at).strftime('%Y-%m-%d %H:%M:%S')
        print(f"\n{'='*60}")
        print(f"✓ RECEIVED EVENT")
        print(f"{'='*60}")
        print(f"Kind: {ev.kind}")
        print(f"ID: {ev.id}")
        print(f"Created at: {event_time} (timestamp: {ev.created_at})")
        print(f"Content: {ev.content[:200]}..." if len(ev.content) > 200 else f"Content: {ev.content}")
        print(f"Tags: {ev.tags}")
        print(f"{'='*60}\n")
    except queue.Empty:
        # No events received within timeout, continue waiting
        heart_beat_counter += 1
        if heart_beat_counter % 6 == 0:  # Every 30 seconds
            print(f"[{datetime.now().strftime('%H:%M:%S')}] Still listening... (no events yet)")
        continue
    except KeyboardInterrupt:
        print("\nShutting down...")
        break