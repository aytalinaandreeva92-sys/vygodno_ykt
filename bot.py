"""
Telegram-бот "Выгодные покупки"
--------------------------------
Пользователь присылает список продуктов, бот ищет их в Google Таблице
и возвращает магазин с самой низкой ценой по каждому товару, а также
помечает реальные скидки (когда "Цена" ниже "Обычной цены").

Структура таблицы (заголовки в первой строке):
    Товар | Магазин | Цена | Обычная цена | Ед. изм. | Обновлено

Столбцы "Обычная цена", "Ед. изм.", "Обновлено" — опциональны.
"""

import logging
import os
import re
from collections import defaultdict

from dotenv import load_dotenv
import gspread
from google.oauth2.service_account import Credentials
from rapidfuzz import fuzz, process
from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import Application, CommandHandler, MessageHandler, ContextTypes, filters

load_dotenv()

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
GOOGLE_SHEET_ID = os.getenv("GOOGLE_SHEET_ID")
GOOGLE_SHEET_NAME = os.getenv("GOOGLE_SHEET_NAME", "Цены")
GOOGLE_CREDENTIALS_PATH = os.getenv("GOOGLE_CREDENTIALS_PATH", "credentials.json")

# Для деплоя на Render (или другой хостинг с webhook).
# Если WEBHOOK_URL задан — бот работает через webhook, иначе через polling (для локального теста).
WEBHOOK_URL = os.getenv("WEBHOOK_URL")  # напр. https://your-service.onrender.com
PORT = int(os.getenv("PORT", "10000"))

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets.readonly",
    "https://www.googleapis.com/auth/drive.readonly",
]

# Порог схожести названий (0-100). Ниже — считаем, что товар не найден.
FUZZY_THRESHOLD = 70

# Кэш данных таблицы, чтобы не дёргать Google API на каждое сообщение.
_sheet_cache = {"rows": None}


def get_sheet_client():
    # На Render удобнее хранить весь JSON сервисного аккаунта в переменной
    # окружения GOOGLE_CREDENTIALS_JSON, а не как отдельный файл.
    creds_json = os.getenv("GOOGLE_CREDENTIALS_JSON")
    if creds_json:
        import json
        info = json.loads(creds_json)
        creds = Credentials.from_service_account_info(info, scopes=SCOPES)
    else:
        creds = Credentials.from_service_account_file(GOOGLE_CREDENTIALS_PATH, scopes=SCOPES)
    return gspread.authorize(creds)


def load_sheet_rows(force_refresh: bool = False):
    """Загружает и кэширует строки таблицы в виде списка словарей."""
    if _sheet_cache["rows"] is not None and not force_refresh:
        return _sheet_cache["rows"]

    client = get_sheet_client()
    sheet = client.open_by_key(GOOGLE_SHEET_ID).worksheet(GOOGLE_SHEET_NAME)
    records = sheet.get_all_records()  # список dict по заголовкам первой строки

    rows = []
    for r in records:
        try:
            name = str(r.get("Товар", "")).strip()
            store = str(r.get("Магазин", "")).strip()
            price = float(str(r.get("Цена", "")).replace(",", "."))
        except (ValueError, TypeError):
            continue
        if not name or not price:
            continue

        usual_price_raw = r.get("Обычная цена", "")
        try:
            usual_price = float(str(usual_price_raw).replace(",", ".")) if usual_price_raw not in ("", None) else None
        except ValueError:
            usual_price = None

        rows.append({
            "name": name,
            "store": store or "—",
            "price": price,
            "usual_price": usual_price,
            "unit": str(r.get("Ед. изм.", "")).strip(),
        })

    _sheet_cache["rows"] = rows
    return rows


def parse_product_list(text: str):
    """Парсит список товаров из сообщения пользователя.

    Поддерживает форматы:
        1. хлеб
        2) молоко
        - сахар
        хлеб, молоко, сахар
        хлеб
        молоко
    """
    # Сначала пробуем построчный разбор
    lines = [line.strip() for line in text.splitlines() if line.strip()]

    items = []
    if len(lines) > 1:
        for line in lines:
            cleaned = re.sub(r"^\s*[\d]+[.)]\s*|^[-*•]\s*", "", line).strip()
            if cleaned:
                items.append(cleaned)
    else:
        # Одна строка — пробуем разделить запятыми
        parts = [p.strip() for p in re.split(r",|;", text) if p.strip()]
        for part in parts:
            cleaned = re.sub(r"^\s*[\d]+[.)]\s*|^[-*•]\s*", "", part).strip()
            if cleaned:
                items.append(cleaned)

    return items


