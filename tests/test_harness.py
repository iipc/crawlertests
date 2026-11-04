"""Checks of the harness itself: fixture server, scoring and Heritrix config generation."""

import gzip
import json
from io import BytesIO
import ssl
import urllib.request
import zlib
import xml.dom.minidom
from pathlib import Path
from urllib.error import HTTPError

import pytest
from warcio.statusandheaders import StatusAndHeaders
from warcio.warcwriter import WARCWriter

from crawlertests.cases import load_cases
from crawlertests.compare import captured_keys, crawler_scores, requested_keys, score_case
from crawlertests.crawlers.heritrix import Heritrix
from crawlertests.server import FixtureServer
from crawlertests.urls import split_marker
from crawlertests.warcs import read_captures

CASES_DIR = Path(__file__).parent.parent / "cases"
HERITRIX_PROFILE = (Path.home() / "src/heritrix3/engine/src/main/resources"
                    / "org/archive/crawler/restlet/profile-crawler-beans.cxml")


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args):
        return None


INSECURE = ssl.create_default_context()
INSECURE.check_hostname = False
INSECURE.verify_mode = ssl.CERT_NONE


@pytest.fixture(scope="module")
def server(tmp_path_factory):
    srv = FixtureServer(load_cases(CASES_DIR), cert_dir=tmp_path_factory.mktemp("tls"))
    srv.start()
    yield srv
    srv.stop()


def get(server, path, port=None, method=None, headers=None, data=None):
    port = port or server.port
    scheme = "https" if port in (server.https_port, server.https_other_port) else "http"
    req = urllib.request.Request(f"{scheme}://127.0.0.1:{port}{path}", data=data, method=method,
                                 headers=headers or {})
    try:
        opener = urllib.request.build_opener(NoRedirect, urllib.request.HTTPSHandler(context=INSECURE))
        resp = opener.open(req)
    except HTTPError as e:
        resp = e
    return resp.status, resp.headers, resp.read()


def test_every_case_loads_and_resolves():
    cases = load_cases(CASES_DIR)
    assert len(cases) >= 20
    for case in cases.values():
        assert (case.dir / "index.html").exists() or "" in case.routes, case.name
        assert case.expect or case.expect_not, case.name
        for exp in case.expected() + case.unexpected():
            assert case.owns(exp.key), f"{case.name}: {exp.key} is outside the case directory"


def test_files_are_templated(server):
    status, headers, body = get(server, "/cases/cross-origin/")
    assert status == 200
    assert f"http://127.0.0.1:{server.other_port}/cases/cross-origin/other-img.png".encode() in body


def test_placeholders(server):
    status, headers, body = get(server, "/cases/html-embeds/anything.png")
    assert status == 200 and headers["Content-Type"] == "image/png" and body.startswith(b"\x89PNG")
    status, headers, body = get(server, "/cases/html-embeds/page.html")
    assert headers["Content-Type"].startswith("text/html") and b"href" not in body
    assert get(server, "/cases/html-embeds/case.toml")[0] == 200  # a placeholder, not the file
    assert b"expect" not in get(server, "/cases/html-embeds/case.toml")[2]
    assert get(server, "/cases/no-such-case/")[0] == 404
    assert get(server, "/cases/html-embeds/../../pyproject.toml")[0] in (200, 404)
    assert b"[project]" not in get(server, "/cases/html-embeds/%2e%2e/%2e%2e/pyproject.toml")[2]


def test_routes(server):
    status, headers, _ = get(server, "/cases/http-redirects/r301")
    assert status == 301 and headers["Location"] == "target-301.html"
    status, headers, _ = get(server, "/cases/http-redirects/r308")
    assert headers["Location"] == f"http://127.0.0.1:{server.port}/cases/http-redirects/target-308.html"
    status, headers, body = get(server, "/cases/http-link-header/")
    assert "rel=stylesheet" in headers["Link"] and b"Link response header" in body


