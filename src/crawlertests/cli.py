"""Command line interface.

  crawlertests list                      list cases
  crawlertests serve                     run the fixture server for manual testing
  crawlertests run [-c CRAWLER] [CASE…]  crawl and compare
  crawlertests compare RUN_DIR           re-score an earlier run (e.g. after editing expectations)
  crawlertests save RUN_DIR RESULTS_DIR  add a run's records to a results history
  crawlertests stale RESULTS_DIR         list saved versions to re-crawl for the current cases (JSON)
  crawlertests site RESULTS_DIR -o DIR   build the results history website
"""

import argparse
import json
import os
import platform
import subprocess
import sys
import time
import tomllib
from datetime import datetime, timezone
from pathlib import Path

from .cases import load_cases
from .compare import captured_keys, requested_keys
from .crawlers import CrawlError, make_crawler
from .history import (HARNESS_VERSION, HASH_SCHEME, CaseHashes, case_hash, load_records, make_record, results_for, save_run,
                      stale_versions, write_record)
from .report import text_summary, write_html, write_json
from .server import FixtureServer, dump_log
from .urls import OTHER
from .warcs import find_warcs, read_captures

DEFAULT_CRAWLERS = {
    "heritrix": {"type": "heritrix"},
    "heritrix-strict-js": {"type": "heritrix", "extractor_js": {"strict": True}},
    "heritrix-browser": {"type": "heritrix-browser"},
    "heritrix-penumbra": {"type": "heritrix-penumbra"},
    "browsertrix": {"type": "browsertrix"},
    "zeno": {"type": "zeno"},
    "stormcrawler": {"type": "stormcrawler"},
    "nutchcc": {"type": "nutchcc"},
}


def load_config(path: Path) -> dict:
    config = {}
    if path.exists():
        with open(path, "rb") as f:
            config = tomllib.load(f)
    crawlers = {name: dict(c) for name, c in DEFAULT_CRAWLERS.items()}
    for name, c in config.get("crawlers", {}).items():
        crawlers.setdefault(name, {}).update(c)
    # Heritrix variants share an installation unless configured separately
    default_home = crawlers.get("heritrix", {}).get("home") or os.environ.get("HERITRIX_HOME")
    for c in crawlers.values():
        if c.get("type", "").startswith("heritrix"):
            c.setdefault("home", default_home)
    config["crawlers"] = crawlers
    config.setdefault("server", {})
    return config


def main(argv=None):
    parser = argparse.ArgumentParser(prog="crawlertests", description="Web archiving crawler link extraction tests")
    parser.add_argument("--config", type=Path, default=Path("crawlertests.toml"))
    parser.add_argument("--cases-dir", type=Path, default=Path("cases"))
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("list", help="list test cases")
    p.add_argument("cases", nargs="*", help="case name substrings or tag:<tag>")

    p = sub.add_parser("serve", help="run the fixture server")
    p.add_argument("--port", type=int, default=8000)
    p.add_argument("--other-port", type=int, default=8001)
    p.add_argument("--https-port", type=int, default=8443)
    p.add_argument("--https-other-port", type=int, default=8444)
    p.add_argument("--bind", default=None)

    p = sub.add_parser("run", help="run crawlers and compare results")
    p.add_argument("cases", nargs="*", help="case name substrings or tag:<tag>")
    p.add_argument("-c", "--crawler", action="append",
                   help="crawler to run (repeatable; default: all configured)")
    p.add_argument("-o", "--output", type=Path, help="run directory (default: runs/<timestamp>)")

    p = sub.add_parser("compare", help="re-score an earlier run against the current expectations")
    p.add_argument("run_dir", type=Path)

    p = sub.add_parser("save", help="add a run's records to a results history directory")
    p.add_argument("run_dir", type=Path)
    p.add_argument("results_dir", type=Path)
    p.add_argument("--force", action="store_true",
                   help="save even if the crawler and suite versions match the latest saved run")

    p = sub.add_parser("stale", help="list saved crawler versions that need re-crawling for the current cases")
    p.add_argument("results_dir", type=Path)

    p = sub.add_parser("site", help="build the results history website")
    p.add_argument("results_dir", type=Path)
    p.add_argument("-o", "--output", type=Path, default=Path("_site"))

    args = parser.parse_args(argv)
    config = load_config(args.config)

    if args.command == "list":
        for name, case in load_cases(args.cases_dir, args.cases).items():
            print(f"{name:28} {case.title}  [{', '.join(case.tags)}]")
    elif args.command == "serve":
        serve(args, config)
    elif args.command == "run":
        return run(args, config)
    elif args.command == "compare":
        return compare(args)
    elif args.command == "save":
        for path in save_run(args.run_dir, args.results_dir, args.force):
            print(f"saved {path}")
    elif args.command == "stale":
        hashes = CaseHashes(load_cases(args.cases_dir), repo_root(args.cases_dir))
        print(json.dumps(stale_versions(load_records(args.results_dir), hashes)))
    elif args.command == "site":
        from .site import build_site
        build_site(args.results_dir, load_cases(args.cases_dir), args.output, repo_root(args.cases_dir))
        print(f"site: {args.output / 'index.html'}")


