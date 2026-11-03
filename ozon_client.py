"""Клиент Ozon Seller API (только чтение).

Ozon время от времени меняет методы, поэтому для остатков и продаж есть запасные пути:
остатки:  /v2/analytics/stock_on_warehouses  →  /v4/product/info/stocks (без разбивки по складам)
продажи:  /v1/analytics/data (ordered_units)  →  /v2/posting/fbo/list (считаем товары в заказах FBO)
В лог пишутся только адреса и количества, без названий и цифр по товарам: логи GitHub публичные.
"""
import asyncio
from datetime import date, datetime, timedelta, timezone

import httpx
from loguru import logger

BASE_URL = "https://api-seller.ozon.ru"


class OzonError(Exception):
    pass


class OzonClient:
    def __init__(self, client_id: str, api_key: str, timeout: float = 60):
        self._http = httpx.AsyncClient(base_url=BASE_URL, timeout=timeout,
                                       headers={"Client-Id": client_id, "Api-Key": api_key, "Content-Type": "application/json"})
        self.notes: list[str] = []   # что пришлось делать запасным путём (показываем в отчёте)

    async def close(self):
        await self._http.aclose()

    async def _request(self, method: str, path: str, body: dict | None = None) -> dict:
        delay = 2.0
        for attempt in range(1, 4):
            try:
                r = await self._http.request(method, path, json=body)
            except httpx.HTTPError as e:
                logger.warning(f"{path}: сеть, попытка {attempt}: {type(e).__name__}")
                if attempt == 3:
                    raise OzonError(f"{path}: Ozon не отвечает ({type(e).__name__})") from e
                await asyncio.sleep(delay); delay *= 2; continue
            logger.info(f"{method} {path} → {r.status_code}")
            if r.status_code == 429 or r.status_code >= 500:
                if attempt == 3:
                    raise OzonError(f"{path}: Ozon ответил {r.status_code} после 3 попыток")
                wait = float(r.headers.get("Retry-After") or delay)
                await asyncio.sleep(min(wait, 60)); delay *= 2; continue
            if r.status_code >= 400:
                msg = r.text[:300]
                if r.status_code in (401, 403):
                    raise OzonError(f"{path}: нет доступа ({r.status_code}). Проверьте Client ID, ключ и его роль. {msg}")
                raise OzonError(f"{path}: ошибка {r.status_code}. {msg}")
            return r.json()
        raise OzonError(f"{path}: не удалось получить ответ")

    # ---------- остатки ----------
    async def get_stocks_by_warehouse(self) -> list[dict]:
        """Остатки по складам: item_code, item_name, sku, free_to_sell_amount, promised_amount, warehouse_name, warehouse_id."""
        try:
            rows, offset = [], 0
            while True:
                data = await self._request("POST", "/v2/analytics/stock_on_warehouses",
                                           {"limit": 1000, "offset": offset, "warehouse_type": "ALL"})
                page = (data.get("result") or {}).get("rows") or []
                for p in page:
                    rows.append({"item_code": str(p.get("item_code", "")), "item_name": str(p.get("item_name", "")),
                                 "sku": int(p.get("sku") or 0), "free_to_sell_amount": int(p.get("free_to_sell_amount") or 0),
                                 "promised_amount": int(p.get("promised_amount") or 0),
                                 "warehouse_name": str(p.get("warehouse_name", "")), "warehouse_id": p.get("warehouse_id")})
                if len(page) < 1000:
                    break
                offset += 1000
            logger.info(f"остатки по складам: строк {len(rows)}")
            return rows
        except OzonError as e:
            logger.warning(f"остатки по складам недоступны, беру общие остатки: {e}")
            self.notes.append("Ozon не отдал остатки по складам, показан общий остаток FBO")
            return await self._get_stocks_total()

    async def _get_stocks_total(self) -> list[dict]:
        rows, cursor = [], ""
        while True:
            data = await self._request("POST", "/v4/product/info/stocks",
                                       {"filter": {"visibility": "ALL"}, "limit": 1000, "cursor": cursor})
            items = data.get("items") or []
            for it in items:
                for st in it.get("stocks") or []:
                    if st.get("type") != "fbo":
                        continue
                    rows.append({"item_code": str(it.get("offer_id", "")), "item_name": str(it.get("offer_id", "")),
                                 "sku": int(st.get("sku") or 0), "free_to_sell_amount": max(0, int(st.get("present") or 0) - int(st.get("reserved") or 0)),
                                 "promised_amount": 0, "warehouse_name": "Все склады FBO", "warehouse_id": None})
            cursor = data.get("cursor") or ""
            if not items or not cursor:
                break
        logger.info(f"общие остатки: строк {len(rows)}")
        return rows

    # ---------- продажи ----------
    async def get_sales_analytics(self, date_from: str, date_to: str, sku_list: list[int], days: int) -> dict[int, float]:
        """Среднее заказов в день по SKU за период."""
        try:
            totals: dict[int, float] = {}
            offset = 0
            while True:
                data = await self._request("POST", "/v1/analytics/data", {
                    "date_from": date_from, "date_to": date_to, "metrics": ["ordered_units"],
                    "dimension": ["sku"], "filters": [], "limit": 1000, "offset": offset, "sort": []})
                page = (data.get("result") or {}).get("data") or []
                for row in page:
                    try:
                        sku = int(row["dimensions"][0]["id"])
                        totals[sku] = totals.get(sku, 0) + float(row["metrics"][0] or 0)
                    except (KeyError, IndexError, ValueError, TypeError):
                        continue
                if len(page) < 1000:
                    break
                offset += 1000
            logger.info(f"продажи из аналитики: SKU {len(totals)}")
        except OzonError as e:
            logger.warning(f"аналитика недоступна, считаю по заказам FBO: {e}")
            self.notes.append("Продажи посчитаны по заказам FBO: аналитика Ozon недоступна для этого ключа")
            totals = await self._sales_from_postings(date_from, date_to)
        wanted = set(sku_list)
        return {s: totals.get(s, 0.0) / days for s in wanted}

    async def _sales_from_postings(self, date_from: str, date_to: str) -> dict[int, float]:
        totals: dict[int, float] = {}
        offset = 0
        since = f"{date_from}T00:00:00Z"
        to = f"{date_to}T23:59:59Z"
        while True:
            data = await self._request("POST", "/v2/posting/fbo/list", {
                "dir": "ASC", "filter": {"since": since, "to": to, "status": ""}, "limit": 1000, "offset": offset,
                "with": {"analytics_data": False, "financial_data": False}})
            postings = data.get("result") or []
            for p in postings:
                if p.get("status") == "cancelled":
                    continue
                for pr in p.get("products") or []:
                    sku = int(pr.get("sku") or 0)
                    totals[sku] = totals.get(sku, 0) + float(pr.get("quantity") or 0)
            if len(postings) < 1000:
                break
            offset += 1000
        logger.info(f"продажи из заказов FBO: SKU {len(totals)}")
        return totals

    # ---------- склады ----------
    async def get_warehouses(self) -> list[dict]:
        """Склады продавца (для FBS). Для проверки ключа при старте."""
        data = await self._request("POST", "/v1/warehouse/list", {})
        return [{"warehouse_id": w.get("warehouse_id"), "name": w.get("name")} for w in (data.get("result") or [])]

    async def get_fbo_supply_limits(self) -> list[dict]:
        """Свободные места на складах FBO. Ozon не отдаёт их через открытый API, поэтому честно возвращаем пусто:
        советник распределит партию по тому, где товар уже лежит, и напишет, что места нужно проверить в кабинете."""
        return []


def period(days: int) -> tuple[str, str]:
    today = datetime.now(timezone.utc).date()
    return (today - timedelta(days=days)).isoformat(), (today - timedelta(days=1)).isoformat()