def find_best_matches(product_name: str, rows: list):
    """Находит все строки таблицы, относящиеся к данному товару (fuzzy match)."""
    unique_names = list({r["name"] for r in rows})
    matches = process.extract(
        product_name, unique_names, scorer=fuzz.WRatio, limit=3
    )
    good_names = {name for name, score, _ in matches if score >= FUZZY_THRESHOLD}
    if not good_names:
        return []
    return [r for r in rows if r["name"] in good_names]


def format_result(product_name: str, matched_rows: list) -> str:
    if not matched_rows:
        return f"❓ *{product_name}* — не найдено в таблице."

    matched_rows = sorted(matched_rows, key=lambda r: r["price"])
    best = matched_rows[0]

    unit = f" / {best['unit']}" if best["unit"] else ""
    lines = [f"✅ *{best['name']}*{unit}"]
    lines.append(f"   Дешевле всего: *{best['store']}* — {best['price']:.0f}฿")

    if best["usual_price"] and best["usual_price"] > best["price"]:
        discount_pct = round((1 - best["price"] / best["usual_price"]) * 100)
        lines.append(f"   🔥 Скидка {discount_pct}% (обычно {best['usual_price']:.0f}฿)")

    if len(matched_rows) > 1:
        others = ", ".join(
            f"{r['store']} — {r['price']:.0f}฿" for r in matched_rows[1:4]
        )
        lines.append(f"   Другие варианты: {others}")

    return "\n".join(lines)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Привет! 👋\n\n"
        "Пришли список продуктов, и я найду, где выгоднее купить.\n\n"
        "Например:\n"
        "1. хлеб\n2. молоко\n3. сахар\n\n"
        "Или просто через запятую: хлеб, молоко, сахар\n\n"
        "Команда /refresh — обновить данные из таблицы вручную."
    )


async def refresh(update: Update, context: ContextTypes.DEFAULT_TYPE):
    load_sheet_rows(force_refresh=True)
    await update.message.reply_text("Данные из таблицы обновлены ✅")


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text
    items = parse_product_list(text)

    if not items:
        await update.message.reply_text(
            "Не смог распознать список продуктов. Пришли, например:\n1. хлеб\n2. молоко"
        )
        return

    try:
        rows = load_sheet_rows()
    except Exception as e:
        logger.exception("Ошибка при загрузке таблицы")
        await update.message.reply_text(
            "⚠️ Не удалось загрузить данные из Google Таблицы. Проверьте настройки доступа."
        )
        return

    if not rows:
        await update.message.reply_text("Таблица пуста или данные не в ожидаемом формате.")
        return

    reply_lines = []
    for item in items:
        matches = find_best_matches(item, rows)
        reply_lines.append(format_result(item, matches))

    await update.message.reply_text("\n\n".join(reply_lines), parse_mode=ParseMode.MARKDOWN)


def main():
    if not TELEGRAM_BOT_TOKEN:
        raise SystemExit("Не задан TELEGRAM_BOT_TOKEN в .env")
    if not GOOGLE_SHEET_ID:
        raise SystemExit("Не задан GOOGLE_SHEET_ID в .env")

    app = Application.builder().token(TELEGRAM_BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("refresh", refresh))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    logger.info("Бот запущен")

    if WEBHOOK_URL:
        # Режим для Render / любого хостинга с постоянным HTTPS-адресом.
        # Используем токен как секретный путь вебхука.
        app.run_webhook(
            listen="0.0.0.0",
            port=PORT,
            url_path=TELEGRAM_BOT_TOKEN,
            webhook_url=f"{WEBHOOK_URL.rstrip('/')}/{TELEGRAM_BOT_TOKEN}",
        )
    else:
        # Режим для локального теста.
        app.run_polling()


if __name__ == "__main__":
    main()
