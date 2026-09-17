"""HTTP fetch helper: per-call timeout, custom headers, and secret redaction.

Wraps :mod:`urllib.request` behind a small surface that:

* never raises on network failure — every outcome, success or failure, is a
  :class:`FetchResult`;
* only ever dials ``https://`` URLs (schemes are checked before any socket
  is touched, which is also what keeps bandit's B310 urlopen check quiet);
* takes an injectable ``opener`` so tests never open a real socket;
* redacts API keys/tokens from URLs, headers, and exception text so a secret
  never ends up in a repr or a log line.
"""

from __future__ import annotations

import re
import urllib.error
import urllib.request
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Protocol
from urllib.parse import urlparse

DEFAULT_TIMEOUT = 10.0

_ALLOWED_SCHEMES = frozenset({"https"})

# Query-string / free-text keys that carry secrets (OpenWeather uses "appid").
_SENSITIVE_KEYS = (
    "appid",
    "api_key",
    "apikey",
    "access_token",
    "token",
    "secret",
    "password",
    "key",
)

# Header names whose *value* is always a secret, regardless of content.
_SENSITIVE_HEADER_NAMES = frozenset(
    {"authorization", "x-api-key", "api-key", "x-auth-token", "proxy-authorization"}
)

_REDACTED = "REDACTED"

_QUERY_PARAM_RE = re.compile(
    r"(?i)\b(" + "|".join(_SENSITIVE_KEYS) + r")=([^&\s'\"]+)",
)


def redact(text: str | None) -> str | None:
    """Redact API keys/tokens found in a URL's query string or free text.

    Safe to call on a full URL, a header value, or exception text — it only
    replaces the values of known secret-bearing keys (``appid``, ``token``,
    ``api_key``, ...), leaving everything else untouched.

    Sibling: :func:`climate.weather.store.redact_url` is the *storage*
    layer's redactor. Both exist on purpose. This one is a regex over
    arbitrary text and never fails on unparseable input, which is what a
    log line or an exception message needs; ``store.redact_url`` parses the
    URL properly (userinfo, caller-supplied secret literals) because its
    output is persisted as ``FetchRecord.endpoint`` and checked for leaks.
    Use this one for anything logged, that one for anything stored.
    """
    if not text:
        return text
    return _QUERY_PARAM_RE.sub(lambda m: f"{m.group(1)}={_REDACTED}", text)


def redact_headers(headers: Mapping[str, str]) -> dict[str, str]:
    """Return a copy of ``headers`` with secret header values redacted."""
    redacted: dict[str, str] = {}
    for name, value in headers.items():
        if name.lower() in _SENSITIVE_HEADER_NAMES:
            redacted[name] = _REDACTED
        else:
            redacted[name] = redact(value) or ""
    return redacted


class Opener(Protocol):
    """Minimal opener interface — real code uses urllib, tests inject a fake."""

    def open(self, request: urllib.request.Request, timeout: float) -> object: ...


class _UrllibOpener:
    """Default opener: delegates to urllib.request.urlopen.

    The scheme is validated in :func:`fetch` before this is ever reached, so
    only ``https://`` URLs reach ``urlopen`` here.
    """

    def open(self, request: urllib.request.Request, timeout: float) -> object:
        return urllib.request.urlopen(request, timeout=timeout)  # nosec B310


_DEFAULT_OPENER = _UrllibOpener()


@dataclass
class FetchResult:
    """Outcome of a single GET — success or failure, always this type."""

    url: str
    status: int | None = None
    headers: dict[str, str] = field(default_factory=dict)
    body: bytes = b""
    error: str | None = None

    @property
    def ok(self) -> bool:
        """True when the request completed with no error and a 2xx status."""
        return self.error is None and self.status is not None and 200 <= self.status < 300

    def __repr__(self) -> str:
        safe_url = redact(self.url)
        safe_headers = redact_headers(self.headers)
        safe_error = redact(self.error)
        return (
            f"{self.__class__.__name__}(url={safe_url!r}, status={self.status!r}, "
            f"headers={safe_headers!r}, body=<{len(self.body)} bytes>, "
            f"error={safe_error!r})"
        )


def fetch(
    url: str,
    *,
    timeout: float = DEFAULT_TIMEOUT,
    headers: Mapping[str, str] | None = None,
    if_modified_since: str | None = None,
    opener: Opener | None = None,
) -> FetchResult:
    """Perform a GET request with a per-call timeout.

    Always returns a :class:`FetchResult` — network errors, HTTP error
    statuses, and non-https URLs are all reported on the result rather than
    raised.
    """
    scheme = urlparse(url).scheme.lower()
    if scheme not in _ALLOWED_SCHEMES:
        return FetchResult(
            url=url,
            error=f"unsupported scheme {scheme or '<none>'!r}: only https is allowed",
        )

    request_headers = dict(headers or {})
    if if_modified_since:
        request_headers["If-Modified-Since"] = if_modified_since

    request = urllib.request.Request(url, headers=request_headers, method="GET")
    active_opener: Opener = opener if opener is not None else _DEFAULT_OPENER

    try:
        with active_opener.open(request, timeout=timeout) as response:  # type: ignore[union-attr]
            body = response.read()
            status = getattr(response, "status", None)
            if status is None and hasattr(response, "getcode"):
                status = response.getcode()
            resp_headers = dict(response.headers.items()) if hasattr(response, "headers") else {}
            return FetchResult(url=url, status=status, headers=resp_headers, body=body)
    except urllib.error.HTTPError as exc:
        body = exc.read() if hasattr(exc, "read") else b""
        resp_headers = dict(exc.headers.items()) if getattr(exc, "headers", None) else {}
        return FetchResult(
            url=url,
            status=exc.code,
            headers=resp_headers,
            body=body,
            error=redact(str(exc)),
        )
    except urllib.error.URLError as exc:
        return FetchResult(url=url, error=redact(str(exc)))
    except (OSError, ValueError) as exc:
        return FetchResult(url=url, error=redact(str(exc)))
