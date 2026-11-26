import json
import os
import re
import time
from datetime import datetime, timezone
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup
from playwright.sync_api import sync_playwright

MIN_DISCOUNT_SAVE = int(os.getenv("MIN_DISCOUNT", "50"))
MAX_PAGES_KARKKAINEN = int(os.getenv("MAX_PAGES_KARKKAINEN", "15"))

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "fi-FI,fi;q=0.9,en;q=0.8",
}


def parse_price(text: str) -> float:
    if not text:
        return 0.0
    m = re.search(r"(\d[\d\s]*[.,]\d{2}|\d+)", text.replace("\xa0", " "))
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

    # 1. Kokeile WooCommerce Store API
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
                    link = p.get("permalink", "")
                    if disc >= MIN_DISCOUNT_SAVE and link not in seen:
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

    # 2. Varavaihtoehto: Outlet HTML -sivut
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
                link = link_el["href"] if link_el else ""
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


def scrape_karkkainen() -> list[dict]:
    items = []
    seen = set()
    base_urls = [
        "https://www.karkkainen.com/verkkokauppa/outlet-tuotteet",
        "https://www.karkkainen.com/verkkokauppa/black-friday",
    ]

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(
            user_agent=HEADERS["User-Agent"],
            locale="fi-FI",
            viewport={"width": 1280, "height": 900},
        )
        page = context.new_page()

        for base_url in base_urls:
            for page_idx in range(MAX_PAGES_KARKKAINEN):
                offset = page_idx * 48
                url = base_url if offset == 0 else f"{base_url}?offset={offset}"
                print(f"[Kärkkäinen] Haetaan: {url}")
                try:
                    page.goto(url, wait_until="domcontentloaded", timeout=35000)
                    page.wait_for_timeout(2500)

                    # Poimi tuotekortit suoraan DOM:sta yleisellä JS-skriptillä
                    extracted = page.evaluate("""() => {
                        const results = [];
                        const anchors = Array.from(document.querySelectorAll('a[href*="/verkkokauppa/"]'));
                        const cardSet = new Set();

                        for (const a of anchors) {
                            const card = a.closest('article, li, [class*="product"], [class*="Product"], [class*="card"], [class*="Card"]') || a.parentElement;
                            if (!card || cardSet.has(card)) continue;
                            const text = (card.innerText || '').trim();
                            if (!text.includes('€')) continue;
                            cardSet.add(card);

                            const href = a.href || '';
                            const imgEl = card.querySelector('img');
                            const img = imgEl ? (imgEl.currentSrc || imgEl.src || imgEl.getAttribute('data-src') || '') : '';
                            const titleEl = card.querySelector('h2, h3, h4, [class*="name"], [class*="Name"], [class*="title"], [class*="Title"]');
                            const name = titleEl ? titleEl.innerText.trim() : (a.getAttribute('title') || text.split('\\n')[0] || '').trim();

                            results.push({ href, name, img, text });
                        }
                        return results;
                    }""")

                    if not extracted:
                        print(f"[Kärkkäinen] Ei tuotteita sivulla offset={offset}, lopetetaan osio.")
                        break

                    found_on_page = 0
                    for raw in extracted:
                        link = urljoin("https://www.karkkainen.com", raw["href"])
                        if not raw["name"] or link in seen:
                            continue

                        text = raw["text"].replace("\xa0", " ")
                        # Etsi kaikki eurohinnat kortista
                        price_matches = re.findall(r"(\d[\d\s]*[.,]\d{2})\s*€", text)
                        prices = sorted(set(parse_price(pm) for pm in price_matches if parse_price(pm) > 0))

                        disc_match = re.search(r"-\s*(\d{1,2})\s*%", text)
                        disc = int(disc_match.group(1)) if disc_match else 0

                        sale, reg = 0.0, 0.0
                        if len(prices) >= 2:
                            sale = prices[0]
                            reg = prices[-1]
                            if disc == 0 and reg > sale:
                                disc = round((1 - sale / reg) * 100)
                        elif len(prices) == 1 and disc > 0:
                            sale = prices[0]
                            reg = round(sale / (1 - disc / 100), 2)

                        if disc >= MIN_DISCOUNT_SAVE and sale > 0:
                            seen.add(link)
                            found_on_page += 1
                            in_stock = not bool(re.search(r"loppu|ei saatavilla", text, re.I))
                            items.append({
                                "shop": "Kärkkäinen",
                                "name": raw["name"][:140],
                                "disc": disc,
                                "sale": round(sale, 2),
                                "reg": round(reg, 2),
                                "inStock": in_stock,
                                "link": link,
                                "img": raw["img"],
                            })

                    print(f"[Kärkkäinen] Löydetty {found_on_page} uutta ≥{MIN_DISCOUNT_SAVE}% tuotetta (yht. {len(items)})")
                except Exception as e:
                    print(f"[Kärkkäinen] Virhe sivulla {url}: {e}")
                    break

        browser.close()

    print(f"[Kärkkäinen] Valmis: {len(items)} tuotetta")
    return items


def main():
    emc_items = scrape_emc24()
    kark_items = scrape_karkkainen()

    all_items = emc_items + kark_items
    all_items.sort(key=lambda x: (-x["disc"], x["sale"]))

    payload = {
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "counts": {
            "EMC24": len(emc_items),
            "Kärkkäinen": len(kark_items),
            "total": len(all_items),
        },
        "items": all_items,
    }

    with open("deals.json", "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)

    print(f"Tallennettu deals.json: yhteensä {len(all_items)} tuotetta.")


if __name__ == "__main__":
    main()
