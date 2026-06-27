"""Unit tests for sibyl/wiki.py — Wikipedia S&P 500 scraper."""
import pytest

from sibyl import wiki


# Minimal HTML fixture mimicking the constituents table structure on
# en.wikipedia.org. Header text matches EXPECTED_HEADERS verbatim.
SAMPLE_HTML = b"""
<html><body>
<table id="constituents" class="wikitable sortable">
  <tr>
    <th>Symbol</th><th>Security</th><th>GICSSector</th>
    <th>GICS Sub-Industry</th><th>Headquarters Location</th>
    <th>Date added</th><th>CIK</th><th>Founded</th>
  </tr>
  <tr>
    <td>AAPL</td><td>Apple Inc.</td><td>Information Technology</td>
    <td>Technology Hardware, Storage &amp; Peripherals</td>
    <td>Cupertino, California</td><td>1982-11-30</td>
    <td>0000320193</td><td>1976</td>
  </tr>
  <tr>
    <td>BRK.B</td><td>Berkshire Hathaway</td><td>Financials</td>
    <td>Multi-Sector Holdings</td><td>Omaha, Nebraska</td>
    <td>2010-02-16</td><td>0001067983</td><td>1839</td>
  </tr>
  <tr>
    <td>WEIRD</td><td>Weird Co</td><td>Industrials</td>
    <td>Conglomerates</td><td>Nowhere</td>
    <td>2024-01-01</td><td></td><td>2024</td>
  </tr>
</table>
</body></html>
"""


# --- _normalise_ticker --------------------------------------------------------

def test_normalise_ticker_dotted_share_class():
    assert wiki._normalise_ticker("BRK.B") == "BRK-B"
    assert wiki._normalise_ticker("brk.b") == "BRK-B"


def test_normalise_ticker_uppercases_and_strips():
    assert wiki._normalise_ticker("  aapl ") == "AAPL"


# --- _parse_cik ---------------------------------------------------------------

def test_parse_cik_strips_leading_zeros():
    assert wiki._parse_cik("0000320193") == 320193


def test_parse_cik_handles_blank_and_garbage():
    assert wiki._parse_cik("") is None
    assert wiki._parse_cik("not a cik") is None
    assert wiki._parse_cik(None) is None


# --- parse_constituents ------------------------------------------------------

def test_parse_constituents_extracts_three_rows():
    members = wiki.parse_constituents(SAMPLE_HTML)
    assert len(members) == 3
    tickers = [m.ticker for m in members]
    assert tickers == ["AAPL", "BRK-B", "WEIRD"]   # BRK.B normalised


def test_parse_constituents_keeps_sector_and_cik():
    members = wiki.parse_constituents(SAMPLE_HTML)
    by_ticker = {m.ticker: m for m in members}
    assert by_ticker["AAPL"].sector == "Information Technology"
    assert by_ticker["AAPL"].cik == 320193
    assert by_ticker["BRK-B"].sector == "Financials"
    assert by_ticker["BRK-B"].cik == 1067983


def test_parse_constituents_blank_cik_becomes_none():
    members = wiki.parse_constituents(SAMPLE_HTML)
    weird = next(m for m in members if m.ticker == "WEIRD")
    assert weird.cik is None


def test_parse_constituents_raises_when_table_missing():
    with pytest.raises(ValueError, match="No constituents table"):
        wiki.parse_constituents(b"<html><body>No table here.</body></html>")


def test_parse_constituents_raises_on_header_drift():
    """A silent column reshuffle on Wikipedia would otherwise corrupt the
    parse — fail loud so we notice."""
    bad = SAMPLE_HTML.replace(b"GICSSector", b"Industry Group")
    with pytest.raises(ValueError, match="header changed"):
        wiki.parse_constituents(bad)


# --- snapshot_html ------------------------------------------------------------

def test_snapshot_html_writes_dated_file(tmp_path):
    p = wiki.snapshot_html(b"<html/>", tmp_path / "snaps", stamp="2026-06-20")
    assert p.exists()
    assert p.name == "wiki_sp500_2026-06-20.html"
    assert p.read_bytes() == b"<html/>"
