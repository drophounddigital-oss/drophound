"""Stock monitors — the core engine.

`SampleMonitor` simulates retailer stock changes so the whole system runs
end-to-end with no network (the default). `HttpMonitor` is the real, optional
fetcher: it polls a product page and looks for the configured in-stock signal,
matching the plan's Phase-1 'watch the top retailer pages' approach in code.

A monitor's job is narrow: observe current state and emit `Observation`s. The
pipeline decides what is a meaningful *change* and whether to alert.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from random import Random
from typing import Any, Sequence

from ..config import Settings

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124 Safari/537.36")


@dataclass
class Observation:
    product_id: int
    status: str                      # in_stock | low_stock | sold_out
    price: float | None = None
    event_type: str | None = None    # set by simulators; derived by pipeline if None
    note: str = ""
    source: str = "monitor"


def _g(product: Any, key: str, default: Any = None) -> Any:
    try:
        return product[key]
    except (KeyError, IndexError, TypeError):
        return getattr(product, key, default)


class SampleMonitor:
    """Simulate realistic stock churn across the catalog (offline, default).

    Pass a fixed `seed` for deterministic output (used in tests). Without one,
    each cycle produces fresh pseudo-random churn so a running loop feels alive.
    """

    name = "sample"

    def __init__(self, seed: int | None = None):
        self.rng = Random(seed)

    def check(self, products: Sequence[Any]) -> list[Observation]:
        obs: list[Observation] = []
        for p in products:
            pid = _g(p, "id")
            status = _g(p, "current_status") or "in_stock"
            retail = _g(p, "retail_price")
            roll = self.rng.random()

            if status == "sold_out":
                if roll < 0.35:
                    obs.append(Observation(pid, "in_stock", retail, "restock",
                                           "Back in stock", "sample"))
            elif status in ("in_stock", "low_stock"):
                if roll < 0.14:
                    obs.append(Observation(pid, "sold_out", None, "sold_out",
                                           "Sold out", "sample"))
                elif roll < 0.22:
                    obs.append(Observation(pid, "low_stock", retail, "low_stock",
                                           "Low stock", "sample"))
                elif roll < 0.30 and retail:
                    obs.append(Observation(pid, "in_stock", round(retail * 0.85, 2),
                                           "price_drop", "Price drop", "sample"))
        return obs


class HttpMonitor:
    """Real monitor: fetch each product page and detect the in-stock signal.

    Optional and best-effort — any network/parse failure for a product is
    swallowed and simply yields no observation for that product. The pipeline
    compares the observed status to the last known status to decide on a restock.
    """

    name = "http"

    def __init__(self, settings: Settings, *, client: Any | None = None):
        self.settings = settings
        self._client = client  # injectable for tests

    def _fetch(self, url: str) -> str:
        if self._client is not None:
            return self._client.get(url).text
        import httpx  # local import keeps httpx optional at import time
        with httpx.Client(timeout=15.0, follow_redirects=True,
                          headers={"User-Agent": "DropHoundBot/0.1"}) as client:
            return client.get(url).text

    def _fetch_json(self, url: str) -> dict:
        if self._client is not None:
            return self._client.get(url).json()
        import httpx
        with httpx.Client(timeout=15.0, follow_redirects=True,
                          headers={"User-Agent": "DropHoundBot/0.1"}) as client:
            return client.get(url).json()

    def _shopify_available(self, url: str) -> bool | None:
        """Shopify exposes a public product object at <product-url>.js (the AJAX
        endpoint) with authoritative `available` flags — the cleanest, ToS-friendly
        stock source. (The sibling .json endpoint omits `available`, so use .js.)
        Returns True/False, or None if this isn't a readable Shopify product."""
        base = url.split("?")[0].rstrip("/")
        if "/products/" not in base:
            return None
        try:
            data = self._fetch_json(base + ".js")  # top-level IS the product
            if "available" in data:
                return bool(data["available"])
            variants = data.get("variants", [])
            if not variants:
                return None
            return any(bool(v.get("available")) for v in variants)
        except Exception:
            return None

    def _observe(self, url: str, signal: str | None) -> str | None:
        avail = self._shopify_available(url)
        if avail is not None:
            return "in_stock" if avail else "sold_out"
        try:
            html = self._fetch(url).lower()
        except Exception:
            return None
        sold_out = any(s in html for s in ("sold out", "out of stock", "notify me"))
        # Only assert a status with positive evidence. A JavaScript-rendered page
        # (e.g. Pop Mart) shows neither marker in raw HTML -> stay silent rather
        # than false-alarm a "sold out".
        if signal and signal.lower() in html and not sold_out:
            return "in_stock"
        if sold_out:
            return "sold_out"
        return None

    def check(self, products: Sequence[Any]) -> list[Observation]:
        obs: list[Observation] = []
        for p in products:
            url = _g(p, "product_url")
            if not url:
                continue
            status = self._observe(url, _g(p, "in_stock_signal"))
            if status:
                obs.append(Observation(_g(p, "id"), status, _g(p, "retail_price"),
                                       None, "", "http"))
            if self.settings.http_delay:
                time.sleep(self.settings.http_delay)
        return obs


class ShopifyStoreMonitor:
    """Efficient monitor for many Shopify products.

    Instead of one request per product, it fetches each store's public
    /products.json once (paginated) and derives availability for every watched
    product from that store by matching the URL handle. Ideal for a large watch
    list spread across a handful of Shopify stores.
    """

    name = "shopify"

    def __init__(self, settings: Settings, *, client: Any | None = None, max_pages: int = 8):
        self.settings = settings
        self._client = client
        self.max_pages = max_pages

    def _get_json(self, url: str) -> dict:
        if self._client is not None:
            return self._client.get(url).json()
        import httpx
        with httpx.Client(timeout=25.0, follow_redirects=True,
                          headers={"User-Agent": UA}) as c:
            return c.get(url).json()

    def _store_availability(self, domain: str) -> dict[str, bool] | None:
        avail: dict[str, bool] = {}
        try:
            for page in range(1, self.max_pages + 1):
                data = self._get_json(f"https://{domain}/products.json?limit=250&page={page}")
                prods = data.get("products", [])
                if not prods:
                    break
                for p in prods:
                    handle = p.get("handle")
                    if handle:
                        avail[handle] = any(v.get("available") for v in p.get("variants", []))
                if len(prods) < 250:
                    break
                if self.settings.http_delay:
                    time.sleep(self.settings.http_delay)
        except Exception:
            return avail or None
        return avail or None

    def check(self, products: Sequence[Any]) -> list[Observation]:
        from urllib.parse import urlparse

        groups: dict[str, list] = {}
        for p in products:
            url = _g(p, "product_url")
            if not url or "/products/" not in url:
                continue
            domain = urlparse(url).netloc
            handle = url.split("/products/", 1)[1].split("?")[0].strip("/").split("/")[0]
            groups.setdefault(domain, []).append((p, handle))

        obs: list[Observation] = []
        for domain, items in groups.items():
            amap = self._store_availability(domain)
            if not amap:
                continue
            for p, handle in items:
                available = amap.get(handle)
                if available is None:
                    continue
                obs.append(Observation(_g(p, "id"),
                                       "in_stock" if available else "sold_out",
                                       _g(p, "retail_price"), None, "", "shopify"))
        return obs
