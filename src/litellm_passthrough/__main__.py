"""Command-line entry point."""

import argparse
import logging

import uvicorn

from .app import create_app
from .config import load_config


def main() -> None:
    parser = argparse.ArgumentParser(description="LiteLLM pass-through relay")
    parser.add_argument("--config", default="relay.yaml", help="YAML config path (default: relay.yaml)")
    args = parser.parse_args()
    config = load_config(args.config)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s")
    uvicorn.run(create_app(config), host=config.host, port=config.port, access_log=False)


if __name__ == "__main__":
    main()
