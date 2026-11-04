"""Compact per-crawler run records, for tracking results across runs and versions.

A record holds what a crawler captured (normalized URL keys with method and
status) and what it requested from the fixture server, along with the crawler
and suite versions. It doesn't hold pass/fail results: those are computed by
scoring the record against the current cases, so old runs can be re-scored as
cases and expectations change.

A record also holds a hash of each crawled case's fixture files and the
harness version. A case's results stay valid until its fixtures change (or the
harness version is bumped), so after a change only the affected cases need
re-crawling, and a crawler version's results combine, case by case, the latest
runs whose fixtures match the current ones.

Records are stored as <results>/runs/<run>/<crawler>.json.
"""

import hashlib
import json
import subprocess
import tomllib
from collections import defaultdict
from pathlib import Path

from .cases import Case
from .crawlers import NOTES
from .compare import crawler_scores, score_case
from .warcs import Capture

FORMAT = 1

# Bump when a harness change alters what crawlers are served or how captures are read, so every case of every
# saved version is re-crawled. Changes to fixtures need no bump: they're tracked per case by case_hash().
HARNESS_VERSION = 1


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
        "note": info.get("note"),
        "track": info.get("track"),
        "version": info.get("version", {}),
        "suite": meta.get("suite", {}),
        "platform": meta.get("platform"),
        "status": info["status"],
        "message": info.get("message"),
        "duration": info.get("duration"),
        "cases": meta["cases"],
        "harness": meta.get("harness"),
        "case_hashes": meta.get("case_hashes"),
        "hash_scheme": meta.get("hash_scheme"),
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


def crawler_note(record: dict) -> str | None:
    """Why a crawler skips some of what the cases test by design, if it does."""
    return record.get("note") or NOTES.get(record.get("type"))


def results_for(records: list[tuple[str, dict]], cases: dict[str, Case], run: str) -> dict:
    """Build a results dict (as rendered by report.py) from (column label, record) pairs."""
    results = {"run": run, "crawlers": {}, "cases": {}}
    scored = {}
    for label, record in records:
        info = {k: record.get(k) for k in ("status", "message", "duration", "version", "track")}
        info["note"] = crawler_note(record)
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


def record_harness(record: dict) -> int:
    """The harness version a record was made with; records from before it was recorded are version 1."""
    harness = record.get("harness")
    return 1 if harness is None else harness


def fingerprint(record: dict) -> tuple:
    """What makes a run worth keeping: a different crawler version, harness version, or set of case fixtures.
    (Records from before case hashes were recorded fall back to the suite revision.)"""
    fixtures = (tuple(sorted(record["case_hashes"].items())) if record.get("case_hashes")
                else (record.get("suite", {}).get("revision"), tuple(record["cases"])))
    return (record["crawler"], record.get("track"), json.dumps(record.get("version"), sort_keys=True),
            record_harness(record), fixtures)


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


# How case_hash() is computed. Records hashed another way are re-hashed from git at their suite revision.
HASH_SCHEME = 2

# case.toml keys that only describe or score a case. Editing them re-scores saved runs but doesn't change what
# a crawler sees, so it doesn't make a case stale.
_SCORING_KEYS = {"title", "description", "tags", "expect", "expect_not"}


def _blob_id(data: bytes) -> str:
    return hashlib.sha1(b"blob %d\0" % len(data) + data).hexdigest()


def _toml_id(data: bytes) -> str:
    """An id for a case.toml covering only what affects the fixtures (routes, robots rules, ...)."""
    try:
        parsed = tomllib.loads(data.decode())
    except (UnicodeDecodeError, tomllib.TOMLDecodeError):
        return _blob_id(data)
    served = {k: v for k, v in parsed.items() if k not in _SCORING_KEYS}
    return _blob_id(json.dumps(served, sort_keys=True, default=str).encode())


def _hash_listing(entries: list[tuple[str, str]]) -> str:
    listing = "".join(f"{path} {blob}\n" for path, blob in sorted(entries) if not path.split("/")[-1].startswith("."))
    return hashlib.sha256(listing.encode()).hexdigest()[:16]


def case_hash(case_dir: Path) -> str:
    """A hash of what a crawler sees of a case: its fixture files' paths and git blob ids, with case.toml reduced
    to its serving keys. It can also be computed for any past commit from git alone (CaseHashes.at)."""
    return _hash_listing([(p.relative_to(case_dir).as_posix(),
                           _toml_id(p.read_bytes()) if p.name == "case.toml" and p.parent == case_dir
                           else _blob_id(p.read_bytes()))
                          for p in case_dir.rglob("*") if p.is_file()])


