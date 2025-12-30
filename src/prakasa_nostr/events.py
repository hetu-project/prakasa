"""
CIP-09 Nostr event types for Prakasa Decentralized Inference Network.

All events are thin wrappers around `pynostr.event.Event` with:
- Fixed `kind` integers (30900-30905)
- Stable `op` strings (e.g. "dinf_workload_proof")
- Helper constructors from structured Python data
"""

from __future__ import annotations

import json
from dataclasses import dataclass, asdict
from typing import Any, Dict, List, Optional

from pynostr.event import Event


# Reserved kinds for Prakasa DINF
DINF_TASK_PUBLISH_KIND = 30900
DINF_TASK_ASSIGN_KIND = 30901
DINF_TASK_RESULT_KIND = 30902
DINF_WORKLOAD_PROOF_KIND = 30903
DINF_SETTLEMENT_KIND = 30904
DINF_EVALUATION_KIND = 30905


INVITE_KIND = 4  
CHAT_KIND = 42  
REASONING_KIND = 20000  


class SubspaceOpEvent(Event):
    """
    Base class for all Prakasa "subspace_op" events.

    Adds the common tags:
    - ["d", "subspace_op"]
    - ["sid", "<subspace_id>"]
    - ["op", "<operation_name>"]
    """

    def __init__(
        self,
        *,
        kind: int,
        sid: str,
        op: str,
        content: str = "",
        tags: Optional[List[List[str]]] = None,
    ) -> None:
        base_tags: List[List[str]] = [
            ["d", "subspace_op"],
            ["sid", sid],
            ["op", op],
        ]
        if tags:
            base_tags.extend(tags)
        # `Event` accepts (content, kind, tags, ...) – other fields (pubkey, created_at)
        # are filled in by pynostr during signing.
        super().__init__(content=content, kind=kind, tags=base_tags)


# === Structured content helpers ===


@dataclass
class ModelRef:
    model_name: str
    num_layers: int
    model_version: Optional[str] = None


@dataclass
class LayerRange:
    start_layer: int
    end_layer: int


@dataclass
class Assignment:
    worker_pubkey: str
    node_id: str
    account: Optional[str]
    start_layer: int
    end_layer: int
    tp_rank: int = 0
    tp_size: int = 1
    dp_rank: int = 0
    dp_size: int = 1
    max_concurrent_requests: Optional[int] = None
    max_sequence_length: Optional[int] = None
    expected_work_units: Optional[float] = None


@dataclass
class RoutingInfo:
    pipeline_id: str
    node_path: List[str]


@dataclass
class SchedulerAssignmentContent:
    assignments: List[Assignment]
    model: ModelRef
    routing: Optional[RoutingInfo] = None
    deadline: Optional[int] = None


@dataclass
class ResultMetrics:
    tokens_in: int
    tokens_out: int
    batch_size: int
    wall_time_ms: Optional[float] = None
    compute_time_ms: Optional[float] = None
    io_time_ms: Optional[float] = None
    estimated_flops: Optional[float] = None
    kv_cache_bytes: Optional[int] = None


@dataclass
class WorkerResultContent:
    output_hash: Optional[str] = None
    output_pointer: Optional[str] = None
    metrics: Optional[ResultMetrics] = None
    evidence: Optional[Dict[str, Any]] = None


@dataclass
class WorkloadProofMetrics(ResultMetrics):
    work_units: float = 0.0


@dataclass
class WorkloadProofContent:
    worker_pubkey: str
    node_id: str
    account: Optional[str]
    model: ModelRef
    layer_range: LayerRange
    metrics: WorkloadProofMetrics
    proof: Optional[Dict[str, Any]] = None


# === Concrete event classes ===


class SchedulerAssignmentEvent(SubspaceOpEvent):
    """30901 — Scheduler Assignment (op: dinf_task_assign)."""

    KIND = DINF_TASK_ASSIGN_KIND
    OP = "dinf_task_assign"

    @classmethod
    def from_content(
        cls,
        *,
        sid: str,
        task_event_id: Optional[str],
        allocation_id: str,
        content: SchedulerAssignmentContent,
    ) -> "SchedulerAssignmentEvent":
        tags: List[List[str]] = [
            ["allocation_id", allocation_id],
            ["model_name", content.model.model_name],
        ]
        if content.model.model_version:
            tags.append(["model_version", content.model.model_version])
        if task_event_id:
            tags.append(["e", task_event_id])

        # For quick indexing, also tag each worker pubkey
        for a in content.assignments:
            tags.append(["p", a.worker_pubkey])
            if a.account:
                tags.append(["account", a.account])

        content_json = json.dumps(asdict(content), ensure_ascii=False, separators=(",", ":"))
        return cls(kind=cls.KIND, sid=sid, op=cls.OP, content=content_json, tags=tags)


