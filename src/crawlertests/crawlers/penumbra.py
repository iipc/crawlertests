"""Heritrix with Penumbra, the Internet Archive's Playwright-based Umbra.

Heritrix sends each HTML page reached by links to Penumbra over AMQP
(AMQPPublishProcessor, from the heritrix-contrib module); Penumbra loads it
in headless Chromium and publishes every URL the page requested, which
Heritrix's AMQPUrlReceiver adds to the frontier. The Heritrix side is the
stock profile, as for the plain heritrix crawler, plus the beans from
Penumbra's sample job.

The harness runs a RabbitMQ container, Penumbra from git (it has no
releases, and the "penumbra" on PyPI is something else) with uv, and
Heritrix with an overlay home whose lib/ adds the contrib jar and the
RabbitMQ client. Heritrix runs with runWhileEmpty, since its frontier is
empty while Penumbra renders the last pages. The contrib module's
AMQPUrlWaiter ends such a crawl at a statistics snapshot (every 20s) once
anything has come back from Penumbra, even if pages are still out. Instead
the harness ends the job once Heritrix is empty and both queues have no
ready or unacknowledged messages, for a few polls running.

Penumbra's Chromium doesn't accept the fixture server's self-signed
certificate, so Penumbra launches it through a script that adds
--ignore-certificate-errors.
"""

import json
import os
import re
import secrets
import shutil
import signal
import ssl
import subprocess
import time
import urllib.parse
import urllib.request
import zipfile
from pathlib import Path
from xml.etree import ElementTree

from . import CrawlError
from .heritrix import JOB_NAME, Heritrix, _free_port, _replace_once

PENUMBRA_REPO = "https://github.com/internetarchive/penumbra"
DEFAULT_RABBITMQ_IMAGE = "docker.io/library/rabbitmq:3"
MAVEN_CENTRAL = "https://repo1.maven.org/maven2"
AMQP_CLIENT_VERSION = "5.36.0"  # if the contrib jar doesn't say
EXCHANGE = "penumbra_exchange"
PAGE_QUEUE = "penumbra_urls"  # Heritrix -> Penumbra
CLIENT_ID = "crawlertests"  # Penumbra -> Heritrix, also the receiver's queue

# from Penumbra's sample job: HTML pages reached by links (and redirects), fetched successfully
SHOULD_PROCESS = ("curi.getContentType().contains('html') &amp;&amp; ( curi.via == null || "
                  "curi.getPathFromSeed()==~'^.*LR*$') &amp;&amp; curi.getHttpAuthChallenges() == null &amp;&amp; "
                  "curi.getFetchStatus() &gt;= 200 &amp;&amp; curi.getFetchStatus() &lt; 300")


