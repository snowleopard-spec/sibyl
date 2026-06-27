from __future__ import annotations

import argparse
import dataclasses
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path

from . import config as config_mod
from . import db as db_mod
from . import download as download_mod
from . import parse as parse_mod
from . import score as score_mod
from . import sections as sections_mod
from . import sp500 as sp500_mod
from .config import stack_clean


def _configure_logging(logs_dir: Path) -> None:
    logs_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    log_path = logs_dir / f"sibyl_{stamp}.log"
    handlers: list[logging.Handler] = [logging.FileHandler(log_path), logging.StreamHandler(sys.stderr)]
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        handlers=handlers,
    )


def _disk_usage(path: Path) -> int:
    if not path.exists():
        return 0
    total = 0
    for p in path.rglob("*"):
        if p.is_file():
            try:
                total += p.stat().st_size
            except OSError:
                pass
    return total


def _human_size(n: int) -> str:
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if n < 1024:
            return f"{n:.1f} {unit}"
        n /= 1024  # type: ignore[assignment]
    return f"{n:.1f} PiB"


def _force_10q_only(cfg: config_mod.Config) -> config_mod.Config:
    """Per spec §4.2, the sentiment monitor is 10-Q only. Build a Config
    variant whose universe.form_types is ['10-Q'] regardless of config.yaml."""
    universe = dataclasses.replace(cfg.universe, form_types=["10-Q"])
    return dataclasses.replace(cfg, universe=universe)


def cmd_download(args: argparse.Namespace) -> int:
    cfg = config_mod.load_config(args.config)
    config_mod.ensure_dirs(cfg)
    _configure_logging(cfg.paths.logs)
    log = logging.getLogger("sibyl.cli")

    conn = db_mod.connect(cfg.paths.db)
    db_mod.init_schema(conn)

    counts = download_mod.download_all(
        conn, cfg,
        stack="sp500",
        ciks=args.cik or None,
        limit=args.limit,
        refresh_submissions=args.refresh_submissions,
    )

    raw_root = config_mod.stack_raw(cfg, "sp500")
    raw_size = _disk_usage(raw_root)
    log.info("CIKs processed: %d", counts.ciks_processed)
    log.info("New filings:    %d", counts.new_filings)
    log.info("Skipped:        %d", counts.skipped)
    log.info("Failed:         %d", counts.failed)
    log.info("Raw disk usage: %s", _human_size(raw_size))
    return 0


def cmd_parse(args: argparse.Namespace) -> int:
    cfg = config_mod.load_config(args.config)
    config_mod.ensure_dirs(cfg)
    _configure_logging(cfg.paths.logs)
    log = logging.getLogger("sibyl.cli")
    conn = db_mod.connect(cfg.paths.db)
    db_mod.init_schema(conn)
    clean_root = stack_clean(cfg, "sp500")

    if args.stats:
        print(parse_mod.render_stats(parse_mod.compute_stats(conn, clean_root)))
        return 0

    counts = parse_mod.parse_all(
        conn, cfg,
        stack="sp500",
        ciks=args.cik or None,
        limit=args.limit,
        force=args.force,
        workers=args.workers,
    )
    clean_size = _disk_usage(clean_root)
    log.info("Filings parsed: %d", counts.parsed)
    log.info("Failed:         %d", counts.failed)
    log.info("Suspicious:     %d", counts.suspicious)
    log.info("Clean disk:     %s", _human_size(clean_size))
    return 0


def cmd_sections(args: argparse.Namespace) -> int:
    cfg = config_mod.load_config(args.config)
    config_mod.ensure_dirs(cfg)
    _configure_logging(cfg.paths.logs)
    log = logging.getLogger("sibyl.cli")
    conn = db_mod.connect(cfg.paths.db)
    db_mod.init_schema(conn)
    clean_root = stack_clean(cfg, "sp500")

    if args.stats:
        print(sections_mod.render_stats(sections_mod.compute_stats(conn, clean_root)))
        return 0

    counts = sections_mod.extract_all(
        conn, cfg,
        stack="sp500",
        ciks=args.cik or None,
        limit=args.limit,
        force=args.force,
        workers=args.workers,
    )
    log.info("Filings processed:  %d", counts.processed)
    log.info("Sections OK (both): %d", counts.both_ok)
    log.info("Partial:            %d", counts.partial)
    log.info("section_fail:       %d", counts.section_fail)
    return 0


