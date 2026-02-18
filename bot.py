import asyncio
import json
import os
from dataclasses import dataclass, asdict
from datetime import datetime, date, timedelta, time as dtime
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from aiogram import Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.filters import CommandStart
from aiogram.types import (
    Message,
    CallbackQuery,
    ReplyKeyboardMarkup,
    KeyboardButton,
    InlineKeyboardMarkup,
)
from aiogram.utils.keyboard import InlineKeyboardBuilder
from dotenv import load_dotenv

# =========================
# CONFIG
# =========================
load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()


TIME_FILE = Path("time.txt")
DUA_SAHARLIK_FILE = Path("molitva-saharlik.txt")
DUA_IFTAR_FILE = Path("molitva-iftar.txt")
USERS_FILE = Path("users.json")

# По умолчанию: Asia/Tashkent (+05:00)
# Для простоты работаем в локальном времени сервера. Лучше запускать на сервере с timezone Asia/Tashkent.
DEFAULT_TZ_NAME = "Asia/Tashkent"

# Пресеты уведомлений (минуты ДО ифтара/сахарлика)
PRESETS = {
    "full": {"iftar": [30, 15, 10, 5, 1], "saharlik": [5]},
    "short": {"iftar": [10, 5, 1], "saharlik": [5]},
    "minimal": {"iftar": [5, 1], "saharlik": [5]},
}

DEFAULT_PRESET = "full"

# Ежедневные сообщения
NIGHT_MESSAGE_AT = "22:00"   # "Завтра ..."
MORNING_MESSAGE_AT = "08:30" # "Сегодня ..."

# Как часто проверяем время (сек)
TICK_SECONDS = 20

# =========================
# DATA MODEL
# =========================
@dataclass
class UserSettings:
    enabled: bool = True
    preset: str = DEFAULT_PRESET
    morning_time: str = MORNING_MESSAGE_AT
    night_time: str = NIGHT_MESSAGE_AT
    # Чтобы не слать одно и то же много раз
    sent_keys: Dict[str, List[str]] = None  # {"YYYY-MM-DD": ["iftar-30", "iftar-now", ...]}

    def __post_init__(self):
        if self.sent_keys is None:
            self.sent_keys = {}


# =========================
# STORAGE
# =========================
def load_users() -> Dict[str, UserSettings]:
    if not USERS_FILE.exists():
        return {}
    try:
        data = json.loads(USERS_FILE.read_text(encoding="utf-8"))
        users: Dict[str, UserSettings] = {}
        for uid, udata in data.items():
            users[uid] = UserSettings(
                enabled=udata.get("enabled", True),
                preset=udata.get("preset", DEFAULT_PRESET),
                morning_time=udata.get("morning_time", MORNING_MESSAGE_AT),
                night_time=udata.get("night_time", NIGHT_MESSAGE_AT),
                sent_keys=udata.get("sent_keys", {}),
            )
        return users
    except Exception:
        return {}

def save_users(users: Dict[str, UserSettings]) -> None:
    data = {uid: asdict(uset) for uid, uset in users.items()}
    USERS_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


# =========================
# TIME TABLE
# =========================
def parse_time_hhmm(s: str) -> dtime:
    hh, mm = s.strip().split(":")
    return dtime(int(hh), int(mm))

def load_schedule() -> Dict[date, Tuple[dtime, dtime]]:
    """
    Load all rows from time.txt.
    Format: YYYY-MM-DD;HH:MM;HH:MM
    """
    schedule: Dict[date, Tuple[dtime, dtime]] = {}
    if not TIME_FILE.exists():
        return schedule

    for line in TIME_FILE.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = [p.strip() for p in line.split(";")]
        if len(parts) < 3:
            continue
        try:
            row_day = date.fromisoformat(parts[0])
            sah = parse_time_hhmm(parts[1])
            ift = parse_time_hhmm(parts[2])
        except Exception:
            continue
        schedule[row_day] = (sah, ift)

    return schedule

def load_day_times(target_date: date) -> Optional[Tuple[dtime, dtime]]:
    """
    Return exact (saharlik_time, iftar_time) for given date from time.txt
    Format: YYYY-MM-DD;HH:MM;HH:MM
    """
    return load_schedule().get(target_date)

