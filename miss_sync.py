import csv
import json
import os
import re
import time
import subprocess
from html import unescape
from urllib.parse import urlparse
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests
from bs4 import BeautifulSoup

# --- selenium imports ---
from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
# ------------------------

APP_VERSION = "v3.2-terminal (update all exact-title duplicates)"
SHOPIFY_API_VERSION = "2025-01"

DEFAULT_WP_CATEGORIES = [
    "https://cocoluxuryfashion.com/product-category/uncategorized/muskarci/obuca-uskoro/tenisice/",
    "https://cocoluxuryfashion.com/product-category/uncategorized/muskarci/modni-dodatci-uskoro-muskarci/torbe/",
    "https://cocoluxuryfashion.com/product-category/uncategorized/zene/obuca/tenisice-obuca/",
    "https://cocoluxuryfashion.com/product-category/uncategorized/zene/torbe-i-modni-dodatci-uskoro/torbe-i-ruksaci/",
]

DEFAULT_IN_STOCK_QTY = 100000
DEFAULT_CSV_PATH = "wp_products_export.csv"
DEFAULT_MAX_WORKERS = max(1, int(os.environ.get("MISS_SYNC_WORKERS", "4")))
REQUEST_TIMEOUT = 18

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/119.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.8,hr;q=0.7,sr;q=0.7",
}

# ========== SHOPIFY CREDENTIALS ==========
SHOPIFY_STORE_URL = os.environ.get(
    "SHOPIFY_STORE_URL",
    "https://780q0s-0u.myshopify.com",
).rstrip("/")
SHOPIFY_TOKEN = os.environ.get(
    "SHOPIFY_TOKEN",
    "",
).strip()
SHOPIFY_LOCATION_ID = os.environ.get("SHOPIFY_LOCATION_ID", "83098501352").strip()
# ========================================

# === SKIP COLLECTION (Posljednji brojevi) ===
SKIP_COLLECTION_ID = "442590331112"
_COLLECTION_MEMBERSHIP_CACHE = {}
# ===========================================

SESSION = requests.Session()
SESSION.headers.update(HEADERS)


# =============================
# WINDOWS SHUTDOWN PROMPT
# =============================
def ask_shutdown_after() -> bool:
    if os.environ.get("GITHUB_ACTIONS") == "true" or os.environ.get("MISS_SYNC_NON_INTERACTIVE") == "1":
        return False
    ans = input("Da li želiš da se računar ugasi posle izvršenja skripte? (y/N): ").strip().lower()
    return ans == "y"


def shutdown_windows():
    subprocess.run("shutdown /s /t 0", shell=True, check=False)


# =============================
# HELPERS
# =============================
def _slug_from_url(url: str) -> str:
    path = urlparse(url).path
    slug = path.strip("/").split("/")[-1]
    return slug or url


def _bs(html):
    return BeautifulSoup(html, "html.parser")


def normalize_title_for_match(title: str) -> str:
    """
    Normalizuje naslov za TAČNO poređenje naziva proizvoda.
    Time 'Nike TN', ' nike   tn ' i 'NIKE TN' postaju isto.
    """
    title = unescape(title or "")
    title = re.sub(r"\s+", " ", title).strip().casefold()
    return title


def product_in_skip_collection(store_url: str, headers: dict, product_id, log=print) -> bool:
    """
    Proverava da li je proizvod u kolekciji/kategoriji koju preskačemo.
    Kešira rezultate da ne udara API stalno.
    """
    key = (str(product_id), str(SKIP_COLLECTION_ID))
    if key in _COLLECTION_MEMBERSHIP_CACHE:
        return _COLLECTION_MEMBERSHIP_CACHE[key]

    try:
        url = f"{store_url}/admin/api/{SHOPIFY_API_VERSION}/collects.json"
        params = {"product_id": str(product_id), "collection_id": str(SKIP_COLLECTION_ID), "limit": 1}
        r = requests.get(url, headers=headers, params=params, timeout=REQUEST_TIMEOUT)
        if r.status_code != 200:
            _COLLECTION_MEMBERSHIP_CACHE[key] = False
            return False

        collects = (r.json() or {}).get("collects", []) or []
        val = len(collects) > 0
        _COLLECTION_MEMBERSHIP_CACHE[key] = val
        return val
    except Exception:
        _COLLECTION_MEMBERSHIP_CACHE[key] = False
        return False


