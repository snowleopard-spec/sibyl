from sibyl.config import VALID_STACKS, _resolve_paths, stack_clean, stack_raw, stack_record


def _cfg(tmp_path):
    from sibyl.config import Config, SecConfig, UnicornConfig, UniverseConfig
    paths = _resolve_paths(tmp_path, None, None)
    return Config(
        paths=paths,
        sec=SecConfig(user_agent="t", rate_limit_per_sec=1),
        unicorn=UnicornConfig(base_url="", universe_path="", expected_contract_version="1.0", token=None),
        universe=UniverseConfig(form_types=[], include_amendments=False, history_start="2016"),
        download_gzip=True,
    )


def test_resolve_paths_includes_both_stacks(tmp_path):
    p = _resolve_paths(tmp_path, None, None)
    assert p.sp500_raw == (tmp_path / "sp500" / "raw").resolve()
    assert p.sp500_clean == (tmp_path / "sp500" / "clean").resolve()
    assert p.sp500_record == (tmp_path / "sp500" / "record.jsonl").resolve()
    assert p.sp500_snapshots == (tmp_path / "sp500" / "membership_snapshots").resolve()
    assert p.queried_raw == (tmp_path / "queried" / "raw").resolve()
    assert p.queried_clean == (tmp_path / "queried" / "clean").resolve()
    assert p.queried_record == (tmp_path / "queried" / "record.jsonl").resolve()


def test_legacy_aliases_point_at_sp500(tmp_path):
    """raw/clean must alias sp500_raw/sp500_clean for legacy callers."""
    p = _resolve_paths(tmp_path, None, None)
    assert p.raw == p.sp500_raw
    assert p.clean == p.sp500_clean


def test_stack_helpers_dispatch_correctly(tmp_path):
    cfg = _cfg(tmp_path)
    assert stack_raw(cfg, "sp500") == cfg.paths.sp500_raw
    assert stack_raw(cfg, "queried") == cfg.paths.queried_raw
    assert stack_clean(cfg, "sp500") == cfg.paths.sp500_clean
    assert stack_clean(cfg, "queried") == cfg.paths.queried_clean
    assert stack_record(cfg, "sp500") == cfg.paths.sp500_record
    assert stack_record(cfg, "queried") == cfg.paths.queried_record


def test_stack_helpers_reject_unknown_stack(tmp_path):
    import pytest
    cfg = _cfg(tmp_path)
    for helper in (stack_raw, stack_clean, stack_record):
        with pytest.raises(ValueError, match="unknown stack"):
            helper(cfg, "garbage")


def test_valid_stacks_constant():
    assert VALID_STACKS == ("sp500", "queried")


def test_ensure_dirs_creates_both_stacks_and_record_files(tmp_path):
    from sibyl.config import ensure_dirs
    cfg = _cfg(tmp_path)
    ensure_dirs(cfg)
    for p in (cfg.paths.sp500_raw, cfg.paths.sp500_clean, cfg.paths.sp500_snapshots,
              cfg.paths.queried_raw, cfg.paths.queried_clean, cfg.paths.logs):
        assert p.is_dir(), f"missing dir {p}"
    for rec in (cfg.paths.sp500_record, cfg.paths.queried_record):
        assert rec.exists(), f"missing record file {rec}"
        assert rec.is_file()
