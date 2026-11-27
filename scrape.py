import json
import os
import re
import time
from datetime import datetime, timezone
from urllib.parse import urljoin, urlsplit, urlunsplit

import requests
from bs4 import BeautifulSoup
from playwright.sync_api import sync_playwright

MIN_DISCOUNT_SAVE = int(os.getenv("MIN_DISCOUNT", "50"))
MAX_PAGES_KARKKAINEN = int(os.getenv("MAX_PAGES_KARKKAINEN", "15"))
MAX_PAGES_OTHERS = 8

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "fi-FI,fi;q=0.9,en;q=0.8",
}


def clean_url(url: str) -> str:
    parts = urlsplit(url)
    return urlunsplit((parts.scheme, parts.netloc, parts.path.rstrip("/"), "", ""))


def parse_price(text: str) -> float:
    if not text:
        return 0.0
    m = re.search(r"(\d[\d\s]*[.,]\d{2}|\d[\d\s]*)", text.replace("\xa0", " "))
    if not m:
        return 0.0
    val = m.group(1).replace(" ", "").replace(",", ".")
    try:
        return float(val)
    except ValueError:
        return 0.0


def scrape_emc24() -> list[dict]:
    items = []
    seen = set()

    page = 1
    api_ok = False
    while page <= 80:
        url = (
            f"https://www.emc24.fi/wp-json/wc/store/v1/products"
            f"?on_sale=true&per_page=100&page={page}"
        )
        try:
            r = requests.get(url, headers=HEADERS, timeout=25)
            if r.status_code != 200:
                break
            data = r.json()
            if not isinstance(data, list) or not data:
                api_ok = True
                break
            api_ok = True
            for p in data:
                prices = p.get("prices") or {}
                minor = 10 ** int(prices.get("currency_minor_unit", 2))
                reg = int(prices.get("regular_price") or 0) / minor
                sale = int(prices.get("sale_price") or prices.get("price") or 0) / minor
                if reg > 0 and 0 < sale < reg:
                    disc = round((1 - sale / reg) * 100)
                    link = clean_url(p.get("permalink", ""))
                    if disc >= MIN_DISCOUNT_SAVE and link and link not in seen:
                        seen.add(link)
                        imgs = p.get("images") or []
                        items.append({
                            "shop": "EMC24",
                            "name": p.get("name", ""),
                            "disc": disc,
                            "sale": round(sale, 2),
                            "reg": round(reg, 2),
                            "inStock": bool(p.get("is_in_stock", True)),
                            "link": link,
                            "img": (imgs[0].get("thumbnail") or imgs[0].get("src", "")) if imgs else "",
                        })
            if len(data) < 100:
                break
            page += 1
            time.sleep(0.25)
        except Exception as e:
            print(f"[EMC24 API] Virhe sivulla {page}: {e}")
            break

    if api_ok and items:
        print(f"[EMC24] API valmis: {len(items)} tuotetta")
        return items

    print("[EMC24] Siirrytään Outlet HTML -hakuun...")
    for p_num in range(1, 50):
        url = f"https://www.emc24.fi/tuote-osasto/outlet/page/{p_num}/"
        try:
            r = requests.get(url, headers=HEADERS, timeout=25)
            if r.status_code == 404:
                break
            soup = BeautifulSoup(r.text, "html.parser")
            cards = soup.select("li.product")
            if not cards:
                break
            for card in cards:
                badge = card.select_one(".onsale, .badge")
                del_el = card.select_one("del")
                ins_el = card.select_one("ins")
                reg = parse_price(del_el.get_text(" ", strip=True) if del_el else "")
                sale = parse_price(ins_el.get_text(" ", strip=True) if ins_el else "")
                disc = 0
                if reg > 0 and 0 < sale < reg:
                    disc = round((1 - sale / reg) * 100)
                elif badge:
                    m = re.search(r"-(\d+)\s*%", badge.get_text(" ", strip=True))
                    if m:
                        disc = int(m.group(1))
                if disc < MIN_DISCOUNT_SAVE:
                    continue
                link_el = card.select_one("a[href]")
                title_el = card.select_one("h2, .woocommerce-loop-product__title")
                img_el = card.select_one("img")
                link = clean_url(link_el["href"]) if link_el else ""
                if not link or link in seen:
                    continue
                seen.add(link)
                items.append({
                    "shop": "EMC24",
                    "name": title_el.get_text(strip=True) if title_el else "",
                    "disc": disc,
                    "sale": round(sale, 2),
                    "reg": round(reg, 2),
                    "inStock": "outofstock" not in " ".join(card.get("class", [])),
                    "link": link,
                    "img": (img_el.get("data-src") or img_el.get("src") or "") if img_el else "",
                })
            time.sleep(0.3)
        except Exception as e:
            print(f"[EMC24 HTML] Virhe sivulla {p_num}: {e}")
            break

    print(f"[EMC24] HTML valmis: {len(items)} tuotetta")
    return items


