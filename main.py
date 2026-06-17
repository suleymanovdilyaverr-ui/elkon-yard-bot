import hmac
import os
import re
from contextlib import asynccontextmanager
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
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
OPERATOR_PASSWORD = os.getenv("OPERATOR_PASSWORD", "").strip()

RENDER_EXTERNAL_HOSTNAME = os.getenv("RENDER_EXTERNAL_HOSTNAME", "").strip()
WEBHOOK_BASE_URL = os.getenv("WEBHOOK_BASE_URL", "").strip()
if not WEBHOOK_BASE_URL and RENDER_EXTERNAL_HOSTNAME:
    WEBHOOK_BASE_URL = f"https://{RENDER_EXTERNAL_HOSTNAME}"

WEBHOOK_PATH = os.getenv("WEBHOOK_PATH", "/telegram-webhook").strip()
if not WEBHOOK_PATH.startswith("/"):
    WEBHOOK_PATH = "/" + WEBHOOK_PATH

YD3_TO_M3 = Decimal("0.764554858")
LB_TO_KG = Decimal("0.45359237")
MAX_CYCLE_M3 = Decimal(os.getenv("MAX_CYCLE_M3", "0.5"))
MIN_CYCLE_M3 = Decimal(os.getenv("MIN_CYCLE_M3", "0.20"))
MAX_ORDER_YD3 = Decimal(os.getenv("MAX_ORDER_YD3", "100"))

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


# ============================================================
# CERTIFIED RECIPES — VALUES FROM THE PROVIDED MIX SHEETS
# All material weights are pounds per 1 cubic yard.
# Admixtures are kept in the original certified unit: oz/yd³.
# ============================================================

