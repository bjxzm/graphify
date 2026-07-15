# Security helpers - URL validation, safe fetch, path guards, label sanitisation
from __future__ import annotations

import html
import http.client
import os
import re
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import ipaddress
import socket

from graphify.paths import GRAPHIFY_OUT, GRAPHIFY_OUT_NAME

_ALLOWED_SCHEMES = {"http", "https"}
_MAX_FETCH_BYTES = 52_428_800   # 50 MB hard cap for binary downloads
_MAX_TEXT_BYTES  = 10_485_760   # 10 MB hard cap for HTML / text

# Graph-load memory-bomb cap: reject .json files larger than this before
# JSON-parsing them into a dict. Without this, a multi-gigabyte (or
# specifically crafted) graph.json can exhaust process memory during
# json.loads + node_link_graph rehydration.
# Default fallback cap. Kept as a module-level constant so the value is
# discoverable and so existing callers/tests that reference it directly keep
# working; the effective cap is resolved at call time by
# ``_max_graph_file_bytes`` (which lets ``GRAPHIFY_MAX_GRAPH_BYTES`` override it).
_MAX_GRAPH_FILE_BYTES = 512 * 1024 * 1024   # 512 MiB


def _max_graph_file_bytes() -> int:
    """Return the graph.json size cap in bytes.

    Honors the ``GRAPHIFY_MAX_GRAPH_BYTES`` environment variable so users with
    large codebases can raise the limit without editing source. The value may
    be plain bytes (``671088640``) or carry an ``MB`` / ``GB`` suffix
    (``640MB``, ``2GB`` — case-insensitive, binary multipliers: ``MB`` is
    1024*1024 and ``GB`` is 1024*1024*1024, i.e. MiB / GiB).
    Falls back to ``_MAX_GRAPH_FILE_BYTES`` (512 MiB) when the env var is unset,
    blank, or unparseable.

    Read fresh on every call so the env var can be set before import and still
    take effect.
    """
    raw = os.environ.get("GRAPHIFY_MAX_GRAPH_BYTES", "").strip()
    if not raw:
        return _MAX_GRAPH_FILE_BYTES
    text = raw.upper()
    multiplier = 1
    if text.endswith("GB"):
        multiplier = 1024 * 1024 * 1024
        text = text[:-2].strip()
    elif text.endswith("MB"):
        multiplier = 1024 * 1024
        text = text[:-2].strip()
    try:
        value = int(text)
    except ValueError:
        return _MAX_GRAPH_FILE_BYTES
    if value <= 0:
        return _MAX_GRAPH_FILE_BYTES
    return value * multiplier

# AWS metadata, link-local, and common cloud metadata endpoints
_BLOCKED_HOSTS = {"metadata.google.internal", "metadata.google.com"}

# RFC 6598 Shared Address Space (CGN) -- is_private misses this on Python <3.11
_CGN_NETWORK = ipaddress.ip_network("100.64.0.0/10")

# RFC 6052 NAT64 Well-Known Prefix -- is_reserved=True in Python but these embed
# public IPv4 addresses and are legitimate public internet traffic, not SSRF vectors.
_NAT64_WKP = ipaddress.ip_network("64:ff9b::/96")


# ---------------------------------------------------------------------------
# URL validation
# ---------------------------------------------------------------------------

