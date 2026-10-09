"""Local-first, review-first feed and quote connectors.

This module deliberately has no import-time network behaviour.  A ``Collector``
only makes an external request after a configuration has an explicit global
authorization, an enabled flag, and a per-feed authorization.  Collected feed
entries are metadata-only suggestions; they are never facts and are never
marked as approved by this module.
"""
from __future__ import annotations

import hashlib
import html
import http.client
import inspect
import ipaddress
import json
import math
import os
import re
import socket
import ssl
import tempfile
import threading
import time
import xml.etree.ElementTree as ET
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Optional
from urllib.parse import SplitResult, parse_qsl, quote, urlencode, urljoin, urlsplit, urlunsplit

try:  # pragma: no cover - fcntl is present on the supported runtime
    import fcntl
except ImportError:  # pragma: no cover - makes the module importable on Windows
    fcntl = None  # type: ignore[assignment]


MAX_RESPONSE_BYTES = 1_048_576
MAX_ITEMS_PER_FEED = 30
MAX_FEEDS_PER_RUN = 5
MIN_INTERVAL_MINUTES = 15
DEFAULT_INTERVAL_MINUTES = 60
HTTP_TIMEOUT_SECONDS = 10.0
MAX_URL_LENGTH = 4096
MAX_TITLE_LENGTH = 500
MAX_DATE_LENGTH = 128
STATE_FILENAME = "connectors_state.json"
LOCK_FILENAME = "connectors_state.lock"

_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")
_XML_DTD_RE = re.compile(br"<!\s*(?:DOCTYPE|ENTITY)\b", re.IGNORECASE)
_TAG_RE = re.compile(r"<[^>]*>")
_WS_RE = re.compile(r"\s+")
_SAFE_HOST_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
_SYMBOL_RE = re.compile(r"^[A-Za-z0-9._-]{1,32}$")


class ConnectorError(Exception):
    """Base class for connector errors."""


class ConfigurationError(ConnectorError, ValueError):
    """Raised when a connector configuration is unsafe or incomplete."""


class UnsafeURLError(ConfigurationError):
    """Raised when a URL is not safe for the connector's network policy."""


class FeedFetchError(ConnectorError):
    """Raised for a failed or disallowed feed HTTP response."""


class FeedParseError(ConnectorError):
    """Raised for malformed or unsafe feed data."""


class QuotaError(ConnectorError):
    """Raised when an optional provider's local quota guard trips."""


@dataclass(frozen=True)
class FetchResult:
    """Small response object accepted by the collector and its test doubles."""

    status: int = 200
    headers: Mapping[str, str] | None = None
    body: bytes = b""

    def normalized_headers(self) -> dict[str, str]:
        return {str(k).lower(): str(v).strip() for k, v in (self.headers or {}).items()}


@dataclass(frozen=True)
class _ParsedEntry:
    title: str
    date: Optional[str]
    url: Optional[str]


class _PinnedHTTPSConnection(http.client.HTTPSConnection):
    """HTTPSConnection which connects to one already-resolved IP address.

    ``HTTPSConnection`` normally resolves the hostname inside ``connect``.
    Overriding that method means the address checked by the caller is exactly
    the address used for the TCP connection.  The original hostname remains in
    ``server_hostname`` for certificate and SNI validation.
    """

    def __init__(
        self,
        hostname: str,
        ip: str,
        port: int,
        *,
        timeout: float,
        context: ssl.SSLContext,
    ) -> None:
        super().__init__(hostname, port=port, timeout=timeout, context=context)
        self._pinned_ip = ip

    def connect(self) -> None:  # pragma: no cover - exercised with a real socket only
        self.sock = socket.create_connection((self._pinned_ip, self.port), self.timeout)
        try:
            self.sock = self._context.wrap_socket(self.sock, server_hostname=self.host)
        except BaseException:
            self.close()
            raise


def _default_state() -> dict[str, Any]:
    return {
        "version": 1,
        "feeds": [],
        "enabled": False,
        "authorized": False,
        "interval_minutes": DEFAULT_INTERVAL_MINUTES,
        "last_run": None,
        "next_run": None,
        "last_error": None,
        "inbox": [],
        "seen_keys": [],
    }