RECIPES: dict[str, dict] = {
    "3000_no_air": {
        "psi": 3000,
        "air": False,
        "name": "3000 PSI — Non-Air",
        "slump_in": Decimal("6"),
        "design_air_pct": Decimal("2.5"),
        "materials_lb": {
            "Cement": Decimal("450"),
            "Stone": Decimal("1977"),
            "Sand": Decimal("1447"),
            "Water": Decimal("190"),
        },
        "admixtures_oz": {
            "Sikament 475": Decimal("31.50"),
            "Air": Decimal("0"),
            "SikaFume 290": Decimal("18.00"),
        },
    },
    "3000_air": {
        "psi": 3000,
        "air": True,
        "name": "3000 PSI — Air",
        "slump_in": Decimal("5"),
        "design_air_pct": Decimal("5.0"),
        "materials_lb": {
            "Cement": Decimal("470"),
            "Stone": Decimal("1860"),
            "Sand": Decimal("1416"),
            "Water": Decimal("198"),
        },
        "admixtures_oz": {
            "Sikament 475": Decimal("32.90"),
            "Air": Decimal("3.53"),
            "SikaFume 290": Decimal("18.80"),
        },
    },
    "3500_no_air": {
        "psi": 3500,
        "air": False,
        "name": "3500 PSI — Non-Air",
        "slump_in": Decimal("6"),
        "design_air_pct": Decimal("2.5"),
        "materials_lb": {
            "Cement": Decimal("480"),
            "Stone": Decimal("1930"),
            "Sand": Decimal("1440"),
            "Water": Decimal("201"),
        },
        "admixtures_oz": {
            "Sikament 475": Decimal("33.60"),
            "Air": Decimal("0"),
            "SikaFume 290": Decimal("19.20"),
        },
    },
    "3500_air": {
        "psi": 3500,
        "air": True,
        "name": "3500 PSI — Air",
        "slump_in": Decimal("6"),
        "design_air_pct": Decimal("5.0"),
        "materials_lb": {
            "Cement": Decimal("500"),
            "Stone": Decimal("1820"),
            "Sand": Decimal("1400"),
            "Water": Decimal("210"),
        },
        "admixtures_oz": {
            "Sikament 475": Decimal("35.00"),
            "Air": Decimal("3.75"),
            "SikaFume 290": Decimal("20.00"),
        },
    },
    "4000_no_air": {
        "psi": 4000,
        "air": False,
        "name": "4000 PSI — Non-Air",
        "slump_in": Decimal("6"),
        "design_air_pct": Decimal("2.5"),
        "materials_lb": {
            "Cement": Decimal("525"),
            "Stone": Decimal("1850"),
            "Sand": Decimal("1433"),
            "Water": Decimal("220"),
        },
        "admixtures_oz": {
            "Sikament 475": Decimal("36.75"),
            "Air": Decimal("0"),
            "SikaFume 290": Decimal("21.00"),
        },
    },
    "4000_air": {
        "psi": 4000,
        "air": True,
        "name": "4000 PSI — Air",
        "slump_in": Decimal("6"),
        "design_air_pct": Decimal("5.0"),
        "materials_lb": {
            "Cement": Decimal("545"),
            "Stone": Decimal("1800"),
            "Sand": Decimal("1332"),
            "Water": Decimal("229"),
        },
        "admixtures_oz": {
            "Sikament 475": Decimal("38.15"),
            "Air": Decimal("4.09"),
            "SikaFume 290": Decimal("21.80"),
        },
    },
    "4500_no_air": {
        "psi": 4500,
        "air": False,
        "name": "4500 PSI — Non-Air",
        "slump_in": Decimal("6"),
        "design_air_pct": Decimal("2.5"),
        "materials_lb": {
            "Cement": Decimal("550"),
            "Stone": Decimal("1830"),
            "Sand": Decimal("1403"),
            "Water": Decimal("231"),
        },
        "admixtures_oz": {
            "Sikament 475": Decimal("38.50"),
            "Air": Decimal("0"),
            "SikaFume 290": Decimal("22.00"),
        },
    },
    "4500_air": {
        "psi": 4500,
        "air": True,
        "name": "4500 PSI — Air",
        "slump_in": Decimal("6"),
        "design_air_pct": Decimal("5.0"),
        "materials_lb": {
            "Cement": Decimal("564"),
            "Stone": Decimal("1780"),
            "Sand": Decimal("1315"),
            "Water": Decimal("237"),
        },
        "admixtures_oz": {
            "Sikament 475": Decimal("39.48"),
            "Air": Decimal("4.23"),
            "SikaFume 290": Decimal("22.56"),
        },
    },
    "5000_no_air": {
        "psi": 5000,
        "air": False,
        "name": "5000 PSI — Non-Air",
        "slump_in": Decimal("6"),
        "design_air_pct": Decimal("2.5"),
        "materials_lb": {
            "Cement": Decimal("580"),
            "Stone": Decimal("1830"),
            "Sand": Decimal("1344"),
            "Water": Decimal("244"),
        },
        "admixtures_oz": {
            "Sikament 475": Decimal("40.60"),
            "Air": Decimal("0"),
            "SikaFume 290": Decimal("23.20"),
        },
    },
    "5000_air": {
        "psi": 5000,
        "air": True,
        "name": "5000 PSI — Air",
        "slump_in": Decimal("6"),
        "design_air_pct": Decimal("5.0"),
        "materials_lb": {
            "Cement": Decimal("600"),
            "Stone": Decimal("1745"),
            "Sand": Decimal("1281"),
            "Water": Decimal("252"),
        },
        "admixtures_oz": {
            "Sikament 475": Decimal("42.00"),
            "Air": Decimal("4.50"),
            "SikaFume 290": Decimal("24.00"),
        },
    },
}


# ============================================================
# SALES INFORMATION
# ============================================================

RETAIL_PRICES = {
    3000: Decimal("170"),
    3500: Decimal("190"),
    4000: Decimal("210"),
    4500: Decimal("219"),
    5000: Decimal("227"),
}

FOB_CONTRACT_PRICES = {
    3000: Decimal("123"),
    3500: Decimal("125"),
    4000: Decimal("128"),
    4500: Decimal("135"),
    5000: Decimal("140"),
}

ADDITIVE_PRICES = {
    "Calcium": Decimal("4.00"),
    "Fiber": Decimal("9.00"),
    "Extra cement": Decimal("15.00"),
    "Liquid FlyAsh": Decimal("13.00"),
    "Hot water": Decimal("3.50"),
}

SHORT_LOAD_FEES = {
    1: Decimal("450"),
    2: Decimal("350"),
    3: Decimal("250"),
    4: Decimal("150"),
    5: Decimal("150"),
}


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

# Authorization is intentionally kept in memory.
# After a service restart the operator must enter the password again.
authorized_users: set[int] = set()


class AuthState(StatesGroup):
    waiting_password = State()


class OrderState(StatesGroup):
    choosing_psi = State()
    choosing_air = State()
    waiting_yards = State()


