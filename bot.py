#!/usr/bin/env python3
"""Публикует пары из data/*.json в Telegram-канал: 5 постов в день."""

import functools
import html
import json
import os
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

print = functools.partial(print, flush=True)

ROOT = Path(__file__).resolve().parent
DATA_DIR = ROOT / "data"
BUNDLED_STATE = ROOT / "state" / "progress.json"
STATE_PATH = BUNDLED_STATE
CA_FILE = "/etc/ssl/cert.pem"
SSL_CONTEXT = ssl.create_default_context()
CATCHUP_GAP_SEC = 25
POLL_TIMEOUT = 25


def configure_runtime() -> None:
    global STATE_PATH, SSL_CONTEXT
    load_env(ROOT / ".env")
    os.environ.setdefault("TZ", "Europe/Berlin")
    configured = os.environ.get("STATE_PATH", "").strip()
    if configured:
        STATE_PATH = Path(configured)
        if not STATE_PATH.exists() and BUNDLED_STATE.exists():
            STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
            STATE_PATH.write_text(BUNDLED_STATE.read_text(encoding="utf-8"), encoding="utf-8")
    if Path(CA_FILE).exists():
        SSL_CONTEXT = ssl.create_default_context(cafile=CA_FILE)


def local_now() -> datetime:
    try:
        return datetime.now(ZoneInfo(os.environ.get("TZ", "Europe/Berlin")))
    except Exception:
        return datetime.now().astimezone()


def load_env(path: Path) -> None:
    if not path.exists():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def post_hours() -> list[tuple[int, int]]:
    raw = os.environ.get("POST_HOURS", "11,13,15,17,19")
    hours = []
    for part in raw.split(","):
        part = part.strip()
        if not part:
            continue
        if ":" in part:
            hour_s, minute_s = part.split(":", 1)
            hours.append((int(hour_s), int(minute_s)))
        else:
            hours.append((int(part), 0))
    if not hours:
        raise SystemExit("POST_HOURS пустой")
    return sorted(hours)


def hashtag(model: str) -> str:
    cleaned = "".join(ch for ch in model if ch.isalnum() or ch == "_")
    if not any(ch.isalpha() for ch in cleaned):
        return ""
    return "#" + cleaned


USD_TO_UAH = 51


def price_in_uah(raw: str) -> str:
    cleaned = raw.replace("$", "").replace(",", "").replace(" ", "").strip()
    try:
        dollars = float(cleaned)
    except ValueError:
        return raw
    uah = round(dollars * USD_TO_UAH)
    return f"{uah:,} грн".replace(",", " ")


def caption(item: dict) -> str:
    lines = [f"<b>{html.escape(item['title'])}</b>"]
    if item["sku"]:
        lines.append(f"SKU: <code>{html.escape(item['sku'])}</code>")
    if item["price"]:
        lines.append(html.escape(price_in_uah(item["price"])))
    tag = hashtag(item["model"])
    if tag:
        lines.append(tag)
    return "\n\n".join(lines)


def load_products() -> list[dict]:
    items = []
    seen = set()
    files = sorted(DATA_DIR.glob("*.json"))
    if not files:
        raise SystemExit(f"В {DATA_DIR} нет json-файлов")
    for path in files:
        data = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            data = data.get("products") or data.get("items") or []
        if not isinstance(data, list):
            continue
        for product in data:
            if not isinstance(product, dict):
                continue
            images = product.get("images") or []
            if isinstance(images, str):
                images = [images]
            photos = []
            for url in images:
                if isinstance(url, str) and url.startswith("http") and url not in photos:
                    photos.append(url)
            title = str(product.get("title") or "").strip()
            sku = str(product.get("sku") or "").strip()
            model = str(product.get("model") or "").strip()
            price = str(product.get("price") or "").strip()
            if not photos or not title:
                continue
            key = sku or str(product.get("id") or title)
            if key in seen:
                continue
            seen.add(key)
            items.append(
                {
                    "key": key,
                    "title": title,
                    "sku": sku,
                    "model": model,
                    "price": price,
                    "images": photos,
                }
            )
    if not items:
        raise SystemExit("В json нет пар с фото и названием")
    return items


