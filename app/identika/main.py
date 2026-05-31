from __future__ import annotations

import uvicorn

from identika.config import settings


def main() -> None:
    uvicorn.run(
        "identika.app:create_app",
        factory=True,
        host=settings.identika_host,
        port=settings.identika_port,
        app_dir="app",
    )


if __name__ == "__main__":
    main()
