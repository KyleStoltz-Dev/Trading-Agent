import ipaddress
import re
import socket
from collections.abc import Callable
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from html.parser import HTMLParser
from urllib.parse import urljoin, urlparse, urlunparse

import httpx

Resolver = Callable[[str, int], tuple[ipaddress.IPv4Address | ipaddress.IPv6Address, ...]]
AuthorizeURL = Callable[[str], None]
ALLOWED_CONTENT_TYPES = frozenset(
    {
        "text/html",
        "text/plain",
        "application/json",
        "application/ld+json",
        "application/xml",
        "text/xml",
    }
)
REDIRECT_CODES = frozenset({301, 302, 303, 307, 308})
DOMAIN_LABEL = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")
SAFE_PATH = re.compile(r"^/[A-Za-z0-9._~/-]*$")
SENSITIVE_PATH = re.compile(
    r"(?:"
    r"journal|private|secret|password|passwd|token|credential|api[-_]?key|"
    r"trade[-_]?notes?|account[-_]?id|session[-_]?id"
    r")",
    re.IGNORECASE,
)
HIGH_ENTROPY_SEGMENT = re.compile(r"^[A-Za-z0-9_-]{24,}$")


class WebFetchError(ValueError):
    pass


@dataclass(frozen=True)
class WebPage:
    url: str
    retrieved_at: str
    content_type: str
    title: str | None
    text: str
    truncated: bool

    def model_dump(self) -> dict:
        return asdict(self)


