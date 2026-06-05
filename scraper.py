#!/usr/bin/env python3
"""
Flipper 880ST / 900ST — boat listing scraper.
Scrapes Nordic/Baltic marketplaces and injects results into index.html.
"""

import json
import re
import time
import logging
from datetime import datetime, timezone
from pathlib import Path
import requests
from bs4 import BeautifulSoup

MODELS = [("880 ST", ["flipper 880 st", "flipper 880st"]),
          ("900 ST", ["flipper 900 st", "flipper 900st"])]

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9,sv;q=0.8,fi;q=0.7,da;q=0.6",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Encoding": "gzip, deflate, br",
}

EUR_SEK = 10.86
DKK_SEK = 1.4545
PLN_SEK = 2.51

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)


# ─── HTTP helper ────────────────────────────────────────────────────────────

def get(url, *, params=None, headers=None, as_json=False, timeout=20):
    h = {**HEADERS, **(headers or {})}
    for attempt in range(3):
        try:
            r = requests.get(url, params=params, headers=h, timeout=timeout)
            r.raise_for_status()
            return r.json() if as_json else r.text
        except Exception as exc:
            log.warning("  attempt %d failed for %s: %s", attempt + 1, url, exc)
            if attempt < 2:
                time.sleep(2 ** attempt)
    return None


# ─── Price helpers ──────────────────────────────────────────────────────────

def parse_price(text):
    """Return (amount_int_or_None, currency_str)."""
    if not text:
        return None, "EUR"
    text = text.strip()
    cur = "EUR"
    tl = text.lower()
    if "kr" in tl or "sek" in tl:
        cur = "SEK"
    elif "dkk" in tl or "dkr" in tl or "kr." in tl:
        cur = "DKK"
    elif "pln" in tl or "zł" in tl:
        cur = "PLN"
    elif "€" in text or "eur" in tl:
        cur = "EUR"
    digits = re.sub(r"[^\d]", "", text)
    return (int(digits) if digits else None), cur


def to_sek(amount, currency):
    if amount is None:
        return None
    rates = {"SEK": 1, "EUR": EUR_SEK, "DKK": DKK_SEK, "PLN": PLN_SEK}
    return round(amount * rates.get(currency, 1))


# ─── Boat factory ───────────────────────────────────────────────────────────

_id_counter = 1000

def new_id():
    global _id_counter
    _id_counter += 1
    return _id_counter


def boat(*, title, model, year, price_orig, currency, hours, engine,
         country, city, url, source_name, vat="unknown"):
    price_sek = to_sek(price_orig, currency)
    return {
        "id": new_id(),
        "model": model,
        "title": title,
        "desc": f"Scraped {datetime.now(timezone.utc).strftime('%Y-%m-%d')} · {source_name}",
        "year": year,
        "country": country,
        "countryName": {
            "SE": "Sverige", "DK": "Danmark", "FI": "Finland",
            "EE": "Estland", "LV": "Lettland", "LT": "Litauen",
            "PL": "Polen", "DE": "Tyskland",
        }.get(country, country or ""),
        "city": city,
        "priceOrig": price_orig,
        "priceOrigCur": currency,
        "priceSEK": price_sek,
        "hours": hours,
        "engine": engine,
        "rating": None,
        "vat": vat,
        "active": True,
        "sources": [{"name": source_name, "url": url}],
        "scraped": True,
    }


# ─── Blocket (SE) ────────────────────────────────────────────────────────────

