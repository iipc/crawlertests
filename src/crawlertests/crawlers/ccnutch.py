"""Common Crawl's fork of Apache Nutch, set up as in cc-nutch-example.

https://github.com/commoncrawl/cc-nutch-example is Common Crawl's description
of how to crawl to WARC files with its fork (https://github.com/commoncrawl/nutch).
The fork has no releases, so the harness builds it from source in a container
image (ccnutch/Dockerfile, which follows the example's build steps), and runs
the example's crawl.sh (ccnutch/crawl.sh, a copy with a few marked changes) in
Hadoop local mode: inject, then rounds of generate, fetch with parsing and WARC
writing, and updatedb.

The configuration is the example's conf/nutch-site.xml, taken from the image,
with these changes: an agent name (the example leaves it for the user), no
politeness delay, and a prefix URL filter listing the seeds in place of the
default one that accepts any http(s) URL. Everything else is as configured
there and in Nutch's defaults, including the URL filters that Common Crawl's
text-oriented crawl relies on: suffix and regex filters that skip images, CSS,
JavaScript and other non-text files, and URLs with a query string. Nutch
doesn't tell page requisites from links, so the scope applies to both.
robots.txt is obeyed, and protocol-okhttp doesn't check certificates
(http.tls.certificates.check is false by default).
"""

import re
import shutil
import subprocess
import xml.etree.ElementTree as ET
from pathlib import Path

from . import CrawlError, Crawler
from .browsertrix import CONTAINER_HOSTS

PROJECT = Path(__file__).parent / "ccnutch"
DEFAULT_IMAGE = "localhost/crawlertests-ccnutch:latest"
EXAMPLE_DIR = "/opt/cc-nutch-example"

# build arg: (repository, default ref, config key)
SOURCES = {
    "NUTCH_REF": ("https://github.com/commoncrawl/nutch.git", "cc", "nutch_ref"),
    "EXAMPLE_REF": ("https://github.com/commoncrawl/cc-nutch-example.git", "HEAD", "example_ref"),
    "CRAWLER_COMMONS_REF": ("https://github.com/crawler-commons/crawler-commons.git", "HEAD",
                            "crawler_commons_ref"),
    "CLD2_REF": ("https://github.com/commoncrawl/language-detection-cld2.git", "HEAD", "cld2_ref"),
}

# changes to the example's nutch-site.xml
SITE_OVERRIDES = {
    "http.agent.name": "crawlertests",
    "http.agent.description": "IIPC crawlertests run",
    "http.agent.url": "https://github.com/iipc/crawlertests",
    "fetcher.server.delay": "0.0",
}


class CCNutch(Crawler):
    def __init__(self, name: str, config: dict):
        super().__init__(name, config)
        self.engine = config.get("engine") or next((e for e in ("podman", "docker") if shutil.which(e)), None)
        self.image = config.get("image", DEFAULT_IMAGE)

    def seed_host(self) -> str:
        return self.config.get("container_host") or CONTAINER_HOSTS.get(self.engine, "host.docker.internal")

    def unavailable(self) -> str | None:
        if self.engine is None or not shutil.which(self.engine):
            return "neither podman nor docker found (set 'engine')"
        return None

    def build_args(self) -> dict[str, str]:
        """The build's source refs, with branches resolved to commits so the layer cache follows them."""
        args = {}
        for arg, (repo, default, key) in SOURCES.items():
            ref = str(self.config.get(key, default))
            if not re.fullmatch(r"[0-9a-f]{40}", ref):
                out = subprocess.run(["git", "ls-remote", repo, ref], capture_output=True, text=True, timeout=60)
                if out.returncode != 0 or not out.stdout.strip():
                    raise CrawlError(f"can't resolve {ref} in {repo}: {out.stderr.strip()}")
                ref = out.stdout.split()[0]
            args[arg] = ref
        return args

    def build(self, workdir: Path):
        cmd = [self.engine, "build", "-t", self.image]
        for arg, value in self.build_args().items():
            cmd += ["--build-arg", f"{arg}={value}"]
        self.run_logged(cmd + [str(PROJECT)], workdir, "build.log")

    def site_config(self) -> str:
        """The example's nutch-site.xml with the harness's changes."""
        out = subprocess.run([self.engine, "run", "--rm", "--entrypoint", "cat", self.image,
                              f"{EXAMPLE_DIR}/conf/nutch-site.xml"], capture_output=True, text=True)
        if out.returncode != 0:
            raise CrawlError(f"can't read nutch-site.xml from {self.image}: {out.stderr.strip()}")
        root = ET.fromstring(out.stdout)
        overrides = SITE_OVERRIDES | {k: str(v) for k, v in self.config.get("conf", {}).items()}
        for prop in root.findall("property"):
            if (name := prop.findtext("name")) in overrides:
                prop.find("value").text = overrides[name]
        present = {prop.findtext("name") for prop in root.findall("property")}
        for name, value in overrides.items():
            if name not in present:
                prop = ET.SubElement(root, "property")
                ET.SubElement(prop, "name").text = name
                ET.SubElement(prop, "value").text = value
        return '<?xml version="1.0"?>\n' + ET.tostring(root, encoding="unicode") + "\n"

    def crawl(self, seeds: list[str], workdir: Path) -> None:
        if self.config.get("build", True):
            self.build(workdir)
        conf = workdir / "conf"
        conf.mkdir(exist_ok=True)
        (conf / "nutch-site.xml").write_text(self.site_config())
        # the scope: in place of the default prefix filter, which accepts any http, https, ftp or file URL
        (conf / "prefix-urlfilter.txt").write_text("\n".join(seeds) + "\n")
        (workdir / "seeds").mkdir(exist_ok=True)
        (workdir / "seeds" / "urls.txt").write_text("".join(f"{seed}\tnutch.score=1.0\n" for seed in seeds))
        shutil.copy(PROJECT / "crawl.sh", workdir / "crawl.sh")

        cmd = [self.engine, "run", "--rm", "-v", f"{workdir.resolve()}:/crawls", "-e", "CONF_DIR=/crawls/conf"]
        if self.engine == "docker":
            cmd.append("--add-host=host.docker.internal:host-gateway")
        cmd += [self.image, "bash", "/crawls/crawl.sh", "/crawls/crawl", str(self.config.get("rounds", 5)),
                "/crawls/seeds"]
        self.run_logged(cmd, workdir, "ccnutch.log")

    def version(self, workdir: Path) -> dict:
        info = {"product": "Apache Nutch (Common Crawl fork)"}
        log = workdir / "ccnutch.log"
        if log.exists():
            text = log.read_text(errors="replace")
            if m := re.search(r"^nutch-version (\S+)", text, re.M):
                info["version"] = m.group(1)
            if m := re.search(r"^nutch ([0-9a-f]{40}) (\S+)", text, re.M):
                info["revision"], info["date"] = m.groups()
        return info
