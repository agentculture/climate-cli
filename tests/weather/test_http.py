"""Tests for climate.weather.http — fake opener only, never opens a real socket."""

from __future__ import annotations

import urllib.error
import urllib.request

import pytest

from climate.weather.http import FetchResult, fetch, redact, redact_headers

# A fake key, assembled at import time: a 32-character literal here trips
# GitHub push protection's "Openweather API Key" pattern even though it is a
# placeholder. Never paste anything key-shaped into a test.
OPENWEATHER_APPID = "-".join(["fake", "appid", "for", "redaction", "tests"])
OPENWEATHER_URL = (
    "https://api.openweathermap.org/data/2.5/weather" f"?q=London&appid={OPENWEATHER_APPID}"
)


class _FakeResponse:
    """Mimics the context-manager object returned by urllib openers."""

    def __init__(self, status: int, headers: dict[str, str], body: bytes) -> None:
        self.status = status
        self.headers = headers
        self._body = body

    def __enter__(self) -> "_FakeResponse":
        return self

    def __exit__(self, *exc_info: object) -> None:
        return None

    def read(self) -> bytes:
        return self._body

    def getcode(self) -> int:
        return self.status


class _FakeOpener:
    """Records the last request/timeout it was called with; never touches a socket."""

    def __init__(self, response: object | None = None, raises: Exception | None = None) -> None:
        self._response = response
        self._raises = raises
        self.last_request: urllib.request.Request | None = None
        self.last_timeout: float | None = None

    def open(self, request: urllib.request.Request, timeout: float) -> object:
        self.last_request = request
        self.last_timeout = timeout
        if self._raises is not None:
            raise self._raises
        assert self._response is not None
        return self._response


# --- fetch: success ---------------------------------------------------------


def test_fetch_returns_status_headers_and_exact_body_bytes() -> None:
    body = b'{"temp": 21.5}'
    response = _FakeResponse(200, {"Content-Type": "application/json"}, body)
    opener = _FakeOpener(response=response)

    result = fetch("https://example.com/data", opener=opener)

    assert isinstance(result, FetchResult)
    assert result.status == 200
    assert result.headers["Content-Type"] == "application/json"
    assert result.body == body
    assert result.error is None
    assert result.ok is True


def test_fetch_passes_per_call_timeout_to_opener() -> None:
    opener = _FakeOpener(response=_FakeResponse(200, {}, b""))

    fetch("https://example.com/data", timeout=3.5, opener=opener)

    assert opener.last_timeout == 3.5


def test_fetch_sends_custom_headers() -> None:
    opener = _FakeOpener(response=_FakeResponse(200, {}, b""))

    fetch("https://example.com/data", headers={"X-Custom": "value"}, opener=opener)

    assert opener.last_request is not None
    assert opener.last_request.get_header("X-custom") == "value"


def test_fetch_sends_if_modified_since_header() -> None:
    opener = _FakeOpener(response=_FakeResponse(304, {}, b""))

    fetch(
        "https://example.com/data",
        if_modified_since="Wed, 21 Oct 2015 07:28:00 GMT",
        opener=opener,
    )

    assert opener.last_request is not None
    assert opener.last_request.get_header("If-modified-since") == "Wed, 21 Oct 2015 07:28:00 GMT"


def test_fetch_uses_get_method() -> None:
    opener = _FakeOpener(response=_FakeResponse(200, {}, b""))

    fetch("https://example.com/data", opener=opener)

    assert opener.last_request is not None
    assert opener.last_request.get_method() == "GET"


# --- fetch: errors become result objects, never uncaught exceptions --------


def test_fetch_url_error_becomes_result_not_exception() -> None:
    opener = _FakeOpener(raises=urllib.error.URLError("timed out"))

    result = fetch("https://example.com/data", opener=opener)

    assert result.error is not None
    assert result.status is None
    assert result.ok is False


def test_fetch_http_error_becomes_result_with_status() -> None:
    http_error = urllib.error.HTTPError(
        "https://example.com/data",
        404,
        "Not Found",
        {"Content-Type": "text/plain"},
        None,
    )
    opener = _FakeOpener(raises=http_error)

    result = fetch("https://example.com/data", opener=opener)

    assert result.status == 404
    assert result.error is not None
    assert result.ok is False


def test_fetch_unexpected_os_error_becomes_result_not_exception() -> None:
    opener = _FakeOpener(raises=OSError("boom"))

    result = fetch("https://example.com/data", opener=opener)

    assert result.error is not None
    assert result.ok is False


def test_fetch_rejects_non_https_scheme_without_calling_opener() -> None:
    opener = _FakeOpener(response=_FakeResponse(200, {}, b""))

    result = fetch("http://example.com/data", opener=opener)

    assert result.error is not None
    assert result.status is None
    assert opener.last_request is None  # no socket-touching attempt was made


def test_fetch_rejects_non_http_scheme_entirely() -> None:
    opener = _FakeOpener(response=_FakeResponse(200, {}, b""))

    result = fetch("ftp://example.com/data", opener=opener)

    assert result.error is not None
    assert opener.last_request is None


# --- redaction ---------------------------------------------------------------


def test_redact_removes_openweather_appid_from_url() -> None:
    redacted = redact(OPENWEATHER_URL)

    assert OPENWEATHER_APPID not in redacted
    assert "appid=" in redacted


def test_redact_headers_removes_authorization_value() -> None:
    headers = {"Authorization": "Bearer super-secret-token", "Accept": "application/json"}

    redacted = redact_headers(headers)

    assert "super-secret-token" not in str(redacted)
    assert redacted["Accept"] == "application/json"


def test_redact_removes_secret_from_exception_text() -> None:
    text = f"failed to fetch {OPENWEATHER_URL}: connection reset"

    redacted = redact(text)

    assert OPENWEATHER_APPID not in redacted


def test_fetch_result_repr_never_leaks_appid() -> None:
    opener = _FakeOpener(response=_FakeResponse(200, {}, b"{}"))

    result = fetch(OPENWEATHER_URL, opener=opener)

    assert OPENWEATHER_APPID not in repr(result)


def test_fetch_result_repr_never_leaks_appid_on_error() -> None:
    opener = _FakeOpener(raises=urllib.error.URLError(f"bad url {OPENWEATHER_URL}"))

    result = fetch(OPENWEATHER_URL, opener=opener)

    assert OPENWEATHER_APPID not in repr(result)


def test_redacted_url_never_appears_in_log_line() -> None:
    opener = _FakeOpener(response=_FakeResponse(200, {}, b"{}"))

    result = fetch(OPENWEATHER_URL, opener=opener)
    log_line = f"climate.weather.http: fetched {redact(result.url)} -> {result.status}"

    assert OPENWEATHER_APPID not in log_line


def test_redact_is_idempotent_on_clean_text() -> None:
    clean = "https://example.com/data?q=London"

    assert redact(clean) == clean


@pytest.mark.parametrize("empty", ["", None])
def test_redact_handles_empty_input(empty: str | None) -> None:
    assert redact(empty) == empty
