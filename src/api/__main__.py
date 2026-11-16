"""``python -m api`` -- serve the API with uvicorn, or ``python -m api orchestrate`` --
run the pipeline in the foreground (T-011)."""

from __future__ import annotations

import sys
from collections.abc import Sequence

import uvicorn

from api.app import create_app
from api.config import ApiSettings
from api.orchestrator import cli_main


def main(argv: Sequence[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if args and args[0] == "orchestrate":
        return cli_main(args[1:])
    settings = ApiSettings.load()
    uvicorn.run(
        create_app,
        factory=True,
        host=settings.host,
        port=settings.port,
        log_level="info",
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