def test_templated_pdf_xref(server):
    _, headers, pdf = get(server, "/cases/pdf-links/docs/doc.pdf")
    assert headers["Content-Type"] == "application/pdf"
    assert b"{{ORIGIN}}" not in pdf and f"127.0.0.1:{server.port}".encode() in pdf
    startxref = int(pdf.rsplit(b"startxref\n", 1)[1].split()[0])
    assert pdf[startxref:].startswith(b"xref\n")
    entries = pdf[startxref:].split(b"\n")[3:]
    for n, entry in enumerate(e for e in entries if e.endswith(b" n ")):
        offset = int(entry.split()[0])
        assert pdf[offset:].startswith(b"%d 0 obj" % (n + 1))
    length = int(pdf.split(b"/Length ", 1)[1].split()[0])
    stream = pdf.split(b"stream\n", 1)[1].split(b"\nendstream", 1)[0]
    assert length == len(stream)


def test_root_files_and_robots(server):
    status, headers, body = get(server, "/robots.txt")
    assert status == 200 and body.startswith(b"User-agent: *\n")
    assert b"Disallow: /cases/robots-txt/disallowed/" in body
    assert f"Sitemap: http://127.0.0.1:{server.port}/cases/sitemaps/sitemap-index.xml".encode() in body
    status, headers, body = get(server, "/favicon.ico")
    assert status == 200 and headers["Content-Type"] in ("image/vnd.microsoft.icon", "image/x-icon")
    status, headers, body = get(server, "/cases/sitemaps/sitemap-news.xml.gz")
    assert "Content-Encoding" not in headers and gzip.decompress(body).startswith(b"<?xml")


def test_content_encoding(server):
    def fetch(path, accept=None):
        req = urllib.request.Request(f"http://127.0.0.1:{server.port}{path}")
        if accept:
            req.add_header("Accept-Encoding", accept)
        resp = urllib.request.urlopen(req)
        return resp.headers, resp.read()
    headers, body = fetch("/cases/http-content-encoding/gzip.html")
    assert "Content-Encoding" not in headers and b"from-gzip.html" in body
    headers, body = fetch("/cases/http-content-encoding/gzip.html", "br, gzip;q=0.5")
    assert headers["Content-Encoding"] == "gzip" and b"from-gzip.html" in gzip.decompress(body)
    headers, body = fetch("/cases/http-content-encoding/deflate.html", "gzip, deflate")
    assert headers["Content-Encoding"] == "deflate" and b"from-deflate.html" in zlib.decompress(body)
    headers, body = fetch("/cases/http-content-encoding/gzip.html", "gzip;q=0")
    assert "Content-Encoding" not in headers
    headers, body = fetch("/cases/http-content-encoding/forced-gzip.html")
    assert headers["Content-Encoding"] == "gzip"
    headers, _ = fetch("/cases/http-content-type/untyped-page")
    assert "Content-Type" not in headers


def test_https(server):
    status, headers, body = get(server, "/cases/cross-origin/", port=server.https_port)
    assert status == 200
    assert f"http://127.0.0.1:{server.other_port}/".encode() in body
    status, _, _ = get(server, "/cases/cross-origin/", port=server.https_other_port)
    assert status == 200


def test_cookies_ranges_and_new_encodings(server):
    import brotli
    import zstandard
    route = next(r for c in load_cases(CASES_DIR).values() for r in c.routes.values()
                 if r.require_cookie and r.without_cookie is None)
    case = next(c for c in load_cases(CASES_DIR).values() if route in c.routes.values())
    path = case.prefix + route.path
    assert get(server, path)[0] == 403
    assert get(server, path, headers={"Cookie": "other=1; " + route.require_cookie})[0] == 200

    status, headers, body = get(server, "/cases/media-range/sound.wav", headers={"Range": "bytes=0-99"})
    assert status == 206 and len(body) == 100 and headers["Content-Range"].startswith("bytes 0-99/")
    status, headers, full = get(server, "/cases/media-range/sound.wav")
    assert status == 200 and headers["Accept-Ranges"] == "bytes" and full.startswith(body)
    assert get(server, "/cases/media-range/sound.wav", headers={"Range": "bytes=-10"})[2] == full[-10:]

    _, headers, body = get(server, "/cases/https-compression/br.html", port=server.https_port,
                           headers={"Accept-Encoding": "gzip, br, zstd"})
    assert headers["Content-Encoding"] == "br" and b"from-br.html" in brotli.decompress(body)
    _, headers, body = get(server, "/cases/https-compression/zstd.html", port=server.https_port,
                           headers={"Accept-Encoding": "zstd"})
    assert headers["Content-Encoding"] == "zstd"
    assert b"from-zstd.html" in zstandard.ZstdDecompressor().decompressobj().decompress(body)