def get_all_shopify_products_by_exact_title(store_url: str, headers: dict, title: str, log=print) -> list:
    """
    NOVO:
    Vraća SVE Shopify proizvode koji imaju ISTI naslov kao prosleđeni title.
    Ne oslanja se samo na prvi batch rezultata.
    """
    search_url = f"{store_url}/admin/api/{SHOPIFY_API_VERSION}/products.json"
    wanted = normalize_title_for_match(title)
    matched = []
    seen_ids = set()
    since_id = 0

    while True:
        params = {
            "title": title,
            "limit": 250,
        }
        if since_id:
            params["since_id"] = since_id

        r = requests.get(search_url, headers=headers, params=params, timeout=REQUEST_TIMEOUT)
        if r.status_code != 200:
            log(f"[ERR] Cannot find product '{title}': {r.text}")
            break

        batch = (r.json() or {}).get("products", []) or []
        if not batch:
            break

        for product in batch:
            pid = product.get("id")
            if pid in seen_ids:
                continue
            seen_ids.add(pid)

            product_title = product.get("title", "")
            if normalize_title_for_match(product_title) == wanted:
                matched.append(product)

        last_id = batch[-1].get("id")
        if len(batch) < 250 or not last_id:
            break

        since_id = last_id

    return matched


def enable_inventory_tracking_for_variant(store_url: str, headers: dict, variant_id, log=print):
    patch_url = f"{store_url}/admin/api/{SHOPIFY_API_VERSION}/variants/{variant_id}.json"
    patch_payload = {"variant": {"id": variant_id, "inventory_management": "shopify"}}
    try:
        requests.put(patch_url, headers=headers, json=patch_payload, timeout=REQUEST_TIMEOUT)
    except Exception as e:
        log(f"[WARN] inventory_management patch failed for variant {variant_id}: {e}")


# =============================
# CATEGORY SCRAPE
# =============================
def get_all_product_links_from_category(category_url: str, log_print=print) -> list:
    links = set()
    page = 1
    last_count = 0

    while True:
        url = f"{category_url.rstrip('/')}/?paged={page}"
        try:
            r = SESSION.get(url, timeout=REQUEST_TIMEOUT)
            if r.status_code >= 400:
                break

            soup = _bs(r.text)
            anchors = soup.select("ul.products li.product a.woocommerce-LoopProduct-link")
            if not anchors:
                anchors = soup.select("li.product a[href*='/proizvod/'], li.product a[href*='/product/']")
            if not anchors:
                break

            pre = len(links)
            for a in anchors:
                href = a.get("href")
                if href:
                    links.add(href)

            post = len(links)
            log_print(f"[PAGE {page}] +{post - pre} (total {post})")

            if post == last_count:
                break

            last_count = post
            page += 1

        except Exception as e:
            log_print(f"[WARN] page {page}: {e}")
            break

    return list(links)


# =============================
# PRODUCT PARSERS
# =============================
def fetch_html(url: str) -> str:
    """
    Prvo pokušava preko selenium/Chrome (da bi uhvatio JS-varijante),
    ako pukne – pada nazad na običan requests.
    """
    driver = None
    try:
        options = Options()
        options.add_argument("--headless=new")
        options.add_argument("--disable-gpu")
        options.add_argument("--no-sandbox")
        options.add_argument("--disable-dev-shm-usage")

        driver = webdriver.Chrome(options=options)
        driver.set_page_load_timeout(30)
        driver.get(url)

        try:
            WebDriverWait(driver, 4).until(
                EC.presence_of_element_located((
                    By.CSS_SELECTOR,
                    ".wcboost-variation-swatches__item, select#pa_velicina, form.variations_form"
                ))
            )
        except Exception:
            time.sleep(1.5)

        return driver.page_source

    except Exception:
        try:
            r = SESSION.get(url, timeout=REQUEST_TIMEOUT)
            r.raise_for_status()
            return r.text
        except Exception:
            return ""

    finally:
        if driver is not None:
            try:
                driver.quit()
            except Exception:
                pass


