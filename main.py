import asyncio
import aiosqlite
from aiogram import Bot, Dispatcher, F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import StatesGroup, State
from aiogram.types import (
    Message,
    CallbackQuery,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    ReplyKeyboardMarkup,
    KeyboardButton,
)
from aiogram.utils.keyboard import InlineKeyboardBuilder, ReplyKeyboardBuilder

TOKEN = os.getenv("TOKEN")

bot = Bot(token=TOKEN)

BOT_TOKEN = os.getenv("TOKEN")
ADMIN_ID = 6113518001  # замените на ваш Telegram ID

DB_PATH = "courses.db"
PAGE_SIZE = 5

router = Router()

# ============================================================
# БАЗА ДАННЫХ
# ============================================================
async def init_db():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS courses (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                price TEXT NOT NULL
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS purchases (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                course_id INTEGER NOT NULL,
                FOREIGN KEY (course_id) REFERENCES courses(id)
            )
        """)
        # демо-курсы для первого запуска
        cursor = await db.execute("SELECT COUNT(*) FROM courses")
        count = (await cursor.fetchone())[0]
        if count == 0:
            demo = [
                ("Python с нуля", "4 900 ₽"),
                ("Telegram-боты на aiogram 3", "6 900 ₽"),
                ("SQL для аналитиков", "3 900 ₽"),
                ("FastAPI + PostgreSQL", "7 900 ₽"),
                ("Docker и CI/CD", "5 900 ₽"),
                ("Алгоритмы и структуры данных", "8 900 ₽"),
            ]
            await db.executemany(
                "INSERT INTO courses (name, price) VALUES (?, ?)", demo
            )
        await db.commit()


async def get_courses(page: int = 0):
    offset = page * PAGE_SIZE
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            "SELECT id, name, price FROM courses ORDER BY id LIMIT ? OFFSET ?",
            (PAGE_SIZE, offset),
        )
        rows = await cursor.fetchall()
        cursor = await db.execute("SELECT COUNT(*) FROM courses")
        total = (await cursor.fetchone())[0]
    return rows, total


async def get_course_by_id(course_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            "SELECT id, name, price FROM courses WHERE id = ?", (course_id,)
        )
        return await cursor.fetchone()


async def get_user_courses(user_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            """
            SELECT c.name, c.price
            FROM purchases p
            JOIN courses c ON c.id = p.course_id
            WHERE p.user_id = ?
            ORDER BY p.id DESC
            """,
            (user_id,),
        )
        return await cursor.fetchall()


async def add_purchase(user_id: int, course_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        # защита от повторной покупки
        cursor = await db.execute(
            "SELECT id FROM purchases WHERE user_id = ? AND course_id = ?",
            (user_id, course_id),
        )
        if await cursor.fetchone() is None:
            await db.execute(
                "INSERT INTO purchases (user_id, course_id) VALUES (?, ?)",
                (user_id, course_id),
            )
            await db.commit()
            return True
        return False


async def update_course(course_id: int, name: str, price: str):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE courses SET name = ?, price = ? WHERE id = ?",
            (name, price, course_id),
        )
        await db.commit()


async def create_course(name: str, price: str):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT INTO courses (name, price) VALUES (?, ?)", (name, price)
        )
        await db.commit()


# ============================================================
# КЛАВИАТУРЫ
# ============================================================
def main_inline_kb() -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="📚 Курсы", callback_data="courses_page:0")
    kb.button(text="🎓 Ваши курсы", callback_data="my_courses")
    return kb.as_markup()


def courses_page_kb(page: int, total: int) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    total_pages = (total + PAGE_SIZE - 1) // PAGE_SIZE

    # кнопки листания, только если страниц больше одной
    if total_pages > 1:
        if page > 0:
            kb.button(text="⬅ Назад", callback_data=f"courses_page:{page - 1}")
        if page < total_pages - 1:
            kb.button(text="Вперёд ➡", callback_data=f"courses_page:{page + 1}")

    # кнопка «Купить курсы» под списком
    kb.row(InlineKeyboardButton(text="🛒 Купить курсы", callback_data="buy_menu"))

    # кнопка «Главное меню» (inline — чтобы можно было вернуться из любого места)
    kb.row(InlineKeyboardButton(text="🏠 Главное меню", callback_data="main_menu"))

    return kb.as_markup()


def buy_course_reply_kb(courses: list) -> ReplyKeyboardMarkup:
    """Обычные кнопки от 1 до 5 + Главное меню."""
    kb = ReplyKeyboardBuilder()
    for i, (course_id, name, price) in enumerate(courses, start=1):
        kb.button(text=f"{i}. {name} — {price}")
    kb.button(text="🏠 Главное меню")
    kb.adjust(1)
    return kb.as_markup(resize_keyboard=True, one_time_keyboard=True)


def course_detail_inline_kb(course_id: int) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="✅ Купить", callback_data=f"buy:{course_id}")
    kb.button(text="🔗 Ссылка на курс", callback_data=f"link:{course_id}")
    kb.button(text="⬅ Назад", callback_data="courses_page:0")
    return kb.as_markup()


def main_reply_kb() -> ReplyKeyboardMarkup:
    kb = ReplyKeyboardBuilder()
    kb.button(text="🏠 Главное меню")
    kb.adjust(1)
    return kb.as_markup(resize_keyboard=True, one_time_keyboard=True)


# ============================================================
# СОСТОЯНИЯ ДЛЯ АДМИН-ПАНЕЛИ
# ============================================================
class AdminStates(StatesGroup):
    choose_course = State()
    enter_name = State()
    enter_price = State()
    create_name = State()
    create_price = State()


# ============================================================
# ХЕНДЛЕРЫ ПОЛЬЗОВАТЕЛЯ
# ============================================================
@router.message(Command("start"))
async def cmd_start(message: Message):
    text = (
        "👋 Добро пожаловать!\n\n"
        "Этот бот создан для **покупки курсов**.\n"
        "Здесь вы можете посмотреть доступные курсы, "
        "приобрести их и получить доступ к уже купленным материалам.\n\n"
        "Выберите действие ниже 👇"
    )
    await message.answer(
        text, reply_markup=main_inline_kb(), parse_mode="Markdown"
    )


@router.callback_query(F.data == "main_menu")
async def main_menu_cb(callback: CallbackQuery):
    await callback.message.edit_text(
        "🏠 Главное меню\n\nВыберите действие:",
        reply_markup=main_inline_kb(),
    )
    await callback.answer()


@router.message(F.text == "🏠 Главное меню")
async def main_menu_reply(message: Message):
    await message.answer(
        "🏠 Главное меню", reply_markup=main_inline_kb()
    )


# ---------- СПИСОК КУРСОВ С ПАГИНАЦИЕЙ ----------
@router.callback_query(F.data.startswith("courses_page:"))
async def show_courses(callback: CallbackQuery):
    page = int(callback.data.split(":")[1])
    courses, total = await get_courses(page)

    if not courses:
        await callback.message.edit_text(
            "Пока нет доступных курсов.",
            reply_markup=main_inline_kb(),
        )
        await callback.answer()
        return

    text = "📚 Доступные курсы:\n\n"
    for course_id, name, price in courses:
        text += f"• **{name}** — {price}\n"

    await callback.message.edit_text(
        text,
        reply_markup=courses_page_kb(page, total),
        parse_mode="Markdown",
    )
    await callback.answer()


# ---------- КУПИТЬ КУРСЫ: ОБЫЧНЫЕ КНОПКИ 1–5 ----------
@router.callback_query(F.data == "buy_menu")
async def buy_menu(callback: CallbackQuery):
    courses, _ = await get_courses(0)  # показываем первые 5
    if not courses:
        await callback.message.edit_text(
            "Курсы ещё не добавлены.", reply_markup=main_inline_kb()
        )
        await callback.answer()
        return

    text = (
        "Какой курс вы хотите купить?\n"
        "Нажмите на соответствующую кнопку ниже 👇"
    )
    await callback.message.edit_text(text, reply_markup=main_inline_kb())  # убираем inline-список
    await callback.message.answer(
        "Выберите курс:",
        reply_markup=buy_course_reply_kb(courses),
    )
    await callback.answer()


# Обработка обычных кнопок «1. Курс — цена»
@router.message(F.text.regexp(r"^\d+\. .+ — .+$"))
async def pick_course_reply(message: Message):
    text = message.text
    # вытаскиваем название курса между «1. » и « — »
    name = text.split(". ", 1)[1].rsplit(" — ", 1)[0]
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            "SELECT id, name, price FROM courses WHERE name = ?", (name,)
        )
        course = await cursor.fetchone()

    if not course:
        await message.answer("Курс не найден. Попробуйте снова.")
        return

    course_id, name, price = course
    await message.answer(
        f"Вы выбрали:\n\n"
        f"**{name}**\n"
        f"Цена: **{price}**",
        reply_markup=course_detail_inline_kb(course_id),
        parse_mode="Markdown",
    )
    await message.answer(
        "Для возврата в меню используйте кнопку ниже.",
        reply_markup=main_reply_kb(),
    )


# ---------- ДЕТАЛИ КУРСА: КУПИТЬ / ССЫЛКА / НАЗАД ----------
@router.callback_query(F.data.startswith("buy:"))
async def buy_course(callback: CallbackQuery):
    course_id = int(callback.data.split(":")[1])
    course = await get_course_by_id(course_id)
    if not course:
        await callback.answer("Курс не найден.", show_alert=True)
        return

    _, name, price = course
    added = await add_purchase(callback.from_user.id, course_id)
    if added:
        await callback.message.edit_text(
            f"✅ Вы успешно приобрели курс **{name}** за **{price}**!\n\n"
            f"Курс добавлен в раздел «Ваши курсы».",
            reply_markup=main_inline_kb(),
            parse_mode="Markdown",
        )
    else:
        await callback.message.edit_text(
            f"У вас уже есть курс **{name}**.",
            reply_markup=main_inline_kb(),
            parse_mode="Markdown",
        )
    await callback.answer()


@router.callback_query(F.data.startswith("link:"))
async def course_link(callback: CallbackQuery):
    course_id = int(callback.data.split(":")[1])
    course = await get_course_by_id(course_id)
    if course:
        await callback.message.answer(
            f"🔗 Ссылка на курс «{course[1]}»: https://example.com/course/{course_id}"
        )
    await callback.answer()


# ---------- ВАШИ КУРСЫ ----------
@router.callback_query(F.data == "my_courses")
async def my_courses(callback: CallbackQuery):
    purchases = await get_user_courses(callback.from_user.id)
    if not purchases:
        text = "У вас пока нет приобретённых курсов."
    else:
        text = "🎓 Ваши курсы:\n\n"
        for name, price in purchases:
            text += f"• **{name}** — {price}\n"
    await callback.message.edit_text(
        text, reply_markup=main_inline_kb(), parse_mode="Markdown"
    )
    await callback.answer()


# ============================================================
# АДМИН-ПАНЕЛЬ (ИЗМЕНЕНИЕ КУРСОВ)
# ============================================================
@router.message(Command("admin"))
async def admin_start(message: Message, state: FSMContext):
    if message.from_user.id != ADMIN_ID:
        await message.answer("⛔ У вас нет доступа.")
        return

    courses, _ = await get_courses(0)
    if not courses:
        await message.answer("Нет курсов. Сначала добавьте курс командой /add.")
        return

    kb = InlineKeyboardBuilder()
    for course_id, name, price in courses:
        kb.button(text=f"{name} — {price}", callback_data=f"admin_edit:{course_id}")
    kb.button(text="➕ Добавить курс", callback_data="admin_add")
    kb.adjust(1)

    await message.answer(
        "🛠 Выберите курс для редактирования:",
        reply_markup=kb.as_markup(),
    )
    await state.set_state(AdminStates.choose_course)


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
    await state.update_data(new_name=message.text.strip())
    await message.answer("Введите новую **цену** курса:")
    await state.set_state(AdminStates.enter_price)


@router.message(AdminStates.enter_price)
async def admin_enter_price(message: Message, state: FSMContext):
    data = await state.get_data()
    await update_course(
        data["course_id"], data["new_name"], message.text.strip()
    )
    await state.clear()
    await message.answer(
        "✅ Курс обновлён.\n\n"
        "Проверить можно в разделе «Курсы».",
        reply_markup=main_inline_kb(),
    )


# ---------- ДОБАВЛЕНИЕ НОВОГО КУРСА ----------
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
    await state.update_data(create_name=message.text.strip())
    await message.answer("Введите **цену** нового курса:")
    await state.set_state(AdminStates.create_price)


@router.message(AdminStates.create_price)
async def admin_create_price(message: Message, state: FSMContext):
    data = await state.get_data()
    await create_course(data["create_name"], message.text.strip())
    await state.clear()
    await message.answer(
        "✅ Новый курс добавлен.", reply_markup=main_inline_kb()
    )


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
