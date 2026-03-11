"""
Google News Tool Handler — Multi-source news aggregator.

Two parallel fetch paths:
  1. Google News RSS (query-targeted, regional)
  2. Curated outlet RSS/Atom feeds (parallel, keyword relevance-scored)

Results are merged, deduplicated, and ranked by query relevance → cross-source
coverage → freshness. Top N articles are returned as structured data for Chalie.
"""

import json
import logging
import os
import re
import time
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor, as_completed, TimeoutError as FuturesTimeout
from urllib.parse import urlencode, urlparse, urlunparse

logger = logging.getLogger(__name__)

_STOP_WORDS = {
    "the", "a", "an", "in", "on", "at", "to", "for", "of", "is", "are", "was",
    "were", "and", "or", "but", "not", "with", "from", "by", "as", "it", "its",
    "this", "that", "has", "have", "had", "be", "been", "will", "would", "can",
    "could", "may", "might", "do", "does", "did", "up", "out", "over", "after",
    "into", "than", "how", "what", "when", "who", "all", "more", "about",
}

# Generic news meta-words that carry no topic specificity
_NEWS_META = {"news", "latest", "today", "update", "updates", "breaking", "report", "reports"}

_RSS_BASE = "https://news.google.com/rss/search"

# Namespace used by Google News RSS for media elements
_NS_MEDIA = {"media": "http://search.yahoo.com/mrss/"}

# Atom 1.0 namespace (used by e.g. The Verge)
_ATOM_NS = "http://www.w3.org/2005/Atom"

_SOURCES_PATH = os.path.join(os.path.dirname(__file__), "sources.json")


