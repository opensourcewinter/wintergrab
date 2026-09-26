# API reference

Every public name, by module, with its signature and what it does. The guides show them
in use; this page is written from the code (`python scripts/api_reference.py`), and a test
checks that it is up to date.

## `wintergrab`: Fetching, parsing and the most used names

- **`AsyncBrowserFetcher(*, headless: bool = True, executable_path: str | None = None, channel: str | None = None, proxy: str | None = None, proxies: ProxyRotator | Sequence[str] | None = None, user_agent: str | None = None, locale: str = 'en-US', timezone_id: str | None = None, viewport: tuple[int, int] = (1366, 768), extra_headers: Mapping[str, str] | None = None, cookies: Mapping[str, str] | Sequence[Mapping[str, Any]] | None = None, block_resources: Iterable[str] = ('image', 'media', 'font'), timeout: float = 30.0, wait_until: str = 'load', max_pages: int = 4, retries: int = 1, retry_statuses: Iterable[int] = {408, 425, 429, 500, 502, 503, 504, 520, 521, 522, 523, 524}, wait_for_challenge: bool = True, challenge_timeout: float = 20.0, user_data_dir: str | None = None, launch_args: Sequence[str] | None = None, adaptive_storage: AdaptiveStorage | None = None, cache: HTTPCache | str | bool | None = None, cache_mode: str | None = None, cache_ttl: float | None = None, resource_filter: ResourceFilter | Mapping[str, Any] | bool | None = None, network_policy: NetworkPolicy | str | bool | None = None)`** (class). Fetch pages with a real (headless) Chromium via Playwright.
  - `aclose(self)`: Close every tab, context and the browser itself.
  - `close(self)`: Close every tab, context and the browser itself.
  - `export_cookies(self, url: str | None = None, *, proxy: str | None = None) -> list[dict[str, Any]]`: Cookies of the browser session (optionally only those sent to ``url``).
  - `get(self, url: str, **kwargs: Any) -> Response`
  - `get_many(self, urls: Iterable[str], *, concurrency: int | None = None, return_exceptions: bool = True, **kwargs: Any) -> list[Response | FetchError]`: Render many pages concurrently (bounded by ``max_pages``).
  - `request(self, method: str, url: str, *, proxy: str | None = None, headers: Mapping[str, str] | None = None, timeout: float | None = None, retries: int | None = None, wait_for: str | None = None, wait: float = 0.0, wait_until: str | None = None, scroll: bool | int = False, page_action: Callable[[Any], Any] | None = None, screenshot: str | Path | bool | None = None, capture: bool | str | Callable[[str], bool] | None = None, layout: bool = False, actions: Any = None, downloads: str | Path | None = None, request: Request | None = None, **_ignored: Any) -> Response`: Open ``url`` in a new tab and return the rendered page.
  - `start(self)`: Launch the browser (done automatically on the first request).
- **`AsyncFetcher(*, max_connections: int = 64, **kwargs: Any)`** (class). Asynchronous version of :class:`Fetcher` for fetching many pages at once.
  - `aclose(self)`
  - `add_cookies(self, cookies: Mapping[str, str] | Iterable[Mapping[str, Any]], *, url: str | None = None, domain: str | None = None)`: Load cookies into the session (see :meth:`Fetcher.add_cookies`).
  - `close(self)`
  - `delete(self, url: str, **kwargs: Any) -> Response`
  - `get(self, url: str, **kwargs: Any) -> Response`
  - `get_many(self, urls: Iterable[str], *, concurrency: int = 10, return_exceptions: bool = True, **kwargs: Any) -> list[Response | FetchError]`: Fetch many URLs concurrently; results come back in input order.
  - `head(self, url: str, **kwargs: Any) -> Response`
  - `iter_many(self, urls: Iterable[str], *, concurrency: int = 10, **kwargs: Any) -> AsyncIterator[Response | FetchError]`: Like :meth:`get_many` but yields each result as soon as it is ready.
  - `patch(self, url: str, **kwargs: Any) -> Response`
  - `post(self, url: str, **kwargs: Any) -> Response`
  - `put(self, url: str, **kwargs: Any) -> Response`
  - `request(self, method: str, url: str, *, params: Mapping[str, Any] | None = None, headers: Mapping[str, str] | None = None, cookies: Mapping[str, str] | None = None, data: Any = None, json: Any = None, proxy: str | None = None, timeout: float | None = None, retries: int | None = None, allow_redirects: bool | None = None, request: Request | None = None, **extra: Any) -> Response`: Async version of :meth:`Fetcher.request`.
