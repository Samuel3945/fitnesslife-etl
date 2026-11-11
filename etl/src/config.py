"""Runtime configuration read from environment variables (.env locally, secrets in CI)."""

import os
from dataclasses import dataclass
from zoneinfo import ZoneInfo

from dotenv import load_dotenv


@dataclass(frozen=True)
class Settings:
    mongodb_uri: str
    mongodb_db: str
    azure_server: str
    azure_database: str
    azure_user: str
    azure_password: str
    # MongoDB stores UTC instants; the application (JVM in Colombia time) converts them
    # to local time before displaying them. The ETL does the same so the warehouse
    # matches the application. Set ETL_SOURCE_TIMEZONE=UTC to keep raw UTC values.
    source_timezone: ZoneInfo | None
    local_timezone: ZoneInfo
    min_publish_ratio: float


def load_settings() -> Settings:
    load_dotenv()
    missing = [
        name
        for name in ("MONGODB_URI", "AZURE_SQL_SERVER", "AZURE_SQL_DATABASE", "AZURE_SQL_USER", "AZURE_SQL_PASSWORD")
        if not os.environ.get(name)
    ]
    if missing:
        raise RuntimeError(f"Missing environment variables: {', '.join(missing)}")

    source_tz = os.environ.get("ETL_SOURCE_TIMEZONE", "").strip() or "America/Bogota"
    return Settings(
        mongodb_uri=os.environ["MONGODB_URI"],
        mongodb_db=os.environ.get("MONGODB_DB", "fitnesslife"),
        azure_server=os.environ["AZURE_SQL_SERVER"],
        azure_database=os.environ["AZURE_SQL_DATABASE"],
        azure_user=os.environ["AZURE_SQL_USER"],
        azure_password=os.environ["AZURE_SQL_PASSWORD"],
        source_timezone=None if source_tz.upper() == "UTC" else ZoneInfo(source_tz),
        local_timezone=ZoneInfo("America/Bogota"),
        min_publish_ratio=float(os.environ.get("ETL_MIN_PUBLISH_RATIO", "0.5")),
    )
