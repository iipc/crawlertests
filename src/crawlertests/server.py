"""Fixture HTTP server.

Serves each case directory under /cases/<name>/ on two ports (a primary and a
secondary "other" origin, for cross-origin tests). Requests are resolved as:

  1. a route declared in the case's case.toml (redirects, custom headers, delays)
  2. a file in the case directory (a directory URL serves index.html)
  3. a small placeholder whose type follows the extension

Cases can also serve files outside their directory ("root" in case.toml, e.g.
/favicon.ico) and contribute lines to a shared /robots.txt ("robots").

Placeholders mean fixtures only need files for pages that contain links: any
link target "exists" and returns 200, so a crawler requesting a URL that is
not a real link (for example from speculative JavaScript extraction) shows up
as a capture too. Placeholders never contain links, which bounds the crawl.

Text files are templated: {{ORIGIN}}, {{HOST}}, {{OTHER_ORIGIN}} and
{{OTHER_HOST}} are replaced using the hostname from the request's Host header,
so the same fixture works for crawlers on the host and in containers.

Every request is logged with the name of the crawler currently running, so the
report can show URLs a crawler requested but did not archive.
"""

import base64
import gzip
import html
import json
import mimetypes
import re
import ssl
import subprocess
import tempfile
import threading
import time
import zlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlsplit

from .cases import Case
from .urls import HTTPS, HTTPS_OTHER, OTHER

TEXT_TYPES = ("text/", "application/javascript", "application/json", "image/svg+xml", "application/xml",
              "application/manifest+json", "application/dash+xml", "application/vnd.apple.mpegurl")

PNG_1X1 = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNkYPhfDwAChwGA60e6kgAAAABJRU5ErkJggg==")

PLACEHOLDERS = {
    ".html": ("text/html; charset=utf-8", b"<!doctype html><title>placeholder</title><p>placeholder</p>\n"),
    ".css": ("text/css", b"/* placeholder */\n"),
    ".js": ("application/javascript", b"// placeholder\n"),
    ".mjs": ("application/javascript", b"// placeholder\n"),
    ".json": ("application/json", b"{}\n"),
    ".webmanifest": ("application/manifest+json", b"{}\n"),
    ".svg": ("image/svg+xml", b'<svg xmlns="http://www.w3.org/2000/svg" width="1" height="1"/>\n'),
    ".vtt": ("text/vtt", b"WEBVTT\n"),
    ".xml": ("application/xml", b"<placeholder/>\n"),
    ".rss": ("application/rss+xml", b"<rss version=\"2.0\"><channel></channel></rss>\n"),
    ".map": ("application/json", b'{"version":3,"sources":[],"mappings":""}\n'),
    ".m3u8": ("application/vnd.apple.mpegurl", b"#EXTM3U\n#EXT-X-ENDLIST\n"),
    ".mpd": ("application/dash+xml", b'<MPD xmlns="urn:mpeg:dash:schema:mpd:2011"/>\n'),
}
IMAGE_EXTS = {".png", ".gif", ".jpg", ".jpeg", ".webp", ".avif", ".ico", ".bmp"}


