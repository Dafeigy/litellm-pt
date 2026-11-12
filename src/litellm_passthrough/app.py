"""FastAPI relay with an explicit credential boundary."""

from __future__ import annotations

from contextlib import asynccontextmanager
import logging

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse
import httpx
from starlette.background import BackgroundTask

from .config import Config, Route


logger = logging.getLogger("litellm_passthrough")
_HOP_HEADERS = {
    b"connection", b"keep-alive", b"proxy-authenticate", b"proxy-authorization",
    b"te", b"trailer", b"transfer-encoding", b"upgrade",
}
_REQUEST_REMOVE = _HOP_HEADERS | {
    b"host", b"content-length", b"authorization", b"pt-api-key", b"x-litellm-api-key",
}
_RESPONSE_REMOVE = _HOP_HEADERS | {b"content-length"}


def _filtered_headers(headers: list[tuple[bytes, bytes]], remove: set[bytes]) -> list[tuple[bytes, bytes]]:
    connection_tokens = set()
    for name, value in headers:
        if name.lower() == b"connection":
            connection_tokens.update(token.strip().lower() for token in value.split(b","))
    blocked = remove | connection_tokens
    return [(name, value) for name, value in headers if name.lower() not in blocked]


def _match_route(routes: tuple[Route, ...], path: str) -> Route | None:
    matches = (route for route in routes if route.prefix == "/" or path == route.prefix or path.startswith(route.prefix + "/"))
    return max(matches, key=lambda route: len(route.prefix), default=None)


def _upstream_url(route: Route, raw_path: bytes, query: bytes) -> httpx.URL:
    path = raw_path.decode("ascii")
    suffix = path[len(route.prefix):] if route.strip_prefix and route.prefix != "/" else path
    if not suffix:
        suffix = "/"
    url = httpx.URL(route.upstream + suffix)
    if query:
        url = url.copy_with(raw_path=url.raw_path + b"?" + query)
    return url


def create_app(config: Config, *, transport: httpx.AsyncBaseTransport | None = None) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI):
        async with httpx.AsyncClient(transport=transport, follow_redirects=False, trust_env=False) as client:
            app.state.client = client
            yield

    app = FastAPI(title="LiteLLM Pass-Through Relay", docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)

    @app.get("/healthz")
    async def healthz():
        return {"status": "ok"}

    @app.api_route("/{path:path}", methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "HEAD"])
    async def relay(request: Request, path: str):
        raw_path = request.scope.get("raw_path", request.url.path.encode("ascii"))
        route = _match_route(config.routes, request.url.path)
        # Require a literal prefix in the raw path too; encoded path tricks must not change routing.
        if route is None or not _match_route((route,), raw_path.decode("ascii", errors="replace")):
            return JSONResponse({"detail": "route not found"}, status_code=404)

        credentials = [value for name, value in request.scope["headers"] if name.lower() == b"pt-api-key"]
        if len(credentials) != 1 or not credentials[0].strip():
            return JSONResponse({"detail": "exactly one nonempty pt-api-key header is required"}, status_code=400)

        remove = _REQUEST_REMOVE | {h.encode("ascii") for h in config.gateway_auth_headers}
        headers = _filtered_headers(request.scope["headers"], remove)
        headers.append((b"authorization", credentials[0]))
        try:
            url = _upstream_url(route, raw_path, request.scope.get("query_string", b""))
            outbound = app.state.client.build_request(
                request.method, url, headers=headers, content=request.stream(),
                timeout=httpx.Timeout(route.timeout_seconds),
            )
            upstream = await app.state.client.send(outbound, stream=True)
        except httpx.TimeoutException as exc:
            logger.warning("upstream timeout route=%s error=%s", route.prefix, type(exc).__name__)
            return JSONResponse({"detail": "upstream timeout"}, status_code=504)
        except (httpx.RequestError, ValueError, UnicodeError) as exc:
            logger.warning("upstream failure route=%s error=%s", route.prefix, type(exc).__name__)
            return JSONResponse({"detail": "upstream unavailable"}, status_code=502)

        logger.info("relay route=%s method=%s status=%d", route.prefix, request.method, upstream.status_code)
        response_headers = _filtered_headers(upstream.headers.raw, _RESPONSE_REMOVE)
        response = StreamingResponse(upstream.aiter_raw(), status_code=upstream.status_code, background=BackgroundTask(upstream.aclose))
        response.raw_headers = response_headers
        return response

    return app
