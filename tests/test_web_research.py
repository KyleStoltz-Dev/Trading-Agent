import ipaddress
import json
from unittest.mock import Mock

import httpx
import pytest

import app.services.web_fetch as web_fetch_module
from app.services.web_fetch import (
    WebFetchError,
    _pinned_url,
    allowed_domain_paths,
    allowed_domains,
    fetch_web_page,
    validate_public_url,
)
from app.services.web_search import WebSearchError, search_brave

PUBLIC_ADDRESS = (ipaddress.ip_address("93.184.216.34"),)
DOCUMENTED_PATHS = {"example.com": ("/docs/",)}


def public_resolver(hostname: str, port: int):
    del hostname, port
    return PUBLIC_ADDRESS


def test_allowed_domains_accept_exact_hosts_and_subdomains() -> None:
    domains = allowed_domains("example.com, docs.example.org")

    assert domains == frozenset({"example.com", "docs.example.org"})
    assert (
        validate_public_url(
            "https://api.example.com/docs/reference",
            resolver=public_resolver,
            domains=domains,
            path_policies={"example.com": ("/docs/",)},
        )
        == "https://api.example.com/docs/reference"
    )


@pytest.mark.parametrize(
    "value",
    [
        "localhost",
        "127.0.0.1",
        "bad domain.example",
        "https://example.com",
        ".example.com",
    ],
)
def test_allowed_domains_rejects_ambiguous_or_unsafe_entries(value: str) -> None:
    with pytest.raises(WebFetchError, match="domain|IP address"):
        allowed_domains(value)


def test_fetch_rejects_nonallowlisted_and_private_hosts_before_request() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(200, text="should not run")

    client = httpx.Client(transport=httpx.MockTransport(handler))
    with pytest.raises(WebFetchError, match="not in WEB_FETCH_ALLOWED_DOMAINS"):
        fetch_web_page(
            "https://outside.example/page",
            domains=frozenset({"example.com"}),
            resolver=public_resolver,
            client=client,
            authorize_url=lambda url: None,
        )
    with pytest.raises(WebFetchError, match="private or non-public"):
        fetch_web_page(
            "https://127.0.0.1/secrets",
            domains=frozenset({"127.0.0.1"}),
            resolver=lambda hostname, port: (ipaddress.ip_address("127.0.0.1"),),
            client=client,
            authorize_url=lambda url: None,
        )

    assert calls == 0
    client.close()


def test_fetch_rejects_model_selected_query_parameters() -> None:
    with pytest.raises(WebFetchError, match="query parameters"):
        validate_public_url(
            "https://example.com/reference?journal=private",
            resolver=public_resolver,
            domains=frozenset({"example.com"}),
            path_policies=DOCUMENTED_PATHS,
        )


def test_allowed_path_policy_rejects_private_or_unconfigured_paths() -> None:
    policies = allowed_domain_paths("example.com=/docs/,/reference/")

    assert policies == {"example.com": ("/docs/", "/reference/")}
    for url in (
        "https://example.com/journal/my-private-trade",
        "https://example.com/docs/journal/private-entry",
        "https://example.com/docs/%73ecret",
        "https://example.com/docs/%2573ecret",
        "https://example.com/docs/privateJournalEntry",
        "https://example.com/docs/abcdefghijklmnopqrstuvwxyz012345",
    ):
        with pytest.raises(WebFetchError, match="path|private|secret"):
            validate_public_url(
                url,
                resolver=public_resolver,
                domains=frozenset({"example.com"}),
                path_policies=policies,
            )


def test_path_exfiltration_is_rejected_before_confirmation_or_network() -> None:
    network = 0
    confirmations = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal network
        network += 1
        return httpx.Response(200, text="must not run")

    def authorize(url: str) -> None:
        nonlocal confirmations
        confirmations += 1

    client = httpx.Client(transport=httpx.MockTransport(handler))
    with pytest.raises(WebFetchError, match="private or secret"):
        fetch_web_page(
            "https://example.com/docs/journal/private-xauusd-entry",
            domains=frozenset({"example.com"}),
            path_policies=DOCUMENTED_PATHS,
            resolver=public_resolver,
            client=client,
            authorize_url=authorize,
        )

    assert confirmations == 0
    assert network == 0
    client.close()