MAIN_MENU = ReplyKeyboardMarkup(
    keyboard=[
        [
            KeyboardButton(text="💵 Sales Menu"),
            KeyboardButton(text="🔐 Production"),
        ],
        [KeyboardButton(text="ℹ️ Help")],
    ],
    resize_keyboard=True,
)

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
        [KeyboardButton(text="⬅️ Main Menu")],
    ],
    resize_keyboard=True,
)

OPERATOR_MENU = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text="🧮 New Calculation")],
        [KeyboardButton(text="📚 History")],
        [
            KeyboardButton(text="🔓 Logout"),
            KeyboardButton(text="⬅️ Main Menu"),
        ],
    ],
    resize_keyboard=True,
)


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
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            )
            """
        )
        # Migration for the first version of the bot.
        await connection.execute(
            "ALTER TABLE orders ADD COLUMN IF NOT EXISTS psi INTEGER"
        )
        await connection.execute(
            "ALTER TABLE orders ADD COLUMN IF NOT EXISTS air_entrained BOOLEAN"
        )
        await connection.execute(
            "ALTER TABLE orders ADD COLUMN IF NOT EXISTS mix_key TEXT"
        )
        await connection.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_orders_user_created
            ON orders (telegram_user_id, created_at DESC)
            """
        )


async def close_db() -> None:
    global pool
    if pool is not None:
        await pool.close()
        pool = None


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
) -> int:
    assert pool is not None
    recipe = RECIPES[mix_key]
    async with pool.acquire() as connection:
        order_id = await connection.fetchval(
            """
            INSERT INTO orders (
                telegram_user_id,
                telegram_username,
                yards,
                cubic_meters,
                cycles_count,
                last_cycle_m3,
                has_warning,
                psi,
                air_entrained,
                mix_key
            )
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10)
            RETURNING id
            """,
            user_id,
            username,
            yards,
            cubic_meters,
            cycles_count,
            last_cycle_m3,
            has_warning,
            recipe["psi"],
            recipe["air"],
            mix_key,
        )
    return int(order_id)


async def get_recent_orders(user_id: int, limit: int = 10):
    assert pool is not None
    async with pool.acquire() as connection:
        return await connection.fetch(
            """
            SELECT id, yards, cubic_meters, cycles_count, last_cycle_m3,
                   has_warning, psi, air_entrained, mix_key, created_at
            FROM orders
            WHERE telegram_user_id = $1
            ORDER BY created_at DESC
            LIMIT $2
            """,
            user_id,
            limit,
        )


async def get_order(user_id: int, order_id: int):
    assert pool is not None
    async with pool.acquire() as connection:
        return await connection.fetchrow(
            """
            SELECT id, yards, cubic_meters, cycles_count, last_cycle_m3,
                   has_warning, psi, air_entrained, mix_key, created_at
            FROM orders
            WHERE telegram_user_id = $1 AND id = $2
            """,
            user_id,
            order_id,
        )


# ============================================================
# CALCULATIONS
# ============================================================

VOL_6 = Decimal("0.000001")


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


def parse_yards(text: str) -> Decimal:
    cleaned = text.strip().lower().replace(",", ".")
    cleaned = cleaned.replace("yd³", "").replace("yd3", "")
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


def split_cycles(total_m3: Decimal) -> list[Decimal]:
    full_count = int(total_m3 // MAX_CYCLE_M3)
    remainder = total_m3 - (MAX_CYCLE_M3 * full_count)

    tolerance = Decimal("0.000000001")
    if remainder < tolerance:
        remainder = Decimal("0")

    cycles = [MAX_CYCLE_M3] * full_count
    if remainder > 0:
        cycles.append(remainder)
    return cycles


def recipe_materials_kg_per_m3(recipe: dict) -> dict[str, Decimal]:
    return {
        name: (weight_lb * LB_TO_KG) / YD3_TO_M3
        for name, weight_lb in recipe["materials_lb"].items()
    }


def calculate_order(yards: Decimal, mix_key: str) -> dict:
    recipe = RECIPES[mix_key]
    total_m3 = yards * YD3_TO_M3
    cycles = split_cycles(total_m3)
    last_cycle = cycles[-1] if cycles else Decimal("0")
    has_warning = (
        bool(cycles)
        and last_cycle < MIN_CYCLE_M3
        and last_cycle < MAX_CYCLE_M3
    )

    materials_total_kg = {
        name: weight_lb * LB_TO_KG * yards
        for name, weight_lb in recipe["materials_lb"].items()
    }
    admixtures_total_oz = {
        name: rate_oz * yards
        for name, rate_oz in recipe["admixtures_oz"].items()
    }
    kg_per_m3 = recipe_materials_kg_per_m3(recipe)

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
                    for name, rate in recipe["admixtures_oz"].items()
                },
            }
        )

    return {
        "mix_key": mix_key,
        "recipe": recipe,
        "yards": yards,
        "total_m3": total_m3,
        "cycles": cycles,
        "last_cycle": last_cycle,
        "has_warning": has_warning,
        "kg_per_m3": kg_per_m3,
        "materials_total_kg": materials_total_kg,
        "admixtures_total_oz": admixtures_total_oz,
        "cycle_rows": cycle_rows,
    }


