import sys
from pathlib import Path
import queue
from datetime import datetime

# Add src directory to Python path
project_root = Path(__file__).parent.parent.parent
sys.path.insert(0, str(project_root / "src"))

from prakasa_nostr import init_global_publisher, get_publisher
from pynostr.key import PrivateKey
from prakasa_nostr.events import DINF_TASK_PUBLISH_KIND, DINF_EVALUATION_KIND, DINF_TASK_ASSIGN_KIND, DINF_TASK_RESULT_KIND, DINF_WORKLOAD_PROOF_KIND, DINF_SETTLEMENT_KIND

listen_kinds_list = [DINF_TASK_PUBLISH_KIND, DINF_TASK_ASSIGN_KIND, DINF_TASK_RESULT_KIND, DINF_WORKLOAD_PROOF_KIND, DINF_SETTLEMENT_KIND, DINF_EVALUATION_KIND]

pk = PrivateKey()
privkey_nsec = pk.bech32()  # Your hex-encoded Nostr private key here

relays = ["ws://62.72.41.239:10545"]
# Initialize once at startup
init_global_publisher(privkey_nsec, relays=relays, listen_kinds=listen_kinds_list)

# Get publisher
pub = get_publisher()
if pub is None:
    print("Failed to initialize Nostr publisher")
    sys.exit(1)

print(f"Listening for Nostr events with kinds: {listen_kinds_list}")
print(f"relayers: {relays}")
print("Press Ctrl+C to exit\n")

# Consume received events from channel
event_channel = pub.get_event_channel()
while True:
    try:
        ev = event_channel.get(timeout=5)
        # Parse timestamp to readable format
        event_time = datetime.fromtimestamp(ev.created_at).strftime('%Y-%m-%d %H:%M:%S')
        print(f"Received event: kind={ev.kind}, id={ev.id}")
        print(f"Created at: {event_time} (timestamp: {ev.created_at})")
        print(f"Content: {ev.content}")
        print(f"Tags: {ev.tags}")
        print("-" * 60)
    except queue.Empty:
        # No events received within timeout, continue waiting
        continue
    except KeyboardInterrupt:
        print("\nShutting down...")
        break