class FixtureServer:
    def __init__(self, cases: dict[str, Case], bind: str = "127.0.0.1", port: int = 0, other_port: int = 0,
                 https_port: int = 0, https_other_port: int = 0, cert_dir: Path | None = None):
        self.cases = cases
        self.tag = None
        self.log: list[dict] = []
        self._lock = threading.Lock()
        handler = self._make_handler()
        self.servers = [ThreadingHTTPServer((bind, p), handler) for p in (port, other_port, https_port, https_other_port)]
        for srv in self.servers:
            srv.daemon_threads = True
        self.port, self.other_port, self.https_port, self.https_other_port = (
            srv.server_address[1] for srv in self.servers)

        self.cert_dir = Path(cert_dir) if cert_dir else Path(tempfile.mkdtemp(prefix="crawlertests-tls-"))
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(*make_certificate(self.cert_dir))
        for srv in self.servers[2:]:
            # handshake in the handler thread, so a client that rejects the certificate can't stall accept()
            srv.socket = context.wrap_socket(srv.socket, server_side=True, do_handshake_on_connect=False)

    @property
    def origins(self) -> dict[int, str]:
        """Origin marker for each port, as used in URL keys."""
        return {self.port: "", self.other_port: OTHER, self.https_port: HTTPS, self.https_other_port: HTTPS_OTHER}

    def start(self):
        for srv in self.servers:
            threading.Thread(target=srv.serve_forever, daemon=True).start()

    def stop(self):
        for srv in self.servers:
            srv.shutdown()
            srv.server_close()

    def set_tag(self, tag: str | None):
        """Attribute subsequent requests to the named crawler."""
        self.tag = tag

    def requests_for(self, tag: str) -> list[dict]:
        with self._lock:
            return [r for r in self.log if r["tag"] == tag]

    def _record(self, entry: dict):
        with self._lock:
            self.log.append(entry)

    def _make_handler(self):
        server = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"
            server_version = "crawlertests"

            def log_message(self, format, *args):
                pass

            def handle(self):
                try:
                    if isinstance(self.request, ssl.SSLSocket):
                        self.request.do_handshake()
                    super().handle()
                except (ssl.SSLError, ConnectionError, TimeoutError):
                    pass

            def do_GET(self):
                self._handle(send_body=True)

            def do_POST(self):
                length = int(self.headers.get("Content-Length") or 0)
                self.rfile.read(length)
                self._handle(send_body=True)

            def do_HEAD(self):
                self._handle(send_body=False)

            def _handle(self, send_body: bool):
                local_port = self.server.server_address[1]
                status, headers, body = server.respond(self.path, self.headers, local_port)
                server._record({
                    "time": time.time(),
                    "tag": server.tag,
                    "port": local_port,
                    "method": self.command,
                    "path": self.path,
                    "status": status,
                    "user_agent": self.headers.get("User-Agent"),
                })
                self.send_response(status)
                for name, value in headers.items():
                    self.send_header(name, value)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                if send_body:
                    self.wfile.write(body)

        return Handler

    def respond(self, raw_path: str, headers, local_port: int) -> tuple[int, dict[str, str], bytes]:
        """Build the response for a request path, given the request headers (a mapping)."""
        parts = urlsplit(raw_path)
        path = unquote(parts.path)
        host_header = headers.get("Host", "")
        hostname = host_header.rsplit(":", 1)[0] if host_header else "127.0.0.1"
        tmpl = {}
        for name, scheme, port in (("", "http", self.port), ("OTHER_", "http", self.other_port),
                                   ("HTTPS_", "https", self.https_port),
                                   ("HTTPS_OTHER_", "https", self.https_other_port)):
            tmpl[f"{{{{{name}ORIGIN}}}}"] = f"{scheme}://{hostname}:{port}"
            tmpl[f"{{{{{name}HOST}}}}"] = f"{hostname}:{port}"

        if path == "/":
            return self._index()
        if path == "/robots.txt":
            return self._robots(tmpl)
        for case in self.cases.values():
            if path in case.root:
                return self._file(case.dir / case.root[path], tmpl)
        if not path.startswith("/cases/"):
            return 404, {"Content-Type": "text/plain"}, b"not found\n"

        name, _, rest = path[len("/cases/"):].partition("/")
        case = self.cases.get(name)
        if case is None:
            return 404, {"Content-Type": "text/plain"}, b"no such case\n"
        if rest == "" and not path.endswith("/"):
            return 301, {"Location": f"/cases/{name}/"}, b""

        route = None
        if parts.query:
            route = case.routes.get(f"{rest}?{parts.query}")
        if route is None:
            route = case.routes.get(rest)
        if route is not None:
            return self._route(case, route, tmpl, headers)

        file = self._find_file(case, rest)
        if file is not None:
            return self._file(file, tmpl)
        return self._placeholder(rest)

    def _index(self):
        items = "".join(f'<li><a href="/cases/{n}/">{html.escape(n)}</a> — {html.escape(c.title)}</li>\n'
                        for n, c in self.cases.items())
        body = f"<!doctype html><title>crawlertests</title><h1>crawlertests</h1><ul>\n{items}</ul>\n"
        return 200, {"Content-Type": "text/html; charset=utf-8"}, body.encode()

    def _robots(self, tmpl: dict[str, str]):
        lines = [c.robots for c in self.cases.values() if c.robots]
        if not lines:
            return 404, {"Content-Type": "text/plain"}, b"not found\n"
        body = "User-agent: *\n" + "\n".join(lines) + "\n"
        return 200, {"Content-Type": "text/plain; charset=utf-8"}, _template(body.encode(), tmpl)

    @staticmethod
    def _find_file(case: Case, rest: str) -> Path | None:
        if rest == "" or rest.endswith("/"):
            rest += "index.html"
        if rest == "case.toml":
            return None
        file = (case.dir / rest).resolve()
        if not file.is_relative_to(case.dir.resolve()) or not file.is_file():
            return None
        return file

    def _file(self, file: Path, tmpl: dict[str, str]):
        content_type = _guess_type(file.name)
        body = file.read_bytes()
        if content_type.startswith(TEXT_TYPES):
            body = _template(body, tmpl)
        elif content_type == "application/pdf":
            body = _rebuild_pdf_xref(_template(body, tmpl))
        return 200, {"Content-Type": content_type}, body

    def _route(self, case: Case, route, tmpl: dict[str, str], request_headers):
        if route.require_cookie and route.require_cookie not in _cookies(request_headers.get("Cookie", "")):
            if route.without_cookie is None:
                return 403, {"Content-Type": "text/plain"}, b"cookie required\n"
            route = route.without_cookie
        if route.delay:
            time.sleep(route.delay)
        status = route.status
        headers = {k: _template(v.encode(), tmpl).decode() for k, v in route.headers.items()}
        if route.file is not None:
            _, file_headers, body = self._file(case.dir / route.file, tmpl)
            headers = file_headers | headers
        else:
            body = _template((route.body or "").encode(), tmpl)
        if route.content_type:
            headers["Content-Type"] = route.content_type
        elif route.content_type == "":
            headers.pop("Content-Type", None)
        if route.gzip_body:
            body = gzip.compress(body, mtime=0)
        if route.encoding:
            if route.negotiate:
                headers["Vary"] = "Accept-Encoding"
            if not route.negotiate or _accepts(request_headers.get("Accept-Encoding", ""), route.encoding):
                body = _encode(body, route.encoding)
                headers["Content-Encoding"] = route.encoding
        if route.ranges:
            headers["Accept-Ranges"] = "bytes"
            if (byte_range := _parse_range(request_headers.get("Range", ""), len(body))) is not None:
                first, last = byte_range
                headers["Content-Range"] = f"bytes {first}-{last}/{len(body)}"
                status, body = 206, body[first:last + 1]
        return status, headers, body

    @staticmethod
    def _placeholder(rest: str):
        ext = Path(rest).suffix.lower()
        if ext in IMAGE_EXTS:
            return 200, {"Content-Type": "image/png"}, PNG_1X1
        if ext in PLACEHOLDERS:
            content_type, body = PLACEHOLDERS[ext]
            return 200, {"Content-Type": content_type}, body
        if ext in ("", ".htm", ".php", ".asp", ".aspx"):
            content_type, body = PLACEHOLDERS[".html"]
            return 200, {"Content-Type": content_type}, body
        return 200, {"Content-Type": _guess_type(rest)}, b""