class HeritrixPenumbra(Heritrix):
    def __init__(self, name: str, config: dict):
        # the frontier is empty while Penumbra renders the last pages; the harness ends the job instead
        overrides = {"crawlController.runWhileEmpty": "true"} | config.get("overrides", {})
        super().__init__(name, {**config, "overrides": overrides}, browser=False)
        self.engine = config.get("engine") or next((e for e in ("podman", "docker") if shutil.which(e)), None)
        self.uv = config.get("uv") or shutil.which("uv")
        self.penumbra_ref = config.get("penumbra_ref", "main")
        self.amqp_port = None
        self._revision = None

    def unavailable(self) -> str | None:
        if reason := super().unavailable():
            return reason
        if self.engine is None or not shutil.which(self.engine):
            return "neither podman nor docker found (set 'engine'); RabbitMQ runs in a container"
        if not self.uv:
            return "uv not found (set 'uv'); it runs Penumbra"
        if not self.config.get("contrib_jar") and self.heritrix_version().endswith("SNAPSHOT"):
            return (f"Heritrix {self.heritrix_version()} isn't released, so set 'contrib_jar' to its "
                    "heritrix-contrib jar (mvn -pl contrib -am package in a heritrix3 checkout)")
        return None

    def heritrix_version(self) -> str:
        return super().version(Path()).get("version", "")

    def penumbra_spec(self) -> str:
        return f"git+{PENUMBRA_REPO}@{self.penumbra_revision()}"

    def penumbra_revision(self) -> str:
        if self._revision is None:
            if re.fullmatch(r"[0-9a-f]{40}", self.penumbra_ref):
                self._revision = self.penumbra_ref
            else:
                out = subprocess.run(["git", "ls-remote", PENUMBRA_REPO, self.penumbra_ref],
                                     capture_output=True, text=True, timeout=60)
                if out.returncode != 0 or not out.stdout.strip():
                    raise CrawlError(f"couldn't resolve penumbra ref {self.penumbra_ref!r}: {out.stderr.strip()}")
                self._revision = out.stdout.split()[0]
        return self._revision

    def build_config(self, seeds: list[str]) -> str:
        cxml = super().build_config(seeds)
        amqp_uri = f"amqp://guest:guest@127.0.0.1:{self.amqp_port}/%2f"
        beans = f"""
 <bean id="amqpPublishProcessor" class="org.archive.modules.AMQPPublishProcessor">
  <property name="amqpUri" value="{amqp_uri}"/>
  <property name="clientId" value="{CLIENT_ID}"/>
  <property name="exchange" value="{EXCHANGE}"/>
  <property name="routingKey" value="{PAGE_QUEUE}"/>
  <property name="shouldProcessRule">
   <bean class="org.archive.modules.deciderules.DecideRuleSequence">
    <property name="rules">
     <list>
      <bean class="org.archive.modules.deciderules.RejectDecideRule"/>
      <bean class="org.archive.modules.deciderules.ExpressionDecideRule">
       <property name="decision" value="ACCEPT"/>
       <property name="groovyExpression" value="{SHOULD_PROCESS}"/>
      </bean>
     </list>
    </property>
   </bean>
  </property>
 </bean>
 <bean id="amqpUrlReceiver" class="org.archive.crawler.frontier.AMQPUrlReceiver">
  <property name="amqpUri" value="{amqp_uri}"/>
  <property name="queueName" value="{CLIENT_ID}"/>
  <property name="exchange" value="{EXCHANGE}"/>
 </bean>
"""
        cxml = _replace_once(cxml, "</beans>", beans + "</beans>", "closing </beans>")
        # at the end of the fetch chain, where the profile has the (commented-out) browser
        cxml = _replace_once(cxml, '<!-- <ref bean="browser"/> -->', '<ref bean="amqpPublishProcessor"/>',
                             "browser ref in fetch chain")
        return cxml

    def version(self, workdir: Path) -> dict:
        """The Heritrix version, with Penumbra's commit as the revision and date: Penumbra is what changes
        from one dev-track run to the next. Heritrix's own revision and date, if known, can be given in the
        config's version table as heritrix_revision and heritrix_date."""
        info = super().version(workdir)
        info["product"] = "Heritrix + Penumbra"
        info["revision"] = self.penumbra_revision()
        api = f"https://api.github.com/repos/internetarchive/penumbra/commits/{info['revision']}"
        try:
            with urllib.request.urlopen(urllib.request.Request(
                    api, headers={"Accept": "application/vnd.github+json"}), timeout=30) as response:
                info["date"] = json.load(response)["commit"]["committer"]["date"]
        except (OSError, KeyError, ValueError):
            pass
        return info

    # --- running the pieces

    def crawl(self, seeds: list[str], workdir: Path) -> None:
        container = f"crawlertests-rabbitmq-{secrets.token_hex(4)}"
        procs = []
        try:
            home = self._overlay_home(workdir)
            self.amqp_port = _free_port()
            self._start_rabbitmq(container, workdir)
            procs.append(self._start_penumbra(workdir))
            self._wait_for_queue(container, PAGE_QUEUE, procs[0])
            procs.append(self._run_heritrix(seeds, workdir, home, container, procs[0]))
        finally:
            for proc in reversed(procs):
                _stop(proc)
            subprocess.run([self.engine, "rm", "-f", container], capture_output=True)

    def _overlay_home(self, workdir: Path) -> Path:
        """A Heritrix home whose lib/ is the real one plus the contrib jar and the RabbitMQ client."""
        home = workdir / "heritrix-home"
        if home.exists():
            shutil.rmtree(home)
        (home / "lib").mkdir(parents=True)
        for entry in self.home.iterdir():
            if entry.name != "lib":
                (home / entry.name).symlink_to(entry)
        for jar in (self.home / "lib").iterdir():
            (home / "lib" / jar.name).symlink_to(jar)
        if contrib := self.config.get("contrib_jar"):
            contrib = Path(contrib).expanduser()
            shutil.copy(contrib, home / "lib")
        else:
            version = self.heritrix_version()
            contrib = home / "lib" / f"heritrix-contrib-{version}.jar"
            _download(f"{MAVEN_CENTRAL}/org/archive/heritrix/heritrix-contrib/{version}/{contrib.name}", contrib)
        if not list((home / "lib").glob("amqp-client-*.jar")):
            v = self.config.get("amqp_client_version") or _amqp_client_version(contrib)
            _download(f"{MAVEN_CENTRAL}/com/rabbitmq/amqp-client/{v}/amqp-client-{v}.jar",
                      home / "lib" / f"amqp-client-{v}.jar")
        return home

    def _start_rabbitmq(self, container: str, workdir: Path):
        image = self.config.get("rabbitmq_image", DEFAULT_RABBITMQ_IMAGE)
        # as the rabbitmq user from the start: the entrypoint's own switch fails in rootless podman
        out = subprocess.run([self.engine, "run", "-d", "--rm", "--name", container, "--user", "rabbitmq",
                              "-p", f"127.0.0.1:{self.amqp_port}:5672", image], capture_output=True, text=True)
        if out.returncode != 0:
            raise CrawlError(f"couldn't start RabbitMQ: {out.stderr.strip()}")
        deadline = time.monotonic() + 120
        while time.monotonic() < deadline:
            ping = subprocess.run([self.engine, "exec", container, "rabbitmq-diagnostics", "-q",
                                   "check_port_listener", "5672"], capture_output=True, text=True)
            if ping.returncode == 0:
                return
            time.sleep(1)
        logs = subprocess.run([self.engine, "logs", container], capture_output=True, text=True)
        (workdir / "rabbitmq.log").write_text(logs.stdout + logs.stderr)
        raise CrawlError("RabbitMQ didn't start (see rabbitmq.log)")

    def _uv_run(self, *args: str) -> list[str]:
        return [self.uv, "tool", "run", "--python", "3.12", "--from", self.penumbra_spec(), *args]

    def _start_penumbra(self, workdir: Path) -> subprocess.Popen:
        log = open(workdir / "penumbra.log", "w")
        # Penumbra would install Playwright's Chromium itself, but the harness needs its path for the wrapper
        install = ["playwright", "install", "chromium"]
        if self.config.get("install_deps", False):
            install.insert(2, "--with-deps")
        subprocess.run(self._uv_run(*install), stdout=log, stderr=subprocess.STDOUT, check=True)
        out = subprocess.run(self._uv_run("python", "-c", "from playwright.sync_api import sync_playwright\n"
                                          "with sync_playwright() as p: print(p.chromium.executable_path)"),
                             capture_output=True, text=True, check=True)
        chromium = self.config.get("chromium") or out.stdout.strip().splitlines()[-1]
        wrapper = workdir / "chromium"
        wrapper.write_text(f'#!/bin/sh\nexec "{chromium}" --ignore-certificate-errors "$@"\n')
        wrapper.chmod(0o755)
        concurrency = self.config.get("concurrency", 4)
        env = {**os.environ,
               "PENUMBRA_AMQP_URL": f"amqp://guest:guest@127.0.0.1:{self.amqp_port}/%2f",
               "PENUMBRA_AMQP_QUEUE_NAME": PAGE_QUEUE,
               "PENUMBRA_AMQP_ROUTING_KEY": PAGE_QUEUE,
               "PENUMBRA_AMQP_EXCHANGE_NAME": EXCHANGE,
               "PENUMBRA_CHROMIUM_EXECUTABLE_PATH": str(wrapper),
               "PENUMBRA_INSTALL_PLAYWRIGHT": "false",
               "PENUMBRA_METRICS_ENABLED": "false",
               "PENUMBRA_CONTEXTS_PER_BROWSER": str(concurrency)}
        env |= {f"PENUMBRA_{k.upper()}": str(v) for k, v in self.config.get("penumbra_env", {}).items()}
        cmd = self._uv_run("penumbra")
        log.write("$ " + " ".join(cmd) + "\n")
        log.flush()
        return subprocess.Popen(cmd, cwd=workdir, stdout=log, stderr=subprocess.STDOUT, env=env,
                                start_new_session=True)

    def _run_heritrix(self, seeds, workdir: Path, home: Path, container: str,
                      penumbra: subprocess.Popen) -> subprocess.Popen:
        jobs_dir = workdir / "jobs"
        job_dir = jobs_dir / JOB_NAME
        job_dir.mkdir(parents=True, exist_ok=True)
        (job_dir / "crawler-beans.cxml").write_text(self.build_config(seeds))
        password = secrets.token_urlsafe(16)
        port = _free_port()
        cmd = [str(home / "bin" / "heritrix"), "-a", f"admin:{password}", "-b", "127.0.0.1", "-p", str(port),
               "-j", str(jobs_dir), "-r", JOB_NAME]
        env = {**os.environ, "FOREGROUND": "true", "HERITRIX_HOME": str(home)}
        if java_opts := self.config.get("java_opts", "").strip():
            env["JAVA_OPTS"] = java_opts
        log = open(workdir / "heritrix.log", "w")
        log.write("$ " + " ".join(cmd) + "\n")
        log.flush()
        proc = subprocess.Popen(cmd, cwd=workdir, stdout=log, stderr=subprocess.STDOUT, env=env,
                                start_new_session=True)
        api = _HeritrixApi(port, password)
        deadline = time.monotonic() + self.timeout
        quiet = 0
        while proc.poll() is None:
            if time.monotonic() > deadline:
                raise CrawlError(f"timed out after {self.timeout}s (see {workdir / 'heritrix.log'})")
            if penumbra.poll() is not None:
                raise CrawlError(f"penumbra exited with status {penumbra.returncode} (see penumbra.log)")
            time.sleep(0.5)
            state = api.state()
            if state == "EMPTY" and self._queues_idle(container):
                quiet += 1
                if quiet >= 3:
                    api.terminate()
                    break
            else:
                quiet = 0
        try:
            code = proc.wait(timeout=120)
        except subprocess.TimeoutExpired:
            raise CrawlError("Heritrix didn't exit after the job was terminated")
        if code != 0:
            raise CrawlError(f"heritrix exited with status {code} (see {workdir / 'heritrix.log'})")
        return proc

    def _queue_counts(self, container: str) -> dict[str, int] | None:
        """Ready plus unacknowledged messages in each queue."""
        out = subprocess.run([self.engine, "exec", container, "rabbitmqctl", "-q", "list_queues", "name",
                              "messages_ready", "messages_unacknowledged"], capture_output=True, text=True)
        if out.returncode != 0:
            return None
        counts = {}
        for line in out.stdout.splitlines():
            parts = line.split()
            if len(parts) == 3 and parts[1].isdigit():
                counts[parts[0]] = int(parts[1]) + int(parts[2])
        return counts

    def _wait_for_queue(self, container: str, queue: str, penumbra: subprocess.Popen):
        """Wait for Penumbra to bind its queue: until then, pages Heritrix publishes would be dropped."""
        deadline = time.monotonic() + 300
        while time.monotonic() < deadline:
            if penumbra.poll() is not None:
                raise CrawlError(f"penumbra exited with status {penumbra.returncode} (see penumbra.log)")
            if queue in (self._queue_counts(container) or {}):
                return
            time.sleep(0.5)
        raise CrawlError("penumbra didn't start consuming (see penumbra.log)")

    def _queues_idle(self, container: str) -> bool:
        """Whether both queues exist and have no ready or unacknowledged messages."""
        counts = self._queue_counts(container)
        return counts is not None and {PAGE_QUEUE, CLIENT_ID} <= counts.keys() and not any(counts[q] for q in (PAGE_QUEUE, CLIENT_ID))