def cmd_score(args: argparse.Namespace) -> int:
    cfg = config_mod.load_config(args.config)
    config_mod.ensure_dirs(cfg)
    _configure_logging(cfg.paths.logs)
    log = logging.getLogger("sibyl.cli")
    conn = db_mod.connect(cfg.paths.db)
    db_mod.init_schema(conn)

    if args.stats:
        print(score_mod.render_stats(score_mod.compute_stats(conn)))
        return 0

    counts = score_mod.score_all(
        conn, cfg,
        stack="sp500",
        ciks=args.cik or None,
        limit=args.limit,
        force=args.force,
    )
    log.info("Filings scored: %d", counts.processed)
    log.info("Rows written:   %d", counts.rows_written)
    log.info("Skipped:        %d", counts.skipped)
    log.info("Errors:         %d", counts.errors)
    return 0


def cmd_refresh(args: argparse.Namespace) -> int:
    """Full pipeline per spec §4: membership → download → parse → sections → score."""
    cfg = _force_10q_only(config_mod.load_config(args.config))
    config_mod.ensure_dirs(cfg)
    _configure_logging(cfg.paths.logs)
    log = logging.getLogger("sibyl.cli")
    conn = db_mod.connect(cfg.paths.db)
    db_mod.init_schema(conn)

    log.info("[1/5] Refreshing S&P 500 membership from Wikipedia.")
    holdings, _, unresolved, snap = sp500_mod.refresh_membership(cfg, conn)
    log.info("Membership: %d holdings, %d unresolved CIKs. Snapshot: %s",
             len(holdings), len(unresolved), snap)

    log.info("[2/5] Downloading missing 10-Q filings.")
    dl_counts = download_mod.download_all(conn, cfg, stack="sp500")
    log.info("Download: %d new, %d skipped, %d failed.",
             dl_counts.new_filings, dl_counts.skipped, dl_counts.failed)

    log.info("[3/5] Parsing HTML → plain text.")
    parse_counts = parse_mod.parse_all(conn, cfg, stack="sp500")
    log.info("Parse: %d parsed, %d failed.", parse_counts.parsed, parse_counts.failed)

    log.info("[4/5] Extracting MD&A + Risk Factors sections.")
    sec_counts = sections_mod.extract_all(conn, cfg, stack="sp500")
    log.info("Sections: %d both_ok, %d partial, %d section_fail.",
             sec_counts.both_ok, sec_counts.partial, sec_counts.section_fail)

    log.info("[5/5] Scoring LM categories (proportional weighting).")
    score_counts = score_mod.score_all(conn, cfg, stack="sp500")
    log.info("Score: %d filings, %d rows written.",
             score_counts.processed, score_counts.rows_written)

    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    refresh_file = cfg.paths.data_root / "last_refresh.txt"
    refresh_file.write_text(stamp + "\n", encoding="utf-8")
    log.info("Refresh complete. Timestamp written: %s", refresh_file)
    return 0


def cmd_rank(args: argparse.Namespace) -> int:
    from . import rank as rank_mod

    cfg = config_mod.load_config(args.config)
    conn = db_mod.connect(cfg.paths.db)
    db_mod.init_schema(conn)
    df = rank_mod.compute_ranks(conn, n_filings=args.n_filings)
    if df.empty:
        print("(no ranked tickers — corpus may be empty or unscored)")
        return 0

    sort_key = "decile_mdna" if args.sort == "mdna" else "decile_risk"
    df = df.sort_values([sort_key, "ticker"], ascending=[False, True])
    print(f"{'TICKER':<8} {'SECTOR':<26} {'D_MDNA':>7} {'D_RISK':>7} "
          f"{'NEG_MDNA':>10} {'NEG_RISK':>10}")
    print("-" * 72)
    for _, r in df.iterrows():
        print(f"{r['ticker']:<8} {str(r['sector'])[:26]:<26} "
              f"{int(r['decile_mdna']):>7d} {int(r['decile_risk']):>7d} "
              f"{r['mean_neg_mdna']:>10.5f} {r['mean_neg_risk']:>10.5f}")
    print(f"\n{len(df)} tickers scored.")
    return 0