def find_nearest_day_times(target_date: date) -> Optional[Tuple[date, Tuple[dtime, dtime]]]:
    schedule = load_schedule()
    if not schedule:
        return None

    nearest_day = min(
        schedule.keys(),
        key=lambda d: (abs((d - target_date).days), 0 if d > target_date else 1, d.toordinal()),
    )
    return nearest_day, schedule[nearest_day]

def resolve_day_times(target_date: date) -> Optional[Tuple[date, dtime, dtime, bool]]:
    exact = load_day_times(target_date)
    if exact:
        sah, ift = exact
        return target_date, sah, ift, True

    nearest = find_nearest_day_times(target_date)
    if not nearest:
        return None
    nearest_day, (sah, ift) = nearest
    return nearest_day, sah, ift, False

def read_dua(path: Path) -> str:
    if not path.exists():
        return "⚠️ Файл молитвы не найден."
    txt = path.read_text(encoding="utf-8").strip()
    return txt if txt else "⚠️ Файл молитвы пуст."


# =========================
# UI: BUTTONS
# =========================
def main_menu_kb() -> ReplyKeyboardMarkup:
    # Обычная клавиатура (внизу)
    kb = [
        [KeyboardButton(text="📅 Сегодня")],
        [KeyboardButton(text="🌙 Сахарлик"), KeyboardButton(text="🌅 Ифтар")],
        [KeyboardButton(text="🤲 Дуа"), KeyboardButton(text="⚙️ Настройки")],
    ]
    return ReplyKeyboardMarkup(keyboard=kb, resize_keyboard=True)

def settings_inline_kb(user: UserSettings) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()

    status = "✅ Включено" if user.enabled else "⛔ Выключено"
    b.button(text=f"Напоминания: {status}", callback_data="set:toggle")

    b.button(text="Preset: FULL (30/15/10/5/1)", callback_data="set:preset:full")
    b.button(text="Preset: SHORT (10/5/1)", callback_data="set:preset:short")
    b.button(text="Preset: MINIMAL (5/1)", callback_data="set:preset:minimal")

    b.button(text=f"Утро: {user.morning_time}", callback_data="set:morning:hint")
    b.button(text=f"Ночь: {user.night_time}", callback_data="set:night:hint")

    b.button(text="⬅️ Назад", callback_data="set:back")
    b.adjust(1)
    return b.as_markup()

def dua_inline_kb() -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text="🌙 Дуа для Сахарлик", callback_data="dua:saharlik")
    b.button(text="🌅 Дуа для Ифтар", callback_data="dua:iftar")
    b.button(text="⬅️ Назад", callback_data="dua:back")
    b.adjust(1)
    return b.as_markup()


# =========================
# BOT LOGIC
# =========================
users = load_users()

def get_user(uid: str) -> UserSettings:
    if uid not in users:
        users[uid] = UserSettings()
        save_users(users)
    # защита от неверного пресета
    if users[uid].preset not in PRESETS:
        users[uid].preset = DEFAULT_PRESET
    return users[uid]

def mark_sent(uid: str, day: date, key: str):
    u = get_user(uid)
    ds = day.isoformat()
    u.sent_keys.setdefault(ds, [])
    if key not in u.sent_keys[ds]:
        u.sent_keys[ds].append(key)
        # чистим старые дни, чтобы файл не рос бесконечно
        # оставим последние 10 дней
        all_days = sorted(u.sent_keys.keys())
        if len(all_days) > 10:
            for old in all_days[:-10]:
                u.sent_keys.pop(old, None)
    save_users(users)

def was_sent(uid: str, day: date, key: str) -> bool:
    u = get_user(uid)
    ds = day.isoformat()
    return key in u.sent_keys.get(ds, [])

def now_local() -> datetime:
    # Важно: чтобы работало “как в Ташкенте”, сервер должен быть на Asia/Tashkent.
    # Иначе — будет смещение. (Можно позже добавить tz-aware)
    return datetime.now()

def combine(day: date, t: dtime) -> datetime:
    return datetime.combine(day, t)

