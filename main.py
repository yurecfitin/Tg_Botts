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
    ReplyKeyboardMarkup,
)
from aiogram.utils.keyboard import InlineKeyboardBuilder, ReplyKeyboardBuilder
from aiogram.exceptions import TelegramBadRequest

BOT_TOKEN = os.getenv("BOT_TOKEN")
ADMIN_ID = 6113518001
DB_PATH = "courses.db"
PAGE_SIZE = 5

CURRENCY = "₽"
LINE = "━━━━━━━━━━━━━━━━━━━━"
LINE_SHORT = "───────────────"

GLOBAL_LINK_KEY = "global_payment_link"

DEMO_NAMES = (
    "Python с нуля",
    "Telegram-боты на aiogram 3",
    "SQL для аналитиков",
    "FastAPI + PostgreSQL",
    "Docker и CI/CD",
    "Алгоритмы и структуры данных",
)

router = Router()


# ============================================================
# ЦЕНА
# ============================================================
def extract_digits(price_str) -> str:
    return re.sub(r"\D", "", str(price_str) if price_str is not None else "")


def fmt_price(price_str) -> str:
    digits = extract_digits(price_str)
    if not digits:
        return f"— {CURRENCY}"
    return f"{int(digits):,}".replace(",", " ") + f" {CURRENCY}"


def parse_price_input(text: str) -> str | None:
    cleaned = (text or "").strip().replace(" ", "").replace("\u00A0", "")
    if not cleaned or not cleaned.isdigit():
        return None
    return cleaned


def is_url(text: str) -> bool:
    return text.startswith(("http://", "https://"))


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

        await db.execute("""
            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT
            )""")

        # нормализация цен: только цифры
        cur = await db.execute("SELECT id, price FROM courses")
        for cid, price in await cur.fetchall():
            digits = extract_digits(price)
            if digits != str(price):
                await db.execute("UPDATE courses SET price = ? WHERE id = ?",
                                 (digits or str(price), cid))

        # удаление демо-курсов
        placeholders = ",".join("?" * len(DEMO_NAMES))
        cur = await db.execute(
            f"SELECT id FROM courses WHERE name IN ({placeholders})", DEMO_NAMES)
        demo_ids = [row[0] for row in await cur.fetchall()]
        if demo_ids:
            ids_ph = ",".join("?" * len(demo_ids))
            await db.execute(f"DELETE FROM purchases WHERE course_id IN ({ids_ph})", demo_ids)
            await db.execute(f"DELETE FROM pending_purchases WHERE course_id IN ({ids_ph})", demo_ids)
            await db.execute(f"DELETE FROM courses WHERE id IN ({ids_ph})", demo_ids)
        await db.commit()


# ---------- SETTINGS ----------
async def get_global_link() -> str | None:
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("SELECT value FROM settings WHERE key = ?",
                               (GLOBAL_LINK_KEY,))
        row = await cur.fetchone()
        return row[0] if row and row[0] else None


async def set_global_link(link: str | None):
    async with aiosqlite.connect(DB_PATH) as db:
        if link:
            await db.execute(
                "INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)",
                (GLOBAL_LINK_KEY, link))
        else:
            await db.execute("DELETE FROM settings WHERE key = ?", (GLOBAL_LINK_KEY,))
        await db.commit()


# ---------- COURSES ----------
async def get_courses(page: int = 0, only_visible: bool = True):
    global_link = await get_global_link() if only_visible else None
    if only_visible:
        if global_link:
            where = "WHERE video_file_id IS NOT NULL"
        else:
            where = ("WHERE video_file_id IS NOT NULL AND payment_link IS NOT NULL "
                     "AND payment_link != ''")
    else:
        where = ""
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


async def get_effective_link(course) -> str | None:
    if not course:
        return None
    return course[4] or await get_global_link()


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


async def delete_course(course_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("DELETE FROM purchases WHERE course_id = ?", (course_id,))
        await db.execute("DELETE FROM pending_purchases WHERE course_id = ?", (course_id,))
        await db.execute("DELETE FROM courses WHERE id = ?", (course_id,))
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
                        payment_link: str | None, video_file_id: str):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT INTO courses (name, price, payment_link, video_file_id) "
            "VALUES (?, ?, ?, ?)",
            (name, price, payment_link, video_file_id))
        await db.commit()


# ============================================================
# УТИЛИТЫ
# ============================================================
async def safe_edit(message: Message, text: str, reply_markup=None, parse_mode="HTML"):
    try:
        await message.edit_text(text, reply_markup=reply_markup, parse_mode=parse_mode)
    except TelegramBadRequest:
        pass


# ============================================================
# КЛАВИАТУРЫ
# ============================================================
def main_inline_kb() -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="📚 Каталог курсов", callback_data="courses_page:0")
    kb.button(text="🎓 Мои курсы", callback_data="my_courses")
    kb.adjust(1)
    return kb.as_markup()


