"""Клиент Wildberries (только чтение): остатки на складах WB и заказы за период.
Остатки: /api/v1/supplier/stocks (статистика), запасной путь: отчёт warehouse_remains (аналитика).
Отдаёт строки в том же виде, что и OzonClient, поэтому расчёт и отчёт общие.
В лог пишутся только адреса и количества."""
import asyncio
from datetime import datetime, timedelta, timezone

import httpx
from loguru import logger

from ozon_client import OzonError

STAT = "https://statistics-api.wildberries.ru"
ANALYTICS = "https://seller-analytics-api.wildberries.ru"


class WBClient:
    def __init__(self, api_key: str, timeout: float = 90):
        self._http = httpx.AsyncClient(timeout=timeout, headers={"Authorization": api_key})
        self.notes: list[str] = []

    async def close(self):
        await self._http.aclose()

    async def _get(self, url: str, params: dict | None = None):
        delay = 20.0
        tries = 5
        for attempt in range(1, tries + 1):
            try:
                r = await self._http.get(url, params=params)
            except httpx.HTTPError as e:
                logger.warning(f"WB {url.split('/api')[1]}: сеть, попытка {attempt}: {type(e).__name__}")
                if attempt == tries:
                    raise OzonError(f"Wildberries не отвечает ({type(e).__name__})") from e
                await asyncio.sleep(delay); continue
            path = url.split(".ru")[-1]
            logger.info(f"WB GET {path} → {r.status_code}")
            if r.status_code == 429 or r.status_code >= 500:
                wait = float(r.headers.get("X-Ratelimit-Retry") or r.headers.get("Retry-After") or delay)
                if attempt == tries:
                    hint = " WB ограничивает частоту запросов: возможно, кабинет WB одновременно использует другой сервис" if r.status_code == 429 else ""
                    raise OzonError(f"Wildberries {path}: ответ {r.status_code} после {tries} попыток.{hint}")
                logger.warning(f"WB {path}: {r.status_code}, жду {min(max(wait, 5), 90):.0f} с")
                await asyncio.sleep(min(max(wait, 5), 90)); delay = min(delay * 1.5, 90); continue
            if r.status_code in (401, 403):
                raise OzonError(f"Wildberries {path}: нет доступа ({r.status_code}). Проверьте токен WB и категории «Статистика» и «Аналитика». {r.text[:200]}")
            if r.status_code >= 400:
                raise OzonError(f"Wildberries {path}: ошибка {r.status_code}. {r.text[:200]}")
            return r.json() if r.content else None
        raise OzonError("Wildberries: не удалось получить ответ")

    async def get_stocks_by_warehouse(self) -> list[dict]:
        try:
            data = await self._get(STAT + "/api/v1/supplier/stocks", {"dateFrom": "2019-06-20"}) or []
            rows = [{"item_code": str(x.get("supplierArticle", "")), "item_name": str(x.get("subject") or x.get("supplierArticle", "")),
                     "sku": int(x.get("nmId") or 0), "free_to_sell_amount": int(x.get("quantity") or 0),
                     "promised_amount": int(x.get("inWayToClient") or 0) * 0, "warehouse_name": str(x.get("warehouseName", "")),
                     "warehouse_id": None} for x in data]
            logger.info(f"WB остатки: строк {len(rows)}")
            return rows
        except OzonError as e:
            logger.warning(f"WB остатки через статистику недоступны, беру отчёт аналитики: {e}")
            self.notes.append("Остатки WB взяты из отчёта аналитики, потому что статистика ответила: " + str(e)[:160])
            try:
                return await self._stocks_report()
            except OzonError as e2:
                raise OzonError(f"Остатки WB не получены. Статистика: {str(e)[:200]} | Отчёт аналитики: {str(e2)[:200]}") from e2

    async def _stocks_report(self) -> list[dict]:
        task = await self._get(ANALYTICS + "/api/v1/warehouse_remains", {"groupByNm": "true", "groupByBrand": "false"})
        tid = ((task or {}).get("data") or {}).get("taskId")
        if not tid:
            raise OzonError("Wildberries: отчёт об остатках не создан")
        for _ in range(12):
            await asyncio.sleep(5)
            st = await self._get(f"{ANALYTICS}/api/v1/warehouse_remains/tasks/{tid}/status")
            if ((st or {}).get("data") or {}).get("status") == "done":
                break
        data = await self._get(f"{ANALYTICS}/api/v1/warehouse_remains/tasks/{tid}/download") or []
        rows = []
        for it in data:
            for w in it.get("warehouses") or []:
                name = str(w.get("warehouseName", ""))
                if not name or "пути" in name.lower() or "всего" in name.lower():
                    continue
                rows.append({"item_code": str(it.get("vendorCode", "")), "item_name": str(it.get("subjectName") or it.get("vendorCode", "")),
                             "sku": int(it.get("nmId") or 0), "free_to_sell_amount": int(w.get("quantity") or 0),
                             "promised_amount": 0, "warehouse_name": name, "warehouse_id": None})
        logger.info(f"WB остатки из отчёта: строк {len(rows)}")
        return rows

    async def get_sales_analytics(self, date_from: str, date_to: str, sku_list: list[int], days: int) -> dict[int, float]:
        """Заказы за период по артикулам WB. Сначала статистика (лимит 1 запрос в минуту),
        если она занята, то воронка продаж из аналитики (у неё отдельный лимит)."""
        try:
            await asyncio.sleep(61)       # у статистики WB лимит: 1 запрос в минуту
            totals = await self._orders_from_statistics(date_from, date_to)
        except OzonError as e:
            logger.warning(f"WB заказы из статистики недоступны, беру аналитику: {e}")
            self.notes.append("Продажи WB взяты из аналитики: статистика WB была занята")
            totals = await self._orders_from_analytics(date_from, date_to, sku_list)
        return {s: totals.get(s, 0.0) / days for s in set(sku_list)}

    async def _orders_from_statistics(self, date_from: str, date_to: str) -> dict[int, float]:
        data = await self._get(STAT + "/api/v1/supplier/orders", {"dateFrom": date_from}) or []
        totals: dict[int, float] = {}
        end = datetime.fromisoformat(date_to).date()
        for o in data:
            if o.get("isCancel") or (o.get("orderType") and o.get("orderType") != "Клиентский"):
                continue
            try:
                d = datetime.fromisoformat(str(o.get("date", ""))[:10]).date()
            except ValueError:
                continue
            if d > end:
                continue
            sku = int(o.get("nmId") or 0)
            totals[sku] = totals.get(sku, 0) + 1
        logger.info(f"WB заказы из статистики: SKU {len(totals)}")
        return totals

    async def _post(self, url: str, body: dict):
        for attempt in range(1, 4):
            try:
                r = await self._http.post(url, json=body)
            except httpx.HTTPError as e:
                if attempt == 3:
                    raise OzonError(f"Wildberries не отвечает ({type(e).__name__})") from e
                await asyncio.sleep(10); continue
            path = url.split(".ru")[-1]
            logger.info(f"WB POST {path} → {r.status_code}")
            if r.status_code == 429 or r.status_code >= 500:
                if attempt == 3:
                    raise OzonError(f"Wildberries {path}: ответ {r.status_code}")
                await asyncio.sleep(min(float(r.headers.get("X-Ratelimit-Retry") or 25), 70)); continue
            if r.status_code >= 400:
                raise OzonError(f"Wildberries {path}: ошибка {r.status_code}. {r.text[:200]}")
            return r.json()
        raise OzonError("Wildberries: не удалось получить ответ")

    async def _orders_from_analytics(self, date_from: str, date_to: str, sku_list: list[int]) -> dict[int, float]:
        ids = [int(s) for s in sku_list if s][:1000]
        errors = []
        # новый метод воронки продаж
        try:
            data = await self._post(ANALYTICS + "/api/analytics/v3/sales-funnel/products",
                                    {"selectedPeriod": {"start": date_from, "end": date_to}, "nmIds": ids, "limit": 1000, "offset": 0})
            totals = {}
            for p in ((data or {}).get("data") or {}).get("products") or []:
                nm = int(((p.get("product") or {}).get("nmId")) or 0)
                sel = ((p.get("statistic") or {}).get("selected")) or {}
                totals[nm] = float(sel.get("orderCount") or sel.get("ordersCount") or 0)
            if totals:
                logger.info(f"WB заказы из воронки v3: SKU {len(totals)}")
                return totals
        except OzonError as e:
            errors.append(str(e)[:150])
        # прежний метод отчёта по карточкам
        try:
            totals, page = {}, 1
            while page <= 10:
                data = await self._post(ANALYTICS + "/api/v2/nm-report/detail", {
                    "nmIDs": ids, "period": {"begin": f"{date_from} 00:00:00", "end": f"{date_to} 23:59:59"},
                    "timezone": "Europe/Moscow", "page": page})
                d = (data or {}).get("data") or {}
                for c in d.get("cards") or []:
                    sel = ((c.get("statistics") or {}).get("selectedPeriod")) or {}
                    totals[int(c.get("nmID") or 0)] = float(sel.get("ordersCount") or 0)
                if not d.get("isNextPage"):
                    break
                page += 1
                await asyncio.sleep(21)
            logger.info(f"WB заказы из отчёта по карточкам: SKU {len(totals)}")
            return totals
        except OzonError as e:
            errors.append(str(e)[:150])
        raise OzonError("Продажи WB не получены ни из статистики, ни из аналитики: " + " | ".join(errors))

    async def get_fbo_supply_limits(self) -> list[dict]:
        return []

    async def get_acceptance_coefficients(self) -> list[dict]:
        """Коэффициенты приёмки складов WB на ближайшие дни: -1 закрыт, 0 бесплатно, больше 0 платно.
        Нужна категория токена «Поставки». Если доступа нет, возвращаем пусто и пишем заметку."""
        try:
            data = await self._get("https://supplies-api.wildberries.ru/api/v1/acceptance/coefficients") or []
            logger.info(f"WB коэффициенты приёмки: строк {len(data)}")
            return data
        except OzonError as e:
            logger.warning(f"WB коэффициенты недоступны: {e}")
            self.notes.append("Коэффициенты приёмки WB не получены: добавьте токену категорию «Поставки»")
            return []