def extract_cards_from_page(page, link_selector: str) -> list[dict]:
    return page.evaluate(
        """(sel) => {
            const results = [];
            const anchors = Array.from(document.querySelectorAll(sel));
            const cardSet = new Set();

            for (const a of anchors) {
                const card = a.closest('article, li, [class*="product"], [class*="Product"], [class*="card"], [class*="Card"], [class*="item"], [class*="Item"]') || a.parentElement;
                if (!card || cardSet.has(card)) continue;
                const text = (card.innerText || '').trim();
                if (!text.includes('€') && !text.includes('EUR') && !text.includes(',-')) continue;
                cardSet.add(card);

                const href = a.href || '';
                const imgEl = card.querySelector('img');
                const img = imgEl ? (imgEl.currentSrc || imgEl.src || imgEl.getAttribute('data-src') || '') : '';
                const titleEl = card.querySelector('h2, h3, h4, [class*="name"], [class*="Name"], [class*="title"], [class*="Title"]');
                const name = titleEl ? titleEl.innerText.trim() : (a.getAttribute('title') || text.split('\\n')[0] || '').trim();

                results.push({ href, name, img, text });
            }
            return results;
        }""",
        link_selector,
    )


def parse_extracted_items(extracted: list[dict], shop_name: str, base_domain: str, seen_links: set, seen_names: set) -> list[dict]:
    found = []
    for raw in extracted:
        link = clean_url(urljoin(base_domain, raw["href"]))
        name = raw["name"][:140].strip()
        name_key = name.lower()

        if not name or not link or link in seen_links or name_key in seen_names:
            continue

        text = raw["text"].replace("\xa0", " ")

        # Etsi hinnat muodoista: 19,90 €, 199 €, 199,-
        price_matches = re.findall(r"(\d[\d\s]*[.,]\d{2})\s*€|(\d[\d\s]*)\s*(?:€|,-)", text)
        prices = []
        for m1, m2 in price_matches:
            val = parse_price(m1 or m2)
            if val > 0:
                prices.append(val)
        prices = sorted(set(prices))

        # Etsi myös "Säästä XX €" tai "-XX %"
        disc_match = re.search(r"-\s*(\d{1,2})\s*%", text)
        disc = int(disc_match.group(1)) if disc_match else 0

        save_match = re.search(r"säästä\s*(\d[\d\s]*(?:[.,]\d{2})?)", text, re.I)
        saved_eur = parse_price(save_match.group(1)) if save_match else 0.0

        sale, reg = 0.0, 0.0
        if len(prices) >= 2:
            # Jos "Säästä X €" oli mukana hinnoissa, suodatetaan se tarvittaessa
            sale = prices[0]
            reg = prices[-1]
            if saved_eur > 0 and abs(sale - saved_eur) < 0.05 and len(prices) >= 3:
                sale = prices[1]
            if disc == 0 and reg > sale:
                disc = round((1 - sale / reg) * 100)
        elif len(prices) == 1:
            sale = prices[0]
            if disc > 0:
                reg = round(sale / (1 - disc / 100), 2)
            elif saved_eur > 0:
                reg = round(sale + saved_eur, 2)
                disc = round((saved_eur / reg) * 100)

        if disc >= MIN_DISCOUNT_SAVE and sale > 0:
            seen_links.add(link)
            seen_names.add(name_key)
            in_stock = not bool(re.search(r"loppu|ei saatavilla|sold out", text, re.I))
            found.append({
                "shop": shop_name,
                "name": name,
                "disc": disc,
                "sale": round(sale, 2),
                "reg": round(reg, 2),
                "inStock": in_stock,
                "link": link,
                "img": raw["img"],
            })
    return found


