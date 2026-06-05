#!/usr/bin/env python3
"""
Flipper 880ST / 900ST — boat listing scraper.

Strategy:
  - Blocket & DBA.dk  → RSS feeds  (lightweight, not blocked)
  - Scanboat, Boat24, Nettivene, Tori.fi, Auto24.ee → Playwright headless Chrome
    (GitHub Actions datacenter IPs are blocked by these sites for plain requests)
"""

import json
import re
import logging
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path

import requests
from bs4 import BeautifulSoup
from playwright.sync_api import sync_playwright

# ─── Config ──────────────────────────────────────────────────────────────────

MODELS = [("880 ST", "flipper 880 st"), ("900 ST", "flipper 900 st")]

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9,sv;q=0.8",
    "Accept": "text/html,application/xhtml+xml,*/*;q=0.8",
}

EUR_SEK = 10.86
DKK_SEK = 1.4545

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

_id_counter = 1000


# ─── Helpers ─────────────────────────────────────────────────────────────────

def new_id():
    global _id_counter
    _id_counter += 1
    return _id_counter


def parse_price(text):
    if not text:
        return None, "EUR"
    text = str(text).strip()
    tl = text.lower()
    if "dkk" in tl or "dkr" in tl or "kr." in tl:
        cur = "DKK"
    elif "kr" in tl or "sek" in tl:
        cur = "SEK"
    elif "€" in text or "eur" in tl:
        cur = "EUR"
    else:
        cur = "EUR"
    # Match numbers including comma/dot/space thousand separators, e.g. "118,620" or "1 500 000"
    nums = re.findall(r"\d[\d\s,. ]*\d|\d{4,}", text)
    if not nums:
        return None, cur
    # Take the longest number (most likely the price, not a year)
    best = max(nums, key=lambda s: len(re.sub(r"[^\d]", "", s)))
    digits = re.sub(r"[^\d]", "", best)
    if not digits or len(digits) < 3:
        return None, cur
    val = int(digits)
    # Sanity check: boat prices should be 1 000 – 10 000 000
    if val < 1000 or val > 10_000_000:
        return None, cur
    return val, cur


def to_sek(amount, currency):
    if amount is None:
        return None
    return round(amount * {"SEK": 1.0, "EUR": EUR_SEK, "DKK": DKK_SEK}.get(currency, 1.0))


def make_boat(*, title, model, year, price_orig, currency, hours, engine,
              country, city, url, source_name, vat="unknown"):
    return {
        "id": new_id(),
        "model": model,
        "title": title[:200],
        "desc": f"Scraped {datetime.now(timezone.utc).strftime('%Y-%m-%d')} · {source_name}",
        "year": year,
        "country": country,
        "countryName": {
            "SE": "Sverige", "DK": "Danmark", "FI": "Finland",
            "EE": "Estland", "LV": "Lettland", "LT": "Litauen", "PL": "Polen",
        }.get(country or "", country or ""),
        "city": city,
        "priceOrig": price_orig,
        "priceOrigCur": currency,
        "priceSEK": to_sek(price_orig, currency),
        "hours": hours,
        "engine": engine,
        "rating": None,
        "vat": vat,
        "active": True,
        "sources": [{"name": source_name, "url": url}],
        "scraped": True,
    }


def matches_model(text, queries):
    tl = text.lower()
    return any(q in tl or q.replace(" ", "") in tl for q in queries)


# ─── RSS scrapers (lightweight, not blocked) ─────────────────────────────────

def _rss_get(url):
    try:
        r = requests.get(url, headers=HEADERS, timeout=15)
        r.raise_for_status()
        return ET.fromstring(r.content)
    except Exception as exc:
        log.warning("  RSS failed %s: %s", url, exc)
        return None


