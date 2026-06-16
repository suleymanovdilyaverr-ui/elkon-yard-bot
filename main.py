import os
import re
from contextlib import asynccontextmanager
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from typing import Iterable

import asyncpg
from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import Command
from aiogram.types import (
    KeyboardButton,
    Message,
    ReplyKeyboardMarkup,
    Update,
)
from fastapi import FastAPI, Header, HTTPException, Request


# -----------------------------
# Настройки
# -----------------------------

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
DATABASE_URL = os.getenv("DATABASE_URL", "").strip()
WEBHOOK_BASE_URL = (
    os.getenv("WEBHOOK_BASE_URL", "").strip()
    or os.getenv("RENDER_EXTERNAL_URL", "").strip()
)
WEBHOOK_PATH = os.getenv("WEBHOOK_PATH", "/telegram-webhook").strip()
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "").strip()

YD3_TO_M3 = Decimal("0.764554858")
MAX_CYCLE_M3 = Decimal(os.getenv("MAX_CYCLE_M3", "0.5"))
MIN_CYCLE_M3 = Decimal(os.getenv("MIN_CYCLE_M3", "0.20"))

CEMENT_KG_PER_YD3 = Decimal(os.getenv("CEMENT_KG_PER_YD3", "238"))
GRAVEL_KG_PER_YD3 = Decimal(os.getenv("GRAVEL_KG_PER_YD3", "839"))
SAND_KG_PER_YD3 = Decimal(os.getenv("SAND_KG_PER_YD3", "650"))
WATER_KG_PER_YD3 = Decimal(os.getenv("WATER_KG_PER_YD3", "100"))

MAX_ORDER_YD3 = Decimal(os.getenv("MAX_ORDER_YD3", "100"))

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN is not set")
if not DATABASE_URL:
    raise RuntimeError("DATABASE_URL is not set")
if not WEBHOOK_BASE_URL:
    raise RuntimeError("WEBHOOK_BASE_URL or RENDER_EXTERNAL_URL is not set")
if not WEBHOOK_SECRET:
    raise RuntimeError("WEBHOOK_SECRET is not set")

if not WEBHOOK_PATH.startswith("/"):
    WEBHOOK_PATH = "/" + WEBHOOK_PATH

WEBHOOK_URL = WEBHOOK_BASE_URL.rstrip("/") + WEBHOOK_PATH


# -----------------------------
# Telegram
# -----------------------------

bot = Bot(
    token=BOT_TOKEN,
    default=DefaultBotProperties(parse_mode=ParseMode.HTML),
)
dp = Dispatcher()
router = Router()
dp.include_router(router)

main_keyboard = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text="🧮 Новый расчёт")],
        [KeyboardButton(text="📚 История"), KeyboardButton(text="ℹ️ Помощь")],
    ],
    resize_keyboard=True,
)


# -----------------------------
# База данных
# -----------------------------

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
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            )
            """
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
    yards: Decimal,
    cubic_meters: Decimal,
    cycles_count: int,
    last_cycle_m3: Decimal,
    has_warning: bool,
) -> int:
    assert pool is not None
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
                has_warning
            )
            VALUES ($1, $2, $3, $4, $5, $6, $7)
            RETURNING id
            """,
            user_id,
            username,
            yards,
            cubic_meters,
            cycles_count,
            last_cycle_m3,
            has_warning,
        )
    return int(order_id)


async def get_recent_orders(user_id: int, limit: int = 10):
    assert pool is not None
    async with pool.acquire() as connection:
        return await connection.fetch(
            """
            SELECT id, yards, cubic_meters, cycles_count, last_cycle_m3,
                   has_warning, created_at
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
                   has_warning, created_at
            FROM orders
            WHERE telegram_user_id = $1 AND id = $2
            """,
            user_id,
            order_id,
        )


# -----------------------------
# Расчёты
# -----------------------------

QTY_2 = Decimal("0.01")
VOL_6 = Decimal("0.000001")


def q2(value: Decimal) -> Decimal:
    return value.quantize(QTY_2, rounding=ROUND_HALF_UP)


def q6(value: Decimal) -> Decimal:
    return value.quantize(VOL_6, rounding=ROUND_HALF_UP)


def fmt_decimal(value: Decimal, places: int = 2) -> str:
    quant = Decimal("1").scaleb(-places)
    text = f"{value.quantize(quant, rounding=ROUND_HALF_UP):f}"
    whole, dot, fraction = text.partition(".")
    whole_with_spaces = f"{int(whole):,}".replace(",", " ")
    if places == 0:
        return whole_with_spaces
    return whole_with_spaces + "," + fraction