- **`AutoThrottle(*, enabled: bool = True, base_delay: float = 0.0, max_delay: float = 60.0, max_concurrency: int = 4, target_concurrency: float | None = None, backoff_factor: float = 2.0, min_backoff_delay: float = 1.0, recovery: float = 0.85, increase_every: int = 10, randomize: bool = True)`** (class). Adaptive, per-domain request pacing (AIMD - like TCP congestion control).
  - `can_start(self, slot: DomainSlot, now: float) -> bool`
  - `describe(self, domain: str) -> str`: One line for reports, e.g.
  - `mode(self, slot: DomainSlot, now: float | None = None) -> str`: ``"paused"`` (honouring Retry-After), ``"backing off"`` (push-back in the last 30 s), ``"recovering"`` (slower than the target after a push-back) or ``"normal"``.
  - `on_error(self, domain: str)`: A timeout or connection error: back off gently.
  - `on_finish(self, slot: DomainSlot)`
  - `on_pushback(self, domain: str, retry_after: float | None = None)`: The site said "slow down" (429/503/block page).
  - `on_start(self, slot: DomainSlot, now: float)`
  - `on_success(self, domain: str, latency: float)`
  - `restore(self, data: dict[str, dict[str, Any]])`
  - `set_min_delay(self, domain: str, delay: float)`: Enforce a floor (e.g.
  - `slot(self, domain: str) -> DomainSlot`
  - `snapshot(self) -> dict[str, dict[str, Any]]`
  - `state(self, domain: str, now: float | None = None) -> dict[str, Any]`: Live throttle state of a domain: delays, concurrency, latency, target rate and back-off mode.
  - `target_delay(self, slot: DomainSlot) -> float`: The delay healthy responses pull towards: ``latency / target_concurrency`` (at least the floor).
- **`BrowserError`** (exception). Something went wrong with the headless browser.
- **`BrowserFetchError`** (exception). A page failed to load or render in the browser (crash, navigation error...).
- **`BrowserFetcher(**kwargs: Any)`** (class). Synchronous wrapper around :class:`AsyncBrowserFetcher`.
  - `close(self, timeout: float | None = 30)`: Close the browser and its background thread.
  - `export_cookies(self, url: str | None = None, *, proxy: str | None = None) -> list[dict[str, Any]]`: Cookies of the browser session - see :meth:`AsyncBrowserFetcher.export_cookies`.
  - `get(self, url: str, **kwargs: Any) -> Response`: Render ``url``.
  - `get_many(self, urls: Iterable[str], **kwargs: Any) -> list[Response | FetchError]`
  - `start(self)`
- **`BrowserNotAvailable`** (exception). Playwright (or a browser binary for it) is not installed.
- **`BudgetExceeded`** (exception). A crawl budget (requests, bytes, runtime...) ran out.
- **`CacheMiss`** (exception). Raised in ``"offline"`` cache mode when a request is not in the cache.
- **`CapturedResponse(url: str, method: str, status: int, headers: dict[str, str], body: bytes, resource_type: str = 'fetch', request_body: str | None = None, order: int = 0)`** (class). An XHR/fetch response recorded while a page rendered (see ``capture=``).
  - `json(self) -> Any`
- **`CheckpointError`** (exception). A crawl could not be checkpointed or resumed.
- **`ConfigurationError`** (exception). Invalid settings, project files or command line options.
- **`CrawlResult(items: list[Any] = ..., stats: dict[str, Any] = ..., status: str = 'finished', crawl_dir: str | None = None, failures: list[FailureDiagnosis] = ..., metrics: dict[str, Any] = ..., changes: Any = None, profile: Any = None, fetch_strategy: Any = None, optimizer: Any = None, run_id: str | None = None)`** (class). What :meth:`Spider.run` returns.
  - `failure_report(self, limit: int = 10) -> str`: The failure diagnoses as readable text.
  - `save(self, path: str | Path) -> Path`: Write :attr:`items` to ``.jsonl``, ``.json`` or ``.csv``.
- **`DropItem`** (exception). Raised by a pipeline to drop an item; the message says why (counted in the stats).
- **`ExportError`** (exception). Items could not be written to an output.
- **`ExpressionError`** (exception). A filter/computed-field expression is invalid, or failed on a record (see :mod:`wintergrab.data.expressions`).
- **`ExtractionError`** (exception). Data could not be extracted (a required field is missing, an extractor crashed...).
- **`FetchError`** (exception). A page could not be fetched (network error, timeout, proxy failure...).
- **`FetchTimeout`** (exception). The request did not complete in time.
- **`Fetcher(**kwargs: Any)`** (class). Synchronous HTTP client with a browser's TLS, HTTP/2 settings and headers (``impersonate``).
  - `add_cookies(self, cookies: Mapping[str, str] | Iterable[Mapping[str, Any]], *, url: str | None = None, domain: str | None = None)`: Load cookies into the session, scoped to a domain.
  - `close(self)`
  - `delete(self, url: str, **kwargs: Any) -> Response`
  - `get(self, url: str, **kwargs: Any) -> Response`
  - `head(self, url: str, **kwargs: Any) -> Response`
  - `patch(self, url: str, **kwargs: Any) -> Response`
  - `post(self, url: str, **kwargs: Any) -> Response`
  - `put(self, url: str, **kwargs: Any) -> Response`
  - `request(self, method: str, url: str, *, params: Mapping[str, Any] | None = None, headers: Mapping[str, str] | None = None, cookies: Mapping[str, str] | None = None, data: Any = None, json: Any = None, proxy: str | None = None, timeout: float | None = None, retries: int | None = None, allow_redirects: bool | None = None, **extra: Any) -> Response`: Send a request and return a :class:`Response` (retrying if needed).
- **`Field(*queries: str, many: bool = False, default: Any = None, regex: str | None = None, transform: Callable[[Any], Any] | None = None, attr: str | None = None, html: bool = False, adaptive: bool = False)`** (class). One field of an extraction schema.
  - `extract(self, sel: Selector) -> Any`
- **`HTTPCache(path: str | os.PathLike[str] | None = None, *, mode: str = 'revalidate', ttl: float | None = None, statuses: Iterable[int] = {200, 203, 204, 300, 301, 308, 404, 405, 410, 414, 501}, methods: Iterable[str] = ('GET', 'HEAD'), max_body: int = 52428800, respect_no_store: bool = True)`** (class). SQLite-backed HTTP cache shared by fetchers and spiders.
  - `clear(self)`
  - `close(self)`
  - `classmethod coerce(cls, value: HTTPCache | str | os.PathLike[str] | bool | None, *, mode: str | None = None, ttl: float | None = None) -> HTTPCache | None`: ``True`` -> default cache, a path -> cache there, a cache -> itself.
  - `conditional_headers(entry: CachedResponse) -> dict[str, str]`
  - `delete(self, request: Request, namespace: str = '')`
  - `entries(self) -> Iterator[CachedResponse]`: Every stored response, oldest first (browser-rendered ones have keys starting ``browser:``).
  - `get(self, request: Request, namespace: str = '') -> CachedResponse | None`
  - `handles(self, method: str) -> bool`
  - `is_fresh(self, entry: CachedResponse, now: float | None = None) -> bool`: Whether ``entry`` can be served without asking the server.
  - `key(request: Request, namespace: str = '') -> str`
  - `put(self, request: Request, response: Response, namespace: str = '') -> bool`: Store ``response`` for ``request`` (if cacheable).
  - `refresh(self, entry: CachedResponse, not_modified: Response) -> CachedResponse`: Apply a ``304 Not Modified`` to a stored entry and return the updated entry.
  - `storable(self, request: Request, response: Response) -> bool`
- **`HTTPError`** (exception). Raised by :meth:`Response.raise_for_status` for 4xx/5xx responses.
`HTTPStatusError`: see [`wintergrab`](#wintergrab-fetching-parsing-and-the-most-used-names).

- **`IgnoreRequest`** (exception). Raised by a middleware's ``process_request`` to drop a request without an error.
- **`ItemPipeline()`** (class). Optional base class for pipelines (every method is optional).
  - `close_spider(self, spider: Spider) -> Any`: Called once after the crawl ends.
  - `open_spider(self, spider: Spider) -> Any`: Called once before the crawl starts.
  - `process_item(self, item: Any, spider: Spider) -> Any`: Return the item (possibly changed), or ``None`` / raise :class:`DropItem` to drop it.
- **`LearnedSchema(container: str | None, fields: dict[str, str])`** (class). Selectors learned by :func:`learn_schema`.
  - `extract(self, source: Any, *, base_url: str | None = None) -> list[dict[str, Any]]`: One dict per record of ``source`` (lxml element, Selector, Response or HTML).
  - `extract_one(self, source: Any, *, base_url: str | None = None) -> dict[str, Any] | None`: The first record (``None`` if there is none).
  - `classmethod from_dict(cls, data: Mapping[str, Any]) -> LearnedSchema`: Rebuild a schema saved with :meth:`to_dict`.
  - `to_dict(self) -> dict[str, Any]`: JSON-serialisable form (see :meth:`from_dict`).
- **`MemoryStorage()`** (class). Keeps fingerprints in a dict.
  - `delete(self, domain: str, identifier: str)`
  - `load(self, domain: str, identifier: str) -> dict[str, Any] | None`
  - `save(self, domain: str, identifier: str, record: dict[str, Any])`
- **`ModelError`** (exception). A language model could not be asked, or gave no usable answer (see :mod:`wintergrab.models`).
- **`NetworkError`** (exception). The connection failed: DNS lookup, refused/reset connection, TLS handshake...
- **`NetworkPolicy(*, allow_private: bool = False, allow_loopback: bool = False, allow_link_local: bool = False, allow_reserved: bool = False, allowed_networks: Iterable[str | IPNetwork] = (), denied_networks: Iterable[str | IPNetwork] = (), allowed_hosts: Iterable[str] = (), denied_hosts: Iterable[str] = (), allowed_ports: Iterable[int] | None = None, schemes: Iterable[str] = ('http', 'https'), resolve: bool = True, dns_cache_ttl: float = 60.0)`** (class). Rules for which hosts and addresses requests may reach.
  - `address_reason(self, address: str | IPAddress) -> str | None`: Why ``address`` is refused, or ``None`` if it is allowed.
  - `check(self, url: str, *, proxied: bool = False)`: Raise :class:`NetworkPolicyError` if ``url`` may not be requested.
  - `check_connected(self, url: str, address: str | None, *, proxied: bool = False)`: After connecting: refuse if curl ended up at a forbidden address (DNS rebinding).
  - `check_sync(self, url: str, *, proxied: bool = False)`: Blocking version of :meth:`check` (for the synchronous :class:`~wintergrab.Fetcher`).
  - `classmethod coerce(cls, value: Any) -> NetworkPolicy | None`: ``None``/``False``/``"any"`` -> no policy; ``True``/``"public"`` -> :meth:`public`; ``"private"`` -> also private networks and loopback; a dict -> keyword arguments.
  - `is_allowed_address(self, address: str) -> bool`
  - `classmethod public(cls) -> NetworkPolicy`: Public internet addresses only (the recommended setting for untrusted sites).
  - `reason(self, url: str) -> str | None`: Static verdict for ``url`` (no DNS): the refusal reason, or ``None``.
- **`NetworkPolicyError`** (exception). The destination is not allowed by the :class:`~wintergrab.netpolicy.NetworkPolicy` (for example a private, loopback or cloud-metadata address: SSRF protection).
- **`ParserError`** (exception). A document or expression could not be parsed.
- **`PolicyError`** (exception). A crawl policy refused the request.
- **`ProxyError`** (exception). The proxy refused or failed the request.
- **`ProxyRotator(proxies: Iterable[str], *, strategy: str = 'round_robin', max_failures: int = 3, cooldown: float = 120.0)`** (class). Hands out proxies in turn and benches the ones that keep failing.
  - `classmethod coerce(cls, value: ProxyRotator | Sequence[str] | str | None) -> ProxyRotator | None`: Turn a list/str/rotator/``None`` into a rotator (or ``None``).
  - `classmethod from_file(cls, path: str | Path, **kwargs: object) -> ProxyRotator`: One proxy per line; blank lines and ``#`` comments are ignored.
  - `next(self) -> str`: The proxy to use for the next request.
  - `report_failure(self, proxy: str | None)`
  - `report_success(self, proxy: str | None)`
  - `reuse(self, proxy: str) -> str`: ``proxy`` once more (a retry of a page the site answered through it), counted as a use.
  - `stats(self) -> list[dict[str, object]]`: Per-proxy counters (passwords hidden).
- **`RecordGroup(container_selector: str, elements: list[etree._Element], score: float, fields: dict[str, str])`** (class). A list of repeating records found by :func:`detect_records`.
  - `as_schema(self) -> LearnedSchema`: The group as a reusable :class:`LearnedSchema` (to extract other pages of the site).
  - `extract(self, base_url: str | None = None) -> list[dict[str, Any]]`: One dict per record (URLs absolute, missing values left out).
- **`Request(url: str, callback: Callback = None, method: str = 'GET', headers: dict[str, str] | None = None, params: Mapping[str, Any] | None = None, data: Any = None, json: Any = None, cookies: dict[str, str] | None = None, meta: dict[str, Any] = ..., cb_kwargs: dict[str, Any] = ..., priority: int = 0, dont_filter: bool = False, session: str | None = None, proxy: str | None = None, errback: Callback = None, options: dict[str, Any] = ...)`** (class). Something to download, plus what to do with the result.
  - `body_bytes(self) -> bytes`
  - `fingerprint(self) -> bytes`: Identity used for duplicate filtering (method + canonical URL + body).
  - `classmethod from_dict(cls, data: dict[str, Any], spider: Spider | None = None) -> Request`
  - `replace(self, **changes: Any) -> Request`: A copy with some fields changed (``meta``/``options`` are copied).
  - `to_dict(self, spider: Spider | None = None) -> dict[str, Any]`: Serialize (callbacks become spider method names).
- **`ResourceFilter(*, block_types: Iterable[str] = ('image', 'media', 'font'), lists: Iterable[str] = (), block_domains: Iterable[str] = (), allow_domains: Iterable[str] = (), block_third_party: bool = False, block_patterns: Iterable[str] = (), allow_patterns: Iterable[str] = ())`** (class). Decides which browser requests to block.
  - `blocks(self, url: str, resource_type: str, page_url: str | None = None, *, main_document: bool = False) -> bool`
  - `classmethod coerce(cls, value: Any, block_types: Iterable[str] | None = None) -> ResourceFilter | None`: ``None``/``False`` -> none; a filter -> itself; a dict -> options; ``True`` -> ads+analytics+trackers.
  - `load_list(self, path: str | Path) -> int`: Add the domains of a blocklist file.
  - `reason(self, url: str, resource_type: str, page_url: str | None = None, *, main_document: bool = False) -> str | None`: Why the request should be blocked, or ``None`` to let it through.
- **`Response(url: str, *, status: int = 200, headers: Headers | Mapping[str, str] | Iterable[tuple[str, str]] | None = None, body: bytes = ..., request: Request | None = None, reason: str = '', encoding: str | None = None, cookies: Mapping[str, str] | None = None, elapsed: float = 0.0, history: list[str] | None = None, http_version: str | None = None, source: str = 'http', adaptive_storage: AdaptiveStorage | None = None)`** (class). A downloaded page.
  - `auto_extract(self, **kwargs: Any) -> list[dict[str, Any]]`: Records from the page's main repeating list, fields inferred automatically.
  - `captured_json(self, url_contains: str | None = None) -> list[Any]`: Parsed JSON bodies of captured API calls (browser fetches with ``capture=``).
  - `css(self, query: str, **kwargs: Any) -> SelectorList`: CSS query on the page.
  - `detect_records(self, **kwargs: Any) -> list[Any]`: Repeating record groups on the page, best first (see :meth:`Selector.detect_records`).
  - `embedded_json(self) -> dict[str, Any]`: JSON state embedded by JavaScript apps (``__NEXT_DATA__``, ``window.__STATE__``...).
  - `extract(self, schema: Mapping[str, Any]) -> dict[str, Any]`: Extract a dict with a schema of selectors.
  - `extract_all(self, query: str, schema: Mapping[str, Any], **kwargs: Any) -> list[dict[str, Any]]`: Extract one dict per element matched by ``query``.
  - `find_by_regex(self, pattern: str | Pattern[str], **kwargs: Any) -> SelectorList`
  - `find_by_text(self, text: str, **kwargs: Any) -> SelectorList`
  - `find_json(self, key: Any, *, limit: int | None = None) -> list[Any]`: Every value under ``key`` in the page's embedded JSON / JSON-LD - or in the body, for JSON responses.
  - `follow(self, url: str | Selector, callback: Any = None, **kwargs: Any) -> Request`: A :class:`Request` for a link on this page (relative URLs are fine).
  - `follow_all(self, css: str | None = None, *, urls: Iterable[str | Selector] | None = None, callback: Any = None, allow: str | Iterable[str] | None = None, deny: str | Iterable[str] | None = None, same_domain: bool = False, **kwargs: Any) -> list[Request]`: Requests for every link matched by ``css`` (or given in ``urls``).
  - `follow_next(self, callback: Any = None, **kwargs: Any) -> Request | None`: A :class:`Request` for the next page of a paginated listing (``None`` on the last page).
  - `get_text(self) -> str`: Readable text of the page body.
  - `json(self, **kwargs: Any) -> Any`: Parse the body as JSON.
  - `learn(self, examples: Any) -> Any`: Learn a reusable extraction schema from example values (see :meth:`Selector.learn`).
  - `links(self, css: str | None = None, **kwargs: Any) -> list[str]`: Absolute URLs of the page's links.
  - `markdown(self, *, main_content: bool = False) -> str`: The page converted to Markdown.
  - `next_page(self) -> str | None`: URL of the "next page" link, if the page has pagination.
  - `raise_for_status(self) -> Response`: Raise :class:`~wintergrab.errors.HTTPStatusError` for 4xx/5xx.
  - `re(self, pattern: str | Pattern[str], flags: int = 0) -> list[str]`: Regex over the raw body text.
  - `re_first(self, pattern: str | Pattern[str], default: str | None = None, flags: int = 0) -> str | None`
  - `save(self, path: str | Path) -> Path`: Write the raw body to a file and return its path.
  - `select(self, query: str, **kwargs: Any) -> SelectorList`: CSS or XPath, guessed from the query.
  - `structured_data(self) -> dict[str, Any]`: JSON-LD, microdata, OpenGraph, Twitter cards and meta tags of the page.
  - `tables(self) -> list[dict[str, Any]]`: Every HTML table as records.
  - `urljoin(self, url: str) -> str`: Resolve a relative URL against this page.
  - `xpath(self, query: str, **kwargs: Any) -> SelectorList`: XPath query on the page.
- **`RobotsPolicyError`** (exception). robots.txt disallows the URL for the configured user agent.
- **`SQLiteStorage(path: str | os.PathLike[str] | None = None)`** (class). Stores fingerprints in a small SQLite database (safe across threads).
  - `close(self)`
  - `delete(self, domain: str, identifier: str)`
  - `identifiers(self, domain: str | None = None) -> list[tuple[str, str]]`: List saved ``(domain, identifier)`` pairs.
  - `load(self, domain: str, identifier: str) -> dict[str, Any] | None`
  - `save(self, domain: str, identifier: str, record: dict[str, Any])`
- **`SchemaError`** (exception). A data schema is malformed (unknown type, bad constraint...).
- **`Selector(text: str | bytes | None = None, *, url: str | None = None, type: str = 'html', root: etree._Element | None = None, adaptive_storage: AdaptiveStorage | None = None)`** (class). A node of an HTML/XML document - or a piece of text pulled out of one.
  - `attr(self, name: str, default: str | None = None) -> str | None`: Value of one attribute, or ``default``.
  - `auto_extract(self, *, min_records: int = 3) -> list[dict[str, Any]]`: Records from the page's main repeating list, with fields inferred automatically.
  - `closest(self, query: str) -> Selector | None`: The nearest ancestor (or self) matching a CSS selector.
  - `css(self, query: str, *, adaptive: bool = False, auto_save: bool = False, identifier: str | None = None, min_score: float = 0.55) -> SelectorList`: Select with a CSS selector.
  - `detect_records(self, *, min_records: int = 3) -> list[Any]`: Repeating record groups on the page (product cards, results, rows), best first.
  - `embedded_json(self) -> dict[str, Any]`: JSON state embedded by JavaScript apps (``__NEXT_DATA__``, ``window.__INITIAL_STATE__``...).
  - `extract(self, schema: Mapping[str, Any]) -> dict[str, Any]`: Pull a dict of values out using a schema of selectors.
  - `extract_all(self, query: str, schema: Mapping[str, Any], **select_kwargs: Any) -> list[dict[str, Any]]`: Run :meth:`extract` on every element matched by ``query``.
  - `find_by_regex(self, pattern: str | Pattern[str], *, tag: str | None = None, flags: int = 0) -> SelectorList`: Elements whose text matches a regular expression.
  - `find_by_text(self, text: str, *, partial: bool = True, case_sensitive: bool = False, tag: str | None = None) -> SelectorList`: Elements whose text matches ``text``.
  - `find_json(self, key: str | Callable[[str], bool], *, limit: int | None = None) -> list[Any]`: Every value stored under ``key`` anywhere in the page's embedded JSON and JSON-LD.
  - `find_similar(self, *, threshold: float = 0.5, ignore_attributes: Iterable[str] = {'action', 'alt', 'content', 'datetime', 'for', 'href', 'id', 'name', 'src', 'srcset', 'style', 'title', 'value'}) -> SelectorList`: Elements structurally similar to this one (other rows, cards...).
  - `get(self, default: str | None = None) -> str | None`: The text value (text selectors) or the outer HTML (elements).
  - `get_text(self) -> str`: Readable text with one line per paragraph/block element.
  - `getall(self) -> list[str]`
  - `learn(self, examples: Mapping[str, str] | list[Mapping[str, str]]) -> Any`: Learn an extraction schema from example values ("scraping by example").
  - `links(self, css: str | None = None, *, allow: str | Iterable[str] | None = None, deny: str | Iterable[str] | None = None, domains: str | Iterable[str] | None = None, same_domain: bool = False, unique: bool = True) -> list[str]`: Absolute http(s) URLs of the links on the page (fragments removed).
  - `markdown(self, *, main_content: bool = False) -> str`: Convert the element to Markdown (links and images made absolute).
  - `next_page(self) -> str | None`: URL of the "next page" link (rel=next, "Next", arrows, numbered pagination...), if any.
  - `re(self, pattern: str | Pattern[str], flags: int = 0) -> list[str]`: Apply a regex to the text and return every match.
  - `re_first(self, pattern: str | Pattern[str], default: str | None = None, flags: int = 0) -> str | None`
  - `remove_namespaces(self)`: Strip XML namespaces so ``//loc`` works on sitemaps and feeds.
  - `select(self, query: str, **kwargs: Any) -> SelectorList`: CSS or XPath, guessed from the query (XPath starts with ``/``, ``./`` or ``(``).
  - `structured_data(self) -> dict[str, Any]`: Machine-readable data the page publishes: JSON-LD, microdata, OpenGraph, Twitter cards, meta tags.
  - `tables(self) -> list[dict[str, Any]]`: Every ``<table>`` as ``{"headers", "rows": [{header: value}], "caption"}`` (colspan/rowspan handled).
  - `urljoin(self, url: str) -> str`: Resolve a (possibly relative) URL against the page URL / ``<base>``.
  - `xpath(self, query: str, *, namespaces: Mapping[str, str] | None = None, adaptive: bool = False, auto_save: bool = False, identifier: str | None = None, min_score: float = 0.55, **variables: Any) -> SelectorList`: Select with XPath 1.0.
- **`SelectorList(iterable=(), /)`** (class). A list of :class:`Selector` objects with the same query helpers.
  - `attr(self, name: str, default: str | None = None) -> str | None`: An attribute of the first element that has it.
  - `attrs(self, name: str) -> list[str]`: An attribute of every element that has it.
  - `css(self, query: str, **kwargs: Any) -> SelectorList`: Run a CSS query on every selector and flatten the results.
  - `filter(self, predicate: Callable[[Selector], bool]) -> SelectorList`
  - `get(self, default: str | None = None) -> str | None`: ``get()`` of the first selector, or ``default`` if the list is empty.
  - `getall(self) -> list[str]`
  - `re(self, pattern: str | Pattern[str], flags: int = 0) -> list[str]`
  - `re_first(self, pattern: str | Pattern[str], default: str | None = None, flags: int = 0) -> str | None`
  - `select(self, query: str, **kwargs: Any) -> SelectorList`
  - `xpath(self, query: str, **kwargs: Any) -> SelectorList`
- **`SelectorSyntaxError`** (exception). A CSS selector or XPath expression could not be parsed.
- **`SessionManager()`** (class). A registry of async fetchers, each with its own cookies and settings.
  - `add(self, name: str, fetcher: Any, *, default: bool = False) -> Any`: Register an :class:`~wintergrab.AsyncFetcher` or :class:`~wintergrab.AsyncBrowserFetcher`.
  - `close_all(self)`
  - `get(self, name: str | None = None) -> Any`
- **`SitemapEntry(loc: str, kind: str = 'url', lastmod: str | None = None, changefreq: str | None = None, priority: float | None = None)`** (class). One ``<url>`` (``kind="url"``) or nested ``<sitemap>`` (``kind="sitemap"``).
- **`Spider(**overrides: Any)`** (class). Base class for crawlers.
  - `arun(self, *, resume: bool = True) -> CrawlResult`: Run the crawl inside an existing event loop.
  - `configure_sessions(self, sessions: SessionManager)`: Register the fetch sessions this spider uses.
  - `fatal(self, error: WintergrabError | Exception)`: Abort the crawl with an error (raised from :meth:`run`).
  - `get_network_policy(self) -> NetworkPolicy | None`: The spider's shared :class:`~wintergrab.netpolicy.NetworkPolicy` (``None`` = no restriction).
  - `http_cache(self) -> HTTPCache | None`: The spider's shared :class:`HTTPCache` (``None`` unless :attr:`cache` is set).
  - `is_blocked(self, response: Response) -> bool`: Decide whether a response is a block/challenge page (retried, and slows the domain down).
  - `metrics(self) -> dict[str, Any]`: Live metrics of the running crawl: rates, latency, per-domain throttle state, budgets (empty when idle).
  - `needs_browser(self, response: Response) -> str | bool | None`: With :attr:`adaptive_fetch`: whether a page fetched over HTTP needs a browser to show its content.
  - `on_close(self, result: CrawlResult) -> Any`: Called once after crawling ends (may be async).
  - `on_error(self, request: Request, error: BaseException) -> Any`: Called when a request finally fails (after retries) and has no errback.
  - `on_start(self) -> Any`: Called once before crawling starts (may be async).
  - `parse(self, response: Response) -> Any`: Default callback.
  - `pause(self)`: Stop gracefully and save state so the crawl can resume (thread-safe).
  - `process_item(self, item: Any) -> Any`: Clean/validate each item.
  - `run(self, *, resume: bool = True) -> CrawlResult`: Run the crawl and block until it finishes, pauses or stops.
  - `start_requests(self) -> Iterable[Request | str] | AsyncIterable[Request | str]`: Initial requests: one per URL in :attr:`start_urls` and :attr:`sitemap_urls`.
  - `stop(self)`: Stop gracefully without keeping the queue (thread-safe).
  - `stream(self, *, resume: bool = True) -> AsyncIterator[Any]`: Run the crawl and yield items as they are scraped::
- **`StorageError`** (exception). Reading or writing stored data failed.
- **`URLNormalizer(*, strip_tracking: bool = True, strip_session_ids: bool = True, remove_fragment: bool = True, keep_hashbang: bool = True, sort_query: bool = True, drop_params: Iterable[str] = (), keep_params: Iterable[str] | None = None, remove_empty_params: bool = False, strip_www: bool = False, remove_trailing_slash: bool = False, remove_index: bool = False, lowercase_path: bool = False, force_https: bool = False)`** (class). Rewrites URLs to one canonical spelling.
  - `classmethod coerce(cls, value: Any) -> Callable[[str], str] | None`: ``None``/``False`` -> no normalizer, ``True`` -> defaults, a dict -> options, a callable -> itself.
- **`URLRules(allow: Sequence[str] = (), deny: Sequence[str] = (), allowed_domains: Sequence[str] = (), denied_domains: Sequence[str] = (), schemes: Sequence[str] = ('http', 'https'), deny_extensions: Collection[str] = {'3gp', '7z', 'aac', 'ai', 'aiff', 'apk', 'asf', 'asx', 'au', 'avi', 'avif', 'bin', 'bmp', 'bz2', 'cdr', 'css', 'deb', 'dmg', 'doc', 'docx', 'drw', 'dxf', 'eot', 'eps', 'exe', 'flac', 'gif', 'gz', 'heic', 'ico', 'iso', 'jar', 'jpeg', 'jpg', 'm4a', 'm4v', 'mid', 'mkv', 'mng', 'mov', 'mp3', 'mp4', 'mpg', 'msi', 'odg', 'odp', 'ods', 'odt', 'ogg', 'otf', 'pct', 'png', 'pps', 'ppt', 'pptx', 'ps', 'psp', 'pst', 'qt', 'ra', 'rar', 'rm', 'rpm', 'rss', 'svg', 'swf', 'tar', 'tgz', 'tif', 'tiff', 'ttf', 'wav', 'webm', 'webp', 'wma', 'wmv', 'woff', 'woff2', 'xls', 'xlsx', 'zip'}, max_url_length: int | None = 2048, max_query_params: int | None = 30, max_path_depth: int | None = 32, max_segment_repeats: int | None = 3)`** (class). Which URLs a crawl may queue.
  - `allows(self, url: str) -> bool`
  - `check(self, url: str, *, sitemap: bool = False) -> str | None`: ``None`` if ``url`` may be queued, else a short reason.
  - `classmethod coerce(cls, value: Any) -> URLRules | None`
- **`ValidationError`** (exception). A record or dataset failed validation.
- **`WinterGrab(*, model: Any = None, browser: bool = False, obey_robots: bool = True, network_policy: Any = None, cache: Any = None, timeout: float = 20.0, log_level: str | None = 'WARNING', **settings: Any)`** (class). Goals, pages and sites with settings shared by all (see the module docs).
  - `arun(self, goal: str | Goal | GoalPlan, output: str | None = None, **options: Any) -> GoalResult`: :meth:`run` from async code (the crawl runs in a thread of its own, with its own event loop).
  - `configure(self, **changes: Any) -> WinterGrab`: A copy with some settings changed: ``wg.configure(browser=True).sources(url)``.
  - `extract(self, page: str | Response, schema: Any, *, all: bool = False) -> ExtractedRecord | list[ExtractedRecord]`: A typed record from a page (a URL, fetched with :meth:`get`, or a :class:`~wintergrab.Response`), with where each value came from and how sure it is: ``schema`` is a template name (``"product"``), a schema file or a :class:`~wintergrab.data.Schema`.
  - `get(self, url: str, **options: Any) -> Response`: One page: over HTTP, or rendered in a browser when this WinterGrab uses one (``browser=True``).
  - `goal(self, text: str, *, sites: list[str] | None = None) -> Goal`: A request in plain words as a :class:`~wintergrab.goals.Goal`, read by the model when there is one.
  - `inspect(self, url: str, *, pages: int = 30) -> SiteSurvey`: A site's robots.txt, sitemaps and ``pages`` pages, with its profile (``survey.profile.describe()``), as ``wintergrab inspect`` reads them.
  - `plan(self, goal: str | Goal, *, sites: list[str] | None = None, sample: int = 30, api: bool = True) -> GoalPlan`: Survey the goal's sites (robots.txt, sitemaps, ``sample`` pages each) and plan the crawl: what to fetch, how, and what it will cost.
  - `run(self, goal: str | Goal | GoalPlan, output: str | None = None, *, sites: list[str] | None = None, sample: int = 30, max_pages: int | None = None, api: bool = True, **settings: Any) -> GoalResult`: Collect a goal's records into ``output`` (``.jsonl``, ``.csv``, a database URL...; kept in ``result.records`` when there is none).
  - `sources(self, page: str | Response) -> DataSources`: Where a page's data is (:func:`~wintergrab.intel.sources.data_sources`): its HTML records, JSON-LD, embedded JSON and, in a browser, the API calls it makes.
- **`WintergrabError`** (exception). Base class for every error raised by wintergrab.
- **`__version__`** = `'0.2.0'`
- **`aget(url: str, **kwargs: Any) -> Response`**. Async :func:`get`.
- **`apost(url: str, **kwargs: Any) -> Response`**. Async :func:`post`.
- **`arender(url: str, **kwargs: Any) -> Response`**. Async :func:`render`.
- **`configure_logging(level: int | str = 'INFO', *, fmt: str | None = None)`**. Send wintergrab's log messages to stderr (idempotent).
- **`get(url: str, **kwargs: Any) -> Response`**. Fetch one page over HTTP, looking like Chrome.
- **`normalize_url(url: str, **options: Any) -> str`**. Normalize one URL (see :class:`URLNormalizer` for the options).
- **`parse(markup: str | bytes, url: str | None = None, *, type: str = 'html', **kwargs) -> wintergrab.parser.selector.Selector`**. Parse HTML (or XML with ``type="xml"``) into a :class:`Selector`.
- **`post(url: str, **kwargs: Any) -> Response`**. POST with ``data=`` (form) or ``json=``.
- **`render(url: str, **kwargs: Any) -> Response`**. Load a page in a headless browser and return the rendered HTML::
- **`sitemap(url: str, *, follow: bool = True, max_sitemaps: int = 200, since: str | datetime | None = None, **fetch_options: object) -> list[SitemapEntry]`**. Every page URL listed in a sitemap (following sitemap indexes).
- **`url_template(url: str, *, include_host: bool = True, include_query: bool = True) -> str`**. The route pattern of a URL.

## `wintergrab.fetchers.actions`: Browser actions

- **`VERBS`**: a dict
- **`Action(verb: str, target: str = '', value: str | None = None, repeat: int = 1, until_gone: bool = False, optional: bool = False)`** (class). One step (see the module docs).
  - `describe(self) -> str`: The step as :func:`str` writes it, with what a ``fill`` types left out (``fill #password => ***``): how it appears in ``response.actions``, logs and errors, which may be kept where a password must not be.
- **`ActionsResult(log: list[dict[str, Any]] = ..., snapshots: list[dict[str, str]] = ..., downloads: list[dict[str, Any]] = ...)`** (class). What the steps did: a log, and what they kept.
- **`load_actions(path: str | Path) -> list[Action]`**. Steps from a JSON or YAML file holding a list of them.
- **`parse_actions(steps: Iterable[str | Mapping[str, Any]] | str | Mapping[str, Any]) -> list[Action]`**. Steps (strings or one-key mappings; see the module docs) as :class:`Action` s.
- **`run_actions(page: Any, actions: Sequence[Action], *, timeout: float = 30.0, downloads: str | Path | None = None) -> ActionsResult`**. Do ``actions`` on a Playwright page, in order (see the module docs); ``timeout`` in seconds per step.

## `wintergrab.spider`: Crawling

`AutoThrottle`: see [`wintergrab`](#wintergrab-fetching-parsing-and-the-most-used-names).

- **`CrawlOptimizer(path: str | os.PathLike[str] | None = None, *, min_pages: int = 20, probe_every: int = 10, depth: int = 2, prioritize: bool = True, prune: bool = True, parameters: bool = True)`** (class). Learns which URL patterns give items and acts on it (see the module docs).
  - `boost(self, url: str) -> int`: Priority to add to a request of ``url``: 20 when its pattern's pages hold items, 10 when they lead to pages that do, 0 otherwise (or not known yet).
  - `classmethod coerce(cls, value: Any, *, crawl_dir: str | os.PathLike[str] | None = None) -> CrawlOptimizer | None`: A spider's ``optimize`` setting: ``True``, a file path or an optimizer.
  - `describe(self, limit: int = 12) -> str`: The patterns seen most: what their pages gave, and what the optimizer does with them.
  - `discard(self, request: Request)`: ``request`` was not queued after all (a duplicate): forget :meth:`enqueued`.
  - `done(self, request: Request)`: ``request`` is finished (its page processed, or given up on): once, whatever the attempts.
  - `duplicate(self, url: str) -> bool`: Whether ``url``'s page was fetched already under another address: with a query parameter found to change nothing on its pattern's pages.
  - `enqueued(self, request: Request, parent: Request | None)`: ``request`` is about to be queued (``parent``: the request whose page led to it).
  - `forecast(self) -> dict[str, float]`: What the queue should still give: requests queued, and items expected from them (at each pattern's rate so far; the pages they lead to are not counted).
  - `load(self, path: str | os.PathLike[str])`: Add what a :meth:`save` file says (a missing or unreadable file is an error).
  - `page(self, request: Request, response: Any, items: int)`: ``request``'s page was fetched and its callback yielded ``items`` (kept) items.
  - `pattern(self, url: str) -> str`: ``url``'s pattern: :func:`~wintergrab.urls.url_template`, with the words of a place in the path that has more than five (below the first segment) counted as one: ``shop.example/tag/{word}``.
  - `rewrite(self, url: str) -> str`: ``url`` without the query parameters found to change nothing on its pattern's pages.
  - `save(self, path: str | os.PathLike[str] | None = None)`: Write what was learned to ``path`` (default: the file it was loaded from), atomically.
  - `skip(self, request: Request, parent: Request | None = None) -> bool`: Whether to leave ``request`` out: its pattern is barren (and it is not a probe).
  - `to_dict(self) -> dict[str, Any]`
`CrawlResult`: see [`wintergrab`](#wintergrab-fetching-parsing-and-the-most-used-names).

`DropItem`: see [`wintergrab`](#wintergrab-fetching-parsing-and-the-most-used-names).

- **`FailureDiagnosis(domain: str, signature: str, category: str, attempts: int, failed_urls: int, affected_urls: int, sample_urls: list[str], first_seen: float, last_seen: float, last_success: float | None, confirmed_cause: str | None, likely_cause: str | None, evidence: list[str], crawler_state: str)`** (class). One kind of failure on one domain, with what is known and what is guessed.
  - `describe(self) -> str`: A multi-line, human-readable report.
  - `to_dict(self) -> dict[str, Any]`
`IgnoreRequest`: see [`wintergrab`](#wintergrab-fetching-parsing-and-the-most-used-names).

`ItemPipeline`: see [`wintergrab`](#wintergrab-fetching-parsing-and-the-most-used-names).

`SessionManager`: see [`wintergrab`](#wintergrab-fetching-parsing-and-the-most-used-names).

`Spider`: see [`wintergrab`](#wintergrab-fetching-parsing-and-the-most-used-names).

- **`open_exporter(path: str | os.PathLike[str], *, append: bool = False, unique_key: str | None = None) -> Exporter`**. Pick an exporter by the output's extension (``.jsonl``, ``.json``, ``.csv``, ``.sqlite``/``.db``, ``.parquet``, ``.xlsx``) or URL scheme (``postgresql://``, ``mysql://``, ``mongodb://``, ``s3://``).
- **`write_items(path: str | os.PathLike[str], items: list[Any]) -> Path`**. Write a list of items in one go (format chosen by extension).

## `wintergrab.extraction`: Typed records from pages

- **`DEFAULT_PRIORS`**: a dict
- **`STRATEGIES`**: a list
- **`Candidate(raw: Any, method: str, source: str, factor: float = 1.0, detail: str = '', value: Any = None, notes: list[str] = ..., confidence: float = 0.0)`** (class). A value one strategy found for one field.
- **`DomHeuristics()`** (class). What pages usually look like: the ``<h1>`` is the title, an element classed "price" holds the price, ``<time datetime>`` a date, ``mailto:``/``tel:`` links contact details...
  - `candidates(self, page: PageContext, f: SchemaField, schema: Schema) -> list[Candidate]`
- **`EmbeddedJson()`** (class). Values in the JSON state that JavaScript apps embed in their HTML (``__NEXT_DATA__``...).
  - `candidates(self, page: PageContext, f: SchemaField, schema: Schema) -> list[Candidate]`
- **`ExtractedRecord(data: dict[str, Any], fields: dict[str, FieldValue], url: str | None, fetched_at: float, schema: str, issues: list[Issue] = ..., model: str | None = None, provenance_default: bool = False)`** (class). A record plus, for every field, its provenance and confidence.
  - `explain(self) -> str`: A table of every field: value, method, confidence and evidence.
  - `get(self, name: str, default: Any = None) -> Any`
  - `provenance(self) -> dict[str, Any]`: Where each value came from: the page, when, which extractor, and per-field evidence.
  - `to_dict(self, *, provenance: bool | None = None, confidence: bool = True) -> dict[str, Any]`
- **`ExtractionModel(*args, **kwargs)`** (class). Anything that turns a :class:`ModelRequest` into ``{field: value}`` (plain or ``async``).
- **`Extractor(schema: Schema | Mapping[str, Any] | str | Path, *, strategies: Sequence[type[Strategy] | Strategy] | None = None, model: Any = None, model_threshold: float = 0.5, vision: bool = False, min_confidence: float = 0.3, priors: Mapping[str, float] | None = None, provenance: bool = False, country: str | None = None, currency: str | None = None, dayfirst: bool | None = None, decimal: str | None = None)`** (class). Extract typed records from pages with the strategy hierarchy.
  - `aextract(self, page: Any, *, url: str | None = None) -> ExtractedRecord`: :meth:`extract` with an asynchronous model (works with a plain one too).
  - `calibrate(self, examples: Iterable[tuple[Any, Mapping[str, Any]]], *, min_samples: int = 5) -> dict[str, float]`: Measure how often each method is right on pages whose correct values you know.
  - `candidates(self, page: Any, *, url: str | None = None) -> dict[str, list[Candidate]]`: Every candidate each strategy found, per field (for debugging an extraction).
  - `explain(self, page: Any, *, url: str | None = None) -> str`: :meth:`ExtractedRecord.explain` of the page's record.
  - `extract(self, page: Any, *, url: str | None = None) -> ExtractedRecord`: One record from a page (a :class:`~wintergrab.Response`, a :class:`~wintergrab.Selector` or HTML).
  - `extract_all(self, page: Any, *, container: str | None = None, min_records: int = 2, url: str | None = None) -> list[ExtractedRecord]`: Every record of a listing page.
  - `why(self, name: str, page: Any, *, url: str | None = None) -> FieldDiagnosis`: Why field ``name`` is what it is on ``page``, or why it is empty: what each strategy saw, and the likely causes, each saying how sure it is (see :mod:`~wintergrab.extraction.explain`).
- **`ExtractorVersion(number: int, reason: str, by: str, status: str, parent: int | None = None, created: float = ..., validation: dict[str, Any] = ...)`** (class). One version of an extractor's schema.
- **`ExtractorVersions(directory: str | os.PathLike[str], schema: Schema | Mapping[str, Any] | str | Path | None = None)`** (class). An extractor's versions, repair log and fixtures, in a directory (see the module docs).
  - `activate(self, number: int, *, reason: str, by: str = 'human') -> ExtractorVersion`: Make version ``number`` the active one.
  - `add(self, schema: Schema, *, reason: str, by: str, status: str = 'active', validation: dict[str, Any] | None = None) -> ExtractorVersion`: Save ``schema`` as a new version; ``status="active"`` makes it the one in use.
  - `add_fixture(self, url: str, html: bytes | str, expected: Mapping[str, Any], *, by: str = 'human') -> Fixture`: Keep a page and values confirmed on it: later repairs must reproduce them.
  - `check_fixtures(self, schema: Schema | None = None) -> list[str]`: Regression test: where ``schema`` (the active version by default) does not reproduce a fixture.
  - `diff(self, old: int, new: int) -> list[str]`: What changed between two versions, field by field (selectors and types).
  - `fixtures(self) -> list[Fixture]`
  - `get(self, number: int) -> ExtractorVersion`
  - `history(self) -> list[dict[str, Any]]`: The repair log, oldest first.
  - `load_state(self) -> dict[str, Any]`: What a :class:`HealingExtractor` learned in earlier runs (baselines, matched elements).
  - `log(self, entry: dict[str, Any])`: Add an entry to the repair log.
  - `mark(self, number: int, status: str)`: Set a version's status (``"accepted"``: a candidate whose change went into a later version).
  - `reject(self, number: int, *, reason: str, by: str = 'human')`
  - `revert(self, number: int, name: str, *, reason: str, by: str = 'auto') -> ExtractorVersion`: Undo what version ``number`` changed in field ``name``: the active version goes back to the one it was made from (:meth:`rollback`); an older one is undone by a new version that keeps the changes made since.
  - `rollback(self, *, reason: str, by: str = 'auto') -> ExtractorVersion`: Go back to the version the active one was made from (it is marked "rolled back").
  - `save_state(self, state: Mapping[str, Any])`
  - `schema(self, number: int | None = None) -> Schema`
- **`FieldDiagnosis(field: str, type: str, url: str | None, value: Any, status: str, seen: list[str] = ..., causes: list[str] = ..., candidates: list[dict[str, Any]] = ...)`** (class). Why a field is what it is on a page (see the module docs).
  - `describe(self) -> str`: The field and its value (or why it has none): what was seen, then why.
  - `to_dict(self) -> dict[str, Any]`
- **`FieldValue(name: str, value: Any = None, raw: Any = None, method: str | None = None, source: str | None = None, confidence: float = 0.0, agreed: list[str] = ..., alternatives: list[dict[str, Any]] = ..., notes: list[str] = ..., validation: str = 'absent')`** (class). One field of an extracted record, with where it came from and how sure we are.
  - `to_dict(self) -> dict[str, Any]`
- **`GeneratedSchema(schema: Schema, base: Schema, fields: dict[str, LearnedField], urls: list[str | None], values: list[dict[str, Any]], methods: list[dict[str, str]], model: str | None = None, usage: dict[str, int] = ...)`** (class). A schema generated from sample pages (see the module docs).
  - `describe(self) -> str`: Each field: its selector and on how many pages it read the value, or why it has none.
  - `expected(self, index: int) -> dict[str, Any]`: The values the generated schema must read on sample page ``index``, without a model: those learned from, but a model's answers for fields no selector was learned for.
  - `to_dict(self) -> dict[str, Any]`
- **`HealingExtractor(directory: str | os.PathLike[str], schema: Schema | Mapping[str, Any] | str | Path | None = None, *, review: ReviewQueue | str | os.PathLike[str] | None = None, auto_apply: float = 0.9, min_pages: int = 10, drop: float = 0.5, keep: int = 12, review_below: float = 0.5, max_value_reviews: int = 20, **extractor_options: Any)`** (class). An :class:`~wintergrab.extraction.Extractor` that repairs its selectors (see the module docs).
  - `apply_reviews(self) -> int`: Act on the review queue's decisions about this extractor: accepted repairs become the active version, rejected ones are marked so; chosen or corrected values become fixtures.
  - `close(self)`
  - `extract(self, page: Any, *, url: str | None = None) -> ExtractedRecord`: Extract a record (like :meth:`Extractor.extract`), watching the fields and healing them.
  - `extract_all(self, page: Any, **options: Any) -> list[ExtractedRecord]`: Records of a listing (not watched: listings are :meth:`extract`'s job for detail pages).
  - `repair(self, name: str) -> RepairResult`: Look for a replacement of ``name``'s selectors, test it, and apply it, queue it, or give up.
  - `save(self)`: Keep what was learned (baselines, matched elements) for the next run; done every 50 pages, when a baseline is learned, and on :meth:`close`.
  - `status(self) -> str`: The versions, each watched field's health, and the last repairs.
  - `why(self, name: str, page: Any | None = None) -> str`: Why ``name`` is what it is (or empty) on ``page``: what each strategy saw and the likely causes (:meth:`Extractor.why`), then the extractor's own story: the element most like the one the selectors used to match, how often they matched, and repairs made or waiting.
- **`LabelledPair(label: str, value: str, how: str, close: bool = False, boxes: tuple[Box, ...] = ())`** (class). A label and its value, read from where they are drawn.
- **`LabelledValues()`** (class). Values next to a label named like the field: ``<dt>Weight</dt><dd>1.2 kg</dd>``, ``SKU: AB-12``, spec tables.
  - `candidates(self, page: PageContext, f: SchemaField, schema: Schema) -> list[Candidate]`
- **`LearnedField(name: str, status: str = 'not found', selector: str | None = None, found_by: str | None = None, pages: int = 0, reproduced: int = 0, extra: int = 0, tried: int = 0, note: str = '')`** (class). What :func:`generate_schema` did for one field.
  - `to_dict(self) -> dict[str, Any]`
- **`MetaTags()`** (class). OpenGraph, Twitter card and ``<meta>`` values (``og:title``, ``product:price:amount``...).
  - `candidates(self, page: PageContext, f: SchemaField, schema: Schema) -> list[Candidate]`
- **`ModelField(name: str, type: str, description: str = '', required: bool = False, many: bool = False, enum: tuple[Any, ...] = ())`** (class). A field the model is asked for.
  - `classmethod of(cls, f: SchemaField) -> ModelField`
- **`ModelRequest(fields: list[ModelField], text: str, url: str | None = None, schema_name: str = 'record', known: dict[str, Any] = ..., images: list[Image] = ...)`** (class). What an extraction model gets: the fields wanted and the page's content (Markdown), and a screenshot of the page when the extractor was asked to send one (``Extractor(vision=True)``).
  - `prompt(self) -> str`: A ready-made instruction for chat models (use it or build your own from the attributes).
- **`PageContext(source: Any, *, url: str | None = None, fetched_at: float | None = None, scope: Selector | None = None, parent: PageContext | None = None)`** (class). A page (a :class:`~wintergrab.Response`, a :class:`~wintergrab.Selector` or HTML) ready for extraction.
  - `iter_nodes(self, kind: str) -> Iterator[tuple[str, dict[str, Any]]]`: ``(path, node)`` for every typed object in the page's JSON-LD (``kind="json-ld"``) or microdata.
  - `nodes(self, kind: str) -> list[tuple[str, dict[str, Any]]]`: :meth:`iter_nodes` as a list, computed once per page.
  - `scoped(self, element: Selector) -> PageContext`: The same page, looking only at ``element`` (one record of a listing).
- **`Patterns()`** (class). Regular expressions over the visible text, for values with a recognisable shape.
  - `candidates(self, page: PageContext, f: SchemaField, schema: Schema) -> list[Candidate]`
- **`RecordFields(fields: dict[str, str])`** (class). For a record of a listing found by :func:`~wintergrab.parser.autoextract.detect_records`: its inferred fields.
  - `candidates(self, page: PageContext, f: SchemaField, schema: Schema) -> list[Candidate]`
- **`RepairResult(field: str, applied: bool = False, queued: bool = False, selector: str | None = None, confidence: float = 0.0, version: int | None = None, checked: dict[str, Any] = ..., candidates: list[dict[str, Any]] = ...)`** (class). What a repair attempt found and did.
- **`ReviewItem(id: str, kind: str, field: str, url: str | None = None, candidates: list[dict[str, Any]] = ..., details: dict[str, Any] = ..., status: str = 'pending', created: float = ..., decision: dict[str, Any] | None = None, html: str | None = None)`** (class). Something to decide (see the module docs).
  - `describe(self) -> str`
  - `to_dict(self) -> dict[str, Any]`
- **`ReviewQueue(path: str | os.PathLike[str])`** (class). The items waiting for a decision, and the decisions (see the module docs).
  - `add(self, kind: str, field: str, *, url: str | None = None, candidates: list[dict[str, Any]] | None = None, details: dict[str, Any] | None = None, html: bytes | str | None = None) -> ReviewItem`: Queue an item; returns it (with its ``id``).
  - `decide(self, item_id: str, decision: str, *, choice: int | None = None, value: Any = None, by: str = 'human', note: str = '') -> ReviewItem`: Record a decision: ``"accept"`` (candidate ``choice``, the first by default), ``"reject"``, or ``"correct"`` (with the right ``value``).
  - `get(self, item_id: str) -> ReviewItem`
  - `items(self, *, status: str | None = None, kind: str | None = None) -> list[ReviewItem]`
  - `pending(self, kind: str | None = None) -> list[ReviewItem]`
- **`Selectors()`** (class). The CSS/XPath selectors given in the schema (``selectors=[".price", "[itemprop=price]::attr(content)"]``).
  - `candidates(self, page: PageContext, f: SchemaField, schema: Schema) -> list[Candidate]`
- **`Strategy()`** (class). Base class: ``candidates(page, field, schema)`` returns what this strategy finds for one field.
  - `candidates(self, page: PageContext, f: SchemaField, schema: Schema) -> list[Candidate]`
- **`StructuredData(node: tuple[str, dict[str, Any]] | None = None, kind: str | None = None)`** (class). JSON-LD and microdata (schema.org): what the site publishes for machines.
  - `candidates(self, page: PageContext, f: SchemaField, schema: Schema) -> list[Candidate]`
- **`VisualLayout()`** (class). Values beside, under or over a label named like the field, where the page draws them (a page fetched in a browser with ``layout=True``; see the module docs).
  - `candidates(self, page: PageContext, f: SchemaField, schema: Schema) -> list[Candidate]`
- **`VisualTable(header: list[str] | None, rows: list[list[str]], path: str, top: float, left: float, bottom: float, right: float, boxes: list[Box] = ...)`** (class). A table read from a layout.
  - `records(self) -> list[dict[str, str]]`: The rows as records, keyed by the header (``column_1``...
  - `to_dict(self) -> dict[str, Any]`
- **`field_kind(f: SchemaField) -> str`**. What a field holds, for the heuristic strategies (see :func:`_kind`).
- **`generate_schema(pages: Iterable[Any], schema: Schema | Mapping[str, Any] | str | Path = 'product', *, model: Any = None, min_confidence: float = 0.7, max_tries: int = 12) -> GeneratedSchema`**. Learn selectors for ``schema``'s fields from sample pages of one site (see the module docs).
- **`grounding(raw: Any, page_text: str, page_numbers: set[str] | None = None) -> str`**. How firmly a model's value is supported by the page.
- **`layout_pairs(layout: Layout) -> list[LabelledPair]`**. The labelled values drawn on the page (see the module docs).
- **`layout_tables(layout: Layout, *, min_rows: int = 3, min_columns: int = 2) -> list[VisualTable]`**. The tables drawn on the page (see the module docs), in the order they are drawn.
- **`register_strategy(strategy: type[Strategy], *, before: str | None = None)`**. Add a strategy extractors use by default: last, or before the one whose ``method`` is ``before``.
- **`schema_types(node: Any) -> list[str]`**. The schema.org type names of a JSON-LD/microdata node (``"https://schema.org/Product"`` -> ``"Product"``).
- **`value_key(value: Any) -> Any`**. What two values must share to count as the same (``$299.99`` = ``299.99``; case and spacing ignored).

## `wintergrab.extraction.templates`: Ready-made schemas

- **`ALIASES`**: a dict
- **`schema_named(value: str | Path) -> Schema`**. A schema file, or, when no such file exists, the template of that name (``"product"``).
- **`template(name: str) -> Schema`**. The extraction schema of template ``name`` (see the module docs).
- **`template_names() -> list[str]`**. The templates there are.

## `wintergrab.parser.layout`: Rendered layouts

- **`LAYOUT_SCRIPT`**: a str
- **`MAX_BOXES`** = `5000`
- **`Box(text: str, x: float, y: float, width: float, height: float, lines: int = 1, size: float = 0.0, weight: int = 400, tag: str = '', path: str = '')`** (class). A piece of visible text and where it was drawn (CSS pixels, from the page's top left).
  - `to_dict(self) -> dict[str, Any]`
- **`Layout(boxes: list[Box] = ..., width: float = 0.0, height: float = 0.0, truncated: bool = False)`** (class). A page's rendered layout: its text boxes in document order, and the page's size.
  - `find(self, text: str) -> list[Box]`: The boxes whose text is ``text`` (ignoring case and surrounding spaces).
  - `classmethod from_dict(cls, data: Mapping[str, Any]) -> Layout`: A layout from :meth:`to_dict` (or what :data:`LAYOUT_SCRIPT` returns).
  - `to_dict(self) -> dict[str, Any]`
  - `within(self, path: str) -> Layout`: The boxes inside the element at ``path`` (a record's card).
- **`element_path(element: Any) -> str`**. The CSS path of a parsed element (lxml) from ``body``, written as :data:`LAYOUT_SCRIPT` writes paths: a tag, with ``:nth-of-type(n)`` when its parent has other children of that tag.

## `wintergrab.parser.pdf`: PDFs

- **`MAX_PAGES`** = `100`
- **`PdfDocument(pages: list[PdfPage], page_count: int, title: str | None = None, author: str | None = None, subject: str | None = None, truncated: bool = False)`** (class). A PDF, read (see the module docs).
  - `html(self) -> str`: The document as simple HTML: a section per page, headings by font size, lines as paragraphs, the tables its lines are drawn as, and its links.
  - `layout(self) -> Layout`: Every page's boxes on one layout, the pages one under the other.
- **`PdfPage(number: int, width: float, height: float, boxes: list[Box] = ..., links: list[tuple[str, float, float, float, float]] = ...)`** (class). A page of a PDF: its size (points) and its text boxes (from the page's top left), and links.
- **`is_pdf(body: bytes, content_type: str = '') -> bool`**. Whether a response holds a PDF: its type says so, or its body starts like one.
- **`read_pdf(data: bytes, *, max_pages: int = 100) -> PdfDocument`**. Read a PDF's pages (at most ``max_pages``): their text where it is drawn, and their links.

## `wintergrab.bench`: Benchmarks

- **`SCENARIOS`** = `('startup', 'crawl', 'parse', 'extract', 'data', 'dedupe', 'outputs', 'browser')`
- **`run_benchmark(*, scenarios: Sequence[str] = ('startup', 'crawl', 'parse', 'extract', 'data', 'dedupe', 'outputs'), pages: int = 100, items: int = 1000, latency: float = 0.0, concurrency: int = 32, rounds: int = 200, startup_runs: int = 5, stores: Sequence[str] = (), on_result: Callable[[str, dict[str, Any]], None] | None = None) -> dict[str, Any]`**. Run the scenarios (see the module docs) and return ``{"environment": {...}, "results": {name: {...}}}``.
- **`serve_shop(pages: int, items: int, *, latency: float = 0.0, port: int = 0)`**. Serve the shop from 127.0.0.1 until stopped, printing its address first (``latency``: seconds per response).

## `wintergrab.data`: Schemas, normalizing, validating, pipelines, quality

- **`FIELD_TYPES`**: a dict
- **`FUNCTIONS`**: a dict
- **`OPERATIONS`**: a dict
- **`Analyze(field: str, *, add: Sequence[str] = ('language', 'keywords', 'words', 'reading_minutes'), prefix: str = '', keywords: int = 8, name: str | None = None)`** (class). Add what a text field says of itself: its language, keywords and size, with no model (:mod:`wintergrab.intel.content`).
  - `apply(self, record: dict[str, Any], ctx: RecordContext) -> dict[str, Any] | None`: Return the record (changed in place, or a new dict), or ``None`` to drop it (set ``ctx.reason``).
  - `classmethod from_config(cls, options: Any, loader: ConfigLoader) -> Analyze`
  - `to_config(self) -> dict[str, Any]`: The stage's configuration-file form: ``{kind: options}``.
- **`Classify(field: str, model: Any, *, categories: Sequence[str] | None = None, add: Sequence[str] | None = None, prefix: str = '', name: str | None = None)`** (class). Ask a model for a text field's topic, category (one of ``categories``), sentiment and entities, and add them, checked (:func:`wintergrab.intel.content.classify_text`).
  - `apply(self, record: dict[str, Any], ctx: RecordContext) -> dict[str, Any] | None`: Return the record (changed in place, or a new dict), or ``None`` to drop it (set ``ctx.reason``).
  - `classmethod from_config(cls, options: Any, loader: ConfigLoader) -> Classify`
  - `to_config(self) -> dict[str, Any]`: The stage's configuration-file form: ``{kind: options}``.
- **`Compute(fields: str | Mapping[str, Any], expression: Any = None, *, on_error: str = 'null', name: str | None = None)`** (class). Set fields from expressions or functions of the record.
  - `apply(self, record: dict[str, Any], ctx: RecordContext) -> dict[str, Any] | None`: Return the record (changed in place, or a new dict), or ``None`` to drop it (set ``ctx.reason``).
  - `classmethod from_config(cls, options: Any, loader: ConfigLoader) -> Compute`
  - `to_config(self) -> dict[str, Any]`: The stage's configuration-file form: ``{kind: options}``.
- **`ConfigLoader(base_dir: str | Path | None = None, *, schemas: Mapping[str, Schema] | None = None, allow_imports: bool = False)`** (class). Resolves what stage options refer to: schema files and names, tables, functions.
  - `function(self, reference: str) -> Callable[..., Any]`: Import ``"package.module:attribute"`` (only with ``allow_imports``).
  - `path(self, value: str | Path) -> Path`
  - `schema(self, spec: Any) -> tuple[Schema, Any]`: ``(schema, reference)``: files are loaded once, so stages naming the same file share it.
- **`ConvertCurrency(fields: str | Sequence[str], *, to: str, rates: Mapping[str, float | str | Decimal], currency_field: str = 'currency', on_error: str = 'keep', name: str | None = None)`** (class). Convert money amounts to one currency, with exchange rates you supply (none are fetched).
  - `apply(self, record: dict[str, Any], ctx: RecordContext) -> dict[str, Any] | None`: Return the record (changed in place, or a new dict), or ``None`` to drop it (set ``ctx.reason``).
  - `classmethod from_config(cls, options: Any, loader: ConfigLoader) -> ConvertCurrency`
  - `to_config(self) -> dict[str, Any]`: The stage's configuration-file form: ``{kind: options}``.
- **`DatasetDiff(added: list[dict[str, Any]] = ..., removed: list[dict[str, Any]] = ..., changed: list[RecordChange] = ..., unchanged: int = 0, stats: dict[str, int] = ...)`** (class). The differences between two datasets (see :func:`diff_records`).
  - `describe(self, limit: int = 10) -> str`
  - `fields(self) -> dict[str, dict[str, Any]]`: Per field: how many records it changed in and how.
  - `rows(self) -> Iterator[dict[str, Any]]`: The differences as flat rows (for a JSON Lines or CSV file): one per added or removed record, one per changed field.
  - `summary(self) -> str`
  - `to_dict(self) -> dict[str, Any]`
- **`DatasetVersions(directory: str | os.PathLike[str], *, key: str | Sequence[str] | None = None)`** (class). A directory of versions of a dataset: ``v1.jsonl.gz``, ``v2.jsonl.gz``...
  - `commit(self, records: Iterable[Mapping[str, Any]], *, message: str | None = None, key: str | Sequence[str] | None = None, force: bool = False) -> Version`: Save ``records`` as the next version, and compare it with the previous one.
  - `diff(self, a: int | str = 'previous', b: int | str = 'latest', **options: Any) -> DatasetDiff`: The differences between two versions (by default the last two).
  - `get(self, ref: int | str) -> Version`: A version by number (``3``), name (``"v3"``), ``"latest"`` or ``"previous"``.
  - `load(self, ref: int | str = 'latest') -> list[dict[str, Any]]`: The records of a version.
- **`Deduplicate(key: str | Sequence[str] | None = None, *, fields: Sequence[str] | None = None, near: bool = False, text_fields: Sequence[str] | None = None, similarity: float = 0.8, mark: bool = False, name: str | None = None)`** (class). Drop (or mark) duplicate records; see :class:`~wintergrab.data.dedupe.Deduplicator`.
  - `apply(self, record: dict[str, Any], ctx: RecordContext) -> dict[str, Any] | None`: Return the record (changed in place, or a new dict), or ``None`` to drop it (set ``ctx.reason``).
  - `details(self) -> str`: A short note for :meth:`Pipeline.describe`.
  - `classmethod from_config(cls, options: Any, loader: ConfigLoader) -> Deduplicate`
  - `to_config(self) -> dict[str, Any]`: The stage's configuration-file form: ``{kind: options}``.
- **`Deduplicator(key: str | Sequence[str] | None = None, *, fields: Sequence[str] | None = None, near: bool = False, text_fields: Sequence[str] | None = None, similarity: float = 0.8, mark: bool = False)`** (class). Drop (or mark) duplicate records.
  - `check(self, record: Mapping[str, Any]) -> tuple[str, int] | None`: ``(kind, index of the first record it duplicates)`` or ``None``; remembers new records.
  - `process_item(self, item: Any, spider: Any = None) -> Any`
  - `run(self, records: Iterable[Mapping[str, Any]]) -> list[Any]`: De-duplicate a list (the first occurrence is kept).
- **`Enrich(fn: Callable[[dict[str, Any]], Any], *, overwrite: bool = True, on_error: str = 'keep', name: str | None = None)`** (class). Merge in the fields a function returns: ``Enrich(lambda r: {"brand": brand_of(r["name"])})``.
  - `aapply(self, record: dict[str, Any], ctx: RecordContext) -> dict[str, Any] | None`: The asynchronous version of :meth:`apply` (override it for stages that wait on I/O).
  - `apply(self, record: dict[str, Any], ctx: RecordContext) -> dict[str, Any] | None`: Return the record (changed in place, or a new dict), or ``None`` to drop it (set ``ctx.reason``).
  - `classmethod from_config(cls, options: Any, loader: ConfigLoader) -> Enrich`
  - `to_config(self) -> dict[str, Any]`: The stage's configuration-file form: ``{kind: options}``.
- **`Entity(id: str, kind: str, name: str, mentions: list[Mention], confidence: float, links: list[Match] = ...)`** (class). One real-world thing and every mention of it.
  - `explain(self) -> str`
  - `to_dict(self, *, mentions: bool = True) -> dict[str, Any]`
- **`EntityResolver(kind: str = 'company', *, merge_threshold: float = 0.95, review_threshold: float = 0.5, max_block: int = 300)`** (class). Groups mentions of names into entities, with evidence (see the module documentation).
  - `add(self, name: Any, *, source: str | None = None, **attributes: Any) -> Mention | None`: Add a mention of ``name``, seen at ``source``, with what else is known about it.
  - `add_records(self, records: Iterable[Mapping[str, Any]], name_field: str, *, source_field: str | None = None, attributes: Mapping[str, str] | Iterable[str] = ()) -> list[Mention | None]`: Add one mention per record: the name in ``name_field`` (a dotted path works), the source in ``source_field``, and ``attributes`` (``{"website": "brand_url"}``, or field names that are attribute names).
  - `compare(self, a: Mention | str, b: Mention | str) -> Match`: Compare two mentions (or names) and explain the score.
  - `resolve(self) -> Resolution`: Group the mentions into entities; list the uncertain pairs for review.
- **`Exclude(fields: str | Sequence[str], *, name: str | None = None)`** (class). Remove fields: ``Exclude(["html", "_raw*"])`` (patterns allowed).
  - `apply(self, record: dict[str, Any], ctx: RecordContext) -> dict[str, Any] | None`: Return the record (changed in place, or a new dict), or ``None`` to drop it (set ``ctx.reason``).
  - `classmethod from_config(cls, options: Any, loader: ConfigLoader) -> Exclude`
  - `to_config(self) -> dict[str, Any]`: The stage's configuration-file form: ``{kind: options}``.
- **`Expression(source: str)`** (class). A compiled expression.
  - `evaluate(self, record: Mapping[str, Any] | None = None, /, **values: Any) -> Any`: Evaluate against ``record``; keyword arguments add (or override) names.
- **`FieldQuality(name: str, present: int = 0, empty: int = 0, errors: int = 0, warnings: int = 0, placeholders: int = 0, damaged_text: int = 0, kinds: Counter[str] = ..., shapes: Counter[str] = ..., values: Counter[str] = ..., distinct: int = 0, numeric: list[float] = ..., numeric_seen: int = 0, examples: list[Any] = ...)`** (class). What was observed about one field.
  - `completeness(self, records: int) -> float`
  - `consistency(self) -> float | None`
  - `to_dict(self, records: int) -> dict[str, Any]`
  - `validity(self) -> float | None`
- **`FieldResult(raw: Any, value: Any, ok: bool, notes: list[str] = ...)`** (class). How normalizing one field went.
- **`Filter(condition: Any, *, keep: bool = True, on_error: str = 'drop', name: str | None = None)`** (class). Keep the records a condition holds for: ``Filter("price > 0 and availability == 'in_stock'")``.
  - `apply(self, record: dict[str, Any], ctx: RecordContext) -> dict[str, Any] | None`: Return the record (changed in place, or a new dict), or ``None`` to drop it (set ``ctx.reason``).
  - `classmethod from_config(cls, options: Any, loader: ConfigLoader) -> Filter`
  - `to_config(self) -> dict[str, Any]`: The stage's configuration-file form: ``{kind: options}``.
- **`Issue(field: str | None, code: str, message: str, severity: str = 'error', value: Any = None)`** (class). Something wrong (or suspicious) with a value, a record or a dataset.
  - `to_dict(self) -> dict[str, Any]`
- **`Locate(*, add: Sequence[str] = ('country', 'region', 'city', 'postal_code', 'coordinates'), prefix: str = '', country: str | None = None, fields: Mapping[str, str | Sequence[str]] | None = None, name: str | None = None)`** (class). Add where a record is: read from its address, location, city, region, postal code, country, coordinates and map link fields, and normalized (:func:`wintergrab.data.places.place_of`).
  - `apply(self, record: dict[str, Any], ctx: RecordContext) -> dict[str, Any] | None`: Return the record (changed in place, or a new dict), or ``None`` to drop it (set ``ctx.reason``).
  - `details(self) -> str`: A short note for :meth:`Pipeline.describe`.
  - `classmethod from_config(cls, options: Any, loader: ConfigLoader) -> Locate`
  - `to_config(self) -> dict[str, Any]`: The stage's configuration-file form: ``{kind: options}``.
- **`Lookup(on: str, table: Mapping[Any, Any] | Sequence[Mapping[str, Any]] | str | Path, *, key: str | None = None, fields: Sequence[str] | None = None, prefix: str = '', required: bool = False, overwrite: bool = False, name: str | None = None)`** (class). Add fields from a reference table, matched on a field.
  - `apply(self, record: dict[str, Any], ctx: RecordContext) -> dict[str, Any] | None`: Return the record (changed in place, or a new dict), or ``None`` to drop it (set ``ctx.reason``).
  - `details(self) -> str`: A short note for :meth:`Pipeline.describe`.
  - `classmethod from_config(cls, options: Any, loader: ConfigLoader) -> Lookup`
  - `to_config(self) -> dict[str, Any]`: The stage's configuration-file form: ``{kind: options}``.
- **`Match(a: Mention, b: Mention, score: float, decision: str, evidence: list[tuple[float, str]] = ..., note: str | None = None)`** (class). Two mentions compared: a score (0-1), a decision and the evidence behind them.
  - `to_dict(self) -> dict[str, Any]`
- **`Mention(name: str, kind: str, source: str | None = None, attributes: dict[str, Any] = ..., id: int = -1)`** (class). One occurrence of a name in the data, with where it was seen.
  - `to_dict(self) -> dict[str, Any]`
- **`MinHashLSH(threshold: float = 0.8, num_perm: int = 128)`** (class). Locality-sensitive hashing over MinHash signatures: candidate pairs above ``threshold`` Jaccard.
  - `false_negative_rate(self, similarity: float) -> float`: Probability that a pair with this Jaccard similarity is *not* found.
  - `insert(self, key: Hashable, signature: Sequence[int])`
  - `query(self, signature: Sequence[int], *, verify: bool = True) -> list[tuple[Hashable, float]]`: ``(key, estimated similarity)`` of stored candidates (``verify`` drops those below the threshold).
- **`Normalize(schema: Schema | Mapping[str, Any] | str | Path, *, base_url: str | None = None, country: str | None = None, currency: str | None = None, dayfirst: bool | None = None, decimal: str | None = None, notes: bool = False, name: str | None = None)`** (class). Type and clean records with a :class:`~wintergrab.data.schema.Schema` (see :meth:`Schema.normalize`).
  - `apply(self, record: dict[str, Any], ctx: RecordContext) -> dict[str, Any] | None`: Return the record (changed in place, or a new dict), or ``None`` to drop it (set ``ctx.reason``).
  - `details(self) -> str`: A short note for :meth:`Pipeline.describe`.
  - `classmethod from_config(cls, options: Any, loader: ConfigLoader) -> Normalize`
  - `to_config(self) -> dict[str, Any]`: The stage's configuration-file form: ``{kind: options}``.
- **`NormalizeContext(base_url: str | None = None, country: str | None = None, currency: str | None = None, dayfirst: bool | None = None, decimal: str | None = None, record: Mapping[str, Any] = ...)`** (class). What normalizers may need besides the value itself.
- **`Operation(name: str, build: Callable[[Any], OperationFn], elementwise: bool = True, strict: bool = False, accepts_none: bool = False, argument: str = 'none')`** (class). A named value operation for :class:`Transform`.
- **`Pipeline(stages: Iterable[Any] = (), *, name: str = 'pipeline')`** (class). Records through a list of stages; see the module docs.
  - `add(self, stage: Any) -> Pipeline`: Append a stage (returns the pipeline, for chaining).
  - `aprocess(self, record: dict[str, Any], ctx: RecordContext | None = None) -> dict[str, Any] | None`: :meth:`process` for pipelines with asynchronous stages.
  - `arun(self, records: Iterable[Any] | AsyncIterable[Any]) -> list[dict[str, Any]]`: :meth:`run` for pipelines with asynchronous stages (also accepts async iterables).
  - `close(self)`
  - `close_spider(self, spider: Any)`
  - `describe(self) -> str`: A table of what each stage did.
  - `classmethod from_config(cls, config: Mapping[str, Any] | Sequence[Any], *, base_dir: str | Path | None = None, schemas: Mapping[str, Schema] | None = None, allow_imports: bool = False, loader: ConfigLoader | None = None) -> Pipeline`: A pipeline from its configuration form (see the module docs).
  - `classmethod load(cls, path: str | Path, *, schemas: Mapping[str, Schema] | None = None, allow_imports: bool = False) -> Pipeline`: Read a pipeline from a JSON, YAML or TOML file (relative paths in it are relative to the file).
  - `open_spider(self, spider: Any)`
  - `process(self, record: dict[str, Any], ctx: RecordContext | None = None) -> dict[str, Any] | None`: One record (changed in place) through every stage; ``None`` if a stage dropped it.
  - `process_item(self, item: Any, spider: Any = None) -> Any`
  - `report(self) -> list[dict[str, Any]]`: Per-stage counts: ``[{"stage", "kind", "in", "out", "dropped", "errors", ...}, ...]``.
  - `run(self, records: Iterable[Any]) -> list[dict[str, Any]]`: Process records (dicts, dataclasses...); the ones that come through, in order.
  - `save(self, path: str | Path) -> Path`: Write :meth:`to_config` as JSON (or YAML for ``.yaml``/``.yml``).
  - `stream(self, records: Iterable[Any]) -> Iterator[dict[str, Any]]`: Process records one at a time, yielding those that come through (for large inputs).
  - `to_config(self) -> dict[str, Any]`: The configuration form (raises for stages built from Python functions).
- **`Place(country: str | None = None, region: str | None = None, city: str | None = None, postal_code: str | None = None, street: str | None = None, coordinates: tuple[float, float] | None = None, remote: bool = False, unsure: str | None = None, sources: dict[str, str] = ...)`** (class). Where a record is; ``None`` for what it does not say.
  - `key(self, part: str) -> Any`: What records are grouped by for ``part``: a city with its region or country (two Portlands are two cities), the others as they are.
  - `to_dict(self) -> dict[str, Any]`
- **`QualityCheck(schema: Schema | Mapping[str, Any] | str | Path | None = None, *, monitor: QualityMonitor | None = None, name: str | None = None, **options: Any)`** (class). Measure dataset quality as records pass; never drops anything.
  - `apply(self, record: dict[str, Any], ctx: RecordContext) -> dict[str, Any] | None`: Return the record (changed in place, or a new dict), or ``None`` to drop it (set ``ctx.reason``).
  - `close_spider(self, spider: Any) -> Any`: Called once after a crawl.
  - `details(self) -> str`: A short note for :meth:`Pipeline.describe`.
  - `classmethod from_config(cls, options: Any, loader: ConfigLoader) -> QualityCheck`
  - `open_spider(self, spider: Any) -> Any`: Called once before a crawl.
  - `report(self) -> QualityReport`
  - `to_config(self) -> dict[str, Any]`: The stage's configuration-file form: ``{kind: options}``.
- **`QualityMonitor(schema: Schema | None = None, *, name: str | None = None, key: Iterable[str] | None = None, baseline: QualityReport | str | Path | None = None, save_to: str | Path | None = 'auto', time_field: str | None = None, max_age: float = 604800, outlier_z: float = 8.0, seed: int = 0)`** (class). Measures dataset quality as records stream by.
  - `close_spider(self, spider: Spider)`
  - `observe(self, record: Mapping[str, Any])`: Account for one (normalized) record.
  - `open_spider(self, spider: Spider)`
  - `process_item(self, item: Any, spider: Any = None) -> Any`
  - `report(self) -> QualityReport`
- **`QualityReport(name: str, records: int, created: float, fields: dict[str, dict[str, Any]], metrics: dict[str, float | None], issues: list[Issue], key: list[str] = ...)`** (class). The quality of a dataset at one point in time (JSON-serialisable via :meth:`to_dict`).
  - `compare(self, baseline: QualityReport, *, drop: float = 0.2, collapse: float = 0.4, volume_drop: float = 0.5, min_records: int = 20) -> list[Issue]`: Degradation relative to ``baseline`` (an earlier run's report).
  - `describe(self, *, fields: int = 30) -> str`: A readable summary: overall metrics, a line per field, and the anomalies.
  - `classmethod from_dict(cls, data: Mapping[str, Any]) -> QualityReport`
  - `classmethod load(cls, path: str | Path) -> QualityReport`
  - `save(self, path: str | Path) -> Path`
  - `to_dict(self) -> dict[str, Any]`
- **`RecordContext(spider: Any = None, schema: Schema | None = None, results: dict[str, FieldResult] | None = None, dropped_by: str = '', reason: str = '')`** (class). What one record's trip through a pipeline has gathered.
- **`Rename(mapping: Mapping[str, str], *, name: str | None = None)`** (class). Rename fields: ``Rename({"cost": "price", "title": "name"})``.
  - `apply(self, record: dict[str, Any], ctx: RecordContext) -> dict[str, Any] | None`: Return the record (changed in place, or a new dict), or ``None`` to drop it (set ``ctx.reason``).
  - `classmethod from_config(cls, options: Any, loader: ConfigLoader) -> Rename`
  - `to_config(self) -> dict[str, Any]`: The stage's configuration-file form: ``{kind: options}``.
- **`Resolution(entities: list[Entity], review: list[Match], stats: dict[str, int], _by_mention: dict[int, Entity] = ...)`** (class). What :meth:`EntityResolver.resolve` found.
  - `entity_of(self, mention: Mention | int) -> Entity`: The entity a mention (or mention id) belongs to.
  - `find(self, name: str) -> list[Entity]`: Entities that were mentioned with exactly this name (ignoring case and surrounding spaces).
  - `records(self) -> list[dict[str, Any]]`: One merged record per entity: its id, name, aliases, confidence, sources and, for each attribute, the most common value.
  - `summary(self) -> str`
- **`Rule(code: str, check: Callable[[Mapping[str, Any]], Any] | str, message: str = '', field: str | None = None, severity: str = 'error', skip_missing: bool = True)`** (class). A custom check on a whole record.
  - `apply(self, record: Mapping[str, Any]) -> Issue | None`
  - `classmethod from_dict(cls, data: Mapping[str, Any]) -> Rule`: A rule from its file form: ``{"code": ..., "check": "<expression>", "message": ..., ...}``.
  - `to_dict(self) -> dict[str, Any]`
- **`Schema(name: str = 'record', fields: list[SchemaField] = ..., version: int = 1, description: str = '', key: list[str] = ..., extra: str = 'keep', container: str | None = None, next_page: str | None = None)`** (class). A named, versioned list of typed fields.
  - `classmethod from_dict(cls, data: Mapping[str, Any]) -> Schema`
  - `classmethod infer(cls, records: Iterable[Mapping[str, Any]], name: str = 'inferred', *, sample: int = 1000) -> Schema`: Guess a schema from sample records (see :func:`wintergrab.data.inference.infer_schema`).
  - `key_of(self, record: Mapping[str, Any]) -> tuple[Any, ...] | None`: The record's identity (its ``key`` fields' values), ``None`` without a key or with missing parts.
  - `classmethod load(cls, path: str | Path) -> Schema`: Read a schema from ``.json``, ``.yaml``/``.yml`` (needs PyYAML) or ``.toml`` (Python 3.11+ or tomli).
  - `normalize(self, record: Mapping[str, Any], *, context: NormalizeContext | None = None, base_url: str | None = None, country: str | None = None, currency: str | None = None) -> tuple[dict[str, Any], dict[str, FieldResult]]`: Typed, cleaned copy of ``record`` and a :class:`FieldResult` per schema field.
  - `normalize_value(self, name: str, raw: Any, *, context: NormalizeContext | None = None) -> FieldResult`: Read one raw value as field ``name`` (without record-level steps such as the currency split).
  - `save(self, path: str | Path) -> Path`
  - `to_dict(self) -> dict[str, Any]`
  - `to_json_schema(self) -> dict[str, Any]`: The schema as JSON Schema (draft 2020-12), e.g.
  - `validate(self, record: Mapping[str, Any], results: Mapping[str, FieldResult] | None = None) -> list[Issue]`: Issues with an already normalized record (``[]`` if it is valid).
- **`SchemaField(name: str, type: str = 'string', required: bool = False, many: bool = False, description: str = '', enum: list[Any] | None = None, minimum: float | None = None, maximum: float | None = None, min_length: int | None = None, max_length: int | None = None, pattern: str | None = None, unit: str | None = None, currency: str | None = None, country: str | None = None, country_field: str | None = None, currency_field: str | None = None, best: float = 5, key: bool = False, selectors: list[str] = ..., sources: list[str] = ..., aliases: list[str] = ..., default: Any = None, fields: list[SchemaField] | None = None, best_given: bool = False, schema: Schema | None = None, _pattern: re.Pattern[str] | None = None)`** (class). One field of a :class:`Schema`.
  - `classmethod from_spec(cls, name: str, spec: Any) -> SchemaField`: A field from its file form: a type name (``"money"``) or a dict of options.
  - `to_dict(self) -> dict[str, Any]`
- **`Select(fields: str | Sequence[str], *, fill_missing: bool = True, keep_metadata: bool = True, name: str | None = None)`** (class). Keep only these fields, in this order: ``Select(["name", "price", "url"])``.
  - `apply(self, record: dict[str, Any], ctx: RecordContext) -> dict[str, Any] | None`: Return the record (changed in place, or a new dict), or ``None`` to drop it (set ``ctx.reason``).
  - `classmethod from_config(cls, options: Any, loader: ConfigLoader) -> Select`
  - `to_config(self) -> dict[str, Any]`: The stage's configuration-file form: ``{kind: options}``.
- **`SimHashIndex(max_distance: int = 3, bits: int = 64)`** (class). Stored fingerprints, searchable for near-duplicates within ``max_distance`` bits.
  - `add(self, key: Hashable, fingerprint: int)`
  - `find_or_add(self, key: Hashable, fingerprint: int) -> Hashable | None`: The key of a near-duplicate already stored; otherwise store ``key`` and return ``None``.
  - `query(self, fingerprint: int) -> list[tuple[Hashable, int]]`: ``(key, distance)`` of every stored fingerprint within ``max_distance``, closest first.
- **`Stage(*, name: str | None = None)`** (class). Base class of pipeline stages: override :meth:`apply` (or :meth:`aapply`).
  - `aapply(self, record: dict[str, Any], ctx: RecordContext) -> dict[str, Any] | None`: The asynchronous version of :meth:`apply` (override it for stages that wait on I/O).
  - `apply(self, record: dict[str, Any], ctx: RecordContext) -> dict[str, Any] | None`: Return the record (changed in place, or a new dict), or ``None`` to drop it (set ``ctx.reason``).
  - `aprocess(self, record: dict[str, Any], ctx: RecordContext | None = None) -> dict[str, Any] | None`
  - `close(self)`: Release files and other resources (the stage can still be used afterwards).
  - `close_spider(self, spider: Any) -> Any`: Called once after a crawl.
  - `details(self) -> str`: A short note for :meth:`Pipeline.describe`.
  - `error(self, message: str)`: Count a problem that did not stop the stage (the first few are logged).
  - `classmethod from_config(cls, options: Any, loader: ConfigLoader) -> Stage`: A stage from its configuration-file form.
  - `open_spider(self, spider: Any) -> Any`: Called once before a crawl.
  - `process(self, record: dict[str, Any], ctx: RecordContext | None = None) -> dict[str, Any] | None`: Run the stage on ``record`` (which it may change), counting what happens.
  - `process_item(self, item: Any, spider: Any = None) -> Any`
  - `to_config(self) -> dict[str, Any]`: The stage's configuration-file form: ``{kind: options}``.
- **`Transform(fields: str | Sequence[str], ops: Any, *, target: str | None = None, on_error: str = 'null', name: str | None = None)`** (class). Apply value operations to fields: ``Transform("price", ["strip", {"regex": "[0-9.,]+"}, "number"])``.
  - `apply(self, record: dict[str, Any], ctx: RecordContext) -> dict[str, Any] | None`: Return the record (changed in place, or a new dict), or ``None`` to drop it (set ``ctx.reason``).
  - `classmethod from_config(cls, options: Any, loader: ConfigLoader) -> Transform`
  - `to_config(self) -> dict[str, Any]`: The stage's configuration-file form: ``{kind: options}``.
- **`TypeGuess(field: str, type: str, share: float, samples: int, present: int, records: int)`** (class). Why a field got its type.
- **`Validate(schema: Schema | Mapping[str, Any] | str | Path | None = None, *, rules: Iterable[Rule | Mapping[str, Any]] = (), on_error: str = 'drop', on_warning: str = 'keep', issues_field: str = '_issues', rejects: str | Path | None = None, name: str | None = None)`** (class). Check records against a schema and rules; drop, flag or keep the invalid ones.
  - `apply(self, record: dict[str, Any], ctx: RecordContext) -> dict[str, Any] | None`: Return the record (changed in place, or a new dict), or ``None`` to drop it (set ``ctx.reason``).
  - `close(self)`: Release files and other resources (the stage can still be used afterwards).
  - `details(self) -> str`: A short note for :meth:`Pipeline.describe`.
  - `classmethod from_config(cls, options: Any, loader: ConfigLoader) -> Validate`
  - `summary(self, limit: int = 10) -> list[tuple[str | None, str, str, int]]`: The most frequent issues: ``[(field, code, severity, count), ...]``.
  - `to_config(self) -> dict[str, Any]`: The stage's configuration-file form: ``{kind: options}``.
- **`compile_expression(source: str) -> Expression`**. :class:`Expression` with a cache (the same source compiles once).
- **`content_hash(value: Any) -> str`**. A stable hex digest of ``value``'s normalized text (dicts and lists: of their sorted items).
- **`diff_records(old: Iterable[Mapping[str, Any]], new: Iterable[Mapping[str, Any]], key: str | Sequence[str] | None = None, *, ignore: Iterable[str] = (), private: bool = False) -> DatasetDiff`**. The differences between two datasets.
- **`distance_km(a: Any, b: Any) -> float | None`**. The great-circle distance between two points, in kilometres (to 0.1 km); ``None`` when either has no coordinates.
- **`explain_inference(records: Iterable[Mapping[str, Any]], **options: Any) -> list[TypeGuess]`**. Why each field got its type (share of values the type could read, presence...).
- **`get_path(record: Any, path: str, default: Any = None) -> Any`**. The value at a dotted ``path`` (``"offers.0.price"``); a key with that exact name wins.
- **`group_records(records: Iterable[Mapping[str, Any]], by: str | Callable[[Mapping[str, Any]], Any], *, stats: Sequence[str] = (), country: str | None = None, fields: Mapping[str, str | Sequence[str]] | None = None, places: Sequence[Place] | None = None) -> list[Group]`**. Group ``records`` by a place part (``"country"``, ``"region"``, ``"city"``, ``"postal_code"``, ``"remote"``: read with :func:`places_of`, so ``"Germany"``, ``"DE"`` and ``"Deutschland"`` are one group), by another field (a dotted path), or by a function of the record.
- **`hamming(a: int, b: int) -> int`**. Number of differing bits.
- **`infer_schema(records: Iterable[Mapping[str, Any]], *, name: str = 'inferred', sample: int = 1000, threshold: float = 0.9, guesses: list[TypeGuess] | None = None) -> Schema`**. Guess a schema from up to ``sample`` records (see the module docs).
- **`is_valid(issues: Iterable[Issue]) -> bool`**. ``True`` when none of ``issues`` is an error (warnings and info are fine).
- **`jaccard(a: set[Any], b: set[Any]) -> float`**. Size of the intersection over size of the union (1.0 for two empty sets).
- **`ks_statistic(a: list[float], b: list[float]) -> float`**. Two-sample Kolmogorov-Smirnov statistic: the largest gap between the two empirical CDFs.
- **`load_schema(path: str | Path) -> Schema`**. Read a schema file (``.json``, ``.yaml``/``.yml``, ``.toml``).
- **`minhash(features: str | Iterable[str], num_perm: int = 128, seed: int = 1) -> tuple[int, ...]`**. MinHash signature of a text's shingles (or of a set of features).
- **`minhash_similarity(a: Sequence[int], b: Sequence[int]) -> float`**. Estimated Jaccard similarity: the share of agreeing signature slots.
- **`place_of(record: Mapping[str, Any], *, country: str | None = None, fields: Mapping[str, str | Sequence[str]] | None = None) -> Place`**. The place of ``record`` (see the module docs).
- **`places_of(records: Iterable[Mapping[str, Any]], *, country: str | None = None, fields: Mapping[str, str | Sequence[str]] | None = None, settle: float = 0.8) -> tuple[list[Place], str | None]`**. The places of ``records``, and the country that settled the most codes naming several places, if any.
- **`register_operation(name: str, build: Callable[[Any], OperationFn], *, elementwise: bool = True, strict: bool = False, accepts_none: bool = False, argument: str = 'none') -> Operation`**. Add a :class:`Transform` operation (see :class:`Operation` for the options).
- **`register_type(name: str, normalizer: Normalizer, json_schema: Mapping[str, Any] | None = None)`**. Add a field type: ``normalizer(raw, field, context, notes) -> value | None``.
- **`simhash(features: str | Iterable[str], bits: int = 64) -> int`**. SimHash of a text (its word 3-gram shingles) or of explicit features (repeats count as weight).
- **`simhash_similarity(a: int, b: int, bits: int = 64) -> float`**. ``1 - hamming/bits``: 1.0 for identical fingerprints.
- **`validate_record(record: Mapping[str, Any], schema: Schema | None, results: Mapping[str, FieldResult] | None = None, *, rules: Sequence[Rule] = (), prefix: str = '') -> list[Issue]`**. Every :class:`~wintergrab.data.issues.Issue` with a normalized record (``[]`` if valid).

## `wintergrab.data.io`: Reading records

- **`READERS`**: a dict
- **`RECORD_SUFFIXES`**: a tuple
- **`URL_READERS`**: a dict
- **`read_records(path: str | Path, *, limit: int | None = None) -> Iterator[dict[str, Any]]`**. The records in a file, one at a time.
- **`register_reader(key: str, reader: Reader | str)`**. Read more: ``".ext"`` for files with that extension, ``"scheme"`` for ``scheme://`` URLs.

## `wintergrab.data.places`: Where records are

- **`DEFAULT_PARTS`** = `('country', 'region', 'city', 'postal_code', 'coordinates')`
- **`PLACE_FIELDS`**: a dict
- **`PLACE_PARTS`** = `('country', 'region', 'city', 'postal_code', 'street', 'coordinates', 'remote')`
- **`Group(key: Any, label: str, count: int, share: float, stats: dict[str, dict[str, float | int]] = ..., indices: list[int] = ...)`** (class). Records sharing a value (:func:`group_records`).
  - `to_dict(self) -> dict[str, Any]`
`Place`: see [`wintergrab.data`](#wintergrabdata-schemas-normalizing-validating-pipelines-quality).

- **`coordinates_of(value: Any) -> tuple[float, float] | None`**. ``(latitude, longitude)`` of a value: a pair, ``"48.8584, 2.2945"`` (or degrees, minutes and seconds), ``{"latitude": ..., "longitude": ...}`` (schema.org ``GeoCoordinates``), a GeoJSON point (``{"type": "Point", "coordinates": [lon, lat]}``), a map link, or a :class:`Place`; ``None`` otherwise.
`distance_km`: see [`wintergrab.data`](#wintergrabdata-schemas-normalizing-validating-pipelines-quality).

`group_records`: see [`wintergrab.data`](#wintergrabdata-schemas-normalizing-validating-pipelines-quality).

- **`in_box(point: Any, south: float, west: float, north: float, east: float) -> bool | None`**. Whether a point is inside a box of latitudes and longitudes (a box across the 180th meridian has ``west > east``); ``None`` when the point has no coordinates.
`place_of`: see [`wintergrab.data`](#wintergrabdata-schemas-normalizing-validating-pipelines-quality).

`places_of`: see [`wintergrab.data`](#wintergrabdata-schemas-normalizing-validating-pipelines-quality).


## `wintergrab.data.graph`: Knowledge graphs

- **`RELATIONS`**: a dict
- **`Edge(source: str, relation: str, target: str, sources: list[str] = ..., count: int = 0, confidence: float | None = None)`** (class). A fact: ``source`` -``relation``-> ``target`` (node ids), the pages that state it, and how sure.
  - `to_dict(self) -> dict[str, Any]`
- **`KnowledgeGraph(*, merge_threshold: float = 0.95, review_threshold: float = 0.5)`** (class). Nodes and typed edges built from records (see the module docs).
  - `add_records(self, records: Iterable[Mapping[str, Any]], kind: str, *, relations: Sequence[Relation] | None = None, source_field: str = 'url') -> int`: Add ``records`` of ``kind``, their relations (by default :data:`RELATIONS` ``[kind]``), and where they came from (``source_field``).
  - `build(self) -> KnowledgeGraph`: Resolve the names and make the nodes and edges (again, after more :meth:`add_records`).
  - `describe(self, *, top: int = 5) -> str`: The nodes and edges by kind, the most connected nodes of each kind that is linked to, and what to review.
  - `find(self, name: str, kind: str | None = None) -> list[Node]`: Nodes spelled ``name`` (ignoring case), of ``kind`` if given.
  - `classmethod from_records(cls, records: Iterable[Mapping[str, Any]], kind: str, *, relations: Sequence[Relation] | None = None, **options: Any) -> KnowledgeGraph`: A graph of ``records`` of ``kind`` (``relations``: the edges, by default :data:`RELATIONS`).
  - `neighbors(self, node_id: str, relation: str | None = None, *, direction: str = 'out') -> list[tuple[Edge, Node]]`: The edges from (``"out"``), to (``"in"``) or at (``"both"``) a node, with the node at their other end.
  - `node(self, node_id: str) -> Node`
  - `save(self, path: str | Path) -> Path`: Write the graph: ``.json`` (nodes, edges and review), ``.graphml``, or a directory (``nodes.csv`` and ``edges.csv``, with the headers Neo4j's import reads).
  - `to_dict(self) -> dict[str, Any]`
- **`Node(id: str, kind: str, name: str, aliases: list[str] = ..., attributes: dict[str, list[Any]] = ..., sources: list[str] = ..., confidence: float = 1.0, count: int = 1)`** (class). A thing in the graph (see the module docs).
  - `to_dict(self) -> dict[str, Any]`
- **`Relation(field: str, relation: str, kind: str)`** (class). A field of a record that names another thing: an edge from the record to it.
  - `classmethod parse(cls, text: str) -> Relation`: ``"FIELD=RELATION:KIND"``, as the command line takes it (``brand=manufactured_by:brand``).

## `wintergrab.goals`: Goals in plain words

- **`ENTITIES`**: a dict
- **`EntityKind(name: str, words: tuple[str, ...], page_types: tuple[str, ...], listing_types: tuple[str, ...], fields: Mapping[str, Any], default_fields: tuple[str, ...], date_field: str | None = None)`** (class). A kind of record a goal can ask for.
  - `schema(self, fields: list[str]) -> Schema`: An extraction schema for these fields (types from :attr:`fields`, strings otherwise).
- **`Estimate(pages: int = 0, exact: bool = False, listing_pages: int = 0, requests: int = 0, browser_pages: int = 0, bytes: int = 0, seconds: float = 0.0, records: int = 0, cpu_seconds: float = 0.0, storage_bytes: int = 0, basis: list[str] = ...)`** (class). What a plan will cost, and what the numbers rest on (``basis``).
  - `describe(self) -> str`
- **`GenerationResult(directory: Path, goal: Goal, stages: list[Stage] = ..., accepted: bool = False, reasons: list[str] = ..., plan: GoalPlan | None = None, generated: GeneratedSchema | None = None)`** (class). What :func:`generate_scraper` made, each step, and the verdict.
  - `describe(self) -> str`: Each step in a line (its problems and warnings under it), then the verdict.
  - `stage(self, name: str) -> Stage | None`: The step called ``name``, if it was taken.
  - `to_dict(self) -> dict[str, Any]`
- **`Goal(text: str, entity: str, fields: list[str], sites: list[str] = ..., filters: list[GoalFilter] = ..., scope: list[str] = ..., limit: int | None = None, monitor: str | None = None, dedupe: bool = True, notes: list[str] = ...)`** (class). What to collect (see the module docs).
  - `describe(self) -> str`: The goal as understood, in a few lines.
  - `classmethod from_dict(cls, data: Mapping[str, Any], *, text: str = '') -> Goal`: A goal from its dict form (a saved plan, or a model's reading of a request), checked.
  - `schema(self) -> Schema`: The extraction schema: the goal's fields, typed.
  - `to_dict(self) -> dict[str, Any]`
- **`GoalFilter(expression: str, text: str = '', field: str | None = None)`** (class). A condition records must meet: an expression (:mod:`wintergrab.data.expressions`) and what it came from.
- **`GoalPlan(goal: Goal, sites: list[SitePlan], created: str = ..., schema: Schema | str | None = None, directory: Path | None = None)`** (class). A goal and a plan per site: see :func:`plan_goal`.
  - `describe(self, *, goal: bool = True) -> str`: The goal as understood (unless ``goal=False``), then each site's steps and estimates, and the warnings.
  - `explain(self) -> str`: What each estimate rests on.
  - `extraction_schema(self) -> Schema`: The schema records are read with: :attr:`schema`, or the goal's.
  - `classmethod from_dict(cls, data: dict[str, Any]) -> GoalPlan`: A plan from its :meth:`to_dict` form.
  - `classmethod load(cls, path: str | Path) -> GoalPlan`
  - `run(self, output: str | None = None, **options: Any) -> GoalResult`: Collect the records (see :func:`~wintergrab.goals.run.run_plan`).
  - `save(self, path: str | Path)`: Write the plan as JSON (edit it, and run it with :meth:`load` and :meth:`run`).
  - `to_dict(self, *, embed_schema: bool = False) -> dict[str, Any]`: The plan as JSON holds it; ``embed_schema``: a schema file's content rather than its name.
- **`GoalResult(plan: GoalPlan, crawl: CrawlResult | None = None, records: list[dict[str, Any]] = ..., output: str | None = None, counts: Counter[str] = ..., found: Counter[str] = ..., pages: list[Response] = ..., notes: list[str] = ...)`** (class). What running a plan gave: the records (when kept), the crawl's result, and counts.
  - `summary(self) -> str`: The records, the fields they have, and what was left out and why.
- **`GoalSpider(goal: Goal, plans: list[SitePlan], *, schema: Schema | None = None, keep_pages: bool = False, use_api: bool = True, **settings: Any)`** (class). Collects a goal's records, following a plan per site (see the module docs).
  - `api_failed(self, request: Request, error: BaseException) -> Any`: An API request that failed: a refusal is reported; another failure on the first page leaves the site to its pages.
  - `parse(self, response: Response) -> Any`: A page of a ``follow`` plan: its record if it has one, and the links that lead to more.
  - `parse_api(self, response: Response) -> Any`: A page of a site's API: its records, and the next page.
  - `parse_record(self, response: Response) -> Any`: A page that holds a record: extract it.
  - `start_requests(self) -> Iterator[Request | str]`: The plans' sitemaps and start pages, and the first page of each API (whose sites' pages wait).
- **`SitePlan(site: str, strategy: str, start_urls: list[str] = ..., sitemap_urls: list[str] = ..., target: list[str] = ..., follow: list[str] = ..., sections: list[str] = ..., page_types: list[str] = ..., fetch: str = 'http', allowed: bool = True, crawl_delay: float | None = None, sample: dict[str, Any] = ..., estimate: Estimate = ..., steps: list[str] = ..., warnings: list[str] = ..., js_patterns: list[str] = ..., api: dict[str, Any] | None = None)`** (class). How to get a goal's records from one site (see the module docs).
  - `classmethod from_dict(cls, data: dict[str, Any]) -> SitePlan`
  - `is_followed(self, url: str) -> bool`
  - `is_target(self, url: str) -> bool`
- **`Stage(name: str, ok: bool, summary: str, problems: list[str] = ..., warnings: list[str] = ..., details: dict[str, Any] = ...)`** (class). One step of the generation (see the module docs).
  - `to_dict(self) -> dict[str, Any]`
- **`generate_scraper(goal: str | Goal, directory: str | Path, *, sites: Sequence[str] = (), model: Any = None, sample: int = 30, train: int = 5, test: int = 10, obey_robots: bool = True, browser: bool = False, timeout: float = 20.0, min_completeness: float = 0.9, min_agreement: float = 0.9, log_level: str | None = 'WARNING', on_stage: Callable[[Stage], None] | None = None) -> GenerationResult`**. Generate a scraper for ``goal``, test it, and accept or reject it (see the module docs).
- **`model_reader(model: Any, *, now: datetime | None = None) -> Callable[[str], Mapping[str, Any]]`**. A ``parser`` for :func:`~wintergrab.goals.parse_goal` that asks ``model`` (a :class:`~wintergrab.models.ModelProvider`), and falls back on the built-in rules (see the module docs).
- **`parse_goal(text: str, *, sites: list[str] | None = None, parser: Callable[[str], Mapping[str, Any]] | None = None, now: datetime | None = None) -> Goal`**. A :class:`Goal` from a request in plain words (see the module docs).
- **`path_pattern(urls: list[str]) -> str`**. A path pattern covering ``urls``: segments they share stay, the others become ``*``.
- **`plan_goal(goal: Goal, *, sample: int = 30, obey_robots: bool = True, browser: bool = False, timeout: float = 20.0, surveys: dict[str, SiteSurvey] | None = None, log_level: str | None = 'WARNING', settings: Mapping[str, Any] | None = None, api: bool = True, probe: int = 3) -> GoalPlan`**. Plan ``goal`` for each of its sites (see the module docs).
- **`run_plan(plan: GoalPlan, output: str | None = None, *, max_pages: int | None = None, keep_items: bool | None = None, keep_pages: bool = False, use_api: bool = True, log_level: str | None = 'INFO', progress: bool | None = None, **settings: Any) -> GoalResult`**. Collect ``plan``'s records into ``output`` (``.jsonl``, ``.csv``, ``.json``...; see the module docs).

## `wintergrab.goals.api`: Records from the API a site's pages call

- **`ApiSource(method: str, url: str, body: Any = None, graphql: str | None = None, path: str = '[]', fields: dict[str, str] = ..., pagination: dict[str, Any] | None = None, per_page: int = 0, total: int | None = None, pages: int | None = None, seen_on: str = '', template: str = '', bytes: int = 0, examples: list[dict[str, Any]] = ..., read_seconds: float = 0.0)`** (class). An API a site's pages call, holding the goal's records (see the module docs).
  - `describe(self) -> str`: ``GET shop.example/api/products?page: 4 record(s) a page (pages by page: 3 in all)``.
  - `classmethod from_dict(cls, data: Mapping[str, Any]) -> ApiSource`
  - `mapping(self) -> str`: ``name <- title, price <- price.amount...``: where each field is read from.
  - `to_dict(self) -> dict[str, Any]`: The source as a plan's JSON holds it (``SitePlan.api``), without its examples.
- **`find_api(goal: Goal, pages: Iterable[Any], *, schema: Schema | None = None, notes: list[str] | None = None) -> ApiSource | None`**. The API, among the calls ``pages`` made as they rendered (their ``captured`` calls), that answers with ``goal``'s records, their name (or title) and as many of the goal's fields as it can; ``None`` if none does, or if its pages cannot be followed.
- **`items_at(data: Any, path: str) -> list[Mapping[str, Any]]`**. The records at ``path`` in an answer (a :class:`~wintergrab.intel.sources.Collection` path: ``items[]``, ``data.products.edges[].node``, ``[]``, ``__APOLLO_STATE__{Product}``).
- **`map_fields(schema: Schema, fields: Iterable[str], collection: Collection, *, base_url: str | None = None) -> dict[str, str]`**. Goal field -> where ``collection``'s records hold it: the first of the field's names (its own, its aliases, :data:`API_NAMES`, schema.org's) whose values, in half the records or more, read as the field's type.
- **`next_page(source: ApiSource, url: str, body: Any, answer: Any, records: int) -> tuple[str, Any] | None`**. The next page's URL and body after the answer to ``url``/``body`` (``records`` records in it), or ``None`` when it was the last.
- **`records_of(source: ApiSource, answer: Any, schema: Schema, *, base_url: str | None = None) -> list[dict[str, Any]]`**. The goal's records in one of the API's answers: each record's mapped fields, read as their types (money split into amount and currency, URLs made absolute against ``base_url``: by default the page that made the call).
- **`total_of(source: ApiSource, url: str, body: Any, answer: Any, records: int) -> int | None`**. How many records the API says it has, in its answer to ``url``/``body``, when it says.

## `wintergrab.intel`: Page types, technologies, site profiles

- **`LANGUAGES`**: a tuple
- **`PAGE_TYPES`**: a tuple
- **`RULES`**: a tuple
- **`ApiCall(method: str, url: str, template: str, status: int, content_type: str, calls: int = 1, graphql: str | None = None, collections: list[Collection] = ..., pagination: Pagination | None = None, seen: list[Any] = ..., request: Any = None, page: str | None = None)`** (class). An API a page called as it rendered (recorded by a browser fetch with ``capture=True``).
  - `describe(self) -> str`
  - `details(self) -> str`: How its pages go, and how many times the page called it (``""`` when there is nothing to say).
  - `head(self) -> str`: The call and what it answers: ``GET shop.example/api/products?page: 3 record(s) at items[] (...)``.
  - `to_dict(self) -> dict[str, Any]`
- **`Collection(path: str, count: int, fields: list[str], types: dict[str, str], records: list[Mapping[str, Any]])`** (class). A list of records in a JSON document.
  - `describe(self, fields: int = 6) -> str`
  - `schema(self, name: str = 'records') -> Schema`: A data schema for these records (:func:`~wintergrab.data.inference.infer_schema`): a start to review.
  - `to_dict(self) -> dict[str, Any]`
- **`DataSources(url: str, html: list[HtmlRecords] = ..., tables: list[dict[str, Any]] = ..., json_ld: dict[str, int] = ..., microdata: dict[str, int] = ..., meta: list[str] = ..., embedded: dict[str, list[Collection]] = ..., api: list[ApiCall] = ..., recorded: bool = False, endpoints: list[tuple[str | None, str]] = ..., document: list[Collection] | None = None, pagination: Pagination | None = None, _json_ld_collections: list[tuple[str, Collection]] = ...)`** (class). Where a page's data is (see the module docs).
  - `describe(self) -> str`
  - `richest(self) -> Source | None`: The place holding the most values (records x fields): often the one to read.
  - `sources(self) -> list[Source]`: Every place holding at least two records, those holding the most values (records x fields) first.
  - `to_dict(self) -> dict[str, Any]`
- **`Endpoint(url: str, method: str | None, source: str, pages: int = 1, note: str | None = None, status: int | None = None, content_type: str | None = None, operations: list[str] = ..., records: str | None = None, paging: str | None = None)`** (class). An API endpoint seen in (or conventional for) the site's pages.
- **`HtmlRecords(selector: str, count: int, fields: list[str])`** (class). Records drawn in a page's HTML: repeated elements (cards, rows) with the same fields.
  - `describe(self) -> str`
  - `to_dict(self) -> dict[str, Any]`
- **`PageClassifier()`** (class). Rule-based page classification (see the module docs).
  - `add_rule(self, page_type: str, name: str, rule: Rule)`: Add evidence: ``rule(features)`` returns a weight (or ``(weight, evidence text)``) when it applies.
  - `classify(self, page: Any, *, url: str | None = None, status: int | None = None) -> PageType`
  - `scores(self, features: PageFeatures) -> tuple[dict[str, float], dict[str, list[str]]]`
- **`PageFeatures(page: PageContext, status: int | None = None)`** (class). Cheap facts about a page that classification rules look at (each computed once).
- **`PageType(type: str, confidence: float, evidence: list[str] = ..., scores: dict[str, float] = ...)`** (class). The kind of a page, with the evidence for it.
  - `to_dict(self) -> dict[str, Any]`
- **`Pagination(kind: str, parameter: str | None = None, value: Any = None, size: int | None = None, next: Any = None, next_url: str | None = None, total: int | None = None, pages: int | None = None, more: bool | None = None)`** (class). How an API's pages go, as far as one call and its answer tell.
  - `describe(self) -> str`
  - `to_dict(self) -> dict[str, Any]`
- **`SiteProfile(domains: list[str], pages: int, statuses: dict[int, int], error_rate: float, average_latency: float | None, bytes: int, technologies: list[dict[str, Any]], languages: dict[str, int], regions: list[str], page_types: dict[str, int], templates: list[TemplateCluster], structured_data: dict[str, int], internal_links: int, external_links: int, external_domains: dict[str, int], endpoints: list[Endpoint], crawlability: dict[str, Any], sitemaps: dict[str, Any] | None = None, change_frequency: float | None = None, topology: Topology | None = None)`** (class). What :class:`SiteProfiler` learned about a site (see the module docs).
  - `describe(self, limit: int = 8, *, depth: int = 2) -> str`: The profile as a short report: ``limit`` entries per list, ``depth`` levels of sections.
  - `to_dict(self) -> dict[str, Any]`
- **`SiteProfiler(*, detailed: int = 500)`** (class). Builds a :class:`SiteProfile` from the pages of a site (see the module docs).
  - `add_robots(self, text: str | None, *, found: bool = True, error: str | None = None)`: What the site's robots.txt says for every crawler (``*``); ``error``: why it could not be read.
  - `add_sitemaps(self, sitemaps: int, indexes: int, entries: Iterable[Any])`: Sitemaps (and sitemap indexes) read, and the pages they list (:class:`~wintergrab.SitemapEntry` objects or URLs).
  - `observe(self, response: Any, *, latency: float | None = None)`: Take a fetched page (a :class:`~wintergrab.Response`) into account.
  - `profile(self, *, complete: bool = False) -> SiteProfile`: The profile so far.
- **`SiteSurvey(url: str, profile: SiteProfile, robots_text: str | None = None, robots_found: bool = False, sitemaps: SitemapRead = ..., pages: list[Response] = ..., stats: dict[str, Any] = ...)`** (class). What :func:`survey_site` found (see the module docs).
- **`SitemapRead(roots: list[str] = ..., sitemaps: int = 0, indexes: int = 0, entries: list[SitemapEntry] = ..., truncated: bool = False)`** (class). What :func:`read_sitemaps` found.
- **`Source(kind: str, where: str, records: int, fields: int)`** (class). One place holding records (see :meth:`DataSources.richest`).
- **`TechDetector(rules: Iterable[TechRule] = (..., ..., ..., ..., ..., ..., ..., ..., ..., ..., ..., ..., ..., ..., ..., ..., ..., ..., ..., ..., ..., ..., ..., ..., ..., ..., ..., ..., ..., ..., ..., ..., ..., ..., ..., ..., ..., ..., ..., ..., ..., ..., ..., ..., ..., ..., ..., ..., ..., ..., ..., ..., ..., ..., ..., ..., ..., ..., ..., ..., ..., ..., ..., ..., ..., ..., ..., ..., ..., ..., ..., ..., ..., ..., ..., ..., ..., ..., ..., ..., ..., ..., ..., ..., ..., ..., ..., ..., ..., ..., ..., ..., ..., ..., ..., ..., ..., ..., ..., ..., ..., ..., ..., ..., ..., ..., ..., ..., ..., ..., ..., ..., ..., ..., ..., ..., ..., ..., ..., ..., ..., ..., ..., ...), extra: Iterable[TechRule] = ())`** (class). Technology detection with a set of fingerprints (the built-in ones plus ``extra``).
  - `detect(self, page: Any, *, url: str | None = None, headers: Mapping[str, str] | None = None, cookies: Iterable[str] = (), html: str | None = None) -> list[Technology]`: Technologies seen on ``page`` (a :class:`~wintergrab.Response`, a Selector or HTML), best first.
- **`TechRule(name: str, category: str, headers: dict[str, str] = ..., cookies: tuple[str, ...] = (), meta: dict[str, str] = ..., scripts: tuple[str, ...] = (), html: tuple[str, ...] = (), url: tuple[str, ...] = (), implies: tuple[str, ...] = (), website: str = '')`** (class). TechRule(name: 'str', category: 'str', headers: 'dict[str, str]' = <factory>, cookies: 'tuple[str, ...]' = (), meta: 'dict[str, str]' = <factory>, scripts: 'tuple[str, ...]' = (), html: 'tuple[str, ...]' = (), url: 'tuple[str, ...]' = (), implies: 'tuple[str, ...]' = (), website: 'str' = '')
- **`Technology(name: str, category: str, confidence: float, version: str | None = None, evidence: list[str] = ...)`** (class). A detected technology.
  - `to_dict(self) -> dict[str, Any]`
- **`TemplateCluster(pattern: str, pages: int, page_type: str | None, examples: list[str], layout: int = 0)`** (class). Pages that share a layout (their tag structure), with the URL pattern they follow.
- **`TextAnalysis(language: str | None, language_confidence: float, script: str | None, words: int | None, sentences: int, reading_minutes: float | None, keywords: list[str] = ..., runner_up: str | None = None, characters: int = 0)`** (class). What :func:`analyze_text` found.
  - `to_dict(self) -> dict[str, Any]`
- **`TextLabels(topic: str | None = None, category: str | None = None, sentiment: str | None = None, entities: list[dict[str, str]] = ..., dropped: list[str] = ...)`** (class). What :func:`classify_text` got from a model, checked.
  - `to_dict(self) -> dict[str, Any]`
- **`Topology(site: str, root: TopologyNode, hosts: list[TopologyNode] = ..., navigation: list[dict[str, Any]] = ..., feeds: list[str] = ..., html_sitemaps: list[str] = ..., paginated: dict[str, int] = ..., dead_ends: list[str] = ..., duplicates: list[dict[str, Any]] = ..., orphans: list[str] | None = None, counts: dict[str, int] = ...)`** (class). A site's organization (see the module docs).
  - `find(self, text: str) -> list[TopologyNode]`: Sections whose name or path mentions ``text`` (any case), biggest first.
  - `render(self, depth: int = 3, width: int = 8) -> str`: The tree as text, ``depth`` levels deep, ``width`` sections per level.
  - `summary(self, limit: int = 8) -> list[str]`: The navigation and the odd pages, one line each (the tree aside).
  - `to_dict(self) -> dict[str, Any]`
- **`TopologyBuilder(*, max_urls: int = 200000)`** (class). Collects what a site's topology is made of, page by page (see the module docs).
  - `add_urls(self, urls: Iterable[str])`: URLs the site lists (sitemaps, feeds): they are counted, and can be orphans.
  - `observe(self, page: Any, *, page_type: str | None = None, links: Iterable[str] | None = None)`: A visited HTML page (a :class:`~wintergrab.Response` or a :class:`PageContext`).
  - `start_urls(self, urls: Iterable[str])`: Where the crawl started: those were reached without a link, so they are not orphans.
  - `topology(self, *, complete: bool = False) -> Topology`: The topology so far.
- **`TopologyNode(path: str, label: str, urls: int, page_type: str | None = None, types: dict[str, int] = ..., children: list[TopologyNode] = ..., other_sections: int = 0, other_urls: int = 0)`** (class). A section of the site: a URL path and the URLs under it.
  - `to_dict(self) -> dict[str, Any]`
  - `walk(self) -> Iterator[TopologyNode]`: This node and every node under it, depth first.
- **`analyze_text(text: str, *, keywords: int = 8) -> TextAnalysis`**. The language, size and keywords of ``text`` (see the module docs).
- **`api_calls(captured: Iterable[Any], *, pages: Iterable[str | None] | None = None) -> list[ApiCall]`**. The calls a browser recorded (``response.captured``), grouped by method, URL pattern and GraphQL operation, in the order they were first made.
- **`classify_page(page: Any, *, url: str | None = None, status: int | None = None) -> PageType`**. The type of a page (a :class:`~wintergrab.Response`, a :class:`~wintergrab.Selector` or HTML).
- **`classify_text(text: str, model: Any, *, categories: Sequence[str] | None = None, entities: bool = True, max_chars: int = 12000) -> TextLabels`**. A topic, a category (one of ``categories``), a sentiment and the entities of ``text``, from ``model`` (a :class:`~wintergrab.models.ModelProvider`), checked (see the module docs).
- **`classify_url(url: str) -> PageType`**. A guess from the URL alone (before fetching): cheap, and less sure than :func:`classify_page`.
- **`data_sources(response: Any, *, recorded: bool | None = None) -> DataSources`**. Where ``response``'s data is (see the module docs).
- **`detect_language(text: str) -> tuple[str | None, float, str | None]`**. ``(language, confidence, runner_up)`` of ``text`` (see the module docs); ``(None, 0.0, None)`` when it cannot be told.
- **`detect_technologies(page: Any, **kwargs: Any) -> list[Technology]`**. :meth:`TechDetector.detect` with the built-in fingerprints.
- **`json_collections(data: Any, *, min_records: int = 2) -> list[Collection]`**. The lists of records in a JSON document, those holding the most values (records x fields) first.
- **`pagination_of(url: str, answer: Any = None, *, request: Mapping[str, Any] | None = None, records: int | None = None) -> Pagination | None`**. How the pages of the API ``url`` go, from its query parameters (or ``request``: a GraphQL call's variables, a JSON request body) and its ``answer``, or ``None`` when neither says.
- **`read_sitemaps(origin: str, robots_text: str | None = None, *, max_sitemaps: int = 10, max_entries: int = 50000, **fetch_options: Any) -> SitemapRead`**. The pages a site's sitemaps list: those named in robots.txt, or ``/sitemap.xml``, following sitemap indexes, up to ``max_sitemaps`` sitemaps and ``max_entries`` pages.
- **`script_endpoints(selector: Any, url: str) -> list[tuple[str | None, str]]`**. The API endpoints a page's inline scripts and ``data-`` attributes name, as ``(method, endpoint)``: calls (``fetch("/api/...")``, axios, jQuery, ``xhr.open``) and API-looking paths (``/api/``, ``/graphql``), query values left out (``https://shop.example/api/products?page=``).
- **`survey_site(url: str, *, pages: int = 30, sitemaps: bool = True, obey_robots: bool = True, browser: bool = False, timeout: float = 20.0, keep_pages: bool = False, prefer: Callable[[str], bool | float] | None = None, extra_urls: Iterable[str] = (), log_level: str | None = 'WARNING', **spider_settings: Any) -> SiteSurvey`**. Read ``url``'s site: robots.txt, sitemaps, and ``pages`` pages, into a :class:`SiteSurvey`.

## `wintergrab.intel.serp`: Search results

- **`PROVIDERS`** = `{'brave': ..., 'google': ..., 'searxng': ...}`
- **`Provider(name: str, endpoint: str | None = None, endpoint_variable: str | None = None, key_variable: str | None = None, key_header: str | None = None, key_param: str | None = None, params: Mapping[str, str] = ..., variables: Mapping[str, str] = ..., query_param: str = 'q', size_param: str | None = None, size: int = 10, page_param: str = 'page', page_by: str = 'number', results_path: str | None = None)`** (class). How to ask a search API: its endpoint, the key, and how its pages go.
  - `page_value(self, index: int) -> int`: The page parameter's value for page ``index`` (0 first).
- **`SearchAnswer(query: str, provider: str, results: list[SearchResult] = ..., related: list[str] = ..., questions: list[dict[str, str]] = ..., total: int | None = None, notes: list[str] = ...)`** (class). What a search gave: its results, and what else the provider said.
- **`SearchResult(query: str, position: int, url: str, title: str = '', snippet: str = '', domain: str = '', date: str | None = None, source: str = '', fetched: str = '')`** (class). One result of a search: its query, its position (1 first, across pages), and what it links to.
  - `to_dict(self) -> dict[str, Any]`: The result as a record (its empty fields left out).
- **`Visibility(domain: str, queries: int, average_position: float, visibility: float, results: int)`** (class). A domain's place in a set of searches.
- **`cluster_queries(results: Iterable[SearchResult], *, depth: int = 10, shared: int = 3) -> list[list[str]]`**. Queries in groups that share at least ``shared`` of their top ``depth`` pages (one topic, one page can answer them).
- **`competitors(results: Iterable[SearchResult], *, domain: str | None = None, depth: int = 10, top: int = 20) -> list[Visibility]`**. The domains ranking in the top ``depth`` of the searches, the most visible first (a result's weight is 1 / its position: a ranking's weight, not a click estimate); ``domain`` (yours) is left out.
- **`gaps(results: Iterable[SearchResult], domain: str, *, rivals: Sequence[str] | None = None, depth: int = 10) -> list[dict[str, Any]]`**. The queries where ``domain`` is not in the top ``depth`` and a rival is (``rivals``, by default the three most visible competitors): each with the rivals' best positions, the best-placed rival's first.
- **`overlap(a: Iterable[SearchResult], b: Iterable[SearchResult], *, depth: int = 10, p: float = 0.9) -> dict[str, float]`**. How alike two lists of results are, in their top ``depth``: ``jaccard``, the share of pages in common, and ``rbo``, rank-biased overlap (the agreement at each depth, the top weighing most: ``p`` is how much each deeper position weighs against the one above), from 0 (nothing alike) to 1 (the same pages, same order).
- **`ranking_changes(before: Iterable[SearchResult], after: Iterable[SearchResult], domain: str, *, depth: int = 100) -> list[dict[str, Any]]`**. Each query's best position for ``domain`` in two collections of results: ``"up"``, ``"down"``, ``"same"``, ``"new"`` (ranked now, not before) or ``"lost"`` (the reverse); the largest moves first.
- **`read_results(records: Iterable[Mapping[str, Any]]) -> list[SearchResult]`**. Records with a query, a position and a URL (a file of collected results, a rank tracker's export) as :class:`SearchResult` objects; the others are left out.
- **`search(query: str, *, provider: str | Provider = 'brave', pages: int = 1, endpoint: str | None = None, key: str | None = None, delay: float = 1.0, timeout: float = 20.0, network_policy: Any = None, fetcher: Any = None) -> SearchAnswer`**. Ask a search API for ``query`` (see the module docs): ``pages`` pages of it, ``delay`` seconds apart.
- **`visibility_score(results: Iterable[SearchResult], domain: str, *, depth: int = 10) -> float`**. ``domain``'s visibility (see :class:`Visibility`) over the searches in ``results``.

## `wintergrab.history`: What changed between crawls

- **`ChangeReport(old: Run | None, new: Run, added: list[str] = ..., removed: list[str] = ..., missing: list[str] = ..., skipped: list[str] = ..., modified: list[PageChange] = ..., unchanged: int = 0)`** (class). What changed between two runs.
  - `counts(self) -> dict[str, int]`
  - `describe(self, limit: int = 10) -> str`
  - `kinds(self) -> dict[str, int]`: How many modified pages had each kind of change, most common first.
  - `summary(self) -> str`: The counts, one line each: ``+ 184 pages``, ``- 27 pages``, ``~ 913 pages modified (...)``.
  - `to_dict(self) -> dict[str, Any]`
- **`Freshness(url: str, first_seen: float, last_seen: float, last_changed: float | None, observations: int, changes: int, rate: float | None, recrawl_after: float, fresh: float)`** (class). How a URL changes, from its history.
  - `due(self, now: float | None = None) -> bool`
  - `to_dict(self) -> dict[str, Any]`
- **`PageChange(url: str, kinds: list[str], details: dict[str, Any] = ...)`** (class). What changed on a page between two snapshots.
  - `to_dict(self) -> dict[str, Any]`
- **`PageHistory(path: str | Path, *, keep_html: bool = False, keep_items: bool = True, target: float = 0.5, min_interval: float = 3600, max_interval: float = 2592000.0, first_interval: float = 86400.0)`** (class). The history of crawls in one SQLite file (see the module docs).
  - `change_frequency(self, run: int | None = None) -> float | None`: The median change rate (changes per day) of the pages of ``run`` (all pages when ``None``) that were fetched at least twice.
  - `close(self)`
  - `commit(self)`
  - `compare(self, old: int | None = None, new: int | None = None, *, name: str | None = None) -> ChangeReport`: What changed from run ``old`` to run ``new``.
  - `due(self, url: str, now: float | None = None) -> bool`: Whether ``url`` should be fetched again now (always, for a page never seen).
  - `finish_run(self, run: int, status: str, stats: dict[str, Any] | None = None)`
  - `freshness(self, url: str, *, now: float | None = None) -> Freshness | None`: How ``url`` changes and when to fetch it again, from its history (``None`` if never seen).
  - `mark_skipped(self, run: int, url: str)`: Record that a page was not fetched because it was probably still fresh.
  - `observe(self, run: int, page: Any, *, items: Iterable[Any] | None = None, url: str | None = None, status: int | None = None, fetched_at: float | None = None) -> PageSnapshot`: Record a fetched page (a :class:`~wintergrab.Response`, a Selector or HTML) and the items extracted from it; returns its snapshot.
  - `observe_status(self, run: int, url: str, status: int, *, fetched_at: float | None = None) -> PageSnapshot`: Record a page that answered with an error status (a 404 means it is gone).
  - `page_html(self, run: int, url: str) -> str | None`: The HTML of a page as fetched in ``run``, if it was kept (``keep_html``).
  - `page_items(self, run: int, url: str) -> list[Any]`: The items extracted from a page in ``run``, if they were kept.
  - `run(self, run_id: int) -> Run`
  - `runs(self, name: str | None = None) -> list[Run]`: Recorded runs, oldest first (only the runs of ``name`` if given).
  - `snapshots(self, url: str) -> list[tuple[int, PageSnapshot]]`: Every snapshot of ``url``: ``(run id, snapshot)``, oldest first.
  - `start_run(self, name: str) -> int`: Begin recording a crawl; returns its run id.
  - `urls(self) -> Iterator[str]`: Every URL in the history.
- **`PageSnapshot(url: str, status: int = 200, fetched_at: float = 0.0, body: str = '', text: str = '', text_simhash: int = 0, text_length: int = 0, title: str | None = None, description: str | None = None, meta: str = '', structured: str | None = None, types: list[str] = ..., price: float | None = None, currency: str | None = None, availability: str | None = None, layout: int = 0, images: str = '', image_count: int = 0, navigation: str = '', nav_count: int = 0, items: str | None = None, item_count: int = 0, etag: str | None = None, last_modified: str | None = None)`** (class). Fingerprints of one fetch of a page.
  - `classmethod from_dict(cls, data: Mapping[str, Any]) -> PageSnapshot`
  - `to_dict(self) -> dict[str, Any]`
- **`Run(id: int, name: str, started: float, finished: float | None = None, status: str | None = None, stats: dict[str, Any] = ..., pages: int = 0)`** (class). One crawl recorded in the history.
  - `describe(self) -> str`
- **`compare_snapshots(old: PageSnapshot, new: PageSnapshot) -> PageChange | None`**. What changed from ``old`` to ``new`` (two snapshots of one URL), or ``None``.
- **`snapshot_page(page: Any, *, url: str | None = None, status: int | None = None, items: Iterable[Any] | None = None, fetched_at: float | None = None) -> PageSnapshot`**. Fingerprints of a page (a :class:`~wintergrab.Response`, a Selector or HTML text).

## `wintergrab.runs`: Run records and replay

- **`DEFAULT_WORKSPACE`** = `'.wintergrab'`
- **`ReplayResult(run: Run, crawl: CrawlResult | None, output: Path, diff: DatasetDiff, missing: int = 0)`** (class). What replaying a recorded run gave.
  - `summary(self) -> str`
- **`Run(id: str, number: int, name: str, started: float, finished: float | None = None, status: str = 'running', recorded: bool = False, resumed: bool = False, stats: dict[str, Any] = ..., settings: dict[str, Any] = ..., recipe: dict[str, Any] = ..., output: str | None = None, failures: list[dict[str, Any]] = ..., error: str | None = None, label: str | None = None, quality: list[dict[str, Any]] = ..., directory: Path = ...)`** (class). One crawl, as the registry keeps it.
  - `describe(self) -> str`: One line: id, when, status, what it did.
  - `details(self) -> str`: Several lines: the run, its settings and stats, its failures, its files.
  - `events(self, kinds: str | Sequence[str] | None = None) -> Iterator[dict[str, Any]]`: The run's events (as dicts), of ``kinds`` or all.
  - `items(self) -> Iterator[dict[str, Any]]`: The items the run wrote (recorded runs).
  - `metrics(self) -> dict[str, Any]`: The crawl's metrics (rates, latency, domains...): the last ones kept while it ran, or its final ones (``{}`` for runs kept before metrics were).
  - `to_dict(self) -> dict[str, Any]`
- **`RunRecorder(registry: RunRegistry, spider: Spider, *, record: bool)`** (class). Keeps a run's record while the engine crawls (used by the engine; see the module docs).
  - `event_kinds(self) -> frozenset[str]`: The events worth keeping: per-response ones only when recording (their timings).
  - `finish(self, result: CrawlResult | None, *, status: str, error: BaseException | None = None)`
  - `item(self, item: Any)`
  - `metrics(self, snapshot: Mapping[str, Any])`: Keep the crawl's metrics as they are now (``metrics.json``, read while the crawl runs).
  - `start(self, *, resumed: bool) -> Run`
- **`RunRegistry(directory: str | os.PathLike[str] = '.wintergrab')`** (class). The runs kept in a workspace directory (``runs/run-N``; see the module docs).
  - `classmethod coerce(cls, value: Any) -> RunRegistry | None`: A spider's ``run_registry`` setting: ``True`` (the default workspace), a directory, or a registry.
  - `create(self, name: str, *, recorded: bool = False, resumed: bool = False, settings: Mapping[str, Any] | None = None, recipe: Mapping[str, Any] | None = None, label: str | None = None) -> Run`: A new run, numbered after the last one (safe with several processes: the directory decides).
  - `get(self, ref: str | int) -> Run`: A run by id (``"run-7"``), number (``7``, ``"7"``), or ``"last"``.
  - `remove(self, ref: str | int) -> Run`: Delete a run and everything it kept.
  - `runs(self, limit: int | None = None) -> list[Run]`: The runs, newest first.
  - `save(self, run: Run)`
- **`replay(run: Run | str | int, spider: type[Spider] | None = None, *, registry: RunRegistry | str | os.PathLike[str] | None = None, output: str | os.PathLike[str] | None = None, key: str | Sequence[str] | None = None, **settings: Any) -> ReplayResult`**. Crawl again from what ``run`` recorded, without touching the network, and compare the items.

## `wintergrab.project`: Projects and the scheduler

- **`PROJECT_FILES`** = `('wintergrab.yaml', 'wintergrab.yml', 'wintergrab.toml', 'wintergrab.json')`
- **`Job(name: str, kind: str, target: str, options: dict[str, Any] = ..., settings: dict[str, Any] = ..., schedule: Schedule | None = None, enabled: bool = True, start_within: timedelta | None = None, description: str = '', watch: str | None = None, check: timedelta = ..., after: tuple[str, ...] = ())`** (class). One job of a project (see the module docs).
  - `command(self, *, workspace: str | None = None, project: str | None = None) -> list[str]`: The ``wintergrab`` command line that runs the job.
  - `trigger(self) -> str`: What runs the job, in words: ``"every 2 hours"``, ``"when https://.../sitemap.xml changes"``...
- **`JobResult(job: Job, status: str, exit_code: int, started: float, finished: float, run: Run | None = None, log: Path | None = None)`** (class). What running a job gave.
  - `describe(self) -> str`
- **`Project(path: str | os.PathLike[str])`** (class). A project file (see the module docs).
  - `dependents(self, name: str) -> list[Job]`: The jobs that run after ``name``.
  - `run_job(self, job: Job, *, log_file: Path | None = None, runner: Callable[..., int] | None = None) -> JobResult`: Run ``job`` in a process of its own (its output to ``log_file``, or through).
  - `select(self, names: Iterable[str] | None) -> list[Job]`
  - `webhooks(self) -> list[Webhook]`: The project's webhooks (``${NAME}`` read from the environment now).
- **`Scheduler(project: Project, *, now: Callable[[], datetime] = datetime.now, sleep: Callable[[float], None] = time.sleep, runner: Callable[..., int] | None = None, webhooks: Sequence[Webhook] | None = None, checker: Callable[..., Any] | None = None)`** (class). Runs a project's jobs on their schedules (see the module docs).
  - `check(self, job: Job) -> Any`: Check ``job``'s watched URL now, and keep what was found (a :class:`~wintergrab.watch.WatchCheck`).
  - `check_due(self, job: Job, now: datetime) -> datetime | None`: When ``job``'s watched URL is checked next (right away the first time).
  - `due(self, job: Job, now: datetime) -> datetime | None`: When ``job`` runs, or its watched URL is checked, next (``None``: never; see :meth:`scheduled` and :meth:`check_due`).
  - `last_run(self, job: Job) -> datetime | None`
  - `loop(self, *, until: Callable[[], bool] | None = None)`: Run jobs as they fall due, until stopped (Ctrl+C, :meth:`stop`, or ``until()``).
  - `plan(self) -> list[tuple[Job, datetime | None]]`: Every job with a schedule or a watched URL, and when it runs (or is checked) next, soonest first.
  - `run(self, job: Job, *, logged: bool = True, trigger: str = 'manual', reason: str | None = None, _chain: frozenset[str] = set()) -> JobResult`: Run ``job`` now (its output in the workspace's ``logs/``, or through with ``logged=False``), tell the webhooks, remember when, and, when it succeeded, run the jobs that come after it.
  - `run_due(self) -> list[JobResult]`: Run the jobs that are due now, one after the other (a watched URL is checked first: its job runs when it changed), and the jobs that run after them.
  - `scheduled(self, job: Job, now: datetime) -> datetime | None`: When ``job``'s schedule runs it next (``None``: never).
  - `stop(self)`
  - `watch_state(self, job: Job) -> dict[str, Any]`: What the last check of ``job``'s watched URL found (``{}`` before the first).
- **`find_project(path: str | os.PathLike[str] | None = None) -> Project`**. The project in ``path`` (a file, or a directory holding ``wintergrab.yaml``...), or the current directory's.
- **`starter_project() -> str`**. The ``wintergrab.yaml`` that ``wintergrab init`` writes.

## `wintergrab.schedules`: Schedules

- **`Cron(text: str, minutes: frozenset[int], hours: frozenset[int], days: frozenset[int], months: frozenset[int], weekdays: frozenset[int], any_day: bool, any_weekday: bool, zone: tzinfo | None = None)`** (class). A cron expression: minute, hour, day of the month, month, day of the week.
  - `next(self, after: datetime, last: datetime | None = None) -> datetime | None`: When the job is due next, from ``after`` on (``after`` itself: due now); ``last``: when it last ran.
  - `classmethod parse(cls, text: str, zone: tzinfo | None = None) -> Cron`
- **`Interval(text: str, seconds: float)`** (class). Every so many seconds, counted from the last run (the first run is right away).
  - `next(self, after: datetime, last: datetime | None = None) -> datetime | None`: When the job is due next, from ``after`` on (``after`` itself: due now); ``last``: when it last ran.
- **`Once(text: str, at: datetime)`** (class). One time.
  - `next(self, after: datetime, last: datetime | None = None) -> datetime | None`: When the job is due next, from ``after`` on (``after`` itself: due now); ``last``: when it last ran.
- **`Schedule()`** (class). When a job runs (see the module docs).
  - `next(self, after: datetime, last: datetime | None = None) -> datetime | None`: When the job is due next, from ``after`` on (``after`` itself: due now); ``last``: when it last ran.
- **`parse_duration(text: str | int | float) -> timedelta`**. A duration: ``"30 minutes"``, ``"2h"``, ``"1 day"``, or a number of seconds.
- **`parse_schedule(text: str | int | float, *, timezone: str | tzinfo | None = None) -> Schedule`**. A :class:`Schedule` from its text (see the module docs), or a number of seconds between runs.

## `wintergrab.watch`: Watching URLs for changes

- **`WatchCheck(url: str, changed: bool = False, first: bool = False, kind: str = 'page', summary: str = '', status: int | None = None, error: str | None = None, state: dict[str, Any] = ...)`** (class). What a check found.
- **`check(url: str, previous: dict[str, Any] | None = None, *, obey_robots: bool = True, user_agent: str = '*', timeout: float = 30.0) -> WatchCheck`**. Check ``url`` against ``previous`` (the last check's :attr:`WatchCheck.state`; see the module docs).

## `wintergrab.webhooks`: Webhooks

- **`PER_PAGE`**: a frozenset
- **`SIGNATURE_HEADER`** = `'X-Wintergrab-Signature'`
- **`Webhook(url: str, *, events: Iterable[str] | None = None, secret: str | None = None, batch: int = 100, interval: float = 1.0, timeout: float = 10.0, max_queue: int = 10000, headers: Mapping[str, str] | None = None)`** (class). Posts events to ``url`` (an :class:`~wintergrab.events.EventBus` subscriber; see the module docs).
  - `close(self, timeout: float = 15.0)`: Deliver what is waiting, and stop (waits at most ``timeout`` seconds).
  - `classmethod coerce(cls, value: Any) -> Webhook`: A webhook, from itself, a URL, or a mapping of its arguments (``{"url": ..., "events": [...]}``).
  - `kinds(self) -> frozenset[str] | None`: The kinds to subscribe to (``None``: all).
  - `post_event(self, data: dict[str, Any])`: Queue one event (a dict with at least ``"event"``) for delivery.
- **`sign(body: bytes, secret: str) -> str`**. The signature of ``body``: ``sha256=<hex HMAC-SHA256>``.
- **`verify(body: bytes, signature: str | None, secret: str) -> bool`**. Whether ``signature`` (the header's value) signs ``body`` with ``secret`` (constant time).

## `wintergrab.events`: Events

- **`EVENT_KINDS`**: a frozenset
- **`Event(kind: str, data: dict[str, Any] = ..., time: float = ..., origin: str | None = None)`** (class). One thing that happened.
  - `get(self, key: str, default: Any = None) -> Any`
  - `to_dict(self) -> dict[str, Any]`
- **`EventBus(origin: str | None = None)`** (class). Delivers events to subscribers (thread-safe to subscribe; emit from the crawl's loop or any thread).
  - `close(self)`: Close subscribers that have a ``close()`` method (file sinks).
  - `drain(self, timeout: float = 10.0)`: Wait (up to ``timeout``) for scheduled coroutine handlers to finish.
  - `emit(self, kind: str, /, **data: Any)`: Deliver an event now.
  - `subscribe(self, handler: Handler, kinds: str | Iterable[str] | None = None) -> Callable[[], None]`: Call ``handler(event)`` for events of ``kinds`` (all when ``None``).
  - `wants(self, kind: str) -> bool`: Whether anyone listens to ``kind`` (check before building an expensive event).
- **`EventRecorder(maxlen: int | None = 10000)`** (class). Keeps the last ``maxlen`` events in memory (tests, dashboards, debugging).
  - `kinds(self) -> list[str]`
  - `of(self, kind: str) -> list[Event]`
- **`JsonlEventSink(path: str | os.PathLike[str], *, flush_every: int = 32)`** (class). Appends events as JSON lines to a file (a subscriber; pass it to :meth:`EventBus.subscribe`).
  - `close(self)`
- **`LoggingEventSink(level: int = 20)`** (class). Logs events on the ``wintergrab.events`` logger (one line each).

## `wintergrab.models`: Language models

- **`PROVIDERS`**: a dict
- **`Anthropic(model: str, *, api_key: str | None = None, base_url: str | None = None, timeout: float = 120.0, max_tokens: int = 1024, temperature: float = 0.0)`** (class). The Anthropic Messages API (``POST {base_url}/v1/messages``).
  - `complete(self, prompt: str, *, system: str | None = None, images: Sequence[Image] = (), json_output: bool = False) -> str`: The model's answer to ``prompt``.
- **`Image(data: bytes, media_type: str = 'image/png')`** (class). An image for a model that reads images (a screenshot, a chart): its bytes and media type.
- **`ModelProvider(model: str, *, api_key: str | None = None, base_url: str | None = None, timeout: float = 120.0, max_tokens: int = 1024, temperature: float = 0.0)`** (class). Asks one model through one API (see the module docs).
  - `complete(self, prompt: str, *, system: str | None = None, images: Sequence[Image] = (), json_output: bool = False) -> str`: The model's answer to ``prompt``.
  - `key_required(self, base_url: str | None) -> bool`: Whether the API at ``base_url`` needs a key.
- **`Ollama(model: str, *, json_mode: bool = True, **options: Any)`** (class). A local Ollama server, through its OpenAI-compatible API (no key).
  - `key_required(self, base_url: str | None) -> bool`: Whether the API at ``base_url`` needs a key.
- **`OpenAICompatible(model: str, *, json_mode: bool = True, **options: Any)`** (class). The chat completions API (``POST {base_url}/chat/completions``) of OpenAI and of servers that speak it.
  - `complete(self, prompt: str, *, system: str | None = None, images: Sequence[Image] = (), json_output: bool = False) -> str`: The model's answer to ``prompt``.
  - `key_required(self, base_url: str | None) -> bool`: Whether the API at ``base_url`` needs a key.
- **`load_model(spec: str, **options: Any) -> ModelProvider`**. A provider from ``"provider:model"`` (``"anthropic:NAME"``, ``"ollama:NAME"``), with its options (``base_url``, ``api_key``, ``timeout``...).
- **`register_provider(name: str, provider: type[ModelProvider])`**. Add a provider for ``--model NAME:MODEL``.

## `wintergrab.plugins`: Plugins

- **`COMMANDS`** = `{}`
- **`PluginInfo(name: str, target: str, package: str | None = None, version: str | None = None, added: list[str] = ..., error: str | None = None)`** (class). One plugin: where it comes from, what it added, and what went wrong.
  - `describe(self) -> str`
- **`Registry(info: PluginInfo)`** (class). What a plugin is given to add what it brings (see the module docs).
  - `command(self, name: str, add_arguments: Callable[[Any], None], run: Callable[[Any], int], *, help: str = '')`: A command: ``wintergrab NAME ...`` (``add_arguments(parser)``, then ``run(args)`` gives the exit status).
  - `exporter(self, key: str, exporter: Any)`: An output: ``".ext"`` or a URL ``"scheme"`` (see :func:`~wintergrab.spider.exporters.register_exporter`).
  - `field_type(self, name: str, normalizer: Any, json_schema: Any = None)`: A schema field type (see :func:`~wintergrab.data.schema.register_type`).
  - `model_provider(self, name: str, provider: Any)`: A model provider for ``--model NAME:MODEL`` (see :mod:`wintergrab.models`).
  - `reader(self, key: str, reader: Any)`: An input for :func:`~wintergrab.data.io.read_records`: ``".ext"`` or a URL ``"scheme"``.
  - `stage(self, stage: Any)`: A data pipeline stage class, used by its ``kind`` in pipeline files.
  - `strategy(self, strategy: Any, *, before: str | None = None)`: An extraction strategy class, tried by extractors (after the built-in ones, or ``before`` one).
- **`load_plugins(*, force: bool = False) -> list[PluginInfo]`**. Load the installed plugins, once (``force``: again).
- **`plugins() -> list[PluginInfo]`**. The plugins loaded so far.

## `wintergrab.storage.parquet`: Parquet

- **`ParquetExporter(path: Path, *, append: bool)`** (class). Items in a Parquet file (see the module docs).
  - `close(self)`
  - `flush(self)`: Push buffered items to disk (called on checkpoints and at the end).
  - `write(self, item: Any)`
- **`read_parquet(path: str | Path) -> Iterator[dict[str, Any]]`**. The records of a Parquet file, with the JSON columns wintergrab wrote decoded again.
- **`write_parquet(path: Path, records: Any)`**. Write the records ``records()`` gives (called twice: once to type the columns) to ``path``.

## `wintergrab.storage.xlsx`: Excel

- **`XlsxExporter(path: Path, *, append: bool)`** (class). Items in an Excel workbook (see the module docs).
  - `close(self)`
  - `flush(self)`: Push buffered items to disk (called on checkpoints and at the end).
  - `write(self, item: Any)`
- **`read_xlsx(path: str | Path) -> Iterator[dict[str, Any]]`**. The rows of a workbook's sheets as records (the first row of each names the fields), with the JSON columns wintergrab wrote decoded again.
- **`write_xlsx(path: Path, records: Any, *, sheet: str = 'items')`**. Write the records ``records()`` gives (called twice: once to find the columns) to ``path``.

## `wintergrab.storage.postgres`: PostgreSQL

- **`PostgresExporter(url: str, *, append: bool, unique_key: str | None = None)`** (class). Items as the rows of a PostgreSQL table (see the module docs).
  - `close(self)`
  - `flush(self)`: Push buffered items to disk (called on checkpoints and at the end).
  - `write(self, item: Any)`
- **`read_postgres(url: str) -> Iterator[dict[str, Any]]`**. The rows of a table as records: a table wintergrab wrote gives its fields back by their names.

## `wintergrab.storage.mysql`: MySQL and MariaDB

- **`MySQLExporter(url: str, *, append: bool, unique_key: str | None = None)`** (class). Items as the rows of a MySQL or MariaDB table (see the module docs).
  - `close(self)`
  - `flush(self)`: Push buffered items to disk (called on checkpoints and at the end).
  - `write(self, item: Any)`
- **`read_mysql(url: str) -> Iterator[dict[str, Any]]`**. The rows of a table as records: a table wintergrab wrote gives its fields back by their names and types (nested values as the objects and lists they were).

## `wintergrab.storage.mongodb`: MongoDB

- **`MongoExporter(url: str, *, append: bool, unique_key: str | None = None)`** (class). Items as the documents of a MongoDB collection (see the module docs).
  - `close(self)`
  - `flush(self)`: Push buffered items to disk (called on checkpoints and at the end).
  - `write(self, item: Any)`
- **`read_mongodb(url: str) -> Iterator[dict[str, Any]]`**. The documents of a collection as records, in the order they were first written (without MongoDB's ``_id``).

## `wintergrab.storage.objects`: S3 and S3-compatible object storage

- **`ObjectExporter(url: str, *, append: bool, unique_key: str | None = None)`** (class). Items in an object of an S3 bucket, in the format its extension names (see the module docs).
  - `close(self)`
  - `flush(self)`: Push buffered items to disk (called on checkpoints and at the end).
  - `write(self, item: Any)`
- **`read_object(url: str) -> Iterator[dict[str, Any]]`**. The records in an object of an S3 bucket, read as a file of its extension is.

## `wintergrab.dashboard`: The dashboard

- **`Dashboard(workspace: str | Path = '.wintergrab', project: Any = None)`** (class). What the dashboard shows: a workspace's runs, and a project's jobs.
  - `jobs(self) -> list[dict[str, Any]]`: The project's jobs: what each does, its schedule, when it runs next, and its last run.
  - `run(self, ref: str) -> Run`
  - `run_data(self, run: Run) -> dict[str, Any]`: Everything about a run, as JSON values (the run page, and ``/api/runs/RUN``).
  - `runs(self, limit: int = 200) -> list[Run]`
  - `state(self, run: Run) -> str`: The run's status; ``"not responding"`` for a run that says it runs but keeps no metrics.
  - `summary(self, run: Run) -> dict[str, Any]`: A run's record without credentials (the ones kept before runs left them out, too).
- **`EventSummary(counts: Counter[str] = ..., failures: list[dict[str, Any]] = ..., blocked: Counter[str] = ..., backoffs: Counter[str] = ..., extraction: dict[str, Any] = ..., changes: dict[str, Any] | None = None, updated: list[dict[str, Any]] = ..., created: list[str] = ..., deleted: list[str] = ..., quality: list[dict[str, Any]] = ..., schema: list[dict[str, Any]] = ..., notable: list[dict[str, Any]] = ..., truncated: bool = False)`** (class). A run's events, summed up (see :func:`summarize_events`).
  - `to_dict(self) -> dict[str, Any]`
- **`serve(workspace: str | Path = '.wintergrab', *, project: Any = None, host: str = '127.0.0.1', port: int = 8710) -> _Server`**. A dashboard server for ``workspace`` (not started: call ``serve_forever()``, or run it in a thread).
- **`summarize_events(path: Path, *, max_bytes: int = 67108864) -> EventSummary`**. Sum up a run's ``events.jsonl`` (its last ``max_bytes`` when it is bigger).

## `wintergrab.builder`: The visual builder

- **`DEFAULT_PORT`** = `8711`
- **`BuilderSession(page: Response, output: str | os.PathLike[str], *, name: str | None = None)`** (class). A page being built on (see the module docs).
  - `card(self, number: Any) -> dict[str, Any]`: The repeated card element ``number`` is in: its selector, how many there are, their numbers, and fields found in them.
  - `element(self, number: Any) -> etree._Element`: The page's element numbered ``number``.
  - `field(self, number: Any, container: str | None = None) -> dict[str, Any]`: Selectors for a field read from element ``number`` (inside a card when ``container`` is given), each with what it reads, and a name, a type and a way to read it guessed from the element.
  - `next_page(self, number: Any) -> dict[str, Any]`: A selector for the link to the next page (element ``number``, or the link it is in).
  - `number(self, el: etree._Element) -> int | None`: ``el``'s number (to show it in the page).
  - `save(self, spec: Mapping[str, Any]) -> dict[str, Any]`: Write ``spec`` to the builder's file (nowhere else), and say how to use it.
  - `state(self) -> dict[str, Any]`
  - `table(self, number: Any) -> dict[str, Any]`: The table element ``number`` is in: a table of records (a card per row, a field per column), or of one record's properties (a field per row, found by its label).
  - `test(self, spec: Mapping[str, Any]) -> dict[str, Any]`: The records ``spec`` reads on the page, as the extractor reads them.
  - `view_html(self) -> str`: The page as the builder shows it: numbered elements, no scripts, links resolved against the page's address.
- **`serve(session: BuilderSession, *, host: str = '127.0.0.1', port: int = 8711) -> _Server`**. A builder server for ``session`` (not started: call ``serve_forever()``, or run it in a thread).

## `wintergrab.redact`: Keeping credentials out

- **`REDACTED`** = `'***'`
- **`is_sensitive(name: Any) -> bool`**. Whether a setting, header or field named ``name`` holds a credential (``X-Api-Key``, ``accessToken`` and ``session_id`` do; ``tokenizer`` does not).
- **`redact(value: Any, name: Any = None) -> Any`**. ``value`` (a setting, a header mapping, JSON data...) with its credentials replaced, recursively.
- **`redact_argv(argv: Sequence[str]) -> list[str]`**. A command line with its credentials replaced: URL user information, the values of headers and ``NAME=VALUE`` settings named like credentials (``-H "Authorization: ..."``, ``-s token=...``), and credentials inside JSON values (``-s 'default_headers={"Cookie": "..."}'``).
- **`redact_url(url: str) -> str`**. ``url`` without its user information (``http://***@proxy:8080``).
