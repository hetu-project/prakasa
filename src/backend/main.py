import asyncio
import json
import time
import traceback
import uuid
import queue
from contextlib import asynccontextmanager
from typing import Dict, Optional

import uvicorn
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from backend.server.request_handler import RequestHandler
from backend.server.scheduler_manage import SchedulerManage
from backend.server.server_args import parse_args
from backend.server.static_config import (
    get_model_list,
    get_node_join_command,
    init_model_info_dict_cache,
)
from prakasa_nostr import init_global_publisher, get_publisher
from prakasa_nostr.events import INVITE_KIND, CHAT_KIND, REASONING_KIND
from prakasa_nostr.crypto import GroupV1Crypto, Nip04Crypto
from pynostr.filters import Filters, FiltersList
from pynostr.relay import Relay
from pynostr.base_relay import RelayPolicy
from pynostr.message_pool import MessagePool
import tornado.ioloop
import time
import logging
import threading
from parallax_utils.ascii_anime import display_parallax_run
from parallax_utils.file_util import get_project_root
from parallax_utils.logging_config import get_logger, set_log_level
from parallax_utils.version_check import check_latest_release

logger = get_logger(__name__)

# Set up logging to see debug messages
logging.basicConfig(
    level=logging.DEBUG,
    format='%(asctime)s [%(levelname)s] %(name)s: %(message)s',
    datefmt='%H:%M:%S'
)

scheduler_manage = None
request_handler = RequestHandler()