def scrape_blocket_rss():
    """Blocket RSS feed — bypasses API 503 blocks."""
    results = []
    for model_label, query in MODELS:
        q = query.replace(" ", "+")
        # Try multiple RSS URL formats
        tree = None
        for rss_url in [
            f"https://www.blocket.se/rss/hela_sverige?q={q}&ca=5050&st=s",
            f"https://www.blocket.se/rss/fritid_hobby?q={q}&st=s",
            f"https://www.blocket.se/rss/?q={q}&ca=5050",
        ]:
            tree = _rss_get(rss_url)
            if tree is not None:
                break
        if tree is None:
            continue
        for item in tree.findall(".//item"):
            title = (item.findtext("title") or "").strip()
            if not matches_model(title, [query]):
                continue
            link = (item.findtext("link") or "").strip()
            desc_html = item.findtext("description") or ""
            desc_text = re.sub(r"<[^>]+>", " ", desc_html)
            # Price often appears as "1 500 000 kr" in title or description
            price_m = re.search(r"([\d\s]{4,})\s*kr", title + " " + desc_text)
            price_orig, currency = parse_price(price_m.group().strip() if price_m else "")
            year_m = re.search(r"\b(20\d{2}|19\d{2})\b", title + " " + desc_text)
            city_m = re.search(r"[-–]\s*([A-ZÅÄÖ][a-zåäö]+(?:\s[A-ZÅÄÖ]?[a-zåäö]+)?)$", title)
            results.append(make_boat(
                title=title, model=model_label,
                year=int(year_m.group()) if year_m else None,
                price_orig=price_orig, currency=currency or "SEK",
                hours=None, engine=None,
                country="SE", city=city_m.group(1) if city_m else None,
                url=link, source_name="Blocket",
                vat="private",
            ))
    return results


def scrape_dba_rss():
    """DBA.dk RSS feed."""
    results = []
    for model_label, query in MODELS:
        q = query.replace(" ", "+")
        tree = _rss_get(f"https://www.dba.dk/rss/?q={q}&sort=rel")
        if tree is None:
            continue
        for item in tree.findall(".//item"):
            title = (item.findtext("title") or "").strip()
            if not matches_model(title, [query]):
                continue
            link = (item.findtext("link") or "").strip()
            desc_html = item.findtext("description") or ""
            desc_text = re.sub(r"<[^>]+>", " ", desc_html)
            combined = title + " " + desc_text
            price_m = re.search(r"([\d\s.,]{4,})\s*(kr\.?|dkk)", combined.lower())
            price_orig, currency = parse_price(price_m.group() if price_m else "")
            if not currency or currency == "EUR":
                currency = "DKK"
            results.append(make_boat(
                title=title, model=model_label, year=None,
                price_orig=price_orig, currency=currency,
                hours=None, engine=None,
                country="DK", city=None,
                url=link, source_name="DBA.dk",
            ))
    return results


# ─── Playwright scrapers ──────────────────────────────────────────────────────

def _pw_get(page, url, wait_ms=2000):
    try:
        page.goto(url, wait_until="domcontentloaded", timeout=25000)
        page.wait_for_timeout(wait_ms)
        html = page.content()
        if _is_sold_page(html):
            log.info("  skipping sold/removed page: %s", url)
            return None
        return html
    except Exception as exc:
        log.warning("  pw_get failed %s: %s", url, exc)
        return None


COUNTRY_MAP = {
    "sweden": "SE", "sverige": "SE",
    "denmark": "DK", "danmark": "DK",
    "finland": "FI", "suomi": "FI",
    "estonia": "EE", "eesti": "EE",
    "latvia": "LV", "lettland": "LV",
    "lithuania": "LT", "litauen": "LT",
    "norway": "NO", "norge": "NO",
    "germany": "DE", "deutschland": "DE",
    "poland": "PL", "polska": "PL",
    "netherlands": "NL", "holland": "NL",
    "belgium": "BE", "belgien": "BE",
    "france": "FR", "frankrike": "FR",
}

# Regex patterns that indicate a commercial seller (not private)
DEALER_PATTERN = re.compile(
    r"\b(A/S|AS\b|AB\b|OÜ|Oy\b|GmbH|Ltd\b|LLC|SIA|UAB|BV\b|S\.A\.|SRL|NV\b|Båt|Marin|Marine|Båtar|Yachts?|Boats?)\b",
    re.IGNORECASE,
)

SOLD_MARKERS = [
    "sold", "solgt", "såld", "såldes", "vendu",
    "poistettu myynnistä",      # Nettivene FI
    "ikke længere tilgængelig", # DBA.dk
    "denna annons är borttagen",
    "listing not found", "ad not found",
    "this boat has been sold",
    "båten är såld",
]


