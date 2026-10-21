import base64
import csv
import html
import io
import json
import os
import re
import sqlite3
import threading
import time
import traceback
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from datetime import date, datetime, timedelta, timezone

import requests


def env_ints(name):
    """Числа из переменной окружения. Кавычки, пробелы и «длинный минус» из копипасты
    не должны ронять бота при запуске."""
    result = []
    for part in os.environ.get(name, "").replace(";", ",").split(","):
        cleaned = part.strip().strip("'\"").replace("−", "-").replace("–", "-")
        if not cleaned:
            continue
        try:
            result.append(int(cleaned))
        except ValueError:
            print(f"Переменная {name}: не понял значение {part!r}, пропускаю")
    return result


# Хостинги по-разному называют переменную с токеном
TOKEN = os.environ.get("TG_TOKEN") or os.environ.get("BOT_TOKEN") or os.environ.get("TOKEN")
if not TOKEN:
    raise SystemExit("Не задан токен бота: добавьте переменную окружения TG_TOKEN (или BOT_TOKEN)")
GROUP_ID = os.environ.get("GROUP_ID", "729")  # ИПиП301Б_2024
CHECK_INTERVAL = int(os.environ.get("CHECK_INTERVAL", "900"))
WATCH_DAYS = int(os.environ.get("WATCH_DAYS", "14"))
API_TIMEOUT = 30
API_RETRIES = 3
RATE_LIMIT = 5  # не больше стольких действий от одного человека...
RATE_WINDOW = 30  # ...за столько секунд
CACHE_TTL = 60  # расписание дня держим в памяти столько секунд
# Telegram ID админов через запятую; узнать свой можно командой /myid
ADMIN_IDS = set(env_ints("ADMIN_IDS"))
# Закрытый канал с домашкой (ID вида -100XXXXXXXXXX). Пока не задан, бот сам подскажет админу ID
HOMEWORK_CHANNEL_ID = (env_ints("HOMEWORK_CHANNEL_ID") or [None])[0]
# Если у хостинга есть постоянный диск, укажите путь на нём, иначе база стирается при пересборке.
# DATA_DIR (BotHost задаёт его сам: /app/data) используется по умолчанию, DB_PATH и STATE_PATH его перекрывают
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.environ.get("DATA_DIR") or BASE_DIR
DB_PATH = os.environ.get("DB_PATH") or os.path.join(DATA_DIR, "users.db")

# Календарь-подписка для iPhone: бот сам отдаёт /schedule.ics на этом порту.
# PUBLIC_URL — публичный адрес, по которому хостинг открывает бота из интернета (https://...)
HTTP_PORT = int(os.environ.get("PORT") or os.environ.get("HTTP_PORT") or "8080")
PUBLIC_URL = os.environ.get("PUBLIC_URL", "").strip()
ICS_DAYS = 120  # на сколько дней вперёд просим расписание
ICS_TTL = 600  # календарь пересобираем не чаще раза в 10 минут
ics_cache = [0, ""]
ics_lock = threading.Lock()

API = "https://usla.unmp.ru/api/app/v1"
TG = f"https://api.telegram.org/bot{TOKEN}"
STATE_FILE = os.environ.get("STATE_PATH") or os.path.join(DATA_DIR, "state.json")

TZ = timezone(timedelta(hours=5))  # Екатеринбург
WEEKDAYS = ["Понедельник", "Вторник", "Среда", "Четверг", "Пятница", "Суббота", "Воскресенье"]
MONTHS = [
    "января", "февраля", "марта", "апреля", "мая", "июня",
    "июля", "августа", "сентября", "октября", "ноября", "декабря",
]
LESSON_TYPES = {"Практическое занятие": "Практика", "Текущие консультации": "Консультация"}

KEYBOARD = {
    "inline_keyboard": [
        [
            {"text": "Вчера", "callback_data": "d:-1"},
            {"text": "Сегодня", "callback_data": "d:0"},
        ],
        [
            {"text": "Завтра", "callback_data": "d:1"},
            {"text": "Послезавтра", "callback_data": "d:2"},
        ],
        [{"text": "📅 Другая дата", "callback_data": "custom"}],
    ]
}