def test_request_log(server):
    server.set_tag("probe")
    get(server, "/cases/js-fetch/api/fetch.json")
    get(server, "/cases/cross-origin/other-img.png", port=server.other_port)
    get(server, "/cases/cross-origin/x.png", port=server.https_other_port)
    server.set_tag(None)
    keys = requested_keys(server.requests_for("probe"), server.origins)
    assert keys == {"/cases/js-fetch/api/fetch.json", "{OTHER}/cases/cross-origin/other-img.png",
                    "{HTTPS_OTHER}/cases/cross-origin/x.png"}


def archive(writer, server, path, port, method="GET", headers=None, data=None):
    """Fetch a URL and write request and response records, as a crawler would (warcio's capture_http
    doesn't record HTTPS)."""
    scheme = "https" if port in (server.https_port, server.https_other_port) else "http"
    url = f"{scheme}://127.0.0.1:{port}{path}"
    status, resp_headers, body = get(server, path, port, method, headers, data)
    http_headers = StatusAndHeaders(f"{status} X", list(resp_headers.items()), protocol="HTTP/1.1")
    response = writer.create_warc_record(url, "response", payload=BytesIO(body), http_headers=http_headers)
    req_headers = StatusAndHeaders(f"{method} {path} HTTP/1.1", list((headers or {}).items()), is_http_request=True)
    request = writer.create_warc_record(url, "request", payload=BytesIO(data or b""), http_headers=req_headers)
    request.rec_headers.add_header("WARC-Concurrent-To", response.rec_headers.get_header("WARC-Record-ID"))
    writer.write_record(response)
    writer.write_record(request)


def test_perfect_crawler_passes(server, tmp_path):
    """Fetch exactly the expected URLs into a WARC, as a browser would: every case should pass."""
    cases = load_cases(CASES_DIR)
    ports = {marker: port for port, marker in server.origins.items()}
    cookies = "; ".join(r.require_cookie for c in cases.values() for r in c.routes.values() if r.require_cookie)
    warc = tmp_path / "perfect.warc.gz"
    with open(warc, "wb") as f:
        writer = WARCWriter(f, gzip=True)
        for case in cases.values():
            for exp in case.expected():
                marker, path = split_marker(exp.key)
                data = b"x=1" if exp.method == "POST" else None
                archive(writer, server, path, ports[marker], exp.method or "GET", {"Cookie": cookies}, data)

    captured = captured_keys(read_captures([warc]), server.origins)
    for case in cases.values():
        result = score_case(case, captured, None)
        assert result["passed"], (case.name, result)
        assert result["extra"] == []


def test_failures_are_scored(server, tmp_path):
    cases = load_cases(CASES_DIR, ["js-false-positives"])
    case = cases["js-false-positives"]
    warc = tmp_path / "junk.warc.gz"
    with open(warc, "wb") as f:
        writer = WARCWriter(f, gzip=True)
        archive(writer, server, "/cases/js-false-positives/application/json", server.port)
        archive(writer, server, "/cases/js-false-positives/unmentioned.html", server.port)
    captured = captured_keys(read_captures([warc]), server.origins)
    result = score_case(case, captured, {"/cases/js-false-positives/real.html"})
    assert not result["passed"]
    assert result["hits"] == len(case.expect_not) - 1
    assert result["extra"] == ["/cases/js-false-positives/unmentioned.html"]
    assert result["not_archived"] == ["/cases/js-false-positives/real.html"]
    assert result["missed"] == 1 and result["overcrawled"] == 1

    scores = crawler_scores([result, result])
    assert scores["expected"] == 2 and scores["missed"] == 2
    assert scores["unexpected"] == 2 * len(case.expect_not) and scores["overcrawled"] == 2
    assert scores["hits"] == scores["total"] - 4 and scores["cases_passed"] == 0


