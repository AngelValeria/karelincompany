"""Telegram: оформление отчёта, отправка и разбор команд /start /check /status /help.
На GitHub бот не висит в сети постоянно: команды забираются при каждом запуске (раз в ~10 минут)."""
import math
from datetime import timedelta, timezone

import httpx
from loguru import logger

from analytics import AnalysisResult
from warehouse_advisor import SupplyPlan

MSK = timezone(timedelta(hours=3))
LINE = "━━━━━━━━━━━━━━━━━━━━"
HELP = ("Я слежу за остатками KARELIN на складах Ozon и Wildberries.\n\n"
        "/check — проверить остатки сейчас\n"
        "/status — показать последнюю проверку\n"
        "/warehouses — где сейчас бесплатная приёмка на WB\n"
        "/help — эта справка\n\n"
        "Сама я проверяю остатки каждые 3 часа и пишу, если какого-то товара осталось меньше чем на 5 дней. "
        "Утром присылаю сводку. Отвечаю на команды с задержкой до 10 минут.")


def _days(x: float) -> str:
    return "нет продаж" if math.isinf(x) else f"{x:.1f} дн."


def format_report(res: AnalysisResult, plans: list[SupplyPlan], threshold: float, supply_days: int, title: str = "") -> str:
    when = res.checked_at.astimezone(MSK).strftime("%d.%m.%Y %H:%M")
    head = title + "\n" if title else ""
    notes = "".join(f"\nℹ️ {n}" for n in res.notes)
    if not res.low_stock_items:
        finite = [x.days_remaining for x in res.all_items if not math.isinf(x.days_remaining)]
        mn = f"{min(finite):.1f} дн." if finite else "товаров с продажами нет"
        return f"{head}✅ Все товары в норме. Минимальный остаток: {mn}\nПроверка: {when} (МСК){notes}"
    out = [f"{head}🔴 КРИТИЧЕСКИЕ ОСТАТКИ — {when} (МСК)",
           f"Найдено товаров с остатком < {threshold:g} дней: {len(res.low_stock_items)}", LINE]
    for plan in plans:
        p = plan.product
        out += [f"📦 {p.item_name} [{p.item_code}]",
                f"⏳ Осталось: {_days(p.days_remaining)} ({p.total_stock} шт.)",
                f"📊 Продажи: {p.avg_daily_sales:.1f} шт/день",
                f"🎯 Оптимальная партия: {p.optimal_batch} шт. (на {supply_days} дней)"]
        if plan.recommendations:
            out.append("📍 Куда отгружать:")
            for r in plan.recommendations:
                lim = "нет данных" if r.free_limit is None else f"{r.free_limit} шт."
                after = "—" if math.isinf(r.days_after_supply) else f"{r.days_after_supply:.0f} дн."
                out += [f"  • {r.warehouse_name}: {r.units_to_send} шт.", f"    └ {r.note}" if r.note else f"    └ Свободно: {lim} | После поставки: {after}"]
        if plan.limit_shortage > 0:
            out.append(f"⚠️ Не хватает мест: {plan.limit_shortage} шт.")
        elif plan.comment:
            out.append(f"ℹ️ {plan.comment}")
        out.append(LINE)
    ok = len(res.all_items) - len(res.low_stock_items)
    out.append(f"✅ Всё хорошо: {ok} товаров с запасом > {threshold:g} дней")
    return "\n".join(out) + notes


def split_message(text: str, limit: int = 4000) -> list[str]:
    parts, cur = [], ""
    for line in text.split("\n"):
        while len(line) > limit:
            parts.append(line[:limit]); line = line[limit:]
        if len(cur) + len(line) + 1 > limit:
            parts.append(cur.rstrip("\n")); cur = ""
        cur += line + "\n"
    if cur.strip():
        parts.append(cur.rstrip("\n"))
    return parts


class Telegram:
    def __init__(self, token: str):
        self._http = httpx.AsyncClient(base_url=f"https://api.telegram.org/bot{token}", timeout=40)

    async def close(self):
        await self._http.aclose()

    async def call(self, method: str, **params) -> dict:
        r = await self._http.post(f"/{method}", json=params)
        data = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
        if not data.get("ok"):
            logger.warning(f"telegram {method}: {r.status_code} {str(data.get('description', ''))[:120]}")
        return data

    async def send(self, chat_id: str | int, text: str):
        for part in split_message(text):
            await self.call("sendMessage", chat_id=chat_id, text=part, disable_web_page_preview=True)
        logger.info(f"отправлено сообщений: {len(split_message(text))}")

    async def updates(self, offset: int) -> list[dict]:
        await self.call("deleteWebhook")       # бот работает через getUpdates
        data = await self.call("getUpdates", offset=offset, timeout=0, allowed_updates=["message"])
        return data.get("result") or []

    async def me_ok(self) -> bool:
        return bool((await self.call("getMe")).get("ok"))
