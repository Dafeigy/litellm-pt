"""Strict, local-only relay configuration."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit
import re

import yaml


_PREFIX = re.compile(r"^/(?:[A-Za-z0-9._~-]+(?:/[A-Za-z0-9._~-]+)*)?$")
_HEADER = re.compile(r"^[!#$%&'*+.^_`|~0-9A-Za-z-]+$")


@dataclass(frozen=True)
class Route:
    prefix: str
    upstream: str
    strip_prefix: bool = False
    timeout_seconds: float = 600.0


@dataclass(frozen=True)
class Config:
    host: str
    port: int
    routes: tuple[Route, ...]
    gateway_auth_headers: tuple[str, ...] = ()


def _mapping(value: object, where: str) -> dict:
    if not isinstance(value, dict):
        raise ValueError(f"{where} must be a mapping")
    return value


def _keys(value: dict, allowed: set[str], where: str) -> None:
    unknown = set(value) - allowed
    if unknown:
        raise ValueError(f"{where} has unknown keys: {', '.join(sorted(map(str, unknown)))}")


def load_config(path: str | Path) -> Config:
    with Path(path).open(encoding="utf-8") as file:
        data = _mapping(yaml.safe_load(file), "config")
    _keys(data, {"listen", "routes", "gateway_auth_headers"}, "config")

    listen = data.get("listen", "127.0.0.1:3000")
    if not isinstance(listen, str):
        raise ValueError("listen must be a host:port string")
    address = urlsplit("http://" + listen)
    try:
        port = address.port
    except ValueError as exc:
        raise ValueError("listen has an invalid port") from exc
    if not address.hostname or port is None or not 1 <= port <= 65535 or address.path or address.query or address.fragment or address.username or address.password:
        raise ValueError("listen must be a host:port string")

    raw_headers = data.get("gateway_auth_headers", [])
    if not isinstance(raw_headers, list) or any(not isinstance(h, str) or not _HEADER.fullmatch(h) for h in raw_headers):
        raise ValueError("gateway_auth_headers must be a list of HTTP header names")
    headers = tuple(h.lower() for h in raw_headers)
    if any(h in {"pt-api-key", "authorization"} for h in headers):
        raise ValueError("gateway_auth_headers cannot include pt-api-key or Authorization")

    raw_routes = data.get("routes")
    if not isinstance(raw_routes, list) or not raw_routes:
        raise ValueError("routes must be a nonempty list")
    routes = []
    seen = set()
    for index, raw in enumerate(raw_routes):
        where = f"routes[{index}]"
        route = _mapping(raw, where)
        _keys(route, {"prefix", "upstream", "strip_prefix", "timeout_seconds"}, where)
        prefix = route.get("prefix")
        if not isinstance(prefix, str) or not _PREFIX.fullmatch(prefix) or any(part in {".", ".."} for part in prefix.split("/")):
            raise ValueError(f"{where}.prefix must be a canonical absolute path without trailing slash")
        if prefix in seen:
            raise ValueError(f"duplicate route prefix: {prefix}")
        seen.add(prefix)
        upstream = route.get("upstream")
        if not isinstance(upstream, str):
            raise ValueError(f"{where}.upstream must be an HTTP(S) URL")
        parsed = urlsplit(upstream)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ValueError(f"{where}.upstream must be an HTTP(S) URL without credentials, query, or fragment")
        try:
            parsed.port
        except ValueError as exc:
            raise ValueError(f"{where}.upstream has an invalid port") from exc
        strip = route.get("strip_prefix", False)
        if not isinstance(strip, bool):
            raise ValueError(f"{where}.strip_prefix must be boolean")
        timeout = route.get("timeout_seconds", 600)
        if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not 0 < timeout < float("inf"):
            raise ValueError(f"{where}.timeout_seconds must be a positive finite number")
        routes.append(Route(prefix, upstream.rstrip("/"), strip, float(timeout)))
    return Config(address.hostname, port, tuple(sorted(routes, key=lambda r: len(r.prefix), reverse=True)), headers)