def serve(args, config):
    cases = load_cases(args.cases_dir)
    bind = args.bind or config["server"].get("bind", "127.0.0.1")
    server = FixtureServer(cases, bind, args.port, args.other_port, args.https_port, args.https_other_port,
                           cert_dir=Path(".tls"))
    server.start()
    print(f"serving {len(cases)} cases at http://{bind}:{server.port}/ (other origin :{server.other_port}, "
          f"https :{server.https_port} and :{server.https_other_port})")
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        server.stop()


def run(args, config) -> int:
    cases = load_cases(args.cases_dir, args.cases)
    if not cases:
        print("no matching cases", file=sys.stderr)
        return 2
    names = args.crawler or list(config["crawlers"])
    unknown = [n for n in names if n not in config["crawlers"]]
    if unknown:
        print(f"unknown crawler(s): {', '.join(unknown)}", file=sys.stderr)
        return 2
    crawlers = [make_crawler(n, config["crawlers"][n]) for n in names]

    run_dir = args.output or Path("runs") / datetime.now().strftime("%Y%m%d-%H%M%S")
    run_dir.mkdir(parents=True, exist_ok=True)
    srv = config["server"]
    server = FixtureServer(cases, srv.get("bind", "127.0.0.1"), srv.get("port", 0), srv.get("other_port", 0),
                           srv.get("https_port", 0), srv.get("https_other_port", 0), cert_dir=run_dir / "tls")
    server.start()
    print(f"fixture server on ports {server.port}/{server.other_port}, https {server.https_port}/"
          f"{server.https_other_port}; {len(cases)} cases; output in {run_dir}")

    meta = {"run": run_dir.name, "time": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "suite": suite_version(args.cases_dir), "platform": f"{platform.system().lower()}-{platform.machine()}",
            "origins": {str(port): marker for port, marker in server.origins.items()},
            "cases": list(cases), "harness": HARNESS_VERSION,
            "case_hashes": {name: case_hash(case.dir) for name, case in cases.items()}, "hash_scheme": HASH_SCHEME,
            "crawlers": {}}
    try:
        for crawler in crawlers:
            meta["crawlers"][crawler.name] = run_crawler(crawler, cases, server, run_dir)
    finally:
        server.stop()
        dump_log(server, run_dir / "requests.jsonl")
        (run_dir / "run.json").write_text(json.dumps(meta, indent=2) + "\n")

    return finish(run_dir, meta, cases)


# What a crawl's results depend on: the fixtures and the harness, but not the report or website code.
SUITE_PATHS = ["cases", "src", ":!src/crawlertests/report.py", ":!src/crawlertests/site.py"]


def repo_root(cases_dir: Path) -> Path | None:
    out = subprocess.run(["git", "rev-parse", "--show-toplevel"], cwd=cases_dir, capture_output=True, text=True)
    return Path(out.stdout.strip()) if out.returncode == 0 else None