def build_summary(calc: dict, order_id: int | None = None) -> str:
    recipe = calc["recipe"]
    cycles = calc["cycles"]
    full_cycles = sum(
        1
        for cycle in cycles
        if abs(cycle - MAX_CYCLE_M3) < Decimal("0.000000001")
    )
    partial_cycles = len(cycles) - full_cycles

    title = "🧱 <b>PRODUCTION CALCULATION</b>"
    if order_id is not None:
        title += f" №{order_id}"

    lines = [
        title,
        "",
        f"Mix: <b>{recipe['name']}</b>",
        f"Design air: {fmt_decimal(recipe['design_air_pct'], 1)}%",
        f"Design slump: {fmt_decimal(recipe['slump_in'], 0)} in",
        f"Order: <b>{fmt_decimal(calc['yards'], 3)} yd³</b>",
        f"Enter in ELKON: <b>{fmt_decimal(calc['total_m3'], 6)} m³</b>",
        "",
        "🔄 <b>CYCLES</b>",
        f"Full cycles of {fmt_decimal(MAX_CYCLE_M3, 3)} m³: {full_cycles}",
        f"Partial cycles: {partial_cycles}",
        f"Total cycles: <b>{len(cycles)}</b>",
        f"Last cycle: <b>{fmt_decimal(calc['last_cycle'], 6)} m³</b>",
    ]

    if calc["has_warning"]:
        lines += [
            "",
            "⚠️ <b>WARNING</b>",
            f"The last cycle is below the configured minimum of "
            f"{fmt_decimal(MIN_CYCLE_M3, 3)} m³.",
            "Do not start production without approval from the responsible person.",
        ]
    else:
        lines += ["", "✅ The last cycle is acceptable."]

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


