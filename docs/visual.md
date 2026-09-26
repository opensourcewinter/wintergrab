# What pages look like

HTML does not always say which values belong together:

- a pricing grid made of `<div>`s is a table only to the eye;
- a dashboard's tile says "1,284" in large type over "Active users";
- a specification is drawn as two aligned columns.

The structure is in how the page is drawn. WINTERGRAB reads it from the
page's **layout** (where each piece of text is drawn, in a browser), and a
model that reads images can be shown the page's **screenshot**.

```bash
wintergrab get https://dash.example/ --layout --extract stats.schema.json
```

```json
{"active_users": 1284, "revenue": {"amount": 48200, "currency": "USD"}, "uptime": 99.9,
 "weight": {"value": 1.2, "unit": "kg"}, "colour": "Graphite", "_confidence": 0.7277}
```

(The test page `tests/data/visual/dashboard.html`: tiles, a specification
and a pricing grid, all `<div>` and `<span>`. Without `--layout`, every field
is empty: its HTML has no label a value is tied to.)

## The layout

A browser fetch with `layout=True` (`--layout`) records every piece of
visible text with its place on the page, its font's size and weight, and
the path of the element holding it, SVG labels included:

```python
from wintergrab import BrowserFetcher

with BrowserFetcher() as browser:
    page = browser.get("https://dash.example/", layout=True, screenshot=True)
page.layout.boxes[0]
# Box(text='Home', x=20, y=37.4, width=42.7, height=17, lines=1, size=16, weight=400, tag='p',
#     path='body > div:nth-of-type(1) > p:nth-of-type(1)')
page.screenshot     # the full page, PNG
```

At most 5,000 pieces of text are recorded per page (`layout.truncated`
says when there were more). Text drawn on a `<canvas>` or inside an image is
not in the layout; a model that reads images can see it (below).

## Tables

`layout_tables(layout)` finds the tables the page draws, whatever its HTML:
an element whose text is drawn in rows with the same columns, left to
right. It needs at least three rows and two columns, making most of the
element's text.

- Pieces of one cell (`$` and `9` in two spans) are joined.
- The first row is a header when it is bold and the others are not, or when
  it holds only words over a column of numbers.

```bash
wintergrab get https://dash.example/ --visual-tables
```

```json
{"url": "https://dash.example/", "visual_tables": [{"header": ["Plan", "Price", "Seats"],
  "rows": [["Starter", "$9", "1"], ["Team", "$29", "10"], ["Business", "$99", "50"], ["Enterprise", "$299", "500"]],
  "path": "body > div:nth-of-type(2) > div:nth-of-type(1)", "box": [240, 79.9, 626.7, 192.9],
  "records": [{"Plan": "Starter", "Price": "$9", "Seats": "1"}, ...]}]}
```

A dashboard's row of tiles is not a table (two lines), and neither is a
chart's labels. `--tables` reads `<table>` elements from the HTML;
`--visual-tables` reads what is drawn.

## Labels and their values

`layout_pairs(layout)` pairs labels with their values. Only pairs whose
drawing says so count:

- **beside**: the value right of its label on the same line, when the two
  are alone in their element (a row of their own) or the label ends with a
  colon;
- **above** and **below**: a tile, two texts stacked alone in their
  element. The larger text is the value, whichever is on top; with equal
  sizes, a bold label on top;
- **table**: the rows of a two-column table of labels.

Cells of a wider table are not pairs, nor are texts that happen to share a
line in different parts of the page (a side menu beside a table).

The extractor uses these pairs when a page has a layout: the `visual`
method (prior 0.75, between `label` and `dom`). A field takes the value of a
label named like it (`active_users`: "Active users"). In a listing, each
record reads its own part of the layout:

```python
from wintergrab.extraction import Extractor

record = Extractor("stats.schema.json").extract(page)
record.fields["active_users"].source      # 'visual:above:Active users'
```

## Screenshots for models that read images

`Extractor(schema, model=..., vision=True)` (`--vision`, with `--extract`
and `--model`) sends the model the page's screenshot with its text. It is
for values drawn rather than written: a chart's figures, text inside an
image, a canvas.

```bash
wintergrab get https://sales.example/q3 --extract report.schema.json --model openai:NAME --vision
```

A value the model gives is checked against the page's text, as always. One
that is there counts as usual. One that is not might be drawn in the
screenshot, or made up, and nothing tells which. It is kept with a low
confidence (0.36 with the default priors) and the note `image-only`, where
a model shown no screenshot would have it dropped. Raise `min_confidence`
above 0.36 to keep only what the text confirms.

The screenshot goes to the API you name, with the page's text; see
[models](models.md#keys-and-what-leaves-your-machine). In a listing, the
screenshot is sent only for the page's own record, not for each card.

## PDFs

A PDF is read the same way: with `pypdf` installed (`pip install
"wintergrab[pdf]"`), a response holding a PDF has the PDF's text where it is
drawn as its layout, and a simple HTML version of it as its page. Its
headings come from font sizes, its lines are paragraphs, its tables are
tables, and its links are links (a crawl follows them). So `get` prints it,
`--visual-tables` reads its tables, and `--extract` reads its fields:

```bash
wintergrab get https://oak.example/prices.pdf
```

```
# Price list 2026

Invoice number: INV-0042

Due date: 2026-10-31

| Product | Unit | Price |
|---|---|---|
| Oak table | each | EUR 450.00 |
| Pine chair | each | EUR 89.50 |
| Walnut shelf | per metre | EUR 120.00 |

[Questions? Write to sales@oak.example or see our site.](https://oak.example/contact)
```

(A PDF made by the test suite, `tests/test_pdf.py`. Read as plain text, as
most tools do, its table is "Oak table each EUR 450.00".)

```python
page = wg.get("https://oak.example/prices.pdf")
page.pdf.title, page.pdf.page_count        # from the PDF's metadata
layout_tables(page.layout)[0].records()    # [{'Product': 'Oak table', 'Unit': 'each', 'Price': 'EUR 450.00'}, ...]
Extractor(invoice).extract(page)["invoice_number"]   # 'INV-0042', from "Invoice number: INV-0042"
```

- A PDF is recognized by its type, or by its first bytes when the server
  calls it something else.
- Positions, font sizes and boldness come from the PDF. Widths are estimated
  from the characters, since PDFs rarely give them.
- Lines close together make a block, and a block of aligned lines is a
  table. Pages sit one under the other in the layout.
- At most 100 pages are read (`pdf.truncated` says when there were more).
- A browser shows a PDF in its viewer, so a browser fetch asks for the file
  itself, with the browser's cookies.
- A damaged or password-protected PDF, or one read without `pypdf`, gives an
  empty page, and a warning says why. A scanned PDF (pictures of text) has
  no text to read.

## What it does not do

- It reads what a browser drew; a page fetched over HTTP has no layout
  (`--layout` implies `--browser`).
- It does not read pixels: text in images and on canvases is left to a
  model that reads images, and what it reads is marked `image-only`.
- A table must be the main content of an element. Tables drawn without an
  element of their own, among other text, are not found.
