import json
import time

import pytest

from sibyl import tickers


def _cfg(tmp_path):
    from sibyl.config import Config, SecConfig, UniverseConfig, _resolve_paths
    paths = _resolve_paths(tmp_path, None, None)
    return Config(
        paths=paths,
        sec=SecConfig(user_agent="t", rate_limit_per_sec=1),
        universe=UniverseConfig(form_types=[], include_amendments=False, history_start="2016"),
        download_gzip=True,
    )


def _write_ticker_file(cfg, mapping: dict[str, int]):
    """Write a minimal SEC-style company_tickers.json fixture."""
    payload = {
        str(i): {"cik_str": cik, "ticker": tk, "title": f"Co {tk}"}
        for i, (tk, cik) in enumerate(mapping.items())
    }
    cfg.paths.company_tickers.parent.mkdir(parents=True, exist_ok=True)
    cfg.paths.company_tickers.write_text(json.dumps(payload))


@pytest.fixture(autouse=True)
def _clear_cache():
    """Prevent test cross-pollution from the module-level _CACHE."""
    tickers._CACHE.clear()
    yield
    tickers._CACHE.clear()


# --- normalize ---------------------------------------------------------------

def test_normalize_uppercases():
    assert tickers.normalize("aapl") == "AAPL"


def test_normalize_dotted_share_class_becomes_dashed():
    assert tickers.normalize("BRK.B") == "BRK-B"
    assert tickers.normalize("brk.b") == "BRK-B"
    assert tickers.normalize("BF.B") == "BF-B"


def test_normalize_strips_whitespace():
    assert tickers.normalize("  GOOG  ") == "GOOG"


# --- resolve ------------------------------------------------------------------

def test_resolve_hits(tmp_path):
    cfg = _cfg(tmp_path)
    _write_ticker_file(cfg, {"AAPL": 320193, "MSFT": 789019})
    assert tickers.resolve(cfg, "AAPL") == 320193
    assert tickers.resolve(cfg, "msft") == 789019


def test_resolve_normalises_dotted(tmp_path):
    cfg = _cfg(tmp_path)
    _write_ticker_file(cfg, {"BRK-B": 1067983})
    assert tickers.resolve(cfg, "BRK.B") == 1067983


def test_resolve_raises_lookuperror_on_miss(tmp_path):
    cfg = _cfg(tmp_path)
    _write_ticker_file(cfg, {"AAPL": 320193})
    with pytest.raises(LookupError, match="not in SEC's"):
        tickers.resolve(cfg, "NOTREAL")


def test_resolve_many(tmp_path):
    cfg = _cfg(tmp_path)
    _write_ticker_file(cfg, {"AAPL": 320193, "MSFT": 789019, "BRK-B": 1067983})
    resolved, unresolved = tickers.resolve_many(cfg, ["AAPL", "BRK.B", "BOGUS"])
    assert resolved == {"AAPL": 320193, "BRK.B": 1067983}
    assert unresolved == ["BOGUS"]


# --- refresh-on-stale --------------------------------------------------------

def test_resolve_does_not_refresh_when_recent(tmp_path, monkeypatch):
    """A recent file should not trigger a download."""
    cfg = _cfg(tmp_path)
    _write_ticker_file(cfg, {"AAPL": 320193})
    called = {"n": 0}

    def fake_download(*a, **k):
        called["n"] += 1
        return cfg.paths.company_tickers

    monkeypatch.setattr(tickers, "download_company_tickers", fake_download)
    tickers.resolve(cfg, "AAPL")
    assert called["n"] == 0


def test_resolve_refreshes_when_file_stale(tmp_path, monkeypatch):
    """A file older than REFRESH_AFTER_SECONDS triggers a download."""
    cfg = _cfg(tmp_path)
    _write_ticker_file(cfg, {"AAPL": 320193})
    # Backdate the file's mtime by 8 days.
    eight_days = 8 * 24 * 3600
    old = time.time() - eight_days
    import os
    os.utime(cfg.paths.company_tickers, (old, old))

    called = {"n": 0}

    def fake_download(dest, **k):
        called["n"] += 1
        # Simulate the refresh having happened by touching the file.
        dest.touch()
        return dest

    monkeypatch.setattr(tickers, "download_company_tickers", fake_download)
    tickers.resolve(cfg, "AAPL")
    assert called["n"] == 1


def test_resolve_refreshes_when_file_missing(tmp_path, monkeypatch):
    """Force-write the SEC table after the download mock runs so resolve succeeds."""
    cfg = _cfg(tmp_path)
    # No file yet on disk.
    assert not cfg.paths.company_tickers.exists()

    def fake_download(dest, **k):
        _write_ticker_file(cfg, {"AAPL": 320193})
        return dest

    monkeypatch.setattr(tickers, "download_company_tickers", fake_download)
    assert tickers.resolve(cfg, "AAPL") == 320193


def test_resolve_force_refresh_invalidates_cache(tmp_path, monkeypatch):
    """refresh=True must re-read the file, picking up newly-added tickers."""
    cfg = _cfg(tmp_path)
    _write_ticker_file(cfg, {"AAPL": 320193})
    assert tickers.resolve(cfg, "AAPL") == 320193

    # Mutate the file behind the cache; without refresh it's stale.
    _write_ticker_file(cfg, {"AAPL": 320193, "MSFT": 789019})
    with pytest.raises(LookupError):
        tickers.resolve(cfg, "MSFT")   # cache miss; old table

    def fake_download(dest, **k):
        return dest

    monkeypatch.setattr(tickers, "download_company_tickers", fake_download)
    assert tickers.resolve(cfg, "MSFT", refresh=True) == 789019