# Global variable to store Nostr startup task info
_nostr_startup_info = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Lifespan event handler for FastAPI."""

    if _nostr_startup_info is not None:
        model_name = _nostr_startup_info
        asyncio.create_task(process_nostr_events(model_name))
    yield


app = FastAPI(lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# --- Group Key Management (In-Memory) ---
class GroupKeyManager:
    def __init__(self):
        self._keys: Dict[str, str] = {}  

    def add_key(self, group_id: str, key: str):
        self._keys[group_id] = key
        print(f"Added key for group {group_id}")

    def get_key(self, group_id: str) -> Optional[str]:
        return self._keys.get(group_id)

group_key_manager = GroupKeyManager()


def restore_group_keys_from_relay(pub, relays: list, timeout: int = 10, max_pages: int = 10):
    """Restore group keys from historical Kind 4 events on relays (synchronous).
    
    Uses pagination to query historical events in batches, going backwards in time.
    This ensures we can retrieve keys even if there are many events on the relay.
    
    Args:
        pub: Nostr publisher instance
        relays: List of relay URLs
        timeout: Timeout per relay connection (seconds)
        max_pages: Maximum number of pages to query (each page = 1000 events)
    """
    if not pub or not relays:
        return
    
    try:
        priv_key = pub._private_key
        our_pubkey = priv_key.public_key.hex()
        print(f"Restoring group keys from relays (our pubkey: {our_pubkey[:16]}...)...")
        
        current_time = int(time.time())
        page_size = 1000  
        days_per_page = 30  
        seconds_per_page = days_per_page * 24 * 3600
        restored_count = 0
        total_events_received = 0
        
        for page in range(max_pages):
            if page == 0:
                until_time = current_time
                since_time = current_time - seconds_per_page
            else:
                until_time = since_time  
                since_time = until_time - seconds_per_page  
            
            print(f"Querying page {page + 1}/{max_pages} (since={since_time}, until={until_time})...")
            
            message_pool = MessagePool(first_response_only=False)
            policy = RelayPolicy()
            relay_connections = []
            
            filters = FiltersList([
                Filters(
                    kinds=[INVITE_KIND],
                    since=since_time,
                    until=until_time,
                    limit=page_size,
                )
            ])
            subscription_id = uuid.uuid4().hex
        
            for relay_url in relays:
                try:
                    io_loop = tornado.ioloop.IOLoop()
                    relay = Relay(
                        relay_url,
                        message_pool,
                        io_loop,
                        policy,
                        timeout=timeout,
                        close_on_eose=True
                    )
                    relay.add_subscription(subscription_id, filters)
                    relay_connections.append((relay, io_loop))
                except Exception as e:
                    print(f"Failed to create relay connection for {relay_url}: {e}")
            
            if not relay_connections:
                if page == 0:
                    print("No relay connections available for key restore")
                    return
                else:
                    break
            
            events_received = []
            start_time = time.time()
            
            def collect_events():
                """Collect events from message pool."""
                while time.time() - start_time < timeout:
                    if message_pool.has_events():
                        event_msg = message_pool.get_event()
                        ev = getattr(event_msg, "event", None)
                        if ev is not None:
                            events_received.append(ev)
                    elif message_pool.has_eose_notices():
                        message_pool.get_eose_notice()
                        break
                    time.sleep(0.1)
            

            for relay, io_loop in relay_connections:
                try:
                    io_loop.run_sync(lambda: relay.connect(), timeout=timeout)
                    collect_thread = threading.Thread(target=collect_events, daemon=True)
                    collect_thread.start()
                    collect_thread.join(timeout=timeout)
                except Exception as e:
                    print(f"Failed to connect to relay {relay.url}: {e}")
                finally:
                    try:
                        relay.close()
                    except Exception:
                        pass
            
            if len(events_received) == 0:
                print(f"No events found in this time range, stopping pagination")
                break
            
            print(f"  Received {len(events_received)} events in this page")
            total_events_received += len(events_received)
            
            page_restored = 0
            for event in events_received:
                try:
                    tags = getattr(event, "tags", [])
                    p_tags = [t[1] for t in tags if len(t) > 1 and t[0] == "p"]
                    # Filter events: only process events where our pubkey is in the p tags
                    if our_pubkey not in p_tags:
                        continue
                    
                    shared_secret = priv_key.compute_shared_secret(event.pubkey).hex()
                    content = Nip04Crypto.decrypt(event.content, shared_secret)
                    content = content.strip()
                    
                    try:
                        data = json.loads(content)
                        if data.get("type") == "invite":
                            group_id = data.get("group_id")
                            key = data.get("key")
                            if group_id and key:
                                if group_key_manager.get_key(group_id) is None:
                                    group_key_manager.add_key(group_id, key)
                                    restored_count += 1
                                    page_restored += 1
                                    print(f"  ✓ Restored key for group {group_id}")


                    except json.JSONDecodeError:
                        continue

                except Exception as e:
                    print(f"Failed to restore key from event {event.id[:8] if hasattr(event, 'id') else 'unknown'}...: {e}")
                    continue
            
            if page_restored > 0:
                print(f"  Restored {page_restored} key(s) from this page")
            

            if len(events_received) < page_size:
                print(f"Reached end of available events (got {len(events_received)} < {page_size})")
                break
        
        print(f"Restored {restored_count} group key(s) from relay history (queried {total_events_received} total events)")
            
    except Exception as e:
        print(f"Failed to restore group keys from relay: {e}")


async def process_nostr_events(target_model_name: str):
    """Async loop to process Nostr events for this scheduler/agent."""
    pub = get_publisher()
    if pub is None:
        return

    event_channel = pub.get_event_channel()
    print(f"Started Nostr event processing loop for model: {target_model_name}")

    while True:
        try:
            try:
                event = event_channel.get_nowait()
            except queue.Empty:
                await asyncio.sleep(0.5)
                continue

            if request_handler.scheduler_manage is None:
                await asyncio.sleep(1)
                continue

            kind = getattr(event, "kind", None)
            current_time = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime())
            print(f"[Nostr] [{current_time}] Received event: kind={kind}, id={getattr(event, 'id', 'unknown')[:8]}...")
            
            # --- Handle Kind 4 (Invite / Group Key Distribution) ---
            if kind == INVITE_KIND:
                try:
                    priv_key = pub._private_key
                    shared_secret = priv_key.compute_shared_secret(event.pubkey).hex()
                    content = Nip04Crypto.decrypt(event.content, shared_secret)
                    content = content.strip()
                    try:
                        data = json.loads(content)
                        if data.get("type") == "invite":
                            group_id = data.get("group_id")
                            key = data.get("key")
                            if group_id and key:
                                group_key_manager.add_key(group_id, key)
                                print(f"Received invite for group {group_id}")
                        else:
                            print(f"Kind 4 event {event.id[:8]}... is not an invite (type: {data.get('type', 'unknown')})")
                    except json.JSONDecodeError:
                        print(f"Kind 4 event {event.id[:8]}... is not in JSON format (likely a regular DM, skipping)")
                        continue
                        
                except Exception as e:
                    print(f"Failed to process Kind 4 event {event.id[:8]}...: {e}")

            # --- Handle Kind 42 (Chat Request) ---
            elif kind == CHAT_KIND:
                tags = getattr(event, "tags", [])
                model_tag = next((t[1] for t in tags if t[0] == "model"), None)
                if model_tag != target_model_name:
                    continue

                try:
                    group_id = next((t[1] for t in tags if t[0] == "d"), None)
                    if not group_id:
                        continue

                    shared_key = group_key_manager.get_key(group_id)
                    if not shared_key:
                        continue

                    payload = GroupV1Crypto.decrypt(event.content, shared_key)
                    user_text = payload.get("text", "")
                    
                    if not user_text:
                        continue

                    evm_address = next((t[1] for t in tags if t[0] == "evm_address"), None)

                    print(f"Processing chat request from Nostr: {user_text[:50]}...")
                    request_id = str(uuid.uuid4())
                    received_ts = time.time()
                    request_data = {
                        "model": model_tag,
                        "messages": [{"role": "user", "content": user_text, "description": "Nostr AI Agents Encrypted Group Chat"}],
                        "stream": True, 
                        "extra_body": {
                            "group_id": group_id,
                            "group_key": shared_key,
                            "reply_to_event_id": event.id,
                            "user_pubkey": event.pubkey,
                            "agent_name": target_model_name,
                        }
                    }
                    # Add evm_address to extra_body if present
                    if evm_address:
                        request_data["extra_body"]["evm_address"] = evm_address
                    response = await request_handler.v1_chat_completions(request_data, request_id, received_ts)
                    if isinstance(response, StreamingResponse):
                        async for _ in response.body_iterator:
                            pass 
                    
                    print(f"Finished processing Nostr request {request_id}")

                except Exception as e:
                    print(f"Error processing Kind 42 event {event.id}: {e}")

        except Exception as e:
            print(f"Error in Nostr event loop: {e}")
            await asyncio.sleep(1)



@app.post("/weight/refit")
async def weight_refit(raw_request: Request):
    request_data = await raw_request.json()
    status = scheduler_manage.weight_refit(request_data)
    if status:
        return JSONResponse(
            content={
                "type": "weight_refit",
                "data": None,
            },
            status_code=200,
        )
    else:
        return JSONResponse(
            content={
                "type": "weight_refit",
                "data": "Sever not ready",
            },
            status_code=500,
        )


@app.get("/weight/refit/timestamp")
async def weight_refit_timstamp():
    last_refit_time = scheduler_manage.get_last_refit_time()

    return JSONResponse(
        content={
            "latest_timestamp": last_refit_time,
        },
        status_code=200,
    )


@app.get("/model/list")
async def model_list():
    return JSONResponse(
        content={
            "type": "model_list",
            "data": get_model_list(),
        },
        status_code=200,
    )


@app.post("/scheduler/init")
async def scheduler_init(raw_request: Request):
    request_data = await raw_request.json()
    model_name = request_data.get("model_name")
    init_nodes_num = request_data.get("init_nodes_num")
    is_local_network = request_data.get("is_local_network")

    # Validate required parameters
    if model_name is None:
        return JSONResponse(
            content={
                "type": "scheduler_init",
                "error": "model_name is required",
            },
            status_code=400,
        )
    if init_nodes_num is None:
        return JSONResponse(
            content={
                "type": "scheduler_init",
                "error": "init_nodes_num is required",
            },
            status_code=400,
        )

    try:
        # If scheduler is already running, stop it first
        if scheduler_manage.is_running():
            print(f"Stopping existing scheduler to switch to model: {model_name}")
            scheduler_manage.stop()

        # Start scheduler with new model
        print(
            f"Initializing scheduler with model: {model_name}, init_nodes_num: {init_nodes_num}"
        )
        scheduler_manage.run(model_name, init_nodes_num, is_local_network)

        return JSONResponse(
            content={
                "type": "scheduler_init",
                "data": {
                    "model_name": model_name,
                    "init_nodes_num": init_nodes_num,
                    "is_local_network": is_local_network,
                },
            },
            status_code=200,
        )
    except Exception as e:
        logger.exception(f"Error initializing scheduler: {e}")
        return JSONResponse(
            content={
                "type": "scheduler_init",
                "error": str(e),
            },
            status_code=500,
        )


@app.get("/node/join/command")
async def node_join_command():
    peer_id = scheduler_manage.get_peer_id()
    is_local_network = scheduler_manage.get_is_local_network()

    return JSONResponse(
        content={
            "type": "node_join_command",
            "data": get_node_join_command(peer_id, is_local_network),
        },
        status_code=200,
    )


@app.get("/cluster/status")
async def cluster_status():
    async def stream_cluster_status():
        while True:
            yield json.dumps(scheduler_manage.get_cluster_status(), ensure_ascii=False) + "\n"
            await asyncio.sleep(1)

    return StreamingResponse(
        stream_cluster_status(),
        media_type="application/x-ndjson",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
        },
    )


@app.get("/cluster/status_json")
async def cluster_status_json() -> JSONResponse:
    if scheduler_manage is None:
        return JSONResponse(content={"error": "Scheduler is not initialized"}, status_code=503)
    return JSONResponse(content=scheduler_manage.get_cluster_status(), status_code=200)


@app.get("/node/status/{account}")
async def get_node_status_by_account(account: str):
    """
    Get detailed worker node status by EVM address (account).
    
    Args:
        account: EVM address of the worker node (e.g., 0x789...)
    
    Returns:
        Detailed node information including:
        - Basic info: node_id, account, status
        - Hardware: GPU info, memory, compute power
        - Layer allocation: start_layer, end_layer, num_layers
        - Performance: current_requests, max_requests, latency
        - Network: RTT to other nodes
        - Timing: last_heartbeat, last_refit_time
    """
    if scheduler_manage is None:
        return JSONResponse(
            content={"error": "Scheduler is not initialized"},
            status_code=503,
        )
    
    node_info = scheduler_manage.get_node_by_account(account)
    
    if node_info is None:
        return JSONResponse(
            content={
                "type": "node_status",
                "error": f"Node with account {account} not found",
            },
            status_code=404,
        )
    
    return JSONResponse(
        content={
            "type": "node_status",
            "data": node_info,
        },
        status_code=200,
    )


@app.post("/v1/chat/completions")
async def openai_v1_chat_completions(raw_request: Request):
    request_data = await raw_request.json()
    request_id = uuid.uuid4()
    received_ts = time.time()
    return await request_handler.v1_chat_completions(request_data, request_id, received_ts)


# Disable caching for index.html
@app.get("/")
async def serve_index():
    response = FileResponse(str(get_project_root()) + "/src/frontend/dist/index.html")
    # Disable cache
    response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    response.headers["Pragma"] = "no-cache"
    response.headers["Expires"] = "0"
    return response


# mount the frontend
app.mount(
    "/",
    StaticFiles(directory=str(get_project_root() / "src" / "frontend" / "dist"), html=True),
    name="static",
)

if __name__ == "__main__":
    args = parse_args()
    set_log_level(args.log_level)
    print(f"args: {args}")
    # Debug print to verify nostr args
    print(f"Debug: nostr_privkey={getattr(args, 'nostr_privkey', 'Not Set')}")

    if args.model_name is None:
        init_model_info_dict_cache(args.use_hfcache)

    if args.log_level != "DEBUG":
        display_parallax_run()

    check_latest_release()

    # Initialize global Nostr publisher for scheduler node if configured.
    if getattr(args, "nostr_privkey", None):
        relays = getattr(args, "nostr_relays", None) or []
        try:
            init_global_publisher(
                args.nostr_privkey,
                relays=relays,
                sid="prakasa-main",
                role="scheduler",
            )
            # Set global variable for lifespan to use
            _nostr_startup_info = args.model_name
            
            # Restore group keys from relay history after initialization
            pub = get_publisher()
            if pub and relays:
                try:
                    restore_group_keys_from_relay(pub, relays, timeout=10)
                except Exception as e:
                    logger.warning(f"Failed to restore group keys from relay during startup: {e}")
                
        except Exception as e:
            print(f"Failed to initialize Nostr publisher (scheduler): {e}")

    scheduler_manage = SchedulerManage(
        initial_peers=args.initial_peers,
        relay_servers=args.relay_servers,
        dht_prefix=args.dht_prefix,
        host_maddrs=[
            f"/ip4/0.0.0.0/tcp/{args.tcp_port}",
            f"/ip4/0.0.0.0/udp/{args.udp_port}/quic-v1",
        ],
        announce_maddrs=args.announce_maddrs,
        http_port=args.port,
        use_hfcache=args.use_hfcache,
        enable_weight_refit=args.enable_weight_refit,
        eth_account=args.eth_account,
        p2p_usage_api_url=getattr(args, "p2p_usage_api_url", None),
    )

    request_handler.set_scheduler_manage(scheduler_manage)

    if getattr(args, "p2p_usage_api_url", None):
        request_handler.set_p2p_usage_api_url(args.p2p_usage_api_url)

    model_name = args.model_name
    init_nodes_num = args.init_nodes_num
    is_local_network = args.is_local_network
    if model_name is not None and init_nodes_num is not None:
        scheduler_manage.run(model_name, init_nodes_num, is_local_network)

    host = args.host
    port = args.port

    uvicorn.run(app, host=host, port=port, log_level="info", loop="uvloop")
