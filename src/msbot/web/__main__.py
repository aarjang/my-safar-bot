"""python -m msbot.web [--host 0.0.0.0] [--port 8765] [--config path.yaml]"""
from __future__ import annotations

import argparse
import logging


def main() -> None:
    p = argparse.ArgumentParser("msbot.web")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8765)
    p.add_argument("--reload", action="store_true")
    args = p.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s | %(message)s",
        datefmt="%H:%M:%S",
    )

    import uvicorn

    uvicorn.run("msbot.web.app:app", host=args.host, port=args.port, reload=args.reload, log_level="info")


if __name__ == "__main__":
    main()
