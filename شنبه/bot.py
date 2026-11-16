"""Telegram freight invoice bot. Run with: python bot.py"""
from __future__ import annotations

import logging
import json
import os
import re
from datetime import datetime
from io import BytesIO

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (Application, CallbackQueryHandler, CommandHandler,
                          ContextTypes, ConversationHandler, MessageHandler, filters)

import config
from utils import (fmt_money, jalali_date_str, number_to_words, tehran_now,
                   to_en_digits, to_fa_digits, valid_national_id)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
# Avoid HTTP request logs that could include the bot token in the request URL.
logging.getLogger("httpx").setLevel(logging.WARNING)
log = logging.getLogger(__name__)

COMMON_FIELDS = [
    ("driver_name", "نام راننده"), ("national_id", "کد ملی راننده"),
    ("license", "شماره گواهی‌نامه"), ("plate", "شماره پلاک (۲ رقم، حرف، ۳ رقم، کد دو رقمی)"),
    ("origin", "مبدأ"), ("destination", "مقصد"),
    ("contact_name", "نام تماس‌گیرنده"), ("contact_phone", "شماره تماس‌گیرنده"),
]
PRODUCT_FIELDS = [
    ("cargo", "نوع بار/محصول"), ("packaging", "نوع بسته‌بندی"),
    ("quantity", "مقدار"), ("product_description", "توضیحات محصول (حداکثر ۷۰ نویسه؛ اگر ندارید خط تیره)"),
]
POST_FIELDS = [
    ("transport_amount", "مبلغ حمل‌ونقل (تومان؛ فقط عدد)"),
    ("handling_fee", "هزینه باربری (تومان؛ فقط عدد، در صورت نداشتن ۰)"),
    ("account_holder", "نام صاحب حساب"),
    ("card_number", "شماره کارت ۱۶ رقمی"),
    ("general_description", "توضیحات کلی فاکتور (حداکثر ۱۸۰ نویسه؛ اگر ندارید خط تیره)"),
    ("invoice_number", "شماره فاکتور (برای شمارهٔ پیش‌فرض «خودکار» بفرستید؛ یا شمارهٔ دلخواه وارد کنید)"),
]
FIELDS = COMMON_FIELDS + POST_FIELDS
MAX_ITEMS = 5
FIRST = 0
ITEM_CARGO, ITEM_PACKAGING, ITEM_QUANTITY, ITEM_DESCRIPTION, ITEM_DECISION = range(100, 105)
ITEM_STATES = [ITEM_CARGO, ITEM_PACKAGING, ITEM_QUANTITY, ITEM_DESCRIPTION]
POST_FIRST = 200


def validate_field(key: str, value: str) -> str | None:
    """Return a user-facing validation error, or None when the value is acceptable."""
    if not value:
        return "این فیلد نمی‌تواند خالی باشد."
    max_lengths = {
    "cargo": 60, "packaging": 40, "quantity": 30,
    "product_description": 70,
    "general_description": 180,
    "invoice_number": 30,
    "account_holder": 100,
    "card_number": 16,
}
    if len(value) > max_lengths.get(key, 200):
        return "متن واردشده بیش از حد طولانی است."
    digits = to_en_digits(value)
    if key == "invoice_number":
        if digits.casefold() in {"خودکار", "auto"}:
            return None
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,30}", digits):
            return "شماره فاکتور فقط می‌تواند شامل حروف انگلیسی، رقم، خط تیره یا زیرخط باشد."
        if is_invoice_number_used(digits):
            return "این شماره فاکتور قبلاً استفاده شده است؛ شمارهٔ دیگری وارد کنید."
    elif key == "national_id":
        if not re.fullmatch(r"\d{10}", digits):
            return "کد ملی باید دقیقاً ۱۰ رقم باشد."
        if not valid_national_id(digits):
            return "کد ملی از نظر رقم کنترل معتبر نیست؛ آن را بررسی کنید."
    elif key == "contact_phone":
        normalized = re.sub(r"[\s()-]", "", digits)
        if not re.fullmatch(r"(?:\+98|0098|0)?9\d{9}", normalized):
            return "شماره تماس معتبر نیست؛ شمارهٔ موبایل را با ۰۹ وارد کنید."
    elif key == "card_number":
        card = re.sub(r"[\s-]", "", digits)

        if not re.fullmatch(r"\d{16}", card):
            return "شماره کارت باید دقیقاً ۱۶ رقم باشد."

        total = 0
        for i, digit in enumerate(reversed(card)):
            n = int(digit)
            if i % 2 == 1:
                n *= 2
                if n > 9:
                    n -= 9
            total += n

        if total % 10 != 0:
            return "شماره کارت معتبر نیست؛ لطفاً دوباره بررسی کنید."
    elif key == "license":
        license_digits = re.sub(r"[\s-]", "", digits)
        if not re.fullmatch(r"\d{8,12}", license_digits):
            return "شمارهٔ گواهی‌نامه باید بین ۸ تا ۱۲ رقم باشد."
    elif key == "plate":
        parts = re.findall(r"\d+|[آ-ی]", digits)
        if len(parts) != 4 or [len(part) for part in parts] != [2, 1, 3, 2] or not parts[1].isalpha():
            return "پلاک را به شکل ۷۶ ص ۳۸۱ - ۶۰ وارد کنید (۲ رقم، حرف، ۳ رقم، کد دو رقمی)."
    elif key in {"transport_amount", "handling_fee"}:
        amount = re.sub(r"[\s,،٬]", "", digits)
        if not re.fullmatch(r"\d{1,12}", amount):
            return "مبلغ را فقط با عدد و به تومان وارد کنید؛ جداکننده و واژهٔ تومان ننویسید."
        if key == "transport_amount" and int(amount) <= 0:
            return "مبلغ حمل‌ونقل باید بیشتر از صفر باشد."
    return None