def build_cycle_text(row: dict) -> str:
    lines = [
        f"🔹 <b>Cycle #{row['number']}</b>",
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
# HELPERS
# ============================================================


def is_authorized(user_id: int) -> bool:
    return user_id in authorized_users


async def ask_for_password(message: Message, state: FSMContext) -> None:
    if not OPERATOR_PASSWORD:
        await message.answer(
            "⚠️ The production password is not configured yet.\n"
            "Add the Render Environment variable "
            "<code>OPERATOR_PASSWORD</code> with the value <code>Aslan</code>."
        )
        return
    await state.set_state(AuthState.waiting_password)
    await message.answer(
        "🔐 Enter the password for the production menu.\n\n"
        "The bot will try to delete your password message if Telegram allows it."
    )


async def require_operator(message: Message, state: FSMContext) -> bool:
    if is_authorized(message.from_user.id):
        return True
    await ask_for_password(message, state)
    return False


async def send_calculation(
    message: Message,
    yards: Decimal,
    mix_key: str,
    *,
    save: bool,
    existing_order_id: int | None = None,
) -> None:
    calc = calculate_order(yards, mix_key)
    order_id = existing_order_id

    if save:
        order_id = await save_order(
            user_id=message.from_user.id,
            username=message.from_user.username,
            mix_key=mix_key,
            yards=calc["yards"],
            cubic_meters=q6(calc["total_m3"]),
            cycles_count=len(calc["cycles"]),
            last_cycle_m3=q6(calc["last_cycle"]),
            has_warning=calc["has_warning"],
        )

    await message.answer(build_summary(calc, order_id), reply_markup=OPERATOR_MENU)
    await message.answer("📋 <b>CALCULATION BY EACH CYCLE</b>")
    cycle_texts = [build_cycle_text(row) for row in calc["cycle_rows"]]
    for chunk in chunk_items(cycle_texts):
        await message.answer(chunk)


# ============================================================
# NAVIGATION — REGISTERED BEFORE STATE-SPECIFIC HANDLERS
# ============================================================


@router.message(Command("start"))
async def start_handler(message: Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer(
        "Hello! Choose the section you need.\n\n"
        "💵 The sales menu is available without a password.\n"
        "🔐 Production calculations are password-protected.",
        reply_markup=MAIN_MENU,
    )


@router.message(StateFilter("*"), Command("cancel"))
@router.message(StateFilter("*"), F.text == "❌ Cancel")
@router.message(StateFilter("*"), F.text == "⬅️ Main Menu")
async def cancel_or_main_handler(message: Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer("Main menu.", reply_markup=MAIN_MENU)


@router.message(F.text == "ℹ️ Help")
async def help_handler(message: Message) -> None:
    await message.answer(
        "<b>How to use</b>\n\n"
        "1. The salesperson opens the Sales Menu and checks prices and terms.\n"
        "2. The operator opens Production, enters the password, and chooses PSI and Air/Non-Air.\n"
        "3. Then enters yards: <code>7.15</code> or <code>7,15</code>.\n"
        "4. The bot returns the ELKON recipe per 1 m³, the full order, and every cycle separately.\n\n"
        "Operator commands: /history and /order NUMBER.",
        reply_markup=MAIN_MENU,
    )


# ============================================================
# SALES MENU — NO PASSWORD
# ============================================================


@router.message(F.text == "💵 Sales Menu")
async def sales_menu_handler(message: Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer(
        "💵 <b>SALES MENU</b>\n\n"
        "This is an information section. Production calculations are not available here.",
        reply_markup=SALES_MENU,
    )


@router.message(F.text == "💰 Retail Prices")
async def retail_prices_handler(message: Message) -> None:
    lines = ["💰 <b>PRICE PER 1 yd³ OF CONCRETE</b>", ""]
    for psi, price in RETAIL_PRICES.items():
        lines.append(f"{psi} PSI — <b>{fmt_money(price)}</b>")
    await message.answer("\n".join(lines), reply_markup=SALES_MENU)


@router.message(F.text == "🤝 FOB Prices")
async def fob_prices_handler(message: Message) -> None:
    lines = [
        "🤝 <b>FOB PRICES FOR CONTRACT CUSTOMERS</b>",
        "Price per 1 yd³:",
        "",
    ]
    for psi, price in FOB_CONTRACT_PRICES.items():
        lines.append(f"{psi} PSI — <b>{fmt_money(price)}</b>")
    lines += ["", "💳 All FOB contract customers pay on <b>COD terms</b>."]
    await message.answer("\n".join(lines), reply_markup=SALES_MENU)


@router.message(F.text == "➕ Additives")
async def additives_prices_handler(message: Message) -> None:
    lines = ["➕ <b>ADDITIVES — PRICE PER 1 yd³</b>", ""]
    for name, price in ADDITIVE_PRICES.items():
        lines.append(f"{name} — <b>{fmt_money(price)}</b>")
    await message.answer("\n".join(lines), reply_markup=SALES_MENU)


@router.message(F.text == "🚚 Short Load Fee")
async def short_load_handler(message: Message) -> None:
    lines = [
        "🚚 <b>SHORT LOAD FEE</b>",
        "Applies when the order is less than 10 yd³.",
        "",
    ]
    for yards, fee in SHORT_LOAD_FEES.items():
        lines.append(f"{yards} yd³ — <b>{fmt_money(fee)}</b>")
    lines += [
        "",
        "⚠️ The rate for 6–9 yd³ was not provided. "
        "The salesperson must confirm it with management.",
    ]
    await message.answer("\n".join(lines), reply_markup=SALES_MENU)


@router.message(F.text == "🕒 Hours & Terms")
async def hours_handler(message: Message) -> None:
    await message.answer(
        "🕒 <b>WORKING HOURS & TERMS</b>\n\n"
        "Monday–Friday: <b>6:00 AM – 5:00 PM</b>\n"
        "Saturday: <b>6:00 AM – 12:00 PM</b>\n\n"
        "🌙 Opening the plant after 5:00 PM for production: "
        "<b>$1,000 fee</b>, regardless of the number of yards.\n\n"
        "💳 All FOB contract customers: <b>COD terms</b>.",
        reply_markup=SALES_MENU,
    )


# ============================================================
# PASSWORD AND OPERATOR MENU
# ============================================================


@router.message(F.text == "🔐 Production")
async def production_entry_handler(message: Message, state: FSMContext) -> None:
    if is_authorized(message.from_user.id):
        await message.answer("Production menu.", reply_markup=OPERATOR_MENU)
        return
    await ask_for_password(message, state)


@router.message(AuthState.waiting_password)
async def password_handler(message: Message, state: FSMContext) -> None:
    entered = (message.text or "").strip()
    try:
        await message.delete()
    except Exception:
        pass

    if OPERATOR_PASSWORD and hmac.compare_digest(entered, OPERATOR_PASSWORD):
        authorized_users.add(message.from_user.id)
        await state.clear()
        await message.answer(
            "✅ Access granted.",
            reply_markup=OPERATOR_MENU,
        )
        return

    await message.answer(
        "❌ Wrong password. Try again or press /cancel."
    )


@router.message(F.text == "🔓 Logout")
async def logout_handler(message: Message, state: FSMContext) -> None:
    authorized_users.discard(message.from_user.id)
    await state.clear()
    await message.answer(
        "Production access closed.",
        reply_markup=MAIN_MENU,
    )


# ============================================================
# PRODUCTION CALCULATOR
# ============================================================


@router.message(F.text == "🧮 New Calculation")
async def new_order_handler(message: Message, state: FSMContext) -> None:
    if not await require_operator(message, state):
        return
    await state.set_state(OrderState.choosing_psi)
    await message.answer("Choose concrete strength:", reply_markup=psi_keyboard())


@router.callback_query(OrderState.choosing_psi, F.data.startswith("psi:"))
async def choose_psi_handler(callback: CallbackQuery, state: FSMContext) -> None:
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
    air_value = callback.data.split(":", maxsplit=1)[1]
    air = air_value == "1"
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
    if not is_authorized(message.from_user.id):
        await state.clear()
        await ask_for_password(message, state)
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
        await message.answer(
            "No recipe selected. Start a new calculation.",
            reply_markup=OPERATOR_MENU,
        )
        return

    await state.clear()
    await send_calculation(message, yards, mix_key, save=True)


# ============================================================
# HISTORY — PASSWORD PROTECTED
# ============================================================


@router.message(Command("history"))
@router.message(F.text == "📚 History")
async def history_handler(message: Message, state: FSMContext) -> None:
    if not await require_operator(message, state):
        return

    rows = await get_recent_orders(message.from_user.id)
    if not rows:
        await message.answer("History is empty so far.", reply_markup=OPERATOR_MENU)
        return

    lines = ["📚 <b>RECENT CALCULATIONS</b>", ""]
    for row in rows:
        mix_key = row["mix_key"]
        mix_name = RECIPES[mix_key]["name"] if mix_key in RECIPES else "Old recipe"
        warning = " ⚠️" if row["has_warning"] else ""
        created = row["created_at"].strftime("%d.%m.%Y %H:%M")
        lines.append(
            f"№{row['id']} — <b>{mix_name}</b> — "
            f"{fmt_decimal(Decimal(row['yards']), 3)} yd³ — "
            f"{row['cycles_count']} cycle(s){warning}\n"
            f"   {created}"
        )
    lines += ["", "Open a calculation: <code>/order NUMBER</code>"]
    await message.answer("\n".join(lines), reply_markup=OPERATOR_MENU)


@router.message(Command("order"))
async def order_handler(message: Message, state: FSMContext) -> None:
    if not await require_operator(message, state):
        return

    parts = (message.text or "").split(maxsplit=1)
    if len(parts) != 2 or not parts[1].isdigit():
        await message.answer("Example: <code>/order 15</code>")
        return

    order_id = int(parts[1])
    row = await get_order(message.from_user.id, order_id)
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

    await send_calculation(
        message,
        Decimal(row["yards"]),
        mix_key,
        save=False,
        existing_order_id=order_id,
    )


@router.message()
async def fallback_handler(message: Message) -> None:
    await message.answer(
        "Use the menu buttons. To begin, press /start.",
        reply_markup=MAIN_MENU,
    )


# ============================================================
# FASTAPI + TELEGRAM WEBHOOK
# ============================================================


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


app = FastAPI(title="ELKON Multi-Mix Concrete Bot", lifespan=lifespan)


@app.get("/")
async def health():
    return {
        "status": "ok",
        "service": "elkon-multi-mix-concrete-bot",
        "recipes": len(RECIPES),
        "webhook_path": WEBHOOK_PATH,
        "operator_password_configured": bool(OPERATOR_PASSWORD),
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
