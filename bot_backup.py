import os
import sqlite3
from datetime import datetime, date, timedelta
from zoneinfo import ZoneInfo

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

NY_TZ = ZoneInfo("America/New_York")

DB_PATH = os.getenv("BOT_DB_PATH", "bot.db")
PLANS_DIR = os.getenv("PLANS_DIR", "plans")

# MVP: 4 файла
PLAN_FILES = {
    ("F", "1500", "budget"): "F1500_budget.pdf",
    ("F", "1500", "premium"): "F1500_premium.pdf",
    ("M", "2200", "budget"): "M2200_budget.pdf",
    ("M", "2200", "premium"): "M2200_premium.pdf",
}

FLAG_KEYS = ["water", "food", "move", "sleep", "sauce"]


def db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    with db() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                tone TEXT DEFAULT NULL,      -- 'hard'/'soft'
                gender TEXT DEFAULT NULL,    -- 'F'/'M'
                goal TEXT DEFAULT NULL,      -- 'cut'/'balance'/'mass'
                deadline INTEGER DEFAULT NULL, -- 14/30/60/90
                budget TEXT DEFAULT NULL,    -- 'budget'/'premium'
                kcal TEXT DEFAULT NULL,
                created_at TEXT DEFAULT NULL
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS diary (
                user_id INTEGER NOT NULL,
                day TEXT NOT NULL,           -- YYYY-MM-DD
                water INTEGER DEFAULT 0,
                food INTEGER DEFAULT 0,
                move INTEGER DEFAULT 0,
                sleep INTEGER DEFAULT 0,
                sauce INTEGER DEFAULT 0,
                PRIMARY KEY (user_id, day)
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS streaks (
                user_id INTEGER PRIMARY KEY,
                streak INTEGER DEFAULT 0,
                last_success_day TEXT DEFAULT NULL
            )
        """)


def upsert_user(user_id: int):
    now = datetime.now(tz=NY_TZ).isoformat()
    with db() as conn:
        conn.execute(
            "INSERT INTO users(user_id, created_at) VALUES(?, ?) ON CONFLICT(user_id) DO NOTHING",
            (user_id, now),
        )
        conn.execute(
            "INSERT INTO streaks(user_id, streak, last_success_day) VALUES(?, 0, NULL) ON CONFLICT(user_id) DO NOTHING",
            (user_id,),
        )


def get_user(user_id: int):
    with db() as conn:
        return conn.execute("SELECT * FROM users WHERE user_id=?", (user_id,)).fetchone()


def set_user_field(user_id: int, field: str, value):
    if field not in {"tone", "gender", "goal", "deadline", "budget", "kcal"}:
        raise ValueError("bad field")
    with db() as conn:
        conn.execute(f"UPDATE users SET {field}=? WHERE user_id=?", (value, user_id))


def tone_text(user, soft: str, hard: str) -> str:
    if user and user["tone"] == "hard":
        return hard
    return soft


def kb(rows):
    return InlineKeyboardMarkup(rows)


def today_str() -> str:
    return datetime.now(tz=NY_TZ).date().isoformat()


def ensure_today_diary(user_id: int):
    with db() as conn:
        conn.execute(
            "INSERT INTO diary(user_id, day) VALUES(?, ?) ON CONFLICT(user_id, day) DO NOTHING",
            (user_id, today_str()),
        )


def get_today_diary(user_id: int) -> dict:
    ensure_today_diary(user_id)
    with db() as conn:
        row = conn.execute(
            "SELECT water, food, move, sleep, sauce FROM diary WHERE user_id=? AND day=?",
            (user_id, today_str()),
        ).fetchone()
        return dict(row)


def toggle_flag(user_id: int, flag: str):
    if flag not in FLAG_KEYS:
        return
    ensure_today_diary(user_id)
    with db() as conn:
        cur = conn.execute(
            f"SELECT {flag} FROM diary WHERE user_id=? AND day=?",
            (user_id, today_str()),
        ).fetchone()
        val = int(cur[0])
        new_val = 0 if val == 1 else 1
        conn.execute(
            f"UPDATE diary SET {flag}=? WHERE user_id=? AND day=?",
            (new_val, user_id, today_str()),
        )


def done_count(di: dict) -> int:
    return sum(int(di[k]) for k in FLAG_KEYS)


def get_streak(user_id: int) -> int:
    with db() as conn:
        row = conn.execute("SELECT streak FROM streaks WHERE user_id=?", (user_id,)).fetchone()
        return int(row["streak"]) if row else 0


def update_streak(user_id: int, di: dict):
    # Успешный день = >=4/5
    if done_count(di) < 4:
        return

    t = datetime.now(tz=NY_TZ).date()
    y = t - timedelta(days=1)

    with db() as conn:
        row = conn.execute(
            "SELECT streak, last_success_day FROM streaks WHERE user_id=?",
            (user_id,),
        ).fetchone()

        streak = int(row["streak"])
        last = row["last_success_day"]

        if last == t.isoformat():
            return  # уже засчитано сегодня

        if last == y.isoformat():
            streak += 1
        else:
            streak = 1

        conn.execute(
            "UPDATE streaks SET streak=?, last_success_day=? WHERE user_id=?",
            (streak, t.isoformat(), user_id),
        )


def diary_keyboard(di: dict):
    def line(key, emoji, text):
        mark = "✅" if di[key] == 1 else "⬜️"
        return f"{mark} {emoji} {text}"

    return kb([
        [InlineKeyboardButton(line("water", "💧", "Вода"), callback_data="d:water")],
        [InlineKeyboardButton(line("food", "🍽", "Еда по плану"), callback_data="d:food")],
        [InlineKeyboardButton(line("move", "🚶", "Движение"), callback_data="d:move")],
        [InlineKeyboardButton(line("sleep", "😴", "Сон"), callback_data="d:sleep")],
        [InlineKeyboardButton(line("sauce", "🥣", "Соус дня"), callback_data="d:sauce")],
        [InlineKeyboardButton("📤 Поделиться итогом", callback_data="menu:share")],
        [InlineKeyboardButton("🧠 Срыв/тяжело", callback_data="menu:slip")],
        [InlineKeyboardButton("⬅️ Меню", callback_data="menu:home")],
    ])


def home_keyboard():
    return kb([
        [InlineKeyboardButton("📒 ДНЕВНИК", callback_data="menu:diary")],
        [InlineKeyboardButton("📤 ПОДЕЛИТЬСЯ", callback_data="menu:share")],
        [InlineKeyboardButton("🧠 СРЫВ/ТЯЖЕЛО", callback_data="menu:slip")],
    ])


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    upsert_user(user_id)
    user = get_user(user_id)

    text = tone_text(
        user,
        "Привет. Я дам план питания на неделю + соусы + дневник отметок.\n\nВыбери тон:",
        "Привет. План на неделю + соусы + дневник.\n\nВыбери тон:",
    )
    await update.message.reply_text(
        text,
        reply_markup=kb([
            [InlineKeyboardButton("ЖЁСТКО (с матом)", callback_data="set:tone:hard")],
            [InlineKeyboardButton("Нормально (без мата)", callback_data="set:tone:soft")],
        ]),
    )


async def ask_gender(chat_id: int, context: ContextTypes.DEFAULT_TYPE, user_id: int):
    user = get_user(user_id)
    await context.bot.send_message(
        chat_id=chat_id,
        text=tone_text(user, "Выбери пол:", "Пол:"),
        reply_markup=kb([
            [InlineKeyboardButton("ЖЕН", callback_data="set:gender:F")],
            [InlineKeyboardButton("МУЖ", callback_data="set:gender:M")],
        ]),
    )


async def ask_goal(chat_id: int, context: ContextTypes.DEFAULT_TYPE, user_id: int):
    user = get_user(user_id)
    await context.bot.send_message(
        chat_id=chat_id,
        text=tone_text(user, "Цель:", "Цель:"),
        reply_markup=kb([
            [InlineKeyboardButton("СУШКА", callback_data="set:goal:cut")],
            [InlineKeyboardButton("БАЛАНС", callback_data="set:goal:balance")],
            [InlineKeyboardButton("МАССА", callback_data="set:goal:mass")],
        ]),
    )


async def ask_deadline(chat_id: int, context: ContextTypes.DEFAULT_TYPE, user_id: int):
    user = get_user(user_id)
    await context.bot.send_message(
        chat_id=chat_id,
        text=tone_text(
            user,
            "За сколько хочешь результат? (14/30/60/90)",
            "Срок. Без фантазий: 14/30/60/90",
        ),
        reply_markup=kb([
            [InlineKeyboardButton("14", callback_data="set:deadline:14"),
             InlineKeyboardButton("30", callback_data="set:deadline:30")],
            [InlineKeyboardButton("60", callback_data="set:deadline:60"),
             InlineKeyboardButton("90", callback_data="set:deadline:90")],
        ]),
    )


async def ask_budget(chat_id: int, context: ContextTypes.DEFAULT_TYPE, user_id: int):
    user = get_user(user_id)
    await context.bot.send_message(
        chat_id=chat_id,
        text=tone_text(user, "Бюджет:", "Подешевле или побогаче?"),
        reply_markup=kb([
            [InlineKeyboardButton("ПОДЕШЕВЛЕ", callback_data="set:budget:budget")],
            [InlineKeyboardButton("ПОБОГАЧЕ", callback_data="set:budget:premium")],
        ]),
    )


async def send_plan(chat_id: int, context: ContextTypes.DEFAULT_TYPE, user_id: int):
    user = get_user(user_id)
    gender = user["gender"]
    budget = user["budget"]

    # MVP: авто-ккал
    kcal = "1500" if gender == "F" else "2200"
    set_user_field(user_id, "kcal", kcal)

    filename = PLAN_FILES.get((gender, kcal, budget))
    if not filename:
        await context.bot.send_message(chat_id=chat_id, text="Не нашёл файл плана. Проверь mapping.")
        return

    path = os.path.join(PLANS_DIR, filename)
    if not os.path.exists(path):
        await context.bot.send_message(
            chat_id=chat_id,
            text=f"Файл {filename} не найден в папке {PLANS_DIR}/. Проверь имена.",
        )
        return

    user = get_user(user_id)
    caption = tone_text(
        user,
        "Готово. Держи план на неделю.\nЖми «📒 ДНЕВНИК» и отмечай галочки.",
        "Готово. Держи план.\nДальше без сказок: дневник, галочки, стрик.",
    )

    await context.bot.send_document(
        chat_id=chat_id,
        document=open(path, "rb"),
        caption=caption,
        reply_markup=home_keyboard(),
    )


async def show_home(chat_id: int, context: ContextTypes.DEFAULT_TYPE, user_id: int):
    user = get_user(user_id)
    await context.bot.send_message(
        chat_id=chat_id,
        text=tone_text(user, "Меню:", "Меню:"),
        reply_markup=home_keyboard(),
    )


async def show_diary(chat_id: int, context: ContextTypes.DEFAULT_TYPE, user_id: int):
    user = get_user(user_id)
    di = get_today_diary(user_id)
    update_streak(user_id, di)
    streak = get_streak(user_id)
    done = done_count(di)

    text = tone_text(
        user,
        f"Дневник на сегодня: {done}/5 | Стрик: {streak}\nОтметь выполненное:",
        f"Сегодня: {done}/5 | Стрик: {streak}\nЖми галочки:",
    )
    await context.bot.send_message(chat_id=chat_id, text=text, reply_markup=diary_keyboard(di))


async def on_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    user_id = q.from_user.id
    upsert_user(user_id)

    data = q.data
    user = get_user(user_id)

    if data.startswith("set:"):
        _, field, value = data.split(":", 2)
        if field == "deadline":
            value = int(value)
        set_user_field(user_id, field, value)

        # продолжение сценария
        if field == "tone":
            await q.edit_message_text(tone_text(get_user(user_id), "Принял. Дальше пол:", "Ок. Пол:"))
            await ask_gender(q.message.chat_id, context, user_id)
            return

        if field == "gender":
            await q.edit_message_text(tone_text(get_user(user_id), "Принял. Дальше цель:", "Ок. Цель:"))
            await ask_goal(q.message.chat_id, context, user_id)
            return

        if field == "goal":
            await ask_deadline(q.message.chat_id, context, user_id)
            return

        if field == "deadline":
            await ask_budget(q.message.chat_id, context, user_id)
            return

        if field == "budget":
            await send_plan(q.message.chat_id, context, user_id)
            return

    if data.startswith("d:"):
        flag = data.split(":", 1)[1]
        toggle_flag(user_id, flag)
        di = get_today_diary(user_id)
        update_streak(user_id, di)
        streak = get_streak(user_id)
        done = done_count(di)

        user = get_user(user_id)
        text = tone_text(
            user,
            f"Дневник на сегодня: {done}/5 | Стрик: {streak}\nОтметь выполненное:",
            f"Сегодня: {done}/5 | Стрик: {streak}\nЖми галочки:",
        )
        await q.edit_message_text(text=text, reply_markup=diary_keyboard(di))
        return

    if data == "menu:home":
        user = get_user(user_id)
        await q.edit_message_text(tone_text(user, "Меню:", "Меню:"), reply_markup=home_keyboard())
        return

    if data == "menu:diary":
        di = get_today_diary(user_id)
        update_streak(user_id, di)
        streak = get_streak(user_id)
        done = done_count(di)
        user = get_user(user_id)
        text = tone_text(
            user,
            f"Дневник на сегодня: {done}/5 | Стрик: {streak}\nОтметь выполненное:",
            f"Сегодня: {done}/5 | Стрик: {streak}\nЖми галочки:",
        )
        await q.edit_message_text(text=text, reply_markup=diary_keyboard(di))
        return

    if data == "menu:share":
        di = get_today_diary(user_id)
        done = done_count(di)
        streak = get_streak(user_id)
        deadline = (get_user(user_id)["deadline"] or 30)

        # share всегда без мата
        if done == 5:
            summary = "Чисто и красиво."
        elif done == 4:
            summary = "Почти идеально. Завтра добью."
        else:
            summary = "Вернулся в режим. Без истерик."

        share_text = (
            f"День 1/{deadline}. Выполнено {done}/5. Стрик: {streak}.\n"
            f"{summary}\n"
            "#дисциплина #планпитания"
        )
        await q.edit_message_text(
            "Скопируй и делись (лучше в сторис без мата):\n\n" + share_text,
            reply_markup=kb([[InlineKeyboardButton("⬅️ Назад", callback_data="menu:diary")]]),
        )
        return

    if data == "menu:slip":
        user = get_user(user_id)
        soft = (
            "Срыв не отменяет план.\n\nПротокол на 24 часа:\n"
            "1) следующий приём пищи нормальный\n"
            "2) вода\n"
            "3) прогулка 20 минут\n"
            "4) без наказаний голодом\n\n"
            "Жми «⬅️ Назад» и отмечай дневник."
        )
        hard = (
            "Проебался? Бывает.\n\nПротокол на 24 часа:\n"
            "1) следующий приём пищи нормальный\n"
            "2) вода\n"
            "3) 20 минут ходьбы\n"
            "4) без цирка и голодовок\n\n"
            "Жми «⬅️ Назад» и отмечай дневник."
        )
        await q.edit_message_text(
            tone_text(user, soft, hard),
            reply_markup=kb([[InlineKeyboardButton("⬅️ Назад", callback_data="menu:diary")]]),
        )
        return


def is_plan_trigger(txt: str) -> bool:
    t = (txt or "").strip().lower()
    return t in {"план", "таблица"}


async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message:
        return
    if not is_plan_trigger(update.message.text):
        return
    user_id = update.effective_user.id
    upsert_user(user_id)
    await ask_gender(update.message.chat_id, context, user_id)


async def cmd_plan(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await on_text(update, context)


async def cmd_day(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    upsert_user(user_id)
    await show_diary(update.message.chat_id, context, user_id)


async def morning_ping(context: ContextTypes.DEFAULT_TYPE):
    with db() as conn:
        users = conn.execute("SELECT user_id, tone FROM users").fetchall()
    for r in users:
        uid = int(r["user_id"])
        user = get_user(uid)
        msg = tone_text(
            user,
            "Доброе утро. 4–5 галочек и ты в плюсе. Жми /day",
            "Утро. Без сказок. Галочки и режим. Жми /day",
        )
        try:
            await context.bot.send_message(chat_id=uid, text=msg)
        except Exception:
            pass


async def lunch_ping(context: ContextTypes.DEFAULT_TYPE):
    with db() as conn:
        users = conn.execute("SELECT user_id, tone FROM users").fetchall()
    for r in users:
        uid = int(r["user_id"])
        user = get_user(uid)
        msg = tone_text(
            user,
            "Обед: белок + овощи + соус. Не усложняй. Жми /day",
            "Обед: белок, овощи, соус. Не фантазируй. Жми /day",
        )
        try:
            await context.bot.send_message(chat_id=uid, text=msg)
        except Exception:
            pass


async def evening_ping(context: ContextTypes.DEFAULT_TYPE):
    with db() as conn:
        users = conn.execute("SELECT user_id, tone FROM users").fetchall()
    for r in users:
        uid = int(r["user_id"])
        user = get_user(uid)
        msg = tone_text(
            user,
            "Вечерний чек: отметь день. Главное повторяемость. Жми /day",
            "Вечер. Отмечай. Проебал? Признай и вернись. Жми /day",
        )
        try:
            await context.bot.send_message(chat_id=uid, text=msg)
        except Exception:
            pass


def schedule_jobs(app: Application):
    jq = app.job_queue
    # 06:00, 12:30, 20:30 по Нью-Йорку
    jq.run_daily(morning_ping, time=datetime.strptime("06:00", "%H:%M").time(), tzinfo=NY_TZ, name="morning")
    jq.run_daily(lunch_ping, time=datetime.strptime("12:30", "%H:%M").time(), tzinfo=NY_TZ, name="lunch")
    jq.run_daily(evening_ping, time=datetime.strptime("20:30", "%H:%M").time(), tzinfo=NY_TZ, name="evening")


def main():
    init_db()
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    if not token:
        raise RuntimeError("Set TELEGRAM_BOT_TOKEN environment variable")

    app = Application.builder().token(token).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("plan", cmd_plan))
    app.add_handler(CommandHandler("day", cmd_day))
    app.add_handler(CallbackQueryHandler(on_callback))
    app.add_handler(MessageHandler(filters.TEXT & (~filters.COMMAND), on_text))

    #schedule_jobs(app)
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
