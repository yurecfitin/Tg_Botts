import asyncio
import aiosqlite
import os
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

BOT_TOKEN = os.getenv("TOKEN")
ADMIN_ID = 6113518001
DB_PATH = "courses.db"
PAGE_SIZE = 5

router = Router()

# Стилистика
LINE = "━━━━━━━━━━━━━━━━━━━━"
LINE_SHORT = "───────────────"

DEMO_NAMES = (
    "Python с нуля",
    "Telegram-боты на aiogram 3",
    "SQL для аналитиков",
    "FastAPI + PostgreSQL",
    "Docker и CI/CD",
    "Алгоритмы и структуры данных",
)

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

        # 🧹 удаляем демо-курсы и связанные записи
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
    nav_row = []
    if page > 0:
        nav_row.append(InlineKeyboardButton(text="⬅️", callback_data=f"courses_page:{page - 1}"))
    nav_row.append(InlineKeyboardButton(text=f"{page + 1} / {total_pages}",
                                        callback_data="noop"))
    if page < total_pages - 1:
        nav_row.append(InlineKeyboardButton(text="➡️", callback_data=f"courses_page:{page + 1}"))
    if nav_row:
        kb.row(*nav_row)
    kb.row(InlineKeyboardButton(text="🛒 Выбрать курс",
                                callback_data=f"buy_menu:{page}"))
    kb.row(InlineKeyboardButton(text="🏠 Главное меню",
                                callback_data="main_menu"))
    return kb.as_markup()


def buy_menu_inline_kb(courses: list, page: int) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    for i, (course_id, name, price) in enumerate(courses, start=1):
        kb.button(text=f"{i}. {name} — {price}",
                  callback_data=f"pick_course:{course_id}:{page}")
    kb.adjust(1)
    kb.row(InlineKeyboardButton(text="⬅️ Назад в каталог",
                                callback_data=f"courses_page:{page}"))
    kb.row(InlineKeyboardButton(text="🏠 Главное меню",
                                callback_data="main_menu"))
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
    if is_admin:
        text += (
            f"{LINE_SHORT}\n"
            f"<i>⚙️ Вы вошли как администратор. Панель управления — "
            f"на нижней панели.</i>"
        )
    else:
        text += f"{LINE}\n<i>Выберите действие 👇</i>"

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
    await safe_edit(
        callback.message,
        f"<b>🏠 Главное меню</b>\n{LINE}\nВыберите раздел:",
        main_inline_kb())
    await callback.answer()


@router.callback_query(F.data == "noop")
async def noop(callback: CallbackQuery):
    await callback.answer()


# ============================================================
# КАТАЛОГ КУРСОВ
# ============================================================
@router.callback_query(F.data.startswith("courses_page:"))
async def show_courses(callback: CallbackQuery):
    page = int(callback.data.split(":")[1])
    courses, total = await get_courses(page)
    if not courses:
        await safe_edit(
            callback.message,
            f"<b>📚 Каталог курсов</b>\n{LINE}\n\n"
            f"<i>Пока курсов нет. Заходите позже 🕐</i>",
            main_inline_kb())
        await callback.answer()
        return

    total_pages = (total + PAGE_SIZE - 1) // PAGE_SIZE
    lines = [f"<b>📚 Каталог курсов</b>",
             f"{LINE}",
             f"<i>Страница {page + 1} из {total_pages} • Всего: {total}</i>",
             ""]
    for idx, (_, name, price) in enumerate(courses, start=page * PAGE_SIZE + 1):
        lines.append(f"  <b>{idx}.</b>  {name}")
        lines.append(f"      💵 <code>{price}</code>")
        lines.append("")
    lines.append(f"{LINE_SHORT}")
    lines.append("<i>👇 Нажмите «Выбрать курс»</i>")

    await safe_edit(callback.message, "\n".join(lines),
                    courses_page_kb(page, total))
    await callback.answer()


@router.callback_query(F.data.startswith("buy_menu:"))
async def buy_menu(callback: CallbackQuery):
    page = int(callback.data.split(":")[1])
    courses, _ = await get_courses(page)
    if not courses:
        await safe_edit(callback.message,
                        "🤷 <i>Курсы не найдены.</i>",
                        main_inline_kb())
        await callback.answer()
        return

    lines = ["<b>🛒 Выбор курса</b>",
             LINE,
             "<i>Нажмите на нужный курс ниже:</i>",
             ""]
    for i, (_, name, price) in enumerate(courses, start=1):
        lines.append(f"  <b>{i}.</b>  {name} — <code>{price}</code>")
    await safe_edit(callback.message, "\n".join(lines),
                    buy_menu_inline_kb(courses, page))
    await callback.answer()