@pytest.mark.skipif(not HERITRIX_PROFILE.exists(), reason="no Heritrix checkout")
@pytest.mark.parametrize("browser", [False, True])
def test_heritrix_config(browser):
    crawler = Heritrix("h", {"profile": str(HERITRIX_PROFILE), "executable": "/usr/bin/firefox",
                             "extractors": ["org.archive.modules.extractor.ExtractorPDF"]}, browser)
    cxml = crawler.build_config(["http://127.0.0.1:1234/cases/a/", "http://127.0.0.1:1234/cases/b/"])
    xml.dom.minidom.parseString(cxml)  # well-formed
    assert "http://127.0.0.1:1234/cases/b/" in cxml
    assert "disposition.minDelayMs=0" in cxml
    assert '<ref bean="extractorSwf"/>\n    <bean class="org.archive.modules.extractor.ExtractorPDF"/>' in cxml
    assert 'class="org.archive.modules.extractor.ExtractorJS"' in cxml
    if browser:
        assert 'class="org.archive.modules.fetcher.FetchHTTP2"' in cxml
        assert '<ref bean="browser"/>' in cxml
        assert '<property name="executable" value="/usr/bin/firefox" />' in cxml
        assert "<value>-headless</value>" in cxml
    else:
        assert '<!-- <ref bean="browser"/> -->' in cxml
        assert 'class="org.archive.modules.fetcher.FetchHTTP"' in cxml


@pytest.mark.skipif(not HERITRIX_PROFILE.exists(), reason="no Heritrix checkout")
def test_heritrix_extractor_js_config():
    crawler = Heritrix("h", {"profile": str(HERITRIX_PROFILE), "extractor_js": {"strict": True}}, False)
    cxml = crawler.build_config(["http://127.0.0.1:1234/cases/a/"])
    xml.dom.minidom.parseString(cxml)
    assert ('<bean id="extractorJs" class="org.archive.modules.extractor.ConfigurableExtractorJS">\n'
            '  <property name="strict" value="true" />') in cxml


def test_records_saving_and_site(tmp_path):
    from crawlertests.history import load_records, make_record, save_run, score_record, write_record
    from crawlertests.site import build_site
    from crawlertests.warcs import Capture

    cases = load_cases(CASES_DIR, ["http-post", "cross-origin"])
    captured = {exp.key: [Capture(exp.key, "response", 200, "", exp.method or "GET")]
                for case in cases.values() for exp in case.expected()}
    meta = {"run": "r1", "time": "2026-01-01T00:00:00+00:00", "suite": {"revision": "abc"}, "cases": list(cases),
            "crawlers": {"perfect": {"status": "ok", "track": "release", "version": {"version": "1.0"}}}}
    record = make_record(meta, "perfect", captured, set())
    run_dir = tmp_path / "r1"
    write_record(record, run_dir / "records" / "perfect.json")

    loaded = json.loads((run_dir / "records" / "perfect.json").read_text())
    assert loaded == record
    assert all(r["passed"] for r in score_record(loaded, cases).values())
    assert ["/cases/http-post/api/save", "POST", 200] in loaded["captures"]

    results = tmp_path / "results"
    assert len(save_run(run_dir, results)) == 1
    assert save_run(run_dir, results) == []  # same versions: nothing new
    assert len(save_run(run_dir, results, force=True)) == 1
    assert [r["run"] for r in load_records(results)] == ["r1"]

    build_site(tmp_path / "empty", cases, tmp_path / "empty-site")
    assert "No runs yet" in (tmp_path / "empty-site" / "index.html").read_text()

    build_site(results, cases, tmp_path / "site")
    index = (tmp_path / "site" / "index.html").read_text()
    assert ">perfect<" in index and "perfect (" not in index and "<svg" in index
    assert (tmp_path / "site" / "runs" / "r1" / "index.html").exists()
    assert (tmp_path / "site" / "history" / "perfect--release.html").exists()
    assert json.loads((tmp_path / "site" / "data" / "index.json").read_text())[0]["id"] == "r1/perfect"