def test_exact_url_authorization_occurs_before_dns_or_http() -> None:
    resolver = Mock()
    network = Mock()
    client = httpx.Client(
        transport=httpx.MockTransport(
            lambda request: network(request) or httpx.Response(200)
        )
    )

    def decline(url: str) -> None:
        raise PermissionError(f"declined {url}")

    with pytest.raises(PermissionError, match="declined"):
        fetch_web_page(
            "https://example.com/docs/reference",
            domains=frozenset({"example.com"}),
            path_policies=DOCUMENTED_PATHS,
            resolver=resolver,
            client=client,
            authorize_url=decline,
        )

    resolver.assert_not_called()
    network.assert_not_called()
    client.close()


def test_fetch_revalidates_redirects_and_extracts_readable_html() -> None:
    authorized: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/docs/redirect":
            return httpx.Response(302, headers={"location": "/docs/article"})
        return httpx.Response(
            200,
            headers={"content-type": "text/html; charset=utf-8"},
            text=(
                "<html><head><title>Market Reference</title>"
                "<script>ignore this</script></head>"
                "<body><h1>Policy rate</h1><p>Current documented value.</p></body></html>"
            ),
        )

    client = httpx.Client(transport=httpx.MockTransport(handler))
    page = fetch_web_page(
        "https://docs.example.com/docs/redirect",
        domains=frozenset({"example.com"}),
        path_policies=DOCUMENTED_PATHS,
        resolver=public_resolver,
        client=client,
        authorize_url=authorized.append,
    )

    assert page.url == "https://docs.example.com/docs/article"
    assert page.title == "Market Reference"
    assert "Policy rate" in page.text
    assert "ignore this" not in page.text
    assert authorized == [
        "https://docs.example.com/docs/redirect",
        "https://docs.example.com/docs/article",
    ]
    client.close()


def test_host_is_canonicalized_before_confirmation_dns_host_and_sni() -> None:
    resolved: list[str] = []
    authorized: list[str] = []
    captured: list[httpx.Request] = []

    def resolver(hostname: str, port: int):
        del port
        resolved.append(hostname)
        return PUBLIC_ADDRESS

    client = httpx.Client(
        transport=httpx.MockTransport(
            lambda request: captured.append(request)
            or httpx.Response(
                200,
                headers={"content-type": "text/plain"},
                text="ok",
            )
        )
    )
    fetch_web_page(
        "HTTPS://DOCS.EXAMPLE.COM./docs/article",
        domains=frozenset({"example.com"}),
        path_policies=DOCUMENTED_PATHS,
        resolver=resolver,
        client=client,
        authorize_url=authorized.append,
    )

    assert authorized == ["https://docs.example.com/docs/article"]
    assert resolved == ["docs.example.com"]
    assert captured[0].headers["host"] == "docs.example.com"
    assert captured[0].extensions["sni_hostname"] == "docs.example.com"
    client.close()


def test_fetch_pins_validated_public_ip_to_prevent_dns_rebinding() -> None:
    resolver_calls = 0
    requests: list[httpx.Request] = []

    def rebinding_resolver(hostname: str, port: int):
        nonlocal resolver_calls
        del hostname, port
        resolver_calls += 1
        if resolver_calls == 1:
            return PUBLIC_ADDRESS
        return (ipaddress.ip_address("127.0.0.1"),)

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200,
            headers={"content-type": "text/plain"},
            text="Pinned public destination.",
        )

    client = httpx.Client(transport=httpx.MockTransport(handler))
    fetch_web_page(
        "https://docs.example.com/docs/article",
        domains=frozenset({"example.com"}),
        path_policies=DOCUMENTED_PATHS,
        resolver=rebinding_resolver,
        client=client,
        authorize_url=lambda url: None,
    )

    assert resolver_calls == 1
    assert requests[0].url.host == str(PUBLIC_ADDRESS[0])
    assert requests[0].headers["host"] == "docs.example.com"
    client.close()


def test_pinned_urls_preserve_ipv4_ipv6_host_and_tls_sni() -> None:
    ipv4_url, ipv4_host = _pinned_url(
        "https://docs.example.com:443/docs/article",
        ipaddress.ip_address("93.184.216.34"),
    )
    ipv6_url, ipv6_host = _pinned_url(
        "https://docs.example.com/docs/article",
        ipaddress.ip_address("2606:2800:220:1:248:1893:25c8:1946"),
    )

    assert ipv4_url == "https://93.184.216.34:443/docs/article"
    assert ipv4_host == "docs.example.com:443"
    assert ipv6_url == (
        "https://[2606:2800:220:1:248:1893:25c8:1946]/docs/article"
    )
    assert ipv6_host == "docs.example.com"

    captured: list[httpx.Request] = []
    client = httpx.Client(
        transport=httpx.MockTransport(
            lambda request: captured.append(request)
            or httpx.Response(
                200,
                headers={"content-type": "text/plain"},
                text="ok",
            )
        )
    )
    fetch_web_page(
        "https://docs.example.com/docs/article",
        domains=frozenset({"example.com"}),
        path_policies=DOCUMENTED_PATHS,
        resolver=public_resolver,
        client=client,
        authorize_url=lambda url: None,
    )
    assert captured[0].headers["host"] == "docs.example.com"
    assert captured[0].extensions["sni_hostname"] == "docs.example.com"
    client.close()


