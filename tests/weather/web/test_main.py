"""Tests for ``climate.weather.web.__main__`` — the web service entry point.

Pins one thing: the web service reads from the store, it does not own index
creation, so its store factory must call ``mongo.build_store`` with
``ensure_indexes=False``. ``climate.weather.mongo`` is lazy-imported inside
``_build_store`` (``tests/test_stdlib_only.py``), so it is monkeypatched
there rather than imported at module scope by ``__main__``.

No socket is opened anywhere here: ``mongo.build_store`` itself is replaced
with a recorder.
"""

from __future__ import annotations

from climate.weather.web import __main__ as web_main


def test_build_store_asks_mongo_not_to_ensure_indexes(monkeypatch):
    """The web service is read-only with respect to indexes: the tracker is
    the sole owner of ensuring them (issue behind dashboard-data-efficiency
    t2)."""
    calls: list = []

    class _FakeStore:
        pass

    def _recording_build_store(*args, **kwargs):
        calls.append((args, kwargs))
        return _FakeStore()

    from climate.weather import mongo

    monkeypatch.setattr(mongo, "build_store", _recording_build_store)

    store = web_main._build_store()

    assert isinstance(store, _FakeStore)
    assert calls == [((), {"ensure_indexes": False})]