def extract_title(soup: BeautifulSoup, url: str) -> str:
    t = soup.select_one("h1.product_title, h1.entry-title, h1")
    return (t.get_text(strip=True) if t else _slug_from_url(url))


def extract_tags(soup: BeautifulSoup) -> set:
    tags = set()
    for a in soup.select('.product_meta .tagged_as a, .product_meta a[rel="tag"], .tagged_as a'):
        txt = a.get_text(strip=True).lower()
        if txt:
            tags.add(txt)
    return tags


def extract_categories(soup: BeautifulSoup) -> set:
    cats = set()
    for a in soup.select('.posted_in a, nav.woocommerce-breadcrumb a, .breadcrumbs a'):
        txt = a.get_text(strip=True).lower()
        if txt:
            cats.add(txt)
    return cats


def detect_tip(tags: set, cats: set, sizes_preview: list) -> str:
    if "tenisice" in tags:
        return "patika"
    if "torbe" in tags:
        return "torba"

    for c in cats:
        if any(w in c for w in ["patike", "tenisice", "sneakers", "obuća", "obuca"]):
            return "patika"
        if any(w in c for w in ["torbe", "bags", "bag"]):
            return "torba"

    if sizes_preview:
        nums = [s for s in sizes_preview if re.search(r"\d", s)]
        if len(nums) >= 2:
            return "patika"

    return "nepoznato"


def parse_variations_from_form(soup: BeautifulSoup) -> list:
    form = soup.select_one('form.variations_form[data-product_variations]')
    out = []
    if not form:
        return out

    raw = form.get('data-product_variations')
    if not raw:
        return out

    try:
        raw = unescape(raw)
        data = json.loads(raw)
        for v in data:
            attrs = v.get('attributes') or {}
            size_key = next((k for k in attrs.keys() if 'velicina' in k.lower()), None)
            size = attrs.get(size_key) if size_key else None

            if not size:
                size = (v.get('option1') or v.get('variationDescription') or '').strip()

            if not size:
                continue

            in_stock = bool(v.get('is_in_stock', True)) and (v.get('max_qty', 1) != 0)
            out.append({"size": str(size).strip(), "in_stock": in_stock})
    except Exception:
        pass

    return out


def parse_sizes_from_swatches(soup: BeautifulSoup) -> list:
    out = []
    for li in soup.select('.wcboost-variation-swatches__item'):
        label = li.get('data-value') or li.get('aria-label') or li.get_text(strip=True)
        if not label:
            continue

        classes = ' '.join(li.get('class') or []).lower()
        in_stock = ('disabled' not in classes) and ('out-of-stock' not in classes)
        out.append({"size": label.strip(), "in_stock": in_stock})

    return out


def parse_sizes_from_select(soup: BeautifulSoup) -> list:
    out = []
    for opt in soup.select('select#pa_velicina option, select[name*="velicina"] option'):
        val = opt.get_text(strip=True)
        if not val or 'odaberi' in val.lower():
            continue

        disabled = opt.has_attr('disabled')
        out.append({"size": val, "in_stock": not disabled})

    return out


def bag_availability_from_soup(soup: BeautifulSoup) -> bool:
    ln = soup.select_one('link[itemprop="availability"]')
    if ln:
        href = (ln.get('href') or '').lower()
        if 'instock' in href:
            return True
        if 'outofstock' in href:
            return False

    for sc in soup.find_all('script', attrs={'type': 'application/ld+json'}):
        try:
            data = json.loads(sc.string or '{}')
        except Exception:
            continue

        def find_av(node):
            if isinstance(node, dict):
                off = node.get('offers')
                if off is None and '@graph' in node:
                    return find_av(node['@graph'])

                if isinstance(off, list):
                    for o in off:
                        a = (o.get('availability') or '').lower()
                        if 'instock' in a:
                            return True
                        if 'outofstock' in a:
                            return False

                if isinstance(off, dict):
                    a = (off.get('availability') or '').lower()
                    if 'instock' in a:
                        return True
                    if 'outofstock' in a:
                        return False

            if isinstance(node, list):
                for it in node:
                    r = find_av(it)
                    if r is not None:
                        return r

            return None

        r = find_av(data)
        if r is not None:
            return r

    stock_el = soup.select_one('p.stock')
    if stock_el:
        classes = ' '.join(stock_el.get('class', [])).lower()
        text = (stock_el.get_text(strip=True) or '').lower()

        if 'out-of-stock' in classes or 'rasprodan' in text or 'rasprodano' in text:
            return False
        if 'in-stock' in classes or 'na zalihi' in text or 'dostupno' in text or 'available' in text:
            return True

    btn = soup.select_one('button.single_add_to_cart_button')
    if btn:
        disabled = btn.has_attr('disabled') or ('disabled' in (btn.get('class') or []))
        return not disabled

    return False