def suite_version(cases_dir: Path) -> dict:
    """The last git commit that changed the cases or harness, and whether they have uncommitted changes."""
    def git(*args):
        out = subprocess.run(["git", *args], cwd=cases_dir, capture_output=True, text=True)
        return out.stdout.strip() if out.returncode == 0 else None
    root = git("rev-parse", "--show-toplevel")
    revision = root and git("-C", root, "log", "-1", "--format=%H", "--", *SUITE_PATHS)
    if not revision:
        return {}
    dirty = git("-C", root, "status", "--porcelain", "--", *SUITE_PATHS)
    return {"revision": revision, "dirty": bool(dirty)}


def run_crawler(crawler, cases, server: FixtureServer, run_dir: Path) -> dict:
    info = {"type": crawler.config.get("type", crawler.name), "track": crawler.config.get("track", "local")}
    if note := crawler.config.get("note"):
        info["note"] = note
    if reason := crawler.unavailable():
        print(f"[{crawler.name}] skipped: {reason}")
        return info | {"status": "skipped", "message": reason, "duration": 0}
    workdir = (run_dir / crawler.name).resolve()
    workdir.mkdir(exist_ok=True)
    host = crawler.seed_host()
    # Browsertrix only runs sitemap discovery for the first seed, within that seed's scope
    ordered = sorted(cases.values(), key=lambda case: "sitemaps" not in case.tags)
    seeds = [f"https://{host}:{server.https_port}{case.prefix}" if case.https
             else f"http://{host}:{server.port}{case.prefix}" for case in ordered]
    print(f"[{crawler.name}] crawling {len(seeds)} seeds via {host}…", flush=True)
    crawler.ca_cert = server.cert_dir.resolve() / "cert.pem"
    server.set_tag(crawler.name)
    start = time.monotonic()
    try:
        crawler.crawl(seeds, workdir)
        status, message = "ok", None
    except CrawlError as e:
        status, message = "error", str(e)
    finally:
        server.set_tag(None)
    duration = time.monotonic() - start
    if status == "error" and find_warcs(workdir):
        status = "ok"  # score whatever was captured, but keep the message
    try:
        version = crawler.version(workdir)
    except Exception as e:  # version detection shouldn't lose a crawl's results
        version = {"error": str(e)}
    version |= crawler.config.get("version", {})
    print(f"[{crawler.name}] {status} in {duration:.0f}s" + (f": {message}" if message else "")
          + (f" ({version['version']})" if version.get("version") else ""))
    return info | {"status": status, "message": message, "duration": duration, "version": version}


def compare(args) -> int:
    meta = json.loads((args.run_dir / "run.json").read_text())
    cases = load_cases(args.cases_dir)
    cases = {n: cases[n] for n in meta["cases"] if n in cases}
    return finish(args.run_dir, meta, cases)


def finish(run_dir: Path, meta: dict, cases) -> int:
    requests = []
    log_path = run_dir / "requests.jsonl"
    if log_path.exists():
        requests = [json.loads(line) for line in log_path.read_text().splitlines() if line]
    if "origins" in meta:
        origins = {int(port): marker for port, marker in meta["origins"].items()}
    else:  # runs from before HTTPS support
        origins = {meta["port"]: "", meta["other_port"]: OTHER}

    records = []
    for name, info in meta["crawlers"].items():
        path = run_dir / "records" / f"{name}.json"
        warcs = find_warcs(run_dir / name) if info["status"] == "ok" else []
        if warcs or not path.exists():
            captured = captured_keys(read_captures(warcs), origins)
            requested = requested_keys([r for r in requests if r["tag"] == name], origins)
            record = make_record(meta, name, captured, requested)
            write_record(record, path)
        else:  # WARCs deleted; the record is all that's left
            record = json.loads(path.read_text())
        records.append((name, record))

    results = results_for(records, cases, meta["run"])
    write_json(results, run_dir / "results.json")
    write_html(results, run_dir / "report.html")
    print()
    print(text_summary(results))
    print(f"\nreport: {run_dir / 'report.html'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
