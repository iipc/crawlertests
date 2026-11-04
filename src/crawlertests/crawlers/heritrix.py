"""Heritrix, with or without the BrowserProcessor.

The job configuration is generated from the profile-crawler-beans.cxml shipped
inside the installed Heritrix's engine jar, so it tracks whatever version is
being tested. Settings are applied through the profile's simpleOverrides
PropertyOverrideConfigurer, plus text substitutions for the seeds and (in
browser mode) the fetcher class and the browser bean. Each substitution is
checked so a profile change fails loudly rather than silently testing the
wrong configuration.

The job is run with "heritrix -r", which launches it, waits for it to finish
and exits.
"""

import os
import re
import secrets
import socket
import subprocess
import zipfile
from pathlib import Path
from xml.sax.saxutils import escape

from . import CrawlError, Crawler

PROFILE_PATH = "org/archive/crawler/restlet/profile-crawler-beans.cxml"
JOB_NAME = "crawlertests"

DEFAULT_OVERRIDES = {
    "metadata.operatorContactUrl": "http://localhost/crawlertests",
    "metadata.jobName": JOB_NAME,
    "metadata.description": "IIPC crawlertests run",
    "disposition.delayFactor": "0.0",
    "disposition.minDelayMs": "0",
    "disposition.maxDelayMs": "0",
    "disposition.respectCrawlDelayUpToSeconds": "0",
    "frontier.retryDelaySeconds": "1",
}

MAC_FIREFOX = "/Applications/Firefox.app/Contents/MacOS/firefox"