def scrape_product(url: str) -> dict:
    try:
        html = fetch_html(url)
    except Exception:
        return {"Title": _slug_from_url(url), "Tip": "nepoznato", "Stanje": "", "sizes_map": [], "url": url}

    soup = _bs(html)
    title = extract_title(soup, url)
    tags = extract_tags(soup)
    cats = extract_categories(soup)

    sizes_preview_src = parse_sizes_from_swatches(soup) or parse_sizes_from_select(soup)
    preview = [s['size'] for s in sizes_preview_src]
    tip = detect_tip(tags, cats, preview)

    if tip == 'torba':
        available = bag_availability_from_soup(soup)
        stanje = 'dostupno' if available else 'nije dostupno'
        sizes_map = [{"size": "ONE", "in_stock": available}]
        return {"Title": title, "Tip": "torba", "Stanje": stanje, "sizes_map": sizes_map, "url": url}

    sizes_map = parse_variations_from_form(soup)
    if not sizes_map:
        sizes_map = parse_sizes_from_swatches(soup)
    if not sizes_map:
        sizes_map = parse_sizes_from_select(soup)

    if sizes_map:
        stanje_items = [f"{s['size']} - {'ima' if s['in_stock'] else 'nema'}" for s in sizes_map]
        stanje = "; ".join(stanje_items)
        return {"Title": title, "Tip": "patika", "Stanje": stanje, "sizes_map": sizes_map, "url": url}

    available = bag_availability_from_soup(soup)
    stanje = 'dostupno' if available else 'nije dostupno'
    sizes_map = [{"size": "ONE", "in_stock": available}]
    return {"Title": title, "Tip": "torba", "Stanje": stanje, "sizes_map": sizes_map, "url": url}


# =============================
# CSV UTIL
# =============================
def write_csv(rows, path):
    with open(path, 'w', newline='', encoding='utf-8') as f:
        keys = ["Title", "Tip", "Stanje", "url"]
        writer = csv.DictWriter(f, fieldnames=keys)
        writer.writeheader()
        for r in rows:
            writer.writerow({k: r.get(k, '') for k in keys})


def read_csv(path):
    out = []
    try:
        with open(path, newline='', encoding='utf-8') as f:
            reader = csv.DictReader(f)
            for row in reader:
                out.append(row)
    except Exception:
        pass
    return out


# =============================
# PARALLEL SCRAPE
# =============================
def scrape_categories_parallel(categories, max_workers=6, log_print=print):
    all_links = set()

    for cat in categories:
        log_print(f"[SCRAPE] Učitavam linkove iz {cat}…")
        links = get_all_product_links_from_category(cat, log_print=log_print)
        log_print(f"[SCRAPE] Nađeno {len(links)} proizvoda")
        all_links.update(links)

    rows = []
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        future_to_url = {executor.submit(scrape_product, url): url for url in all_links}
        for future in as_completed(future_to_url):
            try:
                rows.append(future.result())
            except Exception as e:
                log_print(f"[ERR] {e}")

    return rows


# =============================
# DRY-RUN HELPER
# =============================
def parse_stanje_field(stanje_field, tip='patika'):
    if tip == 'torba':
        return 'dostupno' in (stanje_field or '').lower()

    out = {}
    for part in (stanje_field.split(";") if stanje_field else []):
        m = re.match(r"(.+)\s*-\s*(ima|nema)", part.strip(), re.I)
        if m:
            sz, val = m.groups()
            out[sz.strip().lower()] = val.lower() == 'ima'

    return out


