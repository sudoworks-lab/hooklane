"""Content-free local observability for the Cloudflare spike."""

from __future__ import annotations

import json
from uuid import UUID


class JsonTrace:
    """Emit only content-free fields used to reconstruct state transitions."""

    def emit(
        self,
        transition: str,
        *,
        event_id: UUID,
        attempt: int,
        reason: str,
    ) -> None:
        print(
            json.dumps(
                {
                    "attempt": attempt,
                    "event_id": str(event_id),
                    "reason": reason,
                    "transition": transition,
                },
                separators=(",", ":"),
                sort_keys=True,
            )
        )