class WorkerResultEvent(SubspaceOpEvent):
    """30902 — Worker Result / Response (op: dinf_task_result)."""

    KIND = DINF_TASK_RESULT_KIND
    OP = "dinf_task_result"

    @classmethod
    def from_content(
        cls,
        *,
        sid: str,
        task_event_id: Optional[str],
        assignment_event_id: Optional[str],
        scheduler_pubkey: Optional[str],
        account: Optional[str],
        status: str,
        layer_range: LayerRange,
        content: WorkerResultContent,
    ) -> "WorkerResultEvent":
        tags: List[List[str]] = [
            ["status", status],
            ["start_layer", str(layer_range.start_layer)],
            ["end_layer", str(layer_range.end_layer)],
        ]
        if task_event_id:
            tags.append(["e", task_event_id])
        if assignment_event_id:
            tags.append(["re", assignment_event_id])
        if scheduler_pubkey:
            tags.append(["p", scheduler_pubkey])
        if account:
            tags.append(["account", account])

        metrics = content.metrics
        if metrics:
            tags.extend(
                [
                    ["tokens_in", str(metrics.tokens_in)],
                    ["tokens_out", str(metrics.tokens_out)],
                ]
            )
            if metrics.wall_time_ms is not None:
                tags.append(["latency_ms", str(int(metrics.wall_time_ms))])

        content_json = json.dumps(asdict(content), ensure_ascii=False, separators=(",", ":"))
        return cls(kind=cls.KIND, sid=sid, op=cls.OP, content=content_json, tags=tags)


class WorkloadProofEvent(SubspaceOpEvent):
    """30903 — Workload Proof (op: dinf_workload_proof)."""

    KIND = DINF_WORKLOAD_PROOF_KIND
    OP = "dinf_workload_proof"

    @classmethod
    def from_content(
        cls,
        *,
        sid: str,
        task_event_id: Optional[str],
        result_event_id: Optional[str],
        allocation_id: Optional[str],
        account: Optional[str],
        content: WorkloadProofContent,
        score: Optional[float] = None,
    ) -> "WorkloadProofEvent":
        tags: List[List[str]] = [
            ["p", content.worker_pubkey],
            ["work_units", str(content.metrics.work_units)],
        ]
        if task_event_id:
            tags.append(["e", task_event_id])
        if result_event_id:
            tags.append(["re", result_event_id])
        if allocation_id:
            tags.append(["allocation_id", allocation_id])
        if account:
            tags.append(["account", account])
        if score is not None:
            tags.append(["score", str(score)])

        content_json = json.dumps(asdict(content), ensure_ascii=False, separators=(",", ":"))
        return cls(kind=cls.KIND, sid=sid, op=cls.OP, content=content_json, tags=tags)


class TaskPublishEvent(SubspaceOpEvent):
    """30900 — Decentralized Task Publish (op: dinf_task_publish)."""

    KIND = DINF_TASK_PUBLISH_KIND
    OP = "dinf_task_publish"

    @classmethod
    def from_plaintext(
        cls,
        *,
        sid: str,
        description: str,
        model_name: str,
        num_layers: int,
        power_cost: Optional[float] = None,
        reward: Optional[str] = None,
        deadline: Optional[int] = None,
        category: str = "inference",
        difficulty: str = "medium",
    ) -> "TaskPublishEvent":
        """
        Helper to build a minimal, non-encrypted dinf_task_publish event for HTTP chat requests.

        NOTE: CIP-09 recommends encrypted content; this helper uses plaintext JSON content
        to keep implementation simple and focused on event wiring.
        """
        tags: List[List[str]] = [
            ["power_cost", str(power_cost)] if power_cost is not None else None,
            ["reward", reward] if reward is not None else None,
            ["category", category],
            ["difficulty", difficulty],
        ]
        # Filter out None entries
        tags = [t for t in tags if t is not None]  # type: ignore[list-item]

        content_obj: Dict[str, Any] = {
            "description": description,
            "model_spec": {"model_name": model_name, "num_layers": num_layers},
        }
        content_json = json.dumps(content_obj, ensure_ascii=False, separators=(",", ":"))
        return cls(kind=cls.KIND, sid=sid, op=cls.OP, content=content_json, tags=tags)


class SettlementEvent(SubspaceOpEvent):
    KIND = DINF_SETTLEMENT_KIND
    OP = "dinf_settlement"


