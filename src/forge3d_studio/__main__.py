"""`forge3d-studio` / `python -m forge3d_studio` — run the API with its worker."""

from __future__ import annotations

import argparse

from .config import get_settings


def main() -> None:
    parser = argparse.ArgumentParser(prog="forge3d-studio")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--reload", action="store_true")
    parser.add_argument("--workers", type=int, default=None,
                        help="worker threads inside this process (default: STUDIO_WORKERS)")
    args = parser.parse_args()

    if args.workers:
        import os

        os.environ["STUDIO_WORKERS"] = str(args.workers)

    settings = get_settings()
    settings.ensure_dirs()

    import uvicorn

    uvicorn.run("forge3d_studio.api:app", host=args.host, port=args.port,
                reload=args.reload, log_level="info")


if __name__ == "__main__":
    main()
