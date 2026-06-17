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
            "Цемент": Decimal("450"),
            "Щебень": Decimal("1977"),
            "Песок": Decimal("1447"),
            "Вода": Decimal("190"),
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
            "Цемент": Decimal("470"),
            "Щебень": Decimal("1860"),
            "Песок": Decimal("1416"),
            "Вода": Decimal("198"),
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
            "Цемент": Decimal("480"),
            "Щебень": Decimal("1930"),
            "Песок": Decimal("1440"),
            "Вода": Decimal("201"),
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
            "Цемент": Decimal("500"),
            "Щебень": Decimal("1820"),
            "Песок": Decimal("1400"),
            "Вода": Decimal("210"),
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
            "Цемент": Decimal("525"),
            "Щебень": Decimal("1850"),
            "Песок": Decimal("1433"),
            "Вода": Decimal("220"),
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
            "Цемент": Decimal("545"),
            "Щебень": Decimal("1800"),
            "Песок": Decimal("1332"),
            "Вода": Decimal("229"),
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
            "Цемент": Decimal("550"),
            "Щебень": Decimal("1830"),
            "Песок": Decimal("1403"),
            "Вода": Decimal("231"),
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
            "Цемент": Decimal("564"),
            "Щебень": Decimal("1780"),
            "Песок": Decimal("1315"),
            "Вода": Decimal("237"),
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
            "Цемент": Decimal("580"),
            "Щебень": Decimal("1830"),
            "Песок": Decimal("1344"),
            "Вода": Decimal("244"),
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
            "Цемент": Decimal("600"),
            "Щебень": Decimal("1745"),
            "Песок": Decimal("1281"),
            "Вода": Decimal("252"),
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
            KeyboardButton(text="💵 Меню продажника"),
            KeyboardButton(text="🔐 Производство"),
        ],
        [KeyboardButton(text="ℹ️ Помощь")],
    ],
    resize_keyboard=True,
)

SALES_MENU = ReplyKeyboardMarkup(
    keyboard=[
        [
            KeyboardButton(text="💰 Обычные цены"),
            KeyboardButton(text="🤝 FOB цены"),
        ],
        [
            KeyboardButton(text="➕ Добавки"),
            KeyboardButton(text="🚚 Short Load Fee"),
        ],
        [KeyboardButton(text="🕒 Часы и условия")],
        [KeyboardButton(text="⬅️ Главное меню")],
    ],
    resize_keyboard=True,
)

