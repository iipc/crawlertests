"""Print the crawl workflow's job matrix as JSON.

Reads the workflow_dispatch inputs from the environment. With no inputs (the
nightly run) it runs the latest releases and the Heritrix development build,
plus every saved release whose results predate the current cases or harness
(see `crawlertests stale`), so older versions are re-crawled when the tests
change. With RECRAWL set, it runs only those.
"""

import json
import os
import subprocess

from crawlertests.history import heritrix_java

heritrix = os.environ.get("HERITRIX_RELEASE", "").strip()
browsertrix = os.environ.get("BROWSERTRIX_TAG", "").strip()
java = os.environ.get("JAVA", "").strip()
only = os.environ.get("ONLY", "").strip()
recrawl = os.environ.get("RECRAWL", "") == "true"

if heritrix or browsertrix:
    # a backfill of specific versions
    entries = []
    if heritrix:
        entries.append({"job": "heritrix-release", "version": heritrix, "java": java or heritrix_java(heritrix)})
    if browsertrix:
        entries.append({"job": "browsertrix", "version": browsertrix, "java": ""})
else:
    entries = [] if recrawl else [
        {"job": "heritrix-release", "version": "", "java": java or "21"},
        {"job": "heritrix-dev", "version": "", "java": java or "21"},
        {"job": "browsertrix", "version": "", "java": ""},
    ]
    if os.path.isdir("results"):
        stale = json.loads(subprocess.run(["crawlertests", "stale", "results"], check=True, capture_output=True,
                                          text=True).stdout)
        entries += stale
    if only:
        entries = [e for e in entries if e["job"] == only]

print(json.dumps({"include": entries}))
