"""zlhNode: Web Bridge (Input/Output nodes + HTTP API + static web client)."""

from __future__ import annotations

import logging

from .nodes import NODE_CLASS_MAPPINGS, NODE_DISPLAY_NAME_MAPPINGS

WEB_DIRECTORY = "web"

__all__ = ["NODE_CLASS_MAPPINGS", "NODE_DISPLAY_NAME_MAPPINGS", "WEB_DIRECTORY"]


def _register_routes() -> None:
    from .bridge_routes import register as register_bridge
    from .dataset_routes import register as register_dataset

    register_bridge()
    register_dataset()


try:
    _register_routes()
except Exception:
    logging.exception("[zlhNode] failed to register HTTP routes")
