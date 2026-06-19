import os
import re
from contextlib import asynccontextmanager
from copy import deepcopy
from decimal import Decimal, InvalidOperation, ROUND_CEILING, ROUND_HALF_UP
from html import escape
from typing import Iterable

import asyncpg
from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import Command, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    Message,
    ReplyKeyboardMarkup,
    Update,
)
from fastapi import FastAPI, Header, HTTPException, Request


# ============================================================
# CONFIGURATION
# ============================================================

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
DATABASE_URL = os.getenv("DATABASE_URL", "").strip()
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "").strip()
ADMIN_TELEGRAM_IDS_RAW = os.getenv("ADMIN_TELEGRAM_IDS", "").strip()
ADMIN_TELEGRAM_ID_RAW = os.getenv("ADMIN_TELEGRAM_ID", "").strip()
if ADMIN_TELEGRAM_ID_RAW:
    ADMIN_TELEGRAM_IDS_RAW = ",".join(
        part for part in [ADMIN_TELEGRAM_IDS_RAW, ADMIN_TELEGRAM_ID_RAW] if part
    )

RENDER_EXTERNAL_HOSTNAME = os.getenv("RENDER_EXTERNAL_HOSTNAME", "").strip()
WEBHOOK_BASE_URL = os.getenv("WEBHOOK_BASE_URL", "").strip()
if not WEBHOOK_BASE_URL and RENDER_EXTERNAL_HOSTNAME:
    WEBHOOK_BASE_URL = f"https://{RENDER_EXTERNAL_HOSTNAME}"

WEBHOOK_PATH = os.getenv("WEBHOOK_PATH", "/telegram-webhook").strip()
if not WEBHOOK_PATH.startswith("/"):
    WEBHOOK_PATH = "/" + WEBHOOK_PATH

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN is not set")
if not DATABASE_URL:
    raise RuntimeError("DATABASE_URL is not set")
if not WEBHOOK_BASE_URL:
    raise RuntimeError(
        "WEBHOOK_BASE_URL is not set and RENDER_EXTERNAL_HOSTNAME is unavailable"
    )
if not WEBHOOK_SECRET:
    raise RuntimeError("WEBHOOK_SECRET is not set")

WEBHOOK_URL = WEBHOOK_BASE_URL.rstrip("/") + WEBHOOK_PATH

YD3_TO_M3 = Decimal("0.764554858")
LB_TO_KG = Decimal("0.45359237")
VOL_6 = Decimal("0.000001")
PI = Decimal("3.141592653589793")
CUBIC_FEET_PER_YD3 = Decimal("27")
CUBIC_FEET_TO_M3 = Decimal("0.028316846592")
ORDER_ROUNDING_YD3 = Decimal("0.25")

VOLUME_SHAPES = {
    "slab": {
        "label": "Rectangular Slab / Driveway / Sidewalk",
        "fields": [
            ("length_ft", "Length", "ft"),
            ("width_ft", "Width", "ft"),
            ("thickness_in", "Thickness", "in"),
            ("quantity", "Number of identical sections", "pcs"),
        ],
    },
    "footing": {
        "label": "Continuous Footing",
        "fields": [
            ("length_ft", "Total length", "ft"),
            ("width_in", "Width", "in"),
            ("depth_in", "Depth", "in"),
            ("quantity", "Number of identical footings", "pcs"),
        ],
    },
    "wall": {
        "label": "Concrete Wall",
        "fields": [
            ("length_ft", "Length", "ft"),
            ("height_ft", "Height", "ft"),
            ("thickness_in", "Thickness", "in"),
            ("quantity", "Number of identical walls", "pcs"),
        ],
    },
    "round_column": {
        "label": "Round Column / Pier",
        "fields": [
            ("diameter_in", "Diameter", "in"),
            ("height_ft", "Height", "ft"),
            ("quantity", "Number of columns", "pcs"),
        ],
    },
    "curb": {
        "label": "Curb / Grade Beam",
        "fields": [
            ("length_ft", "Total length", "ft"),
            ("width_in", "Width", "in"),
            ("height_in", "Height", "in"),
            ("quantity", "Number of identical sections", "pcs"),
        ],
    },
    "circular_slab": {
        "label": "Circular Slab",
        "fields": [
            ("diameter_ft", "Diameter", "ft"),
            ("thickness_in", "Thickness", "in"),
            ("quantity", "Number of circular slabs", "pcs"),
        ],
    },
}

VALID_ROLES = {"salesperson", "admin"}
ROLE_LABELS = {
    "salesperson": "Salesperson",
    "admin": "Administrator",
}


def parse_admin_ids(raw: str) -> set[int]:
    result: set[int] = set()
    for part in raw.split(","):
        part = part.strip()
        if not part:
            continue
        if not part.isdigit():
            raise RuntimeError(
                "ADMIN_TELEGRAM_IDS must contain comma-separated numeric Telegram IDs"
            )
        result.add(int(part))
    return result


BOOTSTRAP_ADMIN_IDS = parse_admin_ids(ADMIN_TELEGRAM_IDS_RAW)


# ============================================================
# DEFAULT CERTIFIED RECIPES
# All material weights are lb per 1 yd³.
# Admixtures remain in oz/yd³.
# ============================================================


def recipe(
    psi: int,
    air: bool,
    slump: str,
    design_air: str,
    cement: str,
    stone: str,
    sand: str,
    water: str,
    sikament: str,
    air_oz: str,
    sikafume: str,
) -> dict:
    return {
        "psi": psi,
        "air": air,
        "name": f"{psi} PSI — {'Air' if air else 'Non-Air'}",
        "slump_in": Decimal(slump),
        "design_air_pct": Decimal(design_air),
        "materials_lb": {
            "Cement": Decimal(cement),
            "Stone": Decimal(stone),
            "Sand": Decimal(sand),
            "Water": Decimal(water),
        },
        "admixtures_oz": {
            "Sikament 475": Decimal(sikament),
            "Air": Decimal(air_oz),
            "SikaFume 290": Decimal(sikafume),
        },
    }


DEFAULT_RECIPES: dict[str, dict] = {
    "3000_no_air": recipe(3000, False, "6", "2.5", "450", "1977", "1447", "190", "31.50", "0", "18.00"),
    "3000_air": recipe(3000, True, "5", "5.0", "470", "1860", "1416", "198", "32.90", "3.53", "18.80"),
    "3500_no_air": recipe(3500, False, "6", "2.5", "480", "1930", "1440", "201", "33.60", "0", "19.20"),
    "3500_air": recipe(3500, True, "6", "5.0", "500", "1820", "1400", "210", "35.00", "3.75", "20.00"),
    "4000_no_air": recipe(4000, False, "6", "2.5", "525", "1850", "1433", "220", "36.75", "0", "21.00"),
    "4000_air": recipe(4000, True, "6", "5.0", "545", "1800", "1332", "229", "38.15", "4.09", "21.80"),
    "4500_no_air": recipe(4500, False, "6", "2.5", "550", "1830", "1403", "231", "38.50", "0", "22.00"),
    "4500_air": recipe(4500, True, "6", "5.0", "564", "1780", "1315", "237", "39.48", "4.23", "22.56"),
    "5000_no_air": recipe(5000, False, "6", "2.5", "580", "1830", "1344", "244", "40.60", "0", "23.20"),
    "5000_air": recipe(5000, True, "6", "5.0", "600", "1745", "1281", "252", "42.00", "4.50", "24.00"),
}

DEFAULT_RETAIL_PRICES = {
    3000: Decimal("170"),
    3500: Decimal("190"),
    4000: Decimal("210"),
    4500: Decimal("219"),
    5000: Decimal("227"),
}

DEFAULT_FOB_PRICES = {
    3000: Decimal("123"),
    3500: Decimal("125"),
    4000: Decimal("128"),
    4500: Decimal("135"),
    5000: Decimal("140"),
}

ADDITIVE_LABELS = {
    "calcium": "Calcium",
    "fiber": "Fiber",
    "extra_cement": "Extra cement",
    "liquid_flyash": "Liquid FlyAsh",
    "hot_water": "Hot water",
}

DEFAULT_ADDITIVE_PRICES = {
    "calcium": Decimal("4.00"),
    "fiber": Decimal("9.00"),
    "extra_cement": Decimal("15.00"),
    "liquid_flyash": Decimal("13.00"),
    "hot_water": Decimal("3.50"),
}

DEFAULT_SHORT_LOAD_FEES = {
    1: Decimal("450"),
    2: Decimal("350"),
    3: Decimal("250"),
    4: Decimal("150"),
    5: Decimal("150"),
}

DEFAULT_APP_SETTINGS = {
    "min_cycle_m3": "0.20",
    "max_cycle_m3": "0.5",
    "max_order_yd3": "100",
    "weekday_hours": "6:00 AM – 5:00 PM",
    "saturday_hours": "6:00 AM – 12:00 PM",
    "after_hours_fee": "1000",
    "fob_terms": "COD",
    "short_load_note": "The rate for 6–9 yd³ was not provided. Confirm it with management.",
}

RECIPE_COLUMNS = {
    "cement_lb": "Cement, lb/yd³",
    "stone_lb": "Stone, lb/yd³",
    "sand_lb": "Sand, lb/yd³",
    "water_lb": "Water, lb/yd³",
    "sikament_oz": "Sikament 475, oz/yd³",
    "air_oz": "Air, oz/yd³",
    "sikafume_oz": "SikaFume 290, oz/yd³",
    "slump_in": "Slump, in",
    "design_air_pct": "Design air, %",
}

NUMERIC_APP_SETTINGS = {
    "min_cycle_m3",
    "max_cycle_m3",
    "max_order_yd3",
    "after_hours_fee",
}

TEXT_APP_SETTINGS = {
    "weekday_hours",
    "saturday_hours",
    "fob_terms",
    "short_load_note",
}

# Runtime caches populated from PostgreSQL.
RECIPES = deepcopy(DEFAULT_RECIPES)
RETAIL_PRICES = deepcopy(DEFAULT_RETAIL_PRICES)
FOB_CONTRACT_PRICES = deepcopy(DEFAULT_FOB_PRICES)
ADDITIVE_PRICES = deepcopy(DEFAULT_ADDITIVE_PRICES)
SHORT_LOAD_FEES = deepcopy(DEFAULT_SHORT_LOAD_FEES)
APP_SETTINGS = deepcopy(DEFAULT_APP_SETTINGS)

MAX_CYCLE_M3 = Decimal(APP_SETTINGS["max_cycle_m3"])
MIN_CYCLE_M3 = Decimal(APP_SETTINGS["min_cycle_m3"])
MAX_ORDER_YD3 = Decimal(APP_SETTINGS["max_order_yd3"])


# ============================================================
# TELEGRAM SETUP
# ============================================================

bot = Bot(
    token=BOT_TOKEN,
    default=DefaultBotProperties(parse_mode=ParseMode.HTML),
)
dp = Dispatcher(storage=MemoryStorage())
router = Router()
dp.include_router(router)


