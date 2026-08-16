"""Helper condivisi per i connettori "public_crawl" (scraper di pagine pubbliche).

Tre strategie in cascata, tutte tolleranti ai cambi di layout:
  1. JSON-LD  — blocchi <script type="application/ld+json"> con Review/AggregateRating
  2. Microdata — attributi itemprop (reviewBody / ratingValue / datePublished)
  3. Deep harvest — qualunque JSON embedded (__NEXT_DATA__ o simili): si cercano
     ricorsivamente oggetti che "sembrano" recensioni (rating + testo + data).
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timezone

import httpx

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0 Safari/537.36")

JSONLD_RE = re.compile(
    r'<script[^>]*type="application/ld\+json"[^>]*>(.*?)</script>', re.S | re.I)
EMBED_JSON_RE = re.compile(
    r'<script[^>]*type="application/json"[^>]*>(.*?)</script>', re.S | re.I)


def fetch_html(url: str, timeout: int = 25) -> str:
    r = httpx.get(url, headers={"User-Agent": UA, "Accept-Language": "it"},
                  timeout=timeout, follow_redirects=True)
    r.raise_for_status()
    return r.text


def parse_date(value) -> datetime | None:
    """Prova i formati data più comuni; None se non riconosciuto."""
    if value is None:
        return None
    s = str(value).strip()
    for fmt in (None, "%Y-%m-%d", "%d.%m.%Y", "%d/%m/%Y", "%Y-%m-%d %H:%M:%S %z",
                "%Y-%m-%dT%H:%M:%S%z", "%d-%m-%Y"):
        try:
            if fmt is None:
                return datetime.fromisoformat(s.replace("Z", "+00:00"))
            d = datetime.strptime(s, fmt)
            return d if d.tzinfo else d.replace(tzinfo=timezone.utc)
        except (ValueError, TypeError):
            continue
    return None


def _norm_review(author, stars, text, date, scale_max=5.0) -> dict | None:
    d = parse_date(date)
    try:
        st = float(stars)
    except (TypeError, ValueError):
        return None
    if not d or not (0 <= st <= scale_max):
        return None
    if d.tzinfo is None:
        d = d.replace(tzinfo=timezone.utc)
    return {"author": str(author or "anon"), "stars": st,
            "scale_max": scale_max, "text": (str(text).strip() or None) if text else None,
            "date": d}


def iter_jsonld_reviews(html: str):
    """Estrae le Review dai blocchi JSON-LD (schema.org)."""
    for m in JSONLD_RE.finditer(html):
        try:
            data = json.loads(m.group(1).strip())
        except json.JSONDecodeError:
            continue
        stack = data if isinstance(data, list) else [data]
        while stack:
            node = stack.pop()
            if not isinstance(node, dict):
                continue
            t = str(node.get("@type", "")).lower()
            if t == "review":
                rt = node.get("reviewRating") or {}
                rv = _norm_review(
                    (node.get("author") or {}).get("name") if isinstance(node.get("author"), dict) else node.get("author"),
                    rt.get("ratingValue"),
                    node.get("reviewBody") or node.get("description") or node.get("name"),
                    node.get("datePublished"),
                    float(rt.get("bestRating") or 5))
                if rv:
                    yield rv
            for v in node.values():
                if isinstance(v, dict):
                    stack.append(v)
                elif isinstance(v, list):
                    stack.extend(x for x in v if isinstance(x, dict))


MICRO_BLOCK_RE = re.compile(r'itemprop="review"(.{0,4000}?)(?=itemprop="review"|$)', re.S)
MICRO_FIELD = {
    "stars": re.compile(r'itemprop="ratingValue"[^>]*(?:content="([\d.,]+)"|>([\d.,]+))'),
    "date": re.compile(r'itemprop="datePublished"[^>]*(?:content="([^"]+)"|>([^<]+))'),
    "text": re.compile(r'itemprop="(?:reviewBody|description)"[^>]*>(.*?)</', re.S),
    "author": re.compile(r'itemprop="(?:author|name)"[^>]*>([^<]{1,80})<'),
}


def iter_microdata_reviews(html: str):
    for m in MICRO_BLOCK_RE.finditer(html):
        blk = m.group(1)
        vals = {}
        for k, rx in MICRO_FIELD.items():
            mm = rx.search(blk)
            vals[k] = next((g for g in mm.groups() if g), None) if mm else None
        if vals.get("stars"):
            rv = _norm_review(vals.get("author"),
                              str(vals["stars"]).replace(",", "."),
                              re.sub(r"<[^>]+>", " ", vals.get("text") or "").strip(),
                              vals.get("date"))
            if rv:
                yield rv


_TEXT_KEYS = ("text", "comment", "review", "reviewtext", "body", "content", "description")
_STAR_KEYS = ("rating", "stars", "score", "ratingvalue", "value")
_DATE_KEYS = ("date", "created", "createdat", "publishedat", "datepublished",
              "timestamp", "creationdate", "reviewdate")
_AUTH_KEYS = ("author", "name", "user", "username", "nickname", "reviewer")


def _looks_like_review(d: dict) -> dict | None:
    low = {k.lower().replace("_", ""): v for k, v in d.items()}
    stars = next((low[k] for k in _STAR_KEYS if k in low), None)
    if isinstance(stars, dict):
        stars = stars.get("value") or stars.get("ratingValue")
    date = next((low[k] for k in _DATE_KEYS if k in low), None)
    text = next((low[k] for k in _TEXT_KEYS if k in low and isinstance(low[k], str)), None)
    auth = next((low[k] for k in _AUTH_KEYS if k in low), None)
    if isinstance(auth, dict):
        auth = auth.get("name") or auth.get("nickname")
    if stars is None or date is None:
        return None
    return _norm_review(auth, stars, text, date)


def deep_harvest(html: str):
    """Cerca recensioni in QUALUNQUE JSON embedded nella pagina (Next.js ecc.)."""
    seen = set()
    for rx in (EMBED_JSON_RE,):
        for m in rx.finditer(html):
            try:
                data = json.loads(m.group(1).strip())
            except json.JSONDecodeError:
                continue
            stack = [data]
            while stack:
                node = stack.pop()
                if isinstance(node, dict):
                    rv = _looks_like_review(node)
                    if rv:
                        key = (rv["author"], rv["date"].isoformat(), rv["stars"])
                        if key not in seen:
                            seen.add(key)
                            yield rv
                    stack.extend(node.values())
                elif isinstance(node, list):
                    stack.extend(node)


def scrape_reviews(url: str) -> tuple[list[dict], str]:
    """Applica le strategie in cascata; ritorna (recensioni, metodo)."""
    html = fetch_html(url)
    for method, fn in (("jsonld", iter_jsonld_reviews),
                       ("microdata", iter_microdata_reviews),
                       ("deep", deep_harvest)):
        found = list(fn(html))
        if found:
            return found, method
    return [], "none"