class CaseHashes:
    """Case fixture hashes, now and as of past commits, for records saved before hashes were recorded (or
    hashed with an older HASH_SCHEME)."""

    def __init__(self, cases: dict[str, Case], repo: Path | None = None):
        self.current = {name: case_hash(case.dir) for name, case in cases.items()}
        self.repo = repo
        self._at: dict[str, dict[str, str]] = {}

    def _git(self, *args, input=None) -> bytes | None:
        out = subprocess.run(["git", "-C", str(self.repo), *args], capture_output=True, input=input)
        return out.stdout if out.returncode == 0 else None

    def at(self, revision: str | None) -> dict[str, str]:
        if not revision or self.repo is None:
            return {}
        if revision not in self._at:
            files: dict[str, list] = {}
            tomls = []
            for line in (self._git("ls-tree", "-r", revision, "--", "cases") or b"").decode().splitlines():
                meta, path = line.split("\t", 1)
                parts = path.split("/", 2)
                if len(parts) == 3:
                    entry = [parts[2], meta.split()[2]]
                    files.setdefault(parts[1], []).append(entry)
                    if parts[2] == "case.toml":
                        tomls.append(entry)
            if tomls:
                # read every case.toml in one go: "<blob> blob <size>\n<data>\n" each
                out = self._git("cat-file", "--batch", input="".join(f"{e[1]}\n" for e in tomls).encode()) or b""
                pos = 0
                for entry in tomls:
                    header_end = out.index(b"\n", pos)
                    size = int(out[pos:header_end].split()[2])
                    entry[1] = _toml_id(out[header_end + 1:header_end + 1 + size])
                    pos = header_end + 1 + size + 1
            self._at[revision] = {name: _hash_listing([tuple(e) for e in entries]) for name, entries in files.items()}
        return self._at[revision]

    def of(self, record: dict) -> dict[str, str]:
        """The fixture hash of each case the record crawled (empty where unknown)."""
        if record.get("case_hashes") and record.get("hash_scheme") == HASH_SCHEME:
            return record["case_hashes"]
        return self.at((record.get("suite") or {}).get("revision")) or {}

    def fresh(self, record: dict, case: str) -> bool:
        """Whether the record's results for a case are still valid: same harness and same fixtures. A case
        whose old hash can't be determined counts as fresh."""
        if record_harness(record) != HARNESS_VERSION:
            return False
        old = self.of(record).get(case)
        return old is None or old == self.current.get(case)


def version_key(record: dict) -> tuple:
    """Identifies a crawler build: re-crawls of the same build share this key."""
    v = record.get("version") or {}
    return record["crawler"], record.get("track"), v.get("version"), v.get("revision")


def merge_builds(records: list[dict], hashes: CaseHashes) -> list[tuple[dict, dict[str, dict]]]:
    """Combine each crawler build's runs case by case: for every current case, the latest run of the build that
    crawled it with the current fixtures. Returns (latest record, {case: record it's taken from}) per build;
    cases with no fresh run are left out, so they count as not run until re-crawled."""
    builds = defaultdict(list)
    for record in records:
        builds[version_key(record)].append(record)
    merged = []
    for runs in builds.values():
        runs.sort(key=lambda r: r.get("time") or "")
        sources = {}
        for record in runs:  # later runs override earlier ones
            for case in record["cases"]:
                if case in hashes.current and hashes.fresh(record, case):
                    sources[case] = record
        merged.append((runs[-1], sources))
    return merged


# Java for each Heritrix release line: 3.4.x needs 11, up to 3.13 needs 17.
def heritrix_java(version: str) -> str:
    parts = [int(p) for p in version.split("-")[0].split(".")[:2] if p.isdigit()]
    minor = parts[1] if len(parts) > 1 else 0
    return "11" if minor <= 4 else "17" if minor <= 13 else "21"


def stale_versions(records: list[dict], hashes: CaseHashes) -> list[dict]:
    """Released crawler versions with cases that need re-crawling (new cases, changed fixtures, or a harness
    version bump), newest versions first, as crawl workflow matrix entries {"job", "version", "java", "cases"}
    ("cases" is empty when every case is needed). Development builds aren't included: the nightly run replaces
    them with the current build anyway."""
    entries = {}
    for latest, sources in merge_builds(records, hashes):
        version = (latest.get("version") or {}).get("version")
        if latest.get("track") != "release" or not version:
            continue
        stale = set(hashes.current) - set(sources)
        if not stale:
            continue
        kind = latest.get("type", "")
        if kind in ("heritrix", "heritrix-browser"):
            job, java = "heritrix-release", heritrix_java(version)
        elif kind in ("browsertrix", "zeno"):
            job, java = kind, ""
        elif kind == "stormcrawler":
            job, java = kind, "21"
        else:
            continue
        entry = entries.setdefault((job, version), {"job": job, "version": version, "java": java, "_stale": set(),
                                                     "_date": (latest.get("version") or {}).get("date") or ""})
        entry["_stale"] |= stale
    result = []
    for entry in sorted(entries.values(), key=lambda e: (e["_date"], e["job"], e["version"]), reverse=True):
        stale = entry.pop("_stale")
        entry.pop("_date")
        entry["cases"] = "" if stale == set(hashes.current) else " ".join(sorted(stale))
        result.append(entry)
    return result
