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
    # The application stores LocalDateTime values as-is (gym wall-clock time) and
    # reads them back with getHour(); the ETL keeps that interpretation by default.
    # Set ETL_SOURCE_TIMEZONE to an IANA zone only if source timestamps become true UTC.
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

    source_tz = os.environ.get("ETL_SOURCE_TIMEZONE", "").strip()
    return Settings(
        mongodb_uri=os.environ["MONGODB_URI"],
        mongodb_db=os.environ.get("MONGODB_DB", "fitnesslife"),
        azure_server=os.environ["AZURE_SQL_SERVER"],
        azure_database=os.environ["AZURE_SQL_DATABASE"],
        azure_user=os.environ["AZURE_SQL_USER"],
        azure_password=os.environ["AZURE_SQL_PASSWORD"],
        source_timezone=ZoneInfo(source_tz) if source_tz else None,
        local_timezone=ZoneInfo("America/Bogota"),
        min_publish_ratio=float(os.environ.get("ETL_MIN_PUBLISH_RATIO", "0.5")),
    )