def _copy_json(value: Any) -> Any:
    """Return a JSON-compatible deep copy without sharing mutable state."""

    return json.loads(json.dumps(value, ensure_ascii=False))


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _iso_datetime(value: datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat(timespec="seconds")


def _coerce_datetime(value: Any) -> Optional[datetime]:
    if isinstance(value, datetime):
        result = value
    elif isinstance(value, (int, float)) and not isinstance(value, bool):
        try:
            result = datetime.fromtimestamp(float(value), tz=timezone.utc)
        except (OverflowError, OSError, ValueError):
            return None
    elif isinstance(value, str) and value.strip():
        raw = value.strip()
        try:
            result = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError:
            return None
    else:
        return None
    if result.tzinfo is None:
        result = result.replace(tzinfo=timezone.utc)
    return result.astimezone(timezone.utc)


def _clean_text(value: Any, *, limit: int) -> str:
    if value is None:
        return ""
    text = html.unescape(str(value))
    text = _TAG_RE.sub(" ", text)
    text = _CONTROL_RE.sub(" ", text)
    text = _WS_RE.sub(" ", text).strip()
    return text[:limit]


def _clean_date(value: Any) -> str:
    """Keep feed dates as bounded, parseable metadata."""

    if value is None:
        return ""
    text = _clean_text(value, limit=MAX_DATE_LENGTH)
    if not text or _CONTROL_RE.search(text):
        return ""
    parsed = _coerce_datetime(text)
    if parsed is None:
        # RSS commonly uses RFC 822 dates; retain only a conservative date
        # token when ISO parsing is unavailable, rather than arbitrary XML.
        match = re.fullmatch(r"[A-Za-z]{3},?\s+\d{1,2}\s+[A-Za-z]{3}\s+\d{4}(?:\s+\d{2}:\d{2}(?::\d{2})?(?:\s+[A-Z]{2,5})?)?", text)
        return match.group(0) if match else ""
    return text


def _is_finite_positive(value: Any) -> bool:
    if isinstance(value, bool):
        return False
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return False
    return math.isfinite(number) and number > 0


def _normalise_headers(headers: Any) -> dict[str, str]:
    if headers is None:
        return {}
    if hasattr(headers, "items"):
        return {str(k).lower(): str(v).strip() for k, v in headers.items()}
    return {}


def _invoke_compatible(callable_obj: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
    """Invoke a dependency once using the arity its signature supports.

    A ``TypeError`` raised by the callable itself must never trigger a second
    invocation: fetchers and resolvers can have side effects, and retrying can
    duplicate requests or hide the original failure.  Signature inspection is
    only used to choose the call shape before invoking the dependency.
    """

    try:
        signature = inspect.signature(callable_obj)
    except (TypeError, ValueError):
        # Extension/builtin callables without an inspectable signature retain
        # the documented full contract; any exception is propagated once.
        return callable_obj(*args, **kwargs)

    candidates: list[tuple[tuple[Any, ...], dict[str, Any]]] = [
        (tuple(args), dict(kwargs)),
    ]
    if kwargs:
        candidates.append((tuple(args), {}))
    if args:
        candidates.append((tuple(args[:-1]), {}))

    for candidate_args, candidate_kwargs in candidates:
        try:
            signature.bind(*candidate_args, **candidate_kwargs)
        except TypeError:
            continue
        return callable_obj(*candidate_args, **candidate_kwargs)

    # Preserve a normal, safe TypeError for an incompatible dependency without
    # invoking it.  This branch is not a retry path.
    raise TypeError("callable_signature_incompatible")


def _coerce_fetch_result(value: Any) -> FetchResult:
    """Accept a few deliberately simple shapes for dependency-free test doubles."""

    if isinstance(value, FetchResult):
        result = value
    elif isinstance(value, (bytes, bytearray, memoryview, str)):
        body = value.encode("utf-8") if isinstance(value, str) else bytes(value)
        result = FetchResult(status=200, headers={}, body=body)
    elif isinstance(value, Mapping):
        body = value.get("body", b"")
        if isinstance(body, str):
            body = body.encode("utf-8")
        result = FetchResult(
            status=int(value.get("status", 200)),
            headers=_normalise_headers(value.get("headers")),
            body=bytes(body),
        )
    elif isinstance(value, tuple):
        if len(value) == 2:
            first, second = value
            if isinstance(first, int):
                status, body = first, second
                result = FetchResult(status=status, headers={}, body=body.encode("utf-8") if isinstance(body, str) else bytes(body))
            else:
                body, headers = first, second
                result = FetchResult(status=200, headers=_normalise_headers(headers), body=body.encode("utf-8") if isinstance(body, str) else bytes(body))
        elif len(value) == 3:
            status, headers, body = value
            result = FetchResult(status=int(status), headers=_normalise_headers(headers), body=body.encode("utf-8") if isinstance(body, str) else bytes(body))
        else:
            raise FeedFetchError("invalid_response")
    else:
        raise FeedFetchError("invalid_response")

    if not isinstance(result.body, bytes):
        try:
            body = bytes(result.body)
        except Exception as exc:  # pragma: no cover - defensive branch
            raise FeedFetchError("invalid_response") from exc
        result = FetchResult(result.status, result.headers, body)
    headers = result.normalized_headers()
    content_length = headers.get("content-length")
    if content_length:
        try:
            if int(content_length) < 0 or int(content_length) > MAX_RESPONSE_BYTES:
                raise FeedFetchError("response_too_large")
        except ValueError as exc:
            raise FeedFetchError("invalid_response") from exc
    if len(result.body) > MAX_RESPONSE_BYTES:
        raise FeedFetchError("response_too_large")
    return FetchResult(int(result.status), headers, result.body)


def _validate_source_url(url: Any) -> tuple[str, SplitResult]:
    """Validate and canonicalise a configured source URL without doing DNS."""

    if not isinstance(url, str):
        raise UnsafeURLError("source_url_invalid")
    raw = url.strip()
    if not raw or len(raw) > MAX_URL_LENGTH or _CONTROL_RE.search(raw) or "#" in raw:
        raise UnsafeURLError("source_url_invalid")
    try:
        parsed = urlsplit(raw)
        hostname = parsed.hostname
        port = parsed.port
    except ValueError as exc:
        raise UnsafeURLError("source_url_invalid") from exc
    if parsed.scheme.lower() != "https" or not hostname:
        raise UnsafeURLError("source_url_must_use_https")
    if parsed.query:
        try:
            query_keys = {key.lower() for key, _value in parse_qsl(parsed.query, keep_blank_values=True)}
        except ValueError as exc:
            raise UnsafeURLError("source_url_invalid") from exc
        if query_keys.intersection({"url", "uri", "redirect", "next", "target", "dest", "destination", "return"}):
            raise UnsafeURLError("source_url_redirect_parameter_forbidden")
    if parsed.username is not None or parsed.password is not None or "@" in parsed.netloc:
        raise UnsafeURLError("source_url_credentials_forbidden")
    if port is not None and not 1 <= port <= 65535:
        raise UnsafeURLError("source_url_invalid")
    try:
        host_ascii = hostname.encode("idna").decode("ascii").lower().rstrip(".")
    except (UnicodeError, AttributeError) as exc:
        raise UnsafeURLError("source_url_invalid") from exc
    if not host_ascii or not _SAFE_HOST_RE.match(host_ascii.replace(":", "")):
        # IPv6 literals contain colons and are handled by ipaddress below.
        try:
            ipaddress.ip_address(host_ascii)
        except ValueError as exc:
            raise UnsafeURLError("source_url_invalid") from exc
    try:
        literal_ip = ipaddress.ip_address(host_ascii)
    except ValueError:
        literal_ip = None
    if literal_ip is not None and not literal_ip.is_global:
        raise UnsafeURLError("source_host_not_public")
    if host_ascii.lower() in {"localhost", "localhost.localdomain"} or host_ascii.endswith((".localhost", ".local")):
        raise UnsafeURLError("source_host_not_public")

    if literal_ip is not None and literal_ip.version == 6:
        netloc_host = f"[{host_ascii}]"
    else:
        netloc_host = host_ascii
    if port is not None and port != 443:
        netloc = f"{netloc_host}:{port}"
    else:
        netloc = netloc_host
    canonical = urlunsplit(("https", netloc, parsed.path or "", parsed.query, ""))
    return canonical, urlsplit(canonical)


def _sanitize_item_url(raw_url: Any, source_url: str) -> Optional[str]:
    """Keep only navigable HTTP(S) item links and strip fragments/credentials."""

    if raw_url is None:
        return None
    raw = str(raw_url).strip()
    if not raw or len(raw) > MAX_URL_LENGTH or _CONTROL_RE.search(raw):
        return None
    lowered = raw.lstrip().lower()
    if lowered.startswith(("javascript:", "data:", "vbscript:", "file:", "about:", "blob:")):
        return None
    try:
        joined = urljoin(source_url, raw)
        parsed = urlsplit(joined)
        if parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname:
            return None
        if parsed.username is not None or parsed.password is not None or "@" in parsed.netloc:
            return None
        port = parsed.port
        if port is not None and not 1 <= port <= 65535:
            return None
        host = parsed.hostname.encode("idna").decode("ascii").lower().rstrip(".")
        if not host:
            return None
        try:
            literal_host = ipaddress.ip_address(host)
        except ValueError:
            literal_host = None
        if literal_host is not None and not literal_host.is_global:
            return None
        if host in {"localhost", "localhost.localdomain"} or host.endswith((".localhost", ".local")):
            return None
        if parsed.query:
            try:
                query_keys = {key.lower() for key, _value in parse_qsl(parsed.query, keep_blank_values=True)}
            except ValueError:
                return None
            if query_keys.intersection({"url", "uri", "redirect", "next", "target", "dest", "destination", "return"}):
                return None
        if ":" in host:
            netloc_host = f"[{host}]"
        else:
            netloc_host = host
        if port is not None and ((parsed.scheme.lower() == "http" and port != 80) or (parsed.scheme.lower() == "https" and port != 443)):
            netloc = f"{netloc_host}:{port}"
        else:
            netloc = netloc_host
        return urlunsplit((parsed.scheme.lower(), netloc, parsed.path or "", parsed.query, ""))
    except (UnicodeError, ValueError):
        return None


def canonical_url(url: Any, source_url: Optional[str] = None) -> Optional[str]:
    """Public helper returning a safe canonical item URL or ``None``."""

    return _sanitize_item_url(url, source_url or "https://example.com/")


def _resolve_public_addresses(host: str, port: int, resolver: Optional[Callable[..., Any]] = None) -> list[str]:
    try:
        literal = ipaddress.ip_address(host)
    except ValueError:
        literal = None
    if literal is not None:
        if not literal.is_global:
            raise UnsafeURLError("source_host_not_public")
        return [str(literal)]

    resolver_fn = resolver or socket.getaddrinfo
    try:
        infos = _invoke_compatible(resolver_fn, host, port, type=socket.SOCK_STREAM)
    except Exception as exc:
        raise FeedFetchError("dns_error") from exc
    addresses: list[str] = []
    for info in infos or []:
        try:
            sockaddr = info[4] if len(info) > 4 else info
            address = str(sockaddr[0]).split("%", 1)[0]
            parsed_ip = ipaddress.ip_address(address)
        except (IndexError, KeyError, TypeError, ValueError):
            raise FeedFetchError("dns_error")
        if not parsed_ip.is_global:
            raise UnsafeURLError("source_host_not_public")
        text = str(parsed_ip)
        if text not in addresses:
            addresses.append(text)
    if not addresses:
        raise FeedFetchError("dns_error")
    return addresses


def _http_get_pinned(
    url: str,
    headers: Mapping[str, str],
    *,
    resolver: Optional[Callable[..., Any]] = None,
    timeout: float = HTTP_TIMEOUT_SECONDS,
) -> FetchResult:
    canonical, parsed = _validate_source_url(url)
    del canonical
    host = parsed.hostname or ""
    port = parsed.port or 443
    addresses = _resolve_public_addresses(host, port, resolver)
    timeout_value = min(max(float(timeout), 0.1), HTTP_TIMEOUT_SECONDS)
    context = ssl.create_default_context()
    target = parsed.path or "/"
    if parsed.query:
        target += "?" + parsed.query
    if ":" in host:
        host_header = f"[{host}]"
    else:
        host_header = host
    if port != 443:
        host_header = f"{host_header}:{port}"
    request_headers = {
        "Accept": "application/rss+xml, application/atom+xml, application/feed+json, application/json, */*",
        "Accept-Encoding": "identity",
        "User-Agent": "TMT-Atlas-Connector/1.0",
        "Host": host_header,
    }
    request_headers.update({str(k): str(v) for k, v in headers.items()})

    last_error: Optional[BaseException] = None
    for address in addresses:
        connection: Optional[_PinnedHTTPSConnection] = None
        try:
            connection = _PinnedHTTPSConnection(host, address, port, timeout=timeout_value, context=context)
            connection.request("GET", target, headers=request_headers)
            response = connection.getresponse()
            response_headers = {str(k).lower(): str(v).strip() for k, v in response.getheaders()}
            status = int(response.status)
            if status == 304:
                return FetchResult(status=304, headers=response_headers, body=b"")
            if 300 <= status < 400:
                raise FeedFetchError("redirect_not_allowed")
            if status < 200 or status >= 300:
                raise FeedFetchError("http_error")
            content_length = response_headers.get("content-length")
            if content_length:
                try:
                    if int(content_length) < 0 or int(content_length) > MAX_RESPONSE_BYTES:
                        raise FeedFetchError("response_too_large")
                except ValueError as exc:
                    raise FeedFetchError("invalid_response") from exc
            body = response.read(MAX_RESPONSE_BYTES + 1)
            if len(body) > MAX_RESPONSE_BYTES:
                raise FeedFetchError("response_too_large")
            return FetchResult(status=status, headers=response_headers, body=body)
        except (FeedFetchError, UnsafeURLError):
            raise
        except (OSError, socket.timeout, TimeoutError, ssl.SSLError, http.client.HTTPException) as exc:
            last_error = exc
        finally:
            if connection is not None:
                connection.close()
    raise FeedFetchError("network_error") from last_error


def _xml_local_name(tag: Any) -> str:
    text = str(tag)
    return text.rsplit("}", 1)[-1].lower()


def _direct_child_text(node: ET.Element, names: Iterable[str], *, limit: int = MAX_DATE_LENGTH) -> str:
    wanted = {name.lower() for name in names}
    for child in list(node):
        if _xml_local_name(child.tag) in wanted:
            return _clean_text("".join(child.itertext()), limit=limit)
    return ""


def _direct_child(node: ET.Element, name: str) -> Optional[ET.Element]:
    wanted = name.lower()
    for child in list(node):
        if _xml_local_name(child.tag) == wanted:
            return child
    return None


def _xml_entry_url(node: ET.Element, source_url: str, *, atom: bool) -> Optional[str]:
    if atom:
        candidates: list[tuple[str, str]] = []
        for child in list(node):
            if _xml_local_name(child.tag) != "link":
                continue
            href = child.attrib.get("href", "")
            rel = child.attrib.get("rel", "alternate").lower()
            if href:
                candidates.append((rel, href))
        for rel, href in candidates:
            if rel in {"alternate", ""}:
                safe = _sanitize_item_url(href, source_url)
                if safe:
                    return safe
        for _rel, href in candidates:
            safe = _sanitize_item_url(href, source_url)
            if safe:
                return safe
        return None
    link = _direct_child(node, "link")
    if link is not None:
        text = "".join(link.itertext()).strip() or link.attrib.get("href", "")
        safe = _sanitize_item_url(text, source_url)
        if safe:
            return safe
    guid = _direct_child_text(node, {"guid", "id"})
    is_permalink = (link.attrib.get("isPermaLink", "true").lower() if link is not None else "true")
    if guid and is_permalink != "false":
        return _sanitize_item_url(guid, source_url)
    return None


def _parse_xml_feed(body: bytes, source_url: str, feed_format: str) -> tuple[str, list[_ParsedEntry]]:
    if _XML_DTD_RE.search(body):
        raise FeedParseError("xml_declaration_forbidden")
    try:
        root = ET.fromstring(body)
    except (ET.ParseError, ValueError, UnicodeError) as exc:
        raise FeedParseError("xml_invalid") from exc

    local_root = _xml_local_name(root.tag)
    if feed_format == "atom" or local_root == "feed":
        feed_node = root
        source_title = _direct_child_text(feed_node, {"title"}, limit=MAX_TITLE_LENGTH)
        entries = [node for node in feed_node.iter() if _xml_local_name(node.tag) == "entry"]
        parsed_entries = []
        for node in entries[:MAX_ITEMS_PER_FEED]:
            title = _direct_child_text(node, {"title"}, limit=MAX_TITLE_LENGTH)
            date = _direct_child_text(node, {"published", "updated", "date"}) or None
            parsed_entries.append(_ParsedEntry(title=title, date=date, url=_xml_entry_url(node, source_url, atom=True)))
        return source_title, parsed_entries

    channel = next((node for node in root.iter() if _xml_local_name(node.tag) == "channel"), root)
    source_title = _direct_child_text(channel, {"title"}, limit=MAX_TITLE_LENGTH)
    nodes = [node for node in root.iter() if _xml_local_name(node.tag) == "item"]
    parsed_entries = []
    for node in nodes[:MAX_ITEMS_PER_FEED]:
        title = _direct_child_text(node, {"title"}, limit=MAX_TITLE_LENGTH)
        date = _direct_child_text(node, {"pubdate", "published", "updated", "date"}) or None
        parsed_entries.append(_ParsedEntry(title=title, date=date, url=_xml_entry_url(node, source_url, atom=False)))
    return source_title, parsed_entries


def _parse_json_feed(body: bytes, source_url: str) -> tuple[str, list[_ParsedEntry]]:
    try:
        payload = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise FeedParseError("json_invalid") from exc
    if not isinstance(payload, dict) or not isinstance(payload.get("items"), list):
        raise FeedParseError("json_feed_invalid")
    source_title = _clean_text(payload.get("title"), limit=MAX_TITLE_LENGTH)
    parsed_entries: list[_ParsedEntry] = []
    for item in payload["items"][:MAX_ITEMS_PER_FEED]:
        if not isinstance(item, dict):
            continue
        title = _clean_text(item.get("title"), limit=MAX_TITLE_LENGTH)
        date_value = item.get("date_published") or item.get("date_modified")
        date = _clean_date(date_value) or None
        raw_url = item.get("url") or item.get("external_url")
        parsed_entries.append(_ParsedEntry(title=title, date=date, url=_sanitize_item_url(raw_url, source_url)))
    return source_title, parsed_entries


def parse_feed(
    body: bytes | bytearray | memoryview | str,
    feed_url: str,
    feed_format: str = "auto",
    *,
    content_type: Optional[str] = None,
) -> list[dict[str, Any]]:
    """Parse RSS, Atom, or JSON Feed metadata into unreviewed item records.

    The result intentionally excludes descriptions, authors, content, and other
    body text.  ``Collector`` adds persistence and review state around this
    pure parser.
    """

    source_url, _parsed = _validate_source_url(feed_url)
    if isinstance(body, str):
        raw = body.encode("utf-8")
    else:
        raw = bytes(body)
    if len(raw) > MAX_RESPONSE_BYTES:
        raise FeedParseError("response_too_large")
    format_name = (feed_format or "auto").lower().replace("-", "")
    if format_name in {"jsonfeed", "feedjson"}:
        format_name = "json"
    if format_name not in {"auto", "rss", "atom", "json"}:
        raise FeedParseError("feed_format_invalid")
    stripped = raw.lstrip()
    is_json = format_name == "json" or (format_name == "auto" and stripped.startswith(b"{"))
    if is_json:
        source_title, entries = _parse_json_feed(raw, source_url)
    else:
        source_title, entries = _parse_xml_feed(raw, source_url, format_name)
    source_title = source_title or (urlsplit(source_url).hostname or "source")
    output: list[dict[str, Any]] = []
    local_seen: set[str] = set()
    for entry in entries[:MAX_ITEMS_PER_FEED]:
        title = _clean_text(entry.title, limit=MAX_TITLE_LENGTH) or "Untitled"
        date = _clean_date(entry.date) if entry.date else None
        safe_url = _sanitize_item_url(entry.url, source_url)
        canonical = safe_url
        if canonical:
            dedupe_key = "url:" + canonical
        else:
            fingerprint = "\x00".join((title.casefold(), date or ""))
            dedupe_key = "hash:" + hashlib.sha256(fingerprint.encode("utf-8")).hexdigest()
        if dedupe_key in local_seen:
            continue
        local_seen.add(dedupe_key)
        item_id = "item-" + hashlib.sha256(dedupe_key.encode("utf-8")).hexdigest()[:24]
        output.append(
            {
                "id": item_id,
                "title": title,
                "date": date,
                "url": safe_url,
                "source": {"title": source_title, "url": source_url},
                "source_title": source_title,
                "source_url": source_url,
                "status": "pending",
                "facts_approved": False,
            }
        )
    return output


class Collector:
    """Persistent, review-first collector for explicitly authorized feeds.

    Parameters beyond ``state_dir`` are optional dependency-injection hooks for
    offline tests and local applications.  The public contract is:

    * ``config()`` -> ``feeds``, ``enabled``, ``interval_minutes``,
      ``last_run``, ``next_run``, and ``last_error``;
    * ``configure(payload)`` validates and persists configuration;
    * ``run_once()`` fetches at most five enabled feeds and returns newly staged
      items;
    * ``inbox()`` returns staged items, including their review status;
    * ``review(item_id, status)`` accepts only ``accepted`` or ``rejected``;
    * ``tick()`` runs once only when the persisted schedule is due.
    """

    def __init__(
        self,
        state_dir: str | os.PathLike[str],
        *,
        fetcher: Optional[Callable[..., Any]] = None,
        resolver: Optional[Callable[..., Any]] = None,
        clock: Optional[Callable[[], Any]] = None,
    ) -> None:
        self.state_dir = Path(state_dir).expanduser()
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.state_path = self.state_dir / STATE_FILENAME
        self.lock_path = self.state_dir / LOCK_FILENAME
        self._fetcher = fetcher
        self._resolver = resolver
        self._clock = clock or _utc_now
        self._thread_lock = threading.RLock()
        self._run_lock = threading.Lock()
        self._ensure_state()

    def _now(self) -> datetime:
        value = self._clock()
        result = _coerce_datetime(value)
        return result or _utc_now()

    @contextmanager
    def _locked(self, *, write: bool) -> Any:
        with self._thread_lock:
            handle = self.lock_path.open("a+", encoding="utf-8")
            try:
                if fcntl is not None:
                    operation = fcntl.LOCK_EX if write else fcntl.LOCK_SH
                    fcntl.flock(handle.fileno(), operation)
                yield
            finally:
                if fcntl is not None:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
                handle.close()

    def _read_state_unlocked(self) -> dict[str, Any]:
        try:
            with self.state_path.open("r", encoding="utf-8") as handle:
                raw = json.load(handle)
        except (FileNotFoundError, OSError, json.JSONDecodeError, TypeError, ValueError):
            raw = {}
        state = _default_state()
        if isinstance(raw, dict):
            state.update(raw)
        if not isinstance(state.get("feeds"), list):
            state["feeds"] = []
        if not isinstance(state.get("inbox"), list):
            state["inbox"] = []
        if not isinstance(state.get("seen_keys"), list):
            state["seen_keys"] = []
        if not isinstance(state.get("interval_minutes"), int) or isinstance(state.get("interval_minutes"), bool):
            state["interval_minutes"] = DEFAULT_INTERVAL_MINUTES
        if state["interval_minutes"] < MIN_INTERVAL_MINUTES:
            state["interval_minutes"] = MIN_INTERVAL_MINUTES
        state["enabled"] = bool(state.get("enabled"))
        state["authorized"] = bool(state.get("authorized"))
        return state

    def _write_state_unlocked(self, state: Mapping[str, Any]) -> None:
        payload = json.dumps(state, ensure_ascii=False, indent=2, sort_keys=True).encode("utf-8")
        fd, temporary_name = tempfile.mkstemp(prefix=".connectors-state-", suffix=".tmp", dir=str(self.state_dir))
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary_name, self.state_path)
            try:
                directory_fd = os.open(str(self.state_dir), os.O_RDONLY)
                try:
                    os.fsync(directory_fd)
                finally:
                    os.close(directory_fd)
            except OSError:
                pass
        finally:
            try:
                os.unlink(temporary_name)
            except FileNotFoundError:
                pass

    def _ensure_state(self) -> None:
        with self._locked(write=True):
            if not self.state_path.exists():
                self._write_state_unlocked(_default_state())

    def _state_copy(self) -> dict[str, Any]:
        with self._locked(write=False):
            return _copy_json(self._read_state_unlocked())

    def config(self) -> dict[str, Any]:
        """Return the public scheduler configuration, excluding inbox internals."""

        state = self._state_copy()
        return {
            "feeds": state.get("feeds", []),
            "enabled": bool(state.get("enabled")),
            "interval_minutes": int(state.get("interval_minutes", DEFAULT_INTERVAL_MINUTES)),
            "last_run": state.get("last_run"),
            "next_run": state.get("next_run"),
            "last_error": state.get("last_error"),
        }

    @staticmethod
    def _normalise_feed_entry(entry: Any, global_authorized: bool) -> dict[str, Any]:
        if isinstance(entry, str):
            # A bare URL has no per-feed authorization marker.  Requiring a
            # mapping here prevents a global switch from silently authorizing
            # every URL supplied by a caller.
            raise ConfigurationError("feed_authorization_required")
        elif isinstance(entry, Mapping):
            raw = dict(entry)
        else:
            raise ConfigurationError("feed_invalid")
        if raw.get("authorized") is not True:
            raise ConfigurationError("feed_authorization_required")
        url, _parsed = _validate_source_url(raw.get("url"))
        feed_format = str(raw.get("format", raw.get("kind", "auto"))).lower().replace("-", "")
        if feed_format in {"jsonfeed", "feedjson"}:
            feed_format = "json"
        if feed_format not in {"auto", "rss", "atom", "json"}:
            raise ConfigurationError("feed_format_invalid")
        feed_id = "feed-" + hashlib.sha256(url.encode("utf-8")).hexdigest()[:16]
        title = _clean_text(raw.get("title"), limit=MAX_TITLE_LENGTH)
        enabled = raw.get("enabled", True)
        if not isinstance(enabled, bool):
            raise ConfigurationError("feed_enabled_invalid")
        return {
            "id": feed_id,
            "url": url,
            "format": feed_format,
            "title": title,
            "enabled": enabled,
            "authorized": True,
        }

    def configure(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        """Validate and atomically save an explicit feed/scheduler configuration."""

        if not isinstance(payload, Mapping):
            raise ConfigurationError("configuration_invalid")
        enabled = payload.get("enabled", False)
        if not isinstance(enabled, bool):
            raise ConfigurationError("enabled_must_be_boolean")
        global_authorized = payload.get("authorized") is True
        raw_feeds = payload.get("feeds", payload.get("urls", []))
        if not isinstance(raw_feeds, list):
            raise ConfigurationError("feeds_must_be_list")
        if enabled and not global_authorized:
            raise ConfigurationError("authorization_required")
        if enabled and not raw_feeds:
            raise ConfigurationError("explicit_feed_urls_required")
        feeds = [self._normalise_feed_entry(entry, global_authorized) for entry in raw_feeds]

        old_state = self._state_copy()
        old_by_id = {feed.get("id"): feed for feed in old_state.get("feeds", []) if isinstance(feed, dict)}
        for feed in feeds:
            previous = old_by_id.get(feed["id"], {})
            for key in ("etag", "last_modified"):
                if previous.get(key):
                    feed[key] = previous[key]

        interval_value = payload.get("interval_minutes", old_state.get("interval_minutes", DEFAULT_INTERVAL_MINUTES))
        if isinstance(interval_value, bool):
            raise ConfigurationError("interval_invalid")
        try:
            interval = int(interval_value)
        except (TypeError, ValueError) as exc:
            raise ConfigurationError("interval_invalid") from exc
        if interval < MIN_INTERVAL_MINUTES:
            raise ConfigurationError("interval_below_minimum")

        now = self._now()
        state = old_state
        state["feeds"] = feeds
        state["enabled"] = enabled
        state["authorized"] = global_authorized
        state["interval_minutes"] = interval
        state["last_error"] = None
        if enabled and global_authorized and feeds:
            # Enabling/reconfiguring is immediately due.  ``tick`` advances it
            # by one interval after one run, rather than replaying missed runs.
            state["next_run"] = _iso_datetime(now)
        else:
            state["next_run"] = None
        with self._locked(write=True):
            self._write_state_unlocked(state)
        return self.config()

    def _call_fetcher(self, url: str, headers: Mapping[str, str]) -> FetchResult:
        if self._fetcher is None:
            return _http_get_pinned(url, headers, resolver=self._resolver, timeout=HTTP_TIMEOUT_SECONDS)
        value = _invoke_compatible(self._fetcher, url, dict(headers))
        return _coerce_fetch_result(value)

    def _fetch_url(self, url: str, headers: Mapping[str, str]) -> FetchResult:
        """Fetch one URL; kept as a seam for deterministic application tests."""

        return self._call_fetcher(url, headers)

    @staticmethod
    def _item_from_parsed(item: Mapping[str, Any], feed: Mapping[str, Any], collected_at: str) -> dict[str, Any]:
        source_title = _clean_text(feed.get("title"), limit=MAX_TITLE_LENGTH)
        if not source_title:
            source_title = _clean_text(item.get("source_title"), limit=MAX_TITLE_LENGTH) or (urlsplit(str(feed["url"])).hostname or "source")
        title = _clean_text(item.get("title"), limit=MAX_TITLE_LENGTH) or "Untitled"
        date = _clean_date(item.get("date")) or None
        safe_url = _sanitize_item_url(item.get("url"), str(feed["url"]))
        if safe_url:
            dedupe_key = "url:" + safe_url
        else:
            fingerprint = "\x00".join((title.casefold(), date or ""))
            dedupe_key = "hash:" + hashlib.sha256(fingerprint.encode("utf-8")).hexdigest()
        item_id = "item-" + hashlib.sha256(dedupe_key.encode("utf-8")).hexdigest()[:24]
        return {
            "id": item_id,
            "title": title,
            "date": date,
            "url": safe_url,
            "source": {"title": source_title, "url": str(feed["url"])},
            "source_title": source_title,
            "source_url": str(feed["url"]),
            "feed_id": feed.get("id"),
            "status": "pending",
            "facts_approved": False,
            "collected_at": collected_at,
            "_dedupe_key": dedupe_key,
        }

    def _collect_feed(self, feed: Mapping[str, Any]) -> tuple[list[dict[str, Any]], dict[str, str], int]:
        request_headers: dict[str, str] = {}
        if feed.get("etag"):
            request_headers["If-None-Match"] = str(feed["etag"])
        if feed.get("last_modified"):
            request_headers["If-Modified-Since"] = str(feed["last_modified"])
        result = self._fetch_url(str(feed["url"]), request_headers)
        result = _coerce_fetch_result(result)
        if result.status == 304:
            return [], result.normalized_headers(), result.status
        if 300 <= result.status < 400:
            raise FeedFetchError("redirect_not_allowed")
        if result.status < 200 or result.status >= 300:
            raise FeedFetchError("http_error")
        headers = result.normalized_headers()
        content_type = headers.get("content-type")
        parsed = parse_feed(result.body, str(feed["url"]), str(feed.get("format", "auto")), content_type=content_type)
        return parsed, headers, result.status

    def run_once(self) -> list[dict[str, Any]]:
        """Fetch at most five enabled feeds and return newly staged metadata."""

        return self._run_once_scoped(None)

    @staticmethod
    def _normalise_scope(source_scope: Any) -> Optional[set[str]]:
        if source_scope is None:
            return None
        if not isinstance(source_scope, (list, tuple, set)) or isinstance(source_scope, (str, bytes)):
            raise ConfigurationError("source_scope_invalid")
        if not source_scope:
            raise ConfigurationError("source_scope_empty")
        normalized: set[str] = set()
        for value in source_scope:
            try:
                url, _parsed = _validate_source_url(value)
            except UnsafeURLError:
                raise ConfigurationError("source_scope_invalid") from None
            normalized.add(url)
        if not normalized:
            raise ConfigurationError("source_scope_empty")
        return normalized

    def run_scoped(self, source_scope: Any) -> list[dict[str, Any]]:
        """Fetch only explicitly scoped, globally authorized configured feeds.

        The scope is an allow-list, not an authorization mechanism.  Every
        selected URL must already exist in the persisted configuration and be
        globally enabled and per-feed authorized.  Unknown or unconfigured
        scope entries fail closed before any network request is made.
        """

        normalized_scope = self._normalise_scope(source_scope)
        if normalized_scope is None:
            raise ConfigurationError("source_scope_required")
        return self._run_once_scoped(normalized_scope)

    def _run_once_scoped(self, normalized_scope: Optional[set[str]]) -> list[dict[str, Any]]:
        with self._run_lock:
            snapshot = self._state_copy()
            if not snapshot.get("enabled") or not snapshot.get("authorized"):
                if normalized_scope is not None:
                    raise ConfigurationError("collector_not_authorized")
                return []
            configured_feeds = [
                feed
                for feed in snapshot.get("feeds", [])
                if isinstance(feed, dict)
                and feed.get("enabled", True)
                and feed.get("authorized") is True
            ]
            if normalized_scope is not None:
                configured_urls = {str(feed.get("url")) for feed in configured_feeds}
                if not normalized_scope.issubset(configured_urls):
                    raise ConfigurationError("source_scope_unconfigured")
                selected = [feed for feed in configured_feeds if str(feed.get("url")) in normalized_scope]
                if not selected:
                    raise ConfigurationError("source_scope_unconfigured")
            else:
                selected = configured_feeds
            selected = selected[:MAX_FEEDS_PER_RUN]
            if not selected:
                return []

            collected: list[tuple[dict[str, Any], list[dict[str, Any]], dict[str, str], int]] = []
            failures = False
            for feed in selected:
                try:
                    items, response_headers, status = self._collect_feed(feed)
                    collected.append((feed, items, response_headers, status))
                except Exception:
                    # Persist only the category-level error below.  In
                    # particular, never write exception text containing URLs,
                    # headers, credentials, or provider response bodies.
                    failures = True

            now = self._now()
            now_text = _iso_datetime(now)
            staged: list[dict[str, Any]] = []
            with self._locked(write=True):
                state = self._read_state_unlocked()
                seen = {str(value) for value in state.get("seen_keys", [])}
                existing_ids = {str(item.get("id")) for item in state.get("inbox", []) if isinstance(item, dict)}
                feed_by_id = {str(feed.get("id")): feed for feed in state.get("feeds", []) if isinstance(feed, dict)}
                for feed_snapshot, raw_items, response_headers, status in collected:
                    current_feed = feed_by_id.get(str(feed_snapshot.get("id")))
                    if current_feed is None:
                        continue
                    if response_headers.get("etag"):
                        current_feed["etag"] = response_headers["etag"]
                    if response_headers.get("last-modified"):
                        current_feed["last_modified"] = response_headers["last-modified"]
                    if status == 304:
                        continue
                    for raw_item in raw_items[:MAX_ITEMS_PER_FEED]:
                        item = self._item_from_parsed(raw_item, current_feed, now_text)
                        dedupe_key = item.pop("_dedupe_key")
                        if dedupe_key in seen or item["id"] in existing_ids:
                            continue
                        seen.add(dedupe_key)
                        existing_ids.add(item["id"])
                        state["inbox"].append(item)
                        staged.append(_copy_json(item))
                state["seen_keys"] = sorted(seen)
                state["last_run"] = now_text
                state["next_run"] = _iso_datetime(now + timedelta(minutes=int(state.get("interval_minutes", DEFAULT_INTERVAL_MINUTES))))
                state["last_error"] = "collection_error" if failures else None
                self._write_state_unlocked(state)
            return staged

    def tick(self) -> list[dict[str, Any]]:
        """Run one due collection cycle; never replay a missed interval backlog."""

        state = self._state_copy()
        if not state.get("enabled") or not state.get("authorized"):
            return []
        due_at = _coerce_datetime(state.get("next_run"))
        if due_at is not None and self._now() < due_at:
            return []
        return self.run_once()

    def inbox(self, status: Optional[str] = None) -> list[dict[str, Any]]:
        """Return staged items, optionally filtered to a review status."""

        state = self._state_copy()
        items = [item for item in state.get("inbox", []) if isinstance(item, dict)]
        if status is not None:
            if status not in {"pending", "accepted", "rejected"}:
                raise ValueError("status_invalid")
            items = [item for item in items if item.get("status") == status]
        return _copy_json(items)

    def review(self, item_id: str, status: str) -> dict[str, Any]:
        """Set review status without ever approving the underlying facts."""

        if status not in {"accepted", "rejected"}:
            raise ValueError("status_invalid")
        if not isinstance(item_id, str) or not item_id:
            raise ValueError("item_id_invalid")
        with self._locked(write=True):
            state = self._read_state_unlocked()
            for item in state.get("inbox", []):
                if isinstance(item, dict) and item.get("id") == item_id:
                    item["status"] = status
                    item["reviewed_at"] = _iso_datetime(self._now())
                    item["facts_approved"] = False
                    self._write_state_unlocked(state)
                    return _copy_json(item)
        raise KeyError("item_not_found")


def normalize_quote(payload: Mapping[str, Any]) -> dict[str, Any]:
    """Validate a manually supplied market snapshot.

    This adapter performs no network request and does not imply live data.  A
    caller must supply finite positive ``price`` and ``shares`` plus explicit
    currency, observation time, and source URL metadata.
    """

    if not isinstance(payload, Mapping):
        raise ValueError("quote_payload_invalid")
    source = payload.get("quote") if isinstance(payload.get("quote"), Mapping) else payload
    price = source.get("price")
    shares = source.get("shares")
    if not _is_finite_positive(price) or not _is_finite_positive(shares):
        raise ValueError("quote_price_and_shares_must_be_positive_finite")
    currency = source.get("currency")
    if not isinstance(currency, str) or not re.fullmatch(r"[A-Za-z]{3}", currency.strip()):
        raise ValueError("quote_currency_invalid")
    observed_at = source.get("observed_at")
    if not isinstance(observed_at, str) or not observed_at.strip() or len(observed_at.strip()) > MAX_DATE_LENGTH or _CONTROL_RE.search(observed_at):
        raise ValueError("quote_observed_at_invalid")
    try:
        source_url, _parsed = _validate_source_url(source.get("source_url"))
    except UnsafeURLError as exc:
        raise ValueError("quote_source_url_invalid") from exc
    result: dict[str, Any] = {
        "price": float(price),
        "shares": float(shares),
        "currency": currency.strip().upper(),
        "observed_at": observed_at.strip(),
        "source_url": source_url,
        "facts_approved": False,
        "data_status": "manual_snapshot",
    }
    symbol = source.get("symbol", source.get("ticker"))
    if symbol is not None:
        symbol_text = str(symbol).strip()
        if symbol_text and _SYMBOL_RE.fullmatch(symbol_text):
            result["symbol"] = symbol_text
    return result


_ALPHA_LOCK = threading.Lock()
_ALPHA_CALL_TIMES: list[float] = []
_ALPHA_MAX_CALLS_PER_MINUTE = 5


def fetch_alpha_vantage_quote(
    symbol: str,
    *,
    api_key: Optional[str] = None,
    fetcher: Optional[Callable[..., Any]] = None,
    now: Optional[Callable[[], float]] = None,
) -> dict[str, Any]:
    """Optionally fetch a delayed Alpha Vantage global quote.

    The function is opt-in, requires ``ALPHA_VANTAGE_API_KEY`` or
    ``TMT_ALPHA_VANTAGE_API_KEY`` (unless a key is passed explicitly), and has
    a small process-local quota guard.  It is not called by ``Collector``.
    """

    if not isinstance(symbol, str) or not _SYMBOL_RE.fullmatch(symbol.strip()):
        raise ValueError("symbol_invalid")
    symbol = symbol.strip()
    key = api_key or os.environ.get("ALPHA_VANTAGE_API_KEY") or os.environ.get("TMT_ALPHA_VANTAGE_API_KEY")
    if not key:
        raise ConfigurationError("alpha_vantage_not_configured")
    clock = now or time.monotonic
    current = float(clock())
    with _ALPHA_LOCK:
        global _ALPHA_CALL_TIMES
        _ALPHA_CALL_TIMES = [stamp for stamp in _ALPHA_CALL_TIMES if current - stamp < 60.0]
        if len(_ALPHA_CALL_TIMES) >= _ALPHA_MAX_CALLS_PER_MINUTE:
            raise QuotaError("provider_quota_guard")
        _ALPHA_CALL_TIMES.append(current)

    request_url = "https://www.alphavantage.co/query?" + urlencode({"function": "GLOBAL_QUOTE", "symbol": symbol, "apikey": key})
    if fetcher is None:
        result = _http_get_pinned(request_url, {"Accept": "application/json"}, timeout=HTTP_TIMEOUT_SECONDS)
    else:
        raw_result = _invoke_compatible(fetcher, request_url, {})
        result = _coerce_fetch_result(raw_result)
    if result.status == 304 or result.status < 200 or result.status >= 300:
        raise FeedFetchError("provider_http_error")
    try:
        payload = json.loads(result.body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise FeedParseError("provider_response_invalid") from exc
    if not isinstance(payload, dict) or not isinstance(payload.get("Global Quote"), dict):
        raise FeedFetchError("provider_quota_or_empty")
    quote = payload["Global Quote"]
    try:
        price = float(quote.get("05. price"))
        shares = float(quote.get("06. volume"))
    except (TypeError, ValueError):
        raise FeedParseError("provider_quote_invalid")
    observed_day = str(quote.get("07. latest trading day") or "").strip()
    if not observed_day:
        observed_day = _iso_datetime(_utc_now())[:10]
    normalized = normalize_quote(
        {
            "symbol": symbol,
            "price": price,
            "shares": shares,
            "currency": "USD",
            "observed_at": observed_day + "T00:00:00Z",
            "source_url": "https://www.alphavantage.co/query?function=GLOBAL_QUOTE&symbol=" + quote_url_symbol(symbol),
        }
    )
    normalized.update({"provider": "alpha_vantage", "delayed": True})
    return normalized


def quote_url_symbol(symbol: str) -> str:
    """Quote a symbol for a provider URL without exposing an API key."""

    return quote(symbol, safe="")


__all__ = [
    "Collector",
    "ConfigurationError",
    "ConnectorError",
    "FeedFetchError",
    "FeedParseError",
    "FetchResult",
    "MAX_FEEDS_PER_RUN",
    "MAX_ITEMS_PER_FEED",
    "MAX_RESPONSE_BYTES",
    "MIN_INTERVAL_MINUTES",
    "QuotaError",
    "UnsafeURLError",
    "canonical_url",
    "fetch_alpha_vantage_quote",
    "normalize_quote",
    "parse_feed",
]
