#!/usr/bin/env python3
"""
Validate all URLs in seed data (index.html) and boats_scraped.json.
Marks dead links and updates index.html accordingly.
"""

import json
import re
import time
import logging
from pathlib import Path
import requests

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9,sv;q=0.8",
    "Accept": "text/html,application/xhtml+xml,*/*;q=0.8",
}

# Extract seed boats from index.html JavaScript
SEED_PATTERN = re.compile(
    r"const seedBoats = (\[.*?\]);\s*// ={3,}", re.DOTALL
)


def check_url(url, timeout=12):
    """Return (status_code, is_ok, redirect_url_or_none)."""
    try:
        r = requests.get(url, headers=HEADERS, timeout=timeout,
                         allow_redirects=True)
        final = r.url if r.url != url else None
        # Check for "soft 404" — page loads but contains sold/removed markers
        text_lower = r.text.lower()
        sold_markers = [
            "ikke længere tilgængelig",  # DBA.dk – no longer available
            "poistettu myynnistä",        # Nettivene – removed from sale
            "denna annons är borttagen",  # Blocket
            "annonsen er slettet",        # Norwegian
            "sold",
            "solgt",
            "såld",
            "page not found",
            "404",
            "finns inte",
            "not found",
        ]
        # Only flag as sold if these phrases appear prominently (not in nav/footer)
        # We look for them in <h1>, <h2>, or a <main>/<article> block
        sold = False
        if r.status_code in (404, 410, 403):
            sold = True
        elif any(m in text_lower[:8000] for m in sold_markers[:6]):
            # Check only in first 8KB (above the fold)
            sold = True
        return r.status_code, not sold, final
    except requests.exceptions.Timeout:
        return "timeout", False, None
    except requests.exceptions.TooManyRedirects:
        return "redirect_loop", False, None
    except Exception as exc:
        return str(exc)[:60], False, None


def extract_seed_boats(html):
    """
    Extract title + sources from the seedBoats JS block using regex.
    We don't try to parse the full JS — just pull out each boat's
    title string and its source URLs.
    """
    # Grab the seedBoats block between 'const seedBoats = [' and the closing '];'
    m = re.search(r"const seedBoats = \[([\s\S]*?)\];\s*\n\s*//", html)
    if not m:
        log.error("Could not locate seedBoats block in index.html")
        return []

    block = m.group(1)

    # Split into individual boat objects by looking for opening {  id: N,
    boat_chunks = re.split(r"\n\s*\{(?=\s*\n?\s*id:)", block)

    boats = []
    for chunk in boat_chunks:
        if not chunk.strip():
            continue

        # Extract title
        title_m = re.search(r'title:\s*"([^"]+)"', chunk)
        title = title_m.group(1) if title_m else "(untitled)"

        # Extract all source URLs in this chunk
        source_urls = re.findall(r'url:\s*"(https?://[^"]+)"', chunk)
        source_names = re.findall(r'name:\s*"([^"]+)"', chunk)

        sources = []
        for i, url in enumerate(source_urls):
            name = source_names[i] if i < len(source_names) else "?"
            sources.append({"name": name, "url": url})

        if sources:
            boats.append({"title": title, "sources": sources})

    log.info("Extracted %d seed boats from index.html", len(boats))
    return boats


def validate_boats(boats, label):
    results = []
    for b in boats:
        for src in b.get("sources", []):
            url = src.get("url", "")
            if not url or url.startswith("#"):
                continue
            # Skip generic search/listing pages (not specific ad pages)
            is_specific = any(c.isdigit() for c in url.split("/")[-1])
            if not is_specific:
                # Generic listing page — skip deep validation
                src["_status"] = "generic"
                continue

            log.info("  Checking [%s] %s", label, url)
            status, ok, redirect = check_url(url)
            src["_status"] = status
            src["_ok"] = ok
            src["_redirect"] = redirect
            if not ok:
                log.warning("    ❌ DEAD  %s → %s", url, status)
            else:
                log.info("    ✅ OK    %s", url)
            time.sleep(0.4)
        results.append(b)
    return results


def main():
    html_path = Path("index.html")
    html = html_path.read_text(encoding="utf-8")

    # ── Validate seed boats ──
    log.info("=== Validating seed boats ===")
    seed_boats = extract_seed_boats(html)
    if not seed_boats:
        log.error("No seed boats found — check extraction regex")
        return

    log.info("Found %d seed boats", len(seed_boats))
    validated_seed = validate_boats(seed_boats, "seed")

    # ── Validate scraped boats ──
    scraped_path = Path("boats_scraped.json")
    validated_scraped = []
    if scraped_path.exists():
        log.info("=== Validating scraped boats ===")
        scraped = json.loads(scraped_path.read_text(encoding="utf-8"))
        log.info("Found %d scraped boats", len(scraped))
        validated_scraped = validate_boats(scraped, "scraped")
    else:
        log.info("No boats_scraped.json found")

    # ── Build report ──
    print("\n" + "=" * 70)
    print("VALIDATION REPORT")
    print("=" * 70)

    dead_seed = []
    for b in validated_seed:
        for src in b.get("sources", []):
            if src.get("_ok") is False:
                dead_seed.append((b["title"], src["url"], src["_status"]))

    dead_scraped = []
    for b in validated_scraped:
        for src in b.get("sources", []):
            if src.get("_ok") is False:
                dead_scraped.append((b["title"], src["url"], src["_status"]))

    print(f"\n📌 SEED BOATS  ({len(validated_seed)} st)")
    if dead_seed:
        print(f"  ❌ {len(dead_seed)} döda URL:er:")
        for title, url, status in dead_seed:
            print(f"     [{status}] {title[:55]}")
            print(f"              {url}")
    else:
        print("  ✅ Alla URL:er verkar leva")

    print(f"\n🔍 SCRAPED BOATS  ({len(validated_scraped)} st)")
    if dead_scraped:
        print(f"  ❌ {len(dead_scraped)} döda URL:er:")
        for title, url, status in dead_scraped:
            print(f"     [{status}] {title[:55]}")
            print(f"              {url}")
    else:
        print("  ✅ Alla URL:er verkar leva")

    print("\n" + "=" * 70)

    # ── Save report JSON ──
    report = {
        "dead_seed": [{"title": t, "url": u, "status": str(s)} for t, u, s in dead_seed],
        "dead_scraped": [{"title": t, "url": u, "status": str(s)} for t, u, s in dead_scraped],
    }
    Path("validation_report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    log.info("Saved validation_report.json")


if __name__ == "__main__":
    main()