# =============================
# SHOPIFY UPDATE (real)
# =============================
def normalize_size(s):
    """
    Normalizuje veličine tako da WP veličine tipa '37-23.5cm' ili '38 EU'
    budu prepoznate kao '37' i '38'.
    """
    if not s:
        return ""

    s = s.strip().lower()
    s = re.sub(r"(cm|eu|eur|us|uk)", "", s)
    s = s.replace(",", ".").replace("-", " ").strip()

    m = re.search(r"\d+(?:\.\d+)?", s)
    if m:
        return m.group(0).strip()

    if "one" in s or "uni" in s:
        return "one"

    return re.sub(r"[^0-9a-z]+", "", s)


def update_shopify_from_csv_row(store_url, token, location_id, in_stock_qty, row, log=print):
    """
    Sync za patike / NE-torbe.
    NOVO: ažurira SVE proizvode sa ISTIM naslovom, ne samo jedan batch.
    """
    headers = {
        "X-Shopify-Access-Token": token,
        "Content-Type": "application/json"
    }

    title = (row.get("Title") or "").strip()
    stanje = row.get("Stanje") or ""
    tip = (row.get("Tip") or "").lower()

    if not title:
        log("[WARN] Preskačem red bez naslova.")
        return

    if tip == "torba":
        log(f"[INFO] Preskačem torbu u patika-sync fazi: {title}")
        return

    products = get_all_shopify_products_by_exact_title(store_url, headers, title, log=log)
    if not products:
        log(f"[WARN] Product not found (exact title): {title}")
        return

    log(f"[INFO] Nađeno {len(products)} Shopify proizvoda sa naslovom '{title}'")

    sizes = parse_stanje_field(stanje, "patika")
    sizes = {normalize_size(k): v for k, v in sizes.items()}

    for product in products:
        if product_in_skip_collection(store_url, headers, product.get("id"), log=log):
            log(f"[SKIP] '{product.get('title')}' (id={product.get('id')}) je u kolekciji {SKIP_COLLECTION_ID} → preskačem update")
            continue

        log(f"[INFO] Updating product: {product.get('title')} (id={product.get('id')})")
        variants = product.get("variants", []) or []

        for v in variants:
            opt = normalize_size(v.get("option1") or "")
            if not opt:
                continue

            available = sizes.get(opt, False)
            qty = in_stock_qty if available else 0

            enable_inventory_tracking_for_variant(store_url, headers, v["id"], log=log)

            update_url = f"{store_url}/admin/api/{SHOPIFY_API_VERSION}/inventory_levels/set.json"
            payload = {
                "location_id": location_id,
                "inventory_item_id": v["inventory_item_id"],
                "available": qty
            }

            try:
                ur = requests.post(update_url, headers=headers, json=payload, timeout=REQUEST_TIMEOUT)
                if ur.status_code == 200:
                    log(f"  - {v.get('option1')}: {qty}")
                else:
                    log(f"[ERR] {title} / {v.get('option1')}: {ur.text}")
            except Exception as e:
                log(f"[ERR] {title} / {v.get('option1')}: {e}")


# =============================
# TORBE SYNC
# =============================
def parse_torba_qty(stanje: str) -> int:
    """
    'dostupno' → IN_STOCK_QTY, sve ostalo → 0
    """
    stanje = (stanje or "").strip().lower()
    return DEFAULT_IN_STOCK_QTY if stanje == "dostupno" else 0


