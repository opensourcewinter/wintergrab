# Parsing

Every response can be queried directly. You can also parse any HTML you
already have:

```python
import wintergrab as wg

doc = wg.parse("<ul><li class='a'>one</li><li>two</li></ul>", url="https://example.com/")
doc = wg.parse(xml_string, type="xml")           # XML (sitemaps, feeds)
```

## Selecting

```python
page.css("div.product")                 # CSS -> SelectorList of elements
page.xpath("//div[@class='product']")   # XPath
page.select(".product")                 # guesses: XPath if it starts with / ./ ( ..
page.select("xpath://title/text()")     # or force with a prefix
```

### Getting strings out

| CSS | XPath | Gives |
|---|---|---|
| `h1::text` | `//h1/text()` | text directly inside `<h1>` |
| `h1 ::text` (note the space) | `//h1//text()` | every text node inside, children included |
| `a::attr(href)` | `//a/@href` | an attribute value |

To get an element's full visible text as one clean string, use `.text`:
`page.css("h1").text`.

```python
page.css("h1::text").get()                  # first, or None
page.css("h1::text").get(default="")        # first, or a default
page.css("a::attr(href)").getall()          # all as a list
```

### Working with elements

```python
card = page.css(".product").first      # or [0]; .first/.last are None when empty

card.tag            # 'div'
card.attrib         # {'class': 'product', 'data-id': '7'}
card["data-id"]     # '7'   (KeyError if missing; card.attr("x", default) doesn't raise)
card.text           # all visible text, whitespace normalised ("Blue mug $12 Buy")
card.own_text       # only text placed directly in this element
card.html           # outer HTML; card.inner_html for the inside
card.get_text()     # readable text with one line per block element
card.markdown()     # this element as Markdown

card.parent, card.children, card.next, card.previous, card.siblings, card.ancestors
card.closest("section")           # nearest ancestor (or self) matching CSS
card.css_path                     # a unique CSS selector for this element
card.xpath_path                   # an absolute XPath
```

### `SelectorList` helpers

`css()`/`xpath()` return a `SelectorList` (a `list` with extras):

```python
products = page.css(".product")
products.css(".name::text").getall()   # query inside every element
products.texts                         # [p.text for p in products]
products.text                          # text of the first (None if empty)
products.attr("href")                  # first element that has the attribute
products.attrs("data-id")              # the attribute from every element
products.filter(lambda p: "sale" in p.attr("class", ""))
products.re(r"\$([\d.]+)")             # regex over each element's text
```

### Regular expressions

```python
page.css(".price").re(r"[\d.]+")                    # all matches
page.css(".price").re_first(r"\$(\d+)")             # first group of the first match
page.css(".price::text").re(r"(?P<extract>\d+)\.")  # a group named `extract` wins
page.re(r"var token = '(\w+)'")                     # on a Response: the raw body
```

## Extraction schemas

Describe the record you want. wintergrab fills it in:

```python
from wintergrab import Field

product = page.extract({
    "name": "h1",                                       # string: first match (element -> text)
    "images": ["img.gallery::attr(src)"],               # one-item list: every match
    "price": Field(".sale-price::text", ".price::text", # fallbacks, tried in order
                   regex=r"[\d.]+", transform=float, default=None),
    "sku": "//th[.='SKU']/following-sibling::td/text()",  # XPath works anywhere
    "seller": {"name": ".seller a", "url": ".seller a::attr(href)"},  # nested
    "in_stock": lambda sel: bool(sel.css(".in-stock")),   # any function of the selector
})

rows = page.extract_all("table.results tr", {"name": "td:nth-child(1)", "score": "td:nth-child(2)"})
```

`Field` options: `many`, `default`, `regex`, `transform`, `attr` (read an
attribute of the matched element), `html` (return HTML instead of text),
`adaptive` (see [adaptive selectors](adaptive-selectors.md)).

## Searching without selectors

```python
page.find_by_text("Add to basket")                  # elements whose text contains it
page.find_by_text("Blue mug", partial=False)        # exact (case-insensitive by default)
page.find_by_text("Price: $10")                     # works across <b>/<span> boundaries
page.find_by_regex(r"^\d+ reviews?$", tag="span")

first = page.find_by_text("Blue mug").first.parent  # one product card...
cards = [first, *first.find_similar()]              # ...and every card like it
```

`find_similar()` looks for elements with the same tag, depth and ancestry,
then compares attributes (ignoring values that normally differ between items,
like `href` and `id`) and the shape of their children.

## Links

```python
page.links()                                   # every http(s) link, absolute, no #fragments
page.links(".pagination")                      # only inside a container
page.links("a.product::attr(href)")            # or from attribute selectors
page.links(allow=r"/product/\d+", deny=r"\?sort=")
page.links(same_domain=True)                   # or domains=["example.com"]
page.urljoin("../next")                        # respects <base href>
```

## Text and Markdown

```python
page.get_text()                       # readable plain text
page.markdown()                       # headings, lists, links, tables, code blocks...
page.markdown(main_content=True)      # only <main>/<article>, skipping nav/footer chrome
```

Scripts, styles, `<template>`, `<noscript>`, SVG and iframes are always left
out.

## XML

```python
feed = wg.parse(xml, type="xml")
feed.xpath("//a:entry/a:title/text()", namespaces={"a": "http://www.w3.org/2005/Atom"})
feed.remove_namespaces()                  # then simply:
feed.xpath("//entry/title/text()").getall()
```

Responses with an XML content type (sitemaps, RSS) are parsed as XML
automatically.
