from decimal import Decimal
from pathlib import Path
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    database_url: str = "postgresql+asyncpg://scorched:scorched@localhost:5432/scorched"
    anthropic_api_key: str

    starting_capital: Decimal = Decimal("100000")
    short_term_tax_rate: Decimal = Decimal("0.37")
    long_term_tax_rate: Decimal = Decimal("0.20")
    min_cash_reserve_pct: Decimal = Decimal("0.10")  # keep ≥10% cash at all times

    alpha_vantage_api_key: str = ""
    twelvedata_api_key: str = ""
    fred_api_key: str = ""

    # Broker config
    broker_mode: str = "paper"  # "paper" = DB-only, "alpaca_paper" = Alpaca paper, "alpaca_live" = Alpaca live
    alpaca_api_key: str = ""
    alpaca_secret_key: str = ""
    finnhub_api_key: str = ""

    # Telegram (optional — used for reconciliation alerts from FastAPI process)
    telegram_bot_token: str = ""
    telegram_chat_id: str = ""

    host: str = "0.0.0.0"
    port: int = 8000
    strategy_file: Path = Path("strategy.json")
    settings_pin: str = ""  # if set, PUT /api/v1/strategy requires this PIN to save


settings = Settings()
