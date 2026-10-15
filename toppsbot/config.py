"""Settings, file locations and persistence."""
from __future__ import annotations

import json
import os
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path

APP_NAME = "ToppsBot"
DATA_DIR = Path(os.environ.get("APPDATA") or Path.home()) / APP_NAME
SETTINGS_FILE = DATA_DIR / "settings.json"
LOG_FILE = DATA_DIR / "toppsbot.log"
# Dedicated Chrome profile: keeps the Topps login between runs without touching the user's normal Chrome.
PROFILE_DIR = Path(
    os.environ.get("TOPPSBOT_PROFILE_DIR")
    or Path(os.environ.get("LOCALAPPDATA") or Path.home()) / APP_NAME / "chrome-profile"
)
DEFAULT_ORIGIN = "https://www.topps.com"


@dataclass
class Product:
    url: str
    quantity: int = 1
    max_price: float = 0.0  # 0 = no limit
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:8])


@dataclass
class Settings:
    products: list[Product] = field(default_factory=list)
    interval_min: int = 15
    interval_max: int = 30
    test_mode: bool = True
    clear_cart: bool = True
    sound: bool = True
    # Overrides for store.DEFAULT_SELECTORS, edited by hand in settings.json if Topps changes its pages.
    selectors: dict[str, str] = field(default_factory=dict)


def load() -> Settings:
    try:
        raw = json.loads(SETTINGS_FILE.read_text("utf-8"))
    except (OSError, ValueError):
        return Settings()
    product_fields = Product.__dataclass_fields__
    settings = Settings(**{k: v for k, v in raw.items() if k in Settings.__dataclass_fields__ and k != "products"})
    settings.products = [
        Product(**{k: v for k, v in p.items() if k in product_fields})
        for p in raw.get("products", [])
        if isinstance(p, dict) and p.get("url")
    ]
    return settings


def save(settings: Settings) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    tmp = SETTINGS_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(asdict(settings), indent=2), "utf-8")
    tmp.replace(SETTINGS_FILE)