def user_reply_kb(user_id: int) -> ReplyKeyboardMarkup:
    kb = ReplyKeyboardBuilder()
    kb.button(text="🏠 Главное меню")
    if user_id == ADMIN_ID:
        kb.button(text="⚙️ Админ панель")
    return kb.as_markup(resize_keyboard=True)


def fallback_kb(user_id: int) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="🏠 Главное меню", callback_data="main_menu")
    if user_id == ADMIN_ID:
        kb.button(text="⚙️ Админ панель", callback_data="admin_panel")
    kb.adjust(1)
    return kb.as_markup()


def courses_page_kb(page: int, total: int) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    total_pages = (total + PAGE_SIZE - 1) // PAGE_SIZE
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton(text="⬅️", callback_data=f"courses_page:{page - 1}"))
    nav.append(InlineKeyboardButton(text=f"{page + 1} / {total_pages}", callback_data="noop"))
    if page < total_pages - 1:
        nav.append(InlineKeyboardButton(text="➡️", callback_data=f"courses_page:{page + 1}"))
    if nav:
        kb.row(*nav)
    kb.row(InlineKeyboardButton(text="🛒 Выбрать курс", callback_data=f"buy_menu:{page}"))
    kb.row(InlineKeyboardButton(text="🏠 Главное меню", callback_data="main_menu"))
    return kb.as_markup()


def buy_menu_inline_kb(courses: list, page: int) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    for i, (course_id, name, price) in enumerate(courses, start=1):
        kb.button(text=f"{i}. {name} — {fmt_price(price)}",
                  callback_data=f"pick_course:{course_id}:{page}")
    kb.adjust(1)
    kb.row(InlineKeyboardButton(text="⬅️ Назад в каталог",
                                callback_data=f"courses_page:{page}"))
    kb.row(InlineKeyboardButton(text="🏠 Главное меню", callback_data="main_menu"))
    return kb.as_markup()


def course_detail_inline_kb(course_id: int, page: int,
                            payment_link: str) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="💳 Перейти к оплате", url=payment_link)
    kb.button(text="📸 Отправить чек", callback_data=f"receipt:{course_id}:{page}")
    kb.button(text="⬅️ Назад", callback_data=f"buy_menu:{page}")
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
    enter_global_link = State()


class PurchaseStates(StatesGroup):
    waiting_receipt = State()


# ============================================================
# /start
# ============================================================
@router.message(Command("start"))
async def cmd_start(message: Message, state: FSMContext):
    await state.clear()
    is_admin = message.from_user.id == ADMIN_ID
    text = (
        f"<b>👋 Добро пожаловать!</b>\n"
        f"{LINE}\n\n"
        f"Этот бот поможет вам <b>приобрести курсы</b> и получить "
        f"доступ к видеоурокам в удобном формате.\n\n"
        f"<b>Как это работает:</b>\n"
        f"  1️⃣ Откройте каталог и выберите курс\n"
        f"  2️⃣ Оплатите по ссылке\n"
        f"  3️⃣ Отправьте фото чека\n"
        f"  4️⃣ Получите видео после подтверждения\n\n"
    )
    text += (f"{LINE_SHORT}\n<i>⚙️ Вы вошли как администратор.</i>"
             if is_admin else f"{LINE}\n<i>Выберите действие 👇</i>")

    await message.answer(text, reply_markup=user_reply_kb(message.from_user.id),
                         parse_mode="HTML")
    await message.answer("<b>📋 Главное меню</b>\nВыберите раздел:",
                         reply_markup=main_inline_kb(), parse_mode="HTML")


# ============================================================
# ГЛАВНОЕ МЕНЮ
# ============================================================
@router.message(F.text.in_({"Главное меню", "🏠 Главное меню"}))
async def main_menu_text(message: Message, state: FSMContext):
    await state.clear()
    await message.answer(
        f"<b>🏠 Главное меню</b>\n{LINE}\nВыберите раздел:",
        reply_markup=main_inline_kb(), parse_mode="HTML")
    await message.answer("↩️ Нижняя панель всегда доступна.",
                         reply_markup=user_reply_kb(message.from_user.id))


