"""Dump / verify the committed OpenAPI spec for the Week-2 HTTP surface.

FastAPI generates the spec from the live routes, so the committed
``agent/openapi.json`` is a *snapshot* of that generation. Two modes:

* ``python -m copilot.scripts.dump_openapi``          → (re)write the snapshot.
* ``python -m copilot.scripts.dump_openapi --check``   → exit non-zero if the
  committed snapshot has drifted from what the app now generates (the CI /
  contract-test guard). This keeps the published spec in sync with the
  implementation without hand-maintaining it.

The spec is serialised with ``sort_keys=True`` + a trailing newline so the file
is diff-stable across regenerations.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from copilot.main import app

#: Committed spec location: agent/openapi.json (repo-relative, recoverable).
SPEC_PATH = Path(__file__).resolve().parents[3] / "openapi.json"


def render_spec() -> str:
    """The app's current OpenAPI spec as diff-stable JSON text."""

    return json.dumps(app.openapi(), sort_keys=True, indent=2) + "\n"


def write_spec(path: Path = SPEC_PATH) -> Path:
    """Write the current spec to ``path`` and return it."""

    path.write_text(render_spec(), encoding="utf-8")
    return path


def check_spec(path: Path = SPEC_PATH) -> bool:
    """True iff the committed spec matches the app's current generation."""

    if not path.exists():
        return False
    return path.read_text(encoding="utf-8") == render_spec()


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if "--check" in argv:
        if check_spec():
            print(f"openapi: committed spec is in sync ({SPEC_PATH.name}).")
            return 0
        print(
            f"openapi: DRIFT — {SPEC_PATH.name} is stale. "
            "Run `python -m copilot.scripts.dump_openapi` and commit.",
            file=sys.stderr,
        )
        return 1
    path = write_spec()
    print(f"openapi: wrote {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
