# Upgrading from 0.2

Code written for 0.2 keeps working. This page lists what behaves
differently, what code may need a change, and what wintergrab now writes
on its own. The [changelog](../CHANGELOG.md) has everything that is new.

## What behaves differently

- **Failed requests say why.** Fetch failures are specific `FetchError`
  subclasses (`NetworkError`, `FetchTimeout`, `ProxyError`, `PolicyError`...),
  so `except FetchError` still catches them all. Invalid URLs and redirect
  loops are no longer retried.
- **Quick crawls skip what is not a page.** `wintergrab crawl URL` skips
  links to images, media, archives and crawler traps. `-s url_rules=null`
  turns that off.
- **`max_output_bytes`** counts the bytes each exporter produces, checked
  after every item, rather than the file's size on disk.
- **Plugins load by themselves.** Installed packages that declare a
  `wintergrab.plugins` entry point are loaded when first needed.
  `WINTERGRAB_PLUGINS=0` turns them off.
- **A template name is a schema.** `--extract product` and
  `Extractor("product")` use the product template when no file of that name
  exists. Before, they failed.

## Code that may need a change

- `wintergrab.extraction.STRATEGIES` is a list, not a tuple, so plugins can
  add to it. Copy it (`list(STRATEGIES)`) before changing your own.
- `wintergrab.spider.exporters.EXPORTERS` values may be `"module:Class"`
  strings, imported when first used. Open outputs with `open_exporter()`
  rather than by looking a class up there.
- `wintergrab.events.EVENT_KINDS` holds more kinds. A subscriber to every
  event also gets the new ones (`site_changed`, `record_updated`,
  `job_finished`...).
- `HTTPError` is an alias of `HTTPStatusError`.

## What wintergrab now writes

- **`.wintergrab/`**: once it exists in the current directory, every
  `wintergrab crawl` and `goal` run keeps a record there (`runs/run-N`). A
  project's jobs, its schedule and its watch state live there too. Remove
  old runs with `wintergrab runs --remove RUN`.
- **Spools**: a Parquet or Excel output is spooled in `.NAME.spool.jsonl`
  beside it until the crawl ends.
- **Credentials are left out** of the settings and command lines kept in
  run records, job logs and job events.

## Dependencies

- On Windows, `tzdata` is installed with wintergrab, for schedules in time
  zones.
- New optional extras: `parquet`, `xlsx`, `postgres`. `all` includes them.
