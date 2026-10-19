import html
import json
import os
import re
import threading
import time
import traceback
from datetime import date, datetime, timedelta, timezone

import requests

TOKEN = os.environ["TG_TOKEN"]
GROUP_ID = os.environ.get("GROUP_ID", "729")  # ИПиП301Б_2024
CHECK_INTERVAL = int(os.environ.get("CHECK_INTERVAL", "900"))
WATCH_DAYS = int(os.environ.get("WATCH_DAYS", "14"))
API_TIMEOUT = 30
API_RETRIES = 3

API = "https://usla.unmp.ru/api/app/v1"
TG = f"https://api.telegram.org/bot{TOKEN}"
STATE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "state.json")

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

state_lock = threading.Lock()


def load_state():
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
    env_ids = os.environ.get("CHAT_IDS") or os.environ.get("CHAT_ID", "")
    subscribers += [int(x) for x in env_ids.split(",") if x.strip()]
    return {"subscribers": list(dict.fromkeys(subscribers)), "days": state.get("days")}


def save_state(state):
    with state_lock:
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False, indent=2)


def today():
    return datetime.now(TZ).date()


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
    data = api_get(f"schedule/group/{GROUP_ID}", {"date": day.isoformat()})["data"]
    return sorted(data, key=lambda x: x["startTime"])


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


def keycap(n):
    return "".join(c + "️⃣" for c in str(n))


def format_room(r):
    building, number = r["building"], r["number"]
    if building.startswith("*дист"):
        return "дистанционно"
    building = re.sub(r"^К\.,\s*", "", building)
    return f"корп. {building}, ауд. {number}"


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


def format_day(day, lessons):
    header = f"📅 <b>{day_label(day)}</b>"
    if not lessons:
        return f"{header}\n\n🎉 Пар нет"
    cards = [format_lesson(x, i) for i, x in enumerate(lessons, 1)]
    return header + "\n\n" + "\n\n".join(cards)


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


def send(chat_id, text, keyboard=False):
    payload = {"chat_id": chat_id, "text": text, "parse_mode": "HTML"}
    if keyboard:
        payload["reply_markup"] = KEYBOARD
    r = requests.post(f"{TG}/sendMessage", json=payload, timeout=15)
    if not r.ok:
        print("Telegram отклонил сообщение:", r.text)
        # Повторяем без разметки, чтобы ответ в любом случае дошёл
        payload.pop("parse_mode")
        payload["text"] = re.sub(r"</?b>", "", html.unescape(text))
        requests.post(f"{TG}/sendMessage", json=payload, timeout=15)


def show_day(chat_id, day):
    try:
        send(chat_id, format_day(day, fetch_day(day)), keyboard=True)
    except Exception as e:
        print("Ошибка загрузки расписания:", e)
        send(chat_id, "Не удалось получить расписание, попробуйте позже.", keyboard=True)


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


def handle_message(state, msg):
    chat_id = msg.get("chat", {}).get("id")
    text = msg.get("text", "")
    if chat_id is None:
        return

    if text.startswith("/start"):
        if chat_id not in state["subscribers"]:
            state["subscribers"].append(chat_id)
            save_state(state)
        send(chat_id, "👋 Какой день?", keyboard=True)
    elif text.startswith("/stop"):
        if chat_id in state["subscribers"]:
            state["subscribers"].remove(chat_id)
            save_state(state)
        send(chat_id, "🔕 Уведомления выключены. Вернуть: /start")
    elif text.startswith("/schedule"):
        show_day(chat_id, today())
    else:
        day = parse_date(text)
        if day is None:
            send(chat_id, "Не понял дату. Напишите ДД.ММ или ДД.ММ.ГГГГ, например 25.10", keyboard=True)
        else:
            show_day(chat_id, day)


def handle_callback(cb):
    requests.post(f"{TG}/answerCallbackQuery", json={"callback_query_id": cb["id"]}, timeout=15)
    chat_id = cb.get("message", {}).get("chat", {}).get("id")
    data = cb.get("data", "")
    if chat_id is None:
        return

    if data.startswith("d:"):
        show_day(chat_id, today() + timedelta(days=int(data[2:])))
    elif data == "custom":
        send(chat_id, "Напишите дату в формате ДД.ММ или ДД.ММ.ГГГГ, например 25.10")


def main():
    state = load_state()
    threading.Thread(target=watcher, args=(state,), daemon=True).start()

    offset = None
    while True:
        try:
            r = requests.get(f"{TG}/getUpdates", params={"timeout": 30, "offset": offset}, timeout=40)
            body = r.json()
            if not body.get("ok"):
                print("Ошибка Telegram:", body)
                time.sleep(5)
                continue
            for update in body["result"]:
                offset = update["update_id"] + 1
                try:
                    if "callback_query" in update:
                        handle_callback(update["callback_query"])
                    elif "message" in update:
                        handle_message(state, update["message"])
                except Exception:
                    traceback.print_exc()
        except requests.RequestException as e:
            print("Ошибка сети:", e)
            time.sleep(5)


if __name__ == "__main__":
    main()