def review_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("تأیید و ساخت PDF", callback_data="confirm"),
         InlineKeyboardButton("اصلاح یک فیلد", callback_data="edit")],
        [InlineKeyboardButton("لغو", callback_data="cancel")],
    ])


async def show_summary(message, answers: dict, items: list[dict]) -> None:
    await message.reply_text("لطفاً اطلاعات را بررسی کنید:\n\n" + summary_text(answers, items)
                             + "\n\nبرای ساخت PDF تأیید کنید یا یک فیلد را اصلاح کنید:",
                             reply_markup=review_keyboard())


def allowed(update: Update) -> bool:
    return bool(update.effective_user and update.effective_user.id in config.ALLOWED_USER_IDS)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not allowed(update):
        await update.effective_message.reply_text("شما اجازه استفاده از این بات را ندارید.")
        return ConversationHandler.END
    context.user_data["invoice"] = {}
    context.user_data.pop("reserved_invoice_number", None)
    context.user_data["common_index"] = 0
    context.user_data["items"] = []
    context.user_data.pop("current_item", None)
    await update.effective_message.reply_text("برای تهیه فاکتور باربری، اطلاعات را یکی‌یکی وارد کنید.\n\n" + COMMON_FIELDS[0][1] + ":")
    return FIRST


async def collect_common(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not allowed(update):
        return ConversationHandler.END
    msg = update.effective_message
    value = (msg.text or "").strip()
    answers = context.user_data["invoice"]
    index = context.user_data["common_index"]
    key, label = COMMON_FIELDS[index]
    value = to_en_digits(value)
    error = validate_field(key, value)
    if error:
        await msg.reply_text(f"فیلد «{label}» مشکل دارد: {error}\nلطفاً همین فیلد را دوباره وارد کنید:")
        return FIRST + index
    answers[key] = value
    index += 1
    context.user_data["common_index"] = index
    if index < len(COMMON_FIELDS):
        await msg.reply_text(COMMON_FIELDS[index][1] + ":")
        return FIRST + index
    context.user_data["items"] = []
    context.user_data["current_item"] = {}
    context.user_data["item_step"] = 0
    await prompt_item(msg, context)
    return ITEM_CARGO


async def prompt_item(message, context: ContextTypes.DEFAULT_TYPE) -> None:
    row_number = len(context.user_data.get("items", [])) + 1
    _, label = PRODUCT_FIELDS[context.user_data.get("item_step", 0)]
    await message.reply_text(f"ردیف محصول {to_fa_digits(row_number)} - {label}:")


async def collect_item(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not allowed(update):
        return ConversationHandler.END
    msg = update.effective_message
    step = context.user_data["item_step"]
    key, label = PRODUCT_FIELDS[step]
    value = to_en_digits((msg.text or "").strip())
    error = validate_field(key, value)
    if error:
        await msg.reply_text(f"فیلد «{label}» در این ردیف مشکل دارد: {error}\nلطفاً همین مورد را دوباره وارد کنید:")
        return ITEM_STATES[step]
    context.user_data["current_item"][key] = value
    step += 1
    if step < len(PRODUCT_FIELDS):
        context.user_data["item_step"] = step
        await prompt_item(msg, context)
        return ITEM_STATES[step]

    items = context.user_data["items"]
    items.append(context.user_data.pop("current_item"))
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("➕ افزودن ردیف محصول", callback_data="item:add")],
        [InlineKeyboardButton("✅ پایان ثبت محصولات", callback_data="item:done")],
    ])
    await msg.reply_text(
        f"ردیف محصول {to_fa_digits(len(items))} ثبت شد. محصول دیگری هم دارید؟ (حداکثر {to_fa_digits(MAX_ITEMS)} ردیف)",
        reply_markup=keyboard,
    )
    return ITEM_DECISION