def scrape_playwright_shops() -> dict[str, list[dict]]:
    shop_configs = [
        {
            "name": "Kärkkäinen",
            "domain": "https://www.karkkainen.com",
            "selector": 'a[href*="/verkkokauppa/"]',
            "max_pages": MAX_PAGES_KARKKAINEN,
            "urls": [
                ("https://www.karkkainen.com/verkkokauppa/outlet-tuotteet", "offset", 48),
                ("https://www.karkkainen.com/verkkokauppa/black-friday", "offset", 48),
            ],
        },
        {
            "name": "Verkkokauppa.com",
            "domain": "https://www.verkkokauppa.com",
            "selector": 'a[href*="/fi/product/"]',
            "max_pages": 3,
            "urls": [
                ("https://www.verkkokauppa.com/fi/blackfriday", "page", 1),
            ],
        },
        {
            "name": "Jula",
            "domain": "https://www.jula.fi",
            "selector": 'a[href*="/catalog/"], main a[href]',
            "max_pages": MAX_PAGES_OTHERS,
            "urls": [
                ("https://www.jula.fi/tarjoukset/black-friday/", "page", 1),
            ],
        },
        {
            "name": "Rusta",
            "domain": "https://www.rusta.com",
            "selector": 'a[href*="/fi-fi/"], main a[href]',
            "max_pages": MAX_PAGES_OTHERS,
            "urls": [
                ("https://www.rusta.com/fi-fi/kampanja/black-week", "page", 1),
            ],
        },
    ]

    results_by_shop = {cfg["name"]: [] for cfg in shop_configs}

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(
            user_agent=HEADERS["User-Agent"],
            locale="fi-FI",
            viewport={"width": 1280, "height": 900},
        )
        page = context.new_page()

        for cfg in shop_configs:
            shop = cfg["name"]
            seen_links = set()
            seen_names = set()
            max_pages = cfg.get("max_pages", MAX_PAGES_OTHERS)

            for base_url, param_name, step in cfg["urls"]:
                empty_streak = 0
                for idx in range(max_pages):
                    if idx == 0:
                        url = base_url
                    elif param_name == "offset":
                        url = f"{base_url}?offset={idx * step}"
                    else:
                        sep = "&" if "?" in base_url else "?"
                        url = f"{base_url}{sep}{param_name}={idx + 1}"

                    print(f"[{shop}] Haetaan: {url}")
                    try:
                        page.goto(url, wait_until="domcontentloaded", timeout=35000)
                        page.wait_for_timeout(2500)
                        page.evaluate("window.scrollTo(0, document.body.scrollHeight / 2)")
                        page.wait_for_timeout(800)

                        extracted = extract_cards_from_page(page, cfg["selector"])
                        if idx == 0 and not extracted:
                            print(f"[{shop} DEBUG] Title: {page.title()!r} | URL: {page.url}")
                            break

                        new_items = parse_extracted_items(extracted, shop, cfg["domain"], seen_links, seen_names)
                        results_by_shop[shop].extend(new_items)
                        print(f"[{shop}] Kortteja: {len(extracted)}, uusia ≥{MIN_DISCOUNT_SAVE}%: {len(new_items)} (yht. {len(results_by_shop[shop])})")

                        if idx == 0 and extracted and len(new_items) == 0:
                            sample = extracted[0]["text"].replace("\n", " | ")[:160]
                            print(f"[{shop} SAMPLE] {sample}")

                        if len(new_items) == 0:
                            empty_streak += 1
                            if empty_streak >= 2:
                                print(f"[{shop}] 2 tyhjää sivua peräkkäin, lopetetaan osio.")
                                break
                        else:
                            empty_streak = 0
                    except Exception as e:
                        print(f"[{shop}] Virhe sivulla {url}: {e}")
                        break

        browser.close()

    return results_by_shop


def main():
    emc_items = scrape_emc24()
    pw_results = scrape_playwright_shops()

    all_items = list(emc_items)
    counts = {"EMC24": len(emc_items)}

    for shop_name, items in pw_results.items():
        all_items.extend(items)
        counts[shop_name] = len(items)

    all_items.sort(key=lambda x: (-x["disc"], x["sale"]))
    counts["total"] = len(all_items)

    payload = {
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "counts": counts,
        "items": all_items,
    }

    with open("deals.json", "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)

    print(f"Tallennettu deals.json: {counts}")


if __name__ == "__main__":
    main()
    
