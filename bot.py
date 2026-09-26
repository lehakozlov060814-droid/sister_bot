import asyncio
import logging
import aiosqlite

from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramAPIError
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

# =====================================================================
# КОНФИГУРАЦИЯ
# =====================================================================

BOT_TOKEN: str = "8817032202:AAF9KUagA28yDlxPzukHjfQmp1yFY03CYH8"
SISTER_SECRET: str = "sister_gift_2026_secret"
DB_PATH: str = "bot.db"

# Привязанные Telegram ID
OWNER_ID: int = 5240174256       # Главный админ (уведомления + кнопки действий + команды)
OBSERVER_ID: int = 5167454813    # Наблюдатель (только уведомления о запросах)

TASK_CATALOG: dict[str, str] = {
    "cat": "🐈 Принести кысу",
    "water": "💧 Принести воды",
    "dumplings": "🥟 Помешать пельмени / вареники",
    "laundry": "🧺 Развешать стиралку",
}

STATUS_TEXTS: dict[str, str] = {
    "pending": "⏳ В ожидании",
    "in_progress": "🏃 Уже в пути",
    "completed": "✅ Выполнено",
    "rejected": "❌ Отклонено",
}

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - [%(levelname)s] - %(name)s - %(message)s",
)
logger = logging.getLogger(__name__)

# =====================================================================
# БАЗА ДАННЫХ (aiosqlite)
# =====================================================================

