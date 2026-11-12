import asyncio
from pathlib import Path

import httpx
import pytest

from litellm_passthrough import create_app
from litellm_passthrough.config import Config, Route, load_config


def run_request(config, upstream_handler, method, url, *, headers=(), content=None):
    async def run():
        app = create_app(config, transport=httpx.MockTransport(upstream_handler))
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://relay") as client:
                return await client.request(method, url, headers=headers, content=content)

    return asyncio.run(run())


def config(*routes, gateway_auth_headers=()):
    return Config("127.0.0.1", 3000, routes, gateway_auth_headers)


class Chunks(httpx.AsyncByteStream):
    def __init__(self, *parts):
        self.parts = parts

    async def __aiter__(self):
        for part in self.parts:
            yield part


def test_credential_mapping_path_query_and_upstream_error():
    seen = []

    def upstream(request):
        seen.append(request)
        return httpx.Response(429, stream=Chunks(b'{"error":"limited"}'), headers={"content-type": "application/json", "x-request-id": "abc"})

    result = run_request(
        config(Route("/deepseek", "https://api.deepseek.com", True)), upstream,
        "POST", "/deepseek/v1/chat/completions?tag=a&tag=b",
        headers=[("Authorization", "Bearer litellm-user"), ("pt-api-key", "Bearer upstream-user"),
                 ("x-litellm-api-key", "another-gateway-key"), ("content-type", "application/json")],
        content=b'{"stream":true}',
    )
    assert result.status_code == 429
    assert result.content == b'{"error":"limited"}'
    assert result.headers["x-request-id"] == "abc"
    assert len(seen) == 1
    request = seen[0]
    assert str(request.url) == "https://api.deepseek.com/v1/chat/completions?tag=a&tag=b"
    assert request.content == b'{"stream":true}'
    assert request.headers["authorization"] == "Bearer upstream-user"
    assert "pt-api-key" not in request.headers
    assert "x-litellm-api-key" not in request.headers
    assert request.headers["content-type"] == "application/json"


@pytest.mark.parametrize("headers", [[], [("pt-api-key", "")], [("pt-api-key", "a"), ("PT-API-KEY", "b")]])
def test_missing_empty_or_duplicate_credential_never_calls_upstream(headers):
    def upstream(_):
        raise AssertionError("must not call upstream")

    result = run_request(config(Route("/p", "https://example.com")), upstream, "GET", "/p", headers=headers)
    assert result.status_code == 400


def test_longest_boundary_match_and_custom_gateway_header():
    seen = []

    def upstream(request):
        seen.append(request)
        return httpx.Response(200, stream=Chunks(b"ok"))

    routes = (Route("/p", "https://wide.example"), Route("/p/v1", "https://specific.example", True))
    result = run_request(config(*routes, gateway_auth_headers=("x-company-gateway-key",)), upstream,
                         "GET", "/p/v1/models", headers=[("pt-api-key", "token"), ("x-company-gateway-key", "secret")])
    assert result.status_code == 200
    assert str(seen[0].url) == "https://specific.example/models"
    assert "x-company-gateway-key" not in seen[0].headers

    result = run_request(config(*routes), upstream, "GET", "/prefix", headers=[("pt-api-key", "token")])
    assert result.status_code == 404
    assert len(seen) == 1


def test_hop_by_hop_headers_and_duplicate_response_headers():
    seen = []

    def upstream(request):
        seen.append(request)
        return httpx.Response(200, stream=Chunks(b"data"), headers=[
            ("set-cookie", "a=1"), ("set-cookie", "b=2"), ("connection", "x-private"),
            ("x-private", "remove"), ("content-type", "text/plain"),
        ])

    result = run_request(config(Route("/p", "https://example.com")), upstream, "GET", "/p",
                         headers=[("pt-api-key", "token"), ("connection", "x-internal"), ("x-internal", "remove")])
    assert result.content == b"data"
    assert result.headers.get_list("set-cookie") == ["a=1", "b=2"]
    assert "x-private" not in result.headers
    assert "connection" not in result.headers
    assert "x-internal" not in seen[0].headers


@pytest.mark.parametrize("failure,status", [(httpx.ConnectError("bad"), 502), (httpx.ReadTimeout("slow"), 504)])
def test_transport_errors_are_safe(failure, status):
    def upstream(_):
        raise failure

    result = run_request(config(Route("/p", "https://example.com")), upstream, "GET", "/p", headers=[("pt-api-key", "secret")])
    assert result.status_code == status
    assert b"secret" not in result.content


def test_sse_bytes_are_forwarded_without_rewriting():
    def upstream(_):
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, stream=Chunks(b"data: first\n\n", b"data: second\n\n"))

    result = run_request(config(Route("/p", "https://example.com")), upstream, "POST", "/p",
                         headers=[("pt-api-key", "token")], content=b"{}")
    assert result.status_code == 200
    assert result.headers["content-type"] == "text/event-stream"
    assert result.content == b"data: first\n\ndata: second\n\n"


def test_load_config_and_reject_bad_values(tmp_path: Path):
    file = tmp_path / "relay.yaml"
    file.write_text("listen: '127.0.0.1:3000'\nroutes:\n  - prefix: /p\n    upstream: https://api.example.com\n", encoding="utf-8")
    loaded = load_config(file)
    assert loaded.routes[0].prefix == "/p"

    file.write_text("routes:\n  - prefix: /p\n    upstream: https://user:password@api.example.com\n", encoding="utf-8")
    with pytest.raises(ValueError, match="upstream"):
        load_config(file)