def format_day_info(day: date) -> str:
    resolved = resolve_day_times(day)
    if not resolved:
        return "⚠️ На эту дату время не найдено в time.txt"

    actual_day, sah, ift, is_exact = resolved
    if not is_exact:
        return (
            f"⚠️ На дату <b>{day.isoformat()}</b> записи нет в time.txt\n"
            f"📌 Ближайшая дата: <b>{actual_day.isoformat()}</b>\n"
            f"🌙 Сахарлик: <b>{sah.strftime('%H:%M')}</b>\n"
            f"🌅 Ифтар: <b>{ift.strftime('%H:%M')}</b>"
        )
    return f"🌙 Сахарлик: <b>{sah.strftime('%H:%M')}</b>\n🌅 Ифтар: <b>{ift.strftime('%H:%M')}</b>"

def nice_phrase() -> str:
    phrases = [
        "Пусть Аллах примет твой пост 🤍",
        "Ещё чуть-чуть — ты молодец 🌙",
        "Пусть в этом дне будет баракат ✨",
        "Пусть твоя дуа будет принята 🤲",
    ]
    # без random импортов можно так:
    idx = int(datetime.now().strftime("%S")) % len(phrases)
    return phrases[idx]


# =========================
# HANDLERS
# =========================
dp = Dispatcher()

@dp.message(CommandStart())
async def cmd_start(msg: Message):
    uid = str(msg.from_user.id)
    get_user(uid)
    await msg.answer(
        "Ассаляму алейкум! 🌙\n"
        "Я буду напоминать про сахарлик и ифтар.\n\n"
        "Пользуйся кнопками 👇",
        reply_markup=main_menu_kb()
    )

@dp.message(F.text == "📅 Сегодня")
async def h_today(msg: Message):
    today = now_local().date()
    await msg.answer("📅 <b>Сегодня:</b>\n" + format_day_info(today), reply_markup=main_menu_kb())

@dp.message(F.text == "🌙 Сахарлик")
async def h_saharlik(msg: Message):
    today = now_local().date()
    resolved = resolve_day_times(today)
    if not resolved:
        await msg.answer("⚠️ Не нашёл время на сегодня в time.txt", reply_markup=main_menu_kb())
        return
    actual_day, sah, _, is_exact = resolved
    if is_exact:
        await msg.answer(f"🌙 Сегодня сахарлик: <b>{sah.strftime('%H:%M')}</b>", reply_markup=main_menu_kb())
        return
    await msg.answer(
        "⚠️ На сегодня записи нет в time.txt\n"
        f"🌙 Ближайшая дата <b>{actual_day.isoformat()}</b>: <b>{sah.strftime('%H:%M')}</b>",
        reply_markup=main_menu_kb(),
    )

@dp.message(F.text == "🌅 Ифтар")
async def h_iftar(msg: Message):
    today = now_local().date()
    resolved = resolve_day_times(today)
    if not resolved:
        await msg.answer("⚠️ Не нашёл время на сегодня в time.txt", reply_markup=main_menu_kb())
        return
    actual_day, _, ift, is_exact = resolved
    if is_exact:
        await msg.answer(f"🌅 Сегодня ифтар: <b>{ift.strftime('%H:%M')}</b>", reply_markup=main_menu_kb())
        return
    await msg.answer(
        "⚠️ На сегодня записи нет в time.txt\n"
        f"🌅 Ближайшая дата <b>{actual_day.isoformat()}</b>: <b>{ift.strftime('%H:%M')}</b>",
        reply_markup=main_menu_kb(),
    )

@dp.message(F.text == "🤲 Дуа")
async def h_dua(msg: Message):
    await msg.answer("Выбери дуа:", reply_markup=dua_inline_kb())

@dp.callback_query(F.data.startswith("dua:"))
async def cb_dua(call: CallbackQuery):
    action = call.data.split(":")[1]
    if action == "saharlik":
        await call.message.edit_text("🌙 <b>Дуа для сахарлик:</b>\n\n" + read_dua(DUA_SAHARLIK_FILE),
                                    reply_markup=dua_inline_kb())
    elif action == "iftar":
        await call.message.edit_text("🌅 <b>Дуа для ифтара:</b>\n\n" + read_dua(DUA_IFTAR_FILE),
                                    reply_markup=dua_inline_kb())
    elif action == "back":
        await call.message.edit_text("Ок 👇", reply_markup=None)
        await call.message.answer("Меню:", reply_markup=main_menu_kb())
    await call.answer()