def _is_sold_page(html):
    """Return True if the page content indicates a sold/removed listing."""
    if not html:
        return False
    snippet = html.lower()[:12000]
    return any(m in snippet for m in SOLD_MARKERS)


def _extract_listings(html, model_label, query, source_name, base_url, country):
    """
    Generic extraction: find anchor tags whose text contains the model query,
    then walk up the DOM to find price/year context.
    """
    if not html:
        return []
    soup = BeautifulSoup(html, "lxml")
    results = []
    seen_hrefs = set()

    for a in soup.find_all("a", href=True):
        text = a.get_text(" ", strip=True)
        href = a.get("href", "").strip()
        if not href or href in seen_hrefs:
            continue
        if not matches_model(text, [query, query.replace(" ", "")]):
            continue
        # Skip navigation/menu links (too short)
        if len(text) < 8:
            continue
        seen_hrefs.add(href)
        # Clean up Scanboat / Boat24 title noise
        text = re.sub(r"\s*\|\s*(Motorboat|Motor boat|Sailboat|Year|Location|Price|Details).*$",
                      "", text, flags=re.IGNORECASE).strip()
        # Remove trailing "213,660 EUR Motorboat" style fragments
        text = re.sub(r"\s+[\d][\d,. ]+\s*(EUR|SEK|DKK)\s*(Motorboat|Sailboat|Boat)?\s*$",
                      "", text, flags=re.IGNORECASE).strip()
        text = re.sub(r"\s+(EUR|SEK|DKK)\s*$", "", text).strip()
        if not text or len(text) < 8:
            continue

        # Climb the DOM for price context
        price_orig, currency = None, "EUR"
        year = None
        container = a
        for _ in range(5):
            parent = container.parent
            if parent is None:
                break
            container = parent
            ctx = container.get_text(" ")
            if not price_orig:
                # Include comma/dot/nbsp as thousand separators
                pm = re.search(r"(\d[\d\s,. ]{3,})\s*(€|kr\.?|dkk|eur)", ctx.lower())
                if pm:
                    price_orig, currency = parse_price(pm.group())
            if not year:
                ym = re.search(r"\b(20\d{2}|19\d{2})\b", ctx)
                if ym:
                    year = int(ym.group())
            if price_orig and year:
                break

        full_url = (base_url.rstrip("/") + href
                    if href.startswith("/") else href)
        # Skip anchors that point back to the listing index page itself
        if full_url.rstrip("/") == base_url.rstrip("/"):
            continue

        results.append(make_boat(
            title=text, model=model_label, year=year,
            price_orig=price_orig, currency=currency or "EUR",
            hours=None, engine=None,
            country=country, city=None,
            url=full_url, source_name=source_name,
        ))
    return results


