"""Ricerca web gratuita per il discovery delle pagine aziendali.

Sostituisce la SERP live di DataForSEO (a pagamento) nel trovare le pagine
delle aziende sulle piattaforme (TripAdvisor, Trustpilot, AutoScout24…).
Nessuna credenziale richiesta, nessun costo per query.

Provider, in ordine:
  1. DuckDuckGo (endpoint HTML "lite", niente JavaScript)
  2. Bing (fallback quando DDG non risponde o blocca il datacenter)

Il parsing è fatto con regex/stdlib: il progetto usa solo flask+httpx
(vedi requirements.txt), quindi niente BeautifulSoup o simili.
Le funzioni `_parse_ddg` / `_parse_bing` sono pure (HTML -> risultati)
così i test le esercitano senza rete.

Le query passano ai motori così come sono: l'operatore `site:` è
supportato da entrambi.
"""
from __future__ import annotations

import base64
import re
from html import unescape
from urllib.parse import parse_qs, urlparse

import httpx

# User-Agent desktop realistico: gli endpoint HTML servono la versione
# completa solo ai browser "veri".
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")
TIMEOUT = 15

_TAG_RE = re.compile(r"<[^>]+>")

# DDG HTML: ogni risultato è un <a class="result__a" href="…">Titolo</a>
_DDG_A_RE = re.compile(
    r'<a\b[^>]*class="[^"]*\bresult__a\b[^"]*"[^>]*>(.*?)</a>', re.S)
# Bing: ogni risultato organico è un <li class="b_algo"> con <h2><a href=…>
_BING_LI_RE = re.compile(r'<li[^>]*class="[^"]*\bb_algo\b[^"]*"', re.I)
_BING_H2A_RE = re.compile(
    r'<h2[^>]*>\s*<a\b[^>]*href="([^"]+)"[^>]*>(.*?)</a>', re.S)
_HREF_RE = re.compile(r'href="([^"]+)"')


def _text(fragment: str) -> str:
    """Testo pulito da un frammento HTML (tag via, entità decodificate)."""
    return unescape(_TAG_RE.sub("", fragment)).strip()


def _decode_ddg_href(href: str) -> str | None:
    """URL reale da un link DDG; None se pubblicità o link non valido.

    I link DDG sono spesso redirect del tipo
    //duckduckgo.com/l/?uddg=<url-percent-encoded>&rut=… : l'URL vero va
    estratto dal parametro `uddg` (parse_qs lo decodifica già).
    I link pubblicitari (y.js / ad_domain) vanno saltati.
    """
    href = unescape(href)
    if href.startswith("//"):
        href = "https:" + href
    p = urlparse(href)
    if p.netloc.endswith("duckduckgo.com"):
        # pubblicità: redirect y.js oppure parametro ad_domain
        if "y.js" in p.path or "ad_domain" in p.query:
            return None
        uddg = parse_qs(p.query).get("uddg")
        return uddg[0] if uddg else None
    if p.scheme in ("http", "https"):
        return href
    return None


def _parse_ddg(html: str) -> list[dict]:
    """[{url,title}] dai risultati dell'endpoint HTML di DuckDuckGo."""
    out = []
    for m in _DDG_A_RE.finditer(html):
        hm = _HREF_RE.search(m.group(0))
        if not hm:
            continue
        url = _decode_ddg_href(hm.group(1))
        title = _text(m.group(1))
        if url and title:
            out.append({"url": url, "title": title})
    return out


def _decode_bing_href(href: str) -> str | None:
    """URL reale da un link Bing; None se pubblicità o link non decodificabile.

    I risultati organici sono spesso redirect bing.com/ck/a?…&u=a1<base64url>:
    l'URL vero è nel parametro `u`, prefissato "a1" e codificato base64url
    senza padding. Gli annunci (bing.com/aclick) vanno saltati.
    """
    href = unescape(href)
    if not href.startswith("http"):
        return None
    p = urlparse(href)
    if not p.netloc.endswith("bing.com"):
        return href
    if "/aclick" in p.path:                 # annuncio
        return None
    if p.path.startswith("/ck/"):           # redirect organico
        v = parse_qs(p.query).get("u", [""])[0]
        if v.startswith("a1"):
            v = v[2:] + "=" * (-len(v[2:]) % 4)
            try:
                u = base64.urlsafe_b64decode(v).decode("utf-8", "replace")
                return u if u.startswith("http") else None
            except (ValueError, UnicodeDecodeError):
                return None
    return None                             # altro link interno bing.com


def _parse_bing(html: str) -> list[dict]:
    """[{url,title}] dai blocchi <li class="b_algo"> di Bing."""
    out = []
    pieces = _BING_LI_RE.split(html)[1:]   # tutto ciò che segue ogni b_algo
    for piece in pieces:
        m = _BING_H2A_RE.search(piece)
        if not m:
            continue
        url, title = _decode_bing_href(m.group(1)), _text(m.group(2))
        if url and title:
            out.append({"url": url, "title": title})
    return out


def _fetch_ddg(q: str) -> str:
    r = httpx.post("https://html.duckduckgo.com/html/",
                   data={"q": q},
                   headers={"User-Agent": UA, "Accept-Language": "it"},
                   timeout=TIMEOUT, follow_redirects=True)
    r.raise_for_status()
    return r.text


def _fetch_bing(q: str, num: int) -> str:
    r = httpx.get("https://www.bing.com/search",
                  params={"q": q, "count": num},
                  headers={"User-Agent": UA, "Accept-Language": "it-IT,it"},
                  timeout=TIMEOUT, follow_redirects=True)
    r.raise_for_status()
    return r.text


def search(q: str, num: int = 10) -> list[dict]:
    """Ricerca web gratuita: [{url,title}], al massimo `num` risultati.

    Primario DuckDuckGo, fallback Bing; ogni provider è in try/except,
    se tutto fallisce ritorna [] (mai eccezioni verso il chiamante).
    """
    try:
        hits = _parse_ddg(_fetch_ddg(q))
        if hits:
            return hits[:num]
    except Exception:
        pass
    try:
        hits = _parse_bing(_fetch_bing(q, num))
        if hits:
            return hits[:num]
    except Exception:
        pass
    return []