@dp.message(F.text == "⚙️ Настройки")
async def h_settings(msg: Message):
    uid = str(msg.from_user.id)
    u = get_user(uid)
    txt = (
        "⚙️ <b>Настройки</b>\n"
        f"Напоминания: <b>{'включены' if u.enabled else 'выключены'}</b>\n"
        f"Preset: <b>{u.preset}</b>\n"
        f"Утреннее сообщение: <b>{u.morning_time}</b>\n"
        f"Ночное сообщение: <b>{u.night_time}</b>\n\n"
        "ℹ️ Чтобы поменять утро/ночь — напиши мне так:\n"
        "<code>утро 08:45</code> или <code>ночь 22:15</code>"
    )
    await msg.answer(txt, reply_markup=settings_inline_kb(u))

@dp.callback_query(F.data.startswith("set:"))
async def cb_settings(call: CallbackQuery):
    uid = str(call.from_user.id)
    u = get_user(uid)
    parts = call.data.split(":")
    if parts[1] == "toggle":
        u.enabled = not u.enabled
        save_users(users)
        await call.message.edit_reply_markup(reply_markup=settings_inline_kb(u))
        await call.answer("Готово!")
        return

    if parts[1] == "preset" and len(parts) == 3:
        preset = parts[2]
        if preset in PRESETS:
            u.preset = preset
            save_users(users)
            await call.message.edit_reply_markup(reply_markup=settings_inline_kb(u))
            await call.answer("Preset изменён!")
        else:
            await call.answer("Неизвестный preset")
        return

    if parts[1] in ("morning", "night"):
        await call.answer("Напиши сообщением: 'утро 08:30' или 'ночь 22:00'")
        return

    if parts[1] == "back":
        await call.message.edit_text("Меню:", reply_markup=None)
        await call.message.answer("👇", reply_markup=main_menu_kb())
        await call.answer()
        return

    await call.answer()

@dp.message(F.text.regexp(r"^(утро|ночь)\s+\d{2}:\d{2}$"))
async def h_set_times_text(msg: Message):
    uid = str(msg.from_user.id)
    u = get_user(uid)
    kind, hhmm = msg.text.split()
    # базовая валидация
    try:
        parse_time_hhmm(hhmm)
    except Exception:
        await msg.answer("⚠️ Формат времени должен быть HH:MM, например 08:30")
        return

    if kind == "утро":
        u.morning_time = hhmm
        save_users(users)
        await msg.answer(f"✅ Утреннее сообщение теперь в <b>{hhmm}</b>", reply_markup=main_menu_kb())
    else:
        u.night_time = hhmm
        save_users(users)
        await msg.answer(f"✅ Ночное сообщение теперь в <b>{hhmm}</b>", reply_markup=main_menu_kb())

@dp.message()
async def fallback(msg: Message):
    """
    Умные ответы на текст: "во сколько ифтар", "сахарлик сегодня" и т.п.
    """
    text = (msg.text or "").lower()
    today = now_local().date()
    tomorrow = today + timedelta(days=1)

    if "ифтар" in text:
        target = tomorrow if "завтра" in text else today
        resolved = resolve_day_times(target)
        if not resolved:
            await msg.answer("⚠️ Не нашёл время в time.txt")
            return
        actual_day, _, ift, is_exact = resolved
        label = "Завтра" if target == tomorrow else "Сегодня"
        suffix = "" if is_exact else f" (ближайшая дата: {actual_day.isoformat()})"
        await msg.answer(f"🌅 {label} ифтар{suffix}: <b>{ift.strftime('%H:%M')}</b>", reply_markup=main_menu_kb())
        return

    if "сахар" in text:
        target = tomorrow if "завтра" in text else today
        resolved = resolve_day_times(target)
        if not resolved:
            await msg.answer("⚠️ Не нашёл время в time.txt")
            return
        actual_day, sah, _, is_exact = resolved
        label = "Завтра" if target == tomorrow else "Сегодня"
        suffix = "" if is_exact else f" (ближайшая дата: {actual_day.isoformat()})"
        await msg.answer(f"🌙 {label} сахарлик{suffix}: <b>{sah.strftime('%H:%M')}</b>", reply_markup=main_menu_kb())
        return

    # если непонятно — покажем меню
    await msg.answer("Нажми кнопку 👇", reply_markup=main_menu_kb())


