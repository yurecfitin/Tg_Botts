import asyncio
import re
import os
import aiosqlite
from aiogram import Bot, Dispatcher, F, Router
from aiogram.filters import Command, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import StatesGroup, State
from aiogram.types import (
    Message, CallbackQuery,
    InlineKeyboardMarkup, InlineKeyboardButton,
    ReplyKeyboardMarkup, ReplyKeyboardRemove,
)
from aiogram.utils.keyboard import InlineKeyboardBuilder, ReplyKeyboardBuilder
from aiogram.exceptions import TelegramBadRequest

BOT_TOKEN = os.getenv("TOKEN")
ADMIN_ID = 6113518001
DB_PATH = "courses.db"
PAGE_SIZE = 5

router = Router()
user_buy_page: dict[int, int] = {}

# ============================================================
# БАЗА ДАННЫХ
# ============================================================
async def init_db():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS courses (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                price TEXT NOT NULL,
                video_file_id TEXT,
                payment_link TEXT
            )""")
        cur = await db.execute("PRAGMA table_info(courses)")
        cols = [row[1] for row in await cur.fetchall()]
        if "video_file_id" not in cols:
            await db.execute("ALTER TABLE courses ADD COLUMN video_file_id TEXT")
        if "payment_link" not in cols:
            await db.execute("ALTER TABLE courses ADD COLUMN payment_link TEXT")

        await db.execute("""
            CREATE TABLE IF NOT EXISTS purchases (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                course_id INTEGER NOT NULL
            )""")
        await db.execute("""
            CREATE TABLE IF NOT EXISTS pending_purchases (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                course_id INTEGER NOT NULL,
                receipt_file_id TEXT
            )""")
        cur = await db.execute("PRAGMA table_info(pending_purchases)")
        cols = [row[1] for row in await cur.fetchall()]
        if "receipt_file_id" not in cols:
            await db.execute("ALTER TABLE pending_purchases ADD COLUMN receipt_file_id TEXT")

        cur = await db.execute("SELECT COUNT(*) FROM courses")
        if (await cur.fetchone())[0] == 0:
            demo = [
                ("Python с нуля", "4 900 ₽"),
                ("Telegram-боты на aiogram 3", "6 900 ₽"),
                ("SQL для аналитиков", "3 900 ₽"),
                ("FastAPI + PostgreSQL", "7 900 ₽"),
                ("Docker и CI/CD", "5 900 ₽"),
                ("Алгоритмы и структуры данных", "8 900 ₽"),
            ]
            await db.executemany(
                "INSERT INTO courses (name, price) VALUES (?, ?)", demo)
        await db.commit()


async def get_courses(page: int = 0, only_visible: bool = True):
    where = ("WHERE video_file_id IS NOT NULL AND payment_link IS NOT NULL "
             "AND payment_link != ''") if only_visible else ""
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            f"SELECT id, name, price FROM courses {where} "
            f"ORDER BY id LIMIT ? OFFSET ?",
            (PAGE_SIZE, page * PAGE_SIZE))
        rows = await cur.fetchall()
        cur = await db.execute(f"SELECT COUNT(*) FROM courses {where}")
        total = (await cur.fetchone())[0]
    return rows, total


async def get_course(course_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            "SELECT id, name, price, video_file_id, payment_link "
            "FROM courses WHERE id = ?", (course_id,))
        return await cur.fetchone()


async def get_user_courses(user_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("""
            SELECT c.name, c.price FROM purchases p
            JOIN courses c ON c.id = p.course_id
            WHERE p.user_id = ? ORDER BY p.id DESC""", (user_id,))
        return await cur.fetchall()


async def user_owns_course(user_id: int, course_id: int) -> bool:
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            "SELECT id FROM purchases WHERE user_id = ? AND course_id = ?",
            (user_id, course_id))
        return await cur.fetchone() is not None


async def add_purchase(user_id: int, course_id: int) -> bool:
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            "SELECT id FROM purchases WHERE user_id = ? AND course_id = ?",
            (user_id, course_id))
        if await cur.fetchone() is None:
            await db.execute(
                "INSERT INTO purchases (user_id, course_id) VALUES (?, ?)",
                (user_id, course_id))
            await db.commit()
            return True
        return False


async def create_pending(user_id: int, course_id: int,
                         receipt_file_id: str | None = None) -> int:
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            "INSERT INTO pending_purchases (user_id, course_id, receipt_file_id) "
            "VALUES (?, ?, ?)", (user_id, course_id, receipt_file_id))
        await db.commit()
        return cur.lastrowid


async def get_pending(pending_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            "SELECT id, user_id, course_id, receipt_file_id "
            "FROM pending_purchases WHERE id = ?", (pending_id,))
        return await cur.fetchone()


async def delete_pending(pending_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("DELETE FROM pending_purchases WHERE id = ?", (pending_id,))
        await db.commit()


async def update_course(course_id: int, name: str, price: str,
                        payment_link: str | None = None,
                        video_file_id: str | None = None,
                        change_link: bool = False,
                        change_video: bool = False):
    async with aiosqlite.connect(DB_PATH) as db:
        sets = ["name = ?", "price = ?"]
        params = [name, price]
        if change_link:
            sets.append("payment_link = ?")
            params.append(payment_link)
        if change_video:
            sets.append("video_file_id = ?")
            params.append(video_file_id)
        params.append(course_id)
        await db.execute(f"UPDATE courses SET {', '.join(sets)} WHERE id = ?", params)
        await db.commit()


async def create_course(name: str, price: str,
                        payment_link: str, video_file_id: str):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT INTO courses (name, price, payment_link, video_file_id) "
            "VALUES (?, ?, ?, ?)",
            (name, price, payment_link, video_file_id))
        await db.commit()


# ============================================================
# УТИЛИТЫ
# ============================================================
async def safe_edit(message: Message, text: str, reply_markup=None, parse_mode=None):
    try:
        await message.edit_text(text, reply_markup=reply_markup, parse_mode=parse_mode)
    except TelegramBadRequest:
        pass


# ============================================================
# КЛАВИАТУРЫ
# ============================================================
def main_inline_kb() -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="📚 Курсы", callback_data="courses_page:0")
    kb.button(text="🎓 Ваши курсы", callback_data="my_courses")
    return kb.as_markup()


def admin_reply_kb() -> ReplyKeyboardMarkup:
    kb = ReplyKeyboardBuilder()
    kb.button(text="⚙️ Админ панель")
    return kb.as_markup(resize_keyboard=True)


def fallback_kb(user_id: int) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="🏠 Главное меню", callback_data="main_menu")
    if user_id == ADMIN_ID:
        kb.button(text="⚙️ Админ панель", callback_data="admin_panel")
    return kb.as_markup()


def courses_page_kb(page: int, total: int) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    total_pages = (total + PAGE_SIZE - 1) // PAGE_SIZE
    if page > 0:
        kb.button(text="⬅ Назад", callback_data=f"courses_page:{page - 1}")
    else:
        kb.button(text="⬅ Назад", callback_data="main_menu")
    if page < total_pages - 1:
        kb.button(text="Вперёд ➡", callback_data=f"courses_page:{page + 1}")
    kb.row(InlineKeyboardButton(text="🛒 Купить курсы",
                                callback_data=f"buy_menu:{page}"))
    return kb.as_markup()


def buy_course_reply_kb(courses: list) -> ReplyKeyboardMarkup:
    kb = ReplyKeyboardBuilder()
    for i, (_, name, price) in enumerate(courses, start=1):
        kb.button(text=f"{i}. {name} — {price}")
    kb.adjust(1)
    return kb.as_markup(resize_keyboard=True, one_time_keyboard=True)


def course_detail_inline_kb(course_id: int, page: int,
                            payment_link: str) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="💳 Купить", url=payment_link)
    kb.button(text="📸 Отправить чек", callback_data=f"receipt:{course_id}:{page}")
    kb.button(text="⬅ Назад", callback_data=f"buy_menu:{page}")
    kb.adjust(1)
    return kb.as_markup()


# ============================================================
# СОСТОЯНИЯ
# ============================================================
class AdminStates(StatesGroup):
    choose_course = State()
    enter_name = State()
    enter_price = State()
    enter_link = State()
    enter_video = State()
    create_name = State()
    create_price = State()
    create_link = State()
    create_video = State()


class PurchaseStates(StatesGroup):
    waiting_receipt = State()


# ============================================================
# /start
# ============================================================
@router.message(Command("start"))
async def cmd_start(message: Message, state: FSMContext):
    await state.clear()
    text = (
        "👋 Добро пожаловать!\n\n"
        "Этот бот создан для **покупки курсов**.\n"
        "Посмотрите доступные курсы, купите их и получите доступ к видео.\n\n"
        "Выберите действие:"
    )
    await message.answer(text, reply_markup=main_inline_kb(), parse_mode="Markdown")

    if message.from_user.id == ADMIN_ID:
        # просто показываем reply-клавиатуру с «⚙️ Админ панель», без подсказок
        await message.answer("⚙️", reply_markup=admin_reply_kb())
    else:
        # убираем возможные старые reply-клавиатуры у пользователя
        await message.answer("👌", reply_markup=ReplyKeyboardRemove())


# ============================================================
# ГЛАВНОЕ МЕНЮ (inline + ручной ввод текстом)
# ============================================================
@router.callback_query(F.data == "main_menu")
async def main_menu_cb(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    await safe_edit(callback.message, "🏠 Главное меню\n\nВыберите действие:",
                    main_inline_kb())
    await callback.answer()


@router.message(StateFilter(None), F.text.in_({"Главное меню", "🏠 Главное меню"}))
async def main_menu_text(message: Message):
    await message.answer("🏠 Главное меню", reply_markup=main_inline_kb())


# ============================================================
# СПИСОК КУРСОВ
# ============================================================
@router.callback_query(F.data.startswith("courses_page:"))
async def show_courses(callback: CallbackQuery):
    page = int(callback.data.split(":")[1])
    courses, total = await get_courses(page)
    if not courses:
        await safe_edit(callback.message, "Пока нет доступных курсов.",
                        main_inline_kb())
        await callback.answer()
        return
    text = "📚 Доступные курсы:\n\n" + "\n".join(
        f"• **{name}** — {price}" for _, name, price in courses)
    await safe_edit(callback.message, text,
                    courses_page_kb(page, total), parse_mode="Markdown")
    await callback.answer()


@router.callback_query(F.data.startswith("buy_menu:"))
async def buy_menu(callback: CallbackQuery):
    page = int(callback.data.split(":")[1])
    courses, _ = await get_courses(page)
    if not courses:
        await safe_edit(callback.message, "Курсы не найдены.", main_inline_kb())
        await callback.answer()
        return
    user_buy_page[callback.from_user.id] = page

    back_kb = InlineKeyboardBuilder()
    back_kb.button(text="⬅ Назад", callback_data=f"courses_page:{page}")
    await safe_edit(callback.message, "Выберите курс из меню ниже:",
                    back_kb.as_markup())
    await callback.message.answer(
        "Какой курс вы хотите купить?\nНажмите номер на клавиатуре ниже:",
        reply_markup=buy_course_reply_kb(courses))
    await callback.answer()


@router.message(StateFilter(None), F.text.regexp(r"^\d+"))
async def pick_course_reply(message: Message):
    user_id = message.from_user.id
    page = user_buy_page.get(user_id, 0)
    courses, _ = await get_courses(page)
    if not courses:
        await message.answer("🤷 Я вас не понял.",
                             reply_markup=fallback_kb(user_id))
        return

    m = re.match(r"^(\d+)", message.text or "")
    if not m:
        await message.answer("🤷 Я вас не понял.",
                             reply_markup=fallback_kb(user_id))
        return

    idx = int(m.group(1)) - 1
    if idx < 0 or idx >= len(courses):
        await message.answer("🤷 Я вас не понял.",
                             reply_markup=fallback_kb(user_id))
        return

    course_id, name, price = courses[idx]
    course = await get_course(course_id)
    if not course or not course[4]:
        await message.answer("⚠ У этого курса нет ссылки на оплату.")
        return

    await message.answer(
        f"Вы выбрали:\n\n**{name}**\nЦена: **{price}**\n\n"
        f"1) Нажмите **💳 Купить** — откроется страница оплаты.\n"
        f"2) После оплаты нажмите **📸 Отправить чек** и пришлите скриншот.",
        reply_markup=course_detail_inline_kb(course_id, page, course[4]),
        parse_mode="Markdown")


# ============================================================
# ЧЕК
# ============================================================
@router.callback_query(F.data.startswith("receipt:"))
async def start_receipt(callback: CallbackQuery, state: FSMContext):
    _, course_id_s, _ = callback.data.split(":")
    course_id = int(course_id_s)
    course = await get_course(course_id)
    if not course:
        await callback.answer("Курс не найден.", show_alert=True)
        return
    if await user_owns_course(callback.from_user.id, course_id):
        await callback.answer("У вас уже есть этот курс.", show_alert=True)
        return

    await state.update_data(buy_course_id=course_id)
    await state.set_state(PurchaseStates.waiting_receipt)

    await safe_edit(
        callback.message,
        f"📸 Отправьте скриншот/фото чека об оплате курса "
        f"**{course[1]}** за **{course[2]}**.\n\n"
        f"Администратор проверит чек и подтвердит покупку.",
        main_inline_kb(), parse_mode="Markdown")
    await callback.answer()


@router.message(PurchaseStates.waiting_receipt,
                F.text.in_({"Главное меню", "🏠 Главное меню"}))
async def cancel_receipt(message: Message, state: FSMContext):
    await state.clear()
    await message.answer("Покупка отменена.", reply_markup=main_inline_kb())


@router.message(PurchaseStates.waiting_receipt, F.photo)
async def receive_receipt(message: Message, state: FSMContext, bot: Bot):
    data = await state.get_data()
    course_id = data.get("buy_course_id")
    await state.clear()

    if not course_id:
        await message.answer("Что-то пошло не так. Попробуйте снова.",
                             reply_markup=main_inline_kb())
        return
    course = await get_course(course_id)
    if not course:
        await message.answer("Курс не найден.", reply_markup=main_inline_kb())
        return

    receipt_file_id = message.photo[-1].file_id
    pending_id = await create_pending(message.from_user.id, course_id, receipt_file_id)

    user = message.from_user
    info = (
        f"🛒 Новый запрос на покупку\n\n"
        f"Пользователь: {user.full_name}\n"
        f"Username: @{user.username or '—'}\n"
        f"ID: {user.id}\n"
        f"Курс: {course[1]}\n"
        f"Цена: {course[2]}\n"
        f"Ссылка на оплату: {course[4]}")

    admin_kb = InlineKeyboardBuilder()
    admin_kb.button(text="✅ Подтвердить", callback_data=f"admin_confirm:{pending_id}")
    admin_kb.button(text="❌ Отклонить", callback_data=f"admin_reject:{pending_id}")

    try:
        await bot.send_photo(ADMIN_ID, receipt_file_id,
                             caption=info,
                             reply_markup=admin_kb.as_markup())
    except Exception as e:
        await delete_pending(pending_id)
        await message.answer(f"⚠ Не удалось отправить чек админу: {e}",
                             reply_markup=main_inline_kb())
        return

    await message.answer("✅ Чек отправлен администратору. Ожидайте подтверждения.",
                         reply_markup=main_inline_kb())


@router.message(PurchaseStates.waiting_receipt)
async def wrong_receipt(message: Message):
    await message.answer(
        "📸 Пожалуйста, отправьте именно **фото** чека об оплате "
        "(или «🏠 Главное меню» для отмены).", parse_mode="Markdown")


# ============================================================
# ВАШИ КУРСЫ
# ============================================================
@router.callback_query(F.data == "my_courses")
async def my_courses(callback: CallbackQuery):
    purchases = await get_user_courses(callback.from_user.id)
    text = ("У вас пока нет приобретённых курсов."
            if not purchases else
            "🎓 Ваши курсы:\n\n" + "\n".join(f"• **{n}** — {p}" for n, p in purchases))
    back_kb = InlineKeyboardBuilder()
    back_kb.button(text="⬅ Назад", callback_data="main_menu")
    await safe_edit(callback.message, text, back_kb.as_markup(), parse_mode="Markdown")
    await callback.answer()


# ============================================================
# АДМИН: ПОДТВЕРЖДЕНИЕ / ОТКЛОНЕНИЕ
# ============================================================
@router.callback_query(F.data.startswith("admin_confirm:"))
async def admin_confirm(callback: CallbackQuery):
    if callback.from_user.id != ADMIN_ID:
        await callback.answer("Нет доступа", show_alert=True)
        return

    pending_id = int(callback.data.split(":")[1])
    pending = await get_pending(pending_id)
    if not pending:
        await callback.answer("Запрос не найден или уже обработан.", show_alert=True)
        return

    _, user_id, course_id, _ = pending
    course = await get_course(course_id)
    await add_purchase(user_id, course_id)
    await delete_pending(pending_id)

    try:
        await callback.bot.send_message(
            user_id,
            f"✅ Покупка подтверждена!\nКурс: **{course[1]}**\n\nОтправляю доступ...",
            parse_mode="Markdown")
        if course[3]:
            await callback.bot.send_video(user_id, course[3],
                                          caption=f"Курс: {course[1]}")
        else:
            await callback.bot.send_message(
                user_id, "⚠ Видео курса пока не загружено администратором.")
        await callback.bot.send_message(user_id, "Главное меню:",
                                        reply_markup=main_inline_kb())
    except Exception as e:
        await callback.message.answer(f"⚠ Ошибка отправки пользователю: {e}")

    try:
        if callback.message.photo:
            await callback.message.edit_caption(
                caption=(callback.message.caption or "") + "\n\n✅ Подтверждено",
                reply_markup=None)
        else:
            await callback.message.edit_text(
                (callback.message.text or "") + "\n\n✅ Подтверждено",
                reply_markup=None)
    except TelegramBadRequest:
        pass
    await callback.answer("Подтверждено")


@router.callback_query(F.data.startswith("admin_reject:"))
async def admin_reject(callback: CallbackQuery):
    if callback.from_user.id != ADMIN_ID:
        await callback.answer("Нет доступа", show_alert=True)
        return

    pending_id = int(callback.data.split(":")[1])
    pending = await get_pending(pending_id)
    if not pending:
        await callback.answer("Запрос не найден.", show_alert=True)
        return

    _, user_id, _, _ = pending
    await delete_pending(pending_id)

    try:
        await callback.bot.send_message(
            user_id, "❌ К сожалению, покупка отклонена администратором.",
            reply_markup=main_inline_kb())
    except Exception:
        pass

    try:
        if callback.message.photo:
            await callback.message.edit_caption(
                caption=(callback.message.caption or "") + "\n\n❌ Отклонено",
                reply_markup=None)
        else:
            await callback.message.edit_text(
                (callback.message.text or "") + "\n\n❌ Отклонено",
                reply_markup=None)
    except TelegramBadRequest:
        pass
    await callback.answer("Отклонено")


# ============================================================
# АДМИН-ПАНЕЛЬ
# ============================================================
async def show_admin_panel(target: Message, state: FSMContext):
    courses, _ = await get_courses(0, only_visible=False)
    kb = InlineKeyboardBuilder()
    for course_id, name, price in courses:
        kb.button(text=f"{name} — {price}", callback_data=f"admin_edit:{course_id}")
    kb.button(text="➕ Добавить курс", callback_data="admin_add")
    kb.adjust(1)
    text = ("🛠 Выберите курс для редактирования:"
            if courses else "🛠 Курсов пока нет. Добавьте первый:")
    await target.answer(text, reply_markup=kb.as_markup())
    await state.set_state(AdminStates.choose_course)


@router.message(F.text == "⚙️ Админ панель")
async def admin_panel_btn(message: Message, state: FSMContext):
    if message.from_user.id != ADMIN_ID:
        await message.answer("🤷 Я вас не понял.",
                             reply_markup=fallback_kb(message.from_user.id))
        return
    await state.clear()
    await show_admin_panel(message, state)


@router.callback_query(F.data == "admin_panel")
async def admin_panel_cb(callback: CallbackQuery, state: FSMContext):
    if callback.from_user.id != ADMIN_ID:
        await callback.answer("Нет доступа", show_alert=True)
        return
    await state.clear()
    await show_admin_panel(callback.message, state)
    await callback.answer()


@router.message(Command("admin"))
async def admin_start(message: Message, state: FSMContext):
    if message.from_user.id != ADMIN_ID:
        await message.answer("⛔ У вас нет доступа.")
        return
    await state.clear()
    await show_admin_panel(message, state)


# ---------- РЕДАКТИРОВАНИЕ ----------
@router.callback_query(F.data.startswith("admin_edit:"), AdminStates.choose_course)
async def admin_choose_course(callback: CallbackQuery, state: FSMContext):
    if callback.from_user.id != ADMIN_ID:
        await callback.answer("Нет доступа", show_alert=True)
        return
    course_id = int(callback.data.split(":")[1])
    await state.update_data(course_id=course_id)
    await callback.message.edit_text("Введите новое **название** курса:")
    await state.set_state(AdminStates.enter_name)
    await callback.answer()


@router.message(AdminStates.enter_name)
async def admin_enter_name(message: Message, state: FSMContext):
    await state.update_data(new_name=(message.text or "").strip())
    await message.answer("Введите новую **цену** курса:")
    await state.set_state(AdminStates.enter_price)


@router.message(AdminStates.enter_price)
async def admin_enter_price(message: Message, state: FSMContext):
    await state.update_data(new_price=(message.text or "").strip())
    await message.answer(
        "Отправьте новую **ссылку на оплату** (http:// или https://) "
        "или /skip, чтобы оставить текущую.")
    await state.set_state(AdminStates.enter_link)


@router.message(AdminStates.enter_link, Command("skip"))
async def admin_skip_link(message: Message, state: FSMContext):
    await state.update_data(new_link=None, change_link=False)
    await message.answer(
        "Отправьте **новое видео** курса или /skip, чтобы оставить старое.")
    await state.set_state(AdminStates.enter_video)


@router.message(AdminStates.enter_link)
async def admin_enter_link(message: Message, state: FSMContext):
    link = (message.text or "").strip()
    if not link.startswith(("http://", "https://")):
        await message.answer("⚠ Ссылка должна начинаться с http:// или https://")
        return
    await state.update_data(new_link=link, change_link=True)
    await message.answer(
        "Отправьте **новое видео** курса или /skip, чтобы оставить старое.")
    await state.set_state(AdminStates.enter_video)


@router.message(AdminStates.enter_video, Command("skip"))
async def admin_skip_video(message: Message, state: FSMContext):
    data = await state.get_data()
    await update_course(
        data["course_id"], data["new_name"], data["new_price"],
        payment_link=data.get("new_link"),
        change_link=data.get("change_link", False),
        change_video=False)
    await state.clear()
    await message.answer("✅ Курс обновлён (видео без изменений).",
                         reply_markup=main_inline_kb())


@router.message(AdminStates.enter_video, F.video)
async def admin_enter_video(message: Message, state: FSMContext):
    data = await state.get_data()
    await update_course(
        data["course_id"], data["new_name"], data["new_price"],
        payment_link=data.get("new_link"),
        video_file_id=message.video.file_id,
        change_link=data.get("change_link", False),
        change_video=True)
    await state.clear()
    await message.answer("✅ Курс обновлён (видео заменено).",
                         reply_markup=main_inline_kb())


@router.message(AdminStates.enter_video)
async def admin_enter_video_invalid(message: Message):
    await message.answer("Отправьте видео или /skip.")


# ---------- ДОБАВЛЕНИЕ ----------
@router.callback_query(F.data == "admin_add")
async def admin_add_start(callback: CallbackQuery, state: FSMContext):
    if callback.from_user.id != ADMIN_ID:
        await callback.answer("Нет доступа", show_alert=True)
        return
    await callback.message.edit_text("Введите **название** нового курса:")
    await state.set_state(AdminStates.create_name)
    await callback.answer()


@router.message(AdminStates.create_name)
async def admin_create_name(message: Message, state: FSMContext):
    await state.update_data(create_name=(message.text or "").strip())
    await message.answer("Введите **цену** нового курса:")
    await state.set_state(AdminStates.create_price)


@router.message(AdminStates.create_price)
async def admin_create_price(message: Message, state: FSMContext):
    await state.update_data(create_price=(message.text or "").strip())
    await message.answer("Отправьте **ссылку на оплату** (http:// или https://):")
    await state.set_state(AdminStates.create_link)


@router.message(AdminStates.create_link)
async def admin_create_link(message: Message, state: FSMContext):
    link = (message.text or "").strip()
    if not link.startswith(("http://", "https://")):
        await message.answer("⚠ Ссылка должна начинаться с http:// или https://")
        return
    await state.update_data(create_link=link)
    await message.answer("Отправьте **видео** нового курса:")
    await state.set_state(AdminStates.create_video)


@router.message(AdminStates.create_video, F.video)
async def admin_create_video(message: Message, state: FSMContext):
    data = await state.get_data()
    await create_course(data["create_name"], data["create_price"],
                        data["create_link"], message.video.file_id)
    await state.clear()
    await message.answer(
        "✅ Новый курс добавлен вместе с видео и ссылкой на оплату.\n\n"
        "Его уже видят пользователи в разделе «Курсы».",
        reply_markup=main_inline_kb())


@router.message(AdminStates.create_video)
async def admin_create_video_invalid(message: Message):
    await message.answer("⚠ Нужно отправить именно **видео** файлом.",
                         parse_mode="Markdown")


# ============================================================
# CATCH-ALL (я вас не понял)
# ============================================================
@router.message(StateFilter(None))
async def fallback_message(message: Message):
    await message.answer("🤷 Я вас не понял.",
                         reply_markup=fallback_kb(message.from_user.id))


# ============================================================
# ЗАПУСК
# ============================================================
async def main():
    await init_db()
    bot = Bot(token=BOT_TOKEN)
    dp = Dispatcher()
    dp.include_router(router)
    print("Бот запущен...")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
