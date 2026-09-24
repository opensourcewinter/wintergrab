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

These inputs are trusted, so only use ones you control:
- **Spider files** run by `wintergrab crawl` are Python code.
- **Crawl state** (`crawl_dir`) is stored as pickle files. Loading someone
  else's crawl state can run their code.
- **Schema files** (`--schema`) and proxy lists are your own configuration.

## Crawling from sensitive networks

A redirect that leaves `allowed_domains` is dropped *after* the request was
made. That request can reach anything your machine can reach. When you crawl
sites you don't trust from a machine that can reach internal services (a
cloud instance's metadata endpoint, an intranet), restrict its outbound
network access.
