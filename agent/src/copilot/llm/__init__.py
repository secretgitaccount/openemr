"""The single LLM step of the walking skeleton (FR-8, PRP M1-5).

Turns the minimum-necessary retrieved records into a
:class:`~copilot.schemas.output.GroundedSummary` whose every claim binds to
source records that exist in the input. Grounding is enforced by output
**shape** (structured output), not by prompting alone.
"""

from __future__ import annotations

from copilot.llm.client import LLMClient, LLMError

__all__ = ["LLMClient", "LLMError"]
