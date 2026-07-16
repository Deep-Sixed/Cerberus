"""Command-line launcher for portable Cerberus deployments."""

import argparse
import os

import uvicorn

from .app import create_app
from .config import load_config


def main() -> None:
    parser = argparse.ArgumentParser(prog="cerberus")
    subparsers = parser.add_subparsers(dest="command", required=True)
    serve = subparsers.add_parser("serve")
    serve.add_argument("--config", help="Path to the YAML configuration file")
    args = parser.parse_args()

    if args.command == "serve":
        if args.config:
            os.environ["CERBERUS_CONFIG"] = args.config
        config = load_config()
        uvicorn.run(create_app(config), host=config.server.host, port=config.server.port)