def scrape_blocket():
    """Use Blocket's internal search API (returns JSON via __NEXT_DATA__)."""
    results = []
    for model_label, queries in MODELS:
        q = queries[0]
        # Blocket's REST search endpoint
        api_url = "https://api.blocket.se/search_bff/v2/content"
        params = {
            "q": q,
            "st": "s",          # for sale
            "ca": "5050",       # Boats & watersports category
            "hits_per_page": 60,
            "page": 1,
        }
        data = get(api_url, params=params, as_json=True,
                   headers={"Accept": "application/json"})
        if not data:
            # Fallback: scrape search page and read __NEXT_DATA__
            page_url = (
                "https://www.blocket.se/annonser/hela_sverige/fritid_hobby"
                f"/batar_vattensport?q={q.replace(' ', '+')}"
            )
            html = get(page_url)
            if not html:
                continue
            soup = BeautifulSoup(html, "lxml")
            nd = soup.find("script", id="__NEXT_DATA__")
            if not nd:
                continue
            try:
                nd_data = json.loads(nd.string)
                items = (
                    nd_data.get("props", {})
                    .get("pageProps", {})
                    .get("listings", [])
                )
            except (json.JSONDecodeError, AttributeError):
                continue
        else:
            items = data.get("data", [])

        for item in items:
            subject = item.get("subject", "")
            if not any(q in subject.lower() for q in queries):
                continue
            price_val = (item.get("price") or {}).get("value")
            location_list = item.get("location") or []
            city = location_list[-1].get("name") if location_list else None
            ad_id = item.get("ad_id") or item.get("id", "")
            is_private = (item.get("account_type", "") == "private")
            results.append(boat(
                title=subject,
                model=model_label,
                year=None,
                price_orig=price_val,
                currency="SEK",
                hours=None,
                engine=None,
                country="SE",
                city=city,
                url=f"https://www.blocket.se/annons/{ad_id}",
                source_name="Blocket",
                vat="private" if is_private else "incl",
            ))
    return results


# ─── Scanboat (SE/DK/FI/EU) ──────────────────────────────────────────────────

def scrape_scanboat():
    results = []
    for model_label, queries in MODELS:
        q = queries[0]
        url = (
            "https://www.scanboat.com/en/boat-market/boats"
            f"?SearchCriteria.BoatModelText={q.replace(' ', '+')}"
        )
        html = get(url)
        if not html:
            continue
        soup = BeautifulSoup(html, "lxml")
        for item in soup.select("article.boat-list-item, .boat-item, li.boat"):
            title_el = item.select_one("h2, h3, .boat-name, .title")
            price_el = item.select_one(".price, .boat-price, [class*=price]")
            year_el = item.select_one(".year, [class*=year]")
            loc_el = item.select_one(".location, [class*=location]")
            link_el = item.select_one("a[href]")
            if not title_el:
                continue
            title = title_el.get_text(" ", strip=True)
            if not any(qv in title.lower() for qv in queries):
                continue
            price_orig, cur = parse_price(price_el.get_text() if price_el else "")
            year_m = re.search(r"\b(20\d{2}|19\d{2})\b",
                               year_el.get_text() if year_el else title)
            year = int(year_m.group()) if year_m else None
            city = loc_el.get_text(strip=True) if loc_el else None
            href = link_el.get("href", "") if link_el else ""
            full_url = ("https://www.scanboat.com" + href
                        if href.startswith("/") else href or url)
            results.append(boat(
                title=title, model=model_label, year=year,
                price_orig=price_orig, currency=cur,
                hours=None, engine=None,
                country=None, city=city,
                url=full_url, source_name="Scanboat",
            ))
    return results


# ─── Boat24 (EU) ──────────────────────────────────────────────────────────────

def scrape_boat24():
    results = []
    slugs = {"880 ST": "flipper-880-st", "900 ST": "flipper-900-st"}
    for model_label, queries in MODELS:
        slug = slugs[model_label]
        url = f"https://www.boat24.com/en/powerboats/flipper/{slug}/"
        html = get(url)
        if not html:
            continue
        soup = BeautifulSoup(html, "lxml")
        for item in soup.select("article, .boat-list-item, .result-item"):
            title_el = item.select_one("h2, h3, .title, [class*=title]")
            price_el = item.select_one("[class*=price]")
            link_el = item.select_one("a[href]")
            if not title_el:
                continue
            title = title_el.get_text(" ", strip=True)
            price_orig, cur = parse_price(price_el.get_text() if price_el else "")
            href = link_el.get("href", "") if link_el else ""
            full_url = ("https://www.boat24.com" + href
                        if href.startswith("/") else href or url)
            year_m = re.search(r"\b(20\d{2}|19\d{2})\b", title)
            results.append(boat(
                title=title, model=model_label, year=int(year_m.group()) if year_m else None,
                price_orig=price_orig, currency=cur,
                hours=None, engine=None,
                country=None, city=None,
                url=full_url, source_name="Boat24",
            ))
    return results


