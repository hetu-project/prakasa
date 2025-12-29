"""
Nostr integration for Prakasa / Parallax.

This package defines:
- Typed CIP-09 event wrappers under `events.py`
- A lightweight asynchronous publisher in `publisher.py`
- The defination of the CIP-09, please refer to 
- https://github.com/hetu-project/causalitygraph/blob/main/Key/cip09/Decentralized_Inference_Protocol.md

Scheduler and worker components should depend only on the small helper
APIs in `publisher.py` to avoid pulling Nostr details into hot paths.
"""

from .publisher import (  # noqa: F401
    init_global_publisher,
    get_publisher,
)


