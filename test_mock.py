"""Тесты без настоящего Ozon. Запуск: pytest test_mock.py -v"""
import asyncio
import math
import os

import pytest

from analytics import StockAnalyzer, build_items
from bot import format_report, split_message
from warehouse_advisor import WarehouseAdvisor

ROWS = [  # 5 товаров, 3 склада FBO
    {"sku": 1, "item_code": "A", "item_name": "Набор 216 24T", "free_to_sell_amount": 6, "promised_amount": 0, "warehouse_name": "Тверь", "warehouse_id": 11},
    {"sku": 1, "item_code": "A", "item_name": "Набор 216 24T", "free_to_sell_amount": 4, "promised_amount": 0, "warehouse_name": "Казань", "warehouse_id": 12},
    {"sku": 2, "item_code": "B", "item_name": "Набор 216 72T", "free_to_sell_amount": 3, "promised_amount": 0, "warehouse_name": "Тверь", "warehouse_id": 11},
    {"sku": 3, "item_code": "C", "item_name": "Товар C", "free_to_sell_amount": 300, "promised_amount": 0, "warehouse_name": "Новосибирск", "warehouse_id": 13},
    {"sku": 4, "item_code": "D", "item_name": "Товар D", "free_to_sell_amount": 50, "promised_amount": 0, "warehouse_name": "Казань", "warehouse_id": 12},
    {"sku": 5, "item_code": "E", "item_name": "Товар E без продаж", "free_to_sell_amount": 20, "promised_amount": 0, "warehouse_name": "Тверь", "warehouse_id": 11},
]
SALES = {1: 4.0, 2: 1.5, 3: 2.0, 4: 1.0, 5: 0.0}   # шт/день за 30 дней


def test_days_and_batch():
    res = build_items(ROWS, SALES, threshold=5, supply_days=14)
    by = {x.sku: x for x in res.all_items}
    assert by[1].total_stock == 10 and by[1].days_remaining == pytest.approx(2.5)
    assert by[1].optimal_batch == 56
    assert by[2].days_remaining == pytest.approx(2.0) and by[2].optimal_batch == 21
    assert math.isinf(by[5].days_remaining)
    assert {x.sku for x in res.low_stock_items} == {1, 2}


def test_advisor_respects_limits():
    res = build_items(ROWS, SALES, 5, 14)
    plans = WarehouseAdvisor().build_supply_plans(res, [{"warehouse_id": 11, "free_limit": 10}, {"warehouse_id": 12, "free_limit": 100}])
    for plan in plans:
        for r in plan.recommendations:
            assert r.free_limit is None or r.units_to_send <= r.free_limit
    p1 = next(p for p in plans if p.product.sku == 1)
    assert p1.limit_shortage > 0          # в Твери места меньше, чем нужно


def test_advisor_without_limits_sends_whole_batch():
    res = build_items(ROWS, SALES, 5, 14)
    for plan in WarehouseAdvisor().build_supply_plans(res, []):
        assert plan.total_to_send == plan.product.optimal_batch
        assert plan.limit_shortage == 0


def test_report_and_split():
    res = build_items(ROWS, SALES, 5, 14)
    text = format_report(res, WarehouseAdvisor().build_supply_plans(res, []), 5, 14)
    assert "КРИТИЧЕСКИЕ ОСТАТКИ" in text and "Набор 216 24T" in text
    assert all(len(p) <= 4000 for p in split_message("строка\n" * 2000))


class FakeClient:
    notes = []
    async def get_stocks_by_warehouse(self): return ROWS
    async def get_sales_analytics(self, a, b, skus, days): return SALES
    async def get_fbo_supply_limits(self): return []


def test_analyzer_with_mock():
    res = asyncio.run(StockAnalyzer(FakeClient(), 5, 14).analyze())
    assert len(res.low_stock_items) == 2


@pytest.mark.skipif(not os.environ.get("OZON_API_KEY"), reason="нет ключа Ozon")
def test_real_api():
    from config import settings
    from ozon_client import OzonClient
    async def go():
        c = OzonClient(settings.ozon_client_id, settings.ozon_api_key)
        try:
            res = await StockAnalyzer(c, 5, 14).analyze()
            print("товаров:", len(res.all_items), "мало остатка:", len(res.low_stock_items), "заметки:", res.notes)
        finally:
            await c.close()
    asyncio.run(go())


def test_wb_advisor_free_first_and_closed_excluded():
    from datetime import datetime, timedelta, timezone
    from warehouse_advisor import build_wb_plans
    d = (datetime.now(timezone.utc) + timedelta(days=1)).isoformat()
    coefs = [{"warehouseName": "Коледино", "coefficient": 0, "date": d, "boxTypeID": 2},
             {"warehouseName": "Электросталь", "coefficient": 1.5, "date": d, "boxTypeID": 2},
             {"warehouseName": "Казань", "coefficient": 0, "date": d, "boxTypeID": 2},
             {"warehouseName": "Белая Дача", "coefficient": -1, "date": d, "boxTypeID": 2}]
    res = build_items(ROWS, SALES, 5, 14)
    plans = build_wb_plans(res, coefs)
    names = {r.warehouse_name for p in plans for r in p.recommendations}
    assert "Белая Дача" not in names and names <= {"Коледино", "Казань"}
    for p in plans:
        assert sum(r.units_to_send for r in p.recommendations) == p.product.optimal_batch
