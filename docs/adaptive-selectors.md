# Adaptive selectors

Scrapers usually break when a site changes its markup: a class gets renamed,
a wrapper `<div>` appears, a heading moves. Adaptive selectors let your code
keep working through changes like these. They also log a warning, so you
know to update the selector.

```python
products = page.css(".product-card", adaptive=True)
```

## How it works

1. **Remember.** When an adaptive query matches, wintergrab saves a
   *fingerprint* of up to three of the matched elements:
   - what they look like: tag, attributes, own text;
   - where they live: the path of ancestor tags, the parent's tag, attributes
     and text, sibling and child tags, and position among same-tag siblings.

   Fingerprints are keyed by the page's domain (`www.` is ignored) and an
   identifier, which defaults to the selector string.

2. **Relocate.** When an adaptive query matches **nothing**, wintergrab loads
   the fingerprint and scores every element on the page against it (0 to 1).
   The best match wins if it scores at least `min_score` (default `0.55`).
   If the selector originally matched several elements, such as a list of
   products, the best match is expanded to its structurally similar siblings.
   The result is a normal `SelectorList`.

3. **Update.** The relocated elements' fingerprints replace the old ones, so
   gradual changes are tracked across several redesigns.

Pseudo-elements work as you'd expect. With `.price::text` or
`//a/@href`, the element part is fingerprinted and relocated, then the
text/attribute is taken from the relocated elements.

## API

```python
page.css(query, adaptive=True)              # save when it matches, relocate when it doesn't
page.css(query, auto_save=True)             # only save (never relocate)
page.css(query, adaptive=True, identifier="product-cards")  # a stable name
page.css(query, adaptive=True, min_score=0.7)               # be stricter
page.xpath(query, adaptive=True)            # same for XPath
Field(".price::text", adaptive=True)        # inside extraction schemas
```

**Use `identifier` if you might change the selector later.** Fingerprints
are stored under the identifier, so a new selector string with the same
identifier can still fall back to what the old one matched:

```python
page.css(".totally-new-class", adaptive=True, identifier="product-cards")
```

Nested queries (`card.css("h2", adaptive=True)`) relocate within that
element only. Give them distinct identifiers if the same selector string is
used in different contexts.

## Storage

By default fingerprints live in a small SQLite database:

- Linux: `~/.cache/wintergrab/adaptive.sqlite3` (respects `XDG_CACHE_HOME`)
- macOS: `~/Library/Caches/wintergrab/adaptive.sqlite3`
- Windows: `%LOCALAPPDATA%\wintergrab\adaptive.sqlite3`

Override it with the `WINTERGRAB_ADAPTIVE_DB` environment variable, or per
fetcher / selector:

```python
from wintergrab import SQLiteStorage, MemoryStorage

storage = SQLiteStorage("fingerprints.sqlite3")         # e.g. next to your project
page = wg.get(url, adaptive_storage=storage)
fetcher = wg.Fetcher(adaptive_storage=storage)
doc = wg.parse(html, url=url, adaptive_storage=MemoryStorage())   # throwaway
```

Anything with `save(domain, identifier, record)`, `load(domain, identifier)`
and `delete(domain, identifier)` works as storage, for example Redis or a
shared database for a fleet of scrapers.

## What to expect

Adaptive matching handles the everyday changes well:

- renamed or added classes and ids;
- elements wrapped in, or moved out of, extra containers;
- list items that change content (different products on a new day);
- a title that moved into a `<header>`.

It can't recover from a complete rebuild where the tag, text, attributes and
surroundings all change at once. That's by design: `min_score` stops it from
returning a confident but wrong element. When nothing is similar enough you
get an empty result and a warning, the same as a broken selector.

Tips:

- Run your scraper once on the current site with `adaptive=True` (or
  `auto_save=True`) so there is something to fall back to.
- Keep an eye on the `wintergrab.adaptive` warnings. They tell you which
  selector to fix.
- Relocation reads the whole page. It takes well under a second even on pages
  with thousands of elements, but it only runs when a selector fails.

## Related tools

- `element.find_similar()` finds elements that look like one you already have.
- `page.find_by_text("…")` locates elements by their visible text.
- `element.css_path` / `element.xpath_path` generate a selector for an element
  you found some other way.
