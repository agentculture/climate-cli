"""Entry point: ``python -m climate.weather.web``.

Builds the tracker configuration, the provider registry and the persistent
store, then serves the read-only API and dashboard on
``CLIMATE_WEB_BIND``:``CLIMATE_WEB_PORT`` (default ``127.0.0.1:8095`` —
loopback unless the operator explicitly opts into something else).
"""

from __future__ import annotations

import os
from pathlib import Path

from climate.weather import config as weather_config
from climate.weather.providers import iter_providers
from climate.weather.web.api import create_server

#: Loopback by default; only an explicit environment setting changes it.
DEFAULT_BIND = "127.0.0.1"
DEFAULT_PORT = 8095

BIND_ENV_VAR = "CLIMATE_WEB_BIND"
PORT_ENV_VAR = "CLIMATE_WEB_PORT"

STATIC_ROOT = Path(__file__).resolve().parent / "static"


def _resolve_bind_port() -> tuple[str, int]:
    """Read the bind address and port, defaulting to loopback:8095."""
    bind = os.environ.get(BIND_ENV_VAR, DEFAULT_BIND)
    port = int(os.environ.get(PORT_ENV_VAR, str(DEFAULT_PORT)))
    return bind, port


def _build_store():
    """Build the persistent store the web service reads from.

    SEAM / CONTRACT GAP: the Mongo-backed store (``climate/weather/mongo.py``)
    was being written by a parallel task, so its factory API was not
    available while this module was written. This function is the single
    place that call binds to it, so the assumption is easy to find and fix
    in one spot: it lazy-imports ``climate.weather.mongo`` (keeping
    ``pymongo`` out of every code path that doesn't need it) and calls a
    no-argument module-level factory named ``build_store()``, assumed to
    read its own connection settings from environment variables the way the
    rest of this service does and to return an object that satisfies
    :class:`climate.weather.store.WeatherStore`. If the real module exposes
    a different name or signature, this is the only function that needs to
    change.
    """
    from climate.weather import mongo  # noqa: PLC0415 - intentionally lazy

    return mongo.build_store()


def main() -> None:
    bind, port = _resolve_bind_port()
    config = weather_config.load_config()
    providers = iter_providers()
    store = _build_store()
    server = create_server(
        bind=bind,
        port=port,
        store=store,
        config=config,
        providers=providers,
        static_root=STATIC_ROOT,
    )
    try:
        server.serve_forever()
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