def test_fetch_disables_proxy_environment_and_compressed_responses(
    monkeypatch,
) -> None:
    created: dict = {}
    real_client = httpx.Client

    def client_factory(**kwargs):
        created.update(kwargs)
        return real_client(
            **kwargs,
            transport=httpx.MockTransport(
                lambda request: httpx.Response(
                    200,
                    headers={
                        "content-type": "text/plain",
                        "content-encoding": "gzip",
                    },
                    stream=httpx.ByteStream(b"not-a-valid-compressed-body"),
                )
            ),
        )

    monkeypatch.setattr(web_fetch_module.httpx, "Client", client_factory)
    with pytest.raises(WebFetchError, match="compressed"):
        fetch_web_page(
            "https://docs.example.com/docs/article",
            domains=frozenset({"example.com"}),
            path_policies=DOCUMENTED_PATHS,
            resolver=public_resolver,
            authorize_url=lambda url: None,
        )

    assert created["trust_env"] is False
    assert created["follow_redirects"] is False
    assert created["headers"]["Accept-Encoding"] == "identity"


def test_fetch_blocks_redirect_to_a_domain_outside_the_allowlist() -> None:
    client = httpx.Client(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(
                302,
                headers={"location": "https://outside.example/result"},
            )
        )
    )

    with pytest.raises(WebFetchError, match="not in WEB_FETCH_ALLOWED_DOMAINS"):
        fetch_web_page(
            "https://example.com/docs/start",
            domains=frozenset({"example.com"}),
            path_policies=DOCUMENTED_PATHS,
            resolver=public_resolver,
            client=client,
            authorize_url=lambda url: None,
        )
    client.close()


def test_fetch_enforces_download_limit() -> None:
    client = httpx.Client(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(
                200,
                headers={"content-type": "text/plain"},
                content=b"x" * 2_000,
            )
        )
    )

    with pytest.raises(WebFetchError, match="download limit"):
        fetch_web_page(
            "https://example.com/docs/large",
            domains=frozenset({"example.com"}),
            path_policies=DOCUMENTED_PATHS,
            resolver=public_resolver,
            client=client,
            max_bytes=1_024,
            authorize_url=lambda url: None,
        )
    client.close()


def test_brave_search_normalizes_results_without_exposing_the_key() -> None:
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["key"] = request.headers["x-subscription-token"]
        captured["query"] = request.url.params["q"]
        return httpx.Response(
            200,
            json={
                "web": {
                    "results": [
                        {
                            "title": "  Official   release ",
                            "url": "https://example.com/release",
                            "description": "  Timestamped   source summary. ",
                        },
                        {
                            "title": "Unsafe URL",
                            "url": "file:///etc/passwd",
                            "description": "Ignored.",
                        },
                    ]
                }
            },
        )

    client = httpx.Client(transport=httpx.MockTransport(handler))
    response = search_brave(
        "  gold   market news ",
        api_key="secret-search-key",
        client=client,
    )

    assert captured == {"key": "secret-search-key", "query": "gold market news"}
    assert response.query == "gold market news"
    assert response.results[0].title == "Official release"
    assert response.results[0].snippet == "Timestamped source summary."
    assert len(response.results) == 1
    assert json.dumps(response.model_dump()).count("secret-search-key") == 0
    client.close()


def test_brave_search_fails_closed_without_a_key() -> None:
    with pytest.raises(WebSearchError, match="BRAVE_SEARCH_API_KEY"):
        search_brave("market news", api_key="")


@pytest.mark.parametrize(
    "query",
    [
        "OPENAI_API_KEY=sk-secretvalue123456789",
        "review https://private.example/my-trade",
        "email trader@example.com about gold",
        "a" * 90,
    ],
)
def test_brave_search_rejects_private_or_encoded_query_data(query: str) -> None:
    with pytest.raises(WebSearchError, match="private|credential"):
        search_brave(query, api_key="configured")
