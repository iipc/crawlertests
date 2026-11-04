"""Print the crawl workflow's job matrix as JSON.

Reads the workflow_dispatch inputs from the environment. With no inputs (the
nightly run) it runs the latest releases, the Heritrix development build and
Heritrix with Penumbra and Common Crawl's Nutch from their default branches, in full. It also re-crawls
saved releases that have stale cases (new cases, changed fixtures or a harness
version bump; see `crawlertests stale`), crawling only those cases and at most
BUDGET versions a night, newest first, so a big change catches up over a few
nights. With RECRAWL set, it runs only the re-crawls.
"""

import json
import os
import subprocess

from crawlertests.history import heritrix_java

heritrix = os.environ.get("HERITRIX_RELEASE", "").strip()
browsertrix = os.environ.get("BROWSERTRIX_TAG", "").strip()
zeno = os.environ.get("ZENO_RELEASE", "").strip().removeprefix("v")
stormcrawler = os.environ.get("STORMCRAWLER_RELEASE", "").strip().removeprefix("stormcrawler-")
java = os.environ.get("JAVA", "").strip()
only = os.environ.get("ONLY", "").strip()
recrawl = os.environ.get("RECRAWL", "") == "true"
budget = int(os.environ.get("BUDGET") or 8)  # 0: no limit

if heritrix or browsertrix or zeno or stormcrawler:
    # a backfill of specific versions
    entries = []
    if heritrix:
        entries.append({"job": "heritrix-release", "version": heritrix, "java": java or heritrix_java(heritrix)})
    if browsertrix:
        entries.append({"job": "browsertrix", "version": browsertrix, "java": ""})
    if zeno:
        entries.append({"job": "zeno", "version": zeno, "java": ""})
    if stormcrawler:
        entries.append({"job": "stormcrawler", "version": stormcrawler, "java": java or "21"})
else:
    entries = [] if recrawl else [
        {"job": "heritrix-release", "version": "", "java": java or "21"},
        {"job": "heritrix-dev", "version": "", "java": java or "21"},
        {"job": "heritrix-penumbra", "version": "", "java": java or "21"},
        {"job": "browsertrix", "version": "", "java": ""},
        {"job": "zeno", "version": "", "java": ""},
        {"job": "stormcrawler", "version": "", "java": java or "21"},
        {"job": "ccnutch", "version": "", "java": ""},
    ]
    if os.path.isdir("results"):
        stale = json.loads(subprocess.run(["crawlertests", "stale", "results"], check=True, capture_output=True,
                                          text=True).stdout)
        if not recrawl:
            # each job's newest saved version is most likely the latest release, which runs in full anyway
            newest = set()
            stale = [e for e in stale if e["job"] in newest or newest.add(e["job"])]
        if only:
            stale = [e for e in stale if e["job"] == only]
        entries += stale[:budget] if budget else stale
    if only:
        entries = [e for e in entries if e["job"] == only]

for entry in entries:
    entry.setdefault("cases", "")
print(json.dumps({"include": entries}))
