from __future__ import annotations

import wintergrab as wg

DOC = """
<html><head><title>T</title><style>.x{}</style></head><body>
<header><nav><a href="/">Home</a></nav></header>
<main>
  <h1>Main   title</h1>
  <p>Some <strong>bold</strong> and <em>italic</em> text with a <a href="/link">link</a>.</p>
  <ul><li>one</li><li>two<ul><li>nested</li></ul></li></ul>
  <ol start="3"><li>three</li><li>four</li></ol>
  <pre>  keep
    spacing</pre>
  <blockquote><p>quoted</p></blockquote>
  <table><tr><th>A</th><th>B</th></tr><tr><td>1</td><td>x|y</td></tr></table>
  <img src="/i.png" alt="pic">
  <p>line<br>break</p>
</main>
<script>alert(1)</script>
</body></html>
"""


def test_markdown_conversion() -> None:
    md = wg.parse(DOC, url="https://site.test/page").markdown()
    assert "# Main title" in md
    assert "Some **bold** and *italic* text with a [link](https://site.test/link)." in md
    assert "- one\n- two" in md and "  - nested" in md
    assert "3. three\n4. four" in md
    assert "```\n  keep\n    spacing\n```" in md
    assert "> quoted" in md
    assert "| A | B |\n|---|---|\n| 1 | x\\|y |" in md
    assert "![pic](https://site.test/i.png)" in md
    assert "alert(1)" not in md and ".x{}" not in md
    assert "[Home](https://site.test/)" in md


def test_markdown_main_content_only() -> None:
    md = wg.parse(DOC, url="https://site.test/").markdown(main_content=True)
    assert md.startswith("# Main title")
    assert "Home" not in md


def test_get_text_is_block_aware() -> None:
    text = wg.parse(DOC).css("body").first.get_text()
    lines = text.splitlines()
    assert "Main title" in lines
    assert "Some bold and italic text with a link." in lines
    assert "  keep" in lines and "    spacing" in lines
    assert "A B" in lines
    assert "line" in lines and "break" in lines
    assert "alert(1)" not in text
