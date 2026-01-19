import asyncio
import json
import os
import time
import traceback
import uuid
import queue
from types import SimpleNamespace
from contextlib import asynccontextmanager
from pathlib import Path
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
from pynostr.key import PrivateKey
from pynostr.filters import Filters, FiltersList
from pynostr.relay import Relay
from pynostr.base_relay import RelayPolicy
from pynostr.message_pool import MessagePool
import tornado.ioloop
import time
import logging
import requests
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
# Global cache for NPC info from P2P explore API
_npc_cache_by_pubkey: Dict[str, Dict] = {}
_restore_state = {
    "last_seen_ts": None,
    "processed_event_ids": set(),
}
_local_event_queue: "queue.Queue" = queue.Queue()
_seen_chat_event_ids: set = set()
_local_event_url: Optional[str] = None
_MAX_PROCESSED_EVENT_IDS = 5000
_RESTORE_OVERLAP_SECONDS = 30 * 60


def _get_cache_paths() -> Dict[str, Path]:
    cache_dir = os.environ.get("PRAKASA_CACHE_DIR")
    if cache_dir:
        base = Path(cache_dir).expanduser()
    else:
        base = get_project_root() / ".cache"
    base.mkdir(parents=True, exist_ok=True)
    return {
        "group_keys": base / "group_keys_cache.json",
    }


