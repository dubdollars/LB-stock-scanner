"""
Social-momentum scanner (Twitter / Reddit / StockTwits mode).

Data sources:
  * Apewisdom — free public API aggregating ticker mentions from Reddit
    (WSB, stocks, investing, pennystocks, options) and Twitter sources.
    Endpoint: https://apewisdom.io/api/v1.0/filter/all-stocks/page/<N>
    50 results per page, no auth required, updated hourly.

  * StockTwits — free public per-ticker stream API.
    Endpoint: https://api.stocktwits.com/api/2/streams/symbol/<TICKER>.json
    Last ~30 messages with Bullish / Bearish sentiment tags, no auth.

This module is the DATA LAYER only. UI, enrichment, and integration with
the existing scan pipeline live in app.py + scanner/core.py.

Safety contract:
  * Network failures must never crash the caller. Each fetch returns an
    empty / sentinel value on failure and logs to SOURCE_STATUS.
  * All cache reads tolerate corrupt files (return empty).
  * Cache writes are best-effort with atomic-rename so a partial write
    can never corrupt a good snapshot.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Callable, Optional

import requests
import pandas as pd


# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

_REPO_ROOT = Path(__file__).resolve().parent.parent
SOCIAL_CACHE_DIR = _REPO_ROOT / "data" / "cache" / "social"


def _ensure_cache_dir():
    """Create the snapshot directory on demand. Safe to call repeatedly."""
    try:
        SOCIAL_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Settings + result types
# ---------------------------------------------------------------------------

@dataclass
class SocialScanSettings:
    """Configuration for one social-scan run."""
    # How many top-ranked tickers to pull from Apewisdom (page size is 50,
    # so 100 = 2 pages, 200 = 4 pages, etc.)
    top_n: int = 100
    # Tradeable guardrails — applied AFTER fetching to drop OTC junk and
    # ultra-cheap penny stocks even if they're trending socially.
    min_price: float = 2.50
    min_avg_volume: int = 100_000
    # User-supplied subset of tickers (for testing); None = full Apewisdom pull
    ticker_subset: Optional[list[str]] = None


@dataclass
class SocialScanResult:
    """Everything the Twitter dashboard needs from one social-scan run."""
    timestamp: str
    settings: dict
    universe_size: int          # raw Apewisdom count before guardrails
    after_guardrails: int       # surviving after price/volume/listing checks
    candidates: list[dict] = field(default_factory=list)
    diagnostics: dict = field(default_factory=dict)
    snapshot_path: Optional[str] = None

    def to_dict(self) -> dict:
        return asdict(self)


# Mirrors the SOURCE_STATUS pattern in scanner/core.py — lets the UI show
# which sources succeeded/failed and surface errors helpfully.
SOURCE_STATUS: dict[str, dict] = {}


def _set_source(name: str, ok: bool, count: int = 0, error: Optional[str] = None):
    SOURCE_STATUS[name] = {"ok": ok, "count": count, "error": error}


# ---------------------------------------------------------------------------
# Progress callback type (same shape as scanner/core.py uses)
# ---------------------------------------------------------------------------

ProgressCallback = Optional[Callable[[str, int, int, str], None]]


# ---------------------------------------------------------------------------
# Apewisdom fetcher
# ---------------------------------------------------------------------------

APEWISDOM_BASE = "https://apewisdom.io/api/v1.0/filter/all-stocks/page/{page}"
_APEWISDOM_HEADERS = {
    # Apewisdom doesn't enforce a UA but some CDNs reject default urllib UAs
    "User-Agent": "Mozilla/5.0 (compatible; LegacyBrandsScanner/1.0)",
    "Accept": "application/json",
}

# StockTwits aggressively blocks non-browser User-Agents on their public API
# (started tightening in 2024). Sending a realistic browser UA + the headers
# a real browser sends gets through in most cases.
_STOCKTWITS_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip, deflate, br",
    "Referer": "https://stocktwits.com/",
    "Origin": "https://stocktwits.com",
    "Sec-Fetch-Site": "same-site",
    "Sec-Fetch-Mode": "cors",
    "Sec-Fetch-Dest": "empty",
}

# Back-compat alias for any callers still using the old name
_REQUEST_HEADERS = _APEWISDOM_HEADERS


def fetch_apewisdom_top(limit: int = 100, timeout: int = 15) -> list[dict]:
    """Fetch top-`limit` ranked tickers from Apewisdom.

    Returns a list of dicts with at least these keys (the names mirror the
    Apewisdom API response):
        ticker, name, rank, mentions, mentions_24h_ago,
        rank_24h_ago, upvotes, sentiment, sentiment_score

    On any failure (network error, JSON parse error, missing fields) this
    returns an empty list rather than raising — the caller treats that as
    "no social data available right now" and surfaces it via SOURCE_STATUS.
    """
    out: list[dict] = []
    pages_needed = max(1, (limit + 49) // 50)  # 50 results per page

    for page in range(1, pages_needed + 1):
        url = APEWISDOM_BASE.format(page=page)
        try:
            r = requests.get(url, headers=_REQUEST_HEADERS, timeout=timeout)
            r.raise_for_status()
            data = r.json()
        except Exception as exc:
            _set_source("Apewisdom", ok=False, count=len(out), error=f"page {page}: {exc}")
            return out  # return whatever we got so far; don't lose page 1 data
        results = data.get("results") or []
        if not results:
            break
        out.extend(results)
        if len(out) >= limit:
            out = out[:limit]
            break

    _set_source("Apewisdom", ok=True, count=len(out))
    return out


# ---------------------------------------------------------------------------
# StockTwits per-ticker stream fetcher (used by the SOCIAL deep-dive tab)
# ---------------------------------------------------------------------------

STOCKTWITS_URL = "https://api.stocktwits.com/api/2/streams/symbol/{ticker}.json"


def fetch_stocktwits_stream(ticker: str, timeout: int = 10) -> dict:
    """Fetch the most recent StockTwits messages for a ticker.

    Returns a dict with keys:
        messages: list[dict] — last ~30 messages, each with:
            id, body, created_at, user (dict with username, followers, etc.),
            entities.sentiment (Bullish / Bearish / None — Bullish/Bearish only),
            likes (dict with total)
        bullish_count, bearish_count, neutral_count: int — tallies
        bullish_pct: float — bullish / (bullish + bearish) * 100, 0-100;
            None if both counts are zero
        error: str — empty on success, message on failure

    Never raises — failures return an empty messages list and a populated
    error field so the UI can show "social data temporarily unavailable".
    """
    ticker = (ticker or "").upper().strip()
    if not ticker:
        return {"messages": [], "bullish_count": 0, "bearish_count": 0,
                "neutral_count": 0, "bullish_pct": None, "error": "empty ticker"}

    url = STOCKTWITS_URL.format(ticker=ticker)
    try:
        r = requests.get(url, headers=_STOCKTWITS_HEADERS, timeout=timeout)
        r.raise_for_status()
        data = r.json()
    except Exception as exc:
        msg = str(exc)
        # StockTwits 403s are common — they aggressively block server-side
        # API access. Surface a friendlier message than the raw HTTPError.
        if "403" in msg:
            msg = (
                "StockTwits public API blocked this request (HTTP 403). "
                "Their anti-bot filter has been tightening since 2024. "
                "Aggregated sentiment from Apewisdom + the Recent News section "
                "below still work. Per-message StockTwits content needs an API "
                "key or proxy to access reliably."
            )
        return {"messages": [], "bullish_count": 0, "bearish_count": 0,
                "neutral_count": 0, "bullish_pct": None, "error": msg}

    raw_messages = data.get("messages") or []
    bull = bear = neut = 0
    cleaned = []
    for m in raw_messages:
        sentiment = None
        ent = m.get("entities") or {}
        sent_obj = ent.get("sentiment")
        if isinstance(sent_obj, dict):
            sentiment = sent_obj.get("basic")  # "Bullish" / "Bearish" / None
        likes_obj = m.get("likes") or {}
        likes_total = likes_obj.get("total", 0) if isinstance(likes_obj, dict) else 0
        user_obj = m.get("user") or {}
        cleaned.append({
            "id": m.get("id"),
            "body": (m.get("body") or "")[:600],
            "created_at": m.get("created_at"),
            "sentiment": sentiment,
            "likes": likes_total,
            "username": user_obj.get("username") if isinstance(user_obj, dict) else None,
            "followers": user_obj.get("followers") if isinstance(user_obj, dict) else None,
        })
        if sentiment == "Bullish":
            bull += 1
        elif sentiment == "Bearish":
            bear += 1
        else:
            neut += 1
    bullish_pct = None
    denom = bull + bear
    if denom > 0:
        bullish_pct = round(bull / denom * 100, 1)

    return {
        "messages": cleaned,
        "bullish_count": bull,
        "bearish_count": bear,
        "neutral_count": neut,
        "bullish_pct": bullish_pct,
        "error": "",
    }


# ---------------------------------------------------------------------------
# Reddit fetcher — uses Reddit's PUBLIC JSON endpoint (no OAuth, no account).
# ---------------------------------------------------------------------------
#
# Reddit exposes every page on the site as JSON simply by appending `.json`
# to the URL. The /r/<sub>/search.json endpoint returns the same data PRAW
# would give us — title, selftext, author, score, num_comments, created_utc,
# permalink, flair — with no app registration, no client_id/secret, no
# captcha gauntlet. The only requirement is sending a custom User-Agent
# header (Reddit blocks default UAs like python-requests/2.x and curl/7.x).
#
# Rate limit: ~60 requests per minute for unauthenticated traffic, which is
# plenty for a personal scanner. We add a small inter-request sleep and
# detect 429s to stay polite.

# Subreddits we search for ticker mentions, ordered roughly by activity.
_REDDIT_SUBREDDITS = [
    "wallstreetbets",
    "stocks",
    "investing",
    "pennystocks",
    "options",
    "smallstreetbets",
    "StockMarket",
]

# Custom UA is mandatory — Reddit returns 429/403 for default UAs.
_REDDIT_UA = "LegacyBrandsScanner/1.0 (personal read-only stock research)"

# Inter-request delay (seconds) to stay well under 60 req/min unauth limit.
_REDDIT_REQUEST_DELAY = 0.7


def _reddit_search(
    sub: str, query: str, time_filter: str, limit: int
) -> tuple[list[dict], str]:
    """Hit Reddit's public search.json endpoint for one subreddit + query.

    Returns (raw_children_list, error_string). children_list is the list of
    `data.children[].data` dicts straight from Reddit's JSON response; the
    caller is responsible for relevance-filtering and field extraction.
    """
    url = f"https://www.reddit.com/r/{sub}/search.json"
    params = {
        "q": query,
        "restrict_sr": "1",  # only search within this subreddit
        "sort": "new",
        "t": time_filter,
        "limit": str(limit),
        "raw_json": "1",  # strip HTML entity encoding from text fields
    }
    headers = {"User-Agent": _REDDIT_UA}
    try:
        resp = requests.get(url, params=params, headers=headers, timeout=10)
    except Exception as exc:
        return [], f"network error: {exc}"

    if resp.status_code == 429:
        return [], "rate limited (429)"
    if resp.status_code == 403:
        return [], "forbidden (403) — subreddit may be private/quarantined"
    if resp.status_code != 200:
        return [], f"http {resp.status_code}"

    try:
        payload = resp.json()
    except Exception as exc:
        return [], f"bad JSON: {exc}"

    data = payload.get("data") if isinstance(payload, dict) else None
    children = data.get("children") if isinstance(data, dict) else None
    if not isinstance(children, list):
        return [], ""

    out = []
    for c in children:
        if not isinstance(c, dict):
            continue
        d = c.get("data")
        if isinstance(d, dict):
            out.append(d)
    return out, ""


def fetch_reddit_mentions(
    ticker: str, limit_per_sub: int = 25, time_filter: str = "day"
) -> dict:
    """Search a curated set of finance subreddits for recent posts mentioning a ticker.

    Uses Reddit's public JSON endpoint — no OAuth, no API credentials, no
    Reddit account required. The only requirement is a custom User-Agent
    header, which is baked in.

    Returns a dict with keys:
        posts: list[dict] — each with subreddit, title, author, score,
            num_comments, created_utc, url, permalink, body (selftext), flair
        total_count: int — total posts returned across subs (pre-dedup)
        subreddit_counts: dict[str, int] — mentions per sub
        error: str — empty on success, message on failure

    `time_filter` accepts Reddit's standard values: hour, day, week, month, year, all.
    """
    ticker = (ticker or "").upper().strip()
    if not ticker:
        return {"posts": [], "total_count": 0, "subreddit_counts": {}, "error": "empty ticker"}

    # Search both "$TICKER" cashtag and bare "TICKER" — covers more posts.
    # Cashtag is more specific (fewer false positives), bare is more inclusive.
    queries = [f"${ticker}", ticker]

    seen_ids: set[str] = set()
    posts: list[dict] = []
    sub_counts: dict[str, int] = {}
    last_error: str = ""

    for sub_name in _REDDIT_SUBREDDITS:
        new_in_sub = 0
        for query in queries:
            children, err = _reddit_search(
                sub_name, query, time_filter, limit_per_sub
            )
            if err:
                last_error = err
                # 429 means we should back off and stop hammering — likely
                # IP-throttled, no point burning more requests in this fetch.
                if "rate limited" in err:
                    time.sleep(2.0)
                    break
                # Other errors on this sub+query: just try the next query.
                continue

            for d in children:
                sid = d.get("id")
                if not sid or sid in seen_ids:
                    continue
                title = d.get("title") or ""
                selftext = d.get("selftext") or ""
                # Basic relevance check: ticker must appear in title or body
                # as a standalone token (no partial matches like CAT in
                # CATastrophe). Same rule the PRAW version used.
                text = (title + " " + selftext).upper()
                if (
                    f"${ticker}" not in text
                    and f" {ticker} " not in f" {text} "
                    and not text.startswith(ticker + " ")
                    and not text.endswith(" " + ticker)
                ):
                    continue
                seen_ids.add(sid)
                new_in_sub += 1
                author = d.get("author") or "[deleted]"
                permalink = d.get("permalink") or ""
                posts.append({
                    "subreddit": sub_name,
                    "title": title,
                    "author": str(author),
                    "score": int(d.get("score") or 0),
                    "num_comments": int(d.get("num_comments") or 0),
                    "created_utc": float(d.get("created_utc") or 0),
                    "url": d.get("url") or "",
                    "permalink": f"https://reddit.com{permalink}" if permalink else "",
                    "body": selftext[:800],
                    "flair": d.get("link_flair_text") or "",
                })

            # Be polite — small delay between requests.
            time.sleep(_REDDIT_REQUEST_DELAY)

            if new_in_sub > 0:
                # Found hits with cashtag query, no need to try the bare-ticker fallback.
                break

        sub_counts[sub_name] = sum(1 for p in posts if p["subreddit"] == sub_name)

    # Only surface an error if we got literally zero results AND at least one
    # request failed. Empty-but-no-error means the ticker is genuinely not
    # being talked about right now, which is a valid result.
    final_error = ""
    if not posts and last_error:
        final_error = f"All Reddit fetches failed (last: {last_error})"

    return {
        "posts": posts,
        "total_count": len(posts),
        "subreddit_counts": sub_counts,
        "error": final_error,
    }


# ---------------------------------------------------------------------------
# Snapshot persistence (per-refresh JSON dumps under data/cache/social/)
# ---------------------------------------------------------------------------

def _snapshot_path(timestamp_iso: str) -> Path:
    # Convert ISO timestamp to a filename-safe form
    stamp = timestamp_iso.replace(":", "").replace("-", "").replace("T", "_")
    # Drop microseconds + timezone suffix
    stamp = stamp.split(".")[0].rstrip("Z").rstrip("+0000")
    return SOCIAL_CACHE_DIR / f"snapshot_{stamp}.json"


def save_social_snapshot(result: SocialScanResult) -> Optional[Path]:
    """Persist a SocialScanResult to disk. Returns the path on success."""
    _ensure_cache_dir()
    try:
        path = _snapshot_path(result.timestamp)
        tmp = path.with_suffix(".json.tmp")
        with tmp.open("w", encoding="utf-8") as f:
            json.dump(result.to_dict(), f, indent=2, default=str)
        tmp.replace(path)
        return path
    except Exception as exc:
        logging.warning(f"social snapshot save failed: {exc}")
        return None


def load_social_snapshots(days_back: int = 30) -> list[dict]:
    """Load all snapshots from the last `days_back` days, sorted oldest-first.

    Each returned item is a dict (the JSON-serialised SocialScanResult).
    Snapshots that can't be parsed are silently skipped so one corrupt file
    can't poison the entire history read.
    """
    _ensure_cache_dir()
    cutoff_dt = datetime.now(timezone.utc) - timedelta(days=days_back)
    out: list[dict] = []
    for path in sorted(SOCIAL_CACHE_DIR.glob("snapshot_*.json")):
        try:
            with path.open("r", encoding="utf-8") as f:
                data = json.load(f)
            ts = data.get("timestamp")
            if ts:
                ts_dt = _parse_iso(ts)
                if ts_dt is not None and ts_dt < cutoff_dt:
                    continue
            out.append(data)
        except Exception:
            continue
    return out


def load_latest_social_snapshot() -> Optional[dict]:
    """Read the most recent snapshot. Returns None if no snapshots exist."""
    snaps = load_social_snapshots(days_back=365 * 5)  # essentially "all"
    return snaps[-1] if snaps else None


def _parse_iso(s: str) -> Optional[datetime]:
    if not s or not isinstance(s, str):
        return None
    try:
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Spike ratio computation
# ---------------------------------------------------------------------------

def compute_spike_ratios(
    ticker: str,
    current_mentions: int,
    snapshots: list[dict],
    current_timestamp: Optional[str] = None,
) -> dict:
    """For one ticker, compute spike ratios from snapshot history.

    Ratios:
        spike_24h: current vs the same ticker's mention count ~24h ago
        spike_3d:  current vs avg of the same ticker's mention counts
                   from snapshots between 3 days ago and 1 day ago
        spike_7d:  current vs avg over 7-1 days ago
        spike_30d: current vs avg over 30-1 days ago

    Missing data (no snapshot in that window) returns None for that ratio.
    """
    now = _parse_iso(current_timestamp) if current_timestamp else datetime.now(timezone.utc)
    if now is None:
        now = datetime.now(timezone.utc)

    # Bucket past mention counts by age in days
    by_age_days: list[tuple[float, int]] = []
    for snap in snapshots:
        ts = snap.get("timestamp")
        ts_dt = _parse_iso(ts) if ts else None
        if ts_dt is None:
            continue
        age_days = (now - ts_dt).total_seconds() / 86400.0
        if age_days < 0.5:  # skip snapshots within last 12 hours (essentially "now")
            continue
        # Find this ticker's mention count in the snapshot
        for cand in snap.get("candidates", []):
            if (cand.get("Ticker") or "").upper() == ticker.upper():
                m = cand.get("Mentions")
                if isinstance(m, (int, float)) and m >= 0:
                    by_age_days.append((age_days, int(m)))
                break

    def _window_avg(min_d: float, max_d: float) -> Optional[float]:
        vals = [m for age, m in by_age_days if min_d <= age <= max_d]
        if not vals:
            return None
        return sum(vals) / len(vals)

    def _ratio(window_avg: Optional[float]) -> Optional[float]:
        if window_avg is None or window_avg <= 0:
            return None
        return round(current_mentions / window_avg, 2)

    # 24h ratio uses the closest snapshot to ~24h ago (within 18-36h)
    avg_24h = _window_avg(0.75, 1.5)
    avg_3d = _window_avg(1.0, 3.0)
    avg_7d = _window_avg(1.0, 7.0)
    avg_30d = _window_avg(1.0, 30.0)

    return {
        "spike_24h": _ratio(avg_24h),
        "spike_3d": _ratio(avg_3d),
        "spike_7d": _ratio(avg_7d),
        "spike_30d": _ratio(avg_30d),
    }


# ---------------------------------------------------------------------------
# Apewisdom field normalisation
# ---------------------------------------------------------------------------

def _classify_sentiment(score: Optional[float]) -> str:
    """Apewisdom sentiment_score is 0-100. Apply user-chosen thresholds."""
    if score is None:
        return "Unknown"
    try:
        s = float(score)
    except Exception:
        return "Unknown"
    if s >= 65:
        return "Bullish"
    if s < 40:
        return "Bearish"
    return "Neutral"


def normalise_apewisdom_entry(entry: dict) -> dict:
    """Convert one Apewisdom result dict to our canonical schema.

    Output keys (used downstream by enrichment + table rendering):
        Ticker, Name, Rank, Mentions, Mentions_Prev_24h,
        Rank_Prev_24h, Rank_Delta, Sentiment_Score, Sentiment_Label,
        Spike_24h_raw  (current / prev_24h, computed from Apewisdom\\'s own fields)
    """
    ticker = (entry.get("ticker") or "").upper().strip()
    rank = entry.get("rank")
    mentions = entry.get("mentions") or 0
    mentions_prev = entry.get("mentions_24h_ago") or 0
    rank_prev = entry.get("rank_24h_ago")
    # Apewisdom\'s field names have drifted over time. Try the most specific
    # field first ("sentiment_score" — explicit numeric), then fall back to
    # "sentiment" which can be EITHER a number OR a string label.
    sentiment_score = None
    sentiment_label = None
    raw_score = entry.get("sentiment_score")
    raw_sent = entry.get("sentiment")
    # Case 1: explicit numeric sentiment_score
    if raw_score is not None:
        try:
            sentiment_score = float(raw_score)
        except Exception:
            sentiment_score = None
    # Case 2: bare sentiment is a number
    if sentiment_score is None and raw_sent is not None:
        try:
            sentiment_score = float(raw_sent)
        except (TypeError, ValueError):
            # Case 3: bare sentiment is a string label like "Bullish" or "Bearish"
            if isinstance(raw_sent, str):
                norm = raw_sent.strip().lower()
                if norm in ("bullish", "positive"):
                    sentiment_label = "Bullish"
                    sentiment_score = 75.0  # synthetic mid-bullish for display
                elif norm in ("bearish", "negative"):
                    sentiment_label = "Bearish"
                    sentiment_score = 25.0
                elif norm in ("neutral", "mixed"):
                    sentiment_label = "Neutral"
                    sentiment_score = 50.0
    # If we ended up with a score but no label, classify from thresholds
    if sentiment_label is None:
        sentiment_label = _classify_sentiment(sentiment_score)

    spike_apewisdom = None
    if mentions_prev and mentions_prev > 0:
        try:
            spike_apewisdom = round(mentions / mentions_prev, 2)
        except Exception:
            spike_apewisdom = None

    rank_delta = None
    try:
        if rank is not None and rank_prev is not None:
            rank_delta = int(rank_prev) - int(rank)  # positive = climbing the ranks
    except Exception:
        rank_delta = None

    return {
        "Ticker": ticker,
        "Name": entry.get("name") or "",
        "Rank": int(rank) if rank is not None else None,
        "Mentions": int(mentions),
        "Mentions_Prev_24h": int(mentions_prev) if mentions_prev else 0,
        "Rank_Prev_24h": int(rank_prev) if rank_prev is not None else None,
        "Rank_Delta": rank_delta,
        "Sentiment_Score": sentiment_score,
        "Sentiment_Label": sentiment_label,
        "Spike_24h_apewisdom": spike_apewisdom,
    }


# ---------------------------------------------------------------------------
# Top-level orchestrator (skeleton — full enrichment lands in Phase 3)
# ---------------------------------------------------------------------------

def _enrich_one_ticker(ticker: str, all_fund_cache: dict, all_rs_map: dict) -> dict:
    """For one ticker, load cached price history + fundamentals and compute
    the technical / fundamental columns used by the Twitter table.

    Returns a dict of enrichment fields. Missing values populate as None or
    "N/A" so the downstream table can render them cleanly.

    All failures are caught — bad data for one ticker never blocks the others.
    """
    import yfinance as yf
    # Lazy imports to avoid pulling pandas/numpy at module load if we never
    # need the enrichment path.
    from scanner.core import (
        clean_history, calc_rsi, _next_earnings_date, days_until,
        format_mktcap, SECTOR_ABBREV, safe_get_info,
    )
    from scanner.setups import (
        find_pivot_point, detect_vcp_contractions, vcp_quality_score,
        detect_extension_from_50ma, detect_power_play, detect_breakout_today,
        calc_atr, calculate_smart_stop,
    )
    from scanner.data_cache import (
        load_price_history, save_price_history,
        get_cached_fundamentals, need_refresh, update_fundamentals_entry,
    )

    enrich = {
        "Price": None, "Avg_Volume": None, "RS": None, "RSI": None,
        "Pct_Above_50MA": None, "VCP_Quality": None, "VCP_Count": None,
        "Buy_Trigger": None, "Stop": None, "Risk_Pct": None,
        "Power_Play": False, "Breakout_Today": False, "Vol_vs_Avg": None,
        "RS_NH": "-",
        "Signals": [], "Warnings": [],
        "Fundamentals": "N/A", "Sector": "N/A", "Industry": "N/A",
        "Market_Cap": "N/A", "PEG": None, "Held_Pct_Inst": None,
        "Earnings_Date": None, "Days_To_Earnings": None,
        "passes_guardrails": False, "reason_dropped": None,
    }

    # ---- Step 1: Price history (cache-first, live-fetch fallback) ----
    df = load_price_history(ticker)
    if df is None or df.empty or len(df) < 60:
        # Live-fetch for any ticker that's not in the cache (Apewisdom often
        # surfaces stuff outside the long-scan universe — meme stocks, etc.)
        try:
            start = (datetime.now() - timedelta(days=540)).strftime("%Y-%m-%d")
            fresh = yf.Ticker(ticker).history(start=start, interval="1d")
            if isinstance(fresh.columns, pd.MultiIndex):
                fresh.columns = fresh.columns.get_level_values(0)
            fresh = clean_history(fresh)
            if fresh is not None and len(fresh) >= 60:
                df = fresh
                save_price_history(ticker, df)  # cache for next time
        except Exception:
            df = None

    if df is None or df.empty or len(df) < 60:
        enrich["reason_dropped"] = "insufficient price history"
        return enrich

    try:
        close = float(df["Close"].iloc[-1])
        avg_vol = float(df["Volume"].rolling(50).mean().iloc[-1])
        enrich["Price"] = round(close, 2)
        enrich["Avg_Volume"] = int(avg_vol) if avg_vol > 0 else 0
    except Exception:
        enrich["reason_dropped"] = "price computation error"
        return enrich

    # ---- Step 2: Tradeable guardrails ----
    if enrich["Price"] is None or enrich["Price"] < 2.50:
        enrich["reason_dropped"] = f"price ${enrich['Price']:.2f} below $2.50 guardrail"
        return enrich
    if enrich["Avg_Volume"] is None or enrich["Avg_Volume"] < 100_000:
        enrich["reason_dropped"] = f"avg volume {enrich['Avg_Volume']:,} below 100K guardrail"
        return enrich

    enrich["passes_guardrails"] = True

    # ---- Step 3: Technical indicators ----
    try:
        sma_50 = float(df["Close"].rolling(50).mean().iloc[-1])
        enrich["Pct_Above_50MA"] = round((close - sma_50) / sma_50 * 100, 2) if sma_50 > 0 else None
        enrich["RSI"] = calc_rsi(df["Close"])
    except Exception:
        pass

    # RS rating from latest long-scan rs_map (universe-relative; can\'t recompute here)
    if all_rs_map and ticker in all_rs_map:
        enrich["RS"] = int(all_rs_map[ticker])

    # VCP + pivot + power play + breakout + signals
    try:
        contractions = detect_vcp_contractions(df, lookback=80)
        vcp_q = vcp_quality_score(contractions)
        enrich["VCP_Quality"] = vcp_q.get("quality", "None")
        enrich["VCP_Count"] = vcp_q.get("count", 0)
    except Exception:
        pass

    try:
        pivot_info = find_pivot_point(df, lookback=60)
        if pivot_info is not None:
            pivot_price = float(pivot_info[0])
            atr_20 = calc_atr(df, period=20)
            stop_info = calculate_smart_stop(df, pivot_price, atr_20, sma_50=float(sma_50))
            buy_trigger = round(pivot_price * 1.005, 2)
            natural_stop = stop_info.get("stop")
            enrich["Buy_Trigger"] = buy_trigger
            enrich["Stop"] = round(natural_stop, 2) if natural_stop else None
            if natural_stop and buy_trigger > natural_stop:
                enrich["Risk_Pct"] = round((buy_trigger - natural_stop) / buy_trigger * 100, 2)
    except Exception:
        pass

    try:
        pp = detect_power_play(df)
        enrich["Power_Play"] = bool(pp.get("is_power_play"))
    except Exception:
        pass

    try:
        avg_vol_50 = float(df["Volume"].rolling(50).mean().iloc[-1])
        bo = detect_breakout_today(df, enrich.get("Buy_Trigger") or 0, avg_vol_50)
        enrich["Breakout_Today"] = bool(bo.get("is_breakout"))
        enrich["Vol_vs_Avg"] = round(bo.get("vol_ratio", 0), 2)
    except Exception:
        pass

    # Signals + warnings (subset — full set lives in scanner/core._build_signals_warnings)
    signals = []
    warnings = []
    if enrich["Breakout_Today"]:
        signals.append("BO TODAY")
    if enrich["Power_Play"]:
        signals.append("POWER PLAY")
    if enrich["VCP_Quality"] == "A":
        signals.append("VCP-A")
    elif enrich["VCP_Quality"] == "B":
        signals.append("VCP-B")
    pct50 = enrich.get("Pct_Above_50MA") or 0
    if pct50 > 20:
        warnings.append("EXT")
    if enrich.get("Risk_Pct") and enrich["Risk_Pct"] > 7:
        warnings.append("WIDE")
    enrich["Signals"] = signals
    enrich["Warnings"] = warnings

    # ---- Step 4: Fundamentals (from cache, never live-fetched here) ----
    entry = get_cached_fundamentals(ticker, all_fund_cache)
    if entry:
        enrich["Sector"] = entry.get("sector_short", entry.get("sector", "N/A")) or "N/A"
        enrich["Industry"] = entry.get("industry", "N/A") or "N/A"
        enrich["Market_Cap"] = entry.get("mktcap_display", "N/A") or "N/A"
        enrich["PEG"] = entry.get("peg")
        enrich["Held_Pct_Inst"] = entry.get("held_pct_institutions")
        enrich["Fundamentals"] = entry.get("fund_display") or "N/A"
        enrich["Earnings_Date"] = entry.get("earnings_date")
        if entry.get("earnings_date"):
            enrich["Days_To_Earnings"] = days_until(entry["earnings_date"])

    return enrich


def run_social_scan(
    settings: SocialScanSettings,
    progress: ProgressCallback = None,
) -> SocialScanResult:
    """End-to-end social scan with full enrichment.

    Pipeline:
      1. Fetch top-N from Apewisdom
      2. Normalise into our schema
      3. Compute spike ratios from snapshot history
      4. For each ticker, enrich with technicals + fundamentals using the
         existing price + fundamentals caches (live-fetch price for tickers
         not yet cached)
      5. Apply tradeable guardrails (price ≥ $2.50, avg vol ≥ 100K)
      6. Save snapshot, return result
    """
    SOURCE_STATUS.clear()
    timestamp = datetime.now(timezone.utc).isoformat(timespec="seconds")

    # Lazy import of pandas + scan-time data caches to keep this module light
    from scanner.data_cache import load_fundamentals_cache
    from scanner.cache import load_latest_scan

    if progress:
        progress("apewisdom", 0, 1, f"Fetching top {settings.top_n} from Apewisdom...")
    raw = fetch_apewisdom_top(limit=settings.top_n)
    if settings.ticker_subset is not None:
        wanted = {t.upper().strip() for t in settings.ticker_subset}
        raw = [e for e in raw if (e.get("ticker") or "").upper() in wanted]
    if progress:
        progress("apewisdom", 1, 1, f"Got {len(raw)} entries from Apewisdom")

    universe_size = len(raw)
    candidates = [normalise_apewisdom_entry(e) for e in raw]

    # ---- Load cached resources for enrichment ----
    fund_cache = load_fundamentals_cache()
    # RS rating comes from the latest long scan\'s rs_map (universe-relative,
    # can\'t recompute for arbitrary tickers).
    latest_long = load_latest_scan()
    rs_map: dict[str, int] = {}
    if latest_long:
        for c in latest_long.get("candidates", []):
            t = c.get("Ticker")
            rs = c.get("RS")
            if t and rs is not None:
                rs_map[t] = int(rs)

    # ---- Per-ticker enrichment + guardrail filtering ----
    enriched: list[dict] = []
    dropped_count = 0
    for i, c in enumerate(candidates):
        if progress and (i % 10 == 0 or i == len(candidates) - 1):
            progress("enrich", i, len(candidates), f"Enriching {c['Ticker']} ({i+1}/{len(candidates)})...")
        enrich = _enrich_one_ticker(c["Ticker"], fund_cache, rs_map)
        if not enrich.get("passes_guardrails"):
            dropped_count += 1
            continue
        merged = {**c, **enrich}
        enriched.append(merged)
    if progress:
        progress("enrich", len(candidates), len(candidates),
                 f"Enrichment complete: {len(enriched)} pass guardrails, {dropped_count} dropped")

    # ---- Spike ratios from snapshot history ----
    if progress:
        progress("spike", 0, 1, "Computing spike ratios from snapshot history...")
    history = load_social_snapshots(days_back=30)
    for c in enriched:
        ratios = compute_spike_ratios(
            c["Ticker"], c["Mentions"], history,
            current_timestamp=timestamp,
        )
        c["Spike_24h"] = ratios["spike_24h"] or c.get("Spike_24h_apewisdom")
        c["Spike_3d"] = ratios["spike_3d"]
        c["Spike_7d"] = ratios["spike_7d"]
        c["Spike_30d"] = ratios["spike_30d"]
    if progress:
        progress("spike", 1, 1, "Spike ratios computed")

    diagnostics = {
        "raw_apewisdom_count": universe_size,
        "after_guardrails": len(enriched),
        "dropped_guardrails": dropped_count,
        "history_snapshots_used": len(history),
        "rs_map_size": len(rs_map),
        "source_status": dict(SOURCE_STATUS),
    }

    result = SocialScanResult(
        timestamp=timestamp,
        settings=asdict(settings),
        universe_size=universe_size,
        after_guardrails=len(enriched),
        candidates=enriched,
        diagnostics=diagnostics,
    )
    path = save_social_snapshot(result)
    if path is not None:
        result.snapshot_path = str(path)
    return result
