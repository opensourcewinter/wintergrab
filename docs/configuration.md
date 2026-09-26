# Configuration

Everything wintergrab does can be set in code. The command line, and the
files below, are ways to write the same settings down. This page says
where each kind of setting lives.

## Spider settings

A spider's settings are its class attributes, and every one can be
overridden per instance:

```python
class Books(Spider):
    concurrency = 8
    output = "books.jsonl"

Books(max_pages=100).run()
```

On the command line, `wintergrab crawl` has options for the common ones
and `-s NAME=VALUE` for any other (`-s retries=5`,
`-s 'allowed_statuses=[404]'`). The values are parsed as JSON when they can
be. The [settings reference](spiders.md#settings-reference) lists them all.

## Files

| File | Holds | See |
|---|---|---|
| A schema (`product.schema.json`, or a [template](extraction.md#templates) name) | The fields of a record: types, selectors, validation | [data](data.md#schemas), [extraction](extraction.md) |
| A pipeline (`clean.pipeline.yaml`) | Stages that clean, validate, filter and enrich records | [data](data.md#pipelines-as-configuration) |
| A project (`wintergrab.yaml`, `.toml` or `.json`) | Jobs, their schedules and triggers, and webhooks | [projects](projects.md) |
| A goal plan (`--save-plan plan.json`) | A reviewed crawl plan, to run again with `--plan` | [goals](goals.md) |
| An extraction test suite (a directory with `suite.json`) | Pages with the values expected from them | [testing](testing.md) |
| A proxy list (`--proxy-file`) | One proxy URL per line | [responsible access](responsible-access.md#proxies) |

YAML files need PyYAML (`pip install "wintergrab[yaml]"`). TOML and JSON
need nothing more.

## Where things are kept

| Setting | Default | What |
|---|---|---|
| `crawl_dir` | none | A crawl's checkpoint, so it can pause and resume |
| `--workspace`, a project's `workspace:` | `.wintergrab` | Run records, logs, schedule and watch state |
| `history` | none | Page snapshots across runs |
| `cache`, `--cache-dir` | none | The HTTP cache |
| `WINTERGRAB_ADAPTIVE_DB` | the user's cache directory | Adaptive selector fingerprints |

## Environment variables

| Variable | What it does |
|---|---|
| `OPENAI_API_KEY`, `ANTHROPIC_API_KEY`, `OLLAMA_API_KEY` | Keys of the [model](models.md) providers |
| `PGPASSWORD` (and `~/.pgpass`) | The PostgreSQL password, kept out of the output URL |
| `MYSQL_PWD` (and `~/.my.cnf`) | The MySQL or MariaDB password, kept out of the output URL |
| `WINTERGRAB_MONGODB_PASSWORD` | The MongoDB password, for an output URL that names a user without one |
| `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY`, `AWS_PROFILE`, `AWS_ENDPOINT_URL`... | S3 credentials and endpoint, for `s3://` outputs, as the AWS tools read them |
| `WINTERGRAB_UPLOADS` | Where items for an `s3://` output wait for their upload (default `.wintergrab/uploads`) |
| `BRAVE_SEARCH_API_KEY`; `GOOGLE_API_KEY` and `GOOGLE_CSE_ID`; `SEARXNG_URL` | The search APIs `wintergrab search` and `goal --find-sites` ask ([search](search.md)) |
| `${NAME}` in `-H`, `--cookie`, `--proxy`, and a project's webhooks, `watch:`, jobs' `header:`, `cookie:`, `proxy:` and `credentials:` | Any variable: the process reads it, and the command line holds the name ([credentials](responsible-access.md#credentials)) |
| `WINTERGRAB_PLUGINS=0` | Load no [plugin](plugins.md) |
| `WINTERGRAB_BROWSER_PATH` | A Chrome or Chromium binary to use instead of Playwright's |
| `WINTERGRAB_ADAPTIVE_DB` | Where adaptive selectors keep their fingerprints |
| `WINTERGRAB_LIVE=1` | (tests) Run the tests against real websites |
| `WINTERGRAB_TEST_POSTGRES` | (tests) A PostgreSQL URL to test the PostgreSQL output against |
| `WINTERGRAB_TEST_MYSQL`, `WINTERGRAB_TEST_MONGODB` | (tests) A MySQL (or MariaDB) and a MongoDB URL, the same way |

The usual proxy variables (`HTTPS_PROXY`...) apply to model and webhook
requests, which use the standard library. Crawls use the proxies you give
them (`proxies`, `--proxy`).

## Secrets

Keep credentials out of files that are shared or committed:

- **Webhooks**: read secrets from the environment with `${NAME}`.
- **Logins and keys for a site**: `-H 'Authorization: Bearer ${TOKEN}'`,
  `--cookie 'session=${SESSION}'`, or `credentials=[Credentials(...)]`:
  they go to that site only, and the command line holds the variable's
  name ([credentials](responsible-access.md#credentials)).
- **Projects**: `credentials:` gives each job the variables it needs and
  keeps the others from it ([projects](projects.md#credentials)).
- **Models** and **search APIs**: read keys from the environment.
- **Databases**: use `PGPASSWORD` or `~/.pgpass` (PostgreSQL), `MYSQL_PWD`
  or `~/.my.cnf` (MySQL, MariaDB), `WINTERGRAB_MONGODB_PASSWORD` (MongoDB).
- **S3**: the AWS credential chain (`AWS_ACCESS_KEY_ID`..., profiles, roles);
  an `s3://` URL holding credentials is refused.
- **Proxies**: a proxy file can hold its passwords, or `--proxy
  'http://user:${PROXY_PASSWORD}@proxy.example:8080'`.

Wherever wintergrab writes settings or command lines down, it leaves
credentials out: run records, logs, events, the dashboard. See
[runs](runs.md#what-a-run-keeps).
