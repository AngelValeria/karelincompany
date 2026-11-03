"""Расчёт: на сколько дней хватит остатка и какую партию везти."""
import math
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone

from loguru import logger

from ozon_client import OzonClient, period


@dataclass
class ProductStock:
    sku: int
    item_code: str
    item_name: str
    total_stock: int
    avg_daily_sales: float
    days_remaining: float          # math.inf, если продаж не было
    optimal_batch: int
    warehouses: list[dict] = field(default_factory=list)


@dataclass
class AnalysisResult:
    low_stock_items: list[ProductStock]
    all_items: list[ProductStock]
    checked_at: datetime
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        def p(x: ProductStock):
            d = asdict(x)
            d["days_remaining"] = None if math.isinf(x.days_remaining) else x.days_remaining
            return d
        return {"low": [p(x) for x in self.low_stock_items], "all": [p(x) for x in self.all_items],
                "checked_at": self.checked_at.isoformat(), "notes": self.notes}

    @staticmethod
    def from_dict(d: dict) -> "AnalysisResult":
        def p(x):
            x = dict(x)
            x["days_remaining"] = math.inf if x.get("days_remaining") is None else x["days_remaining"]
            return ProductStock(**x)
        return AnalysisResult([p(x) for x in d.get("low", [])], [p(x) for x in d.get("all", [])],
                              datetime.fromisoformat(d["checked_at"]), d.get("notes", []))


def build_items(stock_rows: list[dict], sales: dict[int, float], threshold: float, supply_days: int) -> AnalysisResult:
    by_sku: dict[int, ProductStock] = {}
    for r in stock_rows:
        sku = int(r["sku"])
        if not sku:
            continue
        it = by_sku.get(sku)
        if not it:
            it = by_sku[sku] = ProductStock(sku, r["item_code"], r["item_name"], 0, 0.0, math.inf, 0, [])
        it.total_stock += int(r["free_to_sell_amount"])
        it.warehouses.append({"warehouse_id": r.get("warehouse_id"), "warehouse_name": r["warehouse_name"],
                              "free_to_sell_amount": int(r["free_to_sell_amount"]), "promised_amount": int(r.get("promised_amount", 0))})
    for it in by_sku.values():
        it.avg_daily_sales = float(sales.get(it.sku, 0.0))
        it.days_remaining = it.total_stock / it.avg_daily_sales if it.avg_daily_sales > 0 else math.inf
        it.optimal_batch = math.ceil(it.avg_daily_sales * supply_days)
    items = sorted(by_sku.values(), key=lambda x: x.days_remaining)
    low = [x for x in items if x.days_remaining < threshold]
    return AnalysisResult(low, items, datetime.now(timezone.utc))


class StockAnalyzer:
    CACHE_SECONDS = 30 * 60

    def __init__(self, client: OzonClient, threshold: float, supply_days: int, period_days: int = 30):
        self.client, self.threshold, self.supply_days, self.period_days = client, threshold, supply_days, period_days
        self._cache: tuple[float, AnalysisResult] | None = None

    async def analyze(self, force: bool = False) -> AnalysisResult:
        if not force and self._cache and time.time() - self._cache[0] < self.CACHE_SECONDS:
            return self._cache[1]
        rows = await self.client.get_stocks_by_warehouse()
        skus = sorted({int(r["sku"]) for r in rows if r["sku"]})
        d_from, d_to = period(self.period_days)
        sales = await self.client.get_sales_analytics(d_from, d_to, skus, self.period_days)
        res = build_items(rows, sales, self.threshold, self.supply_days)
        res.notes = list(dict.fromkeys(self.client.notes))
        logger.info(f"товаров {len(res.all_items)}, мало остатка {len(res.low_stock_items)}")
        self._cache = (time.time(), res)
        return res
