# Where a page's data is

A page often holds the same records several ways:

- cards in its HTML;
- JSON-LD written for search engines;
- the state a JavaScript app embeds in the page (`__NEXT_DATA__`,
  `window.__INITIAL_STATE__`);
- the API calls the page makes as it renders.

The source with typed fields and no layout in the way is usually the one to
read. `--sources` lists them all, with the records each holds:

```bash
wintergrab get https://shop.example/catalog --browser --sources
```

```
https://shop.example/catalog
  HTML           6 records (article.card): title, url, price
                 3 records (table > tbody > tr): size, chest
  tables         3 rows (Size, Chest)
  JSON-LD        Organization, ItemList
  microdata      none
  meta           og:title, og:type, title, description, language
  embedded JSON  __NEXT_DATA__: 6 record(s) at props.pageProps.products[] (id, name, price, currency, url, rating +1)
  API calls      3 recorded as the page rendered
                 GET shop.example/api/products?limit&page: 3 record(s) at items[] (id, name, price, url)
                   pages by page: 1 of 2, 6 record(s) in all, next 2; called 2 times (page 1, 2)
                 POST shop.example/graphql, query Reviews: 2 record(s) at data.reviews.edges[].node (id, author, stars, text)
                   pages by cursor after: next 'r2'
                 POST shop.example/graphql, mutation TrackView: changes data (a mutation), no source to read
  richest        embedded JSON __NEXT_DATA__ props.pageProps.products[]: 6 records, 7 fields
```

(The test suite's page, `tests/data/sources/catalog.html`, and its API.)

- **HTML**: repeated elements with the same fields (cards, rows), as
  [`auto_extract`](power-features.md) finds them.
- **tables**, **JSON-LD**, **microdata**, **meta**: what `--tables` and
  `--structured` read, counted.
- **embedded JSON**: the lists of records in each blob `--json-data` reads.
- **API calls**: with `--browser`, the calls the page made as it rendered,
  and the lists of records in their answers. Calls that differ only in
  their values (`?page=1`, `?page=2`) are one API, called several times.
  Over HTTP no call is recorded: the endpoints the page's scripts name are
  listed instead, and not requested.
- **richest**: the place holding the most values (records times fields). It
  is often the one to read, but it says how much a place holds, not whether
  it holds what you need.

`-f json` prints the same as one JSON document per page, each list of
records with a sample record and each field's type.

An API's answer is a source too: `wintergrab get
'https://shop.example/api/products?page=1&limit=3' --sources` says where its
records are and how its pages go.

## Lists of records in JSON

`json_collections(data)` finds every list of records in a JSON document,
the ones holding the most values first. A path says where the records are:

| Path | Where |
|---|---|
| `items[]` | each item of the list `items` |
| `[]` | each item of the document, a list |
| `data.products.edges[].node` | each edge's `node`: a GraphQL connection, read through |
| `products[].variants[]` | every product's variants, all together |
| `__APOLLO_STATE__{Product}` | each value of a map of records keyed by id (an app's normalized state), those whose `__typename` is `Product` |

A list is one of records when at least two of its items, and four in five,
are objects. Lists that only point at records kept elsewhere
(`{"__ref": "Product:1"}`) are left out, and so is a record whose fields
happen to be objects (`{"price": {...}, "tax": {...}}`).

```python
from wintergrab.intel import data_sources, json_collections

products = json_collections(page.json())[0]
products.path, products.count, products.fields   # 'items[]', 3, ['id', 'name', 'price', 'url']
products.types                                   # {'id': 'integer', 'name': 'string', 'price': 'object', 'url': 'string'}
products.records[0]                              # the records themselves
products.schema("product")                       # a data schema for them, to review and keep
```

Field types are read from up to 50 records with
[schema inference](data.md#inferring-a-schema).

## Pagination

`pagination_of(url, answer)` says how an API's pages go, from the call's
query parameters (or a GraphQL call's variables) and its answer:

| Kind | Read from |
|---|---|
| `page` | a page number: `page`, `p`, `paged`, `pageNumber`... |
| `offset` | the first record's position: `offset`, `start`, `skip`, `from` |
| `cursor` | a token for the next page: `after`, `cursor`, `pageToken`..., with the answer's `endCursor`, `nextCursor`, `hasNextPage`, `has_more` |
| `next` | the next page's URL in the answer: `next`, `links.next`, `_links.next.href` |

With the page size (`limit`, `per_page`, `first`...) and the answer's
totals (`total`, `totalCount`, `total_pages`, `nbPages`, and `count` beside
`next`/`previous`), it tells the number of pages, the next page's value,
and when this page is the last. An API called several times with a number
stepping up (`?n=1`, `?n=2`, `?n=3`) is read as paged by that number, even
under a name it does not know: by page when it steps by one, by offset
when it steps by a page's size.

It reads; it requests nothing. A spider that follows an API's pages
requests them as any other page, under robots.txt and the
[network policy](fetching.md#network-policy-ssrf-protection).

## GraphQL

A call is a GraphQL one when its body (or its URL) holds a query or an
operation name: `POST /graphql {"operationName": "Reviews", "query":
"query Reviews(...)"}`, a persisted query by GET
(`?operationName=Menu&extensions=...`), or a batch of several. Each
operation is its own API. A mutation changes data: it is listed, but never
as a source.

## In a site's profile

`wintergrab inspect URL --browser` records the API calls of every page it
visits, and says what each endpoint answered:

```
APIs:
  GET https://shop.example/api/products?limit=&page=  (captured, 200; records: 3 at items[]; pages by page)
  POST https://shop.example/graphql  (captured, 200; query Reviews, mutation TrackView; records: 2 at data.reviews.edges[].node; pages by cursor after)
```

## In Python

```python
from wintergrab import BrowserFetcher
from wintergrab.intel import data_sources

with BrowserFetcher() as browser:
    page = browser.get("https://shop.example/catalog", capture=True, wait_until="networkidle")
sources = data_sources(page)
sources.richest()        # Source(kind='embedded', where='__NEXT_DATA__ props.pageProps.products[]', records=6, fields=7)
sources.sources()        # every place holding records, the richest first
products = next(call for call in sources.api if "products" in call.template)
products.calls, products.seen    # 2, [1, 2]: the page asked for two pages of it
products.pagination.next         # 2 (the first call asked for page 1)
products.collections[0].records  # the records of the first call's answer
```

## What it does not do

- Over HTTP, a page's API calls are not made, so they are not recorded:
  `--sources` without `--browser` lists the endpoints its scripts name.
- `capture=True` records the answers whose type is JSON. Calls that answer
  with HTML fragments or binary formats are not read; `--capture-filter`
  records calls by URL instead.
- Calls the page makes after it has loaded (on a click, on scroll) are
  recorded only if they happen during the fetch: add
  [browser actions](fetching.md#browser-actions) to make them happen.
- Pagination is read from names and values an API commonly uses. An API
  that pages some other way has no `pagination`, rather than a guess.
