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
    note: str = ""               # для WB: условия приёмки


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


# ---------- Wildberries: склад выбираем по коэффициенту приёмки ----------
HUBS = ["Коледино", "Электросталь", "Казань", "Краснодар", "Екатеринбург"]


def acceptance_by_warehouse(coefs: list[dict], days: int = 7) -> dict[str, dict]:
    """Минимальный коэффициент приёмки коробов по каждому складу на ближайшие дни."""
    from datetime import datetime, timedelta, timezone
    limit = datetime.now(timezone.utc) + timedelta(days=days)
    out: dict[str, dict] = {}
    for c in coefs:
        if c.get("boxTypeID") not in (None, 2) and "короб" not in str(c.get("boxTypeName", "")).lower():
            continue
        try:
            d = datetime.fromisoformat(str(c.get("date", "")).replace("Z", "+00:00"))
        except ValueError:
            continue
        if d > limit:
            continue
        name, k = str(c.get("warehouseName", "")), float(c.get("coefficient", -1))
        cur = out.setdefault(name, {"name": name, "min": None, "free_date": None})
        if k >= 0 and (cur["min"] is None or k < cur["min"]):
            cur["min"] = k
        if k == 0 and (cur["free_date"] is None or d.date().isoformat() < cur["free_date"]):
            cur["free_date"] = d.date().isoformat()
    return out


def wb_rank(acc: dict[str, dict]) -> list[dict]:
    open_ = [a for a in acc.values() if a["min"] is not None]
    hub = lambda n: next((i for i, h in enumerate(HUBS) if h.lower() in n.lower()), len(HUBS))
    return sorted(open_, key=lambda a: (0 if a["min"] == 0 else 1 if a["min"] <= 2 else 2, hub(a["name"]), a["min"]))


def build_wb_plans(analysis: AnalysisResult, coefs: list[dict]) -> list[SupplyPlan]:
    ranked = wb_rank(acceptance_by_warehouse(coefs)) if coefs else []
    if not ranked:
        plans = WarehouseAdvisor().build_supply_plans(analysis, [])
        for p in plans:
            p.comment = "Коэффициенты приёмки WB не получены: склад выберите в кабинете WB"
        return plans
    plans = []
    for p in analysis.low_stock_items:
        top = ranked[:2]
        parts = [p.optimal_batch] if len(top) == 1 else [math.ceil(p.optimal_batch * 0.6), p.optimal_batch - math.ceil(p.optimal_batch * 0.6)]
        recs = []
        for a, u in zip(top, parts):
            note = "Приёмка: бесплатно" if a["min"] == 0 else f"Приёмка: коэф. {a['min']:g}" + (f", бесплатно с {a['free_date']}" if a["free_date"] else "")
            recs.append(WarehouseRecommendation(None, a["name"], u, 0, None, 0.6 if u == parts[0] else 0.4, math.inf, note))
        urg = "КРИТИЧНО" if p.total_stock == 0 else "СРОЧНО" if p.days_remaining < 3 else "ПЛАНОВОЕ"
        plans.append(SupplyPlan(p, [r for r in recs if r.units_to_send > 0], p.optimal_batch, 0, "Срочность: " + urg))
    return plans


def acceptance_text(coefs: list[dict]) -> str:
    acc = acceptance_by_warehouse(coefs)
    if not acc:
        return "Коэффициенты приёмки WB не получены. Добавьте токену WB категорию «Поставки»."
    free = [a["name"] for a in wb_rank(acc) if a["min"] == 0]
    paid = [a for a in wb_rank(acc) if a["min"] and a["min"] > 0]
    closed = [a["name"] for a in acc.values() if a["min"] is None]
    out = ["🏭 Приёмка коробов на складах WB, ближайшие 7 дней", "", "✅ Бесплатно:"] + ([f"  • {n}" for n in free[:15]] or ["  нет"])
    out += ["", "💰 Платно:"] + [f"  • {a['name']}: коэф. {a['min']:g}" for a in paid[:12]]
    out += ["", f"🔴 Закрыто складов: {len(closed)}"]
    return "\n".join(out)