# =========================
# SCHEDULER LOOP (notifications)
# =========================
async def notification_loop(bot: Bot):
    while True:
        try:
            now = now_local()
            today = now.date()

            # Проверка таблицы на сегодня и завтра (для ночного сообщения)
            today_times = load_day_times(today)
            tomorrow_times = load_day_times(today + timedelta(days=1))

            # Пройдём по пользователям
            for uid, u in list(users.items()):
                # Если пользователь отключил
                if not u.enabled:
                    continue

                # --- Ежедневные сообщения ---
                # Утро: "Сегодня ..."
                if now.strftime("%H:%M") == u.morning_time and not was_sent(uid, today, "morning"):
                    if today_times:
                        text = "☀️ <b>Сегодня:</b>\n" + format_day_info(today) + "\n\n" + nice_phrase()
                    else:
                        text = "☀️ Сегодняшнее время не найдено в time.txt"
                    await bot.send_message(int(uid), text)
                    mark_sent(uid, today, "morning")

                # Ночь: "Завтра ..."
                if now.strftime("%H:%M") == u.night_time and not was_sent(uid, today, "night"):
                    tmr = today + timedelta(days=1)
                    if tomorrow_times:
                        text = "🌙 <b>Завтра:</b>\n" + format_day_info(tmr) + "\n\nСпокойной ночи 🤍"
                    else:
                        text = "🌙 Завтрашнее время не найдено в time.txt"
                    await bot.send_message(int(uid), text)
                    mark_sent(uid, today, "night")

                # --- Напоминания про ифтар/сахарлик ---
                if not today_times:
                    continue
                sah_t, ift_t = today_times
                preset = PRESETS.get(u.preset, PRESETS[DEFAULT_PRESET])

                sah_dt = combine(today, sah_t)
                ift_dt = combine(today, ift_t)

                # Ифтар: за N минут
                for mins in preset["iftar"]:
                    key = f"iftar-{mins}"
                    if was_sent(uid, today, key):
                        continue
                    trigger = ift_dt - timedelta(minutes=mins)
                    # Сработать в пределах текущей минуты
                    if trigger <= now < trigger + timedelta(minutes=1):
                        await bot.send_message(
                            int(uid),
                            f"⏳ До ифтара осталось <b>{mins} мин</b>\n{nice_phrase()}"
                        )
                        mark_sent(uid, today, key)

                # Ифтар: в момент
                if not was_sent(uid, today, "iftar-now"):
                    if ift_dt <= now < ift_dt + timedelta(minutes=1):
                        await bot.send_message(
                            int(uid),
                            "🌅 <b>Ифтар наступил.</b> Можно разговляться.\n\n"
                            "🤲 <b>Дуа:</b>\n" + read_dua(DUA_IFTAR_FILE)
                        )
                        mark_sent(uid, today, "iftar-now")

                # Сахарлик: за N минут
                for mins in preset["saharlik"]:
                    key = f"saharlik-{mins}"
                    if was_sent(uid, today, key):
                        continue
                    trigger = sah_dt - timedelta(minutes=mins)
                    if trigger <= now < trigger + timedelta(minutes=1):
                        await bot.send_message(
                            int(uid),
                            f"🌙 До окончания сахарлика осталось <b>{mins} мин</b>\n\n"
                            "🤲 <b>Дуа:</b>\n" + read_dua(DUA_SAHARLIK_FILE)
                        )
                        mark_sent(uid, today, key)

        except Exception as e:
            # Можно писать в файл логов, но чтобы не усложнять:
            # print("Scheduler error:", e)
            pass

        await asyncio.sleep(TICK_SECONDS)


# =========================
# MAIN
# =========================
async def main():
    bot = Bot(
        token=BOT_TOKEN,
        default=DefaultBotProperties(parse_mode="HTML"),
    )
    # запускаем фоновый цикл уведомлений
    asyncio.create_task(notification_loop(bot))
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
