"""Extracting the set of captured URLs from a crawler's WARC files."""

from dataclasses import dataclass
from pathlib import Path

from warcio.archiveiterator import ArchiveIterator

CAPTURE_TYPES = {"response", "revisit", "resource"}


@dataclass
class Capture:
    url: str
    record_type: str
    status: int | None
    warc: str
    method: str = "GET"


def find_warcs(dir: Path) -> list[Path]:
    return sorted(p for p in dir.rglob("*") if p.is_file() and p.name.endswith((".warc", ".warc.gz")))


def read_captures(warcs: list[Path]) -> list[Capture]:
    """Read response, revisit and resource records, taking each one's HTTP method from the request
    record linked to it by WARC-Concurrent-To (in either direction)."""
    captures: dict[str, Capture] = {}
    links: dict[str, list[str]] = {}  # record id -> ids of records concurrent to it (request, metadata...)
    methods: dict[str, str] = {}  # request record id -> method
    for warc in warcs:
        with open(warc, "rb") as f:
            for record in ArchiveIterator(f):
                record_id = record.rec_headers.get_header("WARC-Record-ID")
                for concurrent in record.rec_headers.get_header("WARC-Concurrent-To", "").split():
                    links.setdefault(record_id, []).append(concurrent)
                    links.setdefault(concurrent, []).append(record_id)
                if record.rec_type == "request" and record.http_headers is not None:
                    methods[record_id] = record.http_headers.protocol.upper()
                    continue
                if record.rec_type not in CAPTURE_TYPES:
                    continue
                url = record.rec_headers.get_header("WARC-Target-URI") or ""
                url = url.strip("<>")  # some old writers bracket the URI
                if not url.startswith(("http://", "https://")):
                    continue
                status = None
                if record.http_headers is not None:
                    try:
                        status = int(record.http_headers.get_statuscode())
                    except (TypeError, ValueError):
                        pass
                captures[record_id] = Capture(url, record.rec_type, status, warc.name)
    for record_id, capture in captures.items():
        for linked in links.get(record_id, ()):
            if linked in methods:
                capture.method = methods[linked]
                break
    return list(captures.values())
