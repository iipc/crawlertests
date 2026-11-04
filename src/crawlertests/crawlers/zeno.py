"""Zeno, the Internet Archive's Go crawler.

Runs "Zeno get url" with every seed. Zeno has no scope rules as such: it
crawls outlinks up to --max-hops, and --domains-crawl resets the hop count to
0 for outlinks that match. So the harness sets --max-hops 0 and gives one
--domains-crawl regex per seed prefix, which makes the scope prefix-based
while assets (Zeno's term for page requisites) are fetched from anywhere.
Zeno stops by itself once its queue is empty.

Zeno doesn't fetch or obey robots.txt, and it doesn't validate certificates
unless asked to with --cert-validation. It refuses to crawl localhost and
127.0.0.1, so seeds use the hostname "localhost." instead.
"""

import re
import shutil
import subprocess
from pathlib import Path

from . import Crawler

JOB_NAME = "crawlertests"


class Zeno(Crawler):
    def __init__(self, name: str, config: dict):
        super().__init__(name, config)
        binary = config.get("binary")
        self.binary = (str(Path(binary).expanduser().resolve()) if binary
                       else shutil.which("Zeno") or shutil.which("zeno"))

    def seed_host(self) -> str:
        # Zeno refuses to crawl localhost, 127.0.0.1 or any hostname without a dot; the root-qualified name works
        return self.config.get("seed_host", "localhost.")

    def unavailable(self) -> str | None:
        if self.binary is None:
            return ("set 'binary' to a Zeno executable (a release from github.com/internetarchive/Zeno, or "
                    "go install github.com/internetarchive/Zeno/v2@latest)")
        if not Path(self.binary).exists():
            return f"{self.binary} not found"
        return None

    def command(self, seeds: list[str]) -> list[str]:
        cmd = [self.binary, "get", "url",
               "--job", JOB_NAME,
               "--workers", str(self.config.get("workers", 4)),
               "--max-concurrent-assets", str(self.config.get("max_concurrent_assets", 4)),
               "--max-hops", "0",
               "--disable-rate-limit",
               "--disable-ipv6",  # the fixture server listens on IPv4
               "--min-space-required", "1",
               "--no-log-file", "--no-color-logs",
               "--warc-operator", "IIPC crawlertests run"]
        for seed in seeds:
            cmd += ["--domains-crawl", "^" + re.escape(seed)]
        if self.config.get("headless"):
            cmd.append("--headless")
            if chromium := self.config.get("chromium"):
                cmd += ["--headless-chromium-bin", chromium]
        cmd += self.config.get("extra_args", [])
        return cmd + seeds

    def crawl(self, seeds: list[str], workdir: Path) -> None:
        self.run_logged(self.command(seeds), workdir, "zeno.log")

    def version(self, workdir: Path) -> dict:
        info = {"product": "Zeno"}
        # Zeno's own version is a git hash; the module version is in the Go build info
        try:
            out = subprocess.run(["go", "version", "-m", self.binary], capture_output=True, text=True, timeout=30)
        except (OSError, subprocess.TimeoutExpired):
            return info
        for line in out.stdout.splitlines():
            parts = line.split()
            if parts[:2] == ["mod", "github.com/internetarchive/Zeno/v2"] and len(parts) > 2:
                if parts[2] != "(devel)":
                    info["version"] = parts[2].removeprefix("v")
            elif len(parts) == 2 and parts[0] == "build" and "=" in parts[1]:
                key, value = parts[1].split("=", 1)
                if key == "vcs.revision":
                    info["revision"] = value
                elif key == "vcs.time":
                    info["date"] = value
        return info
