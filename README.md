# crawlertests

Comparative tests for web archiving crawlers. A local fixture server serves a
set of small test sites ("cases"). Each crawler crawls all of them, and the
harness reads the crawler's WARC files to see which URLs it captured. The
captures are checked against each case's expectations, and the crawlers are
compared side by side.

Crawlers supported so far:

| name               | what runs                                                   |
|--------------------|-------------------------------------------------------------|
| `heritrix`         | Heritrix with its standard extractors (FetchHTTP)           |
| `heritrix-strict-js` | Heritrix with ConfigurableExtractorJS in strict mode (only absolute URLs from JavaScript) |
| `heritrix-browser` | Heritrix with FetchHTTP2 and the BrowserProcessor (Firefox or Chrome over WebDriver BiDi) |
| `browsertrix`      | Browsertrix Crawler in a podman or docker container         |

## Quick start

Requires Python 3.11+ and [uv](https://docs.astral.sh/uv/).

```sh
cp crawlertests.example.toml crawlertests.toml # set the Heritrix home, etc.
uv run crawlertests list                       # list cases
uv run crawlertests run                        # run every crawler on every case
uv run crawlertests run -c heritrix js-        # one crawler, cases matching "js-"
uv run crawlertests run tag:srcset             # cases with a tag
uv run crawlertests compare runs/20261103-201500   # re-score a run after editing expectations
uv run crawlertests serve                      # browse the fixtures at http://127.0.0.1:8000/
                                               # (and https://127.0.0.1:8443/)
```

Each run writes `runs/<timestamp>/` containing:

- `<crawler>/` — the crawler's working directory (job config, logs, WARCs)
- `requests.jsonl` — every request the fixture server saw, tagged by crawler
- `results.json` — per-case, per-crawler results
- `report.html` — a summary matrix and per-URL detail for every case

## Results over time

Each run also writes a compact record per crawler to `runs/<run>/records/`:
the URLs it captured (with method and status), the URLs it requested, and
the crawler and test suite versions. Records don't contain pass/fail results,
so any record can be re-scored against the current cases.

```sh
uv run crawlertests save runs/20261103-201500 results/   # add a run's records to a history
uv run crawlertests site results/ -o _site                # build the website from a history
```

`save` skips a crawler whose version and suite version match a saved run.
The website shows the latest result for each crawler, the score over time,
each case's history, and a comparison of any two runs. Saved runs are
re-scored against the current expectations, but a run only has captures for
the cases that existed when it was crawled, and its captures reflect the
fixtures as they were then. So the chart scores every point on the cases all
the plotted runs share, and when a crawler version is re-crawled its new run
replaces the old one.

```sh
uv run crawlertests stale results/   # saved releases that predate the current cases or harness
```

On GitHub, the `crawl` workflow runs nightly: the latest releases of Heritrix
and Browsertrix Crawler ("release" track) and Heritrix built from its default
branch ("dev" track). It saves new records to the `results` branch and
rebuilds the site on GitHub Pages; the `site` workflow also rebuilds it when
the cases change. The nightly run also re-crawls any saved release that
`stale` lists, so after the cases change every version on the chart catches up
within a night. Run `crawl` by hand with `heritrix_release` or
`browsertrix_tag` to backfill older versions, or with `recrawl` to re-crawl the
stale versions now.

## Writing a case

A case is a directory under `cases/` containing `case.toml` and the files to
serve. The seed is the directory URL `/cases/<name>/`, which serves
`index.html`.

```toml
title = "Responsive images (srcset)"
description = "Why this matters for archiving."
tags = ["html", "images"]
expect = ["x1.png", "x2.png"]     # URLs that should be captured
expect_not = ["not-a-link.html"]  # URLs that should not be
```

Expectation URLs are relative to the case directory (`img.png`), to the origin
(`/cases/other/img.png`), or on another origin with a marker:
`{OTHER}/cases/name/img.png` for the second HTTP origin, `{HTTPS}` and
`{HTTPS_OTHER}` for the two HTTPS origins, and `{HTTP}` for the primary HTTP
origin from an HTTPS case. They're compared after normalizing
percent-encoding, ignoring the hostname (crawlers in containers reach the
server under a different name).

An expectation can also require the request method or the response status:

```toml
expect = [
  { url = "api/save", method = "POST" },   # archived as a POST (from the WARC request record)
  { url = "sound.wav", status = 200 },     # the whole file, not a 206 partial response
]
```

Set `https = true` to seed a case on the primary HTTPS origin instead. The
server uses a self-signed certificate made for each run; Heritrix gets it in
a JVM trust store (FetchHTTP2 checks certificates), and the browsers are told
to accept it.

Only pages that contain links need real files. Any other URL inside a case
directory returns a small placeholder of the type its extension suggests
(an HTML page without links, a 1×1 PNG, empty CSS or JS, …). This keeps cases
short, and it means a crawler that requests a URL that isn't a real link
captures it, so `expect_not` can test precision.

Text files are templated: `{{ORIGIN}}`, `{{HOST}}`, `{{OTHER_ORIGIN}}`,
`{{OTHER_HOST}}`, `{{HTTPS_ORIGIN}}`, `{{HTTPS_HOST}}`,
`{{HTTPS_OTHER_ORIGIN}}` and `{{HTTPS_OTHER_HOST}}` are replaced using the
hostname from the request.

A case can also serve files outside its directory, and add lines to the
shared `/robots.txt` (which starts with `User-agent: *`):

```toml
root = { "/favicon.ico" = "favicon.ico" }   # served on the primary origin
robots = """
Disallow: /cases/robots-txt/disallowed/
Sitemap: {{ORIGIN}}/cases/sitemaps/sitemap-index.xml
"""
```

Expectations can refer to these root paths (`"/robots.txt"`).

Custom responses (redirects, headers, compression) are declared as routes:

```toml
[[routes]]
path = "r301"            # relative to the case directory; "" is the index
status = 301
headers = { Location = "target.html" }

[[routes]]
path = ""
file = "index.html"      # serve a file with extra headers
headers = { Link = "<style.css>; rel=stylesheet" }
delay = 2.0              # seconds before responding (avoid: keep tests fast)

[[routes]]
path = "page.html"
file = "page.html"
encoding = "gzip"        # or "deflate", "br", "zstd"; applied if Accept-Encoding allows it
negotiate = false        # ...or always, like a server sending pre-compressed files
content_type = ""        # send no Content-Type header at all
gzip_body = true         # gzip the body itself, as for a .gz file

[[routes]]
path = "sound.wav"
file = "sound.wav"
ranges = true            # answer Range requests with 206 Partial Content

[[routes]]
path = "members.html"
file = "members.html"
require_cookie = "member=yes"                    # without this cookie...
without_cookie = { file = "login-wall.html" }    # ...serve this instead (default: a 403)
```

Responses can set cookies with a `Set-Cookie` entry in `headers`.

Only test behaviour a browser would accept. A stylesheet served as
`text/plain`, for example, is ignored by browsers in standards mode, so
there's nothing for an archive to get right. Likewise br and zstd are only
used on HTTPS cases, since browsers only accept them there.

## Reading the results

A case passes for a crawler when every `expect` URL is captured and no
`expect_not` URL is. The report also lists, per case and crawler:

- **other captures** — URLs inside the case directory that the case doesn't
  mention. These are often harmless, but are worth checking when writing a
  case, and they reveal speculative extraction.
- **requested but not archived** — URLs the fixture server served to the
  crawler that don't appear in its WARCs. For an archiving crawler this is
  usually a bug: the crawler saw the resource but the archive won't replay it.

Some expectations describe ideal archival behaviour rather than what any
crawler does today (all srcset candidates, click handlers). Cases are tagged so
they can be run or discussed in groups.

## Crawler notes

**Heritrix.** The job configuration is generated from the
`profile-crawler-beans.cxml` inside the installed engine jar, so the harness
tests whatever version `home` points to. Politeness delays are set to zero; robots.txt
is obeyed, as in the stock profile. Extra extractors can be added to the fetch
chain with `extractors` (the stock profile has no `ExtractorPDF`). The generated config is kept as
`runs/<run>/<crawler>/jobs/crawlertests/crawler-beans.cxml`. Build a
distribution from a checkout with `mvn -DskipTests package` and unpack
`dist/target/heritrix-*-dist.tar.gz`.

**Heritrix with strict JavaScript extraction.** The `extractorJs` bean
becomes `ConfigurableExtractorJS` with `strict = true`. ExtractorHTML
autowires that bean, so inline scripts and event handlers are extracted the
same way as script files. Any crawler of type `heritrix` can set
`extractor_js` to a table of bean properties (plus an optional `class`).

**Heritrix + BrowserProcessor.** As above, but `fetchHttp` becomes
`FetchHTTP2` and the browser bean is enabled in the fetch chain after the
extractors. The harness passes the fixture certificate to the JVM with
`-Djavax.net.ssl.trustStore`, since FetchHTTP2 (unlike FetchHTTP) verifies
certificates.

**Browsertrix.** Runs `browsertrix-crawler crawl --scopeType prefix
--useRobots --useSitemap` with all case seeds (set `use_robots` or
`use_sitemap` to false to drop those flags). Browsertrix only runs sitemap
discovery for the first seed, within that seed's scope, so cases tagged
`sitemaps` are seeded first. The container reaches the fixture server through
`host.containers.internal` (podman) or `host.docker.internal` (docker).