def make_certificate(dir: Path) -> tuple[Path, Path]:
    """Create (or reuse) a self-signed certificate valid for the names crawlers use to reach the server."""
    cert, key = dir / "cert.pem", dir / "key.pem"
    if not cert.exists():
        dir.mkdir(parents=True, exist_ok=True)
        subprocess.run(["openssl", "req", "-x509", "-newkey", "ec", "-pkeyopt", "ec_paramgen_curve:P-256",
                        "-nodes", "-days", "30", "-subj", "/CN=crawlertests", "-keyout", str(key), "-out", str(cert),
                        "-addext", "subjectAltName=DNS:localhost,DNS:host.containers.internal,"
                                   "DNS:host.docker.internal,IP:127.0.0.1"],
                       check=True, capture_output=True)
    return cert, key


TYPES = {
    ".js": "application/javascript",
    ".mjs": "application/javascript",
    ".webmanifest": "application/manifest+json",
    ".map": "application/json",
    ".wasm": "application/wasm",
    ".mpd": "application/dash+xml",
    ".m3u8": "application/vnd.apple.mpegurl",
    ".m4s": "video/iso.segment",
    ".ts": "video/mp2t",
}


def _guess_type(name: str) -> str:
    if (ext := Path(name).suffix.lower()) in TYPES:
        return TYPES[ext]
    content_type = mimetypes.guess_type(name)[0] or "application/octet-stream"
    if content_type.startswith("text/"):
        content_type += "; charset=utf-8"
    return content_type


