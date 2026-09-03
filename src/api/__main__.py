"""``python -m api`` -- serve the read-only API with uvicorn."""

from __future__ import annotations

import uvicorn

from api.app import create_app
from api.config import ApiSettings


def main() -> int:
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
