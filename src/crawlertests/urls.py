"""URL normalization so captures from different crawlers can be compared.

Crawlers reach the fixture server under different hostnames (127.0.0.1 from
the host, host.containers.internal from a container), so URLs are reduced to
keys that drop the host and mark the origin by which server port it was on:
"/path?query" for the primary HTTP origin, "{OTHER}/path?query" for the
secondary HTTP origin, and "{HTTPS}" and "{HTTPS_OTHER}" for their TLS
counterparts. URLs on any other port are kept whole.
"""

from urllib.parse import quote, unquote_to_bytes, urlsplit

HTTP = "{HTTP}"  # only in expectations; keys for the primary HTTP origin have no marker
OTHER = "{OTHER}"
HTTPS = "{HTTPS}"
HTTPS_OTHER = "{HTTPS_OTHER}"
MARKERS = (OTHER, HTTPS, HTTPS_OTHER)

_PATH_SAFE = "/:@!$&'()*+,;=-._~"
_QUERY_SAFE = _PATH_SAFE + "?"


def split_marker(key: str) -> tuple[str, str]:
    """Split a key into its origin marker ("" for the primary origin) and path."""
    for marker in MARKERS:
        if key.startswith(marker):
            return marker, key[len(marker):]
    return "", key


def normalize_path(path_and_query: str) -> str:
    """Normalize percent-encoding of a "/path?query" string and drop any fragment.

    Works on bytes, so a query percent-encoded in a legacy charset (caf%E9) keeps its bytes, and
    raw non-ASCII characters are taken as UTF-8.
    """
    path_and_query = path_and_query.split("#", 1)[0]
    path, sep, query = path_and_query.partition("?")
    path = quote(unquote_to_bytes(path), safe=_PATH_SAFE) or "/"
    if sep:
        return path + "?" + quote(unquote_to_bytes(query), safe=_QUERY_SAFE)
    return path


def normalize(url: str, origins: dict[int, str]) -> str:
    """Reduce an absolute URL to a comparison key, given the origin marker for each server port."""
    parts = urlsplit(url)
    path = normalize_path(parts.path + ("?" + parts.query if parts.query else ""))
    marker = origins.get(parts.port)
    return url if marker is None else marker + path
