import asyncio
import json
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

from telegram import KeyboardButton, ReplyKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)


def _load_dotenv(path: str = ".env") -> None:
    if not os.path.exists(path):
        return

    try:
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                s = line.strip()
                if not s or s.startswith("#"):
                    continue
                if "=" not in s:
                    continue
                k, v = s.split("=", 1)
                k = k.strip()
                v = v.strip().strip('"').strip("'")
                if k and k not in os.environ:
                    os.environ[k] = v
    except Exception:
        return


@dataclass
class ParsedReceipt:
    aggregator: str
    amount_uzs: int


def _normalize_spaces(s: str) -> str:
    return re.sub(r"\s+", " ", str(s)).strip()


def _detect_aggregator(text: str) -> Optional[str]:
    t = _normalize_spaces(text).lower()

    if "uzum" in t or "статус: успешно" in t or "транзакции" in t or "💰сумма" in t:
        return "Uzum"

    if "payme" in t or "оплачен" in t or "🧾" in t:
        return "Payme"

    if "click" in t or "клик" in t or "🟢" in t:
        return "Click"

    return None


def _extract_uzs_amount(text: str) -> Optional[int]:
    t = str(text)

    patterns = [
        re.compile(r"🇺🇿\s*([0-9][0-9\s.,]*)\s*(?:сум|so'm|som)?", re.IGNORECASE | re.UNICODE),
        re.compile(r"Сумма\s*:\s*([0-9][0-9\s.,]*)", re.IGNORECASE | re.UNICODE),
        re.compile(r"\b([0-9][0-9\s.,]*)\s*(?:сум|so'm|som)\b", re.IGNORECASE | re.UNICODE),
    ]

    for rx in patterns:
        m = rx.search(t)
        if not m:
            continue

        raw = m.group(1)
        
        # Remove decimal part if present (comma or dot followed by 2 digits at word boundary)
        # Examples: "72 000,00" → "72 000", "486,000.00" → "486,000"
        cleaned = re.sub(r"[,.](\d{2})\b", "", raw)
        
        # Now remove all non-digits (spaces, commas, dots as thousands separators)
        cleaned = re.sub(r"[^0-9]", "", cleaned)
        
        if not cleaned:
            continue

        try:
            n = int(cleaned)
        except ValueError:
            continue

        if n > 0:
            return n

    return None


def parse_receipt_message(text: str) -> Optional[ParsedReceipt]:
    aggregator = _detect_aggregator(text)
    if not aggregator:
        return None

    amount = _extract_uzs_amount(text)
    if not amount:
        return None

    return ParsedReceipt(aggregator=aggregator, amount_uzs=amount)


def format_sum_uzs(n: int) -> str:
    try:
        return f"{int(n):,}".replace(",", " ") + " сум"
    except Exception:
        return "0 сум"


def _keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        [
            [KeyboardButton("🧮 Посчитать")],
            [KeyboardButton("🧹 Очистить кэш")],
        ],
        resize_keyboard=True,
        one_time_keyboard=False,
    )


def _base_db_url(db_url: str) -> str:
    return str(db_url).rstrip("/")


def _with_auth(url: str) -> str:
    auth = os.getenv("DATABASE_AUTH", "").strip()
    if not auth:
        return url

    u = urllib.parse.urlparse(url)
    q = dict(urllib.parse.parse_qsl(u.query))
    q["auth"] = auth
    new_q = urllib.parse.urlencode(q)
    return urllib.parse.urlunparse((u.scheme, u.netloc, u.path, u.params, new_q, u.fragment))


def _chat_items_url(db_url: str, chat_id: int) -> str:
    return _with_auth(f"{_base_db_url(db_url)}/chats/{chat_id}/items.json")


def _http_json(url: str, method: str, payload: Any = None) -> Any:
    data = None
    if payload is not None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")

    req = urllib.request.Request(
        url=url,
        method=method,
        data=data,
        headers={"Content-Type": "application/json"},
    )

    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            raw = resp.read().decode("utf-8")
            return json.loads(raw) if raw else None
    except urllib.error.HTTPError as e:
        body = ""
        try:
            body = e.read().decode("utf-8")
        except Exception:
            body = ""
        raise RuntimeError(f"RTDB request failed ({e.code}): {body or e.reason}")


def rtdb_push_item(db_url: str, chat_id: int, item: Dict[str, Any]) -> None:
    _http_json(_chat_items_url(db_url, chat_id), "POST", item)


def rtdb_get_items(db_url: str, chat_id: int) -> List[Dict[str, Any]]:
    data = _http_json(_chat_items_url(db_url, chat_id), "GET")
    if not data:
        return []

    items: List[Dict[str, Any]] = []
    for _id, v in data.items():
        items.append(
            {
                "id": _id,
                "aggregator": v.get("aggregator"),
                "amountUzs": v.get("amountUzs"),
                "createdAt": v.get("createdAt"),
                "raw": v.get("raw"),
            }
        )
    return items


