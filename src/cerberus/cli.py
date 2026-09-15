"""Command-line launcher for Cerberus."""

import argparse
import os

import uvicorn

from cerberus.app import create_app
from cerberus.registry import load_config_document


def main() -> None:
    parser = argparse.ArgumentParser(prog="cerberus")
    subparsers = parser.add_subparsers(dest="command", required=True)
    serve = subparsers.add_parser("serve")
    serve.add_argument("--config", help="Path to the YAML configuration file")

    args = parser.parse_args()

    if args.command == "serve":
        if args.config:
            os.environ["CERBERUS_CONFIG"] = args.config
        document = load_config_document()
        server = document.config.server
        uvicorn.run(
            create_app(document),
            host=server.host,
            port=server.port,
            # never let X-Forwarded-For/Forwarded rewrite request.client:
            # loopback admin authorization depends on the true transport peer
            proxy_headers=False,
        )
