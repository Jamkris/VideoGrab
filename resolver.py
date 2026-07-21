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
    # Referer to send when downloading `url`. None means "use the page URL".
    # Some CDNs only serve the stream for a specific origin (see red69 below).
    referer: str | None = None

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

# --- Site-specific rule: the red69 CDN (redbogo / jbgirl / xbbg mirror boards) --
# These boards all embed videos from one shared CDN that (a) is reachable only
# under a "/cb1" path prefix and (b) 403s unless the Referer is the canonical
# player origin — the board page's own URL is rejected. The player also builds
# the URL in JS from a bare relative "/hls/<id>/master.m3u8", so the generic
# absolute-URL scan never sees it. Both quirks are handled here and nowhere else;
# this is a fragile, host-specific hack that will break if the CDN moves again
# (it already migrated cdn.redxxx.net -> cdn.red69.quest).
_RED69_HOST = "cdn.red69.quest"
_RED69_REFERER = "https://redbogo12.com/"
_RED69_HLS_RE = re.compile(
    r"""/hls/[^\s"'<>()]+?\.m3u8(?:\?[^\s"'<>()]*)?""",
    re.IGNORECASE,
)


def _red69_stream(html: str) -> str | None:
    """Reconstruct the absolute red69 playlist URL from a board/player page.

    Mirrors the site's own JS: the "/hls/…/master.m3u8" path is served from
    https://cdn.red69.quest under a "/cb1" prefix. We extract only the "/hls/…"
    tail (identical whether the page holds a bare relative path or an
    already-absolute, possibly already-/cb1-prefixed URL) and rebuild it, so the
    result is correct and idempotent across every variant. Returns None unless
    the page is clearly a red69 mirror, so a stray "/hls/…m3u8" on an unrelated
    host is never rewritten onto this CDN.
    """
    if "red69" not in html and "redxxx" not in html:
        return None
    match = _RED69_HLS_RE.search(html)
    if not match:
        return None
    return f"https://{_RED69_HOST}/cb1{match.group(0)}"


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

    # (url, referer) pairs in priority order. The red69 CDN rule comes first —
    # its stream needs a specific Referer, and the generic scan would otherwise
    # find the same URL but pair it with the (rejected) page URL. Generic
    # candidates use the page URL as Referer, matching browser behavior.
    candidates: list[tuple[str, str]] = []
    red69 = _red69_stream(html)
    if red69:
        candidates.append((red69, _RED69_REFERER))
    candidates += [(url, page_url) for url in _candidates(html, page_url)]

    for url, referer in candidates:
        if _looks_like_hls(http, url, referer=referer):
            return StreamInfo(url=url, title=title, referer=referer)
    # Verification failed for every candidate (e.g. a flaky proxy). If any
    # candidate has an unambiguous playlist extension, hand it to yt-dlp anyway
    # — yt-dlp does its own retries and is the real arbiter of whether it plays.
    for url, referer in candidates:
        path = urllib.parse.urlsplit(url).path.lower()
        if path.endswith((".m3u8", ".txt")):
            return StreamInfo(url=url, title=title, referer=referer)
    return None