async def item_decision(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    if not allowed(update):
        await query.edit_message_text("شما اجازه استفاده از این بات را ندارید.")
        return ConversationHandler.END
    if query.data == "item:add":
        items = context.user_data.get("items", [])
        if len(items) >= MAX_ITEMS:
            await query.edit_message_text(f"حداکثر {to_fa_digits(MAX_ITEMS)} ردیف محصول مجاز است.",
                                          reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("ادامه به مبالغ", callback_data="item:done")]]))
            return ITEM_DECISION
        context.user_data["current_item"] = {}
        context.user_data["item_step"] = 0
        await query.edit_message_text(f"ردیف محصول {to_fa_digits(len(items) + 1)}")
        await prompt_item(query.message, context)
        return ITEM_CARGO

    context.user_data["post_index"] = 0
    await query.edit_message_text("ثبت محصولات تمام شد.")
    await query.message.reply_text(POST_FIELDS[0][1] + ":")
    return POST_FIRST


async def collect_post(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not allowed(update):
        return ConversationHandler.END
    msg = update.effective_message
    answers = context.user_data["invoice"]
    index = context.user_data["post_index"]
    key, label = POST_FIELDS[index]
    value = to_en_digits((msg.text or "").strip())
    error = validate_field(key, value)
    if error:
        await msg.reply_text(f"فیلد «{label}» مشکل دارد: {error}\nلطفاً همین فیلد را دوباره وارد کنید:")
        return POST_FIRST + index
    if key in {"transport_amount", "handling_fee"}:
        value = re.sub(r"[\s,،٬]", "", value)
    if key == "card_number":
        value = re.sub(r"[\s-]", "", value)
    if key == "invoice_number" and value.casefold() in {"خودکار", "auto"}:
        value = next_invoice_number()
        context.user_data["reserved_invoice_number"] = value
    answers[key] = value
    index += 1
    context.user_data["post_index"] = index
    if index < len(POST_FIELDS):
        await msg.reply_text(POST_FIELDS[index][1] + ":")
        return POST_FIRST + index
    await show_summary(msg, answers, context.user_data["items"])
    return ConversationHandler.END  # callbacks manage the confirmation phase


def summary_text(a: dict, items: list[dict]) -> str:
    lines = [f"شماره فاکتور: {a['invoice_number']}"]
    for key, label in COMMON_FIELDS:
        value = a.get(key, "")
        if key == "plate" and value:
            value = format_plate(value)
        elif key in {"national_id", "license", "contact_phone"} and value:
            value = to_fa_digits(value)
        lines.append(f"{label}: {value}")
    for index, item in enumerate(items, start=1):
        lines.append(f"ردیف محصول {to_fa_digits(index)}:")
        for key, label in PRODUCT_FIELDS:
            value = item.get(key, "")
            if key == "product_description" and value in {"-", "—"}:
                value = "ندارد"
            elif key == "quantity":
                value = format_quantity(value)
            lines.append(f"  {label}: {value}")
    for key, label in POST_FIELDS:
        if key in {"invoice_number", "general_description"}:
            continue
        value = a.get(key, "")
        if key in {"transport_amount", "handling_fee"} and value:
            value = f"{fmt_money(int(value))} تومان"
        lines.append(f"{label}: {value}")
    lines.append(f"توضیحات کلی: {a.get('general_description', '')}")
    if a.get("transport_amount") and a.get("handling_fee"):
        lines.append(f"جمع کل: {fmt_money(int(a['transport_amount']) + int(a['handling_fee']))} تومان")
    lines += [
        f"شماره کارت: {to_fa_digits(a['card_number'])}",
        f"نام صاحب حساب: {a['account_holder']}"
    ]
    return "\n".join(lines)


def next_invoice_number() -> str:
    """Persist the next sequence value so restarts never reuse invoice numbers."""
    config.DATA_DIR.mkdir(parents=True, exist_ok=True)
    counter = config.DATA_DIR / "invoice-counter.json"
    last = config.INVOICE_START
    if counter.exists():
        try:
            last = int(json.loads(counter.read_text(encoding="utf-8"))["last"])
        except (ValueError, KeyError, TypeError, json.JSONDecodeError) as exc:
            raise RuntimeError(f"Invoice counter file is invalid: {counter}") from exc
    used = _read_used_invoice_numbers()
    current = last + 1
    candidate = f"{config.INVOICE_PREFIX}{current:06d}"
    while candidate in used:
        current += 1
        candidate = f"{config.INVOICE_PREFIX}{current:06d}"
    temp = counter.with_suffix(".tmp")
    temp.write_text(json.dumps({"last": current}), encoding="utf-8")
    os.replace(temp, counter)
    reserve_invoice_number(candidate)
    return candidate


def _invoice_numbers_file():
    return config.DATA_DIR / "invoice-numbers.json"


def _read_used_invoice_numbers() -> set[str]:
    path = _invoice_numbers_file()
    if not path.exists():
        return set()
    try:
        values = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(values, list) or not all(isinstance(item, str) for item in values):
            raise ValueError("invalid invoice number list")
        return {to_en_digits(item).strip() for item in values}
    except (ValueError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"Invoice number registry is invalid: {path}") from exc


def is_invoice_number_used(value: str) -> bool:
    return to_en_digits(value).strip() in _read_used_invoice_numbers()


def reserve_invoice_number(value: str) -> bool:
    normalized = to_en_digits(value).strip()
    config.DATA_DIR.mkdir(parents=True, exist_ok=True)
    path = _invoice_numbers_file()
    used = _read_used_invoice_numbers()
    if normalized in used:
        return False
    temp = path.with_suffix(".tmp")
    temp.write_text(json.dumps(sorted(used | {normalized}), ensure_ascii=False), encoding="utf-8")
    os.replace(temp, path)
    return True


def missing_invoice_fields(answers: dict, items: list[dict]) -> list[str]:
    missing = [label for key, label in FIELDS if not answers.get(key)]
    if not items:
        missing.append("حداقل یک ردیف محصول")
    for index, item in enumerate(items, start=1):
        for key, label in PRODUCT_FIELDS:
            if not item.get(key):
                missing.append(f"ردیف {to_fa_digits(index)}: {label}")
    return missing


def edit_keyboard(items: list[dict]) -> InlineKeyboardMarkup:
    buttons = [[InlineKeyboardButton(label, callback_data=f"edit:f:{key}")]
               for key, label in COMMON_FIELDS]
    for index in range(len(items)):
        buttons.extend([[InlineKeyboardButton(f"ردیف {to_fa_digits(index + 1)} - {label}",
                                               callback_data=f"edit:i:{index}:{key}")]
                        for key, label in PRODUCT_FIELDS])
    buttons.extend([[InlineKeyboardButton(label, callback_data=f"edit:f:{key}")]
                    for key, label in POST_FIELDS])
    buttons.append([InlineKeyboardButton("بازگشت به خلاصه", callback_data="review")])
    return InlineKeyboardMarkup(buttons)


async def action(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    if not allowed(update):
        await query.edit_message_text("شما اجازه استفاده از این بات را ندارید.")
        return
    if query.data == "cancel":
        context.user_data.pop("invoice", None)
        context.user_data.pop("items", None)
        context.user_data.pop("current_item", None)
        context.user_data.pop("reserved_invoice_number", None)
        await query.edit_message_text("درخواست لغو شد. برای شروع /new را بفرستید.")
    elif query.data == "edit":
        items = context.user_data.get("items", [])
        await query.edit_message_text("فیلد یا ردیف محصولی را که می‌خواهید اصلاح کنید انتخاب کنید:",
                                      reply_markup=edit_keyboard(items))
    elif query.data == "review":
        answers = context.user_data.get("invoice", {})
        items = context.user_data.get("items", [])
        missing = missing_invoice_fields(answers, items)
        if missing:
            await query.edit_message_text("اطلاعات این فیلدها موجود نیست: " + "، ".join(missing)
                                          + "\nبرای تکمیل فرم /new را بفرستید.")
            return
        await query.edit_message_text("خلاصه فاکتور:")
        await show_summary(query.message, answers, items)
    elif query.data.startswith("edit:"):
        parts = query.data.split(":")
        answers = context.user_data.get("invoice", {})
        items = context.user_data.get("items", [])
        if len(parts) == 3 and parts[1] == "f":
            key = parts[2]
            field = next(((k, label) for k, label in FIELDS if k == key), None)
            editing = {"group": "field", "key": key}
            current = answers.get(key, "")
        elif len(parts) == 4 and parts[1] == "i" and parts[2].isdigit():
            item_index, key = int(parts[2]), parts[3]
            field = next(((k, label) for k, label in PRODUCT_FIELDS if k == key), None)
            if item_index >= len(items):
                field = None
            editing = {"group": "item", "index": item_index, "key": key}
            current = items[item_index].get(key, "") if item_index < len(items) else ""
        else:
            field = None
            editing = {}
            current = ""
        if field is None or missing_invoice_fields(answers, items):
            await query.edit_message_text("اطلاعات فرم در دسترس نیست. برای شروع /new را بفرستید.")
            return
        context.user_data["editing_field"] = editing
        await query.edit_message_text(f"مقدار فعلی «{field[1]}»: {current}\n"
                                      "مقدار جدید همین فیلد را بفرستید؛ بقیه اطلاعات حفظ می‌شوند.")
    elif query.data == "confirm":
        answers = context.user_data.get("invoice")
        items = context.user_data.get("items", [])
        missing = missing_invoice_fields(answers or {}, items)
        if missing:
            await query.edit_message_text("فاکتور کامل نیست. موارد ناقص: " + "، ".join(missing)
                                          + "\nبرای تکمیل، /new را بفرستید.")
            return
        try:
            pdf = make_pdf(answers, items)
        except Exception:
            log.exception("PDF generation failed")
            await query.edit_message_text("ساخت PDF ناموفق بود. فونت فارسی و تنظیمات را بررسی کنید.")
            return
        if answers["invoice_number"] != context.user_data.get("reserved_invoice_number"):
            if not reserve_invoice_number(answers["invoice_number"]):
                await query.edit_message_text("این شماره فاکتور قبلاً صادر شده است. از «اصلاح یک فیلد» شمارهٔ دیگری انتخاب کنید.",
                                              reply_markup=review_keyboard())
                return
        filename = f"invoice-{datetime.now().strftime('%Y%m%d-%H%M%S')}.pdf"
        await query.edit_message_text("فاکتور تأیید شد و PDF آماده است.")
        await query.message.reply_document(document=pdf, filename=filename, caption="فاکتور باربری")
        context.user_data.pop("invoice", None)
        context.user_data.pop("items", None)
        context.user_data.pop("reserved_invoice_number", None)


async def edit_value(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not allowed(update):
        return
    editing = context.user_data.get("editing_field")
    if not editing:
        return
    msg = update.effective_message
    value = to_en_digits((msg.text or "").strip())
    key = editing["key"]
    field_list = PRODUCT_FIELDS if editing["group"] == "item" else FIELDS
    field = next(((k, label) for k, label in field_list if k == key), None)
    if field is None:
        context.user_data.pop("editing_field", None)
        await msg.reply_text("فیلد انتخاب‌شده پیدا نشد. از خلاصه دوباره انتخاب کنید.")
        return
    existing_value = context.user_data.get("invoice", {}).get(key) if editing["group"] == "field" else None
    error = None if key == "invoice_number" and value == existing_value else validate_field(key, value)
    if error:
        await msg.reply_text(f"فیلد «{field[1]}» مشکل دارد: {error}\nلطفاً مقدار همین فیلد را دوباره بفرستید:")
        return
    if key in {"transport_amount", "handling_fee"}:
        value = re.sub(r"[\s,،٬]", "", value)
    if key == "card_number":
        value = re.sub(r"[\s-]", "", value)
    if key == "invoice_number" and value.casefold() in {"خودکار", "auto"}:
        value = next_invoice_number()
        context.user_data["reserved_invoice_number"] = value
    answers = context.user_data.get("invoice", {})
    if editing["group"] == "item":
        context.user_data["items"][editing["index"]][key] = value
    else:
        answers[key] = value
    context.user_data.pop("editing_field", None)
    await msg.reply_text(f"فیلد «{field[1]}» اصلاح شد.")
    await show_summary(msg, answers, context.user_data.get("items", []))


def make_pdf(a: dict, items: list[dict]) -> BytesIO:
    from reportlab.lib import colors
    from reportlab.lib.enums import TA_RIGHT
    from reportlab.lib.pagesizes import A5
    from reportlab.lib.styles import ParagraphStyle
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont
    from reportlab.lib.utils import ImageReader
    from reportlab.platypus import Paragraph
    from xml.sax.saxutils import escape
    try:
        import arabic_reshaper
        from bidi.algorithm import get_display
    except ImportError as exc:
        raise RuntimeError("Install arabic-reshaper and python-bidi") from exc
    if not config.FONT_REGULAR.is_file():
        raise RuntimeError(f"Persian font not found: {config.FONT_REGULAR}")
    if "Hadad" not in pdfmetrics.getRegisteredFontNames():
        pdfmetrics.registerFont(TTFont("Hadad", str(config.FONT_REGULAR)))
    if "Hadad-Bold" not in pdfmetrics.getRegisteredFontNames():
        bold_font = config.FONT_BOLD if config.FONT_BOLD.is_file() else config.FONT_REGULAR
        pdfmetrics.registerFont(TTFont("Hadad-Bold", str(bold_font)))
    def rtl(value: str) -> str:
        return get_display(arabic_reshaper.reshape(str(value)))
    output = BytesIO()
    width, height = A5
    margin = 23
    content_w = width - 2 * margin
    canvas = __import__("reportlab.pdfgen.canvas", fromlist=["Canvas"]).Canvas(output, pagesize=A5)
    canvas.setTitle(rtl("رسید و بیجک کرایه - " + a["invoice_number"]))

    def draw_text(text, x, y, size=8, align="right", color=colors.HexColor("#17212B"), bold=False):
        canvas.setFillColor(color)
        canvas.setFont("Hadad-Bold" if bold and "Hadad-Bold" in pdfmetrics.getRegisteredFontNames() else "Hadad", size)
        shaped = rtl(str(text))
        if align == "center":
            canvas.drawCentredString(x, y, shaped)
        elif align == "left":
            canvas.drawString(x, y, shaped)
        else:
            canvas.drawRightString(x, y, shaped)

    def paragraph(text, x, y, w, h, size=8, align=TA_RIGHT, color=colors.HexColor("#17212B"), bold=False):
        font_name = "Hadad-Bold" if bold and "Hadad-Bold" in pdfmetrics.getRegisteredFontNames() else "Hadad"
        font_size = size
        available_h = max(1, h - 2)
        while True:
            style = ParagraphStyle(
                f"p{font_size}{align}{bold}", fontName=font_name,
                fontSize=font_size, leading=font_size + 2, alignment=align, textColor=color, wordWrap="CJK",
            )
            p = Paragraph(rtl(escape(str(text))), style)
            _, ph = p.wrap(w - 6, available_h)
            if ph <= available_h or font_size <= 6:
                break
            font_size = max(6, font_size - .5)
        p.drawOn(canvas, x + 4, y + max(2, (h - ph) / 2))

    def draw_slash_sequence(parts, center_x, baseline, size, bold=True):
        """Draw numeric date parts left-to-right with vector slashes between them."""
        font_name = "Hadad-Bold" if bold else "Hadad"
        widths = [pdfmetrics.stringWidth(str(part), font_name, size) for part in parts]
        slash_slot = size * .75
        cursor = center_x - (sum(widths) + slash_slot * (len(parts) - 1)) / 2
        canvas.setFillColor(colors.HexColor("#17212B"))
        canvas.setFont(font_name, size)
        for index, part in enumerate(parts):
            canvas.drawString(cursor, baseline, str(part))
            cursor += widths[index]
            if index < len(parts) - 1:
                slash_x = cursor + slash_slot / 2
                canvas.setLineWidth(.9)
                canvas.line(slash_x - 2, baseline - 1, slash_x + 2, baseline + size * .72)
                cursor += slash_slot

    def draw_quantity(value, x, y, w, h, size=10.5):
        """Render x/y quantities with a drawn slash so RTL shaping cannot drop it."""
        match = re.fullmatch(r"\s*(\d+)\s*/\s*(\d+)\s*(.*?)\s*", to_en_digits(value))
        if not match:
            paragraph(to_fa_digits(value), x, y, w, h, size=size, align=1, bold=True)
            return
        numerator, denominator, unit = match.groups()
        font_name = "Hadad-Bold"
        fa_numerator, fa_denominator = to_fa_digits(numerator), to_fa_digits(denominator)
        shaped_unit = rtl(unit) if unit else ""
        unit_width = pdfmetrics.stringWidth(shaped_unit, font_name, size)
        numerator_width = pdfmetrics.stringWidth(fa_numerator, font_name, size)
        denominator_width = pdfmetrics.stringWidth(fa_denominator, font_name, size)
        unit_gap, slash_slot = (5 if unit else 0), size * .75
        total_width = unit_width + unit_gap + denominator_width + slash_slot + numerator_width
        cursor = x + (w - total_width) / 2
        baseline = y + (h - size) / 2 + 1
        canvas.setFillColor(colors.HexColor("#17212B"))
        canvas.setFont(font_name, size)
        if unit:
            canvas.drawString(cursor, baseline, shaped_unit)
            cursor += unit_width + unit_gap
        # Place numerator first visually; otherwise RTL users see 5/2 for 2/5.
        canvas.drawString(cursor, baseline, fa_numerator)
        cursor += numerator_width
        slash_x = cursor + slash_slot / 2
        canvas.setLineWidth(.9)
        canvas.line(slash_x - 2, baseline - 1, slash_x + 2, baseline + size * .72)
        cursor += slash_slot
        canvas.drawString(cursor, baseline, fa_denominator)

    def draw_cell(x, y, w, h, label, value, fill=None, size=9, label_size=7.2):
        canvas.setStrokeColor(colors.HexColor("#929AA1"))
        if fill:
            canvas.setFillColor(fill)
            canvas.rect(x, y, w, h, fill=1, stroke=1)
        else:
            canvas.rect(x, y, w, h, fill=0, stroke=1)
        draw_text(label, x + w - 5, y + h - 10, label_size, color=colors.HexColor("#58636D"))
        paragraph(value, x + 3, y + 2, w - 6, h - 12, size=size, bold=True)

    # Title and date/time are placed first, matching the supplied receipt sample.
    draw_text("رسید و بیجک کرایه", width / 2, height - 27, 16, align="center", bold=True)
    now = tehran_now()
    jalali_year, jalali_month, jalali_day = jalali_date_str(now).split('/')
    date_y = height - 50
    date_right = width - margin
    draw_text("تاریخ:", date_right, date_y, 9)
    date_label_width = pdfmetrics.stringWidth(rtl("تاریخ:"), "Hadad", 9)
    # Render year/month/day left-to-right so Persian reading from right shows day/month/year.
    date_parts = [to_fa_digits(jalali_year), to_fa_digits(jalali_month), to_fa_digits(jalali_day)]
    date_width = sum(pdfmetrics.stringWidth(part, "Hadad-Bold", 9) for part in date_parts) + 2 * 9 * .75
    date_center = date_right - date_label_width - 5 - date_width / 2
    draw_slash_sequence(date_parts, date_center, date_y, 9)
    draw_text(f"ساعت: {to_fa_digits(now.strftime('%H:%M'))}", date_center - date_width / 2 - 7, date_y, 9)
    canvas.setFillColor(colors.HexColor("#17212B"))
    canvas.setFont("Hadad-Bold", 12)
    canvas.drawRightString(margin + 100, height - 50, a["invoice_number"])
    draw_text("شماره فاکتور:", margin + 145, height - 50, 7.5)

    # Driver, identity, route and contact details in a compact two-column box.
    col_w = content_w / 2
    row_h = 36
    info_top = height - 62
    cells = [
        ("national_id", "کد ملی راننده"), ("driver_name", "نام راننده"),
        ("plate", "شماره پلاک"), ("license", "شماره گواهی‌نامه"),
        ("contact_phone", "تلفن تماس‌گیرنده"), ("contact_name", "نام تماس‌گیرنده"),
        ("destination", "مقصد"), ("origin", "مبدأ"),
    ]
    info_bottom = info_top - 4 * row_h
    canvas.setStrokeColor(colors.HexColor("#737D84"))
    canvas.setLineWidth(.8)
    canvas.rect(margin, info_bottom, content_w, 4 * row_h, fill=0, stroke=1)
    for row in range(4):
        y = info_top - (row + 1) * row_h
        for col in range(2):
            key, label = cells[row * 2 + col]
            x = margin + col * col_w
            draw_text(label + ":", x + col_w - 8, y + row_h - 11, 7.5, color=colors.HexColor("#58636D"))
            if key == "plate":
                parts = parse_plate(a[key])
                px, py, ph = x + 5, y + 2, 22
                widths = [23, 22, 37, 25]
                for part, pw in zip(parts, widths):
                    canvas.setFillColor(colors.white)
                    canvas.setStrokeColor(colors.HexColor("#657078"))
                    canvas.rect(px, py, pw, ph, fill=1, stroke=1)
                    draw_text(to_fa_digits(part), px + pw / 2, py + 4.5, 14, align="center", bold=True)
                    px += pw
            else:
                value = to_fa_digits(a[key]) if key in {"national_id", "license", "contact_phone"} else a[key]
                paragraph(value, x + 3, y + 2, col_w - 6, row_h - 12, size=12, bold=True)

    # Cargo table, five rows as in the reference, with the first row populated.
    table_top_y = info_top - 4 * row_h - 8
    headers = ["توضیحات", "مقدار", "نوع بسته‌بندی", "نوع بار", "ردیف"]
    col_widths = [content_w * .32, content_w * .13, content_w * .17, content_w * .30, content_w * .08]
    table_h = 108
    header_h = 19
    row_height = (table_h - header_h) / 5
    x_positions = [margin]
    for cw in col_widths[:-1]:
        x_positions.append(x_positions[-1] + cw)
    for idx, (label, cw, x) in enumerate(zip(headers, col_widths, x_positions)):
        y = table_top_y - header_h
        canvas.setFillColor(colors.HexColor("#EDF1F3"))
        canvas.rect(x, y, cw, header_h, fill=1, stroke=1)
        draw_text(label, x + cw / 2, y + 5, 7.5, align="center")
    for row in range(5):
        y = table_top_y - header_h - (row + 1) * row_height
        for idx, (cw, x) in enumerate(zip(col_widths, x_positions)):
            canvas.rect(x, y, cw, row_height, fill=0, stroke=1)
            if row < len(items):
                item = items[row]
                values = [item.get("product_description", "") or "-", format_quantity(item["quantity"]),
                          item["packaging"], item["cargo"], to_fa_digits(row + 1)]
                if idx == 1:
                    draw_quantity(item["quantity"], x + 2, y, cw - 4, row_height, size=10.5)
                else:
                    paragraph(values[idx], x + 2, y, cw - 4, row_height, size=10.5, align=1, bold=True)

    # Two requested charge fields and their calculated total.
    table_bottom_y = table_top_y - table_h
    amount_top_y = table_bottom_y - 8
    amount_h = 25
    transport = int(a["transport_amount"])
    handling = int(a["handling_fee"])
    amount_rows = [
        ("مبلغ حمل‌ونقل", transport),
        ("هزینه باربری", handling),
        ("جمع کل", transport + handling),
    ]
    for i, (label, amount) in enumerate(amount_rows):
        y = amount_top_y - (i + 1) * amount_h
        canvas.setStrokeColor(colors.HexColor("#929AA1"))
        fill = colors.HexColor("#E9EEF1") if i == 2 else colors.white
        canvas.setFillColor(fill)
        canvas.rect(margin, y, content_w, amount_h, fill=1, stroke=1)
        if i < 2:
            draw_text(label + ":", width - margin - 8, y + 8, 7.5)
            draw_text(f"{fmt_money(amount)} تومان", margin + 8, y + 7, 12, align="left", bold=True)
            words = number_to_words(amount) + " تومان"
            draw_text(words, margin + content_w * .52, y + 8, 8.3, align="center", bold=True)
        else:
            split = content_w * .43
            canvas.line(margin + split, y, margin + split, y + amount_h)
            draw_text("جمع کل:", width - margin - 8, y + 8, 7.5)
            draw_text(f"{fmt_money(amount)} تومان", margin + split + (content_w - split) / 2,
                      y + 7, 12, align="center", bold=True)
            words = number_to_words(amount) + " تومان"
            draw_text("به حروف:", margin + split - 7, y + 8, 7.5, align="right")
            draw_text(words, margin + split / 2, y + 8, 8.3, align="center", bold=True)

    general_top_y = amount_top_y - 3 * amount_h - 8
    general_h = 44
    draw_cell(margin, general_top_y - general_h, content_w, general_h,
              "توضیحات کلی فاکتور", a.get("general_description", "") or "-",
              colors.HexColor("#F7F8F8"), size=10.5, label_size=7.5)

    bank_top_y = general_top_y - general_h - 8
    bank_h = 36
    bank_y = bank_top_y - bank_h
    canvas.setStrokeColor(colors.HexColor("#929AA1"))
    canvas.setFillColor(colors.HexColor("#F7F8F8"))
    canvas.rect(margin, bank_y, content_w, bank_h, fill=1, stroke=1)
    canvas.setStrokeColor(colors.HexColor("#C1C8CD"))
    canvas.line(margin + content_w / 2, bank_y, margin + content_w / 2, bank_y + bank_h)
    half = content_w / 2 
    draw_text("شماره کارت", margin + content_w - 7, bank_y + bank_h - 10, 7.5,
             color=colors.HexColor("#58636D"))

    paragraph(to_fa_digits(a["card_number"]), margin + half + 2, bank_y + 2,
              half - 4, bank_h - 13, size=12, bold=True)

    draw_text("نام صاحب حساب", margin + half - 7, bank_y + bank_h - 10, 7.5,
              color=colors.HexColor("#58636D"))

    paragraph(a["account_holder"], margin + 2, bank_y + 2,
              half - 4, bank_h - 13, size=12, bold=True)

    # The supplied brand image already contains the business name and signature.
    footer_top_y = bank_top_y - bank_h - 8
    footer_h = 62
    logo = config.BASE_DIR / "assets" / "header.png"
    if logo.is_file():
        img = ImageReader(str(logo))
        iw, ih = img.getSize()
        scale = min((content_w * .58) / iw, footer_h / ih)
        lw, lh = iw * scale, ih * scale
        canvas.drawImage(img, margin, footer_top_y - lh, width=lw, height=lh, mask="auto")
    draw_text("امضای تحویل‌گیرنده", width - margin, footer_top_y - 16, 7)
    canvas.setStrokeColor(colors.HexColor("#69737A"))
    canvas.line(width - margin - 105, footer_top_y - 39, width - margin, footer_top_y - 39)
    draw_text("ارسال کالا به تمام نقاط کشور", width / 2, 14, 6.5, align="center", color=colors.HexColor("#58636D"))
    canvas.showPage()
    canvas.save()
    output.seek(0)
    return output


def parse_plate(value: str) -> list[str]:
    parts = re.findall(r"\d+|[آ-ی]", to_en_digits(value))
    if len(parts) != 4 or [len(part) for part in parts] != [2, 1, 3, 2]:
        raise ValueError("Invalid Iranian plate format")
    return parts


def format_plate(value: str) -> str:
    parts = parse_plate(value)
    return f"{to_fa_digits(parts[0])} {parts[1]} {to_fa_digits(parts[2])} - {to_fa_digits(parts[3])}"


def format_quantity(value: str) -> str:
    """Keep slash-separated quantities visually distinct in Telegram text."""
    value = to_fa_digits(value)
    return re.sub(r"(?<=\d)\s*/\s*(?=\d)", " / ", value)


async def cancel_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data.pop("invoice", None)
    context.user_data.pop("items", None)
    context.user_data.pop("current_item", None)
    context.user_data.pop("reserved_invoice_number", None)
    await update.effective_message.reply_text("درخواست لغو شد. برای شروع دوباره /new را بفرستید.")
    return ConversationHandler.END


def main() -> None:
    config.validate()
    app = Application.builder().token(config.BOT_TOKEN).build()
    states = {
        **{FIRST + i: [MessageHandler(filters.TEXT & ~filters.COMMAND, collect_common)]
           for i in range(len(COMMON_FIELDS))},
        **{state: [MessageHandler(filters.TEXT & ~filters.COMMAND, collect_item)] for state in ITEM_STATES},
        ITEM_DECISION: [CallbackQueryHandler(item_decision, pattern="^item:(add|done)$")],
        **{POST_FIRST + i: [MessageHandler(filters.TEXT & ~filters.COMMAND, collect_post)]
           for i in range(len(POST_FIELDS))},
    }
    conv = ConversationHandler(
        entry_points=[CommandHandler(["start", "new"], start)],
        states=states,
        fallbacks=[CommandHandler("cancel", cancel_command)],
        allow_reentry=True,
    )
    app.add_handler(conv)
    app.add_handler(CallbackQueryHandler(action, pattern="^(confirm|cancel|edit|review|edit:.*)$"))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, edit_value))
    if config.DEPLOY_MODE == "webhook":
        hostname = os.getenv("RENDER_EXTERNAL_HOSTNAME", "").strip()
        if not hostname:
            raise SystemExit("Render did not provide RENDER_EXTERNAL_HOSTNAME.")
        port = int(os.getenv("PORT", "10000"))
        app.run_webhook(
            listen="0.0.0.0",
            port=port,
            url_path="telegram",
            webhook_url=f"https://{hostname}/telegram",
            secret_token=config.WEBHOOK_SECRET,
            bootstrap_retries=-1,
        )
    else:
        app.run_polling()


if __name__ == "__main__":
    main()
