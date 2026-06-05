# Flipper 880ST / 900ST – Marknadsöversikt

Automatisk bevakningssida för begagnade Flipper 880ST och 900ST i Norden och Baltikum.

## Vad gör detta?

- **`index.html`** – interaktiv sida med sortering, filtrering och CSV-export
- **`scraper.py`** – scrapar 10 båtmarknadsplatser dagligen efter Flipper 880ST/900ST
- **GitHub Actions** – kör scrapern automatiskt kl. 07:00 CEST varje dag

## Scrapar automatiskt

| Sajt | Land |
|------|------|
| Blocket | 🇸🇪 Sverige |
| Scanboat | 🇸🇪🇩🇰🇫🇮 Norden |
| Boat24 | 🌍 Europa |
| Nettivene | 🇫🇮 Finland |
| Tori.fi | 🇫🇮 Finland |
| DBA.dk | 🇩🇰 Danmark |
| Auto24.ee | 🇪🇪 Estland |
| Venepörssi | 🇫🇮 Finland |
| Veetehnika.ee | 🇪🇪 Estland |
| Mascus | 🌍 Baltikum |

## Lokal körning

```bash
pip install -r requirements.txt
python scraper.py
```

Öppna sedan `index.html` i webbläsaren.
