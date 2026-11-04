"""Crawler adapters.

An adapter runs one crawl over all the case seeds and leaves WARC files in its
work directory. Each crawler is configured by a [crawlers.<name>] table in
crawlertests.toml; see crawlertests.example.toml.
"""

import os
import signal
import subprocess
from pathlib import Path


class CrawlError(Exception):
    pass


class Crawler:
    name: str

    def __init__(self, name: str, config: dict):
        self.name = name
        self.config = config
        self.timeout = config.get("timeout", 1800)
        self.ca_cert: Path | None = None  # the fixture server's self-signed certificate, set before crawl()

    def seed_host(self) -> str:
        """Hostname the crawler uses to reach the fixture server."""
        return "127.0.0.1"

    def unavailable(self) -> str | None:
        """Return a reason this crawler can't run, or None if it can."""
        return None

    def crawl(self, seeds: list[str], workdir: Path) -> None:
        raise NotImplementedError

    def version(self, workdir: Path) -> dict:
        """What was run, detected after the crawl: {"version": ..., plus anything else useful}. The config's
        'version' table is merged on top (e.g. a revision and date for a build from source)."""
        return {}

    def run_logged(self, cmd: list[str], workdir: Path, log_name: str, env: dict | None = None):
        """Run a command with output to a log file, killing its process group on timeout."""
        log_path = workdir / log_name
        with open(log_path, "w") as log:
            log.write("$ " + " ".join(cmd) + "\n")
            log.flush()
            proc = subprocess.Popen(cmd, cwd=workdir, stdout=log, stderr=subprocess.STDOUT,
                                    env={**os.environ, **(env or {})}, start_new_session=True)
            try:
                code = proc.wait(timeout=self.timeout)
            except subprocess.TimeoutExpired:
                os.killpg(proc.pid, signal.SIGTERM)
                try:
                    proc.wait(timeout=30)
                except subprocess.TimeoutExpired:
                    os.killpg(proc.pid, signal.SIGKILL)
                raise CrawlError(f"timed out after {self.timeout}s (see {log_path})")
            except KeyboardInterrupt:
                os.killpg(proc.pid, signal.SIGTERM)
                raise
        if code != 0:
            raise CrawlError(f"exited with status {code} (see {log_path})")


def make_crawler(name: str, config: dict) -> Crawler:
    kind = config.get("type", name)
    if kind == "heritrix":
        from .heritrix import Heritrix
        return Heritrix(name, config, browser=False)
    if kind == "heritrix-browser":
        from .heritrix import Heritrix
        return Heritrix(name, config, browser=True)
    if kind == "heritrix-penumbra":
        from .penumbra import HeritrixPenumbra
        return HeritrixPenumbra(name, config)
    if kind == "browsertrix":
        from .browsertrix import Browsertrix
        return Browsertrix(name, config)
    if kind == "zeno":
        from .zeno import Zeno
        return Zeno(name, config)
    if kind == "stormcrawler":
        from .stormcrawler import StormCrawler
        return StormCrawler(name, config)
    if kind == "nutchcc":
        from .nutchcc import NutchCC
        return NutchCC(name, config)
    raise ValueError(f"unknown crawler type {kind!r} for {name}")
