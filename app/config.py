import os
from dataclasses import dataclass

from dotenv import load_dotenv


load_dotenv()


@dataclass(frozen=True)
class Settings:
    database_url: str
    secret_key: str
    demo_mode: bool = False
    seed_demo: bool = False
    admin_email: str | None = None
    admin_password: str | None = None


def _required_env(name: str) -> str:
    value = os.getenv(name)
    if not value:
        raise RuntimeError(f"Environment variable {name} is required")
    return value


def _bool_env(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def get_settings() -> Settings:
    return Settings(
        database_url=_required_env("DATABASE_URL"),
        secret_key=_required_env("SECRET_KEY"),
        demo_mode=_bool_env("DEMO_MODE", default=False),
        seed_demo=_bool_env("SEED_DEMO", default=False),
        admin_email=os.getenv("ADMIN_EMAIL"),
        admin_password=os.getenv("ADMIN_PASSWORD"),
    )


settings = get_settings()
