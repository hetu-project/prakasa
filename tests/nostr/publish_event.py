import sys
from pathlib import Path
import time

# Add src directory to Python path
project_root = Path(__file__).parent.parent.parent
sys.path.insert(0, str(project_root / "src"))

from prakasa_nostr import init_global_publisher, get_publisher
from pynostr.key import PrivateKey
from prakasa_nostr.events import (
    DINF_TASK_PUBLISH_KIND, 
    DINF_EVALUATION_KIND, 
    DINF_TASK_ASSIGN_KIND, 
    DINF_TASK_RESULT_KIND, 
    DINF_WORKLOAD_PROOF_KIND, 
    DINF_SETTLEMENT_KIND,
    SchedulerAssignmentEvent,
    SchedulerAssignmentContent,
    Assignment,
    ModelRef,
    RoutingInfo,
)

listen_kinds_list = [
    DINF_TASK_PUBLISH_KIND, 
    DINF_TASK_ASSIGN_KIND, 
    DINF_TASK_RESULT_KIND, 
    DINF_WORKLOAD_PROOF_KIND, 
    DINF_SETTLEMENT_KIND, 
    DINF_EVALUATION_KIND
]

pk = PrivateKey()
privkey_nsec = pk.bech32()

# Initialize publisher
init_global_publisher(
    privkey_nsec, 
    relays=["ws://62.72.41.239:10545"], 
    listen_kinds=listen_kinds_list
)

# Get publisher
pub = get_publisher()
if pub is None:
    print("Failed to initialize Nostr publisher")
    sys.exit(1)

print(f"Publisher initialized with pubkey: {pk.public_key.hex()}")

# Construct SchedulerAssignmentEvent
print("\nConstructing SchedulerAssignmentEvent...")

# Create worker assignments
assignments = [
    Assignment(
        worker_pubkey="worker_pubkey_1_hex",
        node_id="node_1",
        account="0x1234567890abcdef1234567890abcdef12345679",
        start_layer=0,
        end_layer=14,
        tp_rank=0,
        tp_size=1,
        dp_rank=0,
        dp_size=1,
        max_concurrent_requests=8,
        max_sequence_length=2048,
        expected_work_units=100.0,
    ),
    Assignment(
        worker_pubkey="worker_pubkey_2_hex",
        node_id="node_2",
        account="0xabcdef1234567890abcdef1234567890abcdef12",
        start_layer=14,
        end_layer=28,
        tp_rank=0,
        tp_size=1,
        dp_rank=0,
        dp_size=1,
        max_concurrent_requests=8,
        max_sequence_length=2048,
        expected_work_units=100.0,
    ),
]

# Create model reference
model = ModelRef(
    model_name="Qwen/Qwen3-0.6B",
    num_layers=28,
    model_version="v1.0",
)

# Create routing info
routing = RoutingInfo(
    pipeline_id="pipeline_123",
    node_path=["node_1", "node_2"],
)

# Note: content will be created inside the loop to ensure uniqueness

# Create initial event for preview
content = SchedulerAssignmentContent(
    assignments=assignments,
    model=model,
    routing=routing,
    deadline=int(time.time()) + 3600,
)

event = SchedulerAssignmentEvent.from_content(
    sid="prakasa-main",
    task_event_id="task_event_id_hex_example",
    allocation_id="allocation_preview",
    content=content,
)

print(f"Event kind: {event.kind}")
print(f"Event tags: {event.tags}")
print(f"Event content preview: {event.content[:200]}...")

# Publish the event in a loop
print("\nPublishing events every 0.5 seconds...")
print("Press Ctrl+C to stop\n")

try:
    count = 0
    while True:
        count += 1
        
        # Create unique content for each event (update deadline to current time + 1 hour)
        content = SchedulerAssignmentContent(
            assignments=assignments,
            model=model,
            routing=routing,
            deadline=int(time.time()) + 3600,  # This changes each second
        )
        
        # Update allocation_id for each event to make them unique
        event = SchedulerAssignmentEvent.from_content(
            sid="prakasa-main",
            task_event_id="task_event_id_hex_example",
            allocation_id=f"allocation_{count}",
            content=content,
        )
        pub.publish_event(event)
        print(f"[{count}] Event published at {time.strftime('%H:%M:%S')} (event_id: {event.id})")
        time.sleep(10)
except KeyboardInterrupt:
    print("\n\nStopped publishing.")
    print(f"Total events published: {count}")

print("\nDone!")