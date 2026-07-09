"""HTTP API package — FastAPI routers for the Clinical Co-Pilot service.

Each module here owns one router; ``copilot.main`` mounts them. The M1
acceptance surface lives in :mod:`copilot.api.summary` (the streamed, cited
patient-summary endpoint).
"""

from __future__ import annotations
