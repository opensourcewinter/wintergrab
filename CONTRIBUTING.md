# Contributing

Bug reports, fixes and new features are welcome. For anything larger than
a small fix, open an issue first so we can agree on the approach. Everyone
taking part follows the [code of conduct](CODE_OF_CONDUCT.md).

## Setup

```bash
git clone https://github.com/opensourcewinter/wintergrab
cd wintergrab
python -m venv .venv && . .venv/bin/activate      # Windows: .venv\Scripts\activate
pip install -e ".[dev,speed]"
playwright install chromium                        # for the browser tests
```

## Checks

Run these before you push. CI runs the same checks on Linux (Python 3.10 to
3.14), macOS and Windows.

```bash
ruff check . && ruff format --check .
mypy
pytest
```

There are three kinds of test:

| Kind | Command | Needs |
|---|---|---|
| Unit and integration | `pytest` | Nothing. A local test site replicates the pages the tests need, so no internet is used. |
| Browser | `pytest` (same run) | Playwright and Chromium. Skipped when they are missing. |
| Live | `WINTERGRAB_LIVE=1 pytest -m live` | Internet. Runs the examples against quotes.toscrape.com and books.toscrape.com (sites built for scraping practice) and fetches a few pages from pypi.org. Skipped by default and in CI. Run it before every release. |

The PostgreSQL output is tested against a real server when
`WINTERGRAB_TEST_POSTGRES` names one
(`WINTERGRAB_TEST_POSTGRES=postgresql://user@localhost/db pytest tests/test_storage.py`),
as a CI job does. Without it those tests are skipped.

New to the code? [docs/architecture.md](docs/architecture.md) shows where
things are. Much can be added as a [plugin](docs/plugins.md), without
changing wintergrab.

`docs/api.md` is written from the code: after changing a public name, a
signature or the first sentence of a docstring, run `python
scripts/api_reference.py` (a test checks that it is up to date).

A bug fix should come with a test that fails without it. The hot-path
shortcuts in `utils.py`, `request.py` and the parser must return exactly
what the code they bypass returns; `tests/test_fast_paths.py` checks that.
For performance work, see [benchmarks/README.md](benchmarks/README.md).

## Releasing

### One-time setup (repository owner)

1. On [pypi.org](https://pypi.org), sign in and go to **Your account →
   Publishing → Add a new pending publisher**. Enter:
   - PyPI project name: `wintergrab`
   - Owner: `opensourcewinter`
   - Repository name: `wintergrab`
   - Workflow name: `release.yml`
   - Environment name: `pypi`
2. On GitHub, go to **Settings → Environments → New environment** and create
   `pypi`. Adding yourself as a required reviewer means every release waits
   for your approval.

With trusted publishing, PyPI accepts uploads from that workflow only. No
API token is stored anywhere.

### Every release

1. Set `__version__` in `src/wintergrab/__init__.py` and add a matching
   `## X.Y.Z` section at the top of `CHANGELOG.md`.
2. Merge to `main` and wait for CI to pass.
3. Run the live tests from a normal network: `WINTERGRAB_LIVE=1 pytest -m live`.
4. Tag and push:

   ```bash
   git tag vX.Y.Z
   git push origin vX.Y.Z
   ```

The [release workflow](.github/workflows/release.yml) checks that the tag,
the package version and the changelog agree. It then builds, publishes to
PyPI and creates the GitHub release with generated notes.