async def init_db() -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            )
            """
        )
        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS requests (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                task_code TEXT NOT NULL,
                task_name TEXT NOT NULL,
                status TEXT NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        await db.commit()


async def get_setting(key: str) -> str | None:
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT value FROM settings WHERE key = ?", (key,)) as cursor:
            row = await cursor.fetchone()
            return row[0] if row else None


async def set_setting(key: str, value: str) -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """
            INSERT INTO settings (key, value)
            VALUES (?, ?)
            ON CONFLICT(key) DO UPDATE SET value = excluded.value
            """,
            (key, str(value)),
        )
        await db.commit()


async def get_recipient_id() -> int | None:
    val = await get_setting("recipient_chat_id")
    return int(val) if val else None


async def set_recipient_id(chat_id: int) -> None:
    await set_setting("recipient_chat_id", str(chat_id))


async def is_sister_token_used() -> bool:
    val = await get_setting("sister_token_used")
    return val == "1"


async def mark_sister_token_used() -> None:
    await set_setting("sister_token_used", "1")


async def create_request(task_code: str, task_name: str) -> int:
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            """
            INSERT INTO requests (task_code, task_name, status)
            VALUES (?, ?, 'pending')
            """,
            (task_code, task_name),
        )
        await db.commit()
        return cursor.lastrowid or 0


async def get_request(req_id: int) -> dict | None:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            """
            SELECT id, task_code, task_name, status, created_at, updated_at
            FROM requests
            WHERE id = ?
            """,
            (req_id,),
        ) as cursor:
            row = await cursor.fetchone()
            return dict(row) if row else None


async def update_request_status(req_id: int, status: str) -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """
            UPDATE requests
            SET status = ?, updated_at = CURRENT_TIMESTAMP
            WHERE id = ?
            """,
            (status, req_id),
        )
        await db.commit()


async def get_recent_requests(limit: int = 10) -> list[dict]:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            """
            SELECT id, task_code, task_name, status, created_at
            FROM requests
            ORDER BY id DESC
            LIMIT ?
            """,
            (limit,),
        ) as cursor:
            rows = await cursor.fetchall()
            return [dict(r) for r in rows]


async def get_task_stats() -> dict[str, int]:
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            """
            SELECT task_code, COUNT(*)
            FROM requests
            WHERE status = 'completed'
            GROUP BY task_code
            """
        ) as cursor:
            rows = await cursor.fetchall()
            return {code: count for code, count in rows}

# =====================================================================
# КЛАВИАТУРЫ И СКЛОНЕНИЯ
# =====================================================================

def get_sister_panel_keyboard() -> InlineKeyboardMarkup:
    buttons = [
        [InlineKeyboardButton(text=title, callback_data=f"sister_task:{code}")]
        for code, title in TASK_CATALOG.items()
    ]
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def get_owner_action_keyboard(request_id: int, in_progress: bool = False) -> InlineKeyboardMarkup:
    buttons = []
    if not in_progress:
        buttons.append([
            InlineKeyboardButton(text="⏳ Уже иду", callback_data=f"owner_act:going:{request_id}")
        ])
    buttons.append([
        InlineKeyboardButton(text="✅ Выполнено", callback_data=f"owner_act:done:{request_id}"),
        InlineKeyboardButton(text="❌ Отклонить", callback_data=f"owner_act:reject:{request_id}"),
    ])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def declension_times(n: int) -> str:
    n_mod100 = n % 100
    n_mod10 = n % 10
    if 11 <= n_mod100 <= 19:
        return f"{n} раз"
    if n_mod10 == 1:
        return f"{n} раз"
    if 2 <= n_mod10 <= 4:
        return f"{n} раза"
    return f"{n} раз"

# =====================================================================
# МАРШРУТИЗАЦИЯ (ХЕНДЛЕРЫ)
# =====================================================================

router = Router()


@router.message(CommandStart())
async def handle_start(message: Message, command: CommandObject, bot: Bot) -> None:
    token = command.args.strip() if command.args else None
    user_id = message.from_user.id
    current_recipient_id = await get_recipient_id()

    # 1. Активация сестрой по секретной ссылке
    if token == SISTER_SECRET:
        if user_id in (OWNER_ID, OBSERVER_ID):
            await message.answer("❌ Ты в списке администраторов/наблюдателей и не можешь активировать пульт сестры.")
            return

        if await is_sister_token_used():
            if current_recipient_id == user_id:
                await message.answer(
                    "Ты уже активировала свой пульт. Нажми /panel, чтобы открыть его снова."
                )
            else:
                await message.answer("⛔ Ссылка активации уже была использована.")
            return

        await set_recipient_id(user_id)
        await mark_sister_token_used()

        await message.answer(
            "🎁 <b>Подарок активирован.</b>\n\n"
            "Теперь у тебя появился пульт управления мной.\n"
            "Используй с умом. Или не используй.",
            reply_markup=get_sister_panel_keyboard(),
        )

        try:
            await bot.send_message(
                OWNER_ID,
                "🎉 <b>Сестра активировала пульт управления!</b>\n"
                "Система готова. Ожидай первые поручения.",
            )
        except TelegramAPIError as e:
            logger.error(f"Не удалось отправить уведомление владельцу: {e}")

        try:
            await bot.send_message(
                OBSERVER_ID,
                "🎉 <b>Сестра активировала пульт управления!</b>",
            )
        except TelegramAPIError:
            pass
        return

    # 2. Обычный запуск без токена
    if user_id == current_recipient_id:
        await message.answer(
            "🎛 Пульт управления готов к работе.",
            reply_markup=get_sister_panel_keyboard(),
        )
        return

    if user_id == OWNER_ID:
        await message.answer(
            "👑 <b>Панель исполнителя активна.</b>\n\n"
            "Сюда приходят все запросы с кнопками действий.\n\n"
            "Команды:\n"
            "• /requests — последние запросы\n"
            "• /stats — статистика выполнения"
        )
        return

    if user_id == OBSERVER_ID:
        await message.answer(
            "👀 <b>Режим наблюдателя активен.</b>\n\n"
            "Сюда будут дублироваться входящие запросы от сестры."
        )
        return

    await message.answer("⛔ Доступ ограничен. Требуется персональная ссылка активации.")


@router.message(Command("panel"))
async def cmd_panel(message: Message) -> None:
    recipient_id = await get_recipient_id()
    if message.from_user.id != recipient_id:
        return

    await message.answer(
        "🎛 <b>Пульт управления:</b>",
        reply_markup=get_sister_panel_keyboard(),
    )


@router.message(Command("requests"))
async def cmd_requests(message: Message) -> None:
    if message.from_user.id != OWNER_ID:
        return

    recent = await get_recent_requests(limit=10)
    if not recent:
        await message.answer("📭 История запросов пока пуста.")
        return

    lines = ["📋 <b>Последние 10 запросов:</b>\n"]
    for r in recent:
        status_readable = STATUS_TEXTS.get(r["status"], r["status"])
        created = r["created_at"][:16]
        lines.append(f"• <b>#{r['id']}</b> | {r['task_name']}\n  Статус: {status_readable} <i>({created})</i>")

    await message.answer("\n\n".join(lines))


@router.message(Command("stats"))
async def cmd_stats(message: Message) -> None:
    if message.from_user.id != OWNER_ID:
        return

    stats = await get_task_stats()
    lines = ["📊 <b>Статистика выполненных поручений:</b>\n"]
    total = 0

    for code, title in TASK_CATALOG.items():
        count = stats.get(code, 0)
        total += count
        lines.append(f"• {title}: <b>{declension_times(count)}</b>")

    lines.append(f"\nВсего выполнено: <b>{declension_times(total)}</b> 🫡")
    await message.answer("\n".join(lines))


@router.callback_query(F.data.startswith("sister_task:"))
async def handle_sister_task(callback: CallbackQuery, bot: Bot) -> None:
    user_id = callback.from_user.id
    recipient_id = await get_recipient_id()

    if user_id != recipient_id:
        if user_id == OWNER_ID:
            await callback.answer("ℹ️ Это пульт сестры. Твоя роль — исполнитель.", show_alert=True)
        elif user_id == OBSERVER_ID:
            await callback.answer("ℹ️ Ты наблюдатель. Нажимать кнопки может только сестра.", show_alert=True)
        else:
            await callback.answer("⛔ Доступ запрещён.", show_alert=True)
        return

    task_code = callback.data.split(":", 1)[1]
    task_name = TASK_CATALOG.get(task_code, "Неизвестное задание")

    req_id = await create_request(task_code, task_name)
    await callback.answer("Запрос отправлен 🫡", show_alert=False)

    # Уведомление для тебя (владелец с кнопками)
    owner_message = (
        f"🚨 <b>НОВЫЙ ЗАПРОС #{req_id}</b>\n\n"
        f"<b>{task_name}</b>\n\n"
        f"Заказчик нажал кнопку.\n"
        f"Пора выполнять."
    )
    try:
        await bot.send_message(
            OWNER_ID,
            owner_message,
            reply_markup=get_owner_action_keyboard(req_id),
        )
    except TelegramAPIError as e:
        logger.error(f"Не удалось доставить запрос #{req_id} владельцу: {e}")

    # Уведомление для наблюдателя (без кнопок)
    observer_message = (
        f"🔔 <b>НОВЫЙ ЗАПРОС #{req_id}</b> (Только просмотр)\n\n"
        f"<b>{task_name}</b>\n\n"
        f"Сестра нажала кнопку на пульте."
    )
    try:
        await bot.send_message(OBSERVER_ID, observer_message)
    except TelegramAPIError as e:
        logger.warning(f"Не удалось доставить запрос #{req_id} наблюдателю (нужно, чтобы он нажал /start): {e}")


@router.callback_query(F.data.startswith("owner_act:"))
async def handle_owner_action(callback: CallbackQuery, bot: Bot) -> None:
    if callback.from_user.id != OWNER_ID:
        await callback.answer("🚫 Только главный исполнитель может нажимать эти кнопки!", show_alert=True)
        return

    parts = callback.data.split(":")
    action = parts[1]
    req_id = int(parts[2])

    req = await get_request(req_id)
    if not req:
        await callback.answer("Запрос не найден в базе данных.", show_alert=True)
        return

    if req["status"] in ("completed", "rejected"):
        await callback.answer("Этот запрос уже завершён.", show_alert=True)
        return

    recipient_id = await get_recipient_id()

    if action == "going":
        await update_request_status(req_id, "in_progress")
        await callback.answer("Статус: уже идёшь 🏃")

        if recipient_id:
            try:
                await bot.send_message(recipient_id, "🫡 Исполнитель уже выдвинулся.")
            except TelegramAPIError as e:
                logger.error(f"Ошибка отправки сестре: {e}")

        new_text = (
            f"🚨 <b>ЗАПРОС #{req_id}</b>\n\n"
            f"<b>{req['task_name']}</b>\n\n"
            f"Текущий статус: ⏳ <b>Уже в пути</b>"
        )
        try:
            await callback.message.edit_text(
                new_text,
                reply_markup=get_owner_action_keyboard(req_id, in_progress=True),
            )
        except TelegramAPIError:
            pass

    elif action == "done":
        await update_request_status(req_id, "completed")
        await callback.answer("Задание отмечено выполненным!")

        if recipient_id:
            try:
                await bot.send_message(recipient_id, "✅ Задание отмечено как выполненное.")
            except TelegramAPIError as e:
                logger.error(f"Ошибка отправки сестре: {e}")

        new_text = (
            f"🚨 <b>ЗАПРОС #{req_id}</b>\n\n"
            f"<b>{req['task_name']}</b>\n\n"
            f"Итог: ✅ <b>Выполнено</b>"
        )
        try:
            await callback.message.edit_text(new_text, reply_markup=None)
        except TelegramAPIError:
            pass

    elif action == "reject":
        await update_request_status(req_id, "rejected")
        await callback.answer("Запрос отклонён.")

        if recipient_id:
            try:
                await bot.send_message(recipient_id, "❌ Запрос отклонён. Возмутительно.")
            except TelegramAPIError as e:
                logger.error(f"Ошибка отправки сестре: {e}")

        new_text = (
            f"🚨 <b>ЗАПРОС #{req_id}</b>\n\n"
            f"<b>{req['task_name']}</b>\n\n"
            f"Итог: ❌ <b>Отклонено</b>"
        )
        try:
            await callback.message.edit_text(new_text, reply_markup=None)
        except TelegramAPIError:
            pass

# =====================================================================
# ТОЧКА ВХОДА
# =====================================================================

async def main() -> None:
    logger.info("Запуск базы данных...")
    await init_db()

    bot = Bot(
        token=BOT_TOKEN,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )
    dp = Dispatcher()
    dp.include_router(router)

    await bot.delete_webhook(drop_pending_updates=True)
    logger.info("Бот готов к работе!")

    try:
        await dp.start_polling(bot)
    finally:
        await bot.session.close()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        logger.info("Бот остановлен.")
