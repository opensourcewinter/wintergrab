# Security

## Reporting a vulnerability

Please don't open a public issue. Report it privately through GitHub:
**Security → Report a vulnerability** on this repository. Fixes go into
the latest release.

## What wintergrab trusts, and what it doesn't

Web pages are untrusted input. wintergrab is built so that a hostile page
can't do the following:
- **Inject SQL through the SQLite exporter.** Item keys become column names
  containing only letters, digits and underscores, and every value is passed
  as a query parameter.
- **Read local files or reach the network through XML.** Sitemaps and XML
  pages are parsed with entity resolution and network access turned off.
- **Stall a crawl with crafted text.** The regular expressions that run on
  page content were tested against hostile inputs of several megabytes, and
  their running time grew linearly.
- **Feed internal data to your callbacks through a redirect.** When a page
  on an allowed domain redirects off `allowed_domains`, the response is
  dropped and counted as `offsite_redirects`.
- **Run code in the dashboard.** What crawls collected is shown as escaped
  text, under a Content Security Policy that allows no script. The
  dashboard only reads, listens on 127.0.0.1, and refuses requests
  addressed to other host names (DNS rebinding).
- **Run code in the builder, or drive it.** `wintergrab build` shows the
  page with its scripts, frames, plugins, event handlers, `javascript:`
  links and refresh tags removed. It shows it in a frame sandboxed without
  scripts, under a policy that allows none, even when the page is opened
  on its own. Only the page's images, styles and fonts load, from wherever
  the page takes them, with no referrer.
  Changes need a token that only the builder's own page holds, sent as JSON
  from the same origin. The builder listens on 127.0.0.1, refuses other
  host names, and writes to the one file named when it started.
- **Plant a formula in a spreadsheet.** Excel output (`.xlsx`) writes text
  as text, so a crawled `=HYPERLINK(...)` never becomes a formula. CSV
  files carry no types: open crawled CSV in a spreadsheet with that in mind,
  or write `.xlsx`.
- **Reach into a database.** The PostgreSQL output quotes every identifier
  and passes every value as a parameter. It only writes tables it created.
- **Make a model's answer count.** A language model's answers are checked
  against the page, and a value the page does not contain is never taken.
  A site a model names that the request did not is dropped.

These inputs are trusted, so only use ones you control:
- **Spider files** run by `wintergrab crawl` are Python code.
- **Crawl state** (`crawl_dir`) is stored as pickle files. Loading someone
  else's crawl state can run their code.
- **Schema files** (`--schema`) and proxy lists are your own configuration.
- **Pipeline files** may name Python functions to call, but they are only
  imported when you allow it (`allow_imports=True`, `--allow-imports`).
  Without that, a pipeline file can only use the built-in stages and the
  expression language, which has no attribute access, imports or loops and
  can read records but not files, the network or Python objects.
- **Project files** (`wintergrab.yaml`) run their jobs as commands.
- **Plugins** are installed packages, and run as wintergrab does.
  `WINTERGRAB_PLUGINS=0` loads none.

## Dependencies

wintergrab depends on few packages: curl_cffi, lxml and cssselect, and
one or two for each extra (Playwright, pyarrow, openpyxl...). On every
push, CI installs wintergrab with every extra in a fresh environment and
audits the packages installed with it, dependencies of dependencies
included, for known vulnerabilities (`pip-audit`, the `dependency audit`
job). To run the same check:

```bash
python -m venv /tmp/wg && /tmp/wg/bin/pip install ".[all]"
/tmp/wg/bin/pip freeze --exclude-editable | grep -v "^wintergrab" > deps.txt
pip install pip-audit && pip-audit --disable-pip --no-deps -r deps.txt
```

## A site's refusals

wintergrab does not try to get past a block, a bot check, a rate limit or
a login it was not given: a refused page is reported and its site slowed
down, and the browser does not hide that it is automated. See
[docs/responsible-access.md](docs/responsible-access.md).

## Credentials

- **Keep them in the environment.** Use `${NAME}` in a project's webhooks,
  `OPENAI_API_KEY` and the like for models, and `PGPASSWORD` for
  PostgreSQL. A job's options take no variables: they become a command
  line, which others on the machine can read.
- **Records leave them out.** The settings and command lines kept in run
  records (and shown by `wintergrab runs` and the dashboard), a project's
  job events and job logs, and an output URL wherever it is shown, leave
  out passwords in URLs (`http://***@proxy`) and values named like
  credentials (`Authorization`, `Cookie`, `api_token`...). The pages a
  crawl visits are not rewritten: a start URL that holds a password shows
  it in that crawl's events and logs, so keep passwords out of URLs.
- **Webhooks are signed.** With a `secret`, each delivery carries
  `X-Wintergrab-Signature` (HMAC-SHA256). Check it before trusting a
  delivery.
- **Models see the page.** A language model is sent the pages it is asked
  about. Choose one you may send them to, or run one locally.

## Crawling from sensitive networks

By default a crawl may connect anywhere your machine can reach. When you
crawl sites you don't trust from a machine that can reach internal services
(a cloud instance's metadata endpoint, an intranet), set a network policy:

```python
class MySpider(Spider):
    network_policy = "public"   # or wg.get(url, network_policy="public")
```

Requests to private, loopback, link-local (cloud metadata), multicast and
reserved addresses are then refused before connecting. Host names are
resolved and every resulting address is checked; every redirect hop is
checked; and the address curl actually connected to is checked afterwards,
which defeats DNS rebinding. Details and limits (proxies, browsers) are in
[docs/fetching.md](docs/fetching.md#network-policy-ssrf-protection).

A redirect that leaves `allowed_domains` is also dropped, but only *after*
the request was made. The network policy is the protection that stops the
request itself. For defence in depth, also restrict the machine's outbound
network access.
