"""python -m ritesmith  →  start the API server."""

import uvicorn

from ritesmith.config import get_settings


def main() -> None:
    settings = get_settings()
    uvicorn.run(
        "ritesmith.api.app:app",
        host=settings.http_host,
        port=settings.http_port,
        reload=False,
        log_level="info",
    )


if __name__ == "__main__":
    main()