# ─── Nettivene (FI) ──────────────────────────────────────────────────────────

def scrape_nettivene():
    results = []
    for model_label, queries in MODELS:
        slug = "880-st" if "880" in model_label else "900-st"
        for url in [
            f"https://www.nettivene.com/en/moottorivene/flipper/{slug}/",
            f"https://www.nettivene.com/moottorivene/flipper/{slug}/",
        ]:
            html = get(url)
            if html:
                break
        if not html:
            continue
        soup = BeautifulSoup(html, "lxml")
        for item in soup.select(".ad-list-item, .result, li.boat-item"):
            title_el = item.select_one("h2, h3, .ad-title, .title")
            price_el = item.select_one("[class*=price]")
            loc_el = item.select_one("[class*=location], [class*=city]")
            link_el = item.select_one("a[href]")
            if not title_el:
                continue
            title = title_el.get_text(" ", strip=True)
            if not any(qv in title.lower() for qv in queries):
                continue
            price_orig, cur = parse_price(price_el.get_text() if price_el else "")
            city = loc_el.get_text(strip=True) if loc_el else None
            href = link_el.get("href", "") if link_el else ""
            full_url = ("https://www.nettivene.com" + href
                        if href.startswith("/") else href or url)
            year_m = re.search(r"\b(20\d{2}|19\d{2})\b", title)
            results.append(boat(
                title=title, model=model_label, year=int(year_m.group()) if year_m else None,
                price_orig=price_orig, currency=cur or "EUR",
                hours=None, engine=None,
                country="FI", city=city,
                url=full_url, source_name="Nettivene",
            ))
    return results


# ─── Tori.fi (FI) ────────────────────────────────────────────────────────────

def scrape_tori():
    results = []
    for model_label, queries in MODELS:
        q = queries[0]
        html = get("https://www.tori.fi/koko_suomi",
                   params={"q": q, "ca": "13", "cg": "2050", "w": "3"})
        if not html:
            continue
        soup = BeautifulSoup(html, "lxml")
        for item in soup.select(".item_row_flex, .list_item, [class*=item-row]"):
            title_el = item.select_one(".li-title, h2, h3, [class*=title]")
            price_el = item.select_one(".list_price, .price_value, [class*=price]")
            loc_el = item.select_one(".cat_geo, [class*=location], [class*=geo]")
            link_el = item.select_one("a[href]")
            if not title_el:
                continue
            title = title_el.get_text(" ", strip=True)
            price_orig, cur = parse_price(price_el.get_text() if price_el else "")
            city = loc_el.get_text(strip=True) if loc_el else None
            href = link_el.get("href", "") if link_el else ""
            full_url = ("https://www.tori.fi" + href
                        if href.startswith("/") else href or "https://www.tori.fi")
            year_m = re.search(r"\b(20\d{2}|19\d{2})\b", title)
            results.append(boat(
                title=title, model=model_label, year=int(year_m.group()) if year_m else None,
                price_orig=price_orig, currency=cur or "EUR",
                hours=None, engine=None,
                country="FI", city=city,
                url=full_url, source_name="Tori.fi",
            ))
    return results


# ─── DBA.dk (DK) ─────────────────────────────────────────────────────────────

def scrape_dba():
    results = []
    for model_label, queries in MODELS:
        q = queries[0]
        url = f"https://www.dba.dk/soeg/?q={q.replace(' ', '+')}&sort=rel&ab=0"
        html = get(url)
        if not html:
            continue
        soup = BeautifulSoup(html, "lxml")
        for item in soup.select(".listing-item, .srp-item, article[class*=listing]"):
            title_el = item.select_one("h2, h3, [class*=title]")
            price_el = item.select_one("[class*=price]")
            link_el = item.select_one("a[href]")
            if not title_el:
                continue
            title = title_el.get_text(" ", strip=True)
            if not any(qv in title.lower() for qv in queries):
                continue
            price_orig, cur = parse_price(price_el.get_text() if price_el else "")
            if cur == "EUR":
                cur = "DKK"  # DBA prices are DKK
            href = link_el.get("href", "") if link_el else ""
            full_url = ("https://www.dba.dk" + href
                        if href.startswith("/") else href or "https://www.dba.dk")
            results.append(boat(
                title=title, model=model_label, year=None,
                price_orig=price_orig, currency=cur,
                hours=None, engine=None,
                country="DK", city=None,
                url=full_url, source_name="DBA.dk",
            ))
    return results