OPERATOR_MENU = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text="🧮 Новый расчёт")],
        [KeyboardButton(text="📚 История")],
        [
            KeyboardButton(text="🔓 Выйти"),
            KeyboardButton(text="⬅️ Главное меню"),
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
    whole_with_spaces = f"{int(whole):,}".replace(",", " ")
    if places == 0:
        return whole_with_spaces
    return whole_with_spaces + "," + fraction


def fmt_money(value: Decimal) -> str:
    if value == value.to_integral_value():
        return f"${fmt_decimal(value, 0)}"
    return f"${fmt_decimal(value, 2)}"


def parse_yards(text: str) -> Decimal:
    cleaned = text.strip().lower().replace(",", ".")
    cleaned = cleaned.replace("yd³", "").replace("yd3", "")
    cleaned = cleaned.replace("yards", "").replace("yard", "")
    cleaned = re.sub(r"\s+", "", cleaned)

    try:
        value = Decimal(cleaned)
    except InvalidOperation as exc:
        raise ValueError("Введите число, например 7.15 или 7,15.") from exc

    if value <= 0:
        raise ValueError("Объём должен быть больше нуля.")
    if value > MAX_ORDER_YD3:
        raise ValueError(
            f"Максимум одного расчёта: {fmt_decimal(MAX_ORDER_YD3, 2)} yd³."
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

    title = "🧱 <b>ПРОИЗВОДСТВЕННЫЙ РАСЧЁТ</b>"
    if order_id is not None:
        title += f" №{order_id}"

    lines = [
        title,
        "",
        f"Смесь: <b>{recipe['name']}</b>",
        f"Design air: {fmt_decimal(recipe['design_air_pct'], 1)}%",
        f"Design slump: {fmt_decimal(recipe['slump_in'], 0)} in",
        f"Заказ: <b>{fmt_decimal(calc['yards'], 3)} yd³</b>",
        f"Ввести в ELKON: <b>{fmt_decimal(calc['total_m3'], 6)} м³</b>",
        "",
        "🔄 <b>ЦИКЛЫ</b>",
        f"Полных циклов по {fmt_decimal(MAX_CYCLE_M3, 3)} м³: {full_cycles}",
        f"Неполных циклов: {partial_cycles}",
        f"Всего циклов: <b>{len(cycles)}</b>",
        f"Последний цикл: <b>{fmt_decimal(calc['last_cycle'], 6)} м³</b>",
    ]

    if calc["has_warning"]:
        lines += [
            "",
            "⚠️ <b>ВНИМАНИЕ</b>",
            f"Последний цикл меньше установленного минимума "
            f"{fmt_decimal(MIN_CYCLE_M3, 3)} м³.",
            "Не запускайте производство без подтверждения ответственного лица.",
        ]
    else:
        lines += ["", "✅ Последний цикл допустим."]

    lines += ["", "⚙️ <b>РЕЦЕПТ ДЛЯ ELKON НА 1 м³</b>"]
    for name, value in calc["kg_per_m3"].items():
        if name == "Вода":
            lines.append(
                f"{name}: <b>{fmt_decimal(value, 2)} кг/м³ "
                f"(≈ {fmt_decimal(value, 2)} л/м³)</b>"
            )
        else:
            lines.append(f"{name}: <b>{fmt_decimal(value, 2)} кг/м³</b>")

    lines += ["", "⚖️ <b>МАТЕРИАЛЫ НА ВЕСЬ ЗАКАЗ</b>"]
    for name, value in calc["materials_total_kg"].items():
        if name == "Вода":
            lines.append(
                f"{name}: <b>{fmt_decimal(value, 2)} кг "
                f"(≈ {fmt_decimal(value, 2)} л)</b>"
            )
        else:
            lines.append(f"{name}: <b>{fmt_decimal(value, 2)} кг</b>")
    lines.append(
        f"Общий вес: <b>{fmt_decimal(sum(calc['materials_total_kg'].values()), 2)} кг</b>"
    )

    lines += ["", "🧪 <b>ДОБАВКИ НА ВЕСЬ ЗАКАЗ</b>"]
    for name, value in calc["admixtures_total_oz"].items():
        shown = "—" if value == 0 else f"{fmt_decimal(value, 2)} oz"
        lines.append(f"{name}: <b>{shown}</b>")

    lines += [
        "",
        "ℹ️ Добавки показаны в исходной единице сертифицированной рецептуры — oz/yd³. "
        "Не переводите их в литры без подтверждения типа ounce и плотности продукта.",
    ]
    return "\n".join(lines)


def build_cycle_text(row: dict) -> str:
    lines = [
        f"🔹 <b>Цикл №{row['number']}</b>",
        f"Объём: <b>{fmt_decimal(row['m3'], 6)} м³</b> "
        f"({fmt_decimal(row['yd3'], 6)} yd³)",
        "",
        "<b>Материалы:</b>",
    ]
    for name, value in row["materials_kg"].items():
        if name == "Вода":
            lines.append(
                f"{name}: {fmt_decimal(value, 2)} кг "
                f"(≈ {fmt_decimal(value, 2)} л)"
            )
        else:
            lines.append(f"{name}: {fmt_decimal(value, 2)} кг")
    lines.append(
        f"Вес цикла: <b>{fmt_decimal(sum(row['materials_kg'].values()), 2)} кг</b>"
    )

    lines += ["", "<b>Добавки:</b>"]
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
            "⚠️ Пароль производства ещё не настроен.\n"
            "Добавьте в Render Environment переменную "
            "<code>OPERATOR_PASSWORD</code> со значением <code>Aslan</code>."
        )
        return
    await state.set_state(AuthState.waiting_password)
    await message.answer(
        "🔐 Введите пароль для производственного меню.\n\n"
        "Сообщение с паролем будет удалено ботом, если Telegram разрешит удаление."
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
    await message.answer("📋 <b>РАСЧЁТ ОТДЕЛЬНО ПО КАЖДОМУ ЦИКЛУ</b>")
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
        "Здравствуйте! Выберите нужный раздел.\n\n"
        "💵 Меню продажника доступно без пароля.\n"
        "🔐 Производственные расчёты защищены паролем.",
        reply_markup=MAIN_MENU,
    )


@router.message(StateFilter("*"), Command("cancel"))
@router.message(StateFilter("*"), F.text == "❌ Отмена")
@router.message(StateFilter("*"), F.text == "⬅️ Главное меню")
async def cancel_or_main_handler(message: Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer("Главное меню.", reply_markup=MAIN_MENU)


@router.message(F.text == "ℹ️ Помощь")
async def help_handler(message: Message) -> None:
    await message.answer(
        "<b>Как пользоваться</b>\n\n"
        "1. Продажник открывает «Меню продажника» и смотрит цены и условия.\n"
        "2. Оператор открывает «Производство», вводит пароль, выбирает PSI и Air/Non-Air.\n"
        "3. Затем вводит ярды: <code>7.15</code> или <code>7,15</code>.\n"
        "4. Бот выдаёт рецепт на 1 м³ для ELKON, весь заказ и каждый цикл отдельно.\n\n"
        "Команды оператора: /history и /order НОМЕР.",
        reply_markup=MAIN_MENU,
    )


# ============================================================
# SALES MENU — NO PASSWORD
# ============================================================


@router.message(F.text == "💵 Меню продажника")
async def sales_menu_handler(message: Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer(
        "💵 <b>МЕНЮ ПРОДАЖНИКА</b>\n\n"
        "Это информационный раздел. Производственные расчёты здесь недоступны.",
        reply_markup=SALES_MENU,
    )


@router.message(F.text == "💰 Обычные цены")
async def retail_prices_handler(message: Message) -> None:
    lines = ["💰 <b>ЦЕНА ЗА 1 yd³ БЕТОНА</b>", ""]
    for psi, price in RETAIL_PRICES.items():
        lines.append(f"{psi} PSI — <b>{fmt_money(price)}</b>")
    await message.answer("\n".join(lines), reply_markup=SALES_MENU)


@router.message(F.text == "🤝 FOB цены")
async def fob_prices_handler(message: Message) -> None:
    lines = [
        "🤝 <b>FOB ЦЕНЫ ДЛЯ КОНТРАКТНИКОВ</b>",
        "Цена за 1 yd³:",
        "",
    ]
    for psi, price in FOB_CONTRACT_PRICES.items():
        lines.append(f"{psi} PSI — <b>{fmt_money(price)}</b>")
    lines += ["", "💳 Все FOB-контрактники работают на условиях <b>COD</b>."]
    await message.answer("\n".join(lines), reply_markup=SALES_MENU)


@router.message(F.text == "➕ Добавки")
async def additives_prices_handler(message: Message) -> None:
    lines = ["➕ <b>ДОБАВКИ — ЦЕНА ЗА 1 yd³</b>", ""]
    for name, price in ADDITIVE_PRICES.items():
        lines.append(f"{name} — <b>{fmt_money(price)}</b>")
    await message.answer("\n".join(lines), reply_markup=SALES_MENU)


@router.message(F.text == "🚚 Short Load Fee")
async def short_load_handler(message: Message) -> None:
    lines = [
        "🚚 <b>SHORT LOAD FEE</b>",
        "Применяется при заказе менее 10 yd³.",
        "",
    ]
    for yards, fee in SHORT_LOAD_FEES.items():
        lines.append(f"{yards} yd³ — <b>{fmt_money(fee)}</b>")
    lines += [
        "",
        "⚠️ Тариф для 6–9 yd³ в предоставленных данных не указан. "
        "Продажник должен уточнить его у руководителя.",
    ]
    await message.answer("\n".join(lines), reply_markup=SALES_MENU)


@router.message(F.text == "🕒 Часы и условия")
async def hours_handler(message: Message) -> None:
    await message.answer(
        "🕒 <b>РАБОЧЕЕ ВРЕМЯ И УСЛОВИЯ</b>\n\n"
        "Понедельник–пятница: <b>6:00 AM – 5:00 PM</b>\n"
        "Суббота: <b>6:00 AM – 12:00 PM</b>\n\n"
        "🌙 После 5:00 PM открытие завода для производства: "
        "<b>$1 000 fee</b>. Количество ярдов значения не имеет.\n\n"
        "💳 Все FOB-контрактники: <b>COD terms</b>.",
        reply_markup=SALES_MENU,
    )


# ============================================================
# PASSWORD AND OPERATOR MENU
# ============================================================


@router.message(F.text == "🔐 Производство")
async def production_entry_handler(message: Message, state: FSMContext) -> None:
    if is_authorized(message.from_user.id):
        await message.answer("Производственное меню.", reply_markup=OPERATOR_MENU)
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
            "✅ Доступ разрешён.",
            reply_markup=OPERATOR_MENU,
        )
        return

    await message.answer(
        "❌ Неверный пароль. Попробуйте ещё раз или нажмите /cancel."
    )


@router.message(F.text == "🔓 Выйти")
async def logout_handler(message: Message, state: FSMContext) -> None:
    authorized_users.discard(message.from_user.id)
    await state.clear()
    await message.answer(
        "Производственный доступ закрыт.",
        reply_markup=MAIN_MENU,
    )


# ============================================================
# PRODUCTION CALCULATOR
# ============================================================


@router.message(F.text == "🧮 Новый расчёт")
async def new_order_handler(message: Message, state: FSMContext) -> None:
    if not await require_operator(message, state):
        return
    await state.set_state(OrderState.choosing_psi)
    await message.answer("Выберите прочность бетона:", reply_markup=psi_keyboard())


@router.callback_query(OrderState.choosing_psi, F.data.startswith("psi:"))
async def choose_psi_handler(callback: CallbackQuery, state: FSMContext) -> None:
    psi = int(callback.data.split(":", maxsplit=1)[1])
    if psi not in RETAIL_PRICES:
        await callback.answer("Неизвестный PSI", show_alert=True)
        return

    await state.update_data(psi=psi)
    await state.set_state(OrderState.choosing_air)
    await callback.answer()
    if callback.message:
        await callback.message.edit_text(
            f"Выбрано: <b>{psi} PSI</b>\nТеперь выберите тип смеси:",
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
        await callback.answer("Рецептура не найдена", show_alert=True)
        return

    await state.update_data(mix_key=mix_key)
    await state.set_state(OrderState.waiting_yards)
    await callback.answer()
    if callback.message:
        await callback.message.edit_text(
            f"Смесь: <b>{RECIPES[mix_key]['name']}</b>\n\n"
            "Введите объём заказа в кубических ярдах.\n"
            "Можно через точку или запятую: <code>7.15</code> или <code>7,15</code>."
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
            "Рецептура не выбрана. Начните новый расчёт.",
            reply_markup=OPERATOR_MENU,
        )
        return

    await state.clear()
    await send_calculation(message, yards, mix_key, save=True)


# ============================================================
# HISTORY — PASSWORD PROTECTED
# ============================================================


@router.message(Command("history"))
@router.message(F.text == "📚 История")
async def history_handler(message: Message, state: FSMContext) -> None:
    if not await require_operator(message, state):
        return

    rows = await get_recent_orders(message.from_user.id)
    if not rows:
        await message.answer("История пока пустая.", reply_markup=OPERATOR_MENU)
        return

    lines = ["📚 <b>ПОСЛЕДНИЕ РАСЧЁТЫ</b>", ""]
    for row in rows:
        mix_key = row["mix_key"]
        mix_name = RECIPES[mix_key]["name"] if mix_key in RECIPES else "Старая рецептура"
        warning = " ⚠️" if row["has_warning"] else ""
        created = row["created_at"].strftime("%d.%m.%Y %H:%M")
        lines.append(
            f"№{row['id']} — <b>{mix_name}</b> — "
            f"{fmt_decimal(Decimal(row['yards']), 3)} yd³ — "
            f"{row['cycles_count']} цикл(ов){warning}\n"
            f"   {created}"
        )
    lines += ["", "Открыть расчёт: <code>/order НОМЕР</code>"]
    await message.answer("\n".join(lines), reply_markup=OPERATOR_MENU)


@router.message(Command("order"))
async def order_handler(message: Message, state: FSMContext) -> None:
    if not await require_operator(message, state):
        return

    parts = (message.text or "").split(maxsplit=1)
    if len(parts) != 2 or not parts[1].isdigit():
        await message.answer("Пример: <code>/order 15</code>")
        return

    order_id = int(parts[1])
    row = await get_order(message.from_user.id, order_id)
    if row is None:
        await message.answer("Такой заказ не найден.")
        return

    mix_key = row["mix_key"]
    if mix_key not in RECIPES:
        await message.answer(
            "Это заказ из старой версии бота без сохранённого типа рецептуры. "
            "Повторный расчёт невозможен."
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
        "Используйте кнопки меню. Для начала нажмите /start.",
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
