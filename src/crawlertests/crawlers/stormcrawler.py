"""Apache StormCrawler, as a local-mode Storm topology writing WARCs.

StormCrawler is a library for building crawl topologies rather than a
crawler you download and run, so the harness has a small Maven project
(stormcrawler/) that builds a topology against the released StormCrawler
artifacts of the configured version. The topology is the one the
StormCrawler archetype generates (fetcher, sitemap, feed and jsoup parsers,
Tika for other documents) with the WARC module's WARCHdfsBolt on the
fetcher's output. In place of URLFrontier it has an in-memory queue that
fetches each URL once and stops the topology when nothing is queued or in
flight.

The configuration is the archetype's, with these changes for archiving:
scope is a prefix filter per seed instead of the archetype's URL regex filter
(which skips images, CSS and JS), there's no SitemapFilter (which restricts
a host with sitemaps to the URLs they list), there is no content limit, nothing is
refetched, politeness delays are zero, and a LinkParseFilter extracts
embedded resources as well as frames (the jsoup parser itself only follows
a[href]). StormCrawler doesn't tell embeds from links, so it can't fetch
requisites outside the scope. robots.txt is obeyed, and okhttp trusts any
certificate by default.
"""

import json
import re
import shutil
import urllib.request
import zipfile
from pathlib import Path

from . import CrawlError, Crawler

PROJECT = Path(__file__).parent / "stormcrawler"
DEFAULT_VERSION = "3.7.0"
DEFAULT_STORM_VERSION = "2.8.9"
MAVEN_CENTRAL = "https://repo1.maven.org/maven2"

# what a browser would load as part of a page; LinkParseFilter turns each into an outlink
EMBED_XPATHS = [
    "//IMG/@src", "//SCRIPT/@src", "//LINK/@href", "//IFRAME/@src", "//SOURCE/@src", "//VIDEO/@src",
    "//VIDEO/@poster", "//AUDIO/@src", "//TRACK/@src", "//EMBED/@src", "//OBJECT/@data", "//INPUT/@src",
]

URL_FILTERS = [
    {"class": "org.apache.stormcrawler.filtering.basic.BasicURLFilter", "name": "BasicURLFilter",
     "params": {"maxPathRepetition": 3, "maxLength": 1024}},
    {"class": "org.apache.stormcrawler.filtering.basic.BasicURLNormalizer", "name": "BasicURLNormalizer",
     "params": {"removeAnchorPart": True, "unmangleQueryString": True, "checkValidURI": True,
                "removeHashes": True, "hostIDNtoASCII": True}},
    {"class": "org.apache.stormcrawler.filtering.regex.RegexURLFilter", "name": "RegexURLFilter",
     "params": {"regexFilterFile": "crawlertests-scope.txt"}},
    {"class": "org.apache.stormcrawler.filtering.basic.SelfURLFilter", "name": "SelfURLFilter"},
    # not the archetype's SitemapFilter, which drops every link not found in a sitemap once a host's robots.txt
    # lists one: the cases share a robots.txt, so the sitemaps case would stop link discovery in all the others
]

PARSE_FILTERS = [
    {"class": "org.apache.stormcrawler.parse.filter.MimeTypeNormalization", "name": "MimeTypeNormalization"},
]