def _parse_scanboat_text(text):
    """
    Parse Scanboat's structured anchor/context text into clean fields.
    Input e.g.: "Flipper 900 ST 213,660 EUR Motorboat | Year : 2026 | Country : Denmark
                 Engine : 2 x Mercury 250 Verado Siim Båd og Motor A/S ..."
    """
    # Year
    year = None
    ym = re.search(r"Year\s*[:\-]\s*(\d{4})", text, re.IGNORECASE)
    if ym:
        year = int(ym.group(1))
    else:
        ym = re.search(r"\b(20\d{2}|19\d{2})\b", text)
        if ym:
            year = int(ym.group())

    # Country
    country_code = None
    cm = re.search(
        r"Country\s*[:\-]\s*([A-Za-zÆØÅæøåÄÖä ]{3,25}?)(?:\s+Engine|\s*\||\s{3,}|$)",
        text, re.IGNORECASE,
    )
    if cm:
        country_code = COUNTRY_MAP.get(cm.group(1).strip().lower())

    # Engine – grab text after "Engine :" until whitespace runs out or dealer name starts
    engine = None
    em = re.search(r"Engine\s*[:\-]\s*(.{5,120})", text, re.IGNORECASE)
    if em:
        raw = em.group(1).strip()
        # Stop at two+ consecutive spaces
        raw = re.split(r"\s{2,}", raw)[0]
        # Stop before broker/dealer names (e.g. "Navark Yachtbrokers", "Siim Båd", "Båtgiganten")
        raw = re.sub(
            r"\s+(?:[A-ZÆØÅ][a-zæøå]+\s+){0,2}(?:Yachtbroker|Broker|Marin|Marine|Båd\b|Båtar|Yachts?\b|Boats?\b|A/S|AS\b|AB\b|OÜ|GmbH|Ltd).*$",
            "", raw, flags=re.IGNORECASE,
        ).strip()
        # Limit and clean
        engine = raw[:80].strip() if raw else None

    # Dealer detection
    is_dealer = bool(DEALER_PATTERN.search(text))

    # Clean title: keep only "Flipper 880/900 ST" before price/noise
    # 1. Split on first pipe and take the left part
    clean = re.split(r"\s*\|", text)[0].strip()
    # 2. Strip trailing "Motorboat" / "Sailboat" etc.
    clean = re.sub(r"\s+(Motorboat|Sailboat|Motor\s*Boat)\b.*$", "", clean, flags=re.IGNORECASE).strip()
    # 3. Strip trailing price fragment e.g. "213,660 EUR" or "1 500 000 kr"
    clean = re.sub(r"\s+\d[\d,. ]+\s*(EUR|SEK|DKK|€)\s*$", "", clean, flags=re.IGNORECASE).strip()
    # 4. Remove remaining price anywhere (in case of no pipe separator)
    clean = re.sub(r"\s+\d[\d,. ]{3,}\s*(EUR|SEK|DKK|€)", "", clean, flags=re.IGNORECASE).strip()
    # 5. Strip leading words before "Flipper" (e.g. Danish "Mere Flipper" = "Also Flipper")
    clean = re.sub(r"^.{0,20}?\b(Flipper\b)", r"\1", clean).strip()

    return {
        "title": clean or "Flipper",
        "year": year,
        "country_code": country_code,
        "engine": engine,
        "is_dealer": is_dealer,
    }


def _pw_scanboat(page):
    results = []
    for model_label, query in MODELS:
        q = query.replace(" ", "+")
        url = f"https://www.scanboat.com/en/boat-market/boats?SearchCriteria.BoatModelText={q}"
        html = _pw_get(page, url, wait_ms=2000)
        if not html:
            continue

        soup = BeautifulSoup(html, "lxml")
        seen_hrefs = set()

        for a in soup.find_all("a", href=True):
            href = a.get("href", "")
            # Only match individual boat listing pages (contain a numeric ID segment)
            if not re.search(r"/boat-market/boats/\w+-flipper-", href):
                continue
            if href in seen_hrefs:
                continue
            seen_hrefs.add(href)

            # Walk up the DOM until we find a container with price OR Year info
            # but stop before the text gets too long (avoids grabbing the whole page)
            container = a
            ctx_text = a.get_text(" ", strip=True)
            for _ in range(6):
                parent = container.parent
                if parent is None:
                    break
                candidate = parent.get_text(" ", strip=True)
                if len(candidate) > 900:
                    break          # would be too broad
                container = parent
                ctx_text = candidate
                if re.search(r"Year\s*[:\-]|Engine\s*[:\-]|\d{4,}\s*(EUR|SEK|DKK|€)", ctx_text, re.IGNORECASE):
                    break          # found structured data — stop here

            parsed = _parse_scanboat_text(ctx_text)
            if not any(qv in parsed["title"].lower() for qv in [query, query.replace(" ", "")]):
                continue

            # Price from context text
            pm = re.search(r"(\d[\d,. ]{3,})\s*(€|EUR|kr\.?|SEK|DKK)", ctx_text, re.IGNORECASE)
            price_orig, currency = parse_price(pm.group() if pm else "")

            full_url = ("https://www.scanboat.com" + href
                        if href.startswith("/") else href)

            results.append(make_boat(
                title=parsed["title"],
                model=model_label,
                year=parsed["year"],
                price_orig=price_orig,
                currency=currency or "EUR",
                hours=None,
                engine=parsed["engine"],
                country=parsed["country_code"],
                city=None,
                url=full_url,
                source_name="Scanboat",
                vat="incl" if parsed["is_dealer"] else "private",
            ))
    return results