class OrderState(StatesGroup):
    choosing_psi = State()
    choosing_air = State()
    waiting_yards = State()
    choosing_cycle_plan = State()


class RoleState(StatesGroup):
    waiting_assign_id = State()
    choosing_role = State()
    waiting_remove_id = State()


class AdminEditState(StatesGroup):
    waiting_value = State()
    confirming = State()


class VolumeCalcState(StatesGroup):
    choosing_shape = State()
    waiting_value = State()
    waiting_waste = State()


SALES_MENU = ReplyKeyboardMarkup(
    keyboard=[
        [
            KeyboardButton(text="💰 Retail Prices"),
            KeyboardButton(text="🤝 FOB Prices"),
        ],
        [
            KeyboardButton(text="➕ Additives"),
            KeyboardButton(text="🚚 Short Load Fee"),
        ],
        [KeyboardButton(text="🕒 Hours & Terms")],
        [KeyboardButton(text="📐 Concrete Volume Calculator")],
        [KeyboardButton(text="⬅️ Main Menu")],
    ],
    resize_keyboard=True,
)

PRODUCTION_MENU = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text="🧮 New Calculation")],
        [KeyboardButton(text="📚 History")],
        [KeyboardButton(text="⬅️ Main Menu")],
    ],
    resize_keyboard=True,
)

ADMIN_MENU = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text="👥 Users & Roles")],
        [
            KeyboardButton(text="💰 Edit Retail Prices"),
            KeyboardButton(text="🤝 Edit FOB Prices"),
        ],
        [
            KeyboardButton(text="➕ Edit Additives"),
            KeyboardButton(text="🚚 Edit Short Load"),
        ],
        [
            KeyboardButton(text="⚙️ Production Settings"),
            KeyboardButton(text="🧪 Edit Recipes"),
        ],
        [KeyboardButton(text="⬅️ Main Menu")],
    ],
    resize_keyboard=True,
)

ROLE_MENU = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text="➕ Assign Role"), KeyboardButton(text="🗑 Remove Access")],
        [KeyboardButton(text="📋 User List")],
        [KeyboardButton(text="⬅️ Admin Settings")],
    ],
    resize_keyboard=True,
)


def main_menu_for_role(role: str | None) -> ReplyKeyboardMarkup:
    rows: list[list[KeyboardButton]] = []
    if role == "salesperson":
        rows.append([KeyboardButton(text="💵 Sales Menu")])
    elif role == "admin":
        rows.append(
            [
                KeyboardButton(text="💵 Sales Menu"),
                KeyboardButton(text="🏭 Production"),
            ]
        )
        rows.append([KeyboardButton(text="⚙️ Admin Settings")])

    if role in {"salesperson", "admin"}:
        rows.append([KeyboardButton(text="📐 Concrete Volume Calculator")])

    rows.append(
        [
            KeyboardButton(text="🆔 My Telegram ID"),
            KeyboardButton(text="ℹ️ Help"),
        ]
    )
    return ReplyKeyboardMarkup(keyboard=rows, resize_keyboard=True)


def psi_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="3000 PSI", callback_data="psi:3000"),
                InlineKeyboardButton(text="3500 PSI", callback_data="psi:3500"),
            ],
            [
                InlineKeyboardButton(text="4000 PSI", callback_data="psi:4000"),
                InlineKeyboardButton(text="4500 PSI", callback_data="psi:4500"),
            ],
            [InlineKeyboardButton(text="5000 PSI", callback_data="psi:5000")],
        ]
    )


def air_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="Non-Air", callback_data="air:0"),
                InlineKeyboardButton(text="Air", callback_data="air:1"),
            ]
        ]
    )


def volume_shape_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="▭ Slab / Driveway", callback_data="volume:shape:slab")],
            [InlineKeyboardButton(text="▱ Continuous Footing", callback_data="volume:shape:footing")],
            [InlineKeyboardButton(text="▥ Concrete Wall", callback_data="volume:shape:wall")],
            [InlineKeyboardButton(text="◯ Round Column / Pier", callback_data="volume:shape:round_column")],
            [InlineKeyboardButton(text="▰ Curb / Grade Beam", callback_data="volume:shape:curb")],
            [InlineKeyboardButton(text="● Circular Slab", callback_data="volume:shape:circular_slab")],
            [InlineKeyboardButton(text="❌ Cancel", callback_data="volume:cancel")],
        ]
    )


def role_choice_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="Salesperson", callback_data="role:set:salesperson")],
            [InlineKeyboardButton(text="Administrator", callback_data="role:set:admin")],
        ]
    )


def cycle_plan_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="✅ Use Optimized Plan",
                    callback_data="cycleplan:optimized",
                )
            ],
            [
                InlineKeyboardButton(
                    text="⚠️ Keep Standard Plan",
                    callback_data="cycleplan:standard",
                )
            ],
            [InlineKeyboardButton(text="❌ Cancel", callback_data="cycleplan:cancel")],
        ]
    )


def confirm_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="✅ Confirm", callback_data="adminconfirm:yes"),
                InlineKeyboardButton(text="❌ Cancel", callback_data="adminconfirm:no"),
            ]
        ]
    )


# ============================================================
# DATABASE
# ============================================================

pool: asyncpg.Pool | None = None