def execute(topic: str, params: dict, config: dict = None, telemetry: dict = None) -> dict:
    """
    Search news via Google News RSS + curated outlet feeds and return top articles.

    Args:
        topic: Conversation topic (passed by framework, unused directly)
        params: {
            "query": str (required),
            "limit": int (optional, default 5, clamped to 1-8),
            "region": str (optional, e.g. "US", "GB"),
            "period": str (optional, e.g. "1d", "7d", "30d"),
            "language": str (optional, e.g. "en")
        }
        config: Tool config from DB (unused — no API key needed)
        telemetry: Client telemetry dict with locale, language fields

    Returns:
        {
            "results": [{"title", "source", "date", "published_at", "description",
                         "url", "image_url", "also_reported_by"}],
            "count": int,
            "query": str,
            "_meta": {observability fields}
        }
    """
    query = (params.get("query") or "").strip()
    if not query:
        return {"results": [], "count": 0, "query": "", "_meta": {}}

    limit = max(1, min(8, int(params.get("limit") or 5)))
    region = (params.get("region") or "").strip().upper()
    period = (params.get("period") or "").strip()
    language = (params.get("language") or "").strip().lower()

    # Auto-detect region and language from telemetry when not specified
    if telemetry:
        if not region:
            locale = telemetry.get("locale", "")  # e.g. "en_US" or "en-US"
            if "_" in locale:
                region = locale.split("_")[-1].upper()
            elif "-" in locale:
                region = locale.rsplit("-", 1)[-1].upper()
        if not language:
            lang_tel = (telemetry.get("language", "") or "").strip()
            if lang_tel:
                language = lang_tel.split("-")[0].lower()

    region = region or "US"
    language = language or "en"

    query_words = _query_words(query)

    # Launch outlet fetch in a background thread while Google News fetches
    _outlet_pool = ThreadPoolExecutor(max_workers=1)
    outlet_future = (
        _outlet_pool.submit(_fetch_all_outlets, query_words)
        if query_words else None
    )

    t0 = time.time()
    gnews_articles, retry_used, fetch_error = _fetch_with_retry(query, region, period, language)
    fetch_latency_ms = int((time.time() - t0) * 1000)

    # Collect outlet results (bounded wait — don't let slow outlets stall the response)
    outlet_articles = []
    if outlet_future is not None:
        try:
            outlet_articles = outlet_future.result(timeout=12)
        except Exception as e:
            logger.debug('{"event":"outlet_future_failed","error":"%s"}', str(e)[:80])
        finally:
            _outlet_pool.shutdown(wait=False)

    if fetch_error and not gnews_articles and not outlet_articles:
        logger.error(
            '{"event":"fetch_error","query":"%s","error_type":"%s",'
            '"retry_attempted":true,"retry_succeeded":false,"fetch_latency_ms":%d}',
            query, _classify_error(fetch_error), fetch_latency_ms,
        )
        return {"results": [], "count": 0, "query": query, "error": str(fetch_error)[:200], "_meta": {}}

    # Score Google News articles (already query-targeted, but score fairly against outlets)
    for a in gnews_articles:
        a["_relevance"] = _relevance_score(a, query_words) if query_words else 1

    # Merge and deduplicate
    all_articles = gnews_articles + outlet_articles
    article_count_raw = len(all_articles)

    url_deduped = _dedup_by_url(all_articles)
    title_deduped = _dedup_by_title(url_deduped)
    article_count_deduped = len(title_deduped)
    dedup_removed = article_count_raw - article_count_deduped

    # Rank: relevance (primary) → real URL (secondary) → cross-source coverage → freshness
    ranked = _rerank(title_deduped)

    # Two-tier selection: real publisher URLs first, Google redirect URLs as fallback.
    # This ensures outlet articles (bbc.com, techcrunch.com, etc.) appear before
    # Google News redirect URLs even when their relevance score is slightly lower.
    real_url_articles = [a for a in ranked if _is_real_url(a.get("url", ""))]
    gnews_articles_ranked = [a for a in ranked if not _is_real_url(a.get("url", ""))]
    results_raw = real_url_articles[:limit]
    if len(results_raw) < limit:
        results_raw += gnews_articles_ranked[:limit - len(results_raw)]

    # Strip internal scoring field before output
    results = []
    for a in results_raw:
        a.pop("_relevance", None)
        results.append(a)

    image_count = sum(1 for r in results if r.get("image_url"))
    image_availability_rate = round(image_count / len(results), 2) if results else 0.0
    source_diversity = len({r.get("source", "") for r in results if r.get("source")})
    dedup_ratio = round(dedup_removed / article_count_raw, 2) if article_count_raw else 0.0

    logger.info(
        '{"event":"fetch_ok","query":"%s","article_count":%d,"gnews_count":%d,'
        '"outlet_count":%d,"dedup_removed":%d,"retry_used":%s,"fetch_latency_ms":%d}',
        query, len(results), len(gnews_articles), len(outlet_articles),
        dedup_removed, str(retry_used).lower(), fetch_latency_ms,
    )

    coverage_signal = "sparse" if len(results) < max(2, limit // 2) else "adequate"

    return {
        "results": results,
        "count": len(results),
        "query": query,
        "_meta": {
            "fetch_latency_ms": fetch_latency_ms,
            "article_count_raw": article_count_raw,
            "article_count_deduped": article_count_deduped,
            "dedup_ratio": dedup_ratio,
            "image_availability_rate": image_availability_rate,
            "retry_used": retry_used,
            "source_diversity": source_diversity,
            "region": region,
            "language": language,
            "coverage_signal": coverage_signal,
            "gnews_count": len(gnews_articles),
            "outlet_count": len(outlet_articles),
        },
    }


# ── Google News RSS Fetch ──────────────────────────────────────────────────────

def _build_rss_url(query: str, region: str, language: str, period: str) -> str:
    """Build a Google News RSS URL with regional targeting.

    hl=language-REGION (e.g. en-GB) — UI language and regional edition
    gl=REGION           (e.g. GB)   — geographic focus for results
    ceid=REGION:lang    (e.g. GB:en) — canonical edition identifier
    period is appended as a Google search operator: when:1d, when:7d, when:30d
    """
    period_ops = {"1d": "when:1d", "7d": "when:7d", "30d": "when:30d"}
    period_op = period_ops.get(period, "")
    full_query = f"{query} {period_op}".strip() if period_op else query

    hl = f"{language}-{region}"   # e.g. "en-GB"
    gl = region                    # e.g. "GB"
    ceid = f"{region}:{language}"  # e.g. "GB:en"

    return f"{_RSS_BASE}?{urlencode({'q': full_query, 'hl': hl, 'gl': gl, 'ceid': ceid})}"


def _fetch_rss(url: str) -> list:
    """Fetch and parse Google News RSS. Returns list of raw article dicts.

    Google News RSS has a <source url="...">Publisher</source> per item
    that identifies which outlet published the article.
    """
    import requests as _req

    resp = _req.get(
        url,
        timeout=15,
        headers={"User-Agent": "Mozilla/5.0 (compatible; newsbot/1.0)"},
    )
    resp.raise_for_status()

    root = ET.fromstring(resp.content)
    channel = root.find("channel")
    if channel is None:
        return []

    articles = []
    for item in channel.findall("item"):
        title = (item.findtext("title") or "").strip()
        if not title:
            continue

        link = (item.findtext("link") or "").strip()
        pub_date = (item.findtext("pubDate") or "").strip()
        description_raw = (item.findtext("description") or "").strip()

        # Strip HTML tags from description (RSS descriptions can contain markup)
        description = re.sub(r"<[^>]+>", " ", description_raw).strip()
        description = re.sub(r"\s{2,}", " ", description)

        # Source name from <source url="...">Publisher</source>
        source_el = item.find("source")
        source = (source_el.text or "").strip() if source_el is not None else ""

        # Google News RSS titles often end with " - Publisher Name" — strip it
        if source and title.endswith(f" - {source}"):
            title = title[: -(len(source) + 3)].strip()

        # Image from media:content (present on some items)
        image_url = ""
        media_el = item.find("media:content", _NS_MEDIA)
        if media_el is not None:
            image_url = media_el.get("url", "")
        if not image_url:
            media_thumb = item.find("media:thumbnail", _NS_MEDIA)
            if media_thumb is not None:
                image_url = media_thumb.get("url", "")

        articles.append({
            "title": title,
            "link": link,
            "media": source,
            "date": pub_date,
            "desc": description,
            "img": image_url,
        })

    return articles


def _fetch_with_retry(query: str, region: str, period: str, language: str):
    """Two-attempt fetch strategy:

    - Attempt 1: full params as requested
    - Attempt 2 on exception: same params, 2s backoff (transient network error)
    - Attempt 2 on empty + period set: drop the period filter (too restrictive
      for some regions/queries — common cause of empty UK/MT results)
    - Attempt 2 on empty + no period: break early, nothing better to try
    """
    retry_used = False
    last_error = None

    for attempt in range(2):
        current_period = period
        if attempt > 0:
            retry_used = True
            if last_error:
                # Exception on first attempt — backoff and retry same params
                time.sleep(2)
            else:
                # Empty on first attempt with period — drop the period filter
                current_period = ""

        try:
            url = _build_rss_url(query, region, language, current_period)
            raw = _fetch_rss(url)
            if raw:
                articles = [_normalize_article(r) for r in raw]
                articles = [a for a in articles if a.get("title")]
                if articles:
                    return articles, retry_used, None
        except Exception as e:
            last_error = e
            logger.warning(
                '{"event":"fetch_attempt_failed","query":"%s","attempt":%d,"error":"%s"}',
                query, attempt + 1, str(e)[:120],
            )

        # On first attempt: if empty and no period, nothing better to try
        if attempt == 0 and not last_error and not period:
            break

    return [], retry_used, last_error


# ── Curated Outlet Fetch ───────────────────────────────────────────────────────

def _load_sources() -> list:
    """Load curated outlet list from sources.json."""
    try:
        with open(_SOURCES_PATH) as f:
            return json.load(f)
    except Exception as e:
        logger.debug('{"event":"sources_load_failed","error":"%s"}', str(e)[:80])
        return []


def _parse_outlet_feed(content: bytes, source_name: str) -> list:
    """Parse an RSS 2.0 or Atom 1.0 feed from a curated outlet.

    Auto-detects format from the root element tag. Atom feeds declare the
    default namespace http://www.w3.org/2005/Atom so the root tag becomes
    {http://www.w3.org/2005/Atom}feed in ElementTree.
    """
    try:
        root = ET.fromstring(content)
    except ET.ParseError as e:
        logger.debug(
            '{"event":"outlet_parse_error","source":"%s","error":"%s"}',
            source_name, str(e)[:80],
        )
        return []

    # Detect Atom by namespace in root tag
    if _ATOM_NS in root.tag:
        return _parse_outlet_atom(root, source_name)
    return _parse_outlet_rss(root, source_name)


def _parse_outlet_rss(root: ET.Element, source_name: str) -> list:
    """Parse RSS 2.0 items from a parsed Element tree root."""
    channel = root.find("channel")
    if channel is None:
        return []

    articles = []
    for item in channel.findall("item"):
        title = (item.findtext("title") or "").strip()
        if not title:
            continue

        link = (item.findtext("link") or "").strip()
        pub_date = (item.findtext("pubDate") or "").strip()
        desc_raw = (item.findtext("description") or "").strip()
        desc = re.sub(r"<[^>]+>", " ", desc_raw).strip()
        desc = re.sub(r"\s{2,}", " ", desc)

        # Strip outlet name suffix from title if present (e.g. "Story - BBC News")
        if source_name and title.endswith(f" - {source_name}"):
            title = title[: -(len(source_name) + 3)].strip()

        image_url = ""
        media_el = item.find("media:content", _NS_MEDIA)
        if media_el is not None:
            image_url = media_el.get("url", "")
        if not image_url:
            media_thumb = item.find("media:thumbnail", _NS_MEDIA)
            if media_thumb is not None:
                image_url = media_thumb.get("url", "")

        articles.append({
            "title": title,
            "link": link,
            "media": source_name,
            "date": pub_date,
            "desc": desc,
            "img": image_url,
        })

    return articles


def _parse_outlet_atom(root: ET.Element, source_name: str) -> list:
    """Parse Atom 1.0 entries from a parsed Element tree root."""
    _e = f"{{{_ATOM_NS}}}"  # namespace prefix shorthand

    articles = []
    for entry in root.findall(f"{_e}entry"):
        title_el = entry.find(f"{_e}title")
        title = (title_el.text or "").strip() if title_el is not None else ""
        if not title:
            continue

        # Prefer rel="alternate" link, fall back to first link
        link_el = (
            entry.find(f"{_e}link[@rel='alternate']")
            or entry.find(f"{_e}link")
        )
        link = (link_el.get("href", "") if link_el is not None else "").strip()

        pub_el = entry.find(f"{_e}published") or entry.find(f"{_e}updated")
        pub = (pub_el.text or "").strip() if pub_el is not None else ""

        summary_el = entry.find(f"{_e}summary") or entry.find(f"{_e}content")
        summary_raw = (summary_el.text or "").strip() if summary_el is not None else ""
        summary = re.sub(r"<[^>]+>", " ", summary_raw).strip()
        summary = re.sub(r"\s{2,}", " ", summary)

        articles.append({
            "title": title,
            "link": link,
            "media": source_name,
            "date": pub,
            "desc": summary,
            "img": "",
        })

    return articles


def _fetch_outlet(source: dict) -> list:
    """Fetch and parse one curated outlet's RSS/Atom feed."""
    import requests as _req

    try:
        resp = _req.get(
            source["url"],
            timeout=6,
            headers={"User-Agent": "Mozilla/5.0 (compatible; newsbot/1.0)"},
        )
        resp.raise_for_status()
        return _parse_outlet_feed(resp.content, source["name"])
    except Exception as e:
        logger.debug(
            '{"event":"outlet_fetch_failed","source":"%s","error":"%s"}',
            source["name"], str(e)[:80],
        )
        return []


def _fetch_all_outlets(query_words: set) -> list:
    """Parallel fetch all curated outlets; return relevance-scored normalized articles.

    Uses ThreadPoolExecutor to fetch all sources concurrently. Articles that
    contain at least one query word are included and scored by match count.
    """
    sources = _load_sources()
    if not sources:
        return []

    raw_articles = []
    with ThreadPoolExecutor(max_workers=min(len(sources), 12)) as executor:
        futures = {executor.submit(_fetch_outlet, s): s for s in sources}
        try:
            for future in as_completed(futures, timeout=10):
                try:
                    raw_articles.extend(future.result())
                except Exception:
                    pass
        except FuturesTimeout:
            # Collect whatever completed before the timeout
            for future in futures:
                if future.done():
                    try:
                        raw_articles.extend(future.result())
                    except Exception:
                        pass

    # Require 2+ query word matches for multi-word queries to reduce false positives.
    # Single-word queries (e.g. "malta") only need 1 match; anything broader needs 2.
    min_required = 1 if len(query_words) == 1 else 2

    result = []
    for raw in raw_articles:
        article = _normalize_article(raw)
        if not article.get("title"):
            continue
        score = _relevance_score(article, query_words)
        if score >= min_required:
            article["_relevance"] = score
            result.append(article)

    return result


# ── Relevance Scoring ──────────────────────────────────────────────────────────

def _query_words(query: str) -> set:
    """Extract significant words from query for relevance scoring.

    Removes stop words and generic news meta-words (news, latest, breaking…)
    that appear in nearly every article and would pollute relevance scoring.
    """
    words = re.sub(r"[^\w\s]", "", query.lower()).split()
    return {
        w for w in words
        if w not in _STOP_WORDS and w not in _NEWS_META and len(w) > 2
    }


def _relevance_score(article: dict, query_words: set) -> int:
    """Count how many query words appear in the article's title and description."""
    if not query_words:
        return 0
    text = f"{article.get('title', '')} {article.get('description', '')}".lower()
    text_words = set(re.sub(r"[^\w\s]", "", text).split())
    return len(query_words & text_words)


# ── Normalization ──────────────────────────────────────────────────────────────

def _normalize_article(raw: dict) -> dict:
    """Normalize a raw RSS/Atom article dict into the canonical article shape."""
    import dateparser

    title = (raw.get("title") or "").strip()
    source = (raw.get("media") or "").strip()
    date_str = (raw.get("date") or "").strip()
    description = (raw.get("desc") or "").strip()
    url = (raw.get("link") or "").strip()
    image_url = (raw.get("img") or "").strip()

    # Remove source-name prefix from description if present
    if description and source and description.lower().startswith(source.lower()):
        description = description[len(source):].lstrip(" -:").strip()

    # Normalize date to ISO 8601
    published_at = ""
    if date_str:
        try:
            parsed = dateparser.parse(
                date_str,
                settings={"RETURN_AS_TIMEZONE_AWARE": False, "PREFER_DAY_OF_MONTH": "first"},
            )
            if parsed:
                published_at = parsed.strftime("%Y-%m-%dT%H:%M:%S")
        except Exception:
            pass

    return {
        "title": title,
        "source": source,
        "date": date_str,
        "published_at": published_at,
        "description": description,
        "url": url,
        "image_url": image_url,
        "also_reported_by": [],
    }


# ── Deduplication ─────────────────────────────────────────────────────────────

def _is_real_url(url: str) -> bool:
    """Return True if the URL points to a real publisher (not a Google redirect)."""
    return bool(url) and "news.google.com" not in url


def _normalize_url(url: str) -> str:
    """Strip query params and fragments for URL comparison."""
    try:
        p = urlparse(url)
        return urlunparse((p.scheme, p.netloc, p.path, "", "", ""))
    except Exception:
        return url


def _dedup_by_url(articles: list) -> list:
    """Remove exact-same articles (same normalized URL)."""
    seen_urls = {}
    for article in articles:
        key = _normalize_url(article["url"])
        if not key or key in seen_urls:
            continue
        seen_urls[key] = article
    return list(seen_urls.values())


def _title_words(title: str) -> set:
    """Extract normalized significant words from a title."""
    words = re.sub(r"[^\w\s]", "", title.lower()).split()
    return {w for w in words if w not in _STOP_WORDS and len(w) > 2}


def _jaccard(a: set, b: set) -> float:
    if not a or not b:
        return 0.0
    union = a | b
    return len(a & b) / len(union)


def _dedup_by_title(articles: list) -> list:
    """
    Merge articles with >50% Jaccard title-word similarity.
    Keeps the best article (has image > longer description > earlier position).
    Attaches cross-referenced source names to the kept article.
    Preserves the highest _relevance score across merged duplicates.
    """
    used = set()
    deduplicated = []

    for i, article in enumerate(articles):
        if i in used:
            continue

        words_i = _title_words(article["title"])
        also_reported_by = list(article.get("also_reported_by") or [])
        best_relevance = article.get("_relevance", 0)

        for j in range(i + 1, len(articles)):
            if j in used:
                continue
            words_j = _title_words(articles[j]["title"])
            if _jaccard(words_i, words_j) > 0.5:
                used.add(j)
                other = articles[j]
                other_source = other.get("source", "")
                if other_source and other_source != article.get("source", ""):
                    also_reported_by.append(other_source)

                # Carry forward the best relevance score
                best_relevance = max(best_relevance, other.get("_relevance", 0))

                # Prefer the better article:
                # real URL > has image > longer description > earlier position
                current_score = (
                    _is_real_url(article.get("url", "")),
                    bool(article.get("image_url")),
                    len(article.get("description", "")),
                )
                other_score = (
                    _is_real_url(other.get("url", "")),
                    bool(other.get("image_url")),
                    len(other.get("description", "")),
                )
                if other_score > current_score:
                    article = {**other, "also_reported_by": []}
                    if article.get("source") != articles[i].get("source"):
                        also_reported_by.append(articles[i].get("source", ""))
                    words_i = _title_words(article["title"])

        article = {**article, "also_reported_by": also_reported_by, "_relevance": best_relevance}
        deduplicated.append(article)

    return deduplicated


# ── Reranking ─────────────────────────────────────────────────────────────────

def _rerank(articles: list) -> list:
    """
    Rank by:
    - Primary:   query relevance score (keyword match count in title + description)
    - Secondary: multi-source cross-coverage (bigger stories have more outlets)
    - Tertiary:  freshness (published_at ISO string; lexicographic order is safe)
    """
    def _score(article):
        relevance = article.get("_relevance", 0)
        real_url = _is_real_url(article.get("url", ""))
        source_count = len(article.get("also_reported_by") or [])
        published_at = article.get("published_at") or ""
        return (relevance, real_url, source_count, published_at)

    return sorted(articles, key=_score, reverse=True)


# ── Utilities ─────────────────────────────────────────────────────────────────

def _classify_error(e: Exception) -> str:
    """Classify a fetch exception for structured logging."""
    msg = str(e).lower()
    if "timeout" in msg or "timed out" in msg:
        return "timeout"
    if "connection" in msg or "network" in msg or "unreachable" in msg:
        return "network"
    if "403" in msg or "401" in msg or "blocked" in msg:
        return "blocked"
    return "fetch_error"