# ─── Auto24.ee (EE) ──────────────────────────────────────────────────────────

def scrape_auto24():
    results = []
    for model_label, queries in MODELS:
        q = queries[0]
        for url in [
            f"https://eng.auto24.ee/vehicles/list.php?a=110&otsi={q.replace(' ', '+')}",
            f"https://www.auto24.ee/kasutatud/nimekiri.php?a=110&otsi={q.replace(' ', '+')}",
        ]:
            html = get(url)
            if html:
                break
        if not html:
            continue
        soup = BeautifulSoup(html, "lxml")
        for item in soup.select(".result-item, tr.result, .aditem, li[class*=item]"):
            title_el = item.select_one("h2, h3, .title, td.title, [class*=title]")
            price_el = item.select_one("[class*=price], td.price")
            link_el = item.select_one("a[href]")
            if not title_el:
                continue
            title = title_el.get_text(" ", strip=True)
            if not any(qv in title.lower() for qv in queries):
                continue
            price_orig, cur = parse_price(price_el.get_text() if price_el else "")
            href = link_el.get("href", "") if link_el else ""
            full_url = ("https://eng.auto24.ee" + href
                        if href.startswith("/") else href or url)
            results.append(boat(
                title=title, model=model_label, year=None,
                price_orig=price_orig, currency=cur or "EUR",
                hours=None, engine=None,
                country="EE", city="Tallinn",
                url=full_url, source_name="Auto24.ee",
            ))
    return results


# ─── Venepörssi (FI) ─────────────────────────────────────────────────────────

def scrape_veneporssi():
    results = []
    for model_label, queries in MODELS:
        q = queries[0]
        html = get(f"https://www.veneporssi.fi/haku?q={q.replace(' ', '+')}")
        if not html:
            continue
        soup = BeautifulSoup(html, "lxml")
        for item in soup.select("article, .ad-item, li[class*=listing]"):
            title_el = item.select_one("h2, h3, [class*=title]")
            price_el = item.select_one("[class*=price]")
            link_el = item.select_one("a[href]")
            if not title_el:
                continue
            title = title_el.get_text(" ", strip=True)
            if not any(qv in title.lower() for qv in queries):
                continue
            price_orig, cur = parse_price(price_el.get_text() if price_el else "")
            href = link_el.get("href", "") if link_el else ""
            full_url = ("https://www.veneporssi.fi" + href
                        if href.startswith("/") else href)
            results.append(boat(
                title=title, model=model_label, year=None,
                price_orig=price_orig, currency=cur or "EUR",
                hours=None, engine=None,
                country="FI", city=None,
                url=full_url, source_name="Venepörssi",
            ))
    return results


# ─── Mascus (FI/Baltic) ───────────────────────────────────────────────────────

def scrape_mascus():
    results = []
    for model_label, queries in MODELS:
        q = queries[0]
        html = get(f"https://www.mascus.com/search#q={q.replace(' ', '%20')}&type=boats")
        if not html:
            continue
        soup = BeautifulSoup(html, "lxml")
        for item in soup.select("article, .search-result-item, li[class*=item]"):
            title_el = item.select_one("h2, h3, [class*=title]")
            price_el = item.select_one("[class*=price]")
            link_el = item.select_one("a[href]")
            if not title_el:
                continue
            title = title_el.get_text(" ", strip=True)
            if not any(qv in title.lower() for qv in queries):
                continue
            price_orig, cur = parse_price(price_el.get_text() if price_el else "")
            href = link_el.get("href", "") if link_el else ""
            full_url = ("https://www.mascus.com" + href
                        if href.startswith("/") else href)
            results.append(boat(
                title=title, model=model_label, year=None,
                price_orig=price_orig, currency=cur or "EUR",
                hours=None, engine=None,
                country=None, city=None,
                url=full_url, source_name="Mascus",
            ))
    return results