def parse_yards(text: str) -> Decimal:
    cleaned = text.strip().replace(",", ".")
    cleaned = re.sub(r"\s+", "", cleaned)

    try:
        value = Decimal(cleaned)
    except InvalidOperation as exc:
        raise ValueError("Введите только число, например 7.15 или 7,15.") from exc

    if value <= 0:
        raise ValueError("Объём должен быть больше нуля.")
    if value > MAX_ORDER_YD3:
        raise ValueError(
            f"Слишком большой объём. Максимум для одного расчёта: "
            f"{fmt_decimal(MAX_ORDER_YD3, 2)} yd³."
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


def recipe_per_m3() -> dict[str, Decimal]:
    return {
        "Цемент": CEMENT_KG_PER_YD3 / YD3_TO_M3,
        "Щебень": GRAVEL_KG_PER_YD3 / YD3_TO_M3,
        "Песок": SAND_KG_PER_YD3 / YD3_TO_M3,
        "Вода": WATER_KG_PER_YD3 / YD3_TO_M3,
    }


def calculate_order(yards: Decimal) -> dict:
    total_m3 = yards * YD3_TO_M3
    cycles = split_cycles(total_m3)
    last_cycle = cycles[-1] if cycles else Decimal("0")
    has_warning = (
        len(cycles) > 0
        and last_cycle < MIN_CYCLE_M3
        and last_cycle < MAX_CYCLE_M3
    )

    totals = {
        "Цемент": yards * CEMENT_KG_PER_YD3,
        "Щебень": yards * GRAVEL_KG_PER_YD3,
        "Песок": yards * SAND_KG_PER_YD3,
        "Вода": yards * WATER_KG_PER_YD3,
    }
    per_m3 = recipe_per_m3()

    cycle_rows = []
    for number, cycle_m3 in enumerate(cycles, start=1):
        cycle_rows.append(
            {
                "number": number,
                "m3": cycle_m3,
                "materials": {
                    name: rate * cycle_m3 for name, rate in per_m3.items()
                },
            }
        )

    return {
        "yards": yards,
        "total_m3": total_m3,
        "cycles": cycles,
        "last_cycle": last_cycle,
        "has_warning": has_warning,
        "totals": totals,
        "cycle_rows": cycle_rows,
    }


def build_summary(calc: dict, order_id: int | None = None) -> str:
    yards = calc["yards"]
    total_m3 = calc["total_m3"]
    cycles = calc["cycles"]
    last_cycle = calc["last_cycle"]
    totals = calc["totals"]

    full_cycles = sum(1 for c in cycles if abs(c - MAX_CYCLE_M3) < Decimal("0.000000001"))
    partial_cycles = len(cycles) - full_cycles

    title = "🧱 <b>РАСЧЁТ ЗАКАЗА</b>"
    if order_id is not None:
        title += f" №{order_id}"

    lines = [
        title,
        "",
        f"Заказ клиента: <b>{fmt_decimal(yards, 3)} yd³</b>",
        f"Объём для ELKON: <b>{fmt_decimal(total_m3, 6)} м³</b>",
        "",
        "🔄 <b>ЦИКЛЫ</b>",
        f"Максимальный цикл: {fmt_decimal(MAX_CYCLE_M3, 3)} м³",
        f"Полных циклов: {full_cycles}",
        f"Неполных циклов: {partial_cycles}",
        f"Всего циклов: <b>{len(cycles)}</b>",
        f"Последний цикл: <b>{fmt_decimal(last_cycle, 6)} м³</b>",
    ]

    if calc["has_warning"]:
        lines += [
            "",
            "⚠️ <b>ВНИМАНИЕ</b>",
            f"Последний цикл меньше установленного минимума "
            f"{fmt_decimal(MIN_CYCLE_M3, 3)} м³.",
            "Перед запуском необходимо подтверждение оператора.",
        ]
    else:
        lines += ["", "✅ Последний цикл допустим по установленному минимуму."]

    lines += [
        "",
        "⚖️ <b>МАТЕРИАЛЫ НА ВЕСЬ ЗАКАЗ</b>",
        f"Цемент: <b>{fmt_decimal(totals['Цемент'], 2)} кг</b>",
        f"Щебень: <b>{fmt_decimal(totals['Щебень'], 2)} кг</b>",
        f"Песок: <b>{fmt_decimal(totals['Песок'], 2)} кг</b>",
        f"Вода: <b>{fmt_decimal(totals['Вода'], 2)} кг</b>",
        f"Общий вес: <b>{fmt_decimal(sum(totals.values()), 2)} кг</b>",
    ]

    return "\n".join(lines)


def build_cycle_text(row: dict) -> str:
    materials = row["materials"]
    return "\n".join(
        [
            f"🔹 <b>Цикл №{row['number']}</b> — "
            f"<b>{fmt_decimal(row['m3'], 6)} м³</b>",
            f"Цемент: {fmt_decimal(materials['Цемент'], 2)} кг",
            f"Щебень: {fmt_decimal(materials['Щебень'], 2)} кг",
            f"Песок: {fmt_decimal(materials['Песок'], 2)} кг",
            f"Вода: {fmt_decimal(materials['Вода'], 2)} кг",
            f"Вес цикла: {fmt_decimal(sum(materials.values()), 2)} кг",
        ]
    )


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


async def send_calculation(
    message: Message,
    yards: Decimal,
    *,
    save: bool,
    existing_order_id: int | None = None,
) -> None:
    calc = calculate_order(yards)

    order_id = existing_order_id
    if save:
        order_id = await save_order(
            user_id=message.from_user.id,
            username=message.from_user.username,
            yards=calc["yards"],
            cubic_meters=q6(calc["total_m3"]),
            cycles_count=len(calc["cycles"]),
            last_cycle_m3=q6(calc["last_cycle"]),
            has_warning=calc["has_warning"],
        )

    await message.answer(build_summary(calc, order_id), reply_markup=main_keyboard)

    await message.answer("📋 <b>РАСЧЁТ ОТДЕЛЬНО ПО КАЖДОМУ ЦИКЛУ</b>")
    cycle_texts = [build_cycle_text(row) for row in calc["cycle_rows"]]
    for chunk in chunk_items(cycle_texts):
        await message.answer(chunk)


# -----------------------------
# Обработчики
# -----------------------------

@router.message(Command("start"))
async def start_handler(message: Message) -> None:
    await message.answer(
        "Здравствуйте!\n\n"
        "Отправьте объём заказа в кубических ярдах.\n"
        "Можно писать через точку или запятую:\n"
        "<code>7.15</code> или <code>7,15</code>\n\n"
        "Я рассчитаю объём для ELKON, материалы, количество циклов "
        "и каждый цикл отдельно.",
        reply_markup=main_keyboard,
    )


@router.message(Command("help"))
@router.message(F.text == "ℹ️ Помощь")
@router.message(F.text == "🧮 Новый расчёт")
async def help_handler(message: Message) -> None:
    await message.answer(
        "Введите количество кубических ярдов одним сообщением.\n\n"
        "Примеры:\n"
        "<code>1</code>\n"
        "<code>6.15</code>\n"
        "<code>7,15</code>\n\n"
        "Команды:\n"
        "/history — последние расчёты\n"
        "/order 15 — открыть заказ №15",
        reply_markup=main_keyboard,
    )


@router.message(Command("history"))
@router.message(F.text == "📚 История")
async def history_handler(message: Message) -> None:
    rows = await get_recent_orders(message.from_user.id)

    if not rows:
        await message.answer("История пока пустая.", reply_markup=main_keyboard)
        return

    lines = ["📚 <b>ПОСЛЕДНИЕ ЗАКАЗЫ</b>", ""]
    for row in rows:
        warning = " ⚠️" if row["has_warning"] else ""
        created = row["created_at"].strftime("%d.%m.%Y %H:%M")
        lines.append(
            f"№{row['id']} — <b>{fmt_decimal(Decimal(row['yards']), 3)} yd³</b> "
            f"— {row['cycles_count']} цикл(ов){warning}\n"
            f"   {created}"
        )

    lines += ["", "Чтобы открыть расчёт: <code>/order НОМЕР</code>"]
    await message.answer("\n".join(lines), reply_markup=main_keyboard)


@router.message(Command("order"))
async def order_handler(message: Message) -> None:
    parts = (message.text or "").split(maxsplit=1)
    if len(parts) != 2 or not parts[1].isdigit():
        await message.answer("Пример команды: <code>/order 15</code>")
        return

    order_id = int(parts[1])
    row = await get_order(message.from_user.id, order_id)
    if row is None:
        await message.answer("Такой заказ не найден в вашей истории.")
        return

    await send_calculation(
        message,
        Decimal(row["yards"]),
        save=False,
        existing_order_id=order_id,
    )


@router.message(F.text)
async def number_handler(message: Message) -> None:
    try:
        yards = parse_yards(message.text or "")
    except ValueError as error:
        await message.answer(str(error), reply_markup=main_keyboard)
        return

    await send_calculation(message, yards, save=True)


# -----------------------------
# FastAPI + Telegram webhook
# -----------------------------

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


app = FastAPI(
    title="ELKON Yard Calculator Bot",
    lifespan=lifespan,
)


@app.get("/")
async def health():
    return {
        "status": "ok",
        "service": "elkon-yard-calculator-bot",
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
