"""Brzi pokretac za miss_sync.py iz istog foldera.

Ne menja originalnu skriptu. Ponovo koristi jednu Chrome sesiju po radniku
umesto da za svaki proizvod pokrece i gasi novi Chrome proces.
"""

from __future__ import annotations

import atexit
import importlib.util
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from selenium.webdriver.common.by import By
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait


ORIGINAL_SCRIPT = Path(__file__).resolve().with_name("miss_sync.py")


def _load_original_script():
    if not ORIGINAL_SCRIPT.is_file():
        raise FileNotFoundError(f"Originalna skripta nije pronadjena: {ORIGINAL_SCRIPT}")

    spec = importlib.util.spec_from_file_location("miss_sync_original", ORIGINAL_SCRIPT)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Ne mogu da ucitam: {ORIGINAL_SCRIPT}")

    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


sync = _load_original_script()

_thread_state = threading.local()
_drivers = []
_drivers_lock = threading.Lock()

# Shopify REST Admin API na ovom nalogu dozvoljava oko 2 poziva u sekundi.
# Originalna skripta nema obradu statusa 429, pa ga pogresno prikazuje kao
# "product not found". Svi Shopify pozivi zato prolaze kroz jedan limiter.
_shopify_rate_lock = threading.Lock()
_shopify_last_call = 0.0
_SHOPIFY_MIN_INTERVAL = 0.60
_raw_requests_get = sync.requests.get
_raw_requests_post = sync.requests.post
_raw_requests_put = sync.requests.put


def _is_shopify_admin_url(url) -> bool:
    value = str(url or "")
    return value.startswith(sync.SHOPIFY_STORE_URL) and "/admin/api/" in value


def _shopify_limited_request(raw_request, method: str, url, **kwargs):
    if not _is_shopify_admin_url(url):
        return raw_request(url, **kwargs)

    global _shopify_last_call
    response = None

    for attempt in range(1, 7):
        with _shopify_rate_lock:
            delay = _SHOPIFY_MIN_INTERVAL - (time.monotonic() - _shopify_last_call)
            if delay > 0:
                time.sleep(delay)
            _shopify_last_call = time.monotonic()
            response = raw_request(url, **kwargs)

        if response.status_code != 429:
            return response

        try:
            retry_after = float(response.headers.get("Retry-After", "1.5"))
        except (TypeError, ValueError):
            retry_after = 1.5

        retry_after = max(retry_after, 1.5)
        print(
            f"[SHOPIFY LIMIT] 429 na {method}; cekam {retry_after:.1f}s "
            f"i pokusavam ponovo ({attempt}/6)...",
            flush=True,
        )
        time.sleep(retry_after)

    return response


def _install_shopify_rate_limit():
    sync.requests.get = lambda url, **kwargs: _shopify_limited_request(
        _raw_requests_get, "GET", url, **kwargs
    )
    sync.requests.post = lambda url, **kwargs: _shopify_limited_request(
        _raw_requests_post, "POST", url, **kwargs
    )
    sync.requests.put = lambda url, **kwargs: _shopify_limited_request(
        _raw_requests_put, "PUT", url, **kwargs
    )


def _new_driver():
    options = sync.Options()
    options.add_argument("--headless=new")
    options.add_argument("--disable-gpu")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--disable-extensions")
    options.add_argument("--disable-background-networking")
    options.add_argument("--disable-default-apps")
    options.add_argument("--disable-features=Translate")
    options.add_argument("--blink-settings=imagesEnabled=false")
    options.add_argument("--log-level=3")
    options.page_load_strategy = "eager"

    driver = sync.webdriver.Chrome(options=options)
    driver.set_page_load_timeout(25)
    driver.set_script_timeout(15)

    with _drivers_lock:
        _drivers.append(driver)

    _thread_state.driver = driver
    return driver


def _get_driver():
    driver = getattr(_thread_state, "driver", None)
    return driver if driver is not None else _new_driver()


def _discard_thread_driver():
    driver = getattr(_thread_state, "driver", None)
    _thread_state.driver = None
    if driver is None:
        return

    with _drivers_lock:
        if driver in _drivers:
            _drivers.remove(driver)

    try:
        driver.quit()
    except Exception:
        pass


def _close_all_drivers():
    with _drivers_lock:
        remaining = list(_drivers)
        _drivers.clear()

    for driver in remaining:
        try:
            driver.quit()
        except Exception:
            pass


atexit.register(_close_all_drivers)


def fetch_html_fast(url: str) -> str:
    """Ucitaj proizvod uz ponovnu upotrebu Chrome sesije."""
    last_error = None

    for attempt in range(2):
        driver = _get_driver()
        try:
            driver.get(url)
            WebDriverWait(driver, 7).until(
                EC.presence_of_element_located(
                    (
                        By.CSS_SELECTOR,
                        ".wcboost-variation-swatches__item, "
                        "select#pa_velicina, form.variations_form, p.stock",
                    )
                )
            )
            # Kratak prostor da JavaScript upise dostupnost varijacija u DOM.
            time.sleep(0.6)
            return driver.page_source or ""
        except Exception as exc:
            last_error = exc
            _discard_thread_driver()
            if attempt == 0:
                time.sleep(0.5)

    # Ako Chrome dva puta ne uspe, ovaj proizvod ne blokira celu obradu.
    try:
        response = sync.SESSION.get(url, timeout=sync.REQUEST_TIMEOUT)
        response.raise_for_status()
        print(f"[WARN] Chrome nije uspeo; requests fallback -> {url}", flush=True)
        return response.text
    except Exception as exc:
        print(f"[WARN] Preskacem neucitanu stranicu {url}: {last_error or exc}", flush=True)
        return ""


def scrape_categories_with_progress(categories, max_workers=4, log_print=print):
    all_links = set()

    for category in categories:
        log_print(f"[SCRAPE] Ucitavam linkove iz {category}...")
        links = sync.get_all_product_links_from_category(category, log_print=log_print)
        log_print(f"[SCRAPE] Nadjeno {len(links)} proizvoda")
        all_links.update(links)

    links = list(all_links)
    total = len(links)
    log_print(f"[SCRAPE] Ukupno jedinstvenih proizvoda: {total}")
    log_print(f"[SCRAPE] Obrada proizvoda sa {max_workers} stalne Chrome sesije...")

    rows = []
    try:
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            futures = {executor.submit(sync.scrape_product, url): url for url in links}
            for done, future in enumerate(as_completed(futures), 1):
                url = futures[future]
                try:
                    row = future.result()
                    rows.append(row)
                    title = row.get("Title") or url
                    log_print(f"[PRODUCT] {done}/{total} zavrseno -> {title}")
                except Exception as exc:
                    log_print(f"[ERR] {done}/{total} {url}: {exc}")
    finally:
        _close_all_drivers()

    return rows


def main():
    # Original koristi relativnu putanju za CSV; zadrzavamo ga uz originalnu skriptu.
    os.chdir(ORIGINAL_SCRIPT.parent)
    _install_shopify_rate_limit()
    sync.fetch_html = fetch_html_fast
    sync.scrape_categories_parallel = scrape_categories_with_progress
    sync.main()


if __name__ == "__main__":
    main()
