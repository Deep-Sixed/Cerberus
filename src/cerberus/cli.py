"""Command-line launcher for Cerberus."""

import argparse
import os
import sys

import uvicorn

from cerberus.app import create_app
from cerberus.fusion import fusion_aliases, generate_worker_config_yaml
from cerberus.registry import load_config_document


def main() -> None:
    parser = argparse.ArgumentParser(prog="cerberus")
    subparsers = parser.add_subparsers(dest="command", required=True)
    serve = subparsers.add_parser("serve")
    serve.add_argument("--config", help="Path to the YAML configuration file")

    gen = subparsers.add_parser(
        "fusion-config",
        help="Generate the fusion_backend worker config from a Cerberus fusion alias (secrets as ${ENV} refs).",
    )
    gen.add_argument("--config", help="Path to the Cerberus YAML configuration file")
    gen.add_argument("--alias", help="Fusion alias name (defaults to the sole fusion alias)")
    gen.add_argument("--out", help="Write to this path instead of stdout")

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
    elif args.command == "fusion-config":
        # generation does not need real secrets — env refs are emitted verbatim
        document = load_config_document(args.config, validate_credentials=False)
        aliases = fusion_aliases(document.config)
        alias = args.alias
        if alias is None:
            if len(aliases) != 1:
                parser.error(f"specify --alias; fusion aliases are: {aliases or 'none'}")
            alias = aliases[0]
        yaml_text = generate_worker_config_yaml(document.config, alias)
        if args.out:
            with open(args.out, "w", encoding="utf-8") as handle:
                handle.write(yaml_text)
        else:
            sys.stdout.write(yaml_text)
