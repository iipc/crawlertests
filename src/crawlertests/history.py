"""Compact per-crawler run records, for tracking results across runs and versions.

A record holds what a crawler captured (normalized URL keys with method and
status) and what it requested from the fixture server, along with the crawler
and suite versions. It doesn't hold pass/fail results: those are computed by
scoring the record against the current cases, so old runs can be re-scored as
cases and expectations change.

Records are stored as <results>/runs/<run>/<crawler>.json.
"""

import json
from pathlib import Path

from .cases import Case
from .compare import crawler_scores, score_case
from .warcs import Capture

FORMAT = 1


def make_record(meta: dict, name: str, captured: dict[str, list[Capture]], requested: set[str]) -> dict:
    info = meta["crawlers"][name]
    captures = sorted({(key, c.method, c.status) for key, cs in captured.items() for c in cs},
                      key=lambda t: (t[0], t[1], t[2] if t[2] is not None else -1))
    return {
        "format": FORMAT,
        "run": meta["run"],
        "time": meta.get("time"),
        "crawler": name,
        "type": info.get("type"),
        "track": info.get("track"),
        "version": info.get("version", {}),
        "suite": meta.get("suite", {}),
        "platform": meta.get("platform"),
        "status": info["status"],
        "message": info.get("message"),
        "duration": info.get("duration"),
        "cases": meta["cases"],
        "captures": [list(t) for t in captures],
        "requested": sorted(requested),
    }


def record_captures(record: dict) -> dict[str, list[Capture]]:
    captured: dict[str, list[Capture]] = {}
    for key, method, status in record["captures"]:
        captured.setdefault(key, []).append(Capture(key, "response", status, "", method))
    return captured


def score_record(record: dict, cases: dict[str, Case]) -> dict[str, dict]:
    """Score a record against the current cases, for the cases the run included that still exist."""
    captured = record_captures(record)
    requested = set(record["requested"])
    return {name: score_case(cases[name], captured, requested) for name in record["cases"] if name in cases}


def results_for(records: list[tuple[str, dict]], cases: dict[str, Case], run: str) -> dict:
    """Build a results dict (as rendered by report.py) from (column label, record) pairs."""
    results = {"run": run, "crawlers": {}, "cases": {}}
    scored = {}
    for label, record in records:
        info = {k: record.get(k) for k in ("status", "message", "duration", "version", "track")}
        results["crawlers"][label] = info
        if record["status"] == "ok":
            scored[label] = score_record(record, cases)
            info["scores"] = crawler_scores(list(scored[label].values()))
    for name, case in cases.items():
        case_results = {label: s[name] for label, s in scored.items() if name in s}
        if not case_results and scored:
            continue
        results["cases"][name] = {"title": case.title, "description": case.description, "tags": case.tags,
                                  "results": case_results}
    return results


def write_record(record: dict, path: Path):
    """Write a record as JSON with one capture or request per line, so changes diff well in git."""
    lines = []
    for key, value in record.items():
        if isinstance(value, list) and value:
            items = ",\n".join("  " + json.dumps(v, ensure_ascii=False) for v in value)
            lines.append(f"{json.dumps(key)}: [\n{items}\n ]")
        else:
            lines.append(f"{json.dumps(key)}: {json.dumps(value, ensure_ascii=False)}")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{\n " + ",\n ".join(lines) + "\n}\n")


def load_records(results_dir: Path) -> list[dict]:
    records = []
    for path in sorted((results_dir / "runs").glob("*/*.json")):
        record = json.loads(path.read_text())
        if record.get("format") == FORMAT:
            records.append(record)
    return records


def fingerprint(record: dict) -> tuple:
    """What makes a run worth keeping: a different crawler version, suite version or case list."""
    return (record["crawler"], record.get("track"), json.dumps(record.get("version"), sort_keys=True),
            record.get("suite", {}).get("revision"), tuple(record["cases"]))


def save_run(run_dir: Path, results_dir: Path, force: bool = False) -> list[Path]:
    """Copy a run's records into the results directory, skipping any whose crawler and suite versions are the
    same as a stored run of that crawler and track (unless forced), so backfilling an older version doesn't make
    the next nightly run save the current version again. Returns the paths written."""
    stored = {fingerprint(record) for record in load_records(results_dir)}
    written = []
    for path in sorted((run_dir / "records").glob("*.json")):
        record = json.loads(path.read_text())
        if record["status"] != "ok":
            continue
        if not force and fingerprint(record) in stored:
            continue
        dest = results_dir / "runs" / record["run"] / path.name
        write_record(record, dest)
        written.append(dest)
    return written


def version_key(record: dict) -> tuple:
    """Identifies a crawler build: re-crawls of the same build for a newer suite share this key."""
    v = record.get("version") or {}
    return record["crawler"], record.get("track"), v.get("version"), v.get("revision")


def current_records(records: list[dict]) -> list[dict]:
    """The latest record of each crawler build, so a build re-crawled after the cases changed replaces its
    earlier record instead of appearing twice."""
    latest = {}
    for record in records:
        key = version_key(record)
        if key not in latest or (record.get("time") or "") >= (latest[key].get("time") or ""):
            latest[key] = record
    return list(latest.values())


# Java for each Heritrix release line: 3.4.x needs 11, up to 3.13 needs 17.
def heritrix_java(version: str) -> str:
    parts = [int(p) for p in version.split("-")[0].split(".")[:2] if p.isdigit()]
    minor = parts[1] if len(parts) > 1 else 0
    return "11" if minor <= 4 else "17" if minor <= 13 else "21"


def stale_versions(records: list[dict], suite_revision: str | None, case_names: list[str]) -> list[dict]:
    """Released crawler versions whose latest saved run predates the current cases or harness, as crawl workflow
    matrix entries ({"job", "version", "java"}). Development builds aren't included: the nightly run replaces
    them with the current build anyway."""
    entries = {}
    for record in current_records(records):
        if record.get("track") != "release" or not (record.get("version") or {}).get("version"):
            continue
        fresh = (record.get("suite") or {}).get("revision") == suite_revision and set(case_names) <= set(record["cases"])
        if fresh:
            continue
        version = record["version"]["version"]
        kind = record.get("type", "")
        if kind in ("heritrix", "heritrix-browser"):
            entries[("heritrix-release", version)] = {"job": "heritrix-release", "version": version,
                                                      "java": heritrix_java(version)}
        elif kind in ("browsertrix", "zeno"):
            entries[(kind, version)] = {"job": kind, "version": version, "java": ""}
        elif kind == "stormcrawler":
            entries[(kind, version)] = {"job": kind, "version": version, "java": "21"}
    return sorted(entries.values(), key=lambda e: (e["job"], e["version"]))