def rtdb_clear_items(db_url: str, chat_id: int) -> None:
    # Firebase requires PUT with null to delete data
    url = _chat_items_url(db_url, chat_id)
    req = urllib.request.Request(
        url=url,
        method="PUT",
        data=b"null",
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            resp.read()
    except urllib.error.HTTPError as e:
        # 400 or 404 is OK if data doesn't exist
        if e.code not in (400, 404):
            raise


def render_items_summary(items: List[Dict[str, Any]]) -> str:
    grouped: Dict[str, Tuple[int, int]] = {}

    for it in items:
        agg = it.get("aggregator") or "Unknown"
        amt = int(it.get("amountUzs") or 0)
        cnt, sm = grouped.get(agg, (0, 0))
        grouped[agg] = (cnt + 1, sm + amt)

    lines = ["📊 <b>Расчёт по агрегаторам:</b>"]
    lines.append("")
    
    for agg, (cnt, sm) in grouped.items():
        lines.append(f"  • {agg}: {cnt} чек = <b>{format_sum_uzs(sm)}</b>")
    
    lines.append("")
    total = sum(int(it.get("amountUzs") or 0) for it in items)
    lines.append(f"💰 <b>ОБЩАЯ СУММА: {format_sum_uzs(total)}</b>")
    
    return "\n".join(lines)


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.effective_message.reply_text(
        "Отправляй сюда чеки Click / Payme / Uzum текстом.\n"
        "Я буду складывать суммы в кэш, а потом посчитаю общую сумму.",
        reply_markup=_keyboard(),
    )


async def cmd_clear(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    db_url = os.environ["FIREBASE_DB_URL"]
    chat_id = update.effective_chat.id
    rtdb_clear_items(db_url, chat_id)
    await update.effective_message.reply_text("Кэш очищен.", reply_markup=_keyboard())


async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    db_url = os.environ["FIREBASE_DB_URL"]
    chat_id = update.effective_chat.id
    text = update.effective_message.text or ""
    user_message_id = update.effective_message.message_id
    chat_type = update.effective_chat.type
    
    # Debug: log all received messages
    print(f"[DEBUG] Message from {chat_id} ({chat_type}): {text[:50]}...")

    # Handle button presses
    if text == "🧮 Посчитать":
        items = rtdb_get_items(db_url, chat_id)
        if not items:
            await update.effective_message.reply_text("📭 Кэш пуст. Отправьте чеки для расчёта.", reply_markup=_keyboard())
            return
        
        # Delete all receipt messages from user
        for item in items:
            msg_id = item.get("messageId")
            if msg_id:
                try:
                    await context.bot.delete_message(chat_id, msg_id)
                except Exception:
                    pass  # Message already deleted or other error
        
        # Calculate summary
        summary = render_items_summary(items)
        
        # Clear Firebase cache
        rtdb_clear_items(db_url, chat_id)

        # Send result and keep keyboard
        await update.effective_message.reply_text(
            f"✅ <b>Сумма посчитана</b>\n\n{summary}",
            parse_mode=ParseMode.HTML,
            reply_markup=_keyboard(),
        )
        return

    if text == "🧹 Очистить кэш":
        rtdb_clear_items(db_url, chat_id)
        await update.effective_message.reply_text("Кэш очищен.", reply_markup=_keyboard())
        return

    # Handle receipt parsing
    parsed = parse_receipt_message(text)
    if not parsed:
        # In private chat, show error
        if update.effective_chat.type == "private":
            await update.effective_message.reply_text(
                "Не понял чек. Пришли текст чека от Click / Payme / Uzum.",
                reply_markup=_keyboard(),
            )
        return

    # Save to Firebase with message_id (fast, no typing action)
    rtdb_push_item(
        db_url,
        chat_id,
        {
            "aggregator": parsed.aggregator,
            "amountUzs": parsed.amount_uzs,
            "createdAt": int(time.time() * 1000),
            "raw": text,
            "messageId": user_message_id,
        },
    )
    
    # If in group, add reaction and reply with amount
    if update.effective_chat.type in ["group", "supergroup"]:
        try:
            # Add fire reaction to the message
            await context.bot.set_message_reaction(
                chat_id=chat_id,
                message_id=user_message_id,
                reaction=[{"type": "emoji", "emoji": "🔥"}],
            )
        except Exception:
            pass  # Reaction might fail if bot doesn't have rights
        
        # Reply with the amount received
        await update.effective_message.reply_text(
            f"🔥 Поступление {parsed.aggregator}: +{format_sum_uzs(parsed.amount_uzs)}",
        )
    else:
        # In private chat, confirm with keyboard
        await update.effective_message.reply_text(
            f"✅ {parsed.aggregator} сохранён",
            reply_markup=_keyboard(),
        )


def main() -> None:
    _load_dotenv(".env")

    bot_token = os.getenv("BOT_TOKEN", "").strip()
    db_url = os.getenv("FIREBASE_DB_URL", "").strip()

    if not bot_token:
        raise RuntimeError("BOT_TOKEN is required")
    if not db_url:
        raise RuntimeError("FIREBASE_DB_URL is required")

    app = Application.builder().token(bot_token).build()

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("clear", cmd_clear))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))

    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