def _pw_blocket(page):
    """
    Blocket via Playwright.
    Tries to extract structured data from __NEXT_DATA__ (Next.js),
    falls back to rendered-HTML link extraction.
    """
    results = []
    for model_label, query in MODELS:
        q = query.replace(" ", "+")
        url = (
            "https://www.blocket.se/annonser/hela_sverige"
            f"/fritid_hobby/batar_vattensport?q={q}"
        )
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=30000)
            page.wait_for_timeout(3000)
        except Exception as exc:
            log.warning("  Blocket page load failed %s: %s", url, exc)
            continue

        html = page.content()
        if _is_sold_page(html):
            continue

        soup = BeautifulSoup(html, "lxml")

        # ── Try __NEXT_DATA__ first ──
        nd = soup.find("script", id="__NEXT_DATA__")
        if nd:
            try:
                data = json.loads(nd.string)
                items = (
                    data.get("props", {})
                    .get("pageProps", {})
                    .get("listings", [])
                )
                for item in items:
                    subject = item.get("subject", "")
                    if not any(qv in subject.lower() for qv in [query, query.replace(" ", "")]):
                        continue
                    price_data = item.get("price") or {}
                    price = price_data.get("value")
                    locs = item.get("location") or [{}]
                    city = locs[-1].get("name") if locs else None
                    ad_id = item.get("ad_id") or item.get("id", "")
                    is_private = item.get("account_type") == "private"
                    results.append(make_boat(
                        title=subject,
                        model=model_label,
                        year=None,
                        price_orig=price,
                        currency="SEK",
                        hours=None,
                        engine=None,
                        country="SE",
                        city=city,
                        url=f"https://www.blocket.se/annons/{ad_id}",
                        source_name="Blocket",
                        vat="private" if is_private else "incl",
                    ))
                if items:
                    continue  # Got data from NEXT_DATA, skip fallback
            except (json.JSONDecodeError, AttributeError, KeyError):
                pass

        # ── Fallback: rendered HTML links ──
        found = _extract_listings(
            html, model_label, query, "Blocket",
            "https://www.blocket.se", "SE",
        )
        results.extend(found)

    return results


def _pw_boat24(page):
    results = []
    slugs = {"880 ST": "flipper-880-st", "900 ST": "flipper-900-st"}
    for model_label, query in MODELS:
        url = f"https://www.boat24.com/en/powerboats/flipper/{slugs[model_label]}/"
        html = _pw_get(page, url)
        results.extend(_extract_listings(
            html, model_label, query, "Boat24",
            "https://www.boat24.com", None,
        ))
    return results


def _pw_nettivene(page):
    results = []
    for model_label, query in MODELS:
        slug = "880-st" if "880" in model_label else "900-st"
        url = f"https://www.nettivene.com/en/moottorivene/flipper/{slug}/"
        html = _pw_get(page, url)
        results.extend(_extract_listings(
            html, model_label, query, "Nettivene",
            "https://www.nettivene.com", "FI",
        ))
    return results


def _pw_tori(page):
    results = []
    for model_label, query in MODELS:
        q = query.replace(" ", "+")
        # Try multiple URL formats — Tori.fi has changed their URL structure
        for url in [
            f"https://www.tori.fi/koko_suomi?q={q}&ca=13&w=3",
            f"https://www.tori.fi/koko_suomi/veneet?q={q}&w=3",
            f"https://www.tori.fi/koko_suomi?q={q}&w=3",
        ]:
            html = _pw_get(page, url)
            found = _extract_listings(
                html, model_label, query, "Tori.fi",
                "https://www.tori.fi", "FI",
            )
            if found:
                results.extend(found)
                break
    return results


def _pw_auto24(page):
    results = []
    for model_label, query in MODELS:
        q = query.replace(" ", "+")
        # Try different URL structures for Auto24
        for url in [
            f"https://eng.auto24.ee/boats/?q={q}",
            f"https://eng.auto24.ee/search/?q={q}&category=boats",
            f"https://www.auto24.ee/kasutatud/nimekiri.php?a=110&otsi={q}",
        ]:
            html = _pw_get(page, url, wait_ms=3000)
            found = _extract_listings(
                html, model_label, query, "Auto24.ee",
                "https://eng.auto24.ee", "EE",
            )
            if found:
                results.extend(found)
                break
    return results


def _pw_veneporssi(page):
    results = []
    for model_label, query in MODELS:
        q = query.replace(" ", "+")
        for url in [
            f"https://www.veneporssi.fi/haku?hakusana={q}",
            f"https://www.veneporssi.fi/?s={q}",
        ]:
            html = _pw_get(page, url)
            found = _extract_listings(
                html, model_label, query, "Venepörssi",
                "https://www.veneporssi.fi", "FI",
            )
            if found:
                results.extend(found)
                break
    return results


