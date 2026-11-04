"""Loading test cases from the cases/ directory.

Each case is a directory containing a case.toml and the fixture files served
for it. The crawl seed for a case is its directory URL, /cases/<name>/, which
serves index.html, on the primary HTTP origin (or the primary HTTPS origin if
the case sets https = true).

URLs in expectations are written relative to the case directory ("img.png"),
relative to the seed's origin ("/cases/other/x.png") or with an explicit
origin marker ("{OTHER}/cases/name/x.png", "{HTTPS}/…", "{HTTPS_OTHER}/…", or
"{HTTP}/…" for the primary HTTP origin).
They are resolved to keys in the same form produced by urls.normalize(). An
expectation can also be a table that requires a method or status:
{ url = "api/save", method = "POST" } or { url = "song.wav", status = 200 }.
"""

import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urljoin

from .urls import HTTP, HTTPS, MARKERS, normalize_path, split_marker


@dataclass
class Route:
    """A custom response for a path within a case (redirects, headers, delays)."""
    path: str
    status: int = 200
    headers: dict[str, str] = field(default_factory=dict)
    body: str | None = None
    file: str | None = None
    content_type: str | None = None  # "" sends no Content-Type at all
    delay: float = 0.0
    encoding: str | None = None  # "gzip", "deflate", "br" or "zstd" Content-Encoding
    negotiate: bool = True  # only apply the encoding if the request's Accept-Encoding allows it
    gzip_body: bool = False  # gzip the body itself, as for a .gz file, without a Content-Encoding
    ranges: bool = False  # answer Range requests with 206 Partial Content
    require_cookie: str | None = None  # "name=value" the request must carry...
    without_cookie: "Route | None" = None  # ...or get this response instead (default 403)


@dataclass(frozen=True)
class Expectation:
    key: str
    method: str | None = None
    status: int | None = None

    @property
    def label(self) -> str:
        label = f"{self.method} {self.key}" if self.method else self.key
        return f"{label} [{self.status}]" if self.status else label

    def matches(self, capture) -> bool:
        return ((self.method is None or capture.method == self.method)
                and (self.status is None or capture.status == self.status))


@dataclass
class Case:
    name: str
    dir: Path
    title: str
    description: str
    tags: list[str]
    expect: list
    expect_not: list
    routes: dict[str, Route]
    root: dict[str, str]  # files this case serves outside its directory, e.g. {"/favicon.ico": "favicon.ico"}
    robots: str  # lines this case contributes to the shared /robots.txt
    https: bool = False  # seed on the HTTPS origin

    @property
    def prefix(self) -> str:
        return f"/cases/{self.name}/"

    @property
    def marker(self) -> str:
        """Origin marker of the case's seed."""
        return HTTPS if self.https else ""

    def resolve(self, ref: str) -> str:
        """Resolve an expectation reference to a normalized URL key."""
        if ref.startswith(HTTP):  # the primary HTTP origin, from an HTTPS case
            return normalize_path(ref[len(HTTP):])
        for marker in MARKERS:
            if ref.startswith(marker):
                return marker + normalize_path(ref[len(marker):])
        return self.marker + normalize_path(urljoin(self.prefix, ref))

    def _expectations(self, refs: list) -> list[Expectation]:
        result = []
        for ref in refs:
            if isinstance(ref, str):
                result.append(Expectation(self.resolve(ref)))
            else:
                result.append(Expectation(self.resolve(ref["url"]), ref.get("method"), ref.get("status")))
        return result

    def expected(self) -> list[Expectation]:
        return self._expectations(self.expect)

    def unexpected(self) -> list[Expectation]:
        return self._expectations(self.expect_not)

    def seeds(self) -> set[str]:
        return {self.marker + self.prefix, self.marker + self.prefix + "index.html"}

    def owns(self, key: str) -> bool:
        """True if a normalized URL key falls within this case's directory on any origin, or is one of its
        root files (on the primary origin only)."""
        if key in self.root or (self.robots and key == "/robots.txt"):
            return True
        return split_marker(key)[1].startswith(self.prefix)


def load_case(dir: Path) -> Case:
    with open(dir / "case.toml", "rb") as f:
        data = tomllib.load(f)
    routes = {}
    for r in data.get("routes", []):
        if "without_cookie" in r:
            r = dict(r, without_cookie=Route(path=r["path"], **r["without_cookie"]))
        route = Route(**r)
        routes[route.path] = route
    return Case(
        name=dir.name,
        dir=dir,
        title=data.get("title", dir.name),
        description=data.get("description", "").strip(),
        tags=data.get("tags", []),
        expect=data.get("expect", []),
        expect_not=data.get("expect_not", []),
        routes=routes,
        root=data.get("root", {}),
        robots=data.get("robots", "").strip(),
        https=data.get("https", False),
    )


def load_cases(cases_dir: Path, selectors: list[str] | None = None) -> dict[str, Case]:
    """Load all cases, optionally filtered by name substring, "name:<exact name>" or "tag:<tag>" selectors."""
    cases = {}
    for dir in sorted(cases_dir.iterdir()):
        if not (dir / "case.toml").exists():
            continue
        case = load_case(dir)
        if selectors and not any(_matches(case, s) for s in selectors):
            continue
        cases[case.name] = case
    return cases


def _matches(case: Case, selector: str) -> bool:
    if selector.startswith("tag:"):
        return selector[4:] in case.tags
    if selector.startswith("name:"):
        return selector[5:] == case.name
    return selector in case.name
