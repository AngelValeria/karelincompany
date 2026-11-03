"""Куда везти партию. Ozon не даёт свободные места складов через API, поэтому партия делится
пропорционально тому, сколько товара уже лежит на каждом складе (там, где он продаётся).
Если по складам ничего нет, партия делится поровну. Если места известны (free_limit), они учитываются."""
import math
from dataclasses import dataclass

from analytics import AnalysisResult, ProductStock


@dataclass
class WarehouseRecommendation:
    warehouse_id: int | None
    warehouse_name: str
    units_to_send: int
    current_stock: int
    free_limit: int | None        # None: Ozon не сообщил
    priority_score: float
    days_after_supply: float


@dataclass
class SupplyPlan:
    product: ProductStock
    recommendations: list[WarehouseRecommendation]
    total_to_send: int
    limit_shortage: int
    comment: str


class WarehouseAdvisor:
    def advise(self, product: ProductStock, available_warehouses: list[dict]) -> list[WarehouseRecommendation]:
        limits = {str(w.get("warehouse_id") or w.get("warehouse_name")): w.get("free_limit") for w in available_warehouses}
        whs = [w for w in product.warehouses if w.get("warehouse_name")]
        if not whs:
            return []
        total_now = sum(w["free_to_sell_amount"] + w.get("promised_amount", 0) for w in whs)
        shares = [((w["free_to_sell_amount"] + w.get("promised_amount", 0)) / total_now) if total_now else 1 / len(whs) for w in whs]
        batch = product.optimal_batch
        raw = [batch * s for s in shares]
        units = [math.floor(x) for x in raw]
        for i in sorted(range(len(raw)), key=lambda i: raw[i] - units[i], reverse=True)[: batch - sum(units)]:
            units[i] += 1
        recs = []
        for w, s, u in zip(whs, shares, units):
            lim = limits.get(str(w.get("warehouse_id") or w["warehouse_name"]))
            if lim is not None:
                u = min(u, int(lim))
            daily = product.avg_daily_sales * s
            after = (w["free_to_sell_amount"] + u) / daily if daily > 0 else math.inf
            recs.append(WarehouseRecommendation(w.get("warehouse_id"), w["warehouse_name"], u, w["free_to_sell_amount"],
                                                None if lim is None else int(lim), round(s, 3), after))
        return [r for r in recs if r.units_to_send > 0]

    def build_supply_plans(self, analysis: AnalysisResult, available_warehouses: list[dict] | None = None) -> list[SupplyPlan]:
        plans = []
        for p in analysis.low_stock_items:
            recs = self.advise(p, available_warehouses or [])
            sent = sum(r.units_to_send for r in recs)
            shortage = max(0, p.optimal_batch - sent) if any(r.free_limit is not None for r in recs) else 0
            known = any(r.free_limit is not None for r in recs)
            comment = "" if known else "Свободные места на складах проверьте в кабинете Ozon: через API их не видно"
            if shortage:
                comment = f"Не хватает мест на складах: {shortage} шт."
            plans.append(SupplyPlan(p, recs, sent, shortage, comment))
        return plans