async def init_db() -> None:
    global pool
    pool = await asyncpg.create_pool(DATABASE_URL, min_size=1, max_size=5)
    async with pool.acquire() as connection:
        await connection.execute(
            """
            CREATE TABLE IF NOT EXISTS orders (
                id BIGSERIAL PRIMARY KEY,
                telegram_user_id BIGINT NOT NULL,
                telegram_username TEXT,
                yards NUMERIC(14, 4) NOT NULL,
                cubic_meters NUMERIC(14, 6) NOT NULL,
                cycles_count INTEGER NOT NULL,
                last_cycle_m3 NUMERIC(14, 6) NOT NULL,
                has_warning BOOLEAN NOT NULL DEFAULT FALSE,
                psi INTEGER,
                air_entrained BOOLEAN,
                mix_key TEXT,
                cycle_plan TEXT NOT NULL DEFAULT 'standard',
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            )
            """
        )
        await connection.execute("ALTER TABLE orders ADD COLUMN IF NOT EXISTS psi INTEGER")
        await connection.execute(
            "ALTER TABLE orders ADD COLUMN IF NOT EXISTS air_entrained BOOLEAN"
        )
        await connection.execute("ALTER TABLE orders ADD COLUMN IF NOT EXISTS mix_key TEXT")
        await connection.execute(
            "ALTER TABLE orders ADD COLUMN IF NOT EXISTS cycle_plan TEXT NOT NULL DEFAULT 'standard'"
        )
        await connection.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_orders_user_created
            ON orders (telegram_user_id, created_at DESC)
            """
        )

        await connection.execute(
            """
            CREATE TABLE IF NOT EXISTS user_roles (
                telegram_user_id BIGINT PRIMARY KEY,
                role TEXT NOT NULL CHECK (role IN ('salesperson', 'admin')),
                telegram_username TEXT,
                display_name TEXT,
                updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            )
            """
        )

        await connection.execute(
            """
            CREATE TABLE IF NOT EXISTS app_settings (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL,
                updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            )
            """
        )

        await connection.execute(
            """
            CREATE TABLE IF NOT EXISTS recipe_settings (
                mix_key TEXT PRIMARY KEY,
                cement_lb NUMERIC NOT NULL,
                stone_lb NUMERIC NOT NULL,
                sand_lb NUMERIC NOT NULL,
                water_lb NUMERIC NOT NULL,
                sikament_oz NUMERIC NOT NULL,
                air_oz NUMERIC NOT NULL,
                sikafume_oz NUMERIC NOT NULL,
                slump_in NUMERIC NOT NULL,
                design_air_pct NUMERIC NOT NULL,
                updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            )
            """
        )

        # Older versions had an Operator role. It is intentionally converted
        # to Salesperson because this version has only Administrator and Salesperson.
        await connection.execute(
            "UPDATE user_roles SET role = 'salesperson', updated_at = NOW() WHERE role = 'operator'"
        )

        await seed_defaults(connection)
        await bootstrap_admins(connection)

    await load_runtime_settings()


async def seed_defaults(connection: asyncpg.Connection) -> None:
    setting_rows: list[tuple[str, str]] = []
    setting_rows.extend((f"retail.{psi}", str(value)) for psi, value in DEFAULT_RETAIL_PRICES.items())
    setting_rows.extend((f"fob.{psi}", str(value)) for psi, value in DEFAULT_FOB_PRICES.items())
    setting_rows.extend(
        (f"additive.{slug}", str(value)) for slug, value in DEFAULT_ADDITIVE_PRICES.items()
    )
    setting_rows.extend(
        (f"shortload.{yards}", str(value)) for yards, value in DEFAULT_SHORT_LOAD_FEES.items()
    )
    setting_rows.extend(DEFAULT_APP_SETTINGS.items())

    await connection.executemany(
        """
        INSERT INTO app_settings (key, value)
        VALUES ($1, $2)
        ON CONFLICT (key) DO NOTHING
        """,
        setting_rows,
    )

    recipe_rows = []
    for mix_key, item in DEFAULT_RECIPES.items():
        recipe_rows.append(
            (
                mix_key,
                item["materials_lb"]["Cement"],
                item["materials_lb"]["Stone"],
                item["materials_lb"]["Sand"],
                item["materials_lb"]["Water"],
                item["admixtures_oz"]["Sikament 475"],
                item["admixtures_oz"]["Air"],
                item["admixtures_oz"]["SikaFume 290"],
                item["slump_in"],
                item["design_air_pct"],
            )
        )

    await connection.executemany(
        """
        INSERT INTO recipe_settings (
            mix_key, cement_lb, stone_lb, sand_lb, water_lb,
            sikament_oz, air_oz, sikafume_oz, slump_in, design_air_pct
        )
        VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10)
        ON CONFLICT (mix_key) DO NOTHING
        """,
        recipe_rows,
    )


async def bootstrap_admins(connection: asyncpg.Connection) -> None:
    for user_id in BOOTSTRAP_ADMIN_IDS:
        await connection.execute(
            """
            INSERT INTO user_roles (telegram_user_id, role)
            VALUES ($1, 'admin')
            ON CONFLICT (telegram_user_id)
            DO UPDATE SET role = 'admin', updated_at = NOW()
            """,
            user_id,
        )


async def close_db() -> None:
    global pool
    if pool is not None:
        await pool.close()
        pool = None


async def load_runtime_settings() -> None:
    global RECIPES
    global RETAIL_PRICES
    global FOB_CONTRACT_PRICES
    global ADDITIVE_PRICES
    global SHORT_LOAD_FEES
    global APP_SETTINGS
    global MAX_CYCLE_M3
    global MIN_CYCLE_M3
    global MAX_ORDER_YD3

    assert pool is not None
    async with pool.acquire() as connection:
        setting_rows = await connection.fetch("SELECT key, value FROM app_settings")
        recipe_rows = await connection.fetch("SELECT * FROM recipe_settings")

    settings = {row["key"]: row["value"] for row in setting_rows}

    RETAIL_PRICES = {
        psi: Decimal(settings.get(f"retail.{psi}", str(default)))
        for psi, default in DEFAULT_RETAIL_PRICES.items()
    }
    FOB_CONTRACT_PRICES = {
        psi: Decimal(settings.get(f"fob.{psi}", str(default)))
        for psi, default in DEFAULT_FOB_PRICES.items()
    }
    ADDITIVE_PRICES = {
        slug: Decimal(settings.get(f"additive.{slug}", str(default)))
        for slug, default in DEFAULT_ADDITIVE_PRICES.items()
    }
    SHORT_LOAD_FEES = {
        yards: Decimal(settings.get(f"shortload.{yards}", str(default)))
        for yards, default in DEFAULT_SHORT_LOAD_FEES.items()
    }
    APP_SETTINGS = {
        key: settings.get(key, default) for key, default in DEFAULT_APP_SETTINGS.items()
    }

    MIN_CYCLE_M3 = Decimal(APP_SETTINGS["min_cycle_m3"])
    MAX_CYCLE_M3 = Decimal(APP_SETTINGS["max_cycle_m3"])
    MAX_ORDER_YD3 = Decimal(APP_SETTINGS["max_order_yd3"])

    loaded = deepcopy(DEFAULT_RECIPES)
    for row in recipe_rows:
        mix_key = row["mix_key"]
        if mix_key not in loaded:
            continue
        loaded[mix_key]["materials_lb"] = {
            "Cement": Decimal(row["cement_lb"]),
            "Stone": Decimal(row["stone_lb"]),
            "Sand": Decimal(row["sand_lb"]),
            "Water": Decimal(row["water_lb"]),
        }
        loaded[mix_key]["admixtures_oz"] = {
            "Sikament 475": Decimal(row["sikament_oz"]),
            "Air": Decimal(row["air_oz"]),
            "SikaFume 290": Decimal(row["sikafume_oz"]),
        }
        loaded[mix_key]["slump_in"] = Decimal(row["slump_in"])
        loaded[mix_key]["design_air_pct"] = Decimal(row["design_air_pct"])
    RECIPES = loaded


async def get_user_role(user_id: int) -> str | None:
    assert pool is not None
    async with pool.acquire() as connection:
        value = await connection.fetchval(
            "SELECT role FROM user_roles WHERE telegram_user_id = $1", user_id
        )
    return str(value) if value else None


async def update_user_identity(message: Message) -> None:
    assert pool is not None
    full_name = " ".join(
        part for part in [message.from_user.first_name, message.from_user.last_name] if part
    )
    async with pool.acquire() as connection:
        await connection.execute(
            """
            UPDATE user_roles
            SET telegram_username = $2,
                display_name = $3,
                updated_at = NOW()
            WHERE telegram_user_id = $1
            """,
            message.from_user.id,
            message.from_user.username,
            full_name or None,
        )


async def set_user_role(user_id: int, role: str) -> None:
    if role not in VALID_ROLES:
        raise ValueError("Invalid role")
    assert pool is not None
    async with pool.acquire() as connection:
        await connection.execute(
            """
            INSERT INTO user_roles (telegram_user_id, role)
            VALUES ($1, $2)
            ON CONFLICT (telegram_user_id)
            DO UPDATE SET role = EXCLUDED.role, updated_at = NOW()
            """,
            user_id,
            role,
        )


async def remove_user_role(user_id: int) -> bool:
    assert pool is not None
    async with pool.acquire() as connection:
        result = await connection.execute(
            "DELETE FROM user_roles WHERE telegram_user_id = $1", user_id
        )
    return result.endswith("1")


async def list_users_with_roles():
    assert pool is not None
    async with pool.acquire() as connection:
        return await connection.fetch(
            """
            SELECT telegram_user_id, role, telegram_username, display_name, updated_at
            FROM user_roles
            ORDER BY
                CASE role WHEN 'admin' THEN 1 ELSE 2 END,
                telegram_user_id
            """
        )


async def upsert_setting(key: str, value: str) -> None:
    assert pool is not None
    async with pool.acquire() as connection:
        await connection.execute(
            """
            INSERT INTO app_settings (key, value)
            VALUES ($1, $2)
            ON CONFLICT (key)
            DO UPDATE SET value = EXCLUDED.value, updated_at = NOW()
            """,
            key,
            value,
        )


async def update_recipe_value(mix_key: str, column: str, value: Decimal) -> None:
    if mix_key not in DEFAULT_RECIPES or column not in RECIPE_COLUMNS:
        raise ValueError("Invalid recipe field")
    assert pool is not None
    async with pool.acquire() as connection:
        await connection.execute(
            f"UPDATE recipe_settings SET {column} = $2, updated_at = NOW() WHERE mix_key = $1",
            mix_key,
            value,
        )


async def save_order(
    *,
    user_id: int,
    username: str | None,
    mix_key: str,
    yards: Decimal,
    cubic_meters: Decimal,
    cycles_count: int,
    last_cycle_m3: Decimal,
    has_warning: bool,
    cycle_plan: str,
) -> int:
    if cycle_plan not in {"standard", "optimized"}:
        raise ValueError("Invalid cycle plan")
    assert pool is not None
    item = RECIPES[mix_key]
    async with pool.acquire() as connection:
        order_id = await connection.fetchval(
            """
            INSERT INTO orders (
                telegram_user_id, telegram_username, yards, cubic_meters,
                cycles_count, last_cycle_m3, has_warning, psi,
                air_entrained, mix_key, cycle_plan
            )
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11)
            RETURNING id
            """,
            user_id,
            username,
            yards,
            cubic_meters,
            cycles_count,
            last_cycle_m3,
            has_warning,
            item["psi"],
            item["air"],
            mix_key,
            cycle_plan,
        )
    return int(order_id)


async def get_recent_orders(limit: int = 15):
    assert pool is not None
    async with pool.acquire() as connection:
        return await connection.fetch(
            """
            SELECT id, telegram_user_id, telegram_username, yards, cubic_meters,
                   cycles_count, last_cycle_m3, has_warning, psi,
                   air_entrained, mix_key, cycle_plan, created_at
            FROM orders
            ORDER BY created_at DESC
            LIMIT $1
            """,
            limit,
        )


async def get_order(order_id: int):
    assert pool is not None
    async with pool.acquire() as connection:
        return await connection.fetchrow(
            "SELECT * FROM orders WHERE id = $1",
            order_id,
        )


# ============================================================
# CALCULATIONS
# ============================================================


def q6(value: Decimal) -> Decimal:
    return value.quantize(VOL_6, rounding=ROUND_HALF_UP)


def fmt_decimal(value: Decimal, places: int = 2) -> str:
    quant = Decimal("1").scaleb(-places)
    text = f"{value.quantize(quant, rounding=ROUND_HALF_UP):f}"
    whole, _, fraction = text.partition(".")
    whole_with_commas = f"{int(whole):,}"
    if places == 0:
        return whole_with_commas
    return whole_with_commas + "." + fraction


def fmt_money(value: Decimal) -> str:
    if value == value.to_integral_value():
        return f"${fmt_decimal(value, 0)}"
    return f"${fmt_decimal(value, 2)}"


def parse_decimal_input(text: str, *, positive: bool = True) -> Decimal:
    cleaned = text.strip().replace(",", ".").replace("$", "").replace("%", "")
    cleaned = re.sub(r"\s+", "", cleaned)
    try:
        value = Decimal(cleaned)
    except InvalidOperation as exc:
        raise ValueError("Enter a valid number.") from exc
    if positive and value <= 0:
        raise ValueError("The value must be greater than zero.")
    if not positive and value < 0:
        raise ValueError("The value cannot be negative.")
    return value


def parse_yards(text: str) -> Decimal:
    cleaned = text.strip().lower().replace(",", ".")
    cleaned = cleaned.replace("yd³", "").replace("yd3", "")
    cleaned = cleaned.replace("yards", "").replace("yard", "")
    cleaned = re.sub(r"\s+", "", cleaned)
    try:
        value = Decimal(cleaned)
    except InvalidOperation as exc:
        raise ValueError("Enter a number, for example 7.15 or 7,15.") from exc
    if value <= 0:
        raise ValueError("Volume must be greater than zero.")
    if value > MAX_ORDER_YD3:
        raise ValueError(
            f"Maximum for one calculation: {fmt_decimal(MAX_ORDER_YD3, 2)} yd³."
        )
    return value


def round_up_to_increment(value: Decimal, increment: Decimal) -> Decimal:
    if value <= 0:
        return Decimal("0")
    units = (value / increment).to_integral_value(rounding=ROUND_CEILING)
    return units * increment


def calculate_concrete_volume(
    shape_key: str,
    values: dict[str, Decimal],
    waste_pct: Decimal,
) -> dict:
    if shape_key not in VOLUME_SHAPES:
        raise ValueError("Unknown concrete shape.")
    if waste_pct < 0 or waste_pct > 50:
        raise ValueError("Waste percentage must be between 0 and 50.")

    quantity = values.get("quantity", Decimal("1"))
    if quantity <= 0 or quantity != quantity.to_integral_value():
        raise ValueError("Quantity must be a positive whole number.")

    if shape_key == "slab":
        cubic_feet = (
            values["length_ft"]
            * values["width_ft"]
            * (values["thickness_in"] / Decimal("12"))
            * quantity
        )
        formula = "Length × Width × Thickness × Quantity"
    elif shape_key == "footing":
        cubic_feet = (
            values["length_ft"]
            * (values["width_in"] / Decimal("12"))
            * (values["depth_in"] / Decimal("12"))
            * quantity
        )
        formula = "Length × Width × Depth × Quantity"
    elif shape_key == "wall":
        cubic_feet = (
            values["length_ft"]
            * values["height_ft"]
            * (values["thickness_in"] / Decimal("12"))
            * quantity
        )
        formula = "Length × Height × Thickness × Quantity"
    elif shape_key == "round_column":
        radius_ft = values["diameter_in"] / Decimal("24")
        cubic_feet = PI * radius_ft * radius_ft * values["height_ft"] * quantity
        formula = "π × Radius² × Height × Quantity"
    elif shape_key == "curb":
        cubic_feet = (
            values["length_ft"]
            * (values["width_in"] / Decimal("12"))
            * (values["height_in"] / Decimal("12"))
            * quantity
        )
        formula = "Length × Width × Height × Quantity"
    elif shape_key == "circular_slab":
        radius_ft = values["diameter_ft"] / Decimal("2")
        cubic_feet = (
            PI
            * radius_ft
            * radius_ft
            * (values["thickness_in"] / Decimal("12"))
            * quantity
        )
        formula = "π × Radius² × Thickness × Quantity"
    else:
        raise ValueError("Unknown concrete shape.")

    if cubic_feet <= 0:
        raise ValueError("Calculated volume must be greater than zero.")

    base_yd3 = cubic_feet / CUBIC_FEET_PER_YD3
    waste_yd3 = base_yd3 * waste_pct / Decimal("100")
    total_yd3 = base_yd3 + waste_yd3
    recommended_yd3 = round_up_to_increment(total_yd3, ORDER_ROUNDING_YD3)
    extra_yd3 = recommended_yd3 - total_yd3

    return {
        "shape_key": shape_key,
        "shape_label": VOLUME_SHAPES[shape_key]["label"],
        "values": values,
        "formula": formula,
        "cubic_feet": cubic_feet,
        "base_yd3": base_yd3,
        "waste_pct": waste_pct,
        "waste_yd3": waste_yd3,
        "total_yd3": total_yd3,
        "recommended_yd3": recommended_yd3,
        "extra_yd3": extra_yd3,
        "total_m3": total_yd3 * YD3_TO_M3,
        "recommended_m3": recommended_yd3 * YD3_TO_M3,
    }


def build_volume_result(calc: dict) -> str:
    lines = [
        "📐 <b>CONCRETE VOLUME RESULT</b>",
        "",
        f"Shape: <b>{escape(calc['shape_label'])}</b>",
        f"Formula: <code>{escape(calc['formula'])}</code>",
        "",
        "<b>Dimensions</b>",
    ]

    shape = VOLUME_SHAPES[calc["shape_key"]]
    for key, label, unit in shape["fields"]:
        value = calc["values"][key]
        places = 0 if key == "quantity" else 3
        if key == "quantity":
            lines.append(f"{label}: <b>{fmt_decimal(value, places)}</b>")
        else:
            lines.append(f"{label}: <b>{fmt_decimal(value, places)} {unit}</b>")

    lines += [
        "",
        f"Raw volume: <b>{fmt_decimal(calc['cubic_feet'], 3)} ft³</b>",
        f"Base concrete: <b>{fmt_decimal(calc['base_yd3'], 3)} yd³</b>",
        f"Waste allowance: <b>{fmt_decimal(calc['waste_pct'], 2)}%</b> "
        f"(+{fmt_decimal(calc['waste_yd3'], 3)} yd³)",
        "",
        f"Exact total: <b>{fmt_decimal(calc['total_yd3'], 3)} yd³</b>",
        f"Exact total in metric: <b>{fmt_decimal(calc['total_m3'], 6)} m³</b>",
        "",
        f"✅ Recommended order: <b>{fmt_decimal(calc['recommended_yd3'], 2)} yd³</b>",
        f"Recommended metric volume: <b>{fmt_decimal(calc['recommended_m3'], 6)} m³</b>",
        f"Rounding allowance: <b>{fmt_decimal(calc['extra_yd3'], 3)} yd³</b>",
        "",
        "The recommended order is rounded up to the nearest 0.25 yd³. "
        "Final field conditions, subgrade, form dimensions, and waste should be verified before ordering.",
    ]
    return "\n".join(lines)


def volume_field_prompt(shape_key: str, field_index: int) -> str:
    shape = VOLUME_SHAPES[shape_key]
    key, label, unit = shape["fields"][field_index]
    if key == "quantity":
        return (
            f"<b>{shape['label']}</b>\n\n"
            f"Enter {label.lower()} as a whole number.\n"
            "Example: <code>1</code>"
        )
    return (
        f"<b>{shape['label']}</b>\n\n"
        f"Enter {label.lower()} in <b>{unit}</b>.\n"
        "You can use a dot or comma, for example <code>12.5</code>."
    )


def split_standard_cycles(total_m3: Decimal) -> list[Decimal]:
    """Split the order into maximum-size cycles plus one remainder cycle."""
    full_count = int(total_m3 // MAX_CYCLE_M3)
    remainder = total_m3 - (MAX_CYCLE_M3 * full_count)
    tolerance = Decimal("0.000000001")
    if remainder < tolerance:
        remainder = Decimal("0")
    cycles = [MAX_CYCLE_M3] * full_count
    if remainder > 0:
        cycles.append(remainder)
    return cycles


def split_optimized_cycles(total_m3: Decimal) -> list[Decimal] | None:
    """
    Return an evenly distributed plan that stays at or below MAX_CYCLE_M3
    and at or above MIN_CYCLE_M3. Return None when no valid plan exists.
    """
    if total_m3 <= 0 or MAX_CYCLE_M3 <= 0:
        return None

    count = int(
        (total_m3 / MAX_CYCLE_M3).to_integral_value(rounding=ROUND_CEILING)
    )
    count = max(1, count)
    exact_cycle = total_m3 / Decimal(count)

    if exact_cycle > MAX_CYCLE_M3 or exact_cycle < MIN_CYCLE_M3:
        return None

    # Round displayed/entered cycle sizes to six decimals while preserving
    # the exact total in the final cycle.
    base = q6(exact_cycle)
    cycles = [base] * max(0, count - 1)
    final_cycle = q6(total_m3 - sum(cycles, Decimal("0")))
    cycles.append(final_cycle)

    tolerance = Decimal("0.000002")
    if any(cycle <= 0 for cycle in cycles):
        return None
    if any(cycle > MAX_CYCLE_M3 + tolerance for cycle in cycles):
        return None
    if any(cycle < MIN_CYCLE_M3 - tolerance for cycle in cycles):
        return None
    return cycles


def is_small_last_cycle(cycles: list[Decimal]) -> bool:
    if not cycles:
        return False
    last_cycle = cycles[-1]
    return last_cycle < MIN_CYCLE_M3 and last_cycle < MAX_CYCLE_M3


def format_cycle_sequence(cycles: list[Decimal]) -> str:
    """Compact repeated consecutive cycle sizes for Telegram output."""
    if not cycles:
        return "—"
    groups: list[tuple[Decimal, int]] = []
    tolerance = Decimal("0.000001")
    for cycle in cycles:
        if groups and abs(groups[-1][0] - cycle) <= tolerance:
            value, count = groups[-1]
            groups[-1] = (value, count + 1)
        else:
            groups.append((cycle, 1))

    parts = []
    for value, count in groups:
        shown = f"{fmt_decimal(value, 6)} m³"
        parts.append(f"{shown} × {count}" if count > 1 else shown)
    return " + ".join(parts)


def recipe_materials_kg_per_m3(item: dict) -> dict[str, Decimal]:
    return {
        name: (weight_lb * LB_TO_KG) / YD3_TO_M3
        for name, weight_lb in item["materials_lb"].items()
    }


def calculate_order(
    yards: Decimal,
    mix_key: str,
    *,
    cycle_plan: str = "standard",
) -> dict:
    if cycle_plan not in {"standard", "optimized"}:
        raise ValueError("Invalid cycle plan")

    item = RECIPES[mix_key]
    total_m3 = yards * YD3_TO_M3
    standard_cycles = split_standard_cycles(total_m3)
    standard_warning = is_small_last_cycle(standard_cycles)
    optimized_cycles = (
        split_optimized_cycles(total_m3) if standard_warning else None
    )
    optimization_available = bool(optimized_cycles)

    if cycle_plan == "optimized":
        if not optimized_cycles:
            raise ValueError("An optimized cycle plan is not available for this order")
        cycles = optimized_cycles
    else:
        cycles = standard_cycles

    last_cycle = cycles[-1] if cycles else Decimal("0")
    has_warning = is_small_last_cycle(cycles)

    materials_total_kg = {
        name: weight_lb * LB_TO_KG * yards
        for name, weight_lb in item["materials_lb"].items()
    }
    admixtures_total_oz = {
        name: rate_oz * yards
        for name, rate_oz in item["admixtures_oz"].items()
    }
    kg_per_m3 = recipe_materials_kg_per_m3(item)

    cycle_rows = []
    for number, cycle_m3 in enumerate(cycles, start=1):
        cycle_yd3 = cycle_m3 / YD3_TO_M3
        cycle_rows.append(
            {
                "number": number,
                "m3": cycle_m3,
                "yd3": cycle_yd3,
                "materials_kg": {
                    name: rate * cycle_m3 for name, rate in kg_per_m3.items()
                },
                "admixtures_oz": {
                    name: rate * cycle_yd3
                    for name, rate in item["admixtures_oz"].items()
                },
            }
        )

    return {
        "mix_key": mix_key,
        "recipe": item,
        "yards": yards,
        "total_m3": total_m3,
        "cycle_plan": cycle_plan,
        "cycles": cycles,
        "standard_cycles": standard_cycles,
        "optimized_cycles": optimized_cycles,
        "optimization_available": optimization_available,
        "standard_warning": standard_warning,
        "last_cycle": last_cycle,
        "has_warning": has_warning,
        "kg_per_m3": kg_per_m3,
        "materials_total_kg": materials_total_kg,
        "admixtures_total_oz": admixtures_total_oz,
        "cycle_rows": cycle_rows,
    }


def build_cycle_plan_offer(calc: dict) -> str:
    optimized = calc["optimized_cycles"]
    if not optimized:
        raise ValueError("No optimized plan available")

    return "\n".join(
        [
            "🧠 <b>SMART BATCH OPTIMIZER</b>",
            "",
            f"Mix: <b>{calc['recipe']['name']}</b>",
            f"Order: <b>{fmt_decimal(calc['yards'], 3)} yd³</b>",
            f"ELKON volume: <b>{fmt_decimal(calc['total_m3'], 6)} m³</b>",
            "",
            "⚠️ <b>Standard plan has a small last cycle</b>",
            f"Standard: {format_cycle_sequence(calc['standard_cycles'])}",
            f"Last cycle: <b>{fmt_decimal(calc['standard_cycles'][-1], 6)} m³</b>",
            f"Configured minimum: <b>{fmt_decimal(MIN_CYCLE_M3, 3)} m³</b>",
            "",
            "✅ <b>Recommended optimized plan</b>",
            f"Optimized: {format_cycle_sequence(optimized)}",
            f"Cycles: <b>{len(optimized)}</b>",
            "",
            "Choose which cycle plan to use. The bot will calculate every cycle separately.",
        ]
    )


def build_summary(calc: dict, order_id: int | None = None) -> str:
    item = calc["recipe"]
    cycles = calc["cycles"]
    full_cycles = sum(
        1 for cycle in cycles if abs(cycle - MAX_CYCLE_M3) < Decimal("0.000000001")
    )
    partial_cycles = len(cycles) - full_cycles

    title = "🧱 <b>PRODUCTION CALCULATION</b>"
    if order_id is not None:
        title += f" #{order_id}"

    plan_label = "Optimized" if calc["cycle_plan"] == "optimized" else "Standard"
    lines = [
        title,
        "",
        f"Mix: <b>{item['name']}</b>",
        f"Design air: {fmt_decimal(item['design_air_pct'], 1)}%",
        f"Design slump: {fmt_decimal(item['slump_in'], 0)} in",
        f"Order: <b>{fmt_decimal(calc['yards'], 3)} yd³</b>",
        f"Enter in ELKON: <b>{fmt_decimal(calc['total_m3'], 6)} m³</b>",
        "",
        "🔄 <b>CYCLES</b>",
        f"Cycle plan: <b>{plan_label}</b>",
        f"Cycle sequence: {format_cycle_sequence(cycles)}",
        f"Full cycles of {fmt_decimal(MAX_CYCLE_M3, 3)} m³: {full_cycles}",
        f"Partial cycles: {partial_cycles}",
        f"Total cycles: <b>{len(cycles)}</b>",
        f"Last cycle: <b>{fmt_decimal(calc['last_cycle'], 6)} m³</b>",
    ]

    if calc["cycle_plan"] == "optimized":
        lines += [
            "",
            "🧠 <b>SMART OPTIMIZATION APPLIED</b>",
            f"Original standard plan: {format_cycle_sequence(calc['standard_cycles'])}",
            f"Optimized plan: {format_cycle_sequence(cycles)}",
        ]

    if calc["has_warning"]:
        lines += [
            "",
            "⚠️ <b>WARNING</b>",
            f"The last cycle is below the configured minimum of {fmt_decimal(MIN_CYCLE_M3, 3)} m³.",
            "Do not start production without administrator approval.",
        ]
    else:
        lines += ["", "✅ Every selected cycle meets the configured cycle limits."]

    lines += ["", "⚙️ <b>ELKON RECIPE PER 1 m³</b>"]
    for name, value in calc["kg_per_m3"].items():
        if name == "Water":
            lines.append(
                f"{name}: <b>{fmt_decimal(value, 2)} kg/m³ "
                f"(≈ {fmt_decimal(value, 2)} L/m³)</b>"
            )
        else:
            lines.append(f"{name}: <b>{fmt_decimal(value, 2)} kg/m³</b>")

    lines += ["", "⚖️ <b>TOTAL MATERIALS FOR THE ORDER</b>"]
    for name, value in calc["materials_total_kg"].items():
        if name == "Water":
            lines.append(
                f"{name}: <b>{fmt_decimal(value, 2)} kg "
                f"(≈ {fmt_decimal(value, 2)} L)</b>"
            )
        else:
            lines.append(f"{name}: <b>{fmt_decimal(value, 2)} kg</b>")
    lines.append(
        f"Total weight: <b>{fmt_decimal(sum(calc['materials_total_kg'].values()), 2)} kg</b>"
    )

    lines += ["", "🧪 <b>TOTAL ADMIXTURES FOR THE ORDER</b>"]
    for name, value in calc["admixtures_total_oz"].items():
        shown = "—" if value == 0 else f"{fmt_decimal(value, 2)} oz"
        lines.append(f"{name}: <b>{shown}</b>")

    lines += [
        "",
        "ℹ️ Admixtures are shown in the original certified unit — oz/yd³. "
        "Do not convert them to liters unless the ounce type and product density are confirmed.",
    ]
    return "\n".join(lines)


def build_cycle_text(row: dict, *, cycle_plan: str) -> str:
    plan_label = "Optimized" if cycle_plan == "optimized" else "Standard"
    lines = [
        f"🔹 <b>Cycle #{row['number']} — {plan_label}</b>",
        f"Volume: <b>{fmt_decimal(row['m3'], 6)} m³</b> "
        f"({fmt_decimal(row['yd3'], 6)} yd³)",
        "",
        "<b>Materials:</b>",
    ]
    for name, value in row["materials_kg"].items():
        if name == "Water":
            lines.append(
                f"{name}: {fmt_decimal(value, 2)} kg "
                f"(≈ {fmt_decimal(value, 2)} L)"
            )
        else:
            lines.append(f"{name}: {fmt_decimal(value, 2)} kg")
    lines.append(
        f"Cycle weight: <b>{fmt_decimal(sum(row['materials_kg'].values()), 2)} kg</b>"
    )
    lines += ["", "<b>Admixtures:</b>"]
    for name, value in row["admixtures_oz"].items():
        shown = "—" if value == 0 else f"{fmt_decimal(value, 2)} oz"
        lines.append(f"{name}: {shown}")
    return "\n".join(lines)


def chunk_items(items: Iterable[str], max_chars: int = 3500) -> list[str]:
    chunks: list[str] = []
    current = ""
    for item in items:
        candidate = item if not current else current + "\n\n" + item
        if len(candidate) > max_chars and current:
            chunks.append(current)
            current = item
        else:
            current = candidate
    if current:
        chunks.append(current)
    return chunks


# ============================================================
# ACCESS HELPERS
# ============================================================


async def require_role(message: Message, allowed: set[str]) -> str | None:
    role = await get_user_role(message.from_user.id)
    if role not in allowed:
        await message.answer(
            "⛔ Access denied.\n\n"
            f"Your Telegram ID: <code>{message.from_user.id}</code>\n"
            "Ask an administrator to assign the correct role.",
            reply_markup=main_menu_for_role(role),
        )
        return None
    await update_user_identity(message)
    return role


async def require_callback_role(callback: CallbackQuery, allowed: set[str]) -> str | None:
    role = await get_user_role(callback.from_user.id)
    if role not in allowed:
        await callback.answer("Access denied", show_alert=True)
        return None
    return role


async def show_main_menu(message: Message) -> None:
    role = await get_user_role(message.from_user.id)
    await update_user_identity(message)
    role_text = ROLE_LABELS.get(role, "No role assigned")
    await message.answer(
        f"28 CONCRETE\n\nYour role: <b>{role_text}</b>",
        reply_markup=main_menu_for_role(role),
    )


async def send_calculation(
    message: Message,
    yards: Decimal,
    mix_key: str,
    *,
    cycle_plan: str,
    save: bool,
    existing_order_id: int | None = None,
    actor_user_id: int | None = None,
    actor_username: str | None = None,
) -> None:
    calc = calculate_order(yards, mix_key, cycle_plan=cycle_plan)
    order_id = existing_order_id
    if save:
        user_id = actor_user_id if actor_user_id is not None else message.from_user.id
        username = actor_username if actor_user_id is not None else message.from_user.username
        order_id = await save_order(
            user_id=user_id,
            username=username,
            mix_key=mix_key,
            yards=calc["yards"],
            cubic_meters=q6(calc["total_m3"]),
            cycles_count=len(calc["cycles"]),
            last_cycle_m3=q6(calc["last_cycle"]),
            has_warning=calc["has_warning"],
            cycle_plan=cycle_plan,
        )

    await message.answer(build_summary(calc, order_id), reply_markup=PRODUCTION_MENU)
    await message.answer(
        "📋 <b>CALCULATION BY EACH CYCLE</b>\n"
        "Every material and admixture value below is calculated separately for the selected cycle plan."
    )
    cycle_texts = [
        build_cycle_text(row, cycle_plan=cycle_plan)
        for row in calc["cycle_rows"]
    ]
    for chunk in chunk_items(cycle_texts):
        await message.answer(chunk)


# ============================================================
# ADMIN EDIT HELPERS
# ============================================================


def retail_edit_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=f"{psi} PSI — {fmt_money(RETAIL_PRICES[psi])}",
                    callback_data=f"adminedit:retail:{psi}",
                )
            ]
            for psi in sorted(RETAIL_PRICES)
        ]
    )


def fob_edit_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=f"{psi} PSI — {fmt_money(FOB_CONTRACT_PRICES[psi])}",
                    callback_data=f"adminedit:fob:{psi}",
                )
            ]
            for psi in sorted(FOB_CONTRACT_PRICES)
        ]
    )


def additive_edit_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=f"{ADDITIVE_LABELS[slug]} — {fmt_money(ADDITIVE_PRICES[slug])}",
                    callback_data=f"adminedit:additive:{slug}",
                )
            ]
            for slug in ADDITIVE_LABELS
        ]
    )


def short_load_edit_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=f"{yards} yd³ — {fmt_money(SHORT_LOAD_FEES[yards])}",
                    callback_data=f"adminedit:shortload:{yards}",
                )
            ]
            for yards in sorted(SHORT_LOAD_FEES)
        ]
    )


def production_settings_keyboard() -> InlineKeyboardMarkup:
    labels = {
        "min_cycle_m3": f"Minimum cycle — {APP_SETTINGS['min_cycle_m3']} m³",
        "max_cycle_m3": f"Maximum cycle — {APP_SETTINGS['max_cycle_m3']} m³",
        "max_order_yd3": f"Maximum order — {APP_SETTINGS['max_order_yd3']} yd³",
        "weekday_hours": f"Weekday hours — {APP_SETTINGS['weekday_hours']}",
        "saturday_hours": f"Saturday hours — {APP_SETTINGS['saturday_hours']}",
        "after_hours_fee": f"After-hours fee — ${APP_SETTINGS['after_hours_fee']}",
        "fob_terms": f"FOB terms — {APP_SETTINGS['fob_terms']}",
        "short_load_note": "Short-load note",
    }
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=label,
                    callback_data=f"adminedit:setting:{key}",
                )
            ]
            for key, label in labels.items()
        ]
    )


def recipe_selection_keyboard() -> InlineKeyboardMarkup:
    rows = []
    for psi in [3000, 3500, 4000, 4500, 5000]:
        rows.append(
            [
                InlineKeyboardButton(
                    text=f"{psi} Non-Air", callback_data=f"adminrecipe:{psi}_no_air"
                ),
                InlineKeyboardButton(
                    text=f"{psi} Air", callback_data=f"adminrecipe:{psi}_air"
                ),
            ]
        )
    return InlineKeyboardMarkup(inline_keyboard=rows)


def recipe_field_keyboard(mix_key: str) -> InlineKeyboardMarkup:
    item = RECIPES[mix_key]
    current = {
        "cement_lb": item["materials_lb"]["Cement"],
        "stone_lb": item["materials_lb"]["Stone"],
        "sand_lb": item["materials_lb"]["Sand"],
        "water_lb": item["materials_lb"]["Water"],
        "sikament_oz": item["admixtures_oz"]["Sikament 475"],
        "air_oz": item["admixtures_oz"]["Air"],
        "sikafume_oz": item["admixtures_oz"]["SikaFume 290"],
        "slump_in": item["slump_in"],
        "design_air_pct": item["design_air_pct"],
    }
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=f"{RECIPE_COLUMNS[column]} — {fmt_decimal(value, 2)}",
                    callback_data=f"adminedit:recipe:{mix_key}:{column}",
                )
            ]
            for column, value in current.items()
        ]
    )


def describe_edit(category: str, key: str, subkey: str | None = None) -> str:
    if category == "retail":
        return f"Retail price for {key} PSI"
    if category == "fob":
        return f"FOB price for {key} PSI"
    if category == "additive":
        return f"Additive price: {ADDITIVE_LABELS[key]}"
    if category == "shortload":
        return f"Short Load Fee for {key} yd³"
    if category == "setting":
        setting_labels = {
            "min_cycle_m3": "Minimum cycle, m³",
            "max_cycle_m3": "Maximum cycle, m³",
            "max_order_yd3": "Maximum order, yd³",
            "weekday_hours": "Weekday hours",
            "saturday_hours": "Saturday hours",
            "after_hours_fee": "After-hours fee",
            "fob_terms": "FOB payment terms",
            "short_load_note": "Short-load note",
        }
        return setting_labels[key]
    if category == "recipe" and subkey:
        return f"{RECIPES[key]['name']} — {RECIPE_COLUMNS[subkey]}"
    return "Setting"


def current_edit_value(category: str, key: str, subkey: str | None = None) -> str:
    if category == "retail":
        return str(RETAIL_PRICES[int(key)])
    if category == "fob":
        return str(FOB_CONTRACT_PRICES[int(key)])
    if category == "additive":
        return str(ADDITIVE_PRICES[key])
    if category == "shortload":
        return str(SHORT_LOAD_FEES[int(key)])
    if category == "setting":
        return APP_SETTINGS[key]
    if category == "recipe" and subkey:
        item = RECIPES[key]
        mapping = {
            "cement_lb": item["materials_lb"]["Cement"],
            "stone_lb": item["materials_lb"]["Stone"],
            "sand_lb": item["materials_lb"]["Sand"],
            "water_lb": item["materials_lb"]["Water"],
            "sikament_oz": item["admixtures_oz"]["Sikament 475"],
            "air_oz": item["admixtures_oz"]["Air"],
            "sikafume_oz": item["admixtures_oz"]["SikaFume 290"],
            "slump_in": item["slump_in"],
            "design_air_pct": item["design_air_pct"],
        }
        return str(mapping[subkey])
    raise ValueError("Unknown setting")


async def apply_admin_edit(
    category: str,
    key: str,
    value: str,
    subkey: str | None = None,
) -> None:
    if category == "retail":
        await upsert_setting(f"retail.{int(key)}", value)
    elif category == "fob":
        await upsert_setting(f"fob.{int(key)}", value)
    elif category == "additive":
        await upsert_setting(f"additive.{key}", value)
    elif category == "shortload":
        await upsert_setting(f"shortload.{int(key)}", value)
    elif category == "setting":
        if key == "min_cycle_m3" and Decimal(value) > Decimal(APP_SETTINGS["max_cycle_m3"]):
            raise ValueError("Minimum cycle cannot be greater than maximum cycle")
        if key == "max_cycle_m3" and Decimal(value) < Decimal(APP_SETTINGS["min_cycle_m3"]):
            raise ValueError("Maximum cycle cannot be smaller than minimum cycle")
        await upsert_setting(key, value)
    elif category == "recipe" and subkey:
        await update_recipe_value(key, subkey, Decimal(value))
    else:
        raise ValueError("Unknown edit category")
    await load_runtime_settings()


# ============================================================
# NAVIGATION
# ============================================================


@router.message(Command("start"))
async def start_handler(message: Message, state: FSMContext) -> None:
    await state.clear()
    await show_main_menu(message)


@router.message(StateFilter("*"), Command("cancel"))
@router.message(StateFilter("*"), F.text == "❌ Cancel")
@router.message(StateFilter("*"), F.text == "⬅️ Main Menu")
async def cancel_or_main_handler(message: Message, state: FSMContext) -> None:
    await state.clear()
    await show_main_menu(message)


@router.message(F.text == "⬅️ Admin Settings")
async def back_to_admin_handler(message: Message, state: FSMContext) -> None:
    await state.clear()
    if not await require_role(message, {"admin"}):
        return
    await message.answer("Admin settings.", reply_markup=ADMIN_MENU)


@router.message(Command("myid"))
@router.message(F.text == "🆔 My Telegram ID")
async def my_id_handler(message: Message) -> None:
    role = await get_user_role(message.from_user.id)
    await message.answer(
        f"Your Telegram ID: <code>{message.from_user.id}</code>\n"
        f"Current role: <b>{ROLE_LABELS.get(role, 'No role assigned')}</b>",
        reply_markup=main_menu_for_role(role),
    )


@router.message(F.text == "ℹ️ Help")
async def help_handler(message: Message) -> None:
    role = await get_user_role(message.from_user.id)
    await message.answer(
        "<b>28 CONCRETE Bot</b>\n\n"
        "Salesperson: sales prices, additives, short-load fees, commercial terms, and the concrete volume calculator.\n"
        "Administrator: full access, including the concrete volume calculator, production calculations, smart cycle optimization, cycle-by-cycle details, user roles, prices, settings, and recipes.\n\n"
        "Use /myid to display your Telegram ID. An administrator assigns access by Telegram ID.",
        reply_markup=main_menu_for_role(role),
    )


# ============================================================
# SALES MENU
# ============================================================


@router.message(F.text == "💵 Sales Menu")
async def sales_menu_handler(message: Message, state: FSMContext) -> None:
    if not await require_role(message, {"salesperson", "admin"}):
        return
    await state.clear()
    await message.answer("💵 <b>SALES MENU</b>", reply_markup=SALES_MENU)


@router.message(F.text == "💰 Retail Prices")
async def retail_prices_handler(message: Message) -> None:
    if not await require_role(message, {"salesperson", "admin"}):
        return
    lines = ["💰 <b>PRICE PER 1 yd³ OF CONCRETE</b>", ""]
    for psi, price in sorted(RETAIL_PRICES.items()):
        lines.append(f"{psi} PSI — <b>{fmt_money(price)}</b>")
    await message.answer("\n".join(lines), reply_markup=SALES_MENU)


@router.message(F.text == "🤝 FOB Prices")
async def fob_prices_handler(message: Message) -> None:
    if not await require_role(message, {"salesperson", "admin"}):
        return
    lines = ["🤝 <b>FOB PRICES FOR CONTRACT CUSTOMERS</b>", "Price per 1 yd³:", ""]
    for psi, price in sorted(FOB_CONTRACT_PRICES.items()):
        lines.append(f"{psi} PSI — <b>{fmt_money(price)}</b>")
    lines += ["", f"💳 FOB payment terms: <b>{escape(APP_SETTINGS['fob_terms'])}</b>."]
    await message.answer("\n".join(lines), reply_markup=SALES_MENU)


@router.message(F.text == "➕ Additives")
async def additives_prices_handler(message: Message) -> None:
    if not await require_role(message, {"salesperson", "admin"}):
        return
    lines = ["➕ <b>ADDITIVES — PRICE PER 1 yd³</b>", ""]
    for slug, label in ADDITIVE_LABELS.items():
        lines.append(f"{label} — <b>{fmt_money(ADDITIVE_PRICES[slug])}</b>")
    await message.answer("\n".join(lines), reply_markup=SALES_MENU)


@router.message(F.text == "🚚 Short Load Fee")
async def short_load_handler(message: Message) -> None:
    if not await require_role(message, {"salesperson", "admin"}):
        return
    lines = ["🚚 <b>SHORT LOAD FEE</b>", "Applies when the order is less than 10 yd³.", ""]
    for yards, fee in sorted(SHORT_LOAD_FEES.items()):
        lines.append(f"{yards} yd³ — <b>{fmt_money(fee)}</b>")
    lines += ["", f"⚠️ {escape(APP_SETTINGS['short_load_note'])}"]
    await message.answer("\n".join(lines), reply_markup=SALES_MENU)


@router.message(F.text == "🕒 Hours & Terms")
async def hours_handler(message: Message) -> None:
    if not await require_role(message, {"salesperson", "admin"}):
        return
    after_hours_fee = Decimal(APP_SETTINGS["after_hours_fee"])
    await message.answer(
        "🕒 <b>WORKING HOURS & TERMS</b>\n\n"
        f"Monday–Friday: <b>{escape(APP_SETTINGS['weekday_hours'])}</b>\n"
        f"Saturday: <b>{escape(APP_SETTINGS['saturday_hours'])}</b>\n\n"
        "🌙 Opening the plant after working hours for production: "
        f"<b>{fmt_money(after_hours_fee)} fee</b>, regardless of the number of yards.\n\n"
        f"💳 FOB contract customers: <b>{escape(APP_SETTINGS['fob_terms'])} terms</b>.",
        reply_markup=SALES_MENU,
    )


# ============================================================
# CONCRETE VOLUME CALCULATOR
# ============================================================


@router.message(Command("volume"))
@router.message(F.text == "📐 Concrete Volume Calculator")
async def volume_calculator_start(message: Message, state: FSMContext) -> None:
    if not await require_role(message, {"salesperson", "admin"}):
        return
    await state.clear()
    await state.set_state(VolumeCalcState.choosing_shape)
    await message.answer(
        "📐 <b>CONCRETE VOLUME CALCULATOR</b>\n\n"
        "Choose the structure type. The calculator uses feet, inches, and cubic yards.",
        reply_markup=volume_shape_keyboard(),
    )


@router.callback_query(VolumeCalcState.choosing_shape, F.data == "volume:cancel")
async def volume_calculator_cancel(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await callback.answer("Cancelled")
    if callback.message:
        await callback.message.edit_text("❌ Volume calculation cancelled.")
        await callback.message.answer("💵 <b>SALES MENU</b>", reply_markup=SALES_MENU)


@router.callback_query(
    VolumeCalcState.choosing_shape,
    F.data.startswith("volume:shape:"),
)
async def volume_shape_selected(callback: CallbackQuery, state: FSMContext) -> None:
    if not await require_callback_role(callback, {"salesperson", "admin"}):
        return
    shape_key = callback.data.split(":", maxsplit=2)[2]
    if shape_key not in VOLUME_SHAPES:
        await callback.answer("Unknown shape", show_alert=True)
        return

    await state.update_data(shape_key=shape_key, field_index=0, values={})
    await state.set_state(VolumeCalcState.waiting_value)
    await callback.answer()
    if callback.message:
        await callback.message.edit_text(volume_field_prompt(shape_key, 0))


@router.message(VolumeCalcState.waiting_value)
async def volume_value_handler(message: Message, state: FSMContext) -> None:
    if not await require_role(message, {"salesperson", "admin"}):
        await state.clear()
        return

    data = await state.get_data()
    shape_key = data.get("shape_key")
    field_index = int(data.get("field_index", 0))
    if shape_key not in VOLUME_SHAPES:
        await state.clear()
        await message.answer("Calculation data expired. Start again.", reply_markup=SALES_MENU)
        return

    fields = VOLUME_SHAPES[shape_key]["fields"]
    if field_index >= len(fields):
        await state.clear()
        await message.answer("Calculation data expired. Start again.", reply_markup=SALES_MENU)
        return

    key, label, unit = fields[field_index]
    try:
        value = parse_decimal_input(message.text or "", positive=True)
    except ValueError as error:
        await message.answer(str(error))
        return

    if key == "quantity":
        if value != value.to_integral_value():
            await message.answer("Quantity must be a whole number, for example <code>1</code>.")
            return
        if value > 1000:
            await message.answer("Quantity cannot exceed 1,000 in one calculation.")
            return
    elif value > Decimal("100000"):
        await message.answer(f"{label} is too large. Check the entered unit ({unit}).")
        return

    values = dict(data.get("values", {}))
    values[key] = str(value)
    next_index = field_index + 1

    if next_index < len(fields):
        await state.update_data(values=values, field_index=next_index)
        await message.answer(volume_field_prompt(shape_key, next_index))
        return

    await state.update_data(values=values)
    await state.set_state(VolumeCalcState.waiting_waste)
    await message.answer(
        "Enter the waste allowance percentage.\n"
        "Recommended starting point: <code>5</code>\n"
        "Enter <code>0</code> for no waste allowance."
    )


@router.message(VolumeCalcState.waiting_waste)
async def volume_waste_handler(message: Message, state: FSMContext) -> None:
    if not await require_role(message, {"salesperson", "admin"}):
        await state.clear()
        return

    try:
        waste_pct = parse_decimal_input(message.text or "", positive=False)
    except ValueError as error:
        await message.answer(str(error))
        return
    if waste_pct > Decimal("50"):
        await message.answer("Waste percentage cannot exceed 50%.")
        return

    data = await state.get_data()
    shape_key = data.get("shape_key")
    raw_values = data.get("values", {})
    if shape_key not in VOLUME_SHAPES:
        await state.clear()
        await message.answer("Calculation data expired. Start again.", reply_markup=SALES_MENU)
        return

    try:
        values = {key: Decimal(str(value)) for key, value in raw_values.items()}
        calc = calculate_concrete_volume(shape_key, values, waste_pct)
    except (ValueError, InvalidOperation, KeyError) as error:
        await state.clear()
        await message.answer(f"Calculation failed: {escape(str(error))}", reply_markup=SALES_MENU)
        return

    await state.clear()
    await message.answer(build_volume_result(calc), reply_markup=SALES_MENU)


# ============================================================
# PRODUCTION
# ============================================================


@router.message(F.text.in_({"🏭 Production", "🔐 Production"}))
async def production_entry_handler(message: Message, state: FSMContext) -> None:
    if not await require_role(message, {"admin"}):
        return
    await state.clear()
    await message.answer("🏭 <b>PRODUCTION MENU</b>", reply_markup=PRODUCTION_MENU)


@router.message(F.text == "🧮 New Calculation")
async def new_order_handler(message: Message, state: FSMContext) -> None:
    if not await require_role(message, {"admin"}):
        return
    await state.set_state(OrderState.choosing_psi)
    await message.answer("Choose concrete strength:", reply_markup=psi_keyboard())


@router.callback_query(OrderState.choosing_psi, F.data.startswith("psi:"))
async def choose_psi_handler(callback: CallbackQuery, state: FSMContext) -> None:
    if not await require_callback_role(callback, {"admin"}):
        return
    psi = int(callback.data.split(":", maxsplit=1)[1])
    if psi not in RETAIL_PRICES:
        await callback.answer("Unknown PSI", show_alert=True)
        return
    await state.update_data(psi=psi)
    await state.set_state(OrderState.choosing_air)
    await callback.answer()
    if callback.message:
        await callback.message.edit_text(
            f"Selected: <b>{psi} PSI</b>\nNow choose the mix type:",
            reply_markup=air_keyboard(),
        )


@router.callback_query(OrderState.choosing_air, F.data.startswith("air:"))
async def choose_air_handler(callback: CallbackQuery, state: FSMContext) -> None:
    if not await require_callback_role(callback, {"admin"}):
        return
    air = callback.data.split(":", maxsplit=1)[1] == "1"
    data = await state.get_data()
    psi = int(data["psi"])
    mix_key = f"{psi}_{'air' if air else 'no_air'}"
    if mix_key not in RECIPES:
        await callback.answer("Recipe not found", show_alert=True)
        return
    await state.update_data(mix_key=mix_key)
    await state.set_state(OrderState.waiting_yards)
    await callback.answer()
    if callback.message:
        await callback.message.edit_text(
            f"Mix: <b>{RECIPES[mix_key]['name']}</b>\n\n"
            "Enter the order volume in cubic yards.\n"
            "You can use a dot or a comma: <code>7.15</code> or <code>7,15</code>."
        )


@router.message(OrderState.waiting_yards)
async def yards_handler(message: Message, state: FSMContext) -> None:
    if not await require_role(message, {"admin"}):
        await state.clear()
        return
    try:
        yards = parse_yards(message.text or "")
    except ValueError as error:
        await message.answer(str(error))
        return
    data = await state.get_data()
    mix_key = data.get("mix_key")
    if mix_key not in RECIPES:
        await state.clear()
        await message.answer("No recipe selected. Start a new calculation.", reply_markup=PRODUCTION_MENU)
        return
    preview = calculate_order(yards, mix_key, cycle_plan="standard")
    if preview["optimization_available"]:
        await state.update_data(yards=str(yards), mix_key=mix_key)
        await state.set_state(OrderState.choosing_cycle_plan)
        await message.answer(
            build_cycle_plan_offer(preview),
            reply_markup=cycle_plan_keyboard(),
        )
        return

    await state.clear()
    await send_calculation(
        message,
        yards,
        mix_key,
        cycle_plan="standard",
        save=True,
    )


@router.callback_query(
    OrderState.choosing_cycle_plan,
    F.data.in_({"cycleplan:optimized", "cycleplan:standard", "cycleplan:cancel"}),
)
async def choose_cycle_plan_handler(
    callback: CallbackQuery,
    state: FSMContext,
) -> None:
    if not await require_callback_role(callback, {"admin"}):
        return

    action = callback.data.split(":", maxsplit=1)[1]
    if action == "cancel":
        await state.clear()
        await callback.answer("Cancelled")
        if callback.message:
            await callback.message.edit_text("❌ Calculation cancelled.")
            await callback.message.answer(
                "🏭 <b>PRODUCTION MENU</b>",
                reply_markup=PRODUCTION_MENU,
            )
        return

    data = await state.get_data()
    mix_key = data.get("mix_key")
    yards_raw = data.get("yards")
    if mix_key not in RECIPES or not yards_raw:
        await state.clear()
        await callback.answer("Calculation data expired", show_alert=True)
        return

    yards = Decimal(str(yards_raw))
    await state.clear()
    await callback.answer("Cycle plan selected")
    if callback.message:
        await callback.message.edit_reply_markup(reply_markup=None)
        await send_calculation(
            callback.message,
            yards,
            mix_key,
            cycle_plan=action,
            save=True,
            actor_user_id=callback.from_user.id,
            actor_username=callback.from_user.username,
        )


@router.message(Command("history"))
@router.message(F.text == "📚 History")
async def history_handler(message: Message) -> None:
    if not await require_role(message, {"admin"}):
        return
    rows = await get_recent_orders()
    if not rows:
        await message.answer("History is empty so far.", reply_markup=PRODUCTION_MENU)
        return
    lines = ["📚 <b>RECENT CALCULATIONS</b>", ""]
    for row in rows:
        mix_key = row["mix_key"]
        mix_name = RECIPES[mix_key]["name"] if mix_key in RECIPES else "Old recipe"
        warning = " ⚠️" if row["has_warning"] else ""
        created = row["created_at"].strftime("%m/%d/%Y %I:%M %p")
        plan = str(row["cycle_plan"] or "standard").title()
        lines.append(
            f"#{row['id']} — <b>{mix_name}</b> — "
            f"{fmt_decimal(Decimal(row['yards']), 3)} yd³ — "
            f"{row['cycles_count']} cycle(s) — {plan}{warning}\n"
            f"   user <code>{row['telegram_user_id']}</code> — {created}"
        )
    lines += ["", "Open a calculation: <code>/order NUMBER</code>"]
    await message.answer("\n".join(lines), reply_markup=PRODUCTION_MENU)


@router.message(Command("order"))
async def order_handler(message: Message) -> None:
    if not await require_role(message, {"admin"}):
        return
    parts = (message.text or "").split(maxsplit=1)
    if len(parts) != 2 or not parts[1].isdigit():
        await message.answer("Example: <code>/order 15</code>")
        return
    order_id = int(parts[1])
    row = await get_order(order_id)
    if row is None:
        await message.answer("That order was not found.")
        return
    mix_key = row["mix_key"]
    if mix_key not in RECIPES:
        await message.answer(
            "This order belongs to an older bot version without a saved recipe type. "
            "Recalculation is not possible."
        )
        return
    cycle_plan = str(row["cycle_plan"] or "standard")
    if cycle_plan not in {"standard", "optimized"}:
        cycle_plan = "standard"
    await send_calculation(
        message,
        Decimal(row["yards"]),
        mix_key,
        cycle_plan=cycle_plan,
        save=False,
        existing_order_id=order_id,
    )


# ============================================================
# ADMIN SETTINGS AND ROLES
# ============================================================


@router.message(F.text == "⚙️ Admin Settings")
async def admin_settings_handler(message: Message, state: FSMContext) -> None:
    if not await require_role(message, {"admin"}):
        return
    await state.clear()
    await message.answer("⚙️ <b>ADMIN SETTINGS</b>", reply_markup=ADMIN_MENU)


@router.message(F.text == "👥 Users & Roles")
async def users_roles_handler(message: Message, state: FSMContext) -> None:
    if not await require_role(message, {"admin"}):
        return
    await state.clear()
    await message.answer("👥 <b>USERS & ROLES</b>", reply_markup=ROLE_MENU)


@router.message(Command("users"))
@router.message(F.text == "📋 User List")
async def user_list_handler(message: Message) -> None:
    if not await require_role(message, {"admin"}):
        return
    rows = await list_users_with_roles()
    if not rows:
        await message.answer("No assigned users.", reply_markup=ROLE_MENU)
        return
    lines = ["📋 <b>ASSIGNED USERS</b>", ""]
    for row in rows:
        username = f"@{row['telegram_username']}" if row["telegram_username"] else "no username"
        display = escape(row["display_name"] or "")
        lines.append(
            f"<code>{row['telegram_user_id']}</code> — <b>{ROLE_LABELS[row['role']]}</b>\n"
            f"{display} {username}".strip()
        )
    await message.answer("\n\n".join(lines), reply_markup=ROLE_MENU)


@router.message(F.text == "➕ Assign Role")
async def assign_role_start(message: Message, state: FSMContext) -> None:
    if not await require_role(message, {"admin"}):
        return
    await state.set_state(RoleState.waiting_assign_id)
    await message.answer(
        "Send the user's numeric Telegram ID.\n\n"
        "The user can find it by pressing “My Telegram ID” or using /myid."
    )


@router.message(RoleState.waiting_assign_id)
async def assign_role_id(message: Message, state: FSMContext) -> None:
    if not await require_role(message, {"admin"}):
        await state.clear()
        return
    text = (message.text or "").strip()
    if not text.isdigit():
        await message.answer("Enter a numeric Telegram ID.")
        return
    await state.update_data(target_user_id=int(text))
    await state.set_state(RoleState.choosing_role)
    await message.answer("Choose the role:", reply_markup=role_choice_keyboard())


@router.callback_query(RoleState.choosing_role, F.data.startswith("role:set:"))
async def assign_role_finish(callback: CallbackQuery, state: FSMContext) -> None:
    if not await require_callback_role(callback, {"admin"}):
        return
    role = callback.data.split(":", maxsplit=2)[2]
    if role not in VALID_ROLES:
        await callback.answer("Invalid role", show_alert=True)
        return
    data = await state.get_data()
    target_user_id = int(data["target_user_id"])
    await set_user_role(target_user_id, role)
    await state.clear()
    await callback.answer("Role assigned")
    if callback.message:
        await callback.message.answer(
            f"✅ User <code>{target_user_id}</code> is now <b>{ROLE_LABELS[role]}</b>.",
            reply_markup=ROLE_MENU,
        )


@router.message(F.text == "🗑 Remove Access")
async def remove_role_start(message: Message, state: FSMContext) -> None:
    if not await require_role(message, {"admin"}):
        return
    await state.set_state(RoleState.waiting_remove_id)
    await message.answer("Send the numeric Telegram ID to remove.")


@router.message(RoleState.waiting_remove_id)
async def remove_role_finish(message: Message, state: FSMContext) -> None:
    if not await require_role(message, {"admin"}):
        await state.clear()
        return
    text = (message.text or "").strip()
    if not text.isdigit():
        await message.answer("Enter a numeric Telegram ID.")
        return
    target_user_id = int(text)
    if target_user_id in BOOTSTRAP_ADMIN_IDS:
        await message.answer(
            "This user is a bootstrap administrator from ADMIN_TELEGRAM_IDS and cannot be removed here."
        )
        return
    removed = await remove_user_role(target_user_id)
    await state.clear()
    await message.answer(
        "✅ Access removed." if removed else "No assigned role was found for that ID.",
        reply_markup=ROLE_MENU,
    )


@router.message(Command("setrole"))
async def setrole_command(message: Message) -> None:
    if not await require_role(message, {"admin"}):
        return
    parts = (message.text or "").split()
    if len(parts) != 3 or not parts[1].isdigit() or parts[2].lower() not in VALID_ROLES:
        await message.answer(
            "Usage: <code>/setrole TELEGRAM_ID salesperson|admin</code>"
        )
        return
    user_id = int(parts[1])
    role = parts[2].lower()
    await set_user_role(user_id, role)
    await message.answer(
        f"✅ User <code>{user_id}</code> is now <b>{ROLE_LABELS[role]}</b>."
    )


@router.message(Command("delrole"))
async def delrole_command(message: Message) -> None:
    if not await require_role(message, {"admin"}):
        return
    parts = (message.text or "").split()
    if len(parts) != 2 or not parts[1].isdigit():
        await message.answer("Usage: <code>/delrole TELEGRAM_ID</code>")
        return
    user_id = int(parts[1])
    if user_id in BOOTSTRAP_ADMIN_IDS:
        await message.answer("A bootstrap administrator cannot be removed here.")
        return
    removed = await remove_user_role(user_id)
    await message.answer("✅ Access removed." if removed else "No role found.")


@router.message(F.text == "💰 Edit Retail Prices")
async def edit_retail_handler(message: Message) -> None:
    if not await require_role(message, {"admin"}):
        return
    await message.answer("Select a retail price to edit:", reply_markup=retail_edit_keyboard())


@router.message(F.text == "🤝 Edit FOB Prices")
async def edit_fob_handler(message: Message) -> None:
    if not await require_role(message, {"admin"}):
        return
    await message.answer("Select an FOB price to edit:", reply_markup=fob_edit_keyboard())


@router.message(F.text == "➕ Edit Additives")
async def edit_additives_handler(message: Message) -> None:
    if not await require_role(message, {"admin"}):
        return
    await message.answer("Select an additive price to edit:", reply_markup=additive_edit_keyboard())


@router.message(F.text == "🚚 Edit Short Load")
async def edit_short_load_handler(message: Message) -> None:
    if not await require_role(message, {"admin"}):
        return
    await message.answer("Select a Short Load Fee to edit:", reply_markup=short_load_edit_keyboard())


@router.message(F.text == "⚙️ Production Settings")
async def production_settings_handler(message: Message) -> None:
    if not await require_role(message, {"admin"}):
        return
    await message.answer(
        "Select a production setting to edit:",
        reply_markup=production_settings_keyboard(),
    )


@router.message(F.text == "🧪 Edit Recipes")
async def edit_recipes_handler(message: Message) -> None:
    if not await require_role(message, {"admin"}):
        return
    await message.answer("Select a certified recipe:", reply_markup=recipe_selection_keyboard())


@router.callback_query(F.data.startswith("adminrecipe:"))
async def choose_recipe_admin(callback: CallbackQuery) -> None:
    if not await require_callback_role(callback, {"admin"}):
        return
    mix_key = callback.data.split(":", maxsplit=1)[1]
    if mix_key not in RECIPES:
        await callback.answer("Recipe not found", show_alert=True)
        return
    await callback.answer()
    if callback.message:
        await callback.message.answer(
            f"🧪 <b>{RECIPES[mix_key]['name']}</b>\n"
            "Select the value to edit. Certified recipe changes should be approved before production.",
            reply_markup=recipe_field_keyboard(mix_key),
        )


@router.callback_query(F.data.startswith("adminedit:"))
async def admin_edit_start(callback: CallbackQuery, state: FSMContext) -> None:
    if not await require_callback_role(callback, {"admin"}):
        return
    parts = callback.data.split(":")
    if len(parts) < 3:
        await callback.answer("Invalid setting", show_alert=True)
        return
    category = parts[1]
    key = parts[2]
    subkey = parts[3] if len(parts) == 4 else None
    try:
        old_value = current_edit_value(category, key, subkey)
        description = describe_edit(category, key, subkey)
    except (KeyError, ValueError):
        await callback.answer("Invalid setting", show_alert=True)
        return
    await state.set_state(AdminEditState.waiting_value)
    await state.update_data(category=category, key=key, subkey=subkey, old_value=old_value)
    await callback.answer()
    if callback.message:
        await callback.message.answer(
            f"<b>{description}</b>\n"
            f"Current value: <code>{escape(str(old_value))}</code>\n\n"
            "Send the new value. Use /cancel to stop."
        )


@router.message(AdminEditState.waiting_value)
async def admin_edit_value(message: Message, state: FSMContext) -> None:
    if not await require_role(message, {"admin"}):
        await state.clear()
        return
    data = await state.get_data()
    category = data["category"]
    key = data["key"]
    subkey = data.get("subkey")
    raw = (message.text or "").strip()

    is_text = category == "setting" and key in TEXT_APP_SETTINGS
    if is_text:
        if not raw:
            await message.answer("The value cannot be empty.")
            return
        normalized = raw
    else:
        try:
            allow_zero = category == "recipe" and subkey == "air_oz"
            value = parse_decimal_input(raw, positive=not allow_zero)
        except ValueError as error:
            await message.answer(str(error))
            return
        normalized = format(value, "f")

    description = describe_edit(category, key, subkey)
    await state.update_data(new_value=normalized)
    await state.set_state(AdminEditState.confirming)
    await message.answer(
        f"Confirm the change:\n\n"
        f"<b>{description}</b>\n"
        f"Old: <code>{escape(str(data['old_value']))}</code>\n"
        f"New: <code>{escape(normalized)}</code>",
        reply_markup=confirm_keyboard(),
    )


@router.callback_query(AdminEditState.confirming, F.data.startswith("adminconfirm:"))
async def admin_edit_confirm(callback: CallbackQuery, state: FSMContext) -> None:
    if not await require_callback_role(callback, {"admin"}):
        return
    decision = callback.data.split(":", maxsplit=1)[1]
    if decision != "yes":
        await state.clear()
        await callback.answer("Cancelled")
        if callback.message:
            await callback.message.answer("Change cancelled.", reply_markup=ADMIN_MENU)
        return

    data = await state.get_data()
    try:
        await apply_admin_edit(
            data["category"],
            data["key"],
            data["new_value"],
            data.get("subkey"),
        )
    except (ValueError, InvalidOperation) as error:
        await state.clear()
        await callback.answer("Update failed", show_alert=True)
        if callback.message:
            await callback.message.answer(f"Update failed: {error}", reply_markup=ADMIN_MENU)
        return

    await state.clear()
    await callback.answer("Saved")
    if callback.message:
        await callback.message.answer(
            "✅ Setting saved and applied immediately.",
            reply_markup=ADMIN_MENU,
        )


# ============================================================
# FALLBACK AND WEBHOOK
# ============================================================


@router.message()
async def fallback_handler(message: Message) -> None:
    role = await get_user_role(message.from_user.id)
    await message.answer(
        "Use the menu buttons. Press /start to refresh your menu.",
        reply_markup=main_menu_for_role(role),
    )


@asynccontextmanager
async def lifespan(_: FastAPI):
    await init_db()
    await bot.set_webhook(
        url=WEBHOOK_URL,
        secret_token=WEBHOOK_SECRET,
        allowed_updates=dp.resolve_used_update_types(),
        drop_pending_updates=False,
    )
    yield
    await close_db()
    await bot.session.close()


app = FastAPI(title="28 CONCRETE Volume & Smart Cycle Bot", lifespan=lifespan)


@app.get("/")
async def health():
    return {
        "status": "ok",
        "service": "28-concrete-volume-smart-cycle-bot",
        "recipes": len(RECIPES),
        "role_system": True,
        "available_roles": ["admin", "salesperson"],
        "smart_cycle_optimizer": True,
        "cycle_by_cycle_calculation": True,
        "concrete_volume_calculator": True,
        "version": "4.0-volume-calculator",
        "bootstrap_admins": len(BOOTSTRAP_ADMIN_IDS),
        "webhook_path": WEBHOOK_PATH,
    }


@app.post(WEBHOOK_PATH)
async def telegram_webhook(
    request: Request,
    x_telegram_bot_api_secret_token: str | None = Header(default=None),
):
    if x_telegram_bot_api_secret_token != WEBHOOK_SECRET:
        raise HTTPException(status_code=403, detail="Invalid webhook secret")

    update_data = await request.json()
    update = Update.model_validate(update_data, context={"bot": bot})
    await dp.feed_update(bot, update)
    return {"ok": True}
