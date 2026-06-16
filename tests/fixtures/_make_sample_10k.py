"""Generate tests/fixtures/sample_10k.html.gz. Re-run when you want to rebuild."""
import gzip
from pathlib import Path

html = """<html><head><script>SHOULD_NOT_APPEAR_script</script><style>SHOULD_NOT_APPEAR_style</style></head>
<body>
<h1>Form 10-K</h1>
<p>Acme Industries Inc. is engaged in the business of selling widgets across the United States and internationally.</p>
<p>Our company manufactures, distributes, and services a broad range of products including “smart” devices and the oﬃce supply category.</p>
<table><tr><td>Revenue</td><td>SHOULD_NOT_APPEAR_table_1234567</td></tr><tr><td>Expenses</td><td>SHOULD_NOT_APPEAR_table_900000</td></tr></table>
<p>The following discussion contains forward-looking statements within the meaning of the Private Securities Litigation Reform Act.</p>
<p>Item 1A. Risk Factors. <ix:nonFraction>1,234</ix:nonFraction> Investors should consider the following risks before investing in our securities. We face significant competition and our business may be adversely affected by changes in regulations.</p>
<p>Item 7. Management Discussion and Analysis of Financial Condition and Results of Operations. We had a strong year with revenue growth of fifteen percent across all business segments.</p>
""" + ("<p>Filler paragraph to ensure the cleaned text meets the word-count floor for ok status. </p>" * 200) + "</body></html>"

out = Path(__file__).parent / "sample_10k.html.gz"
with gzip.open(out, "wb") as f:
    f.write(html.encode("utf-8"))
print(f"wrote {out}: {out.stat().st_size} bytes")