def _load_persistent_cache() -> None:
    """Load group keys and invite restore state from disk."""
    global _restore_state
    paths = _get_cache_paths()
    cache_file = paths["group_keys"]
    if not cache_file.exists():
        return
    try:
        data = json.loads(cache_file.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            keys = data.get("group_keys", {})
            if isinstance(keys, dict):
                for gid, key in keys.items():
                    if gid and key:
                        group_key_manager.add_key(gid, key)
            last_seen_ts = data.get("last_seen_ts")
            if isinstance(last_seen_ts, int):
                _restore_state["last_seen_ts"] = last_seen_ts
            processed = data.get("processed_event_ids", [])
            if isinstance(processed, list):
                _restore_state["processed_event_ids"] = set(
                    [pid for pid in processed if isinstance(pid, str)]
                )
    except Exception as e:
        print(f"Failed to load group keys cache from {cache_file}: {e}")


def _save_persistent_cache() -> None:
    """Persist group keys and invite restore state to disk."""
    paths = _get_cache_paths()
    cache_file = paths["group_keys"]
    try:
        processed_ids = list(_restore_state.get("processed_event_ids", set()))
        if len(processed_ids) > _MAX_PROCESSED_EVENT_IDS:
            processed_ids = processed_ids[-_MAX_PROCESSED_EVENT_IDS :]
        payload = {
            "group_keys": dict(group_key_manager._keys),
            "last_seen_ts": _restore_state.get("last_seen_ts"),
            "processed_event_ids": processed_ids,
        }
        cache_file.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    except Exception as e:
        print(f"Failed to save group keys cache to {cache_file}: {e}")


def _get_npc_private_key(pubkey: str) -> Optional[PrivateKey]:
    npc = _npc_cache_by_pubkey.get(pubkey)
    if not npc:
        return None
    cached = npc.get("_privkey_obj")
    if cached is not None:
        return cached

    private_key = npc.get("private_key")
    if not private_key:
        return None

    try:
        if isinstance(private_key, str) and private_key.startswith("nsec"):
            pk = PrivateKey.from_nsec(private_key)
        else:
            try:
                pk = PrivateKey.from_hex(private_key)
            except Exception:
                pk = PrivateKey(bytes.fromhex(private_key))
        npc["_privkey_obj"] = pk
        return pk
    except Exception as e:
        print(f"Failed to parse NPC private key for pubkey {pubkey[:8]}...: {e}")
        return None


def _record_invite_event(event_id: Optional[str], created_at: Optional[int]) -> None:
    if event_id:
        _restore_state["processed_event_ids"].add(event_id)
    if isinstance(created_at, int):
        prev = _restore_state.get("last_seen_ts")
        if prev is None or created_at > prev:
            _restore_state["last_seen_ts"] = created_at


def _store_group_key(group_id: str, key: str) -> None:
    group_key_manager.add_key(group_id, key)
    _save_persistent_cache()


def _load_npc_cache(p2p_usage_api_url: Optional[str], access_code: Optional[str]) -> None:
    """Load NPC info from external P2P explore API into memory."""
    global _npc_cache_by_pubkey
    if not p2p_usage_api_url:
        print("p2p_usage_api_url not set; skipping NPC cache load")
        _npc_cache_by_pubkey = {}
        return

    url = f"{p2p_usage_api_url.rstrip('/')}/api/v1/chat/p2p/explore"
    try:
        params = {}
        if access_code:
            params["access_code"] = access_code
        response = requests.get(url, params=params, timeout=10)
        response.raise_for_status()
        data = response.json()
        items = data.get("items", []) if isinstance(data, dict) else []
        cache: Dict[str, Dict] = {}
        for item in items:
            pubkey = item.get("public_key")
            if pubkey:
                cache[pubkey] = {
                    "name": item.get("name"),
                    "icon": item.get("icon"),
                    "public_key": pubkey,
                    "private_key": item.get("private_key"),
                }
        _npc_cache_by_pubkey = cache
        print(f"Loaded {len(_npc_cache_by_pubkey)} NPC entries from P2P explore API")
    except Exception as e:
        print(f"Failed to load NPC cache from {url}: {e}")
        _npc_cache_by_pubkey = {}


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


@app.post("/internal/nostr/local_event")
async def ingest_local_nostr_event(raw_request: Request):
    """Inject locally published Nostr event to avoid relay latency."""
    try:
        data = await raw_request.json()
        event = SimpleNamespace(
            id=data.get("id"),
            pubkey=data.get("pubkey"),
            created_at=data.get("created_at"),
            kind=data.get("kind"),
            tags=data.get("tags") or [],
            content=data.get("content") or "",
        )
        _local_event_queue.put(event)
        return JSONResponse(content={"ok": True}, status_code=200)
    except Exception as e:
        return JSONResponse(content={"error": str(e)}, status_code=400)


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


def restore_group_keys_from_relay_for_npcs(
    relays: list, timeout: int = 10, max_pages: Optional[int] = None
):
    """Restore group keys from historical Kind 4 events for all NPC accounts."""
    if not relays or not _npc_cache_by_pubkey:
        return

    try:
        npc_pubkeys = set(_npc_cache_by_pubkey.keys())
        print(f"Restoring group keys for {len(npc_pubkeys)} NPCs from relays...")

        current_time = int(time.time())
        page_size = 1000
        days_per_page = 30
        seconds_per_page = days_per_page * 24 * 3600
        restored_count = 0
        total_events_received = 0
        page = 0
        last_seen_ts = _restore_state.get("last_seen_ts")
        incremental_mode = isinstance(last_seen_ts, int)

        while True:
            if max_pages is not None and page >= max_pages:
                break
            if page == 0:
                until_time = current_time
                if incremental_mode:
                    since_time = max(0, last_seen_ts - _RESTORE_OVERLAP_SECONDS)
                else:
                    since_time = current_time - seconds_per_page
            else:
                until_time = since_time
                since_time = until_time - seconds_per_page

            max_pages_label = "∞" if max_pages is None else str(max_pages)
            print(f"Querying page {page + 1}/{max_pages_label} (since={since_time}, until={until_time})...")

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
                    event_id = getattr(event, "id", None)
                    if event_id and event_id in _restore_state["processed_event_ids"]:
                        continue

                    tags = getattr(event, "tags", [])
                    p_tags = [t[1] for t in tags if len(t) > 1 and t[0] == "p"]
                    matching_pubkeys = [p for p in p_tags if p in npc_pubkeys]
                    if not matching_pubkeys:
                        continue

                    for recipient_pubkey in matching_pubkeys:
                        priv_key = _get_npc_private_key(recipient_pubkey)
                        if not priv_key:
                            continue

                        try:
                            shared_secret = priv_key.compute_shared_secret(event.pubkey).hex()
                            content = Nip04Crypto.decrypt(event.content, shared_secret)
                            content = content.strip()
                        except Exception:
                            continue

                        try:
                            data = json.loads(content)
                            if data.get("type") == "invite":
                                group_id = data.get("group_id")
                                key = data.get("key")
                                if group_id and key:
                                    if group_key_manager.get_key(group_id) is None:
                                        _store_group_key(group_id, key)
                                        restored_count += 1
                                        page_restored += 1
                                        print(f"  ✓ Restored key for group {group_id}")
                                    _record_invite_event(event_id, getattr(event, "created_at", None))
                                    break
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
            page += 1
            if incremental_mode:
                break

        print(f"Restored {restored_count} group key(s) from relay history (queried {total_events_received} total events)")

    except Exception as e:
        print(f"Failed to restore group keys from relay: {e}")


async def process_nostr_events(target_model_name: str):
    """Async loop to process Nostr events for this scheduler/agent."""
    import asyncio  # Ensure asyncio is accessible in this scope
    
    pub = get_publisher()
    if pub is None:
        return

    event_channel = pub.get_event_channel()
    print(f"Started Nostr event processing loop for model: {target_model_name}")

    while True:
        try:
            try:
                event = _local_event_queue.get_nowait()
            except queue.Empty:
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
                    tags = getattr(event, "tags", [])
                    p_tags = [t[1] for t in tags if len(t) > 1 and t[0] == "p"]
                    matching_pubkeys = [p for p in p_tags if p in _npc_cache_by_pubkey]
                    if not matching_pubkeys:
                        continue

                    event_id = getattr(event, "id", None)
                    if event_id and event_id in _restore_state["processed_event_ids"]:
                        continue

                    content = None
                    for recipient_pubkey in matching_pubkeys:
                        priv_key = _get_npc_private_key(recipient_pubkey)
                        if not priv_key:
                            continue
                        shared_secret = priv_key.compute_shared_secret(event.pubkey).hex()
                        try:
                            content = Nip04Crypto.decrypt(event.content, shared_secret)
                            content = content.strip()
                            break
                        except Exception:
                            continue
                    if not content:
                        continue
                    try:
                        data = json.loads(content)
                        if data.get("type") == "invite":
                            group_id = data.get("group_id")
                            key = data.get("key")
                            if group_id and key:
                                _store_group_key(group_id, key)
                                _record_invite_event(event_id, getattr(event, "created_at", None))
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
                event_id = getattr(event, "id", None)
                if event_id and event_id in _seen_chat_event_ids:
                    continue
                model_tag = next((t[1] for t in tags if t[0] == "model"), None)
                if model_tag != target_model_name:
                    print(f"Received chat request for model {model_tag} but it's not for us")
                    continue

                try:
                    group_id = next((t[1] for t in tags if t[0] == "d"), None)
                    if not group_id:
                        print(f"Received chat request for group {group_id} but it's not for us")
                        continue

                    shared_key = group_key_manager.get_key(group_id)
                    if not shared_key:
                        print(f"Received chat request for group {group_id} but it doesn't have a shared key")
                        continue

                    payload = GroupV1Crypto.decrypt(event.content, shared_key)
                    if payload.get("is_streaming") or payload.get("type") == "text_chunk":
                        print(f"Received streaming chunk for group {group_id}, waiting for final message")
                        continue
                    raw_text = payload.get("text", "")
                    # Prefer forwarding only the final answer (strip <think> blocks if present)
                    user_text = raw_text
                    if isinstance(raw_text, str):
                        if "</think>" in raw_text:
                            user_text = raw_text.split("</think>", 1)[1].lstrip()
                        else:
                            user_text = raw_text.replace("<think>", "").strip()
                    
                    if not user_text:
                        print(f"Received chat request for group {group_id} but it doesn't have a user text")
                        continue

                    # Multi-agent orchestration checks (match logical agent pubkey)
                    current_target = payload.get("current_target")
                    remaining_chain = payload.get("remaining_chain")
                    if current_target:
                        if current_target not in _npc_cache_by_pubkey:
                            print(f"Received chat request for group {group_id} but it doesn't have a current target")
                            continue
                    elif isinstance(remaining_chain, list) and remaining_chain:
                        first_agent_pubkey = None
                        first_agent = remaining_chain[0]
                        if isinstance(first_agent, dict):
                            first_agent_pubkey = first_agent.get("pubkey")
                        if not first_agent_pubkey or first_agent_pubkey not in _npc_cache_by_pubkey:
                            print(f"Received chat request for group {group_id} but it doesn't have a first agent pubkey")
                            continue

                    evm_address = next((t[1] for t in tags if t[0] == "evm_address"), None)

                    # Extract agent_name and agent_avatar from user's payload
                    user_agent_name = payload.get("agent_name")
                    user_agent_avatar = payload.get("agent_avatar")
                    is_relay_message = payload.get("is_relay_message")
                    history_ids = payload.get("history_ids")
                    if not isinstance(history_ids, list):
                        history_ids = []
                    
                    # If no next target exists, skip.
                    target_pubkey = current_target
                    if not target_pubkey and isinstance(remaining_chain, list) and remaining_chain:
                        first_agent = remaining_chain[0]
                        if isinstance(first_agent, dict):
                            target_pubkey = first_agent.get("pubkey")
                    if is_relay_message and not target_pubkey:
                        print(f"Received relay message for group {group_id} without next target, skipping")
                        continue

                    print(f"Processing chat request from Nostr: {user_text[:50]}...")
                    request_id = str(uuid.uuid4())
                    received_ts = time.time()

                    # Check if target_pubkey is in the local NPC cache
                    npc_info = _npc_cache_by_pubkey.get(target_pubkey) if target_pubkey else None
                    if npc_info:
                        final_agent_name = npc_info.get("name") or target_model_name
                        final_agent_avatar = npc_info.get("icon")
                    else:
                        final_agent_name = user_agent_name or target_model_name
                        final_agent_avatar = user_agent_avatar
                    
                    request_data = {
                        "model": model_tag,
                        "messages": [{"role": "user", "content": user_text, "description": "Nostr AI Agents Encrypted Group Chat"}],
                        "stream": True, 
                        "extra_body": {
                            "group_id": group_id,
                            "group_key": shared_key,
                            "reply_to_event_id": event.id,
                            "user_pubkey": event.pubkey,
                            "agent_name": final_agent_name,
                            "agent_avatar": final_agent_avatar,
                            "current_target": current_target,
                            "remaining_chain": remaining_chain,
                            "is_relay_message": is_relay_message,
                            "history_ids": history_ids,
                            "local_event_url": _local_event_url,
                            "local_npc_pubkeys": list(_npc_cache_by_pubkey.keys()),
                        }
                    }
                    print(f"Request data: {request_data}")
                    # Add evm_address to extra_body if present
                    if evm_address:
                        request_data["extra_body"]["evm_address"] = evm_address
                    response = await request_handler.v1_chat_completions(request_data, request_id, received_ts)
                    if isinstance(response, StreamingResponse):
                        async for _ in response.body_iterator:
                            pass 
                    if event_id:
                        _seen_chat_event_ids.add(event_id)
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
    relays = getattr(args, "nostr_relays", None) or []
    if getattr(args, "nostr_privkey", None):
        try:
            init_global_publisher(
                args.nostr_privkey,
                relays=relays,
                sid="prakasa-main",
                role="scheduler",
            )
            # Set global variable for lifespan to use
            _nostr_startup_info = args.model_name
            
        except Exception as e:
            print(f"Failed to initialize Nostr publisher (scheduler): {e}")

    # Set p2p_usage_api_url early, before any requests can be processed
    if getattr(args, "p2p_usage_api_url", None):
        request_handler.set_p2p_usage_api_url(args.p2p_usage_api_url)
    request_handler.set_access_code(getattr(args, "access_code", None))

    local_host = args.host if args.host and args.host != "0.0.0.0" else "127.0.0.1"
    _local_event_url = f"http://{local_host}:{args.port}/internal/nostr/local_event"

    # Load NPC cache when starting the scheduler
    _load_npc_cache(getattr(args, "p2p_usage_api_url", None), getattr(args, "access_code", None))
    # Load persisted group keys and invite restore state
    _load_persistent_cache()
    # Restore group keys for all NPCs from relay history
    if relays:
        try:
            restore_group_keys_from_relay_for_npcs(relays, timeout=10, max_pages=None)
        except Exception as e:
            print(f"Failed to restore NPC group keys from relay during startup: {e}")
    
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
        access_code=getattr(args, "access_code", None),
    )

    request_handler.set_scheduler_manage(scheduler_manage)

    model_name = args.model_name
    init_nodes_num = args.init_nodes_num
    is_local_network = args.is_local_network
    if model_name is not None and init_nodes_num is not None:
        scheduler_manage.run(model_name, init_nodes_num, is_local_network)

    host = args.host
    port = args.port

    uvicorn.run(app, host=host, port=port, log_level="info", loop="uvloop")
