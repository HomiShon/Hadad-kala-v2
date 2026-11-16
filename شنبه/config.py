"""تنظیمات از فایل .env یا متغیرهای محیطی خوانده می‌شود."""
import os
import re
import sys
from pathlib import Path

from utils import to_en_digits

BASE_DIR = Path(__file__).resolve().parent

try:
    from dotenv import load_dotenv

    load_dotenv(BASE_DIR / ".env")
except ImportError:  # pragma: no cover
    pass


def _path(name: str, default: str) -> Path:
    p = Path(os.getenv(name, default))
    return p if p.is_absolute() else BASE_DIR / p


def _card(raw: str) -> str:
    raw = to_en_digits(raw).replace(" ", "").replace("-", "").upper()
    return raw


BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
ALLOWED_USER_IDS = {
    int(x) for x in re.split(r"[,\s]+", os.getenv("ALLOWED_USER_IDS", "")) if x.isdigit()
}
COMPANY_NAME = os.getenv("COMPANY_NAME", "کالابار حداد").strip()
ISSUER_PHONES = os.getenv("ISSUER_PHONES", "").strip()
def _default_font() -> str:
    candidates = ([r"C:/Windows/Fonts/arial.ttf", r"C:/Windows/Fonts/tahoma.ttf"] if sys.platform == "win32" else [
        "/usr/share/fonts/truetype/noto/NotoNaskhArabic-Regular.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "fonts/Vazirmatn-Regular.ttf",
    ])
    for candidate in candidates:
        if Path(candidate).is_file():
            return candidate
    return "fonts/Vazirmatn-Regular.ttf"


def _default_bold_font() -> str:
    candidates = ([r"C:/Windows/Fonts/arialbd.ttf", r"C:/Windows/Fonts/tahomabd.ttf"] if sys.platform == "win32" else [
        "/usr/share/fonts/truetype/noto/NotoNaskhArabic-Bold.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        "fonts/Vazirmatn-Bold.ttf",
    ])
    for candidate in candidates:
        if Path(candidate).is_file():
            return candidate
    return _default_font()


FONT_REGULAR = _path("FONT_REGULAR", _default_font())
FONT_BOLD = _path("FONT_BOLD", _default_bold_font())
FOOTER_IMAGE = _path("FOOTER_IMAGE", "assets/footer.png")
DATA_DIR = _path("DATA_DIR", "data")
INVOICE_START = int(os.getenv("INVOICE_START", "1000"))  # شماره فاکتور اول = ۱۰۰۱
INVOICE_PREFIX = os.getenv("INVOICE_PREFIX", "HK-").strip()
DEPLOY_MODE = os.getenv("DEPLOY_MODE", "polling").strip().lower()
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "").strip()


def validate() -> None:
    errors = []
    if not re.fullmatch(r"\d+:[A-Za-z0-9_-]{20,}", BOT_TOKEN):
        errors.append("BOT_TOKEN معتبر در فایل .env تنظیم نشده است.")
    if not ALLOWED_USER_IDS:
        errors.append(
            "ALLOWED_USER_IDS خالی است. این بات اطلاعات حساس (کد ملی و ...) را می‌گیرد "
            "و نباید برای همه باز باشد."
        )
    # FONT_REGULAR can point to a system font or a project-bundled Persian font.
    if not FONT_REGULAR.is_file():
        errors.append(f"فونت فارسی پیدا نشد: {FONT_REGULAR}")
    if DEPLOY_MODE not in {"polling", "webhook"}:
        errors.append("DEPLOY_MODE باید polling یا webhook باشد.")
    if DEPLOY_MODE == "webhook" and not re.fullmatch(r"[A-Za-z0-9_-]{1,256}", WEBHOOK_SECRET):
        errors.append("در حالت webhook، WEBHOOK_SECRET باید ۱ تا ۲۵۶ نویسه از حروف انگلیسی، رقم، _ یا - باشد.")
    if errors:
        raise SystemExit("خطا در تنظیمات:\n- " + "\n- ".join(errors))