def update_torbe_from_csv(
    store_url: str,
    token: str,
    location_id: str,
    csv_path: str = DEFAULT_CSV_PATH,
    log=print
):
    headers = {
        "X-Shopify-Access-Token": token,
        "Content-Type": "application/json"
    }

    try:
        with open(csv_path, newline='', encoding='utf-8') as f:
            reader = csv.DictReader(f)
            rows = [row for row in reader if (row.get("Tip") or "").strip().lower() == "torba"]
    except FileNotFoundError:
        log(f"[TORBE] CSV nije pronađen: {csv_path}")
        return

    log(f"[TORBE] Pronađeno torbi u CSV: {len(rows)}")

    for row in rows:
        title = (row.get("Title") or "").strip()
        qty = parse_torba_qty(row.get("Stanje"))

        if not title:
            continue

        products = get_all_shopify_products_by_exact_title(store_url, headers, title, log=log)
        if not products:
            log(f"[WARN] Torba nije pronađena na Shopify (exact title): {title}")
            continue

        log(f"[TORBE] Nađeno {len(products)} Shopify proizvoda sa naslovom '{title}'")

        for product in products:
            if product_in_skip_collection(store_url, headers, product.get("id"), log=log):
                log(f"[SKIP] TORBA '{product.get('title')}' (id={product.get('id')}) je u kolekciji {SKIP_COLLECTION_ID} → preskačem update")
                continue

            variants = product.get("variants", []) or []
            for v in variants:
                inventory_item_id = v.get("inventory_item_id")
                if not inventory_item_id:
                    continue

                enable_inventory_tracking_for_variant(store_url, headers, v["id"], log=log)

                update_url = f"{store_url}/admin/api/{SHOPIFY_API_VERSION}/inventory_levels/set.json"
                payload = {
                    "location_id": location_id,
                    "inventory_item_id": inventory_item_id,
                    "available": qty
                }

                try:
                    ur = requests.post(update_url, headers=headers, json=payload, timeout=REQUEST_TIMEOUT)
                    if ur.status_code == 200:
                        log(f"[OK] TORBA '{title}' (product {product.get('id')}, variant {v.get('id')}) → {qty}")
                    else:
                        log(f"[ERR] TORBA '{title}' → {ur.text}")
                except Exception as e:
                    log(f"[ERR] TORBA '{title}' → {e}")


# =============================
# AUTO TERMINAL PIPELINE
# =============================
def main():
    if not SHOPIFY_TOKEN:
        raise RuntimeError(
            "SHOPIFY_TOKEN nije podešen. Dodaj ga kao GitHub Actions secret "
            "ili kao environment promenljivu."
        )

    should_shutdown = ask_shutdown_after()
    success = False

    try:
        print("===============================================")
        print(f"  WP → Shopify Sync {APP_VERSION}")
        print("  (auto scrape → CSV → patike sync → torbe sync)")
        print("===============================================")

        # 1) SCRAPE WP KATEGORIJE → CSV
        print("[STEP 1] Pokrećem WP scraping...")
        rows = scrape_categories_parallel(
            DEFAULT_WP_CATEGORIES,
            max_workers=DEFAULT_MAX_WORKERS,
            log_print=print
        )
        write_csv(rows, DEFAULT_CSV_PATH)
        print(f"[STEP 1 DONE] Scraping završen. Sačuvano {len(rows)} proizvoda u {DEFAULT_CSV_PATH}")

        # 2) SYNC SHOPIFY – PATIKE
        print("[STEP 2] Pokrećem Shopify sync za patike (ažurira sve proizvode sa istim naslovom)...")
        csv_rows = read_csv(DEFAULT_CSV_PATH)
        for r in csv_rows:
            update_shopify_from_csv_row(
                SHOPIFY_STORE_URL,
                SHOPIFY_TOKEN,
                SHOPIFY_LOCATION_ID,
                DEFAULT_IN_STOCK_QTY,
                r,
                log=print
            )
        print("[STEP 2 DONE] Shopify patike sync završen.")

        # 3) SYNC SHOPIFY – TORBE
        print("[STEP 3] Pokrećem Shopify sync za torbe (ažurira sve proizvode sa istim naslovom)...")
        update_torbe_from_csv(
            SHOPIFY_STORE_URL,
            SHOPIFY_TOKEN,
            SHOPIFY_LOCATION_ID,
            DEFAULT_CSV_PATH,
            log=print
        )
        print("[STEP 3 DONE] Shopify torbe sync završen.")

        print("===============================================")
        print("[ALL DONE] Kompletna WP → Shopify sinhronizacija završena.")
        print("===============================================")

        success = True

    finally:
        if should_shutdown and success:
            print("[INFO] Gašenje računara je uključeno → gasim računar...")
            shutdown_windows()


if __name__ == "__main__":
    main()
