"""توابع کمکی: ارقام، تاریخ شمسی، عدد به حروف و اعتبارسنجی ورودی‌ها."""
import re
import unicodedata
from datetime import datetime, timedelta, timezone


class InputError(ValueError):
    """پیام خطا برای نمایش به کاربر."""


# ---------- ارقام و متن ----------
_FA = "۰۱۲۳۴۵۶۷۸۹"
_AR = "٠١٢٣٤٥٦٧٨٩"
_TO_EN = {ord(d): str(i) for i, d in enumerate(_FA)}
_TO_EN.update({ord(d): str(i) for i, d in enumerate(_AR)})
_TO_FA = {ord(str(i)): d for i, d in enumerate(_FA)}
_CHAR_FIX = {ord("ي"): "ی", ord("ى"): "ی", ord("ك"): "ک"}
# کاراکترهای کنترلی و کنترل جهت (جلوگیری از به‌هم‌ریختن چیدمان). ZWNJ (200c) مجاز است.
_CTRL = re.compile(r"[\u0000-\u001f\u007f-\u009f\u200b\u200d\u200e\u200f\u202a-\u202e\u2066-\u2069\ufeff]")


def to_en_digits(s: str) -> str:
    return str(s).translate(_TO_EN)


def to_fa_digits(s) -> str:
    return str(s).translate(_TO_FA)


def clean_text(s: str) -> str:
    s = unicodedata.normalize("NFKC", s).translate(_CHAR_FIX)
    s = _CTRL.sub(" ", s)
    return re.sub(r"\s+", " ", s).strip()


def fmt_money(n: int) -> str:
    return to_fa_digits(f"{n:,}")


# ---------- تاریخ شمسی ----------
def gregorian_to_jalali(gy: int, gm: int, gd: int):
    g_d_m = [0, 31, 59, 90, 120, 151, 181, 212, 243, 273, 304, 334]
    gy2 = gy + 1 if gm > 2 else gy
    days = (
        355666 + (365 * gy) + ((gy2 + 3) // 4) - ((gy2 + 99) // 100)
        + ((gy2 + 399) // 400) + gd + g_d_m[gm - 1]
    )
    jy = -1595 + 33 * (days // 12053)
    days %= 12053
    jy += 4 * (days // 1461)
    days %= 1461
    if days > 365:
        jy += (days - 1) // 365
        days = (days - 1) % 365
    if days < 186:
        jm, jd = 1 + days // 31, 1 + days % 31
    else:
        jm, jd = 7 + (days - 186) // 30, 1 + (days - 186) % 30
    return jy, jm, jd


def tehran_now() -> datetime:
    try:
        from zoneinfo import ZoneInfo

        tz = ZoneInfo("Asia/Tehran")
    except Exception:  # tzdata نصب نیست
        tz = timezone(timedelta(hours=3, minutes=30))
    return datetime.now(tz)


def jalali_date_str(dt: datetime) -> str:
    jy, jm, jd = gregorian_to_jalali(dt.year, dt.month, dt.day)
    return f"{jy:04d}/{jm:02d}/{jd:02d}"


# ---------- عدد به حروف ----------
_ONES = ["", "یک", "دو", "سه", "چهار", "پنج", "شش", "هفت", "هشت", "نه", "ده", "یازده",
         "دوازده", "سیزده", "چهارده", "پانزده", "شانزده", "هفده", "هجده", "نوزده"]
_TENS = ["", "", "بیست", "سی", "چهل", "پنجاه", "شصت", "هفتاد", "هشتاد", "نود"]
_HUND = ["", "صد", "دویست", "سیصد", "چهارصد", "پانصد", "ششصد", "هفتصد", "هشتصد", "نهصد"]
_SCALES = ["", "هزار", "میلیون", "میلیارد", "تریلیون"]


def _three(n: int) -> str:
    parts = []
    h, r = divmod(n, 100)
    if h:
        parts.append(_HUND[h])
    if r < 20:
        if r:
            parts.append(_ONES[r])
    else:
        t, o = divmod(r, 10)
        parts.append(_TENS[t])
        if o:
            parts.append(_ONES[o])
    return " و ".join(parts)


def number_to_words(n: int) -> str:
    if n == 0:
        return "صفر"
    groups = []
    i = 0
    while n:
        n, g = divmod(n, 1000)
        if g:
            w = _three(g)
            groups.append(f"{w} {_SCALES[i]}".strip())
        i += 1
    return " و ".join(reversed(groups))


# ---------- اعتبارسنجی ----------
def _digits_only(raw: str) -> str:
    t = re.sub(r"[\s\-_,،٬]", "", to_en_digits(raw))
    if not t.isdigit():
        raise InputError("فقط عدد وارد کنید.")
    return t


def v_text(min_len: int = 2, max_len: int = 60):
    def f(raw: str) -> str:
        t = clean_text(raw)
        if len(t) < min_len:
            raise InputError(f"حداقل {to_fa_digits(min_len)} حرف وارد کنید.")
        if len(t) > max_len:
            raise InputError(f"حداکثر {to_fa_digits(max_len)} حرف مجاز است.")
        return t

    return f


def valid_national_id(code: str) -> bool:
    if not re.fullmatch(r"\d{10}", code) or len(set(code)) == 1:
        return False
    s = sum(int(code[i]) * (10 - i) for i in range(9)) % 11
    return int(code[9]) == (s if s < 2 else 11 - s)


def v_national_id(raw: str) -> str:
    t = _digits_only(raw)
    if len(t) != 10:
        raise InputError("کد ملی باید ۱۰ رقم باشد.")
    if not valid_national_id(t):
        raise InputError("این کد ملی معتبر نیست. دوباره بررسی کنید.")
    return t


def v_license(raw: str) -> str:
    t = _digits_only(raw)
    if not 8 <= len(t) <= 12:
        raise InputError("شماره گواهینامه باید بین ۸ تا ۱۲ رقم باشد.")
    return t


def v_phone(raw: str) -> str:
    t = _digits_only(raw)
    if len(t) == 10 and t.startswith("9"):
        t = "0" + t
    if not (len(t) == 11 and t.startswith("0")):
        raise InputError("شماره تماس را ۱۱ رقمی و با صفر وارد کنید. مثال: ۰۹۱۲۳۴۵۶۷۸۹")
    return t


_EMPTY_WORDS = {"-", "—", "ندارد", "ندارم", "هیچ", "خالی", "نه", "."}


def v_notes(raw: str) -> str:
    t = clean_text(raw)
    if t in _EMPTY_WORDS:
        return ""
    if len(t) > 300:
        raise InputError("توضیحات حداکثر ۳۰۰ حرف می‌تواند باشد.")
    return t


def _amount(raw: str) -> int:
    t = _digits_only(raw)
    if len(t) > 12:
        raise InputError("عدد خیلی بزرگ است.")
    return int(t)


def v_fare(raw: str) -> int:
    n = _amount(raw)
    if n <= 0:
        raise InputError("مبلغ باید بیشتر از صفر باشد.")
    return n


def v_fee(raw: str) -> int:
    if clean_text(raw) in _EMPTY_WORDS:
        return 0
    return _amount(raw)
