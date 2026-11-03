"""Настройки агента. Всё берётся из переменных окружения (на GitHub это секреты репозитория)."""
from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    ozon_client_id: str = Field("", alias="OZON_CLIENT_ID")
    ozon_api_key: str = Field("", alias="OZON_API_KEY")
    wb_api_key: str = Field("", alias="WB_API_KEY")       # если пусто, Wildberries не проверяем
    telegram_bot_token: str = Field("", alias="TELEGRAM_BOT_TOKEN")
    telegram_chat_id: str = Field("", alias="TELEGRAM_CHAT_ID")   # можно не заполнять: бот запомнит того, кто первым напишет /start
    check_interval_hours: float = Field(3, alias="CHECK_INTERVAL_HOURS")
    low_stock_days_threshold: float = Field(5, alias="LOW_STOCK_DAYS_THRESHOLD")
    optimal_supply_days: int = Field(14, alias="OPTIMAL_SUPPLY_DAYS")
    sales_period_days: int = Field(30, alias="SALES_PERIOD_DAYS")
    daily_summary_hour_msk: int = Field(9, alias="DAILY_SUMMARY_HOUR_MSK")   # во сколько по Москве присылать утреннюю сводку
    state_dir: str = Field("state", alias="STATE_DIR")

    def check(self) -> list[str]:
        missing = [n for n, v in (("OZON_CLIENT_ID", self.ozon_client_id), ("OZON_API_KEY", self.ozon_api_key),
                                  ("TELEGRAM_BOT_TOKEN", self.telegram_bot_token)) if not v.strip()]
        return missing


settings = Settings()