def load_state() -> dict:
    if not STATE_PATH.exists():
        return {"channel_id": os.environ.get("CHANNEL") or "", "posted": [], "date": "", "sent_slots": []}
    state = json.loads(STATE_PATH.read_text(encoding="utf-8"))
    state.setdefault("channel_id", os.environ.get("CHANNEL") or "")
    state.setdefault("posted", [])
    state.setdefault("date", "")
    state.setdefault("sent_slots", [])
    env_channel = os.environ.get("CHANNEL") or ""
    if env_channel:
        state["channel_id"] = env_channel
    return state


def save_state(state: dict) -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = STATE_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(STATE_PATH)


def api(method: str, payload: dict | None = None, timeout: int = 40) -> dict:
    token = os.environ["BOT_TOKEN"]
    url = f"https://api.telegram.org/bot{token}/{method}"
    data = None
    headers = {}
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(url, data=data, headers=headers)
    try:
        with urllib.request.urlopen(request, context=SSL_CONTEXT, timeout=timeout) as response:
            body = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", errors="replace")
        try:
            body = json.loads(raw)
        except json.JSONDecodeError:
            raise RuntimeError(f"{method} HTTP {exc.code}: {raw}") from exc
        if exc.code == 429:
            retry = int(body.get("parameters", {}).get("retry_after", 5))
            print(f"лимит Telegram, жду {retry} с")
            time.sleep(retry)
            return api(method, payload, timeout)
        raise RuntimeError(f"{method}: {body}") from exc
    if not body.get("ok"):
        raise RuntimeError(f"{method}: {body}")
    return body


def remember_channel(state: dict, chat: dict | None) -> None:
    if not chat or chat.get("type") != "channel":
        return
    chat_id = str(chat["id"])
    if state.get("channel_id") == chat_id:
        return
    state["channel_id"] = chat_id
    save_state(state)
    title = chat.get("title") or chat.get("username") or chat_id
    print(f"канал: {title} ({chat_id})")


def handle_update(state: dict, update: dict) -> None:
    remember_channel(state, (update.get("my_chat_member") or {}).get("chat"))
    remember_channel(state, (update.get("channel_post") or {}).get("chat"))
    remember_channel(state, (update.get("edited_channel_post") or {}).get("chat"))

    message = update.get("message") or {}
    text = (message.get("text") or "").strip()
    chat = message.get("chat") or {}
    if chat.get("type") != "private" or not text:
        return
    reply_to = chat["id"]
    if text.startswith("/start"):
        api(
            "sendMessage",
            {
                "chat_id": reply_to,
                "text": (
                    "Напиши @username канала, куда постить пары.\n"
                    "Бот должен быть админом канала с правом публиковать сообщения."
                ),
            },
        )
        return
    candidate = text.split()[0]
    if candidate.startswith("@") or candidate.lstrip("-").isdigit():
        try:
            result = api("getChat", {"chat_id": candidate})["result"]
        except RuntimeError as exc:
            api("sendMessage", {"chat_id": reply_to, "text": f"Не вижу этот чат: {exc}"})
            return
        if result.get("type") != "channel":
            api("sendMessage", {"chat_id": reply_to, "text": "Это не канал."})
            return
        state["channel_id"] = str(result["id"])
        save_state(state)
        api(
            "sendMessage",
            {
                "chat_id": reply_to,
                "text": f"Канал сохранён: {result.get('title') or result.get('username')}",
            },
        )


def next_item(products: list[dict], state: dict) -> dict | None:
    posted = set(state.get("posted") or [])
    for item in products:
        if item["key"] not in posted:
            return item
    return None


