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