def cmd_serve(args: argparse.Namespace) -> int:
    from . import serve as serve_mod

    app = serve_mod.create_app(config_path=args.config)
    app.run(host=args.host, port=args.port, debug=args.debug)
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    cfg = config_mod.load_config(args.config)
    conn = db_mod.connect(cfg.paths.db)
    db_mod.init_schema(conn)
    c = db_mod.counts(conn)

    print(f"data_root:   {cfg.paths.data_root}")
    db_size = cfg.paths.db.stat().st_size if cfg.paths.db.exists() else 0
    print(f"db:          {cfg.paths.db}  ({_human_size(db_size)})")
    print(f"raw:         {_human_size(_disk_usage(cfg.paths.sp500_raw))}")
    print(f"clean:       {_human_size(_disk_usage(cfg.paths.sp500_clean))}")
    refresh_file = cfg.paths.data_root / "last_refresh.txt"
    if refresh_file.exists():
        print(f"last_refresh: {refresh_file.read_text(encoding='utf-8').strip()}")
    print("counts:")
    for table, n in c.items():
        print(f"  {table:<22} {n}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="sibyl", description="S&P 500 sentiment monitor")
    parser.add_argument("--config", default="config.yaml", help="path to config.yaml")
    sub = parser.add_subparsers(dest="subcommand", required=True)

    p_refresh = sub.add_parser(
        "refresh", help="Full pipeline: membership → download → parse → sections → score (10-Q only)."
    )
    p_refresh.set_defaults(func=cmd_refresh)

    p_rank = sub.add_parser("rank", help="Print decile-ranked tickers (terminal table).")
    p_rank.add_argument("--n-filings", type=int, default=4, dest="n_filings",
                        help="Number of most recent filings per ticker to average (default: 4).")
    p_rank.add_argument("--sort", choices=("mdna", "risk"), default="mdna",
                        help="Which section's decile to sort by (default: mdna).")
    p_rank.set_defaults(func=cmd_rank)

    p_serve = sub.add_parser("serve", help="Start local Flask report at http://localhost:5000.")
    p_serve.add_argument("--host", default="127.0.0.1")
    p_serve.add_argument("--port", type=int, default=5000)
    p_serve.add_argument("--debug", action="store_true")
    p_serve.set_defaults(func=cmd_serve)

    p_status = sub.add_parser("status", help="DB and disk counts.")
    p_status.set_defaults(func=cmd_status)

    p_download = sub.add_parser("download", help="Pull 10-Q filings for S&P 500 (sub-step of refresh).")
    p_download.add_argument("--cik", type=int, action="append", default=[],
                            help="Download only this CIK (repeatable).")
    p_download.add_argument("--limit", type=int, default=None,
                            help="Stop after N new filings total.")
    p_download.add_argument("--refresh-submissions", action="store_true",
                            help="Re-fetch submissions JSON even if cached.")
    p_download.set_defaults(func=cmd_download)

    p_parse = sub.add_parser("parse", help="Clean raw HTML → full.txt (sub-step of refresh).")
    p_parse.add_argument("--cik", type=int, action="append", default=[])
    p_parse.add_argument("--limit", type=int, default=None)
    p_parse.add_argument("--force", action="store_true")
    p_parse.add_argument("--stats", action="store_true")
    p_parse.add_argument("--workers", type=int, default=None)
    p_parse.set_defaults(func=cmd_parse)

    p_sections = sub.add_parser("sections", help="Extract MD&A + Risk Factors (sub-step of refresh).")
    p_sections.add_argument("--cik", type=int, action="append", default=[])
    p_sections.add_argument("--limit", type=int, default=None)
    p_sections.add_argument("--force", action="store_true")
    p_sections.add_argument("--stats", action="store_true")
    p_sections.add_argument("--workers", type=int, default=None)
    p_sections.set_defaults(func=cmd_sections)

    p_score = sub.add_parser("score", help="LM scoring → filing_scores (sub-step of refresh).")
    p_score.add_argument("--cik", type=int, action="append", default=[])
    p_score.add_argument("--limit", type=int, default=None)
    p_score.add_argument("--force", action="store_true")
    p_score.add_argument("--stats", action="store_true")
    p_score.set_defaults(func=cmd_score)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