class _ReadableHTML(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.title_parts: list[str] = []
        self._ignored_depth = 0
        self._in_title = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        del attrs
        if tag in {"script", "style", "noscript", "svg"}:
            self._ignored_depth += 1
        if tag == "title":
            self._in_title = True
        if tag in {"p", "div", "section", "article", "li", "br", "h1", "h2", "h3", "tr"}:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in {"script", "style", "noscript", "svg"} and self._ignored_depth:
            self._ignored_depth -= 1
        if tag == "title":
            self._in_title = False

    def handle_data(self, data: str) -> None:
        if self._ignored_depth:
            return
        stripped = " ".join(data.split())
        if not stripped:
            return
        self.parts.append(stripped)
        self.parts.append(" ")
        if self._in_title:
            self.title_parts.append(stripped)

    def result(self) -> tuple[str | None, str]:
        lines = (" ".join(line.split()) for line in "".join(self.parts).splitlines())
        text = "\n".join(line for line in lines if line)
        title = " ".join(self.title_parts).strip() or None
        return title, text


def resolve_addresses(
    hostname: str,
    port: int,
) -> tuple[ipaddress.IPv4Address | ipaddress.IPv6Address, ...]:
    try:
        values = socket.getaddrinfo(
            hostname,
            port,
            type=socket.SOCK_STREAM,
        )
    except socket.gaierror as exc:
        raise WebFetchError("web host could not be resolved") from exc
    addresses = tuple(
        dict.fromkeys(ipaddress.ip_address(item[4][0]) for item in values)
    )
    if not addresses:
        raise WebFetchError("web host did not resolve to an address")
    return addresses


def allowed_domains(value: str) -> frozenset[str]:
    domains: set[str] = set()
    for raw in value.split(","):
        domain = raw.strip().lower().rstrip(".")
        if not domain:
            continue
        if (
            "://" in domain
            or "/" in domain
            or ":" in domain
            or domain.startswith(".")
            or domain.endswith(".")
        ):
            raise WebFetchError(f"invalid allowed domain: {raw.strip()}")
        try:
            normalized = domain.encode("idna").decode("ascii")
        except UnicodeError as exc:
            raise WebFetchError(f"invalid allowed domain: {raw.strip()}") from exc
        labels = normalized.split(".")
        if (
            len(normalized) > 253
            or len(labels) < 2
            or any(not DOMAIN_LABEL.fullmatch(label) for label in labels)
        ):
            raise WebFetchError(f"invalid allowed domain: {raw.strip()}")
        try:
            ipaddress.ip_address(normalized)
        except ValueError:
            pass
        else:
            raise WebFetchError("IP addresses cannot be web-fetch allowlist entries")
        domains.add(normalized)
    return frozenset(domains)


def allowed_domain_paths(value: str) -> dict[str, tuple[str, ...]]:
    """Parse `domain=/path/,/other/;domain2=/docs/` path constraints."""
    policies: dict[str, tuple[str, ...]] = {}
    for raw_entry in value.split(";"):
        entry = raw_entry.strip()
        if not entry:
            continue
        domain, separator, raw_paths = entry.partition("=")
        normalized_domains = allowed_domains(domain)
        if not separator or len(normalized_domains) != 1:
            raise WebFetchError(
                "allowed path policy must use domain=/documented/path/"
            )
        paths: list[str] = []
        for raw_path in raw_paths.split(","):
            path = raw_path.strip()
            if (
                not path.startswith("/")
                or "\\" in path
                or "%" in path
                or "?" in path
                or "#" in path
                or not SAFE_PATH.fullmatch(path)
            ):
                raise WebFetchError(f"invalid allowed documented path: {path}")
            if path != "/" and not path.endswith("/"):
                raise WebFetchError(
                    "allowed documented path prefixes must end with /"
                )
            if path != "/" and any(
                segment in {"", ".", ".."} for segment in path[1:-1].split("/")
            ):
                raise WebFetchError(f"invalid allowed documented path: {path}")
            paths.append(path)
        if not paths:
            raise WebFetchError(f"no documented paths configured for {domain}")
        key = next(iter(normalized_domains))
        policies[key] = tuple(dict.fromkeys(paths))
    return policies


def _host_is_allowed(hostname: str, domains: frozenset[str]) -> bool:
    normalized = hostname.lower().rstrip(".").encode("idna").decode("ascii")
    return any(
        normalized == domain or normalized.endswith(f".{domain}")
        for domain in domains
    )


def _allowed_path_prefixes(
    hostname: str,
    policies: dict[str, tuple[str, ...]],
) -> tuple[str, ...] | None:
    normalized = hostname.lower().rstrip(".").encode("idna").decode("ascii")
    matches = [
        (domain, paths)
        for domain, paths in policies.items()
        if normalized == domain or normalized.endswith(f".{domain}")
    ]
    if not matches:
        return None
    return max(matches, key=lambda item: len(item[0]))[1]


def _validate_documented_path(
    hostname: str,
    path: str,
    policies: dict[str, tuple[str, ...]] | None,
) -> None:
    if policies is None:
        return
    if (
        len(path) > 300
        or "\\" in path
        or "%" in path
        or not SAFE_PATH.fullmatch(path)
        or "//" in path
    ):
        raise WebFetchError("web URL path is not a conservative documented path")
    segments = [segment for segment in path.split("/") if segment]
    if (
        any(segment in {".", ".."} for segment in segments)
        or any(len(segment) > 64 for segment in segments)
        or any(HIGH_ENTROPY_SEGMENT.fullmatch(segment) for segment in segments)
        or SENSITIVE_PATH.search(path)
    ):
        raise WebFetchError("web URL path may contain private or secret material")
    prefixes = _allowed_path_prefixes(hostname, policies)
    if prefixes is None:
        raise WebFetchError("web domain has no documented path policy")
    if not any(
        path == prefix
        if prefix == "/"
        else path.startswith(prefix)
        for prefix in prefixes
    ):
        raise WebFetchError("web URL path is outside configured documented paths")


def _validated_destination(
    url: str,
    resolver: Resolver,
    domains: frozenset[str] | None,
    path_policies: dict[str, tuple[str, ...]] | None,
    authorize_url: AuthorizeURL | None = None,
) -> tuple[str, tuple[ipaddress.IPv4Address | ipaddress.IPv6Address, ...]]:
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise WebFetchError("web URL must use HTTP(S)")
    if domains is not None and parsed.scheme != "https":
        raise WebFetchError("allowlisted documented web URLs must use HTTPS")
    if parsed.username or parsed.password or parsed.fragment:
        raise WebFetchError("web URL cannot contain credentials or a fragment")
    if parsed.query:
        raise WebFetchError(
            "model-selected web URLs cannot contain query parameters"
        )
    hostname = parsed.hostname.lower().rstrip(".").encode("idna").decode("ascii")
    if domains is not None and not _host_is_allowed(hostname, domains):
        raise WebFetchError("web domain is not in WEB_FETCH_ALLOWED_DOMAINS")
    _validate_documented_path(hostname, parsed.path or "/", path_policies)
    try:
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
    except ValueError as exc:
        raise WebFetchError("web URL contains an invalid port") from exc
    if port not in {80, 443}:
        raise WebFetchError("web URL must use port 80 or 443")
    netloc_host = f"[{hostname}]" if ":" in hostname else hostname
    netloc = (
        f"{netloc_host}:{parsed.port}"
        if parsed.port is not None
        else netloc_host
    )
    canonical_url = urlunparse(
        parsed._replace(
            scheme=parsed.scheme.lower(),
            netloc=netloc,
            path=parsed.path or "/",
        )
    )
    if authorize_url is not None:
        authorize_url(canonical_url)
    addresses = resolver(hostname, port)
    if any(not address.is_global for address in addresses):
        raise WebFetchError("web URL resolves to a private or non-public address")
    return canonical_url, addresses


def validate_public_url(
    url: str,
    resolver: Resolver = resolve_addresses,
    domains: frozenset[str] | None = None,
    path_policies: dict[str, tuple[str, ...]] | None = None,
) -> str:
    validated, _ = _validated_destination(
        url,
        resolver,
        domains,
        path_policies,
    )
    return validated


def _pinned_url(
    logical_url: str,
    address: ipaddress.IPv4Address | ipaddress.IPv6Address,
) -> tuple[str, str]:
    parsed = urlparse(logical_url)
    if parsed.hostname is None:
        raise WebFetchError("web URL has no hostname")
    host = f"[{address}]" if address.version == 6 else str(address)
    port = parsed.port
    netloc = f"{host}:{port}" if port is not None else host
    pinned = urlunparse(parsed._replace(netloc=netloc))
    host_header = parsed.hostname
    if ":" in host_header:
        host_header = f"[{host_header}]"
    if port is not None:
        host_header = f"{host_header}:{port}"
    return pinned, host_header


def _decode(data: bytes, content_type: str) -> str:
    charset = "utf-8"
    for part in content_type.split(";")[1:]:
        key, separator, value = part.strip().partition("=")
        if separator and key.lower() == "charset":
            charset = value.strip("\"'")
    try:
        return data.decode(charset, errors="replace")
    except LookupError:
        return data.decode("utf-8", errors="replace")


def fetch_web_page(
    url: str,
    *,
    timeout_seconds: float = 10,
    max_bytes: int = 1_000_000,
    max_text_characters: int = 30_000,
    max_redirects: int = 3,
    client: httpx.Client | None = None,
    resolver: Resolver = resolve_addresses,
    domains: frozenset[str] | None = None,
    path_policies: dict[str, tuple[str, ...]] | None = None,
    authorize_url: AuthorizeURL,
) -> WebPage:
    current, addresses = _validated_destination(
        url,
        resolver,
        domains,
        path_policies,
        authorize_url,
    )
    owns_client = client is None
    session = client or httpx.Client(
        timeout=timeout_seconds,
        trust_env=False,
        follow_redirects=False,
        headers={
            "User-Agent": "Trading-Agent/0.1 (+read-only-web-fetch)",
            "Accept": "text/html,text/plain,application/json,application/ld+json",
            "Accept-Encoding": "identity",
        },
    )
    try:
        for redirect_count in range(max_redirects + 1):
            pinned_url, host_header = _pinned_url(current, addresses[0])
            hostname = urlparse(current).hostname
            if hostname is None:
                raise WebFetchError("web URL has no hostname")
            try:
                with session.stream(
                    "GET",
                    pinned_url,
                    headers={"Host": host_header},
                    extensions={"sni_hostname": hostname},
                ) as response:
                    if response.status_code in REDIRECT_CODES:
                        location = response.headers.get("location")
                        if not location:
                            raise WebFetchError("web redirect did not include a location")
                        if redirect_count >= max_redirects:
                            raise WebFetchError("web fetch exceeded the redirect limit")
                        current, addresses = _validated_destination(
                            urljoin(current, location),
                            resolver,
                            domains,
                            path_policies,
                            authorize_url,
                        )
                        continue
                    response.raise_for_status()
                    content_encoding = response.headers.get(
                        "content-encoding",
                        "",
                    ).strip().lower()
                    if content_encoding not in {"", "identity"}:
                        raise WebFetchError(
                            "compressed web responses are not accepted"
                        )
                    raw_content_type = response.headers.get("content-type", "")
                    content_type = raw_content_type.split(";", 1)[0].strip().lower()
                    if content_type not in ALLOWED_CONTENT_TYPES:
                        raise WebFetchError(
                            f"web content type is not allowed: {content_type or 'missing'}"
                        )
                    content_length = response.headers.get("content-length")
                    if content_length:
                        try:
                            declared_length = int(content_length)
                        except ValueError as exc:
                            raise WebFetchError(
                                "web response has an invalid content length"
                            ) from exc
                        if declared_length > max_bytes:
                            raise WebFetchError(
                                "web response exceeds the download limit"
                            )
                    data = bytearray()
                    for chunk in response.iter_bytes():
                        data.extend(chunk)
                        if len(data) > max_bytes:
                            raise WebFetchError("web response exceeds the download limit")
            except httpx.HTTPStatusError as exc:
                raise WebFetchError(
                    f"web request failed with HTTP {exc.response.status_code}"
                ) from exc
            except httpx.HTTPError as exc:
                raise WebFetchError("web request failed") from exc

            decoded = _decode(bytes(data), raw_content_type)
            title: str | None = None
            if content_type == "text/html":
                parser = _ReadableHTML()
                parser.feed(decoded)
                title, decoded = parser.result()
            text = decoded.strip()
            truncated = len(text) > max_text_characters
            if truncated:
                text = text[:max_text_characters].rstrip()
            return WebPage(
                url=current,
                retrieved_at=datetime.now(UTC).isoformat(),
                content_type=content_type,
                title=title,
                text=text,
                truncated=truncated,
            )
        raise WebFetchError("web fetch exceeded the redirect limit")
    finally:
        if owns_client:
            session.close()
