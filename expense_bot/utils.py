"""Shared helpers: logging, retries, image handling, and text/number parsing.

Kept dependency-light and side-effect-free (besides `setup_logging` and the
retry decorator's actual retrying) so it can be unit tested without any
Google or Telegram credentials.
"""

from __future__ import annotations

import asyncio
import functools
import io
import logging
import re
import unicodedata
from decimal import Decimal, InvalidOperation
from logging.handlers import RotatingFileHandler
from typing import Any, Awaitable, Callable, TypeVar

from PIL import Image

logger = logging.getLogger("expense_bot")

T = TypeVar("T")

# Thai solar calendar month names (short and full) mapped to month number.
THAI_MONTHS: dict[str, int] = {
    "ม.ค.": 1, "มกราคม": 1,
    "ก.พ.": 2, "กุมภาพันธ์": 2,
    "มี.ค.": 3, "มีนาคม": 3,
    "เม.ย.": 4, "เมษายน": 4,
    "พ.ค.": 5, "พฤษภาคม": 5,
    "มิ.ย.": 6, "มิถุนายน": 6,
    "ก.ค.": 7, "กรกฎาคม": 7,
    "ส.ค.": 8, "สิงหาคม": 8,
    "ก.ย.": 9, "กันยายน": 9,
    "ต.ค.": 10, "ตุลาคม": 10,
    "พ.ย.": 11, "พฤศจิกายน": 11,
    "ธ.ค.": 12, "ธันวาคม": 12,
}

ENGLISH_MONTHS: dict[str, int] = {
    name.lower(): i
    for i, name in enumerate(
        [
            "January", "February", "March", "April", "May", "June",
            "July", "August", "September", "October", "November", "December",
        ],
        start=1,
    )
}
ENGLISH_MONTHS.update(
    {
        name.lower(): i
        for i, name in enumerate(
            ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"],
            start=1,
        )
    }
)

MONTH_NAMES = [
    "January", "February", "March", "April", "May", "June",
    "July", "August", "September", "October", "November", "December",
]

BUDDHIST_ERA_OFFSET = 543


def setup_logging(log_file: str, level: str = "INFO") -> None:
    """Configure root + module logging: rotating file handler and console."""
    root = logging.getLogger()
    root.setLevel(level)
    root.handlers.clear()

    fmt = logging.Formatter(
        "%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    console = logging.StreamHandler()
    console.setFormatter(fmt)
    root.addHandler(console)

    file_handler = RotatingFileHandler(
        log_file, maxBytes=5 * 1024 * 1024, backupCount=5, encoding="utf-8"
    )
    file_handler.setFormatter(fmt)
    root.addHandler(file_handler)

    # Quiet down noisy third-party loggers.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("google").setLevel(logging.WARNING)
    logging.getLogger("googleapiclient").setLevel(logging.WARNING)


def async_retry(
    *,
    attempts: int = 3,
    base_delay: float = 1.0,
    max_delay: float = 15.0,
    exceptions: tuple[type[BaseException], ...] = (Exception,),
) -> Callable[[Callable[..., Awaitable[T]]], Callable[..., Awaitable[T]]]:
    """Retry an async function with exponential backoff.

    Used around Google API / Telegram calls that may fail transiently
    (rate limits, timeouts, brief outages).
    """

    def decorator(func: Callable[..., Awaitable[T]]) -> Callable[..., Awaitable[T]]:
        @functools.wraps(func)
        async def wrapper(*args: Any, **kwargs: Any) -> T:
            delay = base_delay
            last_exc: BaseException | None = None
            for attempt in range(1, attempts + 1):
                try:
                    return await func(*args, **kwargs)
                except exceptions as exc:  # noqa: BLE001 - intentional broad retry
                    last_exc = exc
                    if attempt == attempts:
                        logger.error(
                            "%s failed after %d attempts: %s", func.__name__, attempts, exc
                        )
                        raise
                    logger.warning(
                        "%s failed (attempt %d/%d): %s - retrying in %.1fs",
                        func.__name__, attempt, attempts, exc, delay,
                    )
                    await asyncio.sleep(delay)
                    delay = min(delay * 2, max_delay)
            raise last_exc  # pragma: no cover - unreachable, satisfies type checker

        return wrapper

    return decorator


def sync_retry(
    *,
    attempts: int = 3,
    base_delay: float = 1.0,
    max_delay: float = 15.0,
    exceptions: tuple[type[BaseException], ...] = (Exception,),
) -> Callable[[Callable[..., T]], Callable[..., T]]:
    """Synchronous counterpart of `async_retry`, for blocking Google API calls."""

    def decorator(func: Callable[..., T]) -> Callable[..., T]:
        @functools.wraps(func)
        def wrapper(*args: Any, **kwargs: Any) -> T:
            import time

            delay = base_delay
            last_exc: BaseException | None = None
            for attempt in range(1, attempts + 1):
                try:
                    return func(*args, **kwargs)
                except exceptions as exc:  # noqa: BLE001
                    last_exc = exc
                    if attempt == attempts:
                        logger.error(
                            "%s failed after %d attempts: %s", func.__name__, attempts, exc
                        )
                        raise
                    logger.warning(
                        "%s failed (attempt %d/%d): %s - retrying in %.1fs",
                        func.__name__, attempt, attempts, exc, delay,
                    )
                    time.sleep(delay)
                    delay = min(delay * 2, max_delay)
            raise last_exc  # pragma: no cover

        return wrapper

    return decorator


def compress_image(data: bytes, max_dimension: int = 2000, quality: int = 85) -> bytes:
    """Downscale and re-encode an image as JPEG to shrink upload size.

    Keeps enough resolution for OCR/legibility while avoiding multi-MB
    phone-camera originals bloating Drive storage. Falls back to the
    original bytes if Pillow cannot decode the image (e.g. already a PDF).
    """
    try:
        with Image.open(io.BytesIO(data)) as img:
            img = img.convert("RGB")
            width, height = img.size
            if max(width, height) > max_dimension:
                scale = max_dimension / max(width, height)
                img = img.resize((int(width * scale), int(height * scale)), Image.LANCZOS)
            buf = io.BytesIO()
            img.save(buf, format="JPEG", quality=quality, optimize=True)
            return buf.getvalue()
    except Exception:  # noqa: BLE001
        logger.warning("compress_image: could not decode image, returning original bytes")
        return data


_AMOUNT_RE = re.compile(r"[-+]?\d[\d,]*(?:\.\d+)?")


def parse_amount(text: str) -> Decimal | None:
    """Parse a currency amount like '1,234.50' or '฿1,234' into a Decimal."""
    if not text:
        return None
    normalized = unicodedata.normalize("NFKC", text)
    match = _AMOUNT_RE.search(normalized)
    if not match:
        return None
    cleaned = match.group(0).replace(",", "")
    try:
        return Decimal(cleaned)
    except InvalidOperation:
        return None


def thai_year_to_gregorian(year: int) -> int:
    """Convert a Buddhist-era year (e.g. 2569) to Gregorian (2026).

    Years already below 2400 are assumed to already be Gregorian.
    """
    return year - BUDDHIST_ERA_OFFSET if year >= 2400 else year


def safe_filename(name: str) -> str:
    """Strip characters that are unsafe for Drive/local filenames."""
    cleaned = re.sub(r"[^\w\-.]+", "_", name, flags=re.UNICODE)
    return cleaned.strip("_") or "file"