def publish(state: dict, item: dict) -> None:
    text = caption(item)
    photos = item["images"]
    chat_id = state["channel_id"]
    if len(photos) == 1:
        api(
            "sendPhoto",
            {
                "chat_id": chat_id,
                "photo": photos[0],
                "caption": text,
                "parse_mode": "HTML",
            },
        )
    else:
        for offset in range(0, len(photos), 10):
            chunk = photos[offset : offset + 10]
            if len(chunk) == 1:
                api("sendPhoto", {"chat_id": chat_id, "photo": chunk[0]})
                break
            media = [{"type": "photo", "media": url} for url in chunk]
            if offset == 0:
                media[0]["caption"] = text
                media[0]["parse_mode"] = "HTML"
            api("sendMediaGroup", {"chat_id": chat_id, "media": media})
    state.setdefault("posted", []).append(item["key"])
    save_state(state)
    print(
        f"пост: {item['title']} | {item['sku']} | {price_in_uah(item['price'])} | {len(photos)} фото | {hashtag(item['model'])}"
    )


def slot_datetime(now: datetime, hour: int, minute: int) -> datetime:
    return now.replace(hour=hour, minute=minute, second=0, microsecond=0)


def maybe_publish(products: list[dict], state: dict, now: datetime) -> None:
    if not state.get("channel_id"):
        return
    today = now.date().isoformat()
    if state.get("date") != today:
        state["date"] = today
        state["sent_slots"] = []
        save_state(state)

    hours = post_hours()
    sent = set(state.get("sent_slots") or [])
    due = []
    for index, (hour, minute) in enumerate(hours):
        if index in sent:
            continue
        if now >= slot_datetime(now, hour, minute):
            due.append(index)
    if not due:
        return

    last_post = state.get("last_post_at") or 0
    if time.time() - last_post < CATCHUP_GAP_SEC and len(sent) > 0:
        return

    item = next_item(products, state)
    slot = due[0]
    if item is None:
        print("все пары из data уже опубликованы")
        state.setdefault("sent_slots", []).append(slot)
        save_state(state)
        return
    try:
        publish(state, item)
    except RuntimeError as exc:
        print(f"не удалось опубликовать {item['key']}: {exc}")
        state.setdefault("posted", []).append(item["key"])
        state.setdefault("failed", []).append({"key": item["key"], "error": str(exc)})
        save_state(state)
        return
    state.setdefault("sent_slots", []).append(slot)
    state["last_post_at"] = time.time()
    save_state(state)


def main() -> None:
    configure_runtime()
    if not os.environ.get("BOT_TOKEN"):
        raise SystemExit("В .env нет BOT_TOKEN")
    products = load_products()
    state = load_state()
    api("deleteWebhook", {"drop_pending_updates": False})
    me = api("getMe")["result"]
    print(f"бот @{me.get('username')} | пар в очереди: {len(products) - len(set(state.get('posted') or []))}")
    if state.get("channel_id"):
        print(f"канал: {state['channel_id']}")
        print("слоты:", ", ".join(f"{h:02d}:{m:02d}" for h, m in post_hours()))
    else:
        print("канал не задан. Напиши боту в личку @username канала")

    offset = 0
    while True:
        try:
            updates = api(
                "getUpdates",
                {
                    "offset": offset,
                    "timeout": POLL_TIMEOUT,
                    "allowed_updates": [
                        "message",
                        "my_chat_member",
                        "channel_post",
                        "edited_channel_post",
                    ],
                },
                timeout=POLL_TIMEOUT + 15,
            )["result"]
        except Exception as exc:
            print(f"getUpdates: {exc}")
            time.sleep(5)
            updates = []
        for update in updates:
            offset = update["update_id"] + 1
            try:
                handle_update(state, update)
            except Exception as exc:
                print(f"update {update.get('update_id')}: {exc}")
        try:
            maybe_publish(products, state, local_now())
        except Exception as exc:
            print(f"публикация: {exc}")


if __name__ == "__main__":
    main()