class _HeritrixApi:
    def __init__(self, port: int, password: str):
        self.url = f"https://127.0.0.1:{port}/engine/job/{JOB_NAME}"
        auth = urllib.request.HTTPDigestAuthHandler()
        auth.add_password("Authentication Required", f"https://127.0.0.1:{port}/", "admin", password)
        context = ssl.create_default_context()
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE
        self.opener = urllib.request.build_opener(auth, urllib.request.HTTPSHandler(context=context))

    def state(self) -> str | None:
        try:
            request = urllib.request.Request(self.url, headers={"Accept": "application/xml"})
            with self.opener.open(request, timeout=10) as response:
                return ElementTree.fromstring(response.read()).findtext("crawlControllerState")
        except (OSError, ElementTree.ParseError):
            return None  # not up yet

    def terminate(self):
        data = urllib.parse.urlencode({"action": "terminate"}).encode()
        with self.opener.open(urllib.request.Request(self.url, data=data, headers={"Accept": "application/xml"}),
                              timeout=30):
            pass


def _amqp_client_version(contrib_jar: Path) -> str:
    """The RabbitMQ client version the contrib module was built against, from the POM inside its jar."""
    with zipfile.ZipFile(contrib_jar) as jar:
        for name in jar.namelist():
            if name.endswith("heritrix-contrib/pom.xml"):
                pom = jar.read(name).decode()
                if m := re.search(r"<artifactId>amqp-client</artifactId>\s*<version>([^<$]+)</version>", pom):
                    return m.group(1)
    return AMQP_CLIENT_VERSION


def _download(url: str, dest: Path):
    try:
        with urllib.request.urlopen(url, timeout=60) as response:
            dest.write_bytes(response.read())
    except OSError as e:
        raise CrawlError(f"couldn't download {url}: {e}")


def _stop(proc: subprocess.Popen):
    if proc.poll() is not None:
        return
    os.killpg(proc.pid, signal.SIGTERM)
    try:
        proc.wait(timeout=30)
    except subprocess.TimeoutExpired:
        os.killpg(proc.pid, signal.SIGKILL)