def _accepts(accept_encoding: str, coding: str) -> bool:
    for item in accept_encoding.split(","):
        name, _, params = item.strip().partition(";")
        if name.strip().lower() in (coding, "*"):
            return params.replace(" ", "") not in ("q=0", "q=0.0", "q=0.00", "q=0.000")
    return False


def _encode(body: bytes, coding: str) -> bytes:
    if coding == "gzip":
        return gzip.compress(body, mtime=0)
    if coding == "deflate":
        return zlib.compress(body)
    if coding == "br":
        import brotli
        return brotli.compress(body)
    if coding == "zstd":
        import zstandard
        return zstandard.ZstdCompressor().compress(body)
    raise ValueError(f"unknown content coding {coding}")


def _cookies(header: str) -> set[str]:
    return {c.strip() for c in header.split(";") if c.strip()}


def _parse_range(header: str, length: int) -> tuple[int, int] | None:
    """Parse a single "bytes=first-last" range, returning inclusive offsets, or None to send the whole body."""
    m = re.fullmatch(r"\s*bytes=(\d*)-(\d*)\s*", header)
    if not m or (not m.group(1) and not m.group(2)) or length == 0:
        return None
    if not m.group(1):  # suffix range: the last N bytes
        return max(0, length - int(m.group(2))), length - 1
    first = int(m.group(1))
    last = min(int(m.group(2)), length - 1) if m.group(2) else length - 1
    return (first, last) if first <= last else None


def _template(body: bytes, tmpl: dict[str, str]) -> bytes:
    for key, value in tmpl.items():
        body = body.replace(key.encode(), value.encode())
    return body


def _rebuild_pdf_xref(body: bytes) -> bytes:
    """Recompute stream lengths and the xref table of a simple uncompressed PDF after templating.

    Fixture PDFs are written by hand with one "N 0 obj" per line start, direct /Length values and a
    classic xref table, so substituting a URL of a different length only shifts byte offsets.
    """
    def fix_length(m):
        return b"/Length %d" % len(m.group(2)) + m.group(1) + m.group(2) + m.group(3)
    body = re.sub(rb"/Length \d+(\s*>>\s*stream\r?\n)(.*?)(\r?\nendstream)", fix_length, body, flags=re.DOTALL)

    xref_pos = body.rindex(b"\nxref\n") + 1
    trailer = body[body.index(b"trailer", xref_pos):body.index(b"startxref", xref_pos)]
    head = body[:xref_pos]
    offsets = {int(m.group(1)): m.start(1) for m in re.finditer(rb"(?m)^(\d+) 0 obj\b", head)}
    size = max(offsets) + 1
    xref = b"xref\n0 %d\n0000000000 65535 f \n" % size
    xref += b"".join(b"%010d 00000 n \n" % offsets[i] for i in range(1, size))
    return head + xref + trailer + b"startxref\n%d\n%%%%EOF\n" % xref_pos


def dump_log(server: FixtureServer, path: Path):
    with open(path, "w") as f:
        for entry in server.log:
            f.write(json.dumps(entry) + "\n")
