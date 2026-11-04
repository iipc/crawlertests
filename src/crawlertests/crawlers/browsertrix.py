"""Browsertrix Crawler, run in a container with podman or docker."""

import json
import re
import shutil
import subprocess
from pathlib import Path

from . import Crawler

DEFAULT_IMAGE = "docker.io/webrecorder/browsertrix-crawler:latest"
CONTAINER_HOSTS = {"podman": "host.containers.internal", "docker": "host.docker.internal"}


class Browsertrix(Crawler):
    def __init__(self, name: str, config: dict):
        super().__init__(name, config)
        self.engine = config.get("engine") or next((e for e in ("podman", "docker") if shutil.which(e)), None)

    def seed_host(self) -> str:
        return self.config.get("container_host") or CONTAINER_HOSTS.get(self.engine, "host.docker.internal")

    def unavailable(self) -> str | None:
        if self.engine is None or not shutil.which(self.engine):
            return "neither podman nor docker found (set 'engine')"
        return None

    def version(self, workdir: Path) -> dict:
        info = {"product": "Browsertrix Crawler"}
        log = workdir / "browsertrix.log"
        if log.exists() and (m := re.search(r'"Browsertrix-Crawler (\S+)', log.read_text(errors="replace"))):
            info["version"] = m.group(1)
        image = self.config.get("image", DEFAULT_IMAGE)
        out = subprocess.run([self.engine, "image", "inspect", image, "--format",
                              '{{json .Config.Labels}}\t{{json .RepoDigests}}\t{{json .Created}}'],
                             capture_output=True, text=True)
        if out.returncode == 0:
            labels, digests, created = (json.loads(part) for part in out.stdout.strip().split("\t", 2))
            labels = labels or {}
            info["version"] = info.get("version") or labels.get("org.opencontainers.image.version")
            info["revision"] = labels.get("org.opencontainers.image.revision")
            # images before about 1.14 have no created label; their build time is within minutes of the release
            info["date"] = labels.get("org.opencontainers.image.created") or created
            info["image"] = (digests or [image])[0]
        return {k: v for k, v in info.items() if v}

    def crawl(self, seeds: list[str], workdir: Path) -> None:
        (workdir / "seeds.txt").write_text("\n".join(seeds) + "\n")
        cmd = [self.engine, "run", "--rm", "-v", f"{workdir.resolve()}:/crawls"]
        if self.engine == "docker":
            cmd.append("--add-host=host.docker.internal:host-gateway")
        cmd += [self.config.get("image", DEFAULT_IMAGE), "crawl",
                "--seedFile", "/crawls/seeds.txt",
                "--scopeType", "prefix",
                "--collection", "crawlertests",
                "--workers", str(self.config.get("workers", 4)),
                *(["--useRobots"] if self.config.get("use_robots", True) else []),
                *(["--useSitemap"] if self.config.get("use_sitemap", True) else []),
                *self.config.get("extra_args", [])]
        self.run_logged(cmd, workdir, "browsertrix.log")
