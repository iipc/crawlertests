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
| `heritrix-penumbra` | Heritrix sending pages over AMQP to Penumbra, which renders them in Chromium |
| `browsertrix`      | Browsertrix Crawler in a podman or docker container         |
| `zeno`             | Zeno, the Internet Archive's Go crawler                     |
| `stormcrawler`     | Apache StormCrawler with its WARC module, as a local-mode Storm topology |
| `nutchcc`          | Common Crawl's fork of Apache Nutch, set up as in cc-nutch-example, in a container |

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

The website shows the latest result for each crawler, its accuracy over time,
each case's history, and a comparison of any two runs. Saved runs are
re-scored against the current expectations, but a run's captures only cover
the cases it crawled, as their fixtures were then. So each record also holds a
hash of each case's fixture files and the harness version
(`HARNESS_VERSION` in `history.py`). A crawler version's result takes each
case from its latest run with the current fixtures; a case whose fixtures
changed since doesn't count until that version is re-crawled for it, and the
chart scores every point on the cases all the plotted runs share. `save`
skips a run whose crawler version, harness version and case fixtures all match
a saved run.

Re-crawling an old version only needs its stale cases:

```sh
uv run crawlertests stale results/            # saved releases with new, changed or (after a harness bump) all cases
uv run crawlertests run -c heritrix name:html-anchors name:css-urls   # crawl exact cases
```

Bump `HARNESS_VERSION` only when a harness change alters what crawlers are
served or how captures are read: it makes every case of every saved version
stale.

On GitHub, the `crawl` workflow runs nightly: the latest releases of Heritrix,
Browsertrix Crawler, Zeno and StormCrawler ("release" track), Heritrix built
from its default branch ("dev" track), and the latest Heritrix release with
Penumbra from its default branch (`heritrix-penumbra`'s "dev" track, whose
revision and date are Penumbra's), and Common Crawl's Nutch fork built from its
default branch (`nutchcc`'s "dev" track; the image is kept in the Actions cache). It saves new records to the `results` branch and
rebuilds the site on GitHub Pages; the `site` workflow also rebuilds it when
the cases change. The nightly run also re-crawls the stale cases of up to 8
saved releases (newest first), so after the cases change older versions catch
up over a night or a few. Run `crawl` by hand with `heritrix_release`,
`browsertrix_tag`, `zeno_release` or `stormcrawler_release` to backfill older
versions, or with `recrawl` (and `budget = 0` for no limit) to re-crawl stale
cases now.

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

**Heritrix + Penumbra.** The `heritrix` configuration plus the AMQP beans
from [Penumbra](https://github.com/internetarchive/penumbra)'s sample job:
`AMQPPublishProcessor` at the end of the fetch chain sends HTML pages reached
by links to Penumbra, which loads each in headless Chromium and publishes
every URL the page requested; `AMQPUrlReceiver` schedules them. The harness
starts RabbitMQ in a container, runs Penumbra from git with `uv tool run`
(it has no releases; `penumbra_ref` picks the branch or commit), and adds
the `heritrix-contrib` jar (from Maven Central, or `contrib_jar`) and the
RabbitMQ client to an overlay of the Heritrix home. Penumbra's Chromium is
launched through a wrapper script that adds `--ignore-certificate-errors`.
Heritrix runs with `runWhileEmpty`, and the harness terminates the job once
Heritrix is empty and both queues have no ready or unacknowledged messages:
the contrib `AMQPUrlWaiter` instead stops at a statistics snapshot (every
20s) once anything has come back, even with pages still out.

**Zeno.** Runs `Zeno get url` with every seed, `--max-hops 0`, and a
`--domains-crawl` regex per seed prefix: outlinks that match are crawled at
hop 0, so the scope is the seed prefixes, while assets are fetched from any
origin. Zeno stops by itself when its queue is empty. It doesn't validate
certificates by default and doesn't fetch robots.txt at all. It refuses to
crawl `localhost`, `127.0.0.1` or any hostname without a dot, so its seeds
use `localhost.` (with the trailing dot). Release binaries are only built for
Linux and Windows; on macOS `go install github.com/internetarchive/Zeno/v2@latest`
(with Xcode's command line tools, setting `CGO_CXXFLAGS="-isysroot $(xcrun
--show-sdk-path) -I$(xcrun --show-sdk-path)/usr/include/c++/v1"` if the C++
headers aren't found).

**StormCrawler.** A library rather than a crawler you download, so the harness
has a small Maven project (`src/crawlertests/crawlers/stormcrawler/`) that
builds a topology against the released StormCrawler artifacts of
`stormcrawler_version`: the archetype's fetcher, sitemap, feed, jsoup and
Tika bolts, with `WARCHdfsBolt` on the fetcher's output, run in a Storm
`LocalCluster`. An in-memory queue replaces URLFrontier; it fetches each URL
once and ends the crawl when nothing is queued and every tuple is acked. The
archetype's configuration is changed for archiving: a prefix regex filter per
seed in place of its URL filter (which skips images, CSS and JS), no
`SitemapFilter` (which ignores links on a host whose robots.txt lists
sitemaps, and all the cases share a robots.txt), no content limit, no
refetching, zero politeness delays, and a `LinkParseFilter` for embedded
resources (`img`, `script`, `link`, `iframe`, `source`, … — set `embeds` to
false for just the archetype's frames), since the jsoup parser only follows
`a[href]`. StormCrawler doesn't distinguish embeds from links, so nothing
outside the seed prefixes is fetched. robots.txt is obeyed, and okhttp trusts
any certificate. Only 2xx responses reach the WARC bolt, so redirects and
errors aren't archived; nor is robots.txt.

**Common Crawl Nutch.** Common Crawl's fork of Nutch, as described by
[cc-nutch-example](https://github.com/commoncrawl/cc-nutch-example). The fork
has no releases, so the harness builds an image (`src/crawlertests/crawlers/nutchcc/Dockerfile`)
following the example's steps: crawler-commons and the CLD2 language detector
from source, then `ant runtime` of the fork's `cc` branch. Branches are resolved
to commits before building, so the image is rebuilt only when one moves. The
crawl is the example's `crawl.sh` (a copy with marked changes) in Hadoop local
mode: inject, then `rounds` cycles of generate, fetch with parsing and WARC
writing, and updatedb, stopping early when there's nothing left to generate.
The configuration is the example's `nutch-site.xml`, read from the image, with
an agent name, no politeness delay, and the seeds as the prefix URL filter (the
default accepts any http(s) URL); the fetch lists are larger than the
example's 100 URLs per host, since every case is on the same host. Everything
else is Common Crawl's or Nutch's defaults, including the URL filters behind
Common Crawl's text-oriented crawl: the suffix and regex filters skip images,
CSS, JavaScript and other non-text files, and the regex filter skips any URL
with `?`, `=`, `*`, `!` or `@`. Nutch doesn't tell embeds from links, so
nothing outside the seed prefixes is fetched. robots.txt is obeyed (and
archived), and okhttp doesn't check certificates. Redirects and errors go to
separate "crawldiagnostics" WARCs, which the harness reads too.

**Browsertrix.** Runs `browsertrix-crawler crawl --scopeType prefix
--useRobots --useSitemap` with all case seeds (set `use_robots` or
`use_sitemap` to false to drop those flags). Browsertrix only runs sitemap
discovery for the first seed, within that seed's scope, so cases tagged
`sitemaps` are seeded first. The container reaches the fixture server through
`host.containers.internal` (podman) or `host.docker.internal` (docker).
