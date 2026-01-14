import asyncio
import json
import time
from typing import Dict, Optional

import aiohttp
from fastapi.responses import JSONResponse, StreamingResponse
from starlette.concurrency import iterate_in_threadpool

from backend.server.constants import NODE_STATUS_AVAILABLE
from parallax_utils.logging_config import get_logger
from parallax_utils.request_metrics import get_request_metrics
from prakasa_nostr import get_publisher
from prakasa_nostr.events import (
    Assignment,
    ModelRef,
    RoutingInfo,
    SchedulerAssignmentContent,
    SchedulerAssignmentEvent,
    TaskPublishEvent,
)

logger = get_logger(__name__)

AIOHTTP_TIMEOUT = aiohttp.ClientTimeout(total=20 * 60 * 60)


class RequestHandler:
    """HTTP request forwarder with scheduler-aware routing and retry logic.

    Behavior for routing resolution:
    - routing_table is None: scheduler has not decided yet -> treat as error for this attempt
    - routing_table is []: all pipelines are full now -> retry up to max attempts
    - routing_table is non-empty: forward to first hop
    """

    MAX_ROUTING_RETRY = 20
    RETRY_DELAY_SEC = 5

    def __init__(self):
        self.scheduler_manage = None
        self.stubs = {}
        self.p2p_usage_api_url = None

    def set_scheduler_manage(self, scheduler_manage):
        self.scheduler_manage = scheduler_manage

    def set_p2p_usage_api_url(self, api_url: str):
        """Set the P2P usage API base URL for logging token usage."""
        self.p2p_usage_api_url = api_url

    def _get_workers_info(self, routing_table: list) -> list:
        """Get worker information (node_id and account) from routing table.
        
        Args:
            routing_table: List of node IDs participating in the request
            
        Returns:
            List of dicts with 'node_id' and 'account' (EVM address) for each worker
        """
        workers = []
        if not routing_table or not self.scheduler_manage or not self.scheduler_manage.scheduler:
            return workers
        
        scheduler = self.scheduler_manage.scheduler
        for node_id in routing_table:
            if node_id in scheduler.node_id_to_node:
                node = scheduler.node_id_to_node[node_id]
                worker_info = {
                    "node_id": node_id,
                    "account": node.account if node.account else None,
                }
                workers.append(worker_info)
            else:
                logger.warning(f"Node {node_id} not found in scheduler.node_id_to_node")
                workers.append({
                    "node_id": node_id,
                    "account": None,
                })
        
        return workers

    async def _log_p2p_usage(
        self,
        address: str,
        model_name: str,
        input_tokens: int,
        output_tokens: int,
        workers: list,
        api_key: Optional[str] = None,
    ):
        """Log token usage to P2P usage API.
        
        Args:
            address: User's EVM address
            model_name: Model name used for the request
            input_tokens: Total input tokens
            output_tokens: Total output tokens
            workers: List of worker information (dict with 'node_id' and 'account')
            api_key: Optional API key
        """
        if not self.p2p_usage_api_url:
            logger.debug("P2P usage API URL not configured, skipping usage log")
            return

        try:
            url = f"{self.p2p_usage_api_url.rstrip('/')}/api/v1/subnet/p2p/usage"
            payload = {
                "address": address,
                "api_key": api_key or "",
                "model_name": model_name,
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
                "workers": workers,
            }

            async with aiohttp.ClientSession(timeout=AIOHTTP_TIMEOUT) as session:
                async with session.post(url, json=payload) as response:
                    if response.status == 200:
                        logger.debug(f"Successfully logged P2P usage for address {address}")
                    else:
                        logger.warning(
                            f"Failed to log P2P usage: HTTP {response.status}, "
                            f"response: {await response.text()}"
                        )
        except Exception as e:
            logger.warning(f"Error logging P2P usage: {e}", exc_info=True)

    def get_stub(self, node_id):
        if node_id not in self.stubs:
            self.stubs[node_id] = self.scheduler_manage.completion_handler.get_stub(node_id)
        return self.stubs[node_id]

    def _publish_task_event(self, request_data: Dict) -> Optional[str]:
        """Publish a CIP-09 dinf_task_publish event for this HTTP request (best-effort)."""
        try:
            pub = get_publisher()
            if pub is None or self.scheduler_manage is None:
                return
            
            # Derive a short human description from the last chat message if present.
            description = "chat_completion"
            messages = request_data.get("messages")
            
            # Calculate total message size for difficulty estimation
            total_message_size = 0
            if isinstance(messages, list) and messages:
                for msg in messages:
                    if isinstance(msg, dict) and "content" in msg:
                        total_message_size += len(str(msg.get("content", "")))
                
                last_msg = messages[-1]
                if isinstance(last_msg, dict) and "content" in last_msg:
                    # Truncate to avoid excessively large content in the event
                    description = str(last_msg.get("content", ""))[:256] or description
                if isinstance(last_msg, dict) and "description" in last_msg:
                    description = str(last_msg.get("description", ""))[:256] or description
            
            # Determine difficulty based on total message size
            if total_message_size < 500:
                difficulty = "easy"
            elif total_message_size < 2000:
                difficulty = "medium"
            else:
                difficulty = "hard"

            model_name = self.scheduler_manage.get_model_name() or "unknown"
            num_layers = getattr(
                getattr(self.scheduler_manage, "scheduler", None), "num_layers", 0
            ) or 0

            task_event = TaskPublishEvent.from_plaintext(
                sid="prakasa-main",
                description=description,
                model_name=model_name,
                num_layers=int(num_layers),
                category="inference",
                difficulty=difficulty,
            )
            logger.debug(f"_publish_task_event: publishing task event for model={model_name}")
            pub.publish_event(task_event)
            return task_event.id
        except Exception as e:
            # Nostr publishing errors are logged at debug level only.
            logger.debug(f"Failed to publish dinf_task_publish event: {e}", exc_info=True)

    def _publish_assignment_event(self, routing_table: list, request_id: str, task_event_id: Optional[str] = None):
        """Publish a CIP-09 dinf_task_assign event for this inference request (best-effort)."""
        try:
            pub = get_publisher()
            if pub is None:
                logger.debug("_publish_assignment_event: publisher is None, skipping")
                return
            if self.scheduler_manage is None:
                logger.debug("_publish_assignment_event: scheduler_manage is None, skipping")
                return
            if not routing_table:
                logger.debug("_publish_assignment_event: routing_table is empty, skipping")
                return
            
            scheduler = self.scheduler_manage.scheduler
            if scheduler is None:
                logger.debug("_publish_assignment_event: scheduler is None, skipping")
                return
            
            model_info = scheduler.model_info
            model_ref = ModelRef(
                model_name=model_info.model_name,
                num_layers=model_info.num_layers,
                model_version=None,
            )
            
            assignments = []
            for node_id in routing_table:
                node = scheduler.node_id_to_node.get(node_id)
                if node is None:
                    logger.debug(f"_publish_assignment_event: node {node_id} not found in scheduler")
                    continue
                
                assignment = Assignment(
                    worker_pubkey=node.account,
                    node_id=node.node_id,
                    account=node.account,
                    start_layer=node.start_layer,
                    end_layer=node.end_layer,
                    tp_rank=0,
                    tp_size=node.hardware.num_gpus,
                    dp_rank=0,
                    dp_size=1,
                    max_concurrent_requests=node.max_requests,
                    max_sequence_length=node.max_sequence_length,
                    expected_work_units=None,
                )
                assignments.append(assignment)
            
            if not assignments:
                logger.debug(f"_publish_assignment_event: no valid assignments for routing_table={routing_table}")
                return
            
            # Build allocation_id and routing info from the routing path
            allocation_id = f"{request_id}:{model_ref.model_name}:{':'.join(routing_table)}"
            
            routing_info = RoutingInfo(
                pipeline_id=allocation_id,  # Use allocation_id as pipeline_id
                node_path=routing_table,
            )
            
            content = SchedulerAssignmentContent(
                assignments=assignments,
                model=model_ref,
                routing=routing_info,
                deadline=None,
            )
            
            event = SchedulerAssignmentEvent.from_content(
                sid="prakasa-main",
                task_event_id=task_event_id,
                allocation_id=allocation_id,
                content=content,
            )
            logger.debug(f"_publish_assignment_event: publishing event for request_id={request_id}, allocation_id={allocation_id}")
            pub.publish_event(event)
        except Exception as e:
            # Nostr errors must never affect request handling; log at debug only.
            logger.debug(f"Failed to publish dinf_task_assign event: {e}", exc_info=True)

    async def _forward_request(self, request_data: Dict, request_id: str, received_ts: int):
        start_time = time.time()
        logger.debug(f"Forwarding request {request_id}; stream={request_data.get('stream', False)}")
        if (
            self.scheduler_manage is None
            or not self.scheduler_manage.get_schedule_status() == NODE_STATUS_AVAILABLE
        ):
            return JSONResponse(
                content={"error": "Server is not ready"},
                status_code=500,
            )

        # Try to resolve routing; retry if table is an empty list (capacity full)
        attempts = 0
        routing_table = None
        while attempts < self.MAX_ROUTING_RETRY:
            try:
                routing_table = self.scheduler_manage.get_routing_table(request_id, received_ts)
                logger.debug(
                    f"get_routing_table for request {request_id} return: {routing_table} (attempt {attempts+1})"
                )
            except Exception as e:
                logger.exception(f"get_routing_table error: {e}")
                return JSONResponse(
                    content={"error": "Get routing table error"},
                    status_code=500,
                )

            # None -> scheduler has not set yet; treat as hard error (no waiting here)
            if routing_table is None:
                return JSONResponse(
                    content={"error": "Routing pipelines not ready"},
                    status_code=503,
                )

            # Non-empty -> proceed
            if len(routing_table) > 0:
                break

            # Empty list -> capacity full now, retry after short delay
            attempts += 1
            if attempts < self.MAX_ROUTING_RETRY:
                # small async delay before re-forwarding
                await asyncio.sleep(self.RETRY_DELAY_SEC)

        # If still empty after retries, return 429 Too Many Requests
        if routing_table is not None and len(routing_table) == 0:
            return JSONResponse(
                content={"error": "All pipelines are busy or not ready. Please retry later."},
                status_code=429,
            )

        # Optionally publish a CIP-09 dinf_task_publish event for this HTTP request.
        # This is best-effort and must not affect the main request handling flow.
        task_evt_id = self._publish_task_event(request_data)
        
        # Publish a CIP-09 dinf_task_assign event for the routing assignment.
        self._publish_assignment_event(routing_table, request_id, task_evt_id)

        # Add request_id and routing_table to request_data
        request_data["rid"] = str(request_id)
        request_data["routing_table"] = routing_table
        
        # Store task_event_id in extra_body for downstream workload proof events
        if "extra_body" not in request_data:
            request_data["extra_body"] = {}
        if task_evt_id:
            request_data["extra_body"]["task_event_id"] = task_evt_id
        
        # Log which workers are participating in this request
        logger.info(
            f"Request ID: {request_id} | Routing path: {routing_table} | "
            f"Number of workers: {len(routing_table)} | Workers: {', '.join(routing_table)}"
        )
        
        agent_name = self.scheduler_manage.get_model_name() if self.scheduler_manage else None
        if agent_name:
            if "extra_body" not in request_data:
                request_data["extra_body"] = {}
            
            if "agent_name" not in request_data["extra_body"]:
                request_data["extra_body"]["agent_name"] = agent_name
        
        stub = self.get_stub(routing_table[0])
        is_stream = request_data.get("stream", False)
        try:
            if is_stream:

                async def stream_generator():
                    response = stub.chat_completion(request_data)
                    first_token_time = None
                    last_chunk = None
                    last_token_time = None
                    try:
                        iterator = iterate_in_threadpool(response)
                        async for chunk in iterator:
                            last_token_time = time.time()
                            if first_token_time is None:
                                first_token_time = last_token_time
                            if chunk is not None and not chunk.decode("utf-8").startswith(
                                "data: [DONE]"
                            ):
                                last_chunk = chunk
                            yield chunk
                    finally:
                        final_usage = None
                        if last_chunk is not None:
                            try:
                                chunk_str = last_chunk.decode("utf-8")
                                if chunk_str.startswith("data: "):
                                    json_str = chunk_str[6:].strip() 
                                    if json_str:
                                        chunk_data = json.loads(json_str)
                                        if "usage" in chunk_data and chunk_data["usage"]:
                                            final_usage = chunk_data["usage"]
                                            logger.info(
                                                f"Request ID: {request_id} | Final Usage: "
                                                f"prompt_tokens={final_usage.get('prompt_tokens', 0)}, "
                                                f"completion_tokens={final_usage.get('completion_tokens', 0)}, "
                                                f"total_tokens={final_usage.get('total_tokens', 0)}"
                                            )
                            except (json.JSONDecodeError, KeyError, UnicodeDecodeError) as e:
                                logger.debug(f"Failed to extract usage from last chunk: {e}")
                            
                            tps, ttft, input_tokens, output_tokens = get_request_metrics(
                                last_chunk, start_time, first_token_time, last_token_time
                            )
                            if (
                                tps is not None
                                and ttft is not None
                                and input_tokens is not None
                                and output_tokens is not None
                            ):
                                logger.info(
                                    f"Request ID: {request_id} | TPS: {tps:.2f} |  TTFT: {ttft} ms | Output tokens: {output_tokens} | Input tokens: {input_tokens}"
                                )
                            
                            if final_usage:
                                extra_body = request_data.get("extra_body", {})
                                evm_address = extra_body.get("evm_address")
                                if evm_address:
                                    model_name = request_data.get("model", "")
                                    prompt_tokens = final_usage.get("prompt_tokens", 0)
                                    completion_tokens = final_usage.get("completion_tokens", 0)

                                    workers = self._get_workers_info(routing_table)
                                    asyncio.create_task(
                                        self._log_p2p_usage(
                                            address=evm_address,
                                            model_name=model_name,
                                            input_tokens=prompt_tokens,
                                            output_tokens=completion_tokens,
                                            workers=workers,
                                        )
                                    )
                        logger.debug(f"client disconnected for {request_id}")
                        response.cancel()

                resp = StreamingResponse(
                    stream_generator(),
                    media_type="text/event-stream",
                    headers={
                        "X-Content-Type-Options": "nosniff",
                        "Cache-Control": "no-cache",
                    },
                )
                logger.debug(f"Streaming response initiated for {request_id}")
                return resp
            else:
                response = stub.chat_completion(request_data)
                content = (await anext(iterate_in_threadpool(response))).decode()
                logger.debug(f"Non-stream response completed for {request_id}")

                try:
                    response_data = json.loads(content)
                    if "usage" in response_data and response_data["usage"]:
                        final_usage = response_data["usage"]
                        logger.info(
                            f"Request ID: {request_id} | Final Usage: "
                            f"prompt_tokens={final_usage.get('prompt_tokens', 0)}, "
                            f"completion_tokens={final_usage.get('completion_tokens', 0)}, "
                            f"total_tokens={final_usage.get('total_tokens', 0)}"
                        )
                        extra_body = request_data.get("extra_body", {})
                        evm_address = extra_body.get("evm_address")
                        if evm_address:
                            model_name = request_data.get("model", "")
                            prompt_tokens = final_usage.get("prompt_tokens", 0)
                            completion_tokens = final_usage.get("completion_tokens", 0)

                            workers = self._get_workers_info(routing_table)
                            asyncio.create_task(
                                self._log_p2p_usage(
                                    address=evm_address,
                                    model_name=model_name,
                                    input_tokens=prompt_tokens,
                                    output_tokens=completion_tokens,
                                    workers=workers,
                                )
                            )
                except (json.JSONDecodeError, KeyError) as e:
                    logger.debug(f"Failed to extract usage from non-stream response: {e}")

                return JSONResponse(content=json.loads(content))
        except Exception as e:
            logger.exception(f"Error in _forward_request: {e}")
            return JSONResponse(
                content={"error": "Internal server error"},
                status_code=500,
            )

    async def v1_chat_completions(self, request_data: Dict, request_id: str, received_ts: int):
        return await self._forward_request(request_data, request_id, received_ts)
