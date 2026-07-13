"""Guideline corpus loading and chunking (PRP-07).

The corpus is the **evidence half** of a grounded answer — public clinical
guidance, kept strictly separate from patient-record facts. Each markdown file
under ``corpus/`` carries YAML front-matter
(``source_title``, ``source_org``, ``citation``, ``url``, ``topic``) and is
split into retrievable ``GuidelineChunk`` units, one per ``##`` section.

Design notes:
- ``GuidelineChunk`` is ``frozen`` + ``extra="forbid"`` like the value objects
  in :mod:`copilot.schemas.core`: an immutable, hashable citable unit.
- ``chunk_id`` is stable and deterministic: ``<topic>::<source-slug>::<NN>``
  where ``source-slug`` is the corpus filename stem and ``NN`` is the section's
  ordinal within that file. Reordering or editing prose never renumbers an
  existing section, so ids survive across runs.
- No PHI, ever. This corpus is public guidance only.
"""

from __future__ import annotations

from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict, Field

__all__ = ["GuidelineChunk", "load_corpus", "CORPUS_DIR"]

CORPUS_DIR = Path(__file__).parent / "corpus"

_FRONT_MATTER_KEYS = ("source_title", "source_org", "citation", "url", "topic")


class GuidelineChunk(BaseModel):
    """One citable, retrievable unit of clinical-guideline evidence (FR-8).

    Every chunk carries the full attribution needed to cite it, so a grounded
    answer can name its source by *shape* rather than by prompting.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    chunk_id: str = Field(min_length=1, description="Stable, deterministic, unique id for this chunk.")
    source_title: str = Field(min_length=1, description="Title of the source document.")
    source_org: str = Field(min_length=1, description="Publishing organization.")
    citation: str = Field(min_length=1, description="Full human-readable citation.")
    section: str = Field(min_length=1, description="Section heading this chunk was drawn from.")
    text: str = Field(min_length=1, description="Paraphrased guideline text (public, no PHI).")


def _slugify_topic(topic: str) -> str:
    """Normalize a front-matter topic into the leading chunk_id segment."""
    return topic.strip().lower().replace(" ", "-")


def _parse_front_matter(raw: str, path: Path) -> tuple[dict[str, str], str]:
    """Split a corpus file into its front-matter mapping and markdown body."""
    if not raw.startswith("---"):
        raise ValueError(f"{path.name}: missing YAML front-matter delimiter")
    # Front-matter is delimited by the first two lines that are exactly '---'.
    parts = raw.split("---", 2)
    if len(parts) < 3:
        raise ValueError(f"{path.name}: unterminated YAML front-matter")
    meta = yaml.safe_load(parts[1]) or {}
    if not isinstance(meta, dict):
        raise ValueError(f"{path.name}: front-matter is not a mapping")
    missing = [k for k in _FRONT_MATTER_KEYS if not str(meta.get(k, "")).strip()]
    if missing:
        raise ValueError(f"{path.name}: front-matter missing {missing}")
    return {k: str(meta[k]).strip() for k in _FRONT_MATTER_KEYS}, parts[2]


def _split_sections(body: str) -> list[tuple[str, str]]:
    """Split a markdown body into (heading, text) pairs, one per ``##`` section."""
    sections: list[tuple[str, str]] = []
    heading: str | None = None
    buffer: list[str] = []
    for line in body.splitlines():
        if line.startswith("## "):
            if heading is not None:
                sections.append((heading, "\n".join(buffer).strip()))
            heading = line[3:].strip()
            buffer = []
        elif heading is not None:
            buffer.append(line)
    if heading is not None:
        sections.append((heading, "\n".join(buffer).strip()))
    return sections


def load_corpus() -> list[GuidelineChunk]:
    """Load and chunk the guideline corpus into citable units.

    Deterministic: files are read in sorted order and sections in document
    order, so the returned list — and every ``chunk_id`` in it — is identical
    across runs. Raises if any file lacks complete metadata or if two chunks
    would collide on ``chunk_id``.
    """
    chunks: list[GuidelineChunk] = []
    seen: set[str] = set()
    for path in sorted(CORPUS_DIR.glob("*.md")):
        raw = path.read_text(encoding="utf-8")
        meta, body = _parse_front_matter(raw, path)
        topic = _slugify_topic(meta["topic"])
        for ordinal, (heading, text) in enumerate(_split_sections(body), start=1):
            if not text:
                raise ValueError(f"{path.name}: empty section '{heading}'")
            chunk_id = f"{topic}::{path.stem}::{ordinal:02d}"
            if chunk_id in seen:
                raise ValueError(f"duplicate chunk_id: {chunk_id}")
            seen.add(chunk_id)
            chunks.append(
                GuidelineChunk(
                    chunk_id=chunk_id,
                    source_title=meta["source_title"],
                    source_org=meta["source_org"],
                    citation=meta["citation"],
                    section=heading,
                    text=text,
                )
            )
    return chunks
