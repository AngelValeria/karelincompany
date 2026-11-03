"""Один запуск агента (так его вызывает GitHub каждые ~10 минут):
1) забирает команды из Telegram и отвечает на них;
2) если прошло CHECK_INTERVAL_HOURS часов, проверяет остатки и пишет, если есть проблемы;
3) раз в день утром присылает сводку.
Состояние (последняя проверка, чей это чат) хранится в STATE_DIR/state.json между запусками."""
import asyncio
import json
import os
import sys
import time
from datetime import datetime

from loguru import logger

from analytics import AnalysisResult, StockAnalyzer
from bot import HELP, MSK, Telegram, format_report
from config import settings
from ozon_client import OzonClient, OzonError
from warehouse_advisor import WarehouseAdvisor, acceptance_text, build_wb_plans
from wb_client import WBClient

STATE = os.path.join(settings.state_dir, "state.json")


def load_state() -> dict:
    try:
        with open(STATE, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def save_state(st: dict):
    os.makedirs(settings.state_dir, exist_ok=True)
    with open(STATE, "w", encoding="utf-8") as f:
        json.dump(st, f, ensure_ascii=False)


async def run_check(markets: list, st: dict) -> tuple[list, str]:
    """Проверяет каждый маркетплейс отдельно: ошибка одного не мешает другому."""
    lows, parts = [], []
    for name, analyzer in markets:
        try:
            res = await analyzer.analyze(force=True)
            if name == "Wildberries":
                plans = build_wb_plans(res, await analyzer.client.get_acceptance_coefficients())
                res.notes = list(dict.fromkeys(res.notes + analyzer.client.notes))
            else:
                plans = WarehouseAdvisor().build_supply_plans(res, await analyzer.client.get_fbo_supply_limits())
            parts.append(format_report(res, plans, settings.low_stock_days_threshold, settings.optimal_supply_days, title=("🟦 OZON" if name == "Ozon" else "🟪 WILDBERRIES")))
            lows += [f"{name}:{x.sku}" for x in res.low_stock_items]
            DIAG.append(f"✅ {name} ответил: товаров {len(res.all_items)}, мало остатка {len(res.low_stock_items)}" + ("; " + "; ".join(res.notes) if res.notes else ""))
        except OzonError as e:
            logger.error(f"{name}: {e}")
            DIAG.append(f"❌ {name}: " + str(e)[:300])
            parts.append(("🟦 OZON" if name == "Ozon" else "🟪 WILDBERRIES") + f"\n⚠️ Не получилось проверить остатки.\n{e}")
    text = ("\n\n" + "═" * 20 + "\n\n").join(parts)
    st["last_check"] = time.time()
    st["last_text"] = text
    return lows, text


DIAG: list[str] = []          # понятный отчёт на странице запуска GitHub (без секретов)


def write_summary():
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if path and DIAG:
        with open(path, "a", encoding="utf-8") as f:
            f.write("## Агент остатков: что произошло\n\n" + "\n".join("- " + d for d in DIAG) + "\n")


async def main() -> int:
    missing = settings.check()
    if missing:
        logger.error("Не заполнены секреты: " + ", ".join(missing))
        DIAG.append("❌ Не заполнены секреты: " + ", ".join(missing)); write_summary()
        return 1
    st = load_state()
    tg = Telegram(settings.telegram_bot_token)
    client = OzonClient(settings.ozon_client_id, settings.ozon_api_key)
    analyzer = StockAnalyzer(client, settings.low_stock_days_threshold, settings.optimal_supply_days, settings.sales_period_days)
    markets = [("Ozon", analyzer)]
    wb = WBClient(settings.wb_api_key) if settings.wb_api_key.strip() else None
    if wb:
        markets.append(("Wildberries", StockAnalyzer(wb, settings.low_stock_days_threshold, settings.optimal_supply_days, settings.sales_period_days)))
    else:
        DIAG.append("ℹ️ Wildberries не проверяю: нет секрета WB_API_KEY")
    chat = settings.telegram_chat_id or st.get("chat_id")
    force_check = os.environ.get("FORCE_CHECK") == "1"
    try:
        me = await tg.call("getMe")
        if me.get("ok"):
            DIAG.append("✅ Telegram: токен работает, бот @" + str((me.get("result") or {}).get("username", "")))
        else:
            DIAG.append("❌ Telegram: токен не подходит (" + str(me.get("description", "нет ответа")) + "). Обновите секрет STOCK_BOT_TOKEN новым токеном от BotFather")
            return 0
        # 1. команды
        commands = []
        ups = await tg.updates(int(st.get("offset", 0)))
        DIAG.append(f"Новых сообщений боту: {len(ups)}")
        for u in ups:
            st["offset"] = u["update_id"] + 1
            m = u.get("message") or {}
            cid, text = (m.get("chat") or {}).get("id"), (m.get("text") or "").strip().split("@")[0].lower()
            if not cid:
                continue
            if not chat and text.startswith("/start") and (m.get("chat") or {}).get("type") == "private":
                chat = str(cid); st["chat_id"] = chat
                await tg.send(cid, "Готово, буду писать сюда.\n\nВаш chat id: " + chat +
                              "\nЕго можно добавить в секрет TELEGRAM_CHAT_ID, чтобы бот всегда знал, кому писать.")
            if str(cid) != str(chat):
                continue                          # чужим не отвечаем
            commands.append(text)
        if not chat:
            logger.info("чат ещё не привязан: напишите боту /start")
            DIAG.append("⚠️ Чат не привязан: напишите боту /start и запустите ещё раз")
            return 0
        DIAG.append("✅ Чат привязан, отчёты будут приходить")
        for c in commands:
            if c.startswith("/start") or c.startswith("/help"):
                await tg.send(chat, HELP)
            elif c.startswith("/status"):
                if st.get("last_text"):
                    await tg.send(chat, "Последняя проверка:\n\n" + st["last_text"])
                else:
                    await tg.send(chat, "Проверок ещё не было. Напишите /check")
            elif c.startswith("/check"):
                force_check = True
            elif c.startswith("/warehouses"):
                if wb:
                    await tg.send(chat, acceptance_text(await wb.get_acceptance_coefficients()))
                else:
                    await tg.send(chat, "Wildberries ещё не подключён: нет ключа WB_API_KEY")

        # 2. плановая проверка и утренняя сводка
        now_msk = datetime.now(MSK)
        today = now_msk.date().isoformat()
        due = time.time() - float(st.get("last_check", 0)) >= settings.check_interval_hours * 3600
        summary_due = now_msk.hour >= settings.daily_summary_hour_msk and st.get("summary_day") != today
        if force_check or due or summary_due:
            if force_check and wb:
                await tg.send(chat, "⏳ Проверяю Ozon и Wildberries, это займёт 2–3 минуты")
            lows, text = await run_check(markets, st)
            low_key = ",".join(lows)
            if force_check:
                await tg.send(chat, text)
            elif summary_due:
                await tg.send(chat, "☀️ Утренняя сводка\n\n" + text)
                st["summary_day"] = today
            elif lows and (low_key != st.get("last_low") or st.get("last_low_day") != today):
                await tg.send(chat, text)          # тревога: только если список изменился или сегодня ещё не писали
            if lows:
                st["last_low"], st["last_low_day"] = low_key, today
            else:
                st["last_low"] = ""
        return 0
    except OzonError as e:
        logger.error(str(e))
        DIAG.append("❌ Ozon: " + str(e)[:300])
        if chat:
            await tg.send(chat, f"⚠️ Не получилось проверить остатки.\n{e}")
        return 0
    finally:
        write_summary()
        save_state(st)
        await client.close()
        if wb:
            await wb.close()
        await tg.close()


if __name__ == "__main__":
    logger.remove(); logger.add(sys.stderr, level="INFO")
    sys.exit(asyncio.run(main()))