ADMIN_KEYBOARD = {
    "inline_keyboard": [
        [
            {"text": "📊 Сводка", "callback_data": "adm:summary"},
            {"text": "🏆 Топ", "callback_data": "adm:top"},
        ],
        [
            {"text": "🆕 Новые", "callback_data": "adm:new"},
            {"text": "📥 CSV", "callback_data": "adm:csv"},
        ],
    ]
}

KNOWN_COMMANDS = {"/rasp", "/rasp_off", "/start", "/stop", "/schedule", "/admin", "/myid", "/calendar"}
BOT_USERNAME = None  # узнаём у Telegram при запуске

state_lock = threading.Lock()
db_lock = threading.Lock()
_db = None
user_actions = {}
day_cache = {}


def rate_limited(user_id):
    now = time.time()
    recent = [t for t in user_actions.get(user_id, []) if now - t < RATE_WINDOW]
    limited = len(recent) >= RATE_LIMIT
    if not limited:
        recent.append(now)
    user_actions[user_id] = recent
    return limited


def restore_base64_file(path, is_valid):
    """Файловый менеджер хостинга может сохранить загруженный файл как base64-текст.
    Если файл не проходит проверку, но после раскодирования проходит, подменяем его настоящим
    (исходный остаётся рядом как .base64). Любой сбой тихо игнорируем, бот не должен падать."""
    try:
        with open(path, "rb") as f:
            raw = f.read()
        if not raw or is_valid(raw):
            return
        decoded = base64.b64decode(raw, validate=False)
        if not is_valid(decoded):
            return
        with open(path + ".base64", "wb") as f:
            f.write(raw)
        tmp = path + ".tmp"
        with open(tmp, "wb") as f:
            f.write(decoded)
        os.replace(tmp, path)
        print(f"Файл {path} был в base64, восстановлен ({len(raw)} -> {len(decoded)} байт)")
    except Exception as e:
        print(f"restore_base64_file({path}): {e}")


def _is_json(raw):
    try:
        json.loads(raw.decode("utf-8-sig"))
        return True
    except Exception:
        return False


def load_state():
    restore_base64_file(STATE_FILE, _is_json)
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            state = json.load(f)
    except FileNotFoundError:
        state = {}
    # None вместо пустого словаря: первый запуск отличаем от «пар нет»
    subscribers = list(state.get("subscribers", []))
    if state.get("chat_id"):  # старый формат: один чат
        subscribers.append(state["chat_id"])
    # CHAT_IDS из окружения (через запятую) нужны хостингам, где диск стирается при перезапуске
    subscribers += env_ints("CHAT_IDS") or env_ints("CHAT_ID")
    return {"subscribers": list(dict.fromkeys(subscribers)), "days": state.get("days")}


def save_state(state):
    with state_lock:
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False, indent=2)


def today():
    return datetime.now(TZ).date()


SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    user_id INTEGER PRIMARY KEY, username TEXT, first_name TEXT,
    first_seen TEXT, last_seen TEXT, actions INTEGER DEFAULT 0, registered_at TEXT
);
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, chat_id INTEGER, kind TEXT, ts TEXT
);
CREATE TABLE IF NOT EXISTS chats (
    chat_id INTEGER PRIMARY KEY, type TEXT, title TEXT, first_seen TEXT
);
CREATE TABLE IF NOT EXISTS homework (
    chat_id INTEGER, message_id INTEGER, day TEXT, PRIMARY KEY (chat_id, message_id)
);
CREATE INDEX IF NOT EXISTS homework_day ON homework(day);
"""


def get_db():
    """Вызывать только под db_lock."""
    global _db
    if _db is None:
        os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
        restore_base64_file(DB_PATH, lambda raw: raw.startswith(b"SQLite format 3\x00"))
        _db = sqlite3.connect(DB_PATH, check_same_thread=False)
        _db.executescript(SCHEMA)
    return _db


def db_query(sql, args=()):
    with db_lock:
        return get_db().execute(sql, args).fetchall()


def track(user, chat, kind, register=False):
    """Записываем, кто и что сделал. Сбой базы не должен ломать бота."""
    if not user or user.get("is_bot"):
        return
    now = datetime.now(TZ).strftime("%Y-%m-%d %H:%M:%S")
    chat_title = chat.get("title") or chat.get("username") or chat.get("first_name")
    try:
        with db_lock:
            db = get_db()
            db.execute(
                "INSERT INTO users(user_id, username, first_name, first_seen, last_seen) VALUES(?,?,?,?,?) "
                "ON CONFLICT(user_id) DO UPDATE SET username=excluded.username, "
                "first_name=excluded.first_name, last_seen=excluded.last_seen",
                (user["id"], user.get("username"), user.get("first_name"), now, now),
            )
            db.execute("UPDATE users SET actions = actions + 1 WHERE user_id = ?", (user["id"],))
            if register:
                db.execute(
                    "UPDATE users SET registered_at = COALESCE(registered_at, ?) WHERE user_id = ?",
                    (now, user["id"]),
                )
            db.execute(
                "INSERT INTO events(user_id, chat_id, kind, ts) VALUES(?,?,?,?)",
                (user["id"], chat.get("id"), kind, now),
            )
            db.execute(
                "INSERT OR IGNORE INTO chats(chat_id, type, title, first_seen) VALUES(?,?,?,?)",
                (chat.get("id"), chat.get("type"), chat_title, now),
            )
            db.commit()
    except Exception:
        traceback.print_exc()


def day_label(day):
    return f"{WEEKDAYS[day.weekday()]}, {day.day} {MONTHS[day.month - 1]}"


def api_get(path, params):
    """Сервер расписания иногда отвечает медленно, поэтому пробуем несколько раз."""
    for attempt in range(1, API_RETRIES + 1):
        try:
            r = requests.get(f"{API}/{path}", params=params, timeout=API_TIMEOUT)
            r.raise_for_status()
            return r.json()
        except requests.RequestException as e:
            print(f"Запрос {path} не удался (попытка {attempt}/{API_RETRIES}):", e)
            if attempt == API_RETRIES:
                raise
            time.sleep(2)


def fetch_day(day):
    cached = day_cache.get(day)
    if cached and time.time() - cached[0] < CACHE_TTL:
        return cached[1]
    data = api_get(f"schedule/group/{GROUP_ID}", {"date": day.isoformat()})["data"]
    lessons = sorted(data, key=lambda x: x["startTime"])
    if len(day_cache) > 200:  # даты вводят вручную, не даём кэшу расти бесконечно
        day_cache.clear()
    day_cache[day] = (time.time(), lessons)
    return lessons


def fetch_range(start, end):
    days = api_get(
        f"schedule/group/{GROUP_ID}/range",
        {"from": start.isoformat(), "to": end.isoformat()},
    )["days"]
    # Дни без пар тоже кладём в снимок, чтобы отличать «пар нет» от «день вне окна»
    result = {}
    for i in range((end - start).days + 1):
        iso = (start + timedelta(days=i)).isoformat()
        result[iso] = {x["id"]: x for x in days.get(iso, [])}
    return result


def ics_escape(text):
    return text.replace("\\", "\\\\").replace(";", "\\;").replace(",", "\\,").replace("\n", "\\n")


def ics_fold(line):
    """Строки iCalendar длиннее 75 байт переносятся (продолжение начинается с пробела)."""
    out, cur = [], ""
    for ch in line:
        if len((cur + ch).encode("utf-8")) > (75 if not out else 74):
            out.append(cur)
            cur = ch
        else:
            cur += ch
    out.append(cur)
    return "\r\n ".join(out)


def build_ics():
    """Календарь с парами: от недели назад на ICS_DAYS дней вперёд. Название и место события
    видны в виджете экрана блокировки iPhone, поэтому кладём туда предмет и аудиторию."""
    start = today() - timedelta(days=7)
    end = today() + timedelta(days=ICS_DAYS)
    days = api_get(
        f"schedule/group/{GROUP_ID}/range",
        {"from": start.isoformat(), "to": end.isoformat()},
    )["days"]
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    lines = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        "PRODID:-//raspisanie301//RU",
        "CALSCALE:GREGORIAN",
        "METHOD:PUBLISH",
        "X-WR-CALNAME:Пары ИПиП301Б",
        "X-WR-TIMEZONE:Asia/Yekaterinburg",
        "REFRESH-INTERVAL;VALUE=DURATION:PT1H",
        "X-PUBLISHED-TTL:PT1H",
        "BEGIN:VTIMEZONE",
        "TZID:Asia/Yekaterinburg",
        "BEGIN:STANDARD",
        "DTSTART:19700101T000000",
        "TZOFFSETFROM:+0500",
        "TZOFFSETTO:+0500",
        "TZNAME:+05",
        "END:STANDARD",
        "END:VTIMEZONE",
    ]
    for iso in sorted(days):
        for x in sorted(days[iso], key=lambda x: x["startTime"]):
            day = iso.replace("-", "")
            lesson_type = LESSON_TYPES.get(x["type"]["name"], x["type"]["name"])
            details = [lesson_type]
            if x["teachers"]:
                details.append(", ".join(t["shortName"] for t in x["teachers"]))
            event = [
                "BEGIN:VEVENT",
                f"UID:lesson-{x['id']}-{iso}@raspisanie301",
                f"DTSTAMP:{stamp}",
                f"DTSTART;TZID=Asia/Yekaterinburg:{day}T{x['startTime'].replace(':', '')}00",
                f"DTEND;TZID=Asia/Yekaterinburg:{day}T{x['endTime'].replace(':', '')}00",
                f"SUMMARY:{ics_escape(x['subject'])}",
                f"DESCRIPTION:{ics_escape(chr(10).join(details))}",
            ]
            if x["rooms"]:
                event.append("LOCATION:" + ics_escape("; ".join(format_room(r) for r in x["rooms"])))
            event.append("END:VEVENT")
            lines += event
    lines.append("END:VCALENDAR")
    return "\r\n".join(ics_fold(line) for line in lines) + "\r\n"


def get_ics():
    with ics_lock:
        if ics_cache[0] and time.time() - ics_cache[0] < ICS_TTL:
            return ics_cache[1]
        ics_cache[:] = [time.time(), build_ics()]
        return ics_cache[1]


class CalendarHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path.split("?")[0] not in ("/schedule.ics", "/"):
            self.send_error(404)
            return
        try:
            body = get_ics().encode("utf-8")
        except Exception as e:
            print("Не удалось собрать календарь:", e)
            self.send_error(502)
            return
        self.send_response(200)
        self.send_header("Content-Type", "text/calendar; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


def serve_calendar():
    try:
        ThreadingHTTPServer(("0.0.0.0", HTTP_PORT), CalendarHandler).serve_forever()
    except Exception as e:
        print("Сервер календаря не запустился:", e)


def send_calendar(chat_id):
    if PUBLIC_URL:
        url = PUBLIC_URL.rstrip("/") + "/schedule.ics"
        webcal = "webcal://" + re.sub(r"^https?://", "", url)
        send(
            chat_id,
            "📆 <b>Календарь для iPhone</b>\n\n"
            f'1. Нажмите на ссылку: <a href="{webcal}">подписаться на расписание</a>\n'
            "    (если не открывается: Настройки → Календарь → Учётные записи → "
            "Новая → Другое → Добавить календарь подписки и вставьте <code>"
            f"{html.escape(url)}</code>)\n"
            "2. Расписание обновляется само, а изменения подтянутся в течение часа.\n"
            "3. Виджет: удерживайте экран блокировки → Настроить → добавьте виджет «Календарь».",
        )
    else:
        requests.post(
            f"{TG}/sendDocument",
            data={"chat_id": chat_id, "caption": "Откройте файл и нажмите «Добавить все». Это разовый импорт без автообновления."},
            files={"document": ("schedule.ics", get_ics().encode("utf-8"), "text/calendar")},
            timeout=60,
        )


def keycap(n):
    return "".join(c + "️⃣" for c in str(n))


def format_room(r):
    building, number = r["building"], r["number"]
    if building.startswith("*дист"):
        return "дистанционно"
    if re.fullmatch(r"[IVXLC]+", number):  # римская цифра — это «Римка»
        return f"Римка {number}"
    if number.isdigit():
        return f"ауд. {number}"
    return number  # например, ФОК


def format_lesson(x, index=None):
    """Карточка пары (HTML). index — номер пары в дне, None — без номера."""
    lesson_type = LESSON_TYPES.get(x["type"]["name"], x["type"]["name"])
    prefix = f"{keycap(index)} " if index else ""
    lines = [
        f"{prefix}<b>{html.escape(x['subject'])}</b>",
        f"🕒 {x['startTime']}–{x['endTime']} · {html.escape(lesson_type)}",
    ]
    if x["rooms"]:
        lines.append("📍 " + html.escape("; ".join(format_room(r) for r in x["rooms"])))
    if x["teachers"]:
        lines.append("👤 " + html.escape(", ".join(t["shortName"] for t in x["teachers"])))
    return "\n".join(lines)


def homework_footer(links):
    if not links:
        return ""
    if len(links) == 1:
        return f'\n\n📚 <a href="{links[0]}">Домашка на этот день</a>'
    items = ", ".join(f'<a href="{url}">пост {i}</a>' for i, url in enumerate(links, 1))
    return f"\n\n📚 Домашка: {items}"


def format_day(day, lessons, links=()):
    header = f"📅 <b>{day_label(day)}</b>"
    footer = homework_footer(links)
    if not lessons:
        return f"{header}\n\n🎉 Пар нет{footer}"
    cards = [format_lesson(x, i) for i, x in enumerate(lessons, 1)]
    return header + "\n\n" + "\n\n".join(cards) + footer


def parse_date(text):
    text = text.strip()
    m = re.fullmatch(r"(\d{4})-(\d{1,2})-(\d{1,2})", text)
    if m:
        y, mo, d = map(int, m.groups())
    else:
        m = re.fullmatch(r"(\d{1,2})[./-](\d{1,2})(?:[./-](\d{2}|\d{4}))?", text)
        if not m:
            return None
        d, mo = int(m.group(1)), int(m.group(2))
        y = int(m.group(3)) if m.group(3) else today().year
        if y < 100:
            y += 2000
    try:
        return date(y, mo, d)
    except ValueError:
        return None


def diff(old_days, new_days):
    lines = []
    for iso in sorted(new_days):
        if iso not in old_days:
            continue  # день только что вошёл в окно отслеживания — это не изменение
        label = day_label(date.fromisoformat(iso))
        old, new = old_days[iso], new_days[iso]
        for lesson_id, x in new.items():
            if lesson_id not in old:
                lines.append(f"➕ <b>Новая пара</b> · {label}\n\n" + format_lesson(x))
            elif old[lesson_id] != x:
                lines.append(f"✏️ <b>Изменение</b> · {label}\n\n" + format_lesson(x))
        for lesson_id, x in old.items():
            if lesson_id not in new:
                lines.append(f"❌ <b>Пара отменена</b> · {label}\n\n" + format_lesson(x))
    return lines


def send(chat_id, text, keyboard=False, markup=None):
    payload = {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "HTML",
        "link_preview_options": {"is_disabled": True},
    }
    if markup or keyboard:
        payload["reply_markup"] = markup or KEYBOARD
    r = requests.post(f"{TG}/sendMessage", json=payload, timeout=15)
    if not r.ok:
        print("Telegram отклонил сообщение:", r.text)
        # Повторяем без разметки, чтобы ответ в любом случае дошёл
        payload.pop("parse_mode")
        payload["text"] = re.sub(r"<[^>]+>", "", html.unescape(text))
        requests.post(f"{TG}/sendMessage", json=payload, timeout=15)


def parse_post_date(text):
    """Дата из заголовка поста: 'четверг 22.10.2026' или просто '22.10.2026'. Даты дальше первой
    строки не берём: «сдать до 30.10.2026» в тексте не должно привязывать пост к другому дню."""
    lines = [line for line in text.splitlines() if line.strip()]
    if not lines:
        return None
    m = re.search(r"(\d{1,2})\.(\d{1,2})\.(\d{4})\b", lines[0])
    if not m:
        return None
    d, mo, y = map(int, m.groups())
    try:
        return date(y, mo, d)
    except ValueError:
        return None


def post_link(chat_id, message_id):
    # Для закрытых каналов ID -100XXXXXXXXXX превращается в t.me/c/XXXXXXXXXX/<номер поста>
    return f"https://t.me/c/{str(chat_id)[4:]}/{message_id}"


def homework_links(day):
    rows = db_query(
        "SELECT chat_id, message_id FROM homework WHERE day = ? ORDER BY message_id", (day.isoformat(),)
    )
    return [post_link(c, m) for c, m in rows]


seen_channels = set()


def notify_unknown_channel(chat):
    """Бота могли добавить в чужой канал, поэтому посты оттуда не берём; админу сообщаем ID один раз."""
    chat_id = chat.get("id")
    if chat_id in seen_channels:
        return
    seen_channels.add(chat_id)
    title = html.escape(chat.get("title") or "без названия")
    print(f"Пост из канала {chat_id} ({title}), HOMEWORK_CHANNEL_ID не совпадает")
    for admin_id in ADMIN_IDS:
        send(
            admin_id,
            f"📣 Бот получил пост из канала «{title}» (ID <code>{chat_id}</code>).\n"
            f"Если это канал с домашкой, задайте переменную HOMEWORK_CHANNEL_ID = {chat_id} и перезапустите бота.",
        )


def handle_channel_post(post):
    chat = post.get("chat", {})
    if HOMEWORK_CHANNEL_ID is None or chat.get("id") != HOMEWORK_CHANNEL_ID:
        notify_unknown_channel(chat)
        return
    # В постах с фото текст лежит в caption
    day = parse_post_date(post.get("text") or post.get("caption") or "")
    with db_lock:
        db = get_db()
        if day:
            db.execute(
                "INSERT OR REPLACE INTO homework(chat_id, message_id, day) VALUES(?,?,?)",
                (chat["id"], post["message_id"], day.isoformat()),
            )
        else:  # пост отредактировали и дату убрали
            db.execute(
                "DELETE FROM homework WHERE chat_id = ? AND message_id = ?", (chat["id"], post["message_id"])
            )
        db.commit()


def show_day(chat_id, day):
    try:
        links = homework_links(day)
    except Exception:
        traceback.print_exc()
        links = []
    try:
        send(chat_id, format_day(day, fetch_day(day), links), keyboard=True)
    except Exception as e:
        print("Ошибка загрузки расписания:", e)
        send(chat_id, "Не удалось получить расписание, попробуйте позже.", keyboard=True)


GROUP_CHATS_SQL = "SELECT COUNT(*) FROM chats WHERE type IN ('group', 'supergroup')"


def user_label(first_name, username):
    name = html.escape(first_name or "без имени")
    return f"{name} (@{html.escape(username)})" if username else name


def admin_summary(state):
    t = today()
    day, week = t.isoformat(), (t - timedelta(days=6)).isoformat()

    def one(sql, *args):
        return db_query(sql, args)[0][0]

    return "\n".join([
        "📊 <b>Сводка</b>",
        "",
        f"👥 Пользователей: <b>{one('SELECT COUNT(*) FROM users')}</b>",
        f"✅ Зарегистрировались: <b>{one('SELECT COUNT(*) FROM users WHERE registered_at IS NOT NULL')}</b>",
        "🆕 Новых сегодня / за 7 дней: "
        f"<b>{one('SELECT COUNT(*) FROM users WHERE first_seen >= ?', day)}</b> / "
        f"<b>{one('SELECT COUNT(*) FROM users WHERE first_seen >= ?', week)}</b>",
        "🔥 Активных сегодня / за 7 дней: "
        f"<b>{one('SELECT COUNT(DISTINCT user_id) FROM events WHERE ts >= ?', day)}</b> / "
        f"<b>{one('SELECT COUNT(DISTINCT user_id) FROM events WHERE ts >= ?', week)}</b>",
        "👆 Действий всего / сегодня: "
        f"<b>{one('SELECT COUNT(*) FROM events')}</b> / "
        f"<b>{one('SELECT COUNT(*) FROM events WHERE ts >= ?', day)}</b>",
        "💬 Чатов / из них групп: "
        f"<b>{one('SELECT COUNT(*) FROM chats')}</b> / "
        f"<b>{one(GROUP_CHATS_SQL)}</b>",
        f"🔔 Подписано на уведомления: <b>{len(state['subscribers'])}</b>",
        f"📚 Постов с домашкой в базе: <b>{one('SELECT COUNT(*) FROM homework')}</b>",
    ])


def admin_top():
    rows = db_query("SELECT first_name, username, actions FROM users ORDER BY actions DESC LIMIT 10")
    if not rows:
        return "🏆 Пока никого нет"
    lines = [f"{i}. {user_label(n, u)} — <b>{a}</b>" for i, (n, u, a) in enumerate(rows, 1)]
    return "🏆 <b>Топ по активности</b>\n\n" + "\n".join(lines)


def admin_new():
    rows = db_query(
        "SELECT first_name, username, first_seen, registered_at FROM users ORDER BY first_seen DESC LIMIT 10"
    )
    if not rows:
        return "🆕 Пока никого нет"
    lines = [
        f"{fs[8:10]}.{fs[5:7]} {fs[11:16]} · {user_label(n, u)}" + (" ✅" if reg else "")
        for n, u, fs, reg in rows
    ]
    return "🆕 <b>Последние новые</b> (✅ — зарегистрировался)\n\n" + "\n".join(lines)


def send_csv(chat_id):
    rows = db_query(
        "SELECT user_id, username, first_name, first_seen, last_seen, actions, registered_at "
        "FROM users ORDER BY actions DESC"
    )
    buf = io.StringIO(newline="")
    writer = csv.writer(buf)
    writer.writerow(["user_id", "username", "first_name", "first_seen", "last_seen", "actions", "registered_at"])
    writer.writerows(rows)
    data = buf.getvalue().encode("utf-8-sig")  # BOM, чтобы Excel открыл кириллицу
    requests.post(
        f"{TG}/sendDocument", data={"chat_id": chat_id}, files={"document": ("users.csv", data)}, timeout=30
    )


def admin_action(state, chat_id, action):
    if action == "csv":
        send_csv(chat_id)
        return
    texts = {"summary": lambda: admin_summary(state), "top": admin_top, "new": admin_new}
    send(chat_id, texts[action](), markup=ADMIN_KEYBOARD)


def watcher(state):
    while True:
        try:
            start = today()
            new = fetch_range(start, start + timedelta(days=WATCH_DAYS - 1))
            if state["days"] is not None and state["subscribers"]:
                for line in diff(state["days"], new):
                    for chat_id in list(state["subscribers"]):
                        try:
                            send(chat_id, line)
                        except Exception as e:  # например, пользователь заблокировал бота
                            print(f"Не удалось отправить в {chat_id}:", e)
            state["days"] = new
            save_state(state)
        except Exception as e:
            print("Ошибка проверки расписания:", e)
        time.sleep(CHECK_INTERVAL)


def parse_command(text):
    """'/rasp@my_bot arg' -> ('/rasp', 'my_bot'); не команда -> (None, None)."""
    if not text.startswith("/"):
        return None, None
    command, _, target = text.split()[0].partition("@")
    return command.lower(), target or None


def handle_message(state, msg):
    chat = msg.get("chat", {})
    chat_id = chat.get("id")
    text = msg.get("text", "")
    if chat_id is None:
        return
    user_id = msg.get("from", {}).get("id", chat_id)

    command, target = parse_command(text)
    if target and BOT_USERNAME and target.lower() != BOT_USERNAME.lower():
        return  # команда адресована другому боту в этом чате
    if command and command not in KNOWN_COMMANDS:
        return  # чужая команда, в общем чате на неё отвечают другие боты
    if command in ("/start", "/stop") and not target and chat.get("type") != "private":
        return  # в группе общий /start предназначен всем ботам сразу, у нас для этого /rasp

    if command == "/admin":
        # Только админам и только в личке; остальным не отвечаем, чтобы не раскрывать панель
        if user_id in ADMIN_IDS and chat.get("type") == "private":
            send(chat_id, "🛠 <b>Админ-панель</b>", markup=ADMIN_KEYBOARD)
        return

    day = None if command else parse_date(text)
    if not command and day is None:
        # В личке подсказываем формат, в групповом чате молчим: там обычная переписка
        if chat.get("type") == "private" and not rate_limited(user_id):
            send(chat_id, "Не понял дату. Напишите ДД.ММ или ДД.ММ.ГГГГ, например 25.10", keyboard=True)
        return
    # Спамера молча игнорируем: ответ на каждое сообщение только добавил бы спама
    if rate_limited(user_id):
        return

    if command == "/myid":
        send(chat_id, f"Ваш Telegram ID: <code>{user_id}</code>")
        return

    if command == "/calendar":
        track(msg.get("from"), chat, "calendar")
        try:
            send_calendar(chat_id)
        except Exception as e:
            print("Календарь:", e)
            send(chat_id, "Не удалось получить расписание, попробуйте позже.")
        return

    kinds = {"/rasp": "start", "/start": "start", "/rasp_off": "stop", "/stop": "stop", "/schedule": "schedule"}
    kind = kinds.get(command, "date")
    track(msg.get("from"), chat, kind, register=kind == "start")

    if kind == "start":
        if chat_id not in state["subscribers"]:
            state["subscribers"].append(chat_id)
            save_state(state)
        send(chat_id, "👋 Какой день?", keyboard=True)
    elif kind == "stop":
        if chat_id in state["subscribers"]:
            state["subscribers"].remove(chat_id)
            save_state(state)
        send(chat_id, "🔕 Уведомления выключены. Вернуть: /rasp")
    elif kind == "schedule":
        show_day(chat_id, today())
    else:
        show_day(chat_id, day)


def handle_callback(state, cb):
    chat = cb.get("message", {}).get("chat", {})
    chat_id = chat.get("id")
    data = cb.get("data", "")
    user = cb.get("from", {})
    answer = {"callback_query_id": cb["id"]}

    if data.startswith("adm:"):
        requests.post(f"{TG}/answerCallbackQuery", json=answer, timeout=15)
        if user.get("id") in ADMIN_IDS and chat.get("type") == "private":
            admin_action(state, chat_id, data[4:])
        return

    limited = rate_limited(user.get("id", chat_id))
    if limited:
        answer["text"] = "⏳ Не так быстро, подождите немного"
    requests.post(f"{TG}/answerCallbackQuery", json=answer, timeout=15)
    if limited or chat_id is None:
        return

    if data.startswith("d:"):
        track(user, chat, data)
        show_day(chat_id, today() + timedelta(days=int(data[2:])))
    elif data == "custom":
        track(user, chat, "custom")
        send(chat_id, "Напишите дату в формате ДД.ММ или ДД.ММ.ГГГГ, например 25.10")


def process_update(state, update):
    try:
        if "callback_query" in update:
            handle_callback(state, update["callback_query"])
        elif "message" in update:
            handle_message(state, update["message"])
        elif "channel_post" in update:
            handle_channel_post(update["channel_post"])
        elif "edited_channel_post" in update:
            handle_channel_post(update["edited_channel_post"])
    except Exception:
        traceback.print_exc()


def main():
    global BOT_USERNAME
    state = load_state()
    pool = ThreadPoolExecutor(max_workers=8)
    print(
        f"Бот запускается. Админов: {len(ADMIN_IDS)}, канал домашки: {HOMEWORK_CHANNEL_ID}, "
        f"база: {DB_PATH}, подписчиков: {len(state['subscribers'])}"
    )

    try:
        BOT_USERNAME = requests.get(f"{TG}/getMe", timeout=15).json()["result"]["username"]
        commands = [
            {"command": "rasp", "description": "Расписание и уведомления"},
            {"command": "rasp_off", "description": "Отключить уведомления"},
            {"command": "calendar", "description": "Расписание в календарь iPhone"},
            {"command": "myid", "description": "Узнать свой Telegram ID"},
        ]
        requests.post(f"{TG}/setMyCommands", json={"commands": commands}, timeout=15)
    except Exception as e:
        print("Не удалось получить имя бота или задать команды:", e)
    threading.Thread(target=watcher, args=(state,), daemon=True).start()
    threading.Thread(target=serve_calendar, daemon=True).start()

    offset = None
    while True:
        try:
            params = {
                "timeout": 30,
                "offset": offset,
                "allowed_updates": json.dumps(["message", "callback_query", "channel_post", "edited_channel_post"]),
            }
            r = requests.get(f"{TG}/getUpdates", params=params, timeout=40)
            body = r.json()
            if not body.get("ok"):
                print("Ошибка Telegram:", body)
                time.sleep(5)
                continue
            for update in body["result"]:
                offset = update["update_id"] + 1
                # Параллельно, чтобы медленный запрос одного человека не тормозил остальных
                pool.submit(process_update, state, update)
        except requests.RequestException as e:
            print("Ошибка сети:", e)
            time.sleep(5)


if __name__ == "__main__":
    main()