def _ip_is_blocked(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    """Return True if *ip* falls in a private/reserved/internal range.

    Shared by validate_url (pre-flight DNS check) and the SSRF-guarded
    connection classes (connect-time check) so both use identical logic.
    NAT64 well-known-prefix addresses are unwrapped to their embedded IPv4
    before the check, since those carry legitimate public traffic.
    """
    # For NAT64 addresses, check the embedded IPv4 instead of the wrapper
    if isinstance(ip, ipaddress.IPv6Address) and ip in _NAT64_WKP:
        ip = ipaddress.ip_address(int(ip) & 0xFFFFFFFF)
    return (
        ip.is_private
        or ip.is_reserved
        or ip.is_loopback
        or ip.is_link_local
        or ip in _CGN_NETWORK
    )


def validate_url(url: str) -> str:
    """Raise ValueError if *url* is not http or https, or targets a private/internal IP.

    Blocks file://, ftp://, data:, and any other scheme that could be used
    for SSRF or local file access. Also blocks requests to private/reserved
    IP ranges (127.x, 10.x, 169.254.x, etc.) and cloud metadata endpoints
    to prevent SSRF in cloud environments.
    """
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme.lower() not in _ALLOWED_SCHEMES:
        raise ValueError(
            f"Blocked URL scheme '{parsed.scheme}' - only http and https are allowed. "
            f"Got: {url!r}"
        )

    hostname = parsed.hostname
    if hostname:
        # Block known cloud metadata hostnames
        if hostname.lower() in _BLOCKED_HOSTS:
            raise ValueError(
                f"Blocked cloud metadata endpoint '{hostname}'. "
                f"Got: {url!r}"
            )

        # Resolve hostname and block private/reserved IP ranges
        try:
            infos = socket.getaddrinfo(hostname, None, socket.AF_UNSPEC, socket.SOCK_STREAM)
            for info in infos:
                addr = info[4][0]
                ip = ipaddress.ip_address(addr)
                if _ip_is_blocked(ip):
                    raise ValueError(
                        f"Blocked private/internal IP {addr} (resolved from '{hostname}'). "
                        f"Got: {url!r}"
                    )
        except socket.gaierror as exc:
            raise ValueError(
                f"DNS resolution failed for '{hostname}': {exc}. Got: {url!r}"
            ) from exc

    return url


# ---------------------------------------------------------------------------
# SSRF-guarded connections
#
# Instead of monkey-patching the process-global socket.getaddrinfo (a
# non-thread-safe TOCTOU hazard when multiple fetches run concurrently),
# we subclass the HTTP(S) connection so each connection resolves DNS exactly
# once, validates the resulting IP, and then connects to that exact IP. There
# is no second resolution, so a DNS-rebind attack cannot swap in a private
# address (e.g. 169.254.169.254) between validation and connection.
# ---------------------------------------------------------------------------


def _resolve_and_validate(host: str, port: int) -> tuple[int, str]:
    """Resolve *host* once and return (family, validated_ip) for the first
    address that is not in a blocked range.

    Raises OSError if every resolved address is private/reserved/internal,
    matching the failure mode urllib/http.client expect from connect().
    """
    infos = socket.getaddrinfo(host, port, socket.AF_UNSPEC, socket.SOCK_STREAM)
    for family, _type, _proto, _canon, sockaddr in infos:
        addr = str(sockaddr[0])
        try:
            ip = ipaddress.ip_address(addr)
        except ValueError:
            continue
        if _ip_is_blocked(ip):
            raise OSError(
                f"SSRF blocked: IP {addr} resolved from '{host}' is private/reserved"
            )
        return family, addr
    raise OSError(f"SSRF blocked: no usable address resolved from '{host}'")


def _normalise_host(host: str) -> str:
    """Return a stable representation for proxy endpoint comparisons."""
    value = host.strip().strip("[]").rstrip(".").lower()
    try:
        return str(ipaddress.ip_address(value))
    except ValueError:
        return value


def _is_configured_proxy_endpoint(host: str, port: int) -> bool:
    """Return True when *host*:*port* exactly matches an OS/env proxy.

    urllib transparently rewrites a request's connection host to the configured
    proxy. The guard must distinguish that transport endpoint from the still
    untrusted destination URL.
    """
    wanted_host = _normalise_host(host)
    for name, raw_proxy in urllib.request.getproxies().items():
        if name.lower() == "no" or not isinstance(raw_proxy, str):
            continue
        value = raw_proxy.strip()
        if not value:
            continue
        if "://" not in value:
            value = f"http://{value}"
        try:
            parsed = urllib.parse.urlparse(value)
            proxy_host = parsed.hostname
            proxy_port = parsed.port
        except ValueError:
            continue
        if not proxy_host:
            continue
        if proxy_port is None:
            proxy_port = 443 if parsed.scheme.lower() == "https" else 80
        if _normalise_host(proxy_host) == wanted_host and proxy_port == port:
            return True
    return False


def _resolve_configured_local_proxy(host: str, port: int) -> tuple[int, str]:
    """Resolve an explicitly configured loopback proxy and pin its IP.

    Private-network proxies remain blocked. This exception is intentionally
    limited to loopback endpoints controlled by the local environment.
    """
    if not _is_configured_proxy_endpoint(host, port):
        raise OSError(
            f"SSRF blocked: '{host}:{port}' is not an explicitly configured proxy"
        )

    infos = socket.getaddrinfo(host, port, socket.AF_UNSPEC, socket.SOCK_STREAM)
    usable: list[tuple[int, str]] = []
    for family, _type, _proto, _canon, sockaddr in infos:
        addr = str(sockaddr[0])
        try:
            ip = ipaddress.ip_address(addr)
        except ValueError:
            continue
        if not ip.is_loopback:
            raise OSError(
                f"SSRF blocked: configured proxy '{host}:{port}' resolved to "
                f"non-loopback IP {addr}"
            )
        usable.append((family, addr))
    if usable:
        return usable[0]
    raise OSError(f"SSRF blocked: no usable address resolved from proxy '{host}'")


def _request_target(req: urllib.request.Request) -> tuple[str, int]:
    parsed = urllib.parse.urlparse(req.full_url)
    if not parsed.hostname:
        raise ValueError(f"URL has no hostname: {req.full_url!r}")
    port = parsed.port or (443 if parsed.scheme.lower() == "https" else 80)
    return parsed.hostname, port


def _guarded_endpoint(
    host: str,
    port: int,
    target_host: str,
    target_port: int,
) -> tuple[int, str]:
    """Validate a direct target or an explicitly configured local proxy hop."""
    if _is_configured_proxy_endpoint(host, port):
        # The proxy is only a transport hop; the destination still gets a
        # fresh connect-time SSRF check.
        _resolve_and_validate(target_host, target_port)
        return _resolve_configured_local_proxy(host, port)
    return _resolve_and_validate(host, port)


class _SSRFGuardedHTTPConnection(http.client.HTTPConnection):
    """HTTPConnection that pins either a public target or a local proxy IP."""

    def __init__(
        self,
        host: str,
        port: int | None = None,
        *,
        target_host: str,
        target_port: int,
        **kwargs,
    ) -> None:
        super().__init__(host, port=port, **kwargs)
        self._graphify_target_host = target_host
        self._graphify_target_port = target_port

    def _guarded_endpoint(self) -> tuple[int, str]:
        return _guarded_endpoint(
            self.host,
            self.port,
            self._graphify_target_host,
            self._graphify_target_port,
        )

    def connect(self) -> None:
        _family, ip = self._guarded_endpoint()
        self.sock = socket.create_connection(
            (ip, self.port),
            self.timeout,
            getattr(self, "source_address", None),
        )
        if getattr(self, "_tunnel_host", None):
            getattr(self, "_tunnel")()


class _SSRFGuardedHTTPSConnection(http.client.HTTPSConnection):
    """HTTPSConnection that validates the destination behind a local proxy."""

    def __init__(
        self,
        host: str,
        port: int | None = None,
        *,
        target_host: str,
        target_port: int,
        **kwargs,
    ) -> None:
        super().__init__(host, port=port, **kwargs)
        self._graphify_target_host = target_host
        self._graphify_target_port = target_port

    def _guarded_endpoint(self) -> tuple[int, str]:
        return _guarded_endpoint(
            self.host,
            self.port,
            self._graphify_target_host,
            self._graphify_target_port,
        )

    def connect(self) -> None:
        _family, ip = self._guarded_endpoint()
        sock = socket.create_connection(
            (ip, self.port),
            self.timeout,
            getattr(self, "source_address", None),
        )
        if getattr(self, "_tunnel_host", None):
            self.sock = sock
            getattr(self, "_tunnel")()
            sock = self.sock
        server_hostname = getattr(self, "_tunnel_host", None) or self._graphify_target_host
        self.sock = getattr(self, "_context").wrap_socket(
            sock, server_hostname=server_hostname
        )


class _SSRFGuardedHTTPHandler(urllib.request.HTTPHandler):
    """urllib handler that routes http:// through _SSRFGuardedHTTPConnection."""

    def http_open(self, req):
        target_host, target_port = _request_target(req)

        def connection_factory(host, **kwargs):
            return _SSRFGuardedHTTPConnection(
                host,
                target_host=target_host,
                target_port=target_port,
                **kwargs,
            )

        return self.do_open(connection_factory, req)


class _SSRFGuardedHTTPSHandler(urllib.request.HTTPSHandler):
    """urllib handler that routes https:// through _SSRFGuardedHTTPSConnection."""

    def https_open(self, req):
        target_host, target_port = _request_target(req)

        def connection_factory(host, **kwargs):
            return _SSRFGuardedHTTPSConnection(
                host,
                target_host=target_host,
                target_port=target_port,
                **kwargs,
            )

        return self.do_open(connection_factory, req)


class _NoFileRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Redirect handler that re-validates every redirect target.

    Prevents open-redirect SSRF attacks where an http:// URL redirects
    to file:// or an internal address.
    """

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        validate_url(newurl)          # raises ValueError if scheme is wrong
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _build_opener() -> urllib.request.OpenerDirector:
    # build_opener replaces the default HTTP(S)Handlers with our SSRF-guarded
    # subclasses, so every connection resolves+validates DNS once and connects
    # to that exact IP. Thread-safe: no process-global state is mutated.
    return urllib.request.build_opener(
        _SSRFGuardedHTTPHandler,
        _SSRFGuardedHTTPSHandler,
        _NoFileRedirectHandler,
    )


# ---------------------------------------------------------------------------
# Safe fetch
# ---------------------------------------------------------------------------

def safe_fetch(url: str, max_bytes: int = _MAX_FETCH_BYTES, timeout: int = 30) -> bytes:
    """Fetch *url* and return raw bytes.

    Protections applied:
    - URL scheme validated (http / https only)
    - Redirects re-validated via _NoFileRedirectHandler
    - Response body capped at *max_bytes* (streaming read)
    - Non-2xx status raises urllib.error.HTTPError
    - Network errors propagate as urllib.error.URLError / OSError

    Raises:
        ValueError        - disallowed scheme or redirect target
        urllib.error.HTTPError  - non-2xx HTTP status
        urllib.error.URLError   - DNS / connection failure
        OSError               - size cap exceeded
    """
    validate_url(url)
    opener = _build_opener()
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 graphify/1.0"})

    with opener.open(req, timeout=timeout) as resp:
        # urllib raises HTTPError for non-2xx when using urlopen directly;
        # with a custom opener we check manually to be safe.
        status = getattr(resp, "status", None) or getattr(resp, "code", None)
        if status is not None and not (200 <= status < 300):
            raise urllib.error.HTTPError(url, status, f"HTTP {status}", {}, None)

        chunks: list[bytes] = []
        total = 0
        while True:
            chunk = resp.read(65_536)
            if not chunk:
                break
            total += len(chunk)
            if total > max_bytes:
                raise OSError(
                    f"Response from {url!r} exceeds size limit "
                    f"({max_bytes // 1_048_576} MB). Aborting download."
                )
            chunks.append(chunk)

    return b"".join(chunks)


def safe_fetch_text(url: str, max_bytes: int = _MAX_TEXT_BYTES, timeout: int = 15) -> str:
    """Fetch *url* and return decoded text (UTF-8, replacing bad bytes).

    Wraps safe_fetch with tighter defaults for HTML / text content.
    """
    raw = safe_fetch(url, max_bytes=max_bytes, timeout=timeout)
    return raw.decode("utf-8", errors="replace")


# ---------------------------------------------------------------------------
# Path validation
# ---------------------------------------------------------------------------

def validate_graph_path(path: str | Path, base: Path | None = None) -> Path:
    """Resolve *path* and verify it stays inside *base*.

    *base* defaults to the `graphify-out` directory relative to CWD.
    Also requires the base directory to exist, so a caller cannot
    trick graphify into reading files before any graph has been built.

    Raises:
        ValueError  - path escapes base, or base does not exist
        FileNotFoundError - resolved path does not exist
    """
    if base is None:
        resolved_hint = Path(path).resolve()
        for candidate in [resolved_hint, *resolved_hint.parents]:
            if candidate.name == GRAPHIFY_OUT_NAME:
                base = candidate
                break
        if base is None:
            base = Path(GRAPHIFY_OUT).resolve()

    base = base.resolve()
    if not base.exists():
        raise ValueError(
            f"Graph base directory does not exist: {base}. "
            "Run /graphify first to build the graph."
        )

    resolved = Path(path).resolve()
    try:
        resolved.relative_to(base)
    except ValueError:
        raise ValueError(
            f"Path {path!r} escapes the allowed directory {base}. "
            "Only paths inside graphify-out/ are permitted."
        )

    if not resolved.exists():
        raise FileNotFoundError(f"Graph file not found: {resolved}")

    return resolved


def check_graph_file_size_cap(path: Path) -> None:
    """Reject *path* if its size exceeds the configured graph-file cap.

    Protects callers from memory bombs by failing fast before a multi-GiB
    graph.json is read into memory and JSON-parsed. Silently returns when
    ``path.stat()`` cannot be read — the caller's own existence/path check
    is expected to surface a clearer error in that case.

    The cap is resolved on every call via :func:`_max_graph_file_bytes`, so the
    ``GRAPHIFY_MAX_GRAPH_BYTES`` env var can be set before import and still
    apply.

    Raises:
        ValueError - file size exceeds the cap. The message includes the
        observed size, the cap, and how to raise the limit.
    """
    cap = _max_graph_file_bytes()
    try:
        size = path.stat().st_size
    except OSError:
        return
    if size > cap:
        raise ValueError(
            f"graph file {path} is {size:_d} bytes, exceeds {cap:_d}-byte cap\n"
            f"(set GRAPHIFY_MAX_GRAPH_BYTES=<bytes> or "
            f"GRAPHIFY_MAX_GRAPH_BYTES=<N>GB to raise the limit)"
        )


# ---------------------------------------------------------------------------
# Label sanitisation (mirrors code-review-graph's _sanitize_name pattern)
# ---------------------------------------------------------------------------

_CONTROL_CHAR_RE = re.compile(r"[\x00-\x1f\x7f]")
_MAX_LABEL_LEN = 256


def sanitize_label(text: str | None) -> str:
    """Strip control characters and cap length.

    Safe for embedding in JSON data (inside <script> tags) and plain text.
    For direct HTML injection, wrap the result with html.escape().
    """
    if text is None:
        return ""
    text = _CONTROL_CHAR_RE.sub("", str(text))
    if len(text) > _MAX_LABEL_LEN:
        text = text[:_MAX_LABEL_LEN]
    return text


# ---------------------------------------------------------------------------
# Metadata sanitisation (recursive, bounded, HTML-safe)
# ---------------------------------------------------------------------------

_METADATA_MAX_VALUE_LEN = 512
_METADATA_MAX_LIST_ITEMS = 50


def _sanitize_metadata_string(value: object) -> str:
    """Return a control-character-free, HTML-escaped, bounded string."""
    text = _CONTROL_CHAR_RE.sub("", str(value))
    text = html.escape(text, quote=True)
    if len(text) > _METADATA_MAX_VALUE_LEN:
        text = text[:_METADATA_MAX_VALUE_LEN]
    return text  # html is imported at module level (line 5)


def _sanitize_metadata_value(value: object) -> object:
    """Sanitize a metadata value while preserving simple JSON-compatible types."""
    if isinstance(value, bool):
        # bool is a subclass of int — must be checked first to avoid coercion.
        return value
    if isinstance(value, str):
        return _sanitize_metadata_string(value)
    if isinstance(value, dict):
        return sanitize_metadata(value)
    if isinstance(value, (list, tuple)):
        return [_sanitize_metadata_value(item) for item in value[:_METADATA_MAX_LIST_ITEMS]]
    if isinstance(value, (int, float)) or value is None:
        return value
    return _sanitize_metadata_string(value)


def sanitize_metadata(metadata: Mapping[str, Any] | None) -> dict[str, object]:
    """Sanitize metadata keys and values before graph export.

    Metadata is less constrained than node labels: it can contain nested
    dicts, lists, source snippets, external index symbols, and docstring
    text. This helper keeps the data JSON-compatible, strips control
    characters, escapes HTML-sensitive characters in strings, caps long
    strings/lists, and drops entries whose key becomes empty after
    sanitization.
    """
    if metadata is None:
        return {}

    result: dict[str, object] = {}
    for key, value in metadata.items():
        clean_key = _sanitize_metadata_string(key)
        if not clean_key:
            continue
        result[clean_key] = _sanitize_metadata_value(value)
    return result