@router.callback_query(F.data == "main_menu")
async def main_menu_cb(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    await safe_edit(callback.message,
                    f"<b>🏠 Главное меню</b>\n{LINE}\nВыберите раздел:",
                    main_inline_kb())
    await callback.answer()


@router.callback_query(F.data == "noop")
async def noop(callback: CallbackQuery):
    await callback.answer()


# ============================================================
# КАТАЛОГ
# ============================================================
@router.callback_query(F.data.startswith("courses_page:"))
async def show_courses(callback: CallbackQuery):
    page = int(callback.data.split(":")[1])
    courses, total = await get_courses(page)
    if not courses:
        await safe_edit(callback.message,
                        f"<b>📚 Каталог курсов</b>\n{LINE}\n\n"
                        f"<i>Пока курсов нет. Заходите позже 🕐</i>",
                        main_inline_kb())
        await callback.answer()
        return

    total_pages = (total + PAGE_SIZE - 1) // PAGE_SIZE
    lines = ["<b>📚 Каталог курсов</b>",
             LINE,
             f"<i>Страница {page + 1} из {total_pages} • Всего: {total}</i>",
             ""]
    for idx, (_, name, price) in enumerate(courses, start=page * PAGE_SIZE + 1):
        lines.append(f"  <b>{idx}.</b>  {name}")
        lines.append(f"      💵 <code>{fmt_price(price)}</code>")
        lines.append("")
    lines += [LINE_SHORT, "<i>👇 Нажмите «Выбрать курс»</i>"]
    await safe_edit(callback.message, "\n".join(lines),
                    courses_page_kb(page, total))
    await callback.answer()


@router.callback_query(F.data.startswith("buy_menu:"))
async def buy_menu(callback: CallbackQuery):
    page = int(callback.data.split(":")[1])
    courses, _ = await get_courses(page)
    if not courses:
        await safe_edit(callback.message, "🤷 <i>Курсы не найдены.</i>", main_inline_kb())
        await callback.answer()
        return
    lines = ["<b>🛒 Выбор курса</b>", LINE,
             "<i>Нажмите на нужный курс ниже:</i>", ""]
    for i, (_, name, price) in enumerate(courses, start=1):
        lines.append(f"  <b>{i}.</b>  {name} — <code>{fmt_price(price)}</code>")
    await safe_edit(callback.message, "\n".join(lines),
                    buy_menu_inline_kb(courses, page))
    await callback.answer()


@router.callback_query(F.data.startswith("pick_course:"))
async def pick_course_cb(callback: CallbackQuery):
    _, course_id_s, page_s = callback.data.split(":")
    course_id, page = int(course_id_s), int(page_s)
    course = await get_course(course_id)
    if not course:
        await callback.answer("Курс не найден.", show_alert=True)
        return
    payment_link = await get_effective_link(course)
    if not payment_link:
        await callback.answer("⚠ У курса нет ссылки на оплату.", show_alert=True)
        return

    text = (
        f"<b>🎓 Карточка курса</b>\n"
        f"{LINE}\n\n"
        f"<b>Название:</b>\n  {course[1]}\n\n"
        f"<b>Стоимость:</b>\n  💵 <code>{fmt_price(course[2])}</code>\n\n"
        f"{LINE_SHORT}\n"
        f"<b>Как купить:</b>\n"
        f"  1️⃣ Нажмите <b>«💳 Перейти к оплате»</b>\n"
        f"  2️⃣ Оплатите удобным способом\n"
        f"  3️⃣ Вернитесь и нажмите <b>«📸 Отправить чек»</b>\n\n"
        f"<i>После подтверждения администратором вы получите видео.</i>"
    )
    await safe_edit(callback.message, text,
                    course_detail_inline_kb(course_id, page, payment_link))
    await callback.answer()


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

    text = (
        f"<b>📸 Отправьте чек</b>\n"
        f"{LINE}\n\n"
        f"Курс: <b>{course[1]}</b>\n"
        f"К оплате: 💵 <code>{fmt_price(course[2])}</code>\n\n"
        f"{LINE_SHORT}\n"
        f"Пришлите <b>фотографию</b> или <b>скриншот</b> квитанции.\n"
        f"<i>Для отмены — «🏠 Главное меню».</i>"
    )
    await safe_edit(callback.message, text, main_inline_kb())
    await callback.answer()


@router.message(PurchaseStates.waiting_receipt,
                F.text.in_({"Главное меню", "🏠 Главное меню"}))
async def cancel_receipt(message: Message, state: FSMContext):
    await state.clear()
    await message.answer(
        f"<b>❌ Покупка отменена</b>\n{LINE}\n<i>Вы в главном меню.</i>",
        reply_markup=user_reply_kb(message.from_user.id), parse_mode="HTML")
    await message.answer("Выберите раздел:", reply_markup=main_inline_kb())


@router.message(PurchaseStates.waiting_receipt, F.photo)
async def receive_receipt(message: Message, state: FSMContext, bot: Bot):
    data = await state.get_data()
    course_id = data.get("buy_course_id")
    await state.clear()
    if not course_id:
        await message.answer("⚠ <i>Что-то пошло не так.</i>",
                             reply_markup=user_reply_kb(message.from_user.id),
                             parse_mode="HTML")
        return
    course = await get_course(course_id)
    if not course:
        await message.answer("⚠ <i>Курс не найден.</i>",
                             reply_markup=user_reply_kb(message.from_user.id),
                             parse_mode="HTML")
        return

    receipt_file_id = message.photo[-1].file_id
    pending_id = await create_pending(message.from_user.id, course_id, receipt_file_id)
    payment_link = await get_effective_link(course)

    user = message.from_user
    caption = (
        f"<b>🛒 Новая заявка на покупку</b>\n"
        f"{LINE}\n"
        f"<b>👤 Покупатель</b>\n"
        f"  • Имя: {user.full_name}\n"
        f"  • Username: @{user.username or '—'}\n"
        f"  • ID: <code>{user.id}</code>\n\n"
        f"<b>🎓 Курс</b>\n"
        f"  • Название: {course[1]}\n"
        f"  • Цена: <code>{fmt_price(course[2])}</code>\n\n"
        f"<b>🔗 Ссылка на оплату</b>\n"
        f"  {payment_link or '—'}\n\n"
        f"{LINE_SHORT}\n"
        f"<i>Подтвердите или отклоните заявку 👇</i>"
    )

    admin_kb = InlineKeyboardBuilder()
    admin_kb.button(text="✅ Подтвердить", callback_data=f"admin_confirm:{pending_id}")
    admin_kb.button(text="❌ Отклонить", callback_data=f"admin_reject:{pending_id}")
    admin_kb.adjust(2)

    try:
        await bot.send_photo(ADMIN_ID, receipt_file_id, caption=caption,
                             reply_markup=admin_kb.as_markup(), parse_mode="HTML")
    except Exception as e:
        await delete_pending(pending_id)
        await message.answer(f"⚠ <i>Не удалось отправить чек админу: {e}</i>",
                             reply_markup=user_reply_kb(message.from_user.id),
                             parse_mode="HTML")
        return

    await message.answer(
        f"<b>✅ Чек отправлен</b>\n{LINE}\n"
        f"<i>Администратор проверит оплату и подтвердит покупку.</i>",
        reply_markup=user_reply_kb(message.from_user.id), parse_mode="HTML")


@router.message(PurchaseStates.waiting_receipt)
async def wrong_receipt(message: Message):
    await message.answer(
        f"📸 <b>Нужна именно фотография</b>\n{LINE}\n"
        f"<i>Отправьте чек как фото или скриншот.\n"
        f"Для отмены — «🏠 Главное меню».</i>",
        parse_mode="HTML")


# ============================================================
# МОИ КУРСЫ
# ============================================================
@router.callback_query(F.data == "my_courses")
async def my_courses(callback: CallbackQuery):
    purchases = await get_user_courses(callback.from_user.id)
    if not purchases:
        text = (f"<b>🎓 Мои курсы</b>\n{LINE}\n\n"
                f"<i>У вас пока нет приобретённых курсов.</i>\n\n"
                f"<i>Загляните в каталог, чтобы выбрать свой первый курс 👇</i>")
    else:
        lines = [f"<b>🎓 Мои курсы</b>", LINE,
                 f"<i>Всего приобретено: <b>{len(purchases)}</b></i>", ""]
        for name, price in purchases:
            lines.append(f"  ✅  <b>{name}</b>")
            lines.append(f"      💵 <code>{fmt_price(price)}</code>")
            lines.append("")
        lines += [LINE_SHORT, "<i>Видео по этим курсам вы получали при покупке.</i>"]
        text = "\n".join(lines)
    back_kb = InlineKeyboardBuilder()
    back_kb.button(text="⬅️ В главное меню", callback_data="main_menu")
    await safe_edit(callback.message, text, back_kb.as_markup())
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
        await callback.answer("Заявка уже обработана.", show_alert=True)
        return
    _, user_id, course_id, _ = pending
    course = await get_course(course_id)
    await add_purchase(user_id, course_id)
    await delete_pending(pending_id)

    try:
        await callback.bot.send_message(
            user_id,
            f"<b>🎉 Покупка подтверждена!</b>\n{LINE}\n\n"
            f"Курс: <b>{course[1]}</b>\n"
            f"Оплата: 💵 <code>{fmt_price(course[2])}</code>\n\n"
            f"{LINE_SHORT}\n<i>Отправляю доступ к видеоуроку...</i>",
            parse_mode="HTML")
        if course[3]:
            await callback.bot.send_video(user_id, course[3],
                                          caption=f"🎓 <b>{course[1]}</b>",
                                          parse_mode="HTML")
        else:
            await callback.bot.send_message(user_id,
                                            "⚠ <i>Видео курса пока не загружено.</i>",
                                            parse_mode="HTML")
        await callback.bot.send_message(
            user_id,
            f"<b>✅ Готово</b>\n{LINE}\n<i>Курс доступен в разделе «Мои курсы».</i>",
            reply_markup=user_reply_kb(user_id), parse_mode="HTML")
    except Exception as e:
        await callback.message.answer(f"⚠ Ошибка отправки пользователю: {e}")

    try:
        if callback.message.photo:
            await callback.message.edit_caption(
                caption=(callback.message.caption or "") + "\n\n<b>✅ ПОДТВЕРЖДЕНО</b>",
                parse_mode="HTML", reply_markup=None)
        else:
            await callback.message.edit_text(
                (callback.message.text or "") + "\n\n<b>✅ ПОДТВЕРЖДЕНО</b>",
                parse_mode="HTML", reply_markup=None)
    except TelegramBadRequest:
        pass
    await callback.answer("✅ Подтверждено")


@router.callback_query(F.data.startswith("admin_reject:"))
async def admin_reject(callback: CallbackQuery):
    if callback.from_user.id != ADMIN_ID:
        await callback.answer("Нет доступа", show_alert=True)
        return
    pending_id = int(callback.data.split(":")[1])
    pending = await get_pending(pending_id)
    if not pending:
        await callback.answer("Заявка уже обработана.", show_alert=True)
        return
    _, user_id, _, _ = pending
    await delete_pending(pending_id)
    try:
        await callback.bot.send_message(
            user_id,
            f"<b>❌ Заявка отклонена</b>\n{LINE}\n"
            f"<i>К сожалению, администратор не подтвердил оплату.</i>",
            reply_markup=user_reply_kb(user_id), parse_mode="HTML")
    except Exception:
        pass
    try:
        if callback.message.photo:
            await callback.message.edit_caption(
                caption=(callback.message.caption or "") + "\n\n<b>❌ ОТКЛОНЕНО</b>",
                parse_mode="HTML", reply_markup=None)
        else:
            await callback.message.edit_text(
                (callback.message.text or "") + "\n\n<b>❌ ОТКЛОНЕНО</b>",
                parse_mode="HTML", reply_markup=None)
    except TelegramBadRequest:
        pass
    await callback.answer("❌ Отклонено")


# ============================================================
# АДМИН-ПАНЕЛЬ
# ============================================================
async def build_admin_panel():
    courses, _ = await get_courses(0, only_visible=False)
    global_link = await get_global_link()

    kb = InlineKeyboardBuilder()
    for course_id, name, price in courses:
        kb.button(text=f"✏️ {name} — {fmt_price(price)}",
                  callback_data=f"admin_edit:{course_id}")
    kb.button(text="➕ Добавить курс", callback_data="admin_add")
    mark = "✓" if global_link else "✗"
    kb.button(text=f"🔗 Одна ссылка для всех {mark}",
              callback_data="admin_global_link")
    kb.adjust(1)

    header = f"<b>⚙️ Админ-панель</b>\n{LINE}\n<i>Всего курсов: <b>{len(courses)}</b></i>\n"
    if global_link:
        header += f"<i>🌐 Общая ссылка: <code>{global_link}</code></i>\n"
    else:
        header += "<i>🌐 Общая ссылка: <b>не установлена</b></i>\n"
    header += "\n<b>Управление:</b>"
    return header, kb.as_markup()


async def show_admin_panel(target: Message, state: FSMContext):
    text, kb = await build_admin_panel()
    await target.answer(text, reply_markup=kb, parse_mode="HTML")
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
    text, kb = await build_admin_panel()
    await safe_edit(callback.message, text, kb)
    await state.set_state(AdminStates.choose_course)
    await callback.answer()


@router.message(Command("admin"))
async def admin_start(message: Message, state: FSMContext):
    if message.from_user.id != ADMIN_ID:
        await message.answer("⛔ У вас нет доступа.")
        return
    await state.clear()
    await show_admin_panel(message, state)


# ---------- ПОДМЕНЮ КУРСА ----------
@router.callback_query(F.data.startswith("admin_edit:"), AdminStates.choose_course)
async def admin_choose_course(callback: CallbackQuery, state: FSMContext):
    if callback.from_user.id != ADMIN_ID:
        await callback.answer("Нет доступа", show_alert=True)
        return
    course_id = int(callback.data.split(":")[1])
    course = await get_course(course_id)
    if not course:
        await callback.answer("Курс не найден.", show_alert=True)
        return
    await state.update_data(course_id=course_id)

    own_link = course[4] or ""
    global_link = await get_global_link() or ""
    effective = own_link or global_link or "—"
    link_src = ("своя" if own_link else ("глобальная" if global_link else "—"))

    kb = InlineKeyboardBuilder()
    kb.button(text="✏️ Редактировать", callback_data="admin_do_edit")
    kb.button(text="🗑 Удалить курс", callback_data="admin_do_delete")
    kb.button(text="⬅️ К панели", callback_data="admin_panel")
    kb.adjust(1)

    await safe_edit(callback.message,
        f"<b>🎓 Управление курсом</b>\n"
        f"{LINE}\n"
        f"<b>Название:</b> {course[1]}\n"
        f"<b>Цена:</b> <code>{fmt_price(course[2])}</code>\n"
        f"<b>Видео:</b> {'✅ загружено' if course[3] else '❌ нет'}\n"
        f"<b>Активная ссылка</b> ({link_src}):\n  {effective}\n\n"
        f"{LINE_SHORT}\n<i>Что сделать с курсом?</i>",
        kb.as_markup())
    await callback.answer()


@router.callback_query(F.data == "admin_do_edit", AdminStates.choose_course)
async def admin_do_edit(callback: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    course = await get_course(data["course_id"])
    await safe_edit(callback.message,
        f"<b>✏️ Редактирование курса</b>\n{LINE}\n"
        f"<b>Текущее название:</b> {course[1]}\n"
        f"<b>Текущая цена:</b> <code>{fmt_price(course[2])}</code>\n\n"
        f"{LINE_SHORT}\n<i>Шаг 1 из 4 — введите новое название:</i>")
    await state.set_state(AdminStates.enter_name)
    await callback.answer()


@router.callback_query(F.data == "admin_do_delete", AdminStates.choose_course)
async def admin_do_delete(callback: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    course = await get_course(data["course_id"])
    if not course:
        await callback.answer("Курс не найден.", show_alert=True)
        return

    kb = InlineKeyboardBuilder()
    kb.button(text="✅ Да, удалить", callback_data="admin_delete_confirm")
    kb.button(text="❌ Отмена", callback_data=f"admin_edit:{course[0]}")
    kb.adjust(1)

    await safe_edit(callback.message,
        f"<b>🗑 Удаление курса</b>\n{LINE}\n\n"
        f"<b>{course[1]}</b>\n"
        f"💵 <code>{fmt_price(course[2])}</code>\n\n"
        f"⚠ <b>Это действие нельзя отменить.</b>\n"
        f"<i>Все связанные покупки и заявки будут удалены.</i>",
        kb.as_markup())
    await callback.answer()


@router.callback_query(F.data == "admin_delete_confirm", AdminStates.choose_course)
async def admin_delete_confirm(callback: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    course_id = data.get("course_id")
    course = await get_course(course_id) if course_id else None
    if not course:
        await callback.answer("Курс не найден.", show_alert=True)
        return

    await delete_course(course_id)
    await state.clear()

    await safe_edit(callback.message,
        f"<b>✅ Курс удалён</b>\n{LINE}\n"
        f"<i>Курс «{course[1]}» и все связанные данные удалены.</i>",
        main_inline_kb())
    await callback.answer("Удалено")

    text, kb = await build_admin_panel()
    await callback.message.answer(text, reply_markup=kb, parse_mode="HTML")
    await state.set_state(AdminStates.choose_course)


# ---------- РЕДАКТИРОВАНИЕ ----------
@router.message(AdminStates.enter_name)
async def admin_enter_name(message: Message, state: FSMContext):
    name = (message.text or "").strip()
    if not name:
        await message.answer("⚠ <i>Название не может быть пустым.</i>",
                             parse_mode="HTML")
        return
    await state.update_data(new_name=name)
    await message.answer(
        f"<b>💰 Шаг 2 из 4</b>\n{LINE}\n"
        f"<i>Введите новую цену <b>только цифрами</b> (без {CURRENCY}), "
        f"например: <code>4900</code>:</i>",
        parse_mode="HTML")
    await state.set_state(AdminStates.enter_price)


@router.message(AdminStates.enter_price)
async def admin_enter_price(message: Message, state: FSMContext):
    price = parse_price_input(message.text)
    if price is None:
        await message.answer(
            f"⚠ <b>Некорректная цена</b>\n{LINE}\n"
            f"<i>Введите цену <b>только цифрами</b> (без {CURRENCY}).\n"
            f"Например: <code>4900</code>.</i>",
            parse_mode="HTML")
        return
    await state.update_data(new_price=price)
    await message.answer(
        f"<b>🔗 Шаг 3 из 4</b>\n{LINE}\n"
        f"<i>Отправьте новую ссылку (http:// или https://) или /skip.</i>",
        parse_mode="HTML")
    await state.set_state(AdminStates.enter_link)


@router.message(AdminStates.enter_link, Command("skip"))
async def admin_skip_link(message: Message, state: FSMContext):
    await state.update_data(new_link=None, change_link=False)
    await message.answer(
        f"<b>🎬 Шаг 4 из 4</b>\n{LINE}\n"
        f"<i>Отправьте новое видео курса или /skip.</i>",
        parse_mode="HTML")
    await state.set_state(AdminStates.enter_video)


@router.message(AdminStates.enter_link)
async def admin_enter_link(message: Message, state: FSMContext):
    link = (message.text or "").strip()
    if not is_url(link):
        await message.answer("⚠ <i>Ссылка должна начинаться с http:// или https://</i>",
                             parse_mode="HTML")
        return
    await state.update_data(new_link=link, change_link=True)
    await message.answer(
        f"<b>🎬 Шаг 4 из 4</b>\n{LINE}\n"
        f"<i>Отправьте новое видео курса или /skip.</i>",
        parse_mode="HTML")
    await state.set_state(AdminStates.enter_video)


@router.message(AdminStates.enter_video, Command("skip"))
async def admin_skip_video(message: Message, state: FSMContext):
    data = await state.get_data()
    await update_course(data["course_id"], data["new_name"], data["new_price"],
                        payment_link=data.get("new_link"),
                        change_link=data.get("change_link", False),
                        change_video=False)
    await state.clear()
    await message.answer(
        f"<b>✅ Курс обновлён</b>\n{LINE}\n<i>Видео не изменялось.</i>",
        reply_markup=main_inline_kb(), parse_mode="HTML")


@router.message(AdminStates.enter_video, F.video)
async def admin_enter_video(message: Message, state: FSMContext):
    data = await state.get_data()
    await update_course(data["course_id"], data["new_name"], data["new_price"],
                        payment_link=data.get("new_link"),
                        video_file_id=message.video.file_id,
                        change_link=data.get("change_link", False),
                        change_video=True)
    await state.clear()
    await message.answer(
        f"<b>✅ Курс обновлён</b>\n{LINE}\n<i>Видео заменено.</i>",
        reply_markup=main_inline_kb(), parse_mode="HTML")


@router.message(AdminStates.enter_video)
async def admin_enter_video_invalid(message: Message):
    await message.answer("⚠ <i>Отправьте видео или /skip.</i>", parse_mode="HTML")


# ---------- ДОБАВЛЕНИЕ ----------
@router.callback_query(F.data == "admin_add")
async def admin_add_start(callback: CallbackQuery, state: FSMContext):
    if callback.from_user.id != ADMIN_ID:
        await callback.answer("Нет доступа", show_alert=True)
        return
    await safe_edit(callback.message,
        f"<b>➕ Новый курс</b>\n{LINE}\n<i>Шаг 1 из 4 — введите название:</i>")
    await state.set_state(AdminStates.create_name)
    await callback.answer()


@router.message(AdminStates.create_name)
async def admin_create_name(message: Message, state: FSMContext):
    name = (message.text or "").strip()
    if not name:
        await message.answer("⚠ <i>Название не может быть пустым.</i>",
                             parse_mode="HTML")
        return
    await state.update_data(create_name=name)
    await message.answer(
        f"<b>💰 Шаг 2 из 4</b>\n{LINE}\n"
        f"<i>Введите цену <b>только цифрами</b> (без {CURRENCY}):</i>",
        parse_mode="HTML")
    await state.set_state(AdminStates.create_price)


@router.message(AdminStates.create_price)
async def admin_create_price(message: Message, state: FSMContext):
    price = parse_price_input(message.text)
    if price is None:
        await message.answer(
            f"⚠ <b>Некорректная цена</b>\n{LINE}\n"
            f"<i>Введите <b>только цифры</b>, например: <code>4900</code>.</i>",
            parse_mode="HTML")
        return
    await state.update_data(create_price=price)
    global_link = await get_global_link()
    hint = (f"<i>Отправьте ссылку или /skip, если хотите использовать "
            f"глобальную ссылку.</i>" if global_link else
            "<i>Отправьте ссылку (http:// или https://).</i>")
    await message.answer(f"<b>🔗 Шаг 3 из 4</b>\n{LINE}\n{hint}",
                         parse_mode="HTML")
    await state.set_state(AdminStates.create_link)


@router.message(AdminStates.create_link, Command("skip"))
async def admin_create_skip_link(message: Message, state: FSMContext):
    global_link = await get_global_link()
    if not global_link:
        await message.answer(
            f"⚠ <b>Глобальная ссылка не установлена</b>\n{LINE}\n"
            f"<i>Установите её в админ-панели «🔗 Одна ссылка для всех» "
            f"или введите ссылку для этого курса.</i>",
            parse_mode="HTML")
        return
    await state.update_data(create_link=None)
    await message.answer(
        f"<b>🎬 Шаг 4 из 4</b>\n{LINE}\n<i>Отправьте видео курса:</i>",
        parse_mode="HTML")
    await state.set_state(AdminStates.create_video)


@router.message(AdminStates.create_link)
async def admin_create_link(message: Message, state: FSMContext):
    link = (message.text or "").strip()
    if not is_url(link):
        await message.answer("⚠ <i>Ссылка должна начинаться с http:// или https://</i>",
                             parse_mode="HTML")
        return
    await state.update_data(create_link=link)
    await message.answer(
        f"<b>🎬 Шаг 4 из 4</b>\n{LINE}\n<i>Отправьте видео курса:</i>",
        parse_mode="HTML")
    await state.set_state(AdminStates.create_video)


@router.message(AdminStates.create_video, F.video)
async def admin_create_video(message: Message, state: FSMContext):
    data = await state.get_data()
    await create_course(data["create_name"], data["create_price"],
                        data.get("create_link"), message.video.file_id)
    await state.clear()
    await message.answer(
        f"<b>🎉 Курс добавлен!</b>\n{LINE}\n\n"
        f"<b>{data['create_name']}</b>\n"
        f"💵 <code>{fmt_price(data['create_price'])}</code>\n\n"
        f"{LINE_SHORT}\n"
        f"<i>Курс уже доступен пользователям в каталоге.</i>",
        reply_markup=main_inline_kb(), parse_mode="HTML")


@router.message(AdminStates.create_video)
async def admin_create_video_invalid(message: Message):
    await message.answer("⚠ <i>Нужно отправить именно видео файлом.</i>",
                         parse_mode="HTML")


# ============================================================
# ГЛОБАЛЬНАЯ ССЫЛКА
# ============================================================
@router.callback_query(F.data == "admin_global_link",
                       StateFilter(AdminStates.choose_course))
async def admin_global_link_menu(callback: CallbackQuery, state: FSMContext):
    if callback.from_user.id != ADMIN_ID:
        await callback.answer("Нет доступа", show_alert=True)
        return
    global_link = await get_global_link()

    kb = InlineKeyboardBuilder()
    if global_link:
        kb.button(text="✏️ Изменить", callback_data="admin_global_link_set")
        kb.button(text="🗑 Убрать", callback_data="admin_global_link_clear")
    else:
        kb.button(text="✏️ Установить", callback_data="admin_global_link_set")
    kb.button(text="⬅️ К панели", callback_data="admin_panel")
    kb.adjust(1)

    body = (f"<b>🔗 Одна ссылка для всех</b>\n{LINE}\n\n"
            f"<b>Текущая ссылка:</b>\n"
            f"  {global_link if global_link else '<i>не установлена</i>'}\n\n"
            f"{LINE_SHORT}\n"
            f"<b>Как это работает:</b>\n"
            f"  • Если у курса есть своя ссылка — используется она\n"
            f"  • Если нет — используется глобальная\n"
            f"  • Курсы без ссылок (и без глобальной) скрыты от пользователей\n\n"
            f"<i>При создании курса ссылку можно пропустить через /skip — "
            f"тогда применится глобальная.</i>")

    await safe_edit(callback.message, body, kb.as_markup())
    await callback.answer()


@router.callback_query(F.data == "admin_global_link_set",
                       StateFilter(AdminStates.choose_course))
async def admin_global_link_set_start(callback: CallbackQuery, state: FSMContext):
    if callback.from_user.id != ADMIN_ID:
        await callback.answer("Нет доступа", show_alert=True)
        return
    await safe_edit(callback.message,
        f"<b>✏️ Установка глобальной ссылки</b>\n{LINE}\n"
        f"<i>Отправьте URL (http:// или https://):</i>")
    await state.set_state(AdminStates.enter_global_link)
    await callback.answer()


@router.message(AdminStates.enter_global_link)
async def admin_global_link_set(message: Message, state: FSMContext):
    link = (message.text or "").strip()
    if not is_url(link):
        await message.answer(
            "⚠ <i>Ссылка должна начинаться с http:// или https://</i>",
            parse_mode="HTML")
        return
    await set_global_link(link)
    await state.clear()

    text, kb = await build_admin_panel()
    await message.answer(
        f"<b>✅ Глобальная ссылка установлена</b>\n{LINE}\n"
        f"<code>{link}</code>", parse_mode="HTML")
    await message.answer(text, reply_markup=kb, parse_mode="HTML")
    await state.set_state(AdminStates.choose_course)


@router.callback_query(F.data == "admin_global_link_clear",
                       StateFilter(AdminStates.choose_course))
async def admin_global_link_clear(callback: CallbackQuery, state: FSMContext):
    if callback.from_user.id != ADMIN_ID:
        await callback.answer("Нет доступа", show_alert=True)
        return
    await set_global_link(None)
    text, kb = await build_admin_panel()
    await safe_edit(callback.message,
                    f"<b>✅ Глобальная ссылка удалена</b>\n{LINE}\n"
                    f"<i>Курсы без собственных ссылок больше не показываются "
                    f"пользователям.</i>", kb)
    await callback.answer("Удалено")


# ============================================================
# CATCH-ALL
# ============================================================
@router.message(StateFilter(None))
async def fallback_message(message: Message):
    await message.answer(
        f"<b>🤷 Я вас не понял</b>\n{LINE}\n"
        f"<i>Воспользуйтесь кнопками ниже.</i>",
        reply_markup=fallback_kb(message.from_user.id), parse_mode="HTML")


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