class EvaluationEvent(SubspaceOpEvent):
    KIND = DINF_EVALUATION_KIND
    OP = "dinf_evaluation"



class InviteEvent(Event):
    """
    Kind 4 — Invite/Kick event (NIP-04 encrypted direct message).
    
    Content is encrypted using NIP-04 (AES-256-CBC) with ECDH shared secret.
    Use `prakasa_nostr.crypto.Nip04Crypto` to encrypt/decrypt the content.
    """

    KIND = INVITE_KIND

    @classmethod
    def from_encrypted_content(
        cls,
        *,
        encrypted_content: str,
        recipient_pubkey: Optional[str] = None,
        tags: Optional[List[List[str]]] = None,
    ) -> "InviteEvent":
        """
        Create an invite event with pre-encrypted content.
        
        Args:
            encrypted_content: NIP-04 encrypted JSON string (format: Base64(ciphertext)?iv=Base64(iv))
            recipient_pubkey: Optional recipient pubkey for tagging
            tags: Additional tags
        """
        event_tags: List[List[str]] = []
        if recipient_pubkey:
            event_tags.append(["p", recipient_pubkey])
        if tags:
            event_tags.extend(tags)

        return cls(content=encrypted_content, kind=cls.KIND, tags=event_tags if event_tags else None)


class ChatEvent(Event):
    """
    Kind 42 — Group chat message (AES-256-GCM encrypted).
    
    Content is encrypted using AES-GCM with group shared key.
    Use `prakasa_nostr.crypto.GroupV1Crypto` to encrypt/decrypt the content.
    """

    KIND = CHAT_KIND

    @classmethod
    def from_encrypted_content(
        cls,
        *,
        encrypted_content: str,
        group_id: Optional[str] = None,
        tags: Optional[List[List[str]]] = None,
    ) -> "ChatEvent":
        """
        Create a chat event with pre-encrypted content.
        
        Args:
            encrypted_content: AES-GCM encrypted JSON string (format: Base64(nonce + ciphertext + tag))
            group_id: Optional group identifier for tagging
            tags: Additional tags
        """
        event_tags: List[List[str]] = []
        if group_id:
            event_tags.append(["g", group_id])
        if tags:
            event_tags.extend(tags)

        return cls(content=encrypted_content, kind=cls.KIND, tags=event_tags if event_tags else None)


class ReasoningEvent(Event):
    """
    Kind 20000 — Reasoning process stream chunk (AES-256-GCM encrypted).
    
    Content is encrypted using AES-GCM with group shared key.
    Use `prakasa_nostr.crypto.GroupV1Crypto` to encrypt/decrypt the content.
    """

    KIND = REASONING_KIND

    @classmethod
    def from_encrypted_content(
        cls,
        *,
        encrypted_content: str,
        group_id: Optional[str] = None,
        task_event_id: Optional[str] = None,
        tags: Optional[List[List[str]]] = None,
    ) -> "ReasoningEvent":
        """
        Create a reasoning event with pre-encrypted content.
        
        Args:
            encrypted_content: AES-GCM encrypted JSON string (format: Base64(nonce + ciphertext + tag))
            group_id: Optional group identifier for tagging
            task_event_id: Optional reference to related task event
            tags: Additional tags
        """
        event_tags: List[List[str]] = []
        if group_id:
            event_tags.append(["g", group_id])
        if task_event_id:
            event_tags.append(["e", task_event_id])
        if tags:
            event_tags.extend(tags)

        return cls(content=encrypted_content, kind=cls.KIND, tags=event_tags if event_tags else None)


__all__ = [
    # DINF event classes
    "SubspaceOpEvent",
    "TaskPublishEvent",
    "SchedulerAssignmentEvent",
    "WorkerResultEvent",
    "WorkloadProofEvent",
    "SettlementEvent",
    "EvaluationEvent",
    # Standard Nostr event classes
    "InviteEvent",
    "ChatEvent",
    "ReasoningEvent",
    # Kind constants
    "INVITE_KIND",
    "CHAT_KIND",
    "REASONING_KIND",
    "DINF_TASK_PUBLISH_KIND",
    "DINF_TASK_ASSIGN_KIND",
    "DINF_TASK_RESULT_KIND",
    "DINF_WORKLOAD_PROOF_KIND",
    "DINF_SETTLEMENT_KIND",
    "DINF_EVALUATION_KIND",
    # Content helpers
    "ModelRef",
    "LayerRange",
    "Assignment",
    "RoutingInfo",
    "SchedulerAssignmentContent",
    "ResultMetrics",
    "WorkerResultContent",
    "WorkloadProofMetrics",
    "WorkloadProofContent",
]