class StormCrawler(Crawler):
    def __init__(self, name: str, config: dict):
        super().__init__(name, config)
        self.sc_version = str(config.get("stormcrawler_version", DEFAULT_VERSION))
        self.mvn = config.get("mvn") or shutil.which("mvn")
        self.java = config.get("java") or _java()

    def unavailable(self) -> str | None:
        if not self.mvn:
            return "mvn not found (set 'mvn'); it builds the topology against the StormCrawler release"
        if not self.java:
            return "java not found (set 'java' or JAVA_HOME); StormCrawler needs Java 17 or later"
        return None

    def storm_version(self) -> str:
        """The Storm version the StormCrawler release was built with, from its parent POM."""
        if v := self.config.get("storm_version"):
            return str(v)
        url = f"{MAVEN_CENTRAL}/org/apache/stormcrawler/stormcrawler/{self.sc_version}/stormcrawler-{self.sc_version}.pom"
        try:
            with urllib.request.urlopen(url, timeout=30) as response:
                pom = response.read().decode()
        except OSError:
            return DEFAULT_STORM_VERSION
        m = re.search(r"<storm-client\.version>([^<]+)<", pom) or re.search(r"<storm\.version>([^<]+)<", pom)
        return m.group(1) if m else DEFAULT_STORM_VERSION

    def build(self, workdir: Path) -> str:
        """Build the topology; returns its classpath."""
        build_dir = workdir / "build"
        self.run_logged([self.mvn, "-B", "-q", "-f", str(PROJECT / "pom.xml"),
                         f"-Dstormcrawler.version={self.sc_version}", f"-Dstorm.version={self.storm_version()}",
                         f"-Dbuild.dir={build_dir}", "package", "dependency:build-classpath"],
                        workdir, "build.log")
        return f"{build_dir / 'classes'}:{(build_dir / 'classpath.txt').read_text().strip()}"

    def write_config(self, seeds: list[str], conf_dir: Path):
        conf_dir.mkdir(parents=True, exist_ok=True)
        scope = ["-^(file|ftp|mailto):"] + [f"+^\\Q{seed}\\E" for seed in seeds] + ["-."]
        (conf_dir / "crawlertests-scope.txt").write_text("\n".join(scope) + "\n")
        (conf_dir / "crawlertests-urlfilters.json").write_text(
            json.dumps({"org.apache.stormcrawler.filtering.URLFilters": URL_FILTERS}, indent=2))
        (conf_dir / "crawlertests-parsefilters.json").write_text(
            json.dumps({"org.apache.stormcrawler.parse.ParseFilters": PARSE_FILTERS}, indent=2))
        links = {"frames": "//FRAME/@src"}
        if self.config.get("embeds", True):
            links["embeds"] = EMBED_XPATHS
        jsoup_filters = [{"class": "org.apache.stormcrawler.jsoup.LinkParseFilter", "name": "LinkParseFilter",
                          "params": links}]
        (conf_dir / "crawlertests-jsoupfilters.json").write_text(
            json.dumps({"org.apache.stormcrawler.parse.JSoupFilters": jsoup_filters}, indent=2))

        conf = {
            "topology.workers": 1,
            "topology.message.timeout.secs": 300,
            "topology.max.spout.pending": 100,
            "topology.kryo.register": ["org.apache.stormcrawler.Metadata", "org.apache.stormcrawler.persistence.Status"],
            "crawlertests.software": f"Apache StormCrawler {self.sc_version}",
            "http.agent.name": "crawlertests",
            "http.agent.version": "1",
            "http.agent.description": "IIPC crawlertests run",
            "http.agent.url": "http://localhost/crawlertests",
            "http.agent.email": "",
            "http.protocol.implementation": "org.apache.stormcrawler.protocol.okhttp.HttpProtocol",
            "https.protocol.implementation": "org.apache.stormcrawler.protocol.okhttp.HttpProtocol",
            "http.store.headers": True,
            "http.content.limit": -1,
            "fetcher.threads.number": self.config.get("threads", 8),
            "fetcher.server.delay": 0.0,
            "fetcher.server.min.delay": 0.0,
            "fetcher.max.crawl.delay": 0,
            "fetchInterval.default": -1,
            "fetchInterval.fetch.error": -1,
            "fetchInterval.error": -1,
            "sitemap.discovery": True,
            "urlfilters.config.file": "crawlertests-urlfilters.json",
            "parsefilters.config.file": "crawlertests-parsefilters.json",
            "jsoup.filters.config.file": "crawlertests-jsoupfilters.json",
            "jsoup.treat.non.html.as.error": False,
            "parser.mimetype.whitelist": ["application/.+word.*", "application/.+excel.*",
                                          "application/.+powerpoint.*", "application/.*pdf.*"],
        } | self.config.get("conf", {})
        (conf_dir / "crawlertests-conf.json").write_text(json.dumps({"config": conf}, indent=2))

    def crawl(self, seeds: list[str], workdir: Path) -> None:
        classpath = self.build(workdir)
        conf_dir = workdir / "conf"
        self.write_config(seeds, conf_dir)
        (workdir / "seeds.txt").write_text("\n".join(seeds) + "\n")
        (workdir / "warcs").mkdir(exist_ok=True)
        cmd = [self.java, *self.config.get("java_opts", "-Xmx2g").split(), "-Djava.net.preferIPv4Stack=true",
               "-cp", f"{conf_dir}:{classpath}", "org.netpreserve.crawlertests.CrawlTopology",
               str(conf_dir / "crawlertests-conf.json"), str(workdir / "seeds.txt"), str(workdir / "warcs")]
        self.run_logged(cmd, workdir, "stormcrawler.log")

    def version(self, workdir: Path) -> dict:
        info = {"product": "Apache StormCrawler", "version": self.sc_version}
        jar = (Path.home() / ".m2/repository/org/apache/stormcrawler/stormcrawler-core" / self.sc_version
               / f"stormcrawler-core-{self.sc_version}.jar")
        if jar.exists():
            with zipfile.ZipFile(jar) as z:
                for entry in z.infolist():
                    if entry.filename.endswith("/pom.properties"):
                        info["date"] = "%04d-%02d-%02d" % entry.date_time[:3]  # when the release was built
        return info


def _java() -> str | None:
    import os
    if java_home := os.environ.get("JAVA_HOME"):
        return str(Path(java_home) / "bin" / "java")
    return shutil.which("java")