def run_playwright_scrapers():
    all_results = []
    scrapers = [
        ("Blocket",     _pw_blocket),
        ("Scanboat",    _pw_scanboat),
        ("Boat24",      _pw_boat24),
        ("Nettivene",   _pw_nettivene),
        ("Tori.fi",     _pw_tori),
        ("Auto24.ee",   _pw_auto24),
        ("Venepörssi",  _pw_veneporssi),
    ]
    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-setuid-sandbox", "--disable-dev-shm-usage"],
        )
        ctx = browser.new_context(
            user_agent=HEADERS["User-Agent"],
            locale="en-US",
            viewport={"width": 1280, "height": 900},
        )
        page = ctx.new_page()
        # Block images/fonts to speed up loading
        page.route("**/*.{png,jpg,jpeg,gif,svg,woff,woff2,ttf,eot}", lambda r: r.abort())

        for name, fn in scrapers:
            log.info("Scraping %s (Playwright) ...", name)
            try:
                found = fn(page)
                log.info("  → %d listings", len(found))
                all_results.extend(found)
            except Exception as exc:
                log.error("  → FAILED: %s", exc)

        browser.close()
    return all_results


# ─── Dedup ───────────────────────────────────────────────────────────────────

def dedup(boats_list):
    seen_urls = set()
    seen_title_year = set()
    out = []
    for b in boats_list:
        url_tail = None
        for s in b.get("sources", []):
            parts = s.get("url", "").rstrip("/").split("/")
            if parts and len(parts[-1]) > 4:
                url_tail = parts[-1]
                break
        title_key = (
            re.sub(r"\s+", " ", (b.get("title") or "").lower().strip()),
            b.get("year"),
        )
        if url_tail and url_tail in seen_urls:
            continue
        if title_key[0] and title_key in seen_title_year:
            continue
        if url_tail:
            seen_urls.add(url_tail)
        if title_key[0]:
            seen_title_year.add(title_key)
        out.append(b)
    return out


# ─── HTML injection ──────────────────────────────────────────────────────────

MARKER_START = "<!-- SCRAPED_DATA_START -->"
MARKER_END = "<!-- SCRAPED_DATA_END -->"


def inject(boats_list, html_path="index.html"):
    path = Path(html_path)
    html = path.read_text(encoding="utf-8")
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    boats_json = json.dumps(boats_list, ensure_ascii=False)
    block = (
        f"{MARKER_START}\n"
        f"const scrapedBoats = {boats_json};\n"
        f"const lastScraped = '{now}';\n"
        f"{MARKER_END}"
    )
    if MARKER_START in html:
        html = re.sub(
            re.escape(MARKER_START) + r".*?" + re.escape(MARKER_END),
            lambda _: block,
            html,
            flags=re.DOTALL,
        )
    else:
        html = html.replace(
            "// Initial render\nrenderTable();",
            f"{block}\n\n// Initial render\nrenderTable();",
        )
    path.write_text(html, encoding="utf-8")
    log.info("Wrote %d boats to %s (last scraped: %s)", len(boats_list), html_path, now)


# ─── Main ────────────────────────────────────────────────────────────────────

def main():
    all_boats = []

    # Lightweight RSS scrapers (no bot blocking)
    for name, fn in [("Blocket (RSS)", scrape_blocket_rss), ("DBA.dk (RSS)", scrape_dba_rss)]:
        log.info("Scraping %s ...", name)
        try:
            found = fn()
            log.info("  → %d listings", len(found))
            all_boats.extend(found)
        except Exception as exc:
            log.error("  → FAILED: %s", exc)

    # Playwright scrapers (headless Chrome bypasses bot protection)
    log.info("Starting Playwright scrapers ...")
    try:
        found = run_playwright_scrapers()
        all_boats.extend(found)
    except Exception as exc:
        log.error("Playwright scrapers FAILED: %s", exc)

    boats = dedup(all_boats)
    log.info("Total after dedup: %d", len(boats))

    Path("boats_scraped.json").write_text(
        json.dumps(boats, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    inject(boats)


if __name__ == "__main__":
    main()