@router.callback_query(F.data.startswith("pick_course:"))
async def pick_course_cb(callback: CallbackQuery):
    _, course_id_s, page_s = callback.data.split(":")
    course_id = int(course_id_s)
    page = int(page_s)
    course = await get_course(course_id)
    if not course:
        await callback.answer("Курс не найден.", show_alert=True)
        return
    if not course[4]:
        await callback.answer("⚠ У курса нет ссылки на оплату.", show_alert=True)
        return

    text = (
        f"<b>🎓 Карточка курса</b>\n"
        f"{LINE}\n\n"
        f"<b>Название:</b>\n  {course[1]}\n\n"
        f"<b>Стоимость:</b>\n  💵 <code>{course[2]}</code>\n\n"
        f"{LINE_SHORT}\n"
        f"<b>Как купить:</b>\n"
        f"  1️⃣ Нажмите <b>«💳 Перейти к оплате»</b>\n"
        f"  2️⃣ Оплатите удобным способом\n"
        f"  3️⃣ Вернитесь и нажмите <b>«📸 Отправить чек»</b>\n\n"
        f"<i>После подтверждения администратором вы получите видео.</i>"
    )
    await safe_edit(callback.message, text,
                    course_detail_inline_kb(course_id, page, course[4]))
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
        f"К оплате: 💵 <code>{course[2]}</code>\n\n"
        f"{LINE_SHORT}\n"
        f"Пришлите <b>фотографию</b> или <b>скриншот</b> квитанции.\n"
        f"Администратор проверит оплату и подтвердит покупку.\n\n"
        f"<i>Чтобы отменить — нажмите «🏠 Главное меню».</i>"
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
        await message.answer("⚠ <i>Что-то пошло не так, попробуйте снова.</i>",
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
        f"  • Цена: <code>{course[2]}</code>\n\n"
        f"<b>🔗 Ссылка на оплату</b>\n"
        f"  {course[4]}\n\n"
        f"{LINE_SHORT}\n"
        f"<i>Подтвердите или отклоните заявку 👇</i>"
    )

    admin_kb = InlineKeyboardBuilder()
    admin_kb.button(text="✅ Подтвердить", callback_data=f"admin_confirm:{pending_id}")
    admin_kb.button(text="❌ Отклонить", callback_data=f"admin_reject:{pending_id}")
    admin_kb.adjust(2)

    try:
        await bot.send_photo(ADMIN_ID, receipt_file_id,
                             caption=caption,
                             reply_markup=admin_kb.as_markup(),
                             parse_mode="HTML")
    except Exception as e:
        await delete_pending(pending_id)
        await message.answer(f"⚠ <i>Не удалось отправить чек админу: {e}</i>",
                             reply_markup=user_reply_kb(message.from_user.id),
                             parse_mode="HTML")
        return

    await message.answer(
        f"<b>✅ Чек отправлен</b>\n"
        f"{LINE}\n"
        f"<i>Администратор проверит оплату. Обычно это занимает немного времени.</i>\n\n"
        f"<i>Как только покупка будет подтверждена, вы получите видео.</i>",
        reply_markup=user_reply_kb(message.from_user.id), parse_mode="HTML")


@router.message(PurchaseStates.waiting_receipt)
async def wrong_receipt(message: Message):
    await message.answer(
        "📸 <b>Нужна именно фотография</b>\n"
        f"{LINE}\n"
        "<i>Отправьте чек как фото или скриншот.\n"
        "Для отмены — «🏠 Главное меню».</i>",
        parse_mode="HTML")


