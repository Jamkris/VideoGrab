"""Fallback stream resolver for pages yt-dlp can't handle directly.

Some sites hide the video inside a nested player iframe and disguise the HLS
playlist/segments with innocent extensions (.txt/.dat instead of .m3u8/.ts) to
dodge crawlers and DPI. yt-dlp's generic extractor gives up on these with an
"Unsupported URL" error even though the page clearly plays a video.

This module fetches the page, harvests candidate stream URLs from iframe `src`
params and raw references, and verifies which ones are real HLS playlists by
checking for the #EXTM3U signature. The caller then hands the verified URL back
to yt-dlp with the original page as the Referer.
"""

import html as html_module
import re
import urllib.parse
from typing import NamedTuple

import urllib3


class StreamInfo(NamedTuple):
    url: str
    title: str | None

BROWSER_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36"
)
# Absolute or protocol-relative URLs ending in a playlist-ish extension.
_STREAM_RE = re.compile(
    r"""(?:https?:)?//[^\s"'<>()]+?\.(?:m3u8|txt)(?:\?[^\s"'<>()]*)?""",
    re.IGNORECASE,
)
# `src=<url-encoded>` query params, as used by player.html?src=... wrappers.
_SRC_PARAM_RE = re.compile(r"""[?&]src=([^&"'<>]+)""", re.IGNORECASE)
_TITLE_RE = re.compile(r"<title[^>]*>(.*?)</title>", re.IGNORECASE | re.DOTALL)


def _page_title(html: str) -> str | None:
    match = _TITLE_RE.search(html)
    if not match:
        return None
    title = html_module.unescape(match.group(1)).strip()
    return title or None


def _decode_maybe_double(value: str) -> str:
    """URL-decode a value, twice if it's still percent-encoded (common in
    player wrappers that double-encode the inner stream URL)."""
    once = urllib.parse.unquote(value)
    if "%25" in value or "%2F" in once or "%3A" in once:
        return urllib.parse.unquote(once)
    return once


def _candidates(html: str, base_url: str) -> list[str]:
    found: list[str] = []
    for match in _SRC_PARAM_RE.findall(html):
        decoded = _decode_maybe_double(match)
        if re.search(r"\.(m3u8|txt)(\?|$)", decoded, re.IGNORECASE):
            found.append(decoded)
    for match in _STREAM_RE.findall(html):
        found.append(match)
    # Normalize protocol-relative and relative URLs; dedupe, preserve order.
    seen: set[str] = set()
    result: list[str] = []
    for url in found:
        absolute = urllib.parse.urljoin(base_url, url)
        if absolute not in seen:
            seen.add(absolute)
            result.append(absolute)
    return result


# A DPI-bypass proxy occasionally drops the first attempt, so retry a few times
# with backoff before declaring a candidate dead.
_RETRY = urllib3.Retry(total=3, backoff_factor=0.5, status_forcelist=[502, 503, 504])


def _looks_like_hls(http: urllib3.PoolManager, url: str, referer: str) -> bool:
    try:
        resp = http.request(
            "GET",
            url,
            headers={"User-Agent": BROWSER_UA, "Referer": referer},
            timeout=urllib3.Timeout(connect=10, read=15),
            retries=_RETRY,
            preload_content=False,
        )
        head = resp.read(64)
        resp.release_conn()
    except urllib3.exceptions.HTTPError:
        return False
    if resp.status >= 400:
        return False
    return head.lstrip().startswith(b"#EXTM3U")


def resolve_stream(page_url: str, proxy: str = "") -> StreamInfo | None:
    """Return a verified HLS stream embedded in page_url, or None if none found.

    proxy, when set, routes both the page fetch and verification through it so
    the resolver sees the same (possibly DPI-bypassed) network path yt-dlp will
    use for the actual download.
    """
    http = urllib3.ProxyManager(proxy) if proxy else urllib3.PoolManager()
    try:
        resp = http.request(
            "GET",
            page_url,
            headers={"User-Agent": BROWSER_UA},
            timeout=urllib3.Timeout(connect=10, read=20),
            retries=_RETRY,
        )
    except urllib3.exceptions.HTTPError:
        return None
    if resp.status >= 400:
        return None
    html = resp.data.decode("utf-8", errors="replace")
    title = _page_title(html)

    candidates = _candidates(html, page_url)
    for candidate in candidates:
        if _looks_like_hls(http, candidate, referer=page_url):
            return StreamInfo(url=candidate, title=title)
    # Verification failed for every candidate (e.g. a flaky proxy). If any
    # candidate has an unambiguous playlist extension, hand it to yt-dlp anyway
    # — yt-dlp does its own retries and is the real arbiter of whether it plays.
    for candidate in candidates:
        path = urllib.parse.urlsplit(candidate).path.lower()
        if path.endswith((".m3u8", ".txt")):
            return StreamInfo(url=candidate, title=title)
    return None