class Heritrix(Crawler):
    def __init__(self, name: str, config: dict, browser: bool):
        super().__init__(name, config)
        self.browser = browser
        home = config.get("home")
        self.home = Path(home).expanduser().resolve() if home else None

    def unavailable(self) -> str | None:
        if self.home is None:
            return "set 'home' to a Heritrix distribution directory (or HERITRIX_HOME)"
        if not (self.home / "bin" / "heritrix").exists():
            return f"{self.home}/bin/heritrix not found"
        if self.config.get("profile") is None and not self._engine_jar():
            return f"no heritrix-engine jar in {self.home}/lib"
        return None

    def _engine_jar(self) -> Path | None:
        jars = sorted((self.home / "lib").glob("heritrix-engine-*.jar"))
        return jars[-1] if jars else None

    def _load_profile(self) -> str:
        if profile := self.config.get("profile"):
            return Path(profile).expanduser().read_text()
        with zipfile.ZipFile(self._engine_jar()) as jar:
            return jar.read(PROFILE_PATH).decode()

    def build_config(self, seeds: list[str]) -> str:
        cxml = self._load_profile()

        overrides = DEFAULT_OVERRIDES | {"crawlController.maxToeThreads": str(self.config.get("threads", 8))}
        if self.browser:
            overrides["browser.concurrency"] = str(self.config.get("browser_concurrency", 4))
        overrides |= self.config.get("overrides", {})
        lines = "\n".join(f"{k}={v}" for k, v in overrides.items())
        cxml = _replace_once(cxml, "##..more?..##", lines, "simpleOverrides placeholder")

        cxml = _replace_once(cxml, "http://example.example/example", "\n".join(seeds), "seeds placeholder")

        if self.browser:
            cxml = _replace_once(cxml, '<bean id="fetchHttp" class="org.archive.modules.fetcher.FetchHTTP">',
                                 '<bean id="fetchHttp" class="org.archive.modules.fetcher.FetchHTTP2">',
                                 "fetchHttp bean")
            cxml, n = re.subn(r'<!--\s*<bean id="browser".*?</bean>\s*-->', lambda m: self._browser_bean(), cxml,
                              count=1, flags=re.DOTALL)
            if n != 1:
                raise CrawlError("couldn't find the commented-out browser bean in the profile")
            cxml = _replace_once(cxml, '<!-- <ref bean="browser"/> -->', '<ref bean="browser"/>',
                                 "browser ref in fetch chain")

        if (js := self.config.get("extractor_js")) is not None:
            # ExtractorHTML autowires the ExtractorJS bean by type, so this also applies to inline scripts
            js = dict(js)
            cls = js.pop("class", "org.archive.modules.extractor.ConfigurableExtractorJS")
            props = "".join(f'\n  <property name="{escape(k)}" value="{escape(_bean_value(v))}" />'
                            for k, v in js.items())
            cxml = _replace_once(cxml, '<bean id="extractorJs" class="org.archive.modules.extractor.ExtractorJS">',
                                 f'<bean id="extractorJs" class="{escape(cls)}">{props}', "extractorJs bean")

        if extractors := self.config.get("extractors"):
            # after the profile's last extractor, so before the browser in browser mode
            beans = "".join(f'\n    <bean class="{escape(e)}"/>' for e in extractors)
            cxml = _replace_once(cxml, '<ref bean="extractorSwf"/>', '<ref bean="extractorSwf"/>' + beans,
                                 "extractorSwf ref in fetch chain")

        if extra := self.config.get("extra_beans"):
            cxml = _replace_once(cxml, "</beans>", extra + "\n</beans>", "closing </beans>")
        return cxml

    def version(self, workdir: Path) -> dict:
        info = {"product": "Heritrix"}
        for jar in sorted((self.home / "lib").glob("heritrix-commons-*.jar")):
            with zipfile.ZipFile(jar) as z:
                info["version"] = z.read("org/archive/util/version.txt").decode().strip()
        if self.browser and (executable := self._executable()):
            try:
                out = subprocess.run([executable, "--version"], capture_output=True, text=True, timeout=30)
                info["browser"] = out.stdout.strip().splitlines()[-1] if out.stdout.strip() else executable
            except (OSError, subprocess.TimeoutExpired):
                info["browser"] = executable
        return info

    def _executable(self) -> str | None:
        executable = self.config.get("executable")
        if executable is None and Path(MAC_FIREFOX).exists():
            executable = MAC_FIREFOX
        return executable

    def _browser_bean(self) -> str:
        executable = self._executable()
        options = self.config.get("options")
        if options is None and executable and "firefox" in executable.lower():
            options = ["-headless"]

        props = []
        if executable:
            props.append(f'  <property name="executable" value="{escape(executable)}" />')
        if options is not None:
            values = "".join(f"<value>{escape(o)}</value>" for o in options)
            props.append(f'  <property name="options"><list>{values}</list></property>')
        if behaviors := self.config.get("behaviors"):
            beans = "".join(f'<bean class="{escape(b)}" />' for b in behaviors)
            props.append(f'  <property name="behaviors"><list>{beans}</list></property>')
        return ('<bean id="browser" class="org.archive.crawler.processor.BrowserProcessor">\n'
                + "\n".join(props) + "\n </bean>")

    def _trust_store_opts(self, workdir: Path) -> str:
        store = workdir / "truststore.p12"
        store.unlink(missing_ok=True)
        subprocess.run([_keytool(), "-importcert", "-noprompt", "-alias", "crawlertests", "-file", str(self.ca_cert),
                        "-keystore", str(store), "-storetype", "PKCS12", "-storepass", "crawlertests"],
                       check=True, capture_output=True)
        return (f"-Djavax.net.ssl.trustStore={store} -Djavax.net.ssl.trustStoreType=PKCS12"
                f" -Djavax.net.ssl.trustStorePassword=crawlertests")

    def crawl(self, seeds: list[str], workdir: Path) -> None:
        jobs_dir = workdir / "jobs"
        job_dir = jobs_dir / JOB_NAME
        job_dir.mkdir(parents=True, exist_ok=True)
        (job_dir / "crawler-beans.cxml").write_text(self.build_config(seeds))

        cmd = [str(self.home / "bin" / "heritrix"),
               "-a", f"admin:{secrets.token_urlsafe(16)}",
               "-b", "127.0.0.1",
               "-p", str(_free_port()),
               "-j", str(jobs_dir),
               "-r", JOB_NAME]
        env = {"FOREGROUND": "true", "HERITRIX_HOME": str(self.home)}
        java_opts = self.config.get("java_opts", "")
        if self.ca_cert:
            # FetchHTTP trusts any certificate, but FetchHTTP2 uses the JVM's trust store
            java_opts += " " + self._trust_store_opts(workdir)
        if java_opts.strip():
            env["JAVA_OPTS"] = java_opts.strip()
        self.run_logged(cmd, workdir, "heritrix.log", env)


def _bean_value(value) -> str:
    return str(value).lower() if isinstance(value, bool) else str(value)


def _keytool() -> str:
    if java_home := os.environ.get("JAVA_HOME"):
        return str(Path(java_home) / "bin" / "keytool")
    return "keytool"


def _replace_once(text: str, old: str, new: str, what: str) -> str:
    count = text.count(old)
    if count != 1:
        raise CrawlError(f"expected exactly one {what} in the Heritrix profile, found {count}")
    return text.replace(old, new)


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]