# ============================================================
# ВАШИ КУРСЫ
# ============================================================
@router.callback_query(F.data == "my_courses")
async def my_courses(callback: CallbackQuery):
    purchases = await get_user_courses(callback.from_user.id)
    if not purchases:
        text = (
            f"<b>🎓 Мои курсы</b>\n"
            f"{LINE}\n\n"
            f"<i>У вас пока нет приобретённых курсов.</i>\n\n"
            f"<i>Загляните в каталог, чтобы выбрать свой первый курс 👇</i>"
        )
    else:
        lines = [f"<b>🎓 Мои курсы</b>",
                 LINE,
                 f"<i>Всего приобретено: <b>{len(purchases)}</b></i>",
                 ""]
        for idx, (name, price) in enumerate(purchases, start=1):
            lines.append(f"  ✅  <b>{name}</b>")
            lines.append(f"      💵 <code>{price}</code>")
            lines.append("")
        lines.append(LINE_SHORT)
        lines.append("<i>Видео по этим курсам вы получали при покупке.</i>")
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
            f"<b>🎉 Покупка подтверждена!</b>\n"
            f"{LINE}\n\n"
            f"Курс: <b>{course[1]}</b>\n"
            f"Оплата: 💵 <code>{course[2]}</code>\n\n"
            f"{LINE_SHORT}\n"
            f"<i>Отправляю доступ к видеоуроку...</i>",
            parse_mode="HTML")
        if course[3]:
            await callback.bot.send_video(user_id, course[3],
                                          caption=f"🎓 <b>{course[1]}</b>",
                                          parse_mode="HTML")
        else:
            await callback.bot.send_message(
                user_id, "⚠ <i>Видео курса пока не загружено.</i>",
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
                caption=(callback.message.caption or "") +
                        "\n\n<b>✅ ПОДТВЕРЖДЕНО</b>",
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
            f"<b>❌ Заявка отклонена</b>\n"
            f"{LINE}\n"
            f"<i>К сожалению, администратор не подтвердил оплату.\n"
            f"Если вы уверены, что оплата прошла — свяжитесь с поддержкой.</i>",
            reply_markup=user_reply_kb(user_id), parse_mode="HTML")
    except Exception:
        pass

    try:
        if callback.message.photo:
            await callback.message.edit_caption(
                caption=(callback.message.caption or "") +
                        "\n\n<b>❌ ОТКЛОНЕНО</b>",
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
async def show_admin_panel(target: Message, state: FSMContext):
    courses, _ = await get_courses(0, only_visible=False)
    kb = InlineKeyboardBuilder()
    for course_id, name, price in courses:
        kb.button(text=f"✏️ {name} — {price}",
                  callback_data=f"admin_edit:{course_id}")
    kb.button(text="➕ Добавить курс", callback_data="admin_add")
    kb.adjust(1)

    if courses:
        header = (
            f"<b>⚙️ Админ-панель</b>\n"
            f"{LINE}\n"
            f"<i>Всего курсов: <b>{len(courses)}</b></i>\n\n"
            f"<b>Управление:</b>"
        )
    else:
        header = (
            f"<b>⚙️ Админ-панель</b>\n"
            f"{LINE}\n"
            f"<i>Курсов пока нет.</i>\n\n"
            f"<b>Начните с добавления первого курса:</b>"
        )
    await target.answer(header, reply_markup=kb.as_markup(), parse_mode="HTML")
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
    course = await get_course(course_id)
    await state.update_data(course_id=course_id)
    await callback.message.edit_text(
        f"<b>✏️ Редактирование курса</b>\n"
        f"{LINE}\n"
        f"<b>Текущее название:</b> {course[1]}\n"
        f"<b>Текущая цена:</b> <code>{course[2]}</code>\n\n"
        f"{LINE_SHORT}\n"
        f"<i>Шаг 1 из 4 — введите новое название:</i>",
        parse_mode="HTML")
    await state.set_state(AdminStates.enter_name)
    await callback.answer()


@router.message(AdminStates.enter_name)
async def admin_enter_name(message: Message, state: FSMContext):
    await state.update_data(new_name=(message.text or "").strip())
    await message.answer(
        f"<b>💰 Шаг 2 из 4</b>\n{LINE}\n"
        f"<i>Введите новую цену курса:</i>",
        parse_mode="HTML")
    await state.set_state(AdminStates.enter_price)


@router.message(AdminStates.enter_price)
async def admin_enter_price(message: Message, state: FSMContext):
    await state.update_data(new_price=(message.text or "").strip())
    await message.answer(
        f"<b>🔗 Шаг 3 из 4</b>\n{LINE}\n"
        f"<i>Отправьте новую ссылку на оплату (http:// или https://)\n"
        f"или /skip, чтобы оставить текущую.</i>",
        parse_mode="HTML")
    await state.set_state(AdminStates.enter_link)


@router.message(AdminStates.enter_link, Command("skip"))
async def admin_skip_link(message: Message, state: FSMContext):
    await state.update_data(new_link=None, change_link=False)
    await message.answer(
        f"<b>🎬 Шаг 4 из 4</b>\n{LINE}\n"
        f"<i>Отправьте новое видео курса или /skip, чтобы оставить старое.</i>",
        parse_mode="HTML")
    await state.set_state(AdminStates.enter_video)


@router.message(AdminStates.enter_link)
async def admin_enter_link(message: Message, state: FSMContext):
    link = (message.text or "").strip()
    if not link.startswith(("http://", "https://")):
        await message.answer("⚠ <i>Ссылка должна начинаться с http:// или https://</i>",
                             parse_mode="HTML")
        return
    await state.update_data(new_link=link, change_link=True)
    await message.answer(
        f"<b>🎬 Шаг 4 из 4</b>\n{LINE}\n"
        f"<i>Отправьте новое видео курса или /skip, чтобы оставить старое.</i>",
        parse_mode="HTML")
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
    await message.answer(
        f"<b>✅ Курс обновлён</b>\n{LINE}\n"
        f"<i>Видео не изменялось.</i>",
        reply_markup=main_inline_kb(), parse_mode="HTML")


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
    await message.answer(
        f"<b>✅ Курс обновлён</b>\n{LINE}\n"
        f"<i>Видео успешно заменено.</i>",
        reply_markup=main_inline_kb(), parse_mode="HTML")


@router.message(AdminStates.enter_video)
async def admin_enter_video_invalid(message: Message):
    await message.answer("⚠ <i>Отправьте видео или /skip.</i>",
                         parse_mode="HTML")


# ---------- ДОБАВЛЕНИЕ ----------
@router.callback_query(F.data == "admin_add")
async def admin_add_start(callback: CallbackQuery, state: FSMContext):
    if callback.from_user.id != ADMIN_ID:
        await callback.answer("Нет доступа", show_alert=True)
        return
    await callback.message.edit_text(
        f"<b>➕ Новый курс</b>\n"
        f"{LINE}\n"
        f"<i>Шаг 1 из 4 — введите название курса:</i>",
        parse_mode="HTML")
    await state.set_state(AdminStates.create_name)
    await callback.answer()


@router.message(AdminStates.create_name)
async def admin_create_name(message: Message, state: FSMContext):
    await state.update_data(create_name=(message.text or "").strip())
    await message.answer(
        f"<b>💰 Шаг 2 из 4</b>\n{LINE}\n"
        f"<i>Введите цену курса (например, 4900 ₽):</i>",
        parse_mode="HTML")
    await state.set_state(AdminStates.create_price)


@router.message(AdminStates.create_price)
async def admin_create_price(message: Message, state: FSMContext):
    await state.update_data(create_price=(message.text or "").strip())
    await message.answer(
        f"<b>🔗 Шаг 3 из 4</b>\n{LINE}\n"
        f"<i>Отправьте ссылку на оплату (http:// или https://):</i>",
        parse_mode="HTML")
    await state.set_state(AdminStates.create_link)


@router.message(AdminStates.create_link)
async def admin_create_link(message: Message, state: FSMContext):
    link = (message.text or "").strip()
    if not link.startswith(("http://", "https://")):
        await message.answer("⚠ <i>Ссылка должна начинаться с http:// или https://</i>",
                             parse_mode="HTML")
        return
    await state.update_data(create_link=link)
    await message.answer(
        f"<b>🎬 Шаг 4 из 4</b>\n{LINE}\n"
        f"<i>Отправьте видео курса файлом:</i>",
        parse_mode="HTML")
    await state.set_state(AdminStates.create_video)


@router.message(AdminStates.create_video, F.video)
async def admin_create_video(message: Message, state: FSMContext):
    data = await state.get_data()
    await create_course(data["create_name"], data["create_price"],
                        data["create_link"], message.video.file_id)
    await state.clear()
    await message.answer(
        f"<b>🎉 Курс добавлен!</b>\n"
        f"{LINE}\n\n"
        f"<b>{data['create_name']}</b>\n"
        f"💵 <code>{data['create_price']}</code>\n\n"
        f"{LINE_SHORT}\n"
        f"<i>Курс уже доступен пользователям в каталоге.</i>",
        reply_markup=main_inline_kb(), parse_mode="HTML")


@router.message(AdminStates.create_video)
async def admin_create_video_invalid(message: Message):
    await message.answer("⚠ <i>Нужно отправить именно видео файлом.</i>",
                         parse_mode="HTML")


# ============================================================
# CATCH-ALL
# ============================================================
@router.message(StateFilter(None))
async def fallback_message(message: Message):
    await message.answer(
        f"<b>🤷 Я вас не понял</b>\n"
        f"{LINE}\n"
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