# ─── Veetehnika.ee (EE) ───────────────────────────────────────────────────────

def scrape_veetehnika():
    results = []
    for model_label, queries in MODELS:
        q = queries[0]
        html = get(f"https://eng.veetehnika.ee/vehicles/list.php?a=110&otsi={q.replace(' ', '+')}")
        if not html:
            continue
        soup = BeautifulSoup(html, "lxml")
        for item in soup.select(".result-item, tr.result, [class*=aditem]"):
            title_el = item.select_one("h2, h3, [class*=title], td.title")
            price_el = item.select_one("[class*=price], td.price")
            link_el = item.select_one("a[href]")
            if not title_el:
                continue
            title = title_el.get_text(" ", strip=True)
            if not any(qv in title.lower() for qv in queries):
                continue
            price_orig, cur = parse_price(price_el.get_text() if price_el else "")
            href = link_el.get("href", "") if link_el else ""
            full_url = ("https://eng.veetehnika.ee" + href
                        if href.startswith("/") else href or "https://eng.veetehnika.ee")
            results.append(boat(
                title=title, model=model_label, year=None,
                price_orig=price_orig, currency=cur or "EUR",
                hours=None, engine=None,
                country="EE", city=None,
                url=full_url, source_name="Veetehnika.ee",
            ))
    return results


# ─── Dedup ───────────────────────────────────────────────────────────────────

def dedup(boats_list):
    """Keep first occurrence per (normalized_title, year) or URL path."""
    seen_urls = set()
    seen_title_year = set()
    out = []
    for b in boats_list:
        url_key = None
        for s in b.get("sources", []):
            u = s.get("url", "")
            parts = u.rstrip("/").split("/")
            if parts and len(parts[-1]) > 3:
                url_key = parts[-1]
                break
        title_key = (
            re.sub(r"\s+", " ", b.get("title", "").lower().strip()),
            b.get("year"),
        )
        if url_key and url_key in seen_urls:
            continue
        if title_key[0] and title_key in seen_title_year:
            continue
        if url_key:
            seen_urls.add(url_key)
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
        # Use lambda so JSON backslashes aren't misread as regex backreferences
        html = re.sub(
            re.escape(MARKER_START) + r".*?" + re.escape(MARKER_END),
            lambda _: block,
            html,
            flags=re.DOTALL,
        )
    else:
        # First run: insert just before the closing </script> of the main script block
        html = html.replace(
            "// Initial render\nrenderTable();",
            f"{block}\n\n// Initial render\nrenderTable();",
        )
    path.write_text(html, encoding="utf-8")
    log.info("Wrote %d boats to %s (last scraped: %s)", len(boats_list), html_path, now)


# ─── Main ────────────────────────────────────────────────────────────────────

SCRAPERS = [
    ("Blocket",      scrape_blocket),
    ("Scanboat",     scrape_scanboat),
    ("Boat24",       scrape_boat24),
    ("Nettivene",    scrape_nettivene),
    ("Tori.fi",      scrape_tori),
    ("DBA.dk",       scrape_dba),
    ("Auto24.ee",    scrape_auto24),
    ("Venepörssi",   scrape_veneporssi),
    ("Veetehnika",   scrape_veetehnika),
    ("Mascus",       scrape_mascus),
]


def main():
    all_boats = []
    for name, fn in SCRAPERS:
        log.info("Scraping %s ...", name)
        try:
            found = fn()
            log.info("  → %d listings", len(found))
            all_boats.extend(found)
        except Exception as exc:
            log.error("  → FAILED: %s", exc)
        time.sleep(1.5)

    boats = dedup(all_boats)
    log.info("Total after dedup: %d", len(boats))

    # Save raw JSON for debugging
    Path("boats_scraped.json").write_text(
        json.dumps(boats, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    inject(boats)


if __name__ == "__main__":
    main()
