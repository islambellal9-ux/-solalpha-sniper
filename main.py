# -*- coding: utf-8 -*-
"""
SolAlpha Sniper v5.0 — Telegram bot (v4.4 + مسح سوق 24/24 مستقل: عملات ناشئة + مُثبّتة + تنظيف تلقائي) (Render Web Service)
مصدر العملات: قناة الإشارات https://t.me/solhousesignal حصرياً.
طريقة القراءة (بالترتيب): 0) الوضع اليدوي: ترسل للبوت عقوداً أو تحوّل (Forward) رسائل الإشارات فيحللها /best (لا يحتاج أي إعداد)
                          1) Telethon بجلسة مستخدم (TELETHON_SESSION) — الأوثق
                          2) HTTP على مرايا المعاينة العامة (t.me/s وأخواتها) كاحتياط
ملاحظة: حسابات البوت لا تستطيع قراءة سجل القنوات (BotMethodInvalidError) فلا تُستخدم.

/best : يقرأ آخر رسائل القناة → يستخرج عقود Solana → يفحصها ويعطيك أفضل صفقة + نوعها + بدائل
/diag : تشخيص مصدر القراءة (Telethon/HTTP) وفلترة العقود
/help : شرح

أنواع الصفقات:
  ⚡ SCALP    : دخول من Fibonacci Golden Zone (0.5–0.618)، هدف +18% / +25%
  🚀 MOMENTUM : عملة تصعد بفوليوم متسارع، هدف +15% / +30%
  🧭 HOLD     : عملة أقدم (24h+) بسيولة وفوليوم جيدين، هدف +50% / +100%

الحظر الصلب (لا يُخفَّف أبداً): Mint/Freeze ملغاة، LP محروقة/مقفولة، ليست Rugged، لا Danger في RugCheck.
أما Dev/Top10/Insider/تقييم RugCheck فتخفض الـ Score وتظهر كتحذيرات (المستوى B).

متغيرات البيئة:
  TELEGRAM_BOT_TOKEN (مطلوب) | PORT و RENDER_EXTERNAL_URL (تضبطهما Render)
  TELEGRAM_API_ID + TELEGRAM_API_HASH (مطلوبان — من https://my.telegram.org → API development tools)
  TELETHON_SESSION : StringSession لحساب مستخدم، تولّدها مرة واحدة محلياً بـ gen_session.py
  SIGNAL_HTTP_MIRRORS (اختياري) : قوالب روابط مفصولة بفاصلة، فيها {ch}، تُجرَّب بالترتيب
  SIGNAL_CHANNEL (افتراضي solhousesignal) | SIGNAL_MESSAGE_LIMIT (افتراضي 60) | SIGNAL_MAX_AGE_HOURS (افتراضي 24)
  ALLOW_RISKY=0 للصرامة الكاملة
"""

import asyncio
import html
import io
import json
import logging
import math
import os
import re
import signal
import time
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Optional

import httpx
from aiohttp import web
from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters
from telethon import TelegramClient
from telethon.sessions import StringSession

# ───────────────────────────── الإعدادات ─────────────────────────────

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return default


MIN_SCORE = _env_float("MIN_SCORE", 80)            # 🟢 قوية (مع المستوى A)
RISKY_MIN_SCORE = _env_float("RISKY_MIN_SCORE", 65)  # 🟡 متوسطة

# فلتر أولي صلب
MIN_LIQUIDITY_USD = _env_float("MIN_LIQUIDITY_USD", 8_000)
MIN_VOLUME_H1_USD = _env_float("MIN_VOLUME_H1_USD", 5_000)
MIN_MCAP_USD = _env_float("MIN_MCAP_USD", 15_000)
MAX_MCAP_USD = _env_float("MAX_MCAP_USD", 100_000_000)
MIN_PAIR_AGE_MIN = _env_float("MIN_PAIR_AGE_MIN", 3)
MOMENTUM_MAX_H1_PCT = _env_float("MOMENTUM_MAX_H1_PCT", 100)   # فوقه = مطاردة قمة (Cart +123% انهارت −87%)
# تحت هذه القيم تُقبل العملة لكن بتحذير صريح وعقوبة في التقييم (لا تُرفض)
FLAG_LIQ_USD = _env_float("FLAG_LIQ_USD", 15_000)
FLAG_MCAP_USD = _env_float("FLAG_MCAP_USD", 30_000)
FLAG_AGE_MIN = _env_float("FLAG_AGE_MIN", 45)
MARKET_FLAG_PENALTY = 4
MAX_H1_CHANGE_PCT = _env_float("MAX_H1_CHANGE_PCT", 300)
MAX_5M_DROP_PCT = _env_float("MAX_5M_DROP_PCT", 10)
MAX_1H_DROP_PCT = _env_float("MAX_1H_DROP_PCT", 25)

# الأمان
MAX_DEV_PCT = _env_float("MAX_DEV_PCT", 5)
MAX_TOP10_PCT = _env_float("MAX_TOP10_PCT", 25)
MIN_LP_LOCKED_PCT = _env_float("MIN_LP_LOCKED_PCT", 95)
RUGCHECK_MAX_NORM = _env_float("RUGCHECK_MAX_NORM", 20)
RUGCHECK_MAX_RAW = _env_float("RUGCHECK_MAX_RAW", 500)
ALLOW_RISKY = _env_float("ALLOW_RISKY", 1) >= 1
RISKY_MAX_TOP10_PCT = _env_float("RISKY_MAX_TOP10_PCT", 50)
RISKY_MAX_DEV_PCT = _env_float("RISKY_MAX_DEV_PCT", 10)
RISKY_MAX_RUG_NORM = _env_float("RISKY_MAX_RUG_NORM", 50)
RISK_PENALTY = 8

# Fibonacci / الخطط
MIN_CANDLES = 20
MAX_WINDOW_CANDLES = 72
SWING_LOOKBACK_CANDLES = 36
MIN_LEG_PCT = _env_float("MIN_LEG_PCT", 12)
MAX_ABOVE_ZONE_PCT = _env_float("MAX_ABOVE_ZONE_PCT", 10)
MAX_SL_RISK_PCT = _env_float("MAX_SL_RISK_PCT", 25)
SL_BUFFER = 0.015
TP1_PCT = 0.18
TP2_PCT = 0.25

MAX_DISCOVERED_TOKENS = 150

# قناة الإشارات + Telethon
TG_API_ID = os.getenv("TELEGRAM_API_ID", "").strip()
TG_API_HASH = os.getenv("TELEGRAM_API_HASH", "").strip()
TG_SESSION = os.getenv("TELETHON_SESSION", "").strip()
SIGNAL_CHANNEL = os.getenv("SIGNAL_CHANNEL", "solhousesignal").strip().lstrip("@").replace("https://t.me/", "")
SIGNAL_MESSAGE_LIMIT = max(5, int(_env_float("SIGNAL_MESSAGE_LIMIT", 60)))
MANUAL_TTL_HOURS = _env_float("MANUAL_TTL_HOURS", 12)
MANUAL_MAX = 60
SIGNAL_PAGES = max(1, int(_env_float("SIGNAL_PAGES", 2)))            # صفحات HTTP، كل صفحة ≈ 20 رسالة
SIGNAL_HTTP_MIRRORS = [
    t.strip() for t in os.getenv(
        "SIGNAL_HTTP_MIRRORS",
        "https://t.me/s/{ch},https://telegram.me/s/{ch},https://tx.me/s/{ch}",
    ).split(",") if "{ch}" in t
]
WEB_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml",
    "Accept-Language": "en-US,en;q=0.8",
    "Cache-Control": "no-cache",
}
SIGNAL_MAX_AGE_HOURS = _env_float("SIGNAL_MAX_AGE_HOURS", 24)
MAX_SECURITY_CHECKS = int(_env_float("MAX_SECURITY_CHECKS", 30))
MAX_CANDLE_FETCH = int(_env_float("MAX_CANDLE_FETCH", 8))
SCAN_BUDGET_SEC = _env_float("SCAN_BUDGET_SEC", 100)
USER_COOLDOWN_SEC = 15
SCAN_CACHE_SEC = 20
RUGCHECK_CACHE_SEC = 600
KEEPALIVE_SEC = 540

DEX = "https://api.dexscreener.com"
GECKO = "https://api.geckoterminal.com/api/v2"
RUGCHECK = "https://api.rugcheck.xyz/v1"

EXCLUDED_MINTS = {
    "So11111111111111111111111111111111111111112",
    "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",
    "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB",
    # عناوين برامج/نظام تظهر أحياناً في الرسائل وليست عملات
    "11111111111111111111111111111111",
    "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA",
    "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb",
    "ATokenGPvbdGVxr1b2hvZbsiqW5xWH25efTNsLJA8knL",
    "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P",
    "JUP6LkbZbjS1jKKwapdHNy74zcZ3tLUZoi5QNyVTaV4",
}

KIND_LABEL = {
    "SCALP": "⚡ سكالبيج — Fibonacci Golden Zone",
    "MOMENTUM": "🚀 زخم Momentum — ركوب الصعود",
    "HOLD": "🧭 احتفاظ Swing — ساعات إلى أيام",
}
KIND_SHORT = {"SCALP": "⚡ سكالبيج", "MOMENTUM": "🚀 زخم", "HOLD": "🧭 احتفاظ"}

logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    level=logging.INFO,
)
logging.getLogger("httpx").setLevel(logging.WARNING)
log = logging.getLogger("solalpha")

# ───────────────────────────── أدوات مساعدة ─────────────────────────────


def to_float(v: Any, default: float = 0.0) -> float:
    if v is None or isinstance(v, bool):
        return default
    try:
        f = float(v)
    except (TypeError, ValueError):
        return default
    if math.isnan(f) or math.isinf(f):
        return default
    return f


def clamp(x: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, x))


def fmt_usd(v: float) -> str:
    if v >= 1e9:
        return f"${v / 1e9:.2f}B"
    if v >= 1e6:
        return f"${v / 1e6:.2f}M"
    if v >= 1e3:
        return f"${v / 1e3:.1f}K"
    return f"${v:,.0f}"


def fmt_price(p: float) -> str:
    if p <= 0:
        return "0"
    decimals = max(2, -int(math.floor(math.log10(p))) + 3)
    return f"{p:.{decimals}f}"


API_STATUS: dict = {}   # host -> (آخر حالة، وقتها) — يظهر في /diag وفي رسائل الفشل


def _note_api(url: str, status: str) -> None:
    try:
        API_STATUS[url.split("/")[2]] = (status, time.time())
    except IndexError:
        pass


def api_status_text() -> str:
    names = {"api.dexscreener.com": "DexScreener", "api.geckoterminal.com": "GeckoTerminal", "api.rugcheck.xyz": "RugCheck"}
    lines = []
    for host, name in names.items():
        st = API_STATUS.get(host)
        if st:
            lines.append(f"• {name}: {st[0]} (قبل {int(time.time() - st[1])}ث)")
    return "\n".join(lines) if lines else "• لا توجد معلومات عن حالة المصادر بعد"


GECKO_MIN_GAP = _env_float("GECKO_MIN_GAP", 2.1)       # ثوانٍ بين أي طلبين لـ GeckoTerminal (الحد 30/د)
_GECKO: dict = {"lock": None, "last": 0.0}


async def _gecko_wait() -> None:
    if _GECKO["lock"] is None:
        _GECKO["lock"] = asyncio.Lock()
    async with _GECKO["lock"]:
        gap = time.time() - _GECKO["last"]
        if gap < GECKO_MIN_GAP:
            await asyncio.sleep(GECKO_MIN_GAP - gap)
        _GECKO["last"] = time.time()


async def fetch_json(
    client: httpx.AsyncClient,
    url: str,
    params: Optional[dict] = None,
    headers: Optional[dict] = None,
    retries: int = 1,
) -> Any:
    """طلب JSON بإعادة محاولة أسّية (1.5، 3، 6، 12… ثانية + jitter) عند 429 / 5xx / أخطاء الشبكة، مع احترام Retry-After."""
    import random
    for attempt in range(retries + 1):
        try:
            if "geckoterminal" in url:
                await _gecko_wait()
            r = await client.get(url, params=params, headers=headers)
            _note_api(url, f"HTTP {r.status_code}")
            if r.status_code == 429 or 500 <= r.status_code < 600:
                if attempt >= retries:
                    return None
                try:
                    wait = float(r.headers.get("retry-after", "") or 0)
                except (ValueError, AttributeError):
                    wait = 0.0
                await asyncio.sleep(min(30.0, max(1.5 * (2 ** attempt), wait)) + random.uniform(0, 0.5))
                continue
            if r.status_code != 200:
                log.warning("HTTP %s from %s", r.status_code, url)
                return None
            return r.json()
        except ValueError:
            _note_api(url, "JSON غير صالح")
            return None
        except httpx.HTTPError as e:
            _note_api(url, type(e).__name__)
            log.warning("Request failed (%s): %s", url, e)
            if attempt >= retries:
                return None
            await asyncio.sleep(min(20.0, 1.0 * (2 ** attempt)) + random.uniform(0, 0.4))
    return None


# ───────────────────────────── اكتشاف العملات (من قناة الإشارات) ─────────────────────────────

B58_ALPHABET = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
B58_INDEX = {ch: i for i, ch in enumerate(B58_ALPHABET)}

# عنوان Solana = Base58 بطول 32–44 حرفاً (والتحقق النهائي أنه يفك إلى 32 بايت)
SOL_ADDR_RE = re.compile(r"(?<![1-9A-HJ-NP-Za-km-z])[1-9A-HJ-NP-Za-km-z]{32,44}(?![1-9A-HJ-NP-Za-km-z])")
# روابط DexScreener تحمل عنوان "الزوج" لا العملة → نحلّه لاحقاً عبر endpoint الأزواج
DEX_PAIR_URL_RE = re.compile(r"(?i:dexscreener\.com)/solana/([1-9A-HJ-NP-Za-km-z]{32,44})")
# روابط المحافظ/الحسابات ليست عقود عملات
EXPLORER_URL_RE = re.compile(r"(?i:solscan\.io|solana\.fm|explorer\.solana\.com)/(?:account|address)/\S+")



@dataclass
class Signal:
    mint: str                       # عنوان العملة (أو عنوان زوج إذا is_hint)
    post_id: int
    ts: Optional[float]             # وقت نشر الرسالة (epoch ثوانٍ)
    is_hint: bool = False           # جاء من رابط DexScreener (قد يكون عنوان زوج)
    manual: bool = False            # أرسله المستخدم يدوياً (لصق/Forward)
    added: float = 0.0              # وقت استلام البوت له (للوضع اليدوي)
    done: bool = False              # حُلِّل سابقاً (لا يُعاد تحليله إلا بـ /best all)


def is_valid_solana_address(s: str) -> bool:
    n = 0
    for ch in s:
        v = B58_INDEX.get(ch)
        if v is None:
            return False
        n = n * 58 + v
    pad = len(s) - len(s.lstrip("1"))
    return pad + (n.bit_length() + 7) // 8 == 32


def extract_addresses(text: str):
    """يرجع (عناوين_عقود، عناوين_من_روابط_DexScreener) مرتبة وفريدة."""
    if not text:
        return [], []
    hints: list = []
    for a in DEX_PAIR_URL_RE.findall(text):
        if a not in hints and a not in EXCLUDED_MINTS and is_valid_solana_address(a):
            hints.append(a)
    cleaned = EXPLORER_URL_RE.sub(" ", DEX_PAIR_URL_RE.sub(" ", text))
    mints: list = []
    for a in SOL_ADDR_RE.findall(cleaned):
        if a not in mints and a not in EXCLUDED_MINTS and is_valid_solana_address(a):
            mints.append(a)
    return mints, hints


def fmt_age(minutes: float) -> str:
    if minutes < 1:
        return "الآن"
    if minutes < 60:
        return f"منذ {int(minutes)} دقيقة"
    if minutes < 1440:
        return f"منذ {minutes / 60:.1f} ساعة"
    return f"منذ {minutes / 1440:.1f} يوم"


_POST_RE = re.compile(r'data-post="([^"]+)"')
_TEXT_RE = re.compile(r'<div class="tgme_widget_message_text js-message_text[^"]*"[^>]*>(.*?)</div>', re.S)
_PREVIEW_RE = re.compile(r'<a[^>]*tgme_widget_message_link_preview[^>]*href="([^"]+)"')
_TIME_RE = re.compile(r'<time[^>]*datetime="([^"]+)"')
_ANCHOR_RE = re.compile(r'<a\s[^>]*href="([^"]+)"[^>]*>(.*?)</a>', re.S)


def _html_to_text(fragment: str) -> str:
    s = _ANCHOR_RE.sub(lambda m: f" {m.group(1)} {m.group(2)} ", fragment)  # نُبقي قيمة href
    s = re.sub(r"<br\s*/?>", "\n", s)
    s = re.sub(r"<[^>]+>", " ", s)
    return html.unescape(s)


def parse_channel_html(page: str) -> list:
    """يحلل صفحة معاينة القناة (…/s/<channel>) → [{id, ts, text}]."""
    matches = list(_POST_RE.finditer(page))
    out = []
    for i, mt in enumerate(matches):
        end = matches[i + 1].start() if i + 1 < len(matches) else len(page)
        chunk = page[mt.start():end]
        chan, _, pid = mt.group(1).partition("/")
        if chan.lower() != SIGNAL_CHANNEL.lower() or not pid.isdigit():
            continue
        parts = [_html_to_text(t) for t in _TEXT_RE.findall(chunk)]
        parts += [html.unescape(u) for u in _PREVIEW_RE.findall(chunk)]
        ts = None
        tm = _TIME_RE.search(chunk)
        if tm:
            try:
                ts = datetime.fromisoformat(tm.group(1).replace("Z", "+00:00")).timestamp()
            except ValueError:
                ts = None
        out.append({"id": int(pid), "ts": ts, "text": "\n".join(parts)})
    return out


TL: dict = {"client": None, "error": None}   # حالة عميل Telethon (يُهيّأ في post_init)


async def create_telethon():
    """يرجع (client, خطأ). يحتاج جلسة مستخدم؛ حسابات البوت ممنوعة من قراءة سجل القنوات."""
    if not TG_SESSION:
        return None, "TELETHON_SESSION غير مضبوطة (جلسة البوت لا تقرأ سجل القنوات)"
    if not (TG_API_ID and TG_API_HASH):
        return None, "TELEGRAM_API_ID / TELEGRAM_API_HASH غير مضبوطين"
    try:
        api_id = int(TG_API_ID)
    except ValueError:
        return None, "TELEGRAM_API_ID يجب أن يكون رقماً"
    client = TelegramClient(StringSession(TG_SESSION), api_id, TG_API_HASH, receive_updates=False)
    try:
        await client.connect()
        if not await client.is_user_authorized():
            await client.disconnect()
            return None, "TELETHON_SESSION غير صالحة/منتهية — ولّدها من جديد بـ gen_session.py"
    except Exception as e:
        log.exception("Telethon connect failed")
        try:
            await client.disconnect()
        except Exception:
            pass
        return None, f"فشل اتصال Telethon: {type(e).__name__}: {e}"
    me = None
    try:
        me = await client.get_me()
    except Exception:
        pass
    if me is not None and getattr(me, "bot", False):
        await client.disconnect()
        return None, "TELETHON_SESSION تخص بوتاً — استخدم جلسة حساب مستخدم عادي"
    log.info("Telethon connected (user session)")
    return client, None


async def _read_channel(tl, limit: int, cutoff: float, info: dict) -> list:
    """أحدث الرسائل أولاً → [{id, ts, text}] مع نص الرسالة وروابطها (hidden links/أزرار/معاينة)."""
    out: list = []
    async for msg in tl.iter_messages(SIGNAL_CHANNEL, limit=limit):
        ts = msg.date.timestamp() if getattr(msg, "date", None) else None
        info["messages"] += 1
        if ts:
            info["newest_ts"] = max(info.get("newest_ts", 0.0), ts)
        if ts and ts < cutoff:
            info["expired"] += 1
            break  # الرسائل مرتبة من الأحدث → ما بعدها أقدم
        parts = [msg.raw_text or ""]
        try:
            for ent, _txt in msg.get_entities_text():
                if getattr(ent, "url", None):
                    parts.append(ent.url)           # رابط مخفي خلف نص
        except Exception:
            pass
        wp = getattr(getattr(msg, "media", None), "webpage", None)
        if getattr(wp, "url", None):
            parts.append(wp.url)                    # معاينة الرابط
        try:
            for row in msg.buttons or []:
                for b in row:
                    if getattr(b, "url", None):
                        parts.append(b.url)         # أزرار inline
        except Exception:
            pass
        out.append({"id": msg.id, "ts": ts, "text": "\n".join(parts)})
    return out


def _describe_tl_error(e: Exception) -> str:
    name = type(e).__name__
    if name == "BotMethodInvalidError":
        return "Telethon: الجلسة جلسة بوت والبوتات لا تقرأ سجل القنوات — استخدم TELETHON_SESSION لحساب مستخدم"
    if name == "FloodWaitError" and getattr(e, "seconds", None):
        return f"Telethon: FloodWait — انتظر {e.seconds} ثانية"
    return f"Telethon: {name}: {e}"


async def _read_http(client: httpx.AsyncClient, info: dict, cutoff: float, errors: list):
    """يجرّب مرايا المعاينة العامة بالترتيب. يرجع (رسائل، نجحت_القراءة)."""
    for tmpl in SIGNAL_HTTP_MIRRORS:
        base = tmpl.format(ch=SIGNAL_CHANNEL)
        host = base.split("/")[2] if "//" in base else base
        info.update(messages=0, expired=0, newest_ts=0.0)
        msgs: list = []
        seen: set = set()
        before = None
        reached = False
        for _ in range(SIGNAL_PAGES):
            try:
                r = await client.get(base, params={"before": before} if before else None, headers=WEB_HEADERS)
            except httpx.HTTPError as e:
                errors.append(f"{host}: {type(e).__name__}")
                break
            if r.status_code != 200:
                errors.append(f"{host}: HTTP {r.status_code}")
                break
            if "tgme_widget_message" not in r.text:
                errors.append(f"{host}: لا معاينة عامة أو الطلب محجوب")
                break
            reached = True
            page = [m for m in parse_channel_html(r.text) if m["id"] not in seen]
            if not page:
                break
            oldest = None
            for m in page:
                seen.add(m["id"])
                info["messages"] += 1
                if m["ts"]:
                    info["newest_ts"] = max(info["newest_ts"], m["ts"])
                    oldest = m["ts"] if oldest is None else min(oldest, m["ts"])
                if m["ts"] and m["ts"] < cutoff:
                    info["expired"] += 1
                    continue
                msgs.append(m)
            if oldest is not None and oldest < cutoff:
                break
            before = min(m["id"] for m in page)
            await asyncio.sleep(0.3)
        if reached:
            info["source"] = f"HTTP ({host})"
            return msgs, True
    return [], False


async def discover_signals(client: Optional[httpx.AsyncClient] = None):
    """يقرأ آخر رسائل القناة: Telethon (جلسة مستخدم) ثم HTTP كاحتياط. يرجع (Signal الأحدث أولاً، info)."""
    info = {
        "ok": False, "error": None, "errors": [], "source": None, "messages": 0, "expired": 0,
        "with_addr": 0, "addresses": 0, "newest_age_min": None, "newest_ts": 0.0,
    }
    now = time.time()
    cutoff = now - SIGNAL_MAX_AGE_HOURS * 3600
    errors: list = info["errors"]
    msgs: list = []
    got = False

    tl = TL.get("client")
    if tl is not None:
        try:
            msgs = await asyncio.wait_for(_read_channel(tl, SIGNAL_MESSAGE_LIMIT, cutoff, info), timeout=25)
            got = True
            info["source"] = "Telethon"
        except asyncio.TimeoutError:
            errors.append("Telethon: انتهت مهلة القراءة")
        except Exception as e:
            log.warning("Telethon read failed: %s: %s", type(e).__name__, e)
            errors.append(_describe_tl_error(e))
    else:
        errors.append("Telethon: " + (TL.get("error") or "غير مهيّأ"))

    if not got and client is not None:
        msgs, got = await _read_http(client, info, cutoff, errors)

    info["ok"] = got
    if not got:
        info["error"] = " | ".join(errors)
        return [], info

    found: dict = {}

    def add(addr: str, post_id: int, ts, is_hint: bool) -> None:
        cur = found.get(addr)
        if cur is None or post_id > cur.post_id:
            found[addr] = Signal(addr, post_id, ts, is_hint and not (cur and not cur.is_hint))

    for m in msgs:
        mints, hints = extract_addresses(m["text"])
        if mints or hints:
            info["with_addr"] += 1
        for a in mints:
            add(a, m["id"], m["ts"], False)
        for a in hints:
            add(a, m["id"], m["ts"], True)

    if info["newest_ts"]:
        info["newest_age_min"] = (now - info["newest_ts"]) / 60
    ordered = sorted(found.values(), key=lambda s: (s.ts or 0, s.post_id), reverse=True)
    info["addresses"] = len(ordered)
    return ordered[:MAX_DISCOVERED_TOKENS], info


def _merge_pairs(best: dict, payloads: list) -> None:
    for data in payloads:
        if isinstance(data, dict):
            data = data.get("pairs")
        if not isinstance(data, list):
            continue
        for p in data:
            if not isinstance(p, dict) or p.get("chainId") != "solana":
                continue
            base = (p.get("baseToken") or {}).get("address")
            if not base or base in EXCLUDED_MINTS:
                continue
            liq = to_float((p.get("liquidity") or {}).get("usd"))
            cur = best.get(base)
            if cur is None or liq > to_float((cur.get("liquidity") or {}).get("usd")):
                best[base] = p


async def fetch_gecko_pairs(client: httpx.AsyncClient, mints: list) -> list:
    """احتياط: GeckoTerminal (tokens/multi + top_pools) محوَّل لشكل أزواج DexScreener."""
    out: list = []
    for i in range(0, len(mints), 30):
        chunk = mints[i:i + 30]
        data = await fetch_json(
            client, f"{GECKO}/networks/solana/tokens/multi/{','.join(chunk)}", params={"include": "top_pools"}
        )
        if not isinstance(data, dict):
            continue
        pools = {p.get("id"): p for p in (data.get("included") or []) if isinstance(p, dict)}
        for tok in data.get("data") or []:
            try:
                ta = tok.get("attributes") or {}
                mint = ta.get("address")
                tp = ((tok.get("relationships") or {}).get("top_pools") or {}).get("data") or []
                pool = pools.get(tp[0].get("id")) if tp else None
                if not mint or not pool:
                    continue
                pa = pool.get("attributes") or {}
                dex_id = str((((pool.get("relationships") or {}).get("dex") or {}).get("data") or {}).get("id") or "")
                created_ms = None
                if pa.get("pool_created_at"):
                    try:
                        created_ms = datetime.fromisoformat(str(pa["pool_created_at"]).replace("Z", "+00:00")).timestamp() * 1000
                    except ValueError:
                        created_ms = None
                tx = pa.get("transactions") or {}
                out.append({
                    "chainId": "solana",
                    "dexId": dex_id.replace("-", ""),          # pump-fun → pumpfun
                    "pairAddress": pa.get("address"),
                    "baseToken": {"address": mint, "name": ta.get("name"), "symbol": ta.get("symbol")},
                    "priceUsd": ta.get("price_usd"),
                    "liquidity": {"usd": pa.get("reserve_in_usd")},
                    "marketCap": pa.get("market_cap_usd") or ta.get("market_cap_usd"),
                    "fdv": pa.get("fdv_usd") or ta.get("fdv_usd"),
                    "volume": pa.get("volume_usd") or {},
                    "priceChange": pa.get("price_change_percentage") or {},
                    "txns": {k: {"buys": (v or {}).get("buys"), "sells": (v or {}).get("sells")} for k, v in tx.items()},
                    "pairCreatedAt": created_ms,
                })
            except Exception:
                log.exception("gecko pair parse failed")
    return out


async def fetch_pairs(client: httpx.AsyncClient, addresses: list, pair_hints: Any = (), sink: Optional[dict] = None,
                      light: bool = False) -> list:
    """sink: قاموس يُملأ تدريجياً فتبقى النتائج الجزئية حتى لو انتهت المهلة."""
    best: dict = sink if sink is not None else {}
    hint_set = set(pair_hints)
    mints = [a for a in addresses if a not in hint_set]

    # 1) DexScreener (الجديد)
    chunks = [addresses[i:i + 30] for i in range(0, len(addresses), 30)]
    responses = await asyncio.gather(
        *(fetch_json(client, f"{DEX}/tokens/v1/solana/{','.join(c)}") for c in chunks)
    )
    _merge_pairs(best, list(responses))
    if light:                                   # للمراقبة الدورية: بلا طلبات احتياطية إضافية
        return list(best.values())

    # 2) DexScreener (endpoint القديم) لما لم يُحَل
    missing = [a for a in mints if a not in best]
    if missing:
        mchunks = [missing[i:i + 30] for i in range(0, len(missing), 30)]
        resp2 = await asyncio.gather(
            *(fetch_json(client, f"{DEX}/latest/dex/tokens/{','.join(c)}") for c in mchunks)
        )
        _merge_pairs(best, list(resp2))

    # 3) GeckoTerminal احتياطاً لما لم يُحَل
    missing = [a for a in mints if a not in best]
    if missing:
        _merge_pairs(best, [await fetch_gecko_pairs(client, missing)])

    # عناوين جاءت من روابط DexScreener ولم تُحَل كعملات → نجرّبها كعناوين أزواج
    known_pairs = {p.get("pairAddress") for p in best.values()}
    hints = [h for h in dict.fromkeys(hint_set) if h not in best and h not in known_pairs]
    if hints:
        hchunks = [hints[i:i + 30] for i in range(0, len(hints), 30)]
        hresp = await asyncio.gather(
            *(fetch_json(client, f"{DEX}/latest/dex/pairs/solana/{','.join(c)}") for c in hchunks)
        )
        _merge_pairs(best, list(hresp))
    return list(best.values())


async def probe_apis(client: httpx.AsyncClient) -> list:
    """اختبار مباشر لاتصال السيرفر بكل مصدر (يظهر في /diag)."""
    sol = "So11111111111111111111111111111111111111112"
    usdc = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
    tests = {
        "DexScreener": f"{DEX}/tokens/v1/solana/{sol}",
        "GeckoTerminal": f"{GECKO}/networks/solana/tokens/{sol}",
        "RugCheck": f"{RUGCHECK}/tokens/{usdc}/report",
    }

    async def one(name: str, url: str):
        t0 = time.time()
        try:
            r = await client.get(url, timeout=10)
            return name, f"{'✅' if r.status_code == 200 else '❌'} HTTP {r.status_code} ({time.time() - t0:.1f}s)"
        except Exception as e:
            return name, f"❌ {type(e).__name__}"

    return list(await asyncio.gather(*(one(n, u) for n, u in tests.items())))


# ───────────────────────────── المقاييس والفلترة الأولية ─────────────────────────────


def extract_metrics(pair: dict) -> Optional[dict]:
    base = pair.get("baseToken") or {}
    mint = base.get("address")
    pair_addr = pair.get("pairAddress")
    if not mint or not pair_addr:
        return None
    vol = pair.get("volume") or {}
    chg = pair.get("priceChange") or {}
    tx = pair.get("txns") or {}
    t5 = tx.get("m5") or {}

    created = to_float(pair.get("pairCreatedAt"))
    age_min = (time.time() * 1000 - created) / 60000 if created > 0 else None

    m = {
        "mint": mint,
        "name": str(base.get("name") or "Unknown"),
        "symbol": str(base.get("symbol") or "?"),
        "pair": pair_addr,
        "dex": str(pair.get("dexId") or ""),
        "price": to_float(pair.get("priceUsd")),
        "liq": to_float((pair.get("liquidity") or {}).get("usd")),
        "mcap": to_float(pair.get("marketCap")) or to_float(pair.get("fdv")),
        "vol5": to_float(vol.get("m5")),
        "vol1": to_float(vol.get("h1")),
        "vol6": to_float(vol.get("h6")),
        "vol24": to_float(vol.get("h24")),
        "chg5": to_float(chg.get("m5")),
        "chg1": to_float(chg.get("h1")),
        "chg6": to_float(chg.get("h6")),
        "chg24": to_float(chg.get("h24")),
        "buys5": to_float(t5.get("buys")),
        "sells5": to_float(t5.get("sells")),
        "age_min": age_min,
    }
    m["vol_pace"] = (m["vol5"] * 12 / m["vol1"]) if m["vol1"] > 0 else 0.0
    m["h1_vs_h6"] = (m["vol1"] / (m["vol6"] / 6)) if m["vol6"] > 0 else 0.0
    m["liq_mcap"] = (m["liq"] / m["mcap"]) if m["mcap"] > 0 else 0.0
    total5 = m["buys5"] + m["sells5"]
    m["buy_ratio5"] = (m["buys5"] / total5) if total5 > 0 else 0.0
    return m


def pre_filter(m: dict) -> Optional[str]:
    if m["dex"] == "pumpfun":
        return "لم تهاجر من Bonding Curve بعد"
    if m["price"] <= 0:
        return "بيانات سعر غير صالحة"
    if m["liq"] < MIN_LIQUIDITY_USD:
        return "سيولة منخفضة"
    if not (MIN_MCAP_USD <= m["mcap"] <= MAX_MCAP_USD):
        return "الماركت كاب خارج النطاق"
    if m["age_min"] is None or m["age_min"] < MIN_PAIR_AGE_MIN:
        return "عمر الزوج قصير جداً"
    if m["vol1"] < MIN_VOLUME_H1_USD:
        return "حجم تداول 1h ضعيف"
    if m["chg5"] <= -MAX_5M_DROP_PCT or m["chg1"] <= -MAX_1H_DROP_PCT:
        return "انهيار حاد في السعر"
    if m["chg1"] > MAX_H1_CHANGE_PCT:
        return "ارتفاع مفرط خلال ساعة (خطر قمة)"
    return None


def reason_detail(m: dict, reason: str) -> str:
    """يحوّل سبب الفلتر الأولي إلى جملة بالأرقام الفعلية والحد المطلوب."""
    try:
        if reason == "سيولة منخفضة":
            return f"سيولة {fmt_usd(m['liq'])} (الحد الأدنى {fmt_usd(MIN_LIQUIDITY_USD)})"
        if reason == "الماركت كاب خارج النطاق":
            return f"ماركت كاب {fmt_usd(m['mcap'])} (النطاق {fmt_usd(MIN_MCAP_USD)}–{fmt_usd(MAX_MCAP_USD)})"
        if reason == "عمر الزوج قصير جداً":
            if m["age_min"] is None:
                return "عمر الزوج غير معروف"
            return f"عمر الزوج {m['age_min']:.0f} دقيقة (الحد الأدنى {MIN_PAIR_AGE_MIN:.0f})"
        if reason == "حجم تداول 1h ضعيف":
            return f"فوليوم 1h {fmt_usd(m['vol1'])} (الحد الأدنى {fmt_usd(MIN_VOLUME_H1_USD)})"
        if reason == "انهيار حاد في السعر":
            return f"هبوط 5د {m['chg5']:+.0f}% / 1h {m['chg1']:+.0f}%"
        if reason.startswith("ارتفاع مفرط"):
            return f"صعود 1h {m['chg1']:+.0f}% (الحد {MAX_H1_CHANGE_PCT:.0f}%)"
        if reason.startswith("لم تهاجر"):
            return "ما زالت على Bonding Curve (لا LP للفحص) — سأراقبها وأفحصها فور الهجرة"
    except Exception:
        pass
    return reason


def sec_detail(sec: "Security") -> str:
    nums = [f"Top10 {sec.top10:.0f}%"]
    if sec.dev is not None:
        nums.append(f"Dev {sec.dev:.1f}%")
    if sec.lp_locked is not None:
        nums.append(f"LP {sec.lp_locked:.0f}%")
    if sec.rug_norm is not None:
        nums.append(f"RugCheck {sec.rug_norm:.0f}/100")
    return " + ".join(sec.reasons) + " [" + " • ".join(nums) + "]"


def prelim_rank(m: dict) -> float:
    return (
        min(m["vol_pace"], 4)
        + min(m["h1_vs_h6"], 6) / 2
        + min(m["liq_mcap"], 0.2) * 10
        + (1.0 if m["chg1"] > 0 else 0.0)
    )


# ───────────────────────────── الفلترة الأمنية (RugCheck) ─────────────────────────────


@dataclass
class Security:
    passed: bool
    reasons: list = field(default_factory=list)      # حظر صلب
    warnings: list = field(default_factory=list)     # مخاطرة أعلى (المستوى B)
    top10: float = 0.0
    dev: Optional[float] = None
    lp_locked: Optional[float] = None
    rug_norm: Optional[float] = None
    rug_raw: Optional[float] = None
    bundled: bool = False


_RUG_CACHE: dict = {}


_RUG_STATE: dict = {"sem": None, "last": 0.0, "neg": {}}


async def get_rugcheck_report(client: httpx.AsyncClient, mint: str) -> Optional[dict]:
    cached = _RUG_CACHE.get(mint)
    if cached and time.time() - cached[0] < RUGCHECK_CACHE_SEC:
        return cached[1]
    neg = _RUG_STATE["neg"].get(mint)
    if neg and time.time() - neg < 60:          # فشل حديث: لا نضغط على RugCheck
        return None
    if _RUG_STATE["sem"] is None:
        _RUG_STATE["sem"] = asyncio.Semaphore(2)
    async with _RUG_STATE["sem"]:
        gap = time.time() - _RUG_STATE["last"]
        if gap < 0.7:
            await asyncio.sleep(0.7 - gap)
        _RUG_STATE["last"] = time.time()
        data = await fetch_json(client, f"{RUGCHECK}/tokens/{mint}/report", retries=4)
    if isinstance(data, dict):
        _RUG_CACHE[mint] = (time.time(), data)
        return data
    _RUG_STATE["neg"][mint] = time.time()
    return None


def analyze_security(report: dict, pair_address: str) -> Security:
    reasons: list = []
    soft: list = []
    token = report.get("token") if isinstance(report.get("token"), dict) else {}

    def authority_revoked(key: str) -> Optional[bool]:
        for src in (token, report):
            if key in src:
                return src[key] in (None, "", "null")
        return None

    mint_rev = authority_revoked("mintAuthority")
    if mint_rev is not True:
        reasons.append("Mint Authority غير ملغاة" if mint_rev is False else "تعذر التحقق من Mint Authority")
    freeze_rev = authority_revoked("freezeAuthority")
    if freeze_rev is not True:
        reasons.append("Freeze Authority غير ملغاة" if freeze_rev is False else "تعذر التحقق من Freeze Authority")

    if report.get("rugged") is True:
        reasons.append("التوكن مُعلَّم كـ Rugged")
    tf = report.get("transferFee")
    if isinstance(tf, dict) and to_float(tf.get("pct")) > 0:
        reasons.append("رسوم تحويل على التوكن")

    risks = [r for r in (report.get("risks") or []) if isinstance(r, dict)]
    if any(str(r.get("level", "")).lower() == "danger" for r in risks):
        reasons.append("RugCheck: مخاطر بمستوى Danger")

    norm = report.get("score_normalised")
    raw = report.get("score")
    rug_norm = to_float(norm, -1.0) if norm is not None else None
    rug_raw = to_float(raw, -1.0) if raw is not None else None
    if rug_norm is not None:
        if rug_norm > RISKY_MAX_RUG_NORM:
            reasons.append("تقييم RugCheck سيئ")
        elif rug_norm > RUGCHECK_MAX_NORM:
            soft.append(f"RugCheck ليس Good (خطورة {rug_norm:.0f}/100)")
    elif rug_raw is not None:
        if rug_raw > RUGCHECK_MAX_RAW:
            reasons.append("تقييم RugCheck سيئ")
    else:
        reasons.append("تعذر قراءة تقييم RugCheck")

    markets = [mk for mk in (report.get("markets") or []) if isinstance(mk, dict)]
    chosen = next((mk for mk in markets if mk.get("pubkey") == pair_address), None)
    if chosen is None and markets:
        chosen = max(
            markets,
            key=lambda mk: to_float((mk.get("lp") or {}).get("baseUSD"))
            + to_float((mk.get("lp") or {}).get("quoteUSD")),
        )
    lp_locked: Optional[float] = None
    if chosen is not None and isinstance(chosen.get("lp"), dict) and "lpLockedPct" in chosen["lp"]:
        lp_locked = to_float(chosen["lp"].get("lpLockedPct"))
    if lp_locked is None:
        reasons.append("تعذر التحقق من حالة LP")
    elif lp_locked < MIN_LP_LOCKED_PCT:
        reasons.append("LP غير محروقة/مقفولة بالكامل")

    pool_accounts = set()
    for mk in markets:
        for key in ("pubkey", "liquidityA", "liquidityB", "mintLP"):
            if mk.get(key):
                pool_accounts.add(mk[key])
    all_holders = [h for h in (report.get("topHolders") or []) if isinstance(h, dict)]
    real_holders = [
        h for h in all_holders
        if h.get("address") not in pool_accounts and h.get("owner") not in pool_accounts
    ]
    real_holders.sort(key=lambda h: to_float(h.get("pct")), reverse=True)
    top10 = sum(to_float(h.get("pct")) for h in real_holders[:10])
    if not all_holders:
        reasons.append("تعذر جلب بيانات الحيتان")
    elif top10 > RISKY_MAX_TOP10_PCT:
        reasons.append("تركّز كبار الملاك Top10 مرتفع جداً")
    elif top10 > MAX_TOP10_PCT:
        soft.append(f"Top10 مرتفع ({top10:.0f}%)")

    dev: Optional[float] = None
    creator = report.get("creator")
    if creator:
        dev = 0.0
        for h in all_holders:
            if h.get("address") == creator or h.get("owner") == creator:
                dev = max(dev, to_float(h.get("pct")))
        supply = to_float(token.get("supply"))
        creator_bal = to_float(report.get("creatorBalance"))
        if supply > 0 and creator_bal > 0:
            dev = max(dev, creator_bal / supply * 100)
        if dev > RISKY_MAX_DEV_PCT:
            reasons.append("حصة المطور Dev مرتفعة جداً")
        elif dev > MAX_DEV_PCT:
            soft.append(f"حصة المطور {dev:.1f}%")

    bundled = (
        any(h.get("insider") for h in real_holders)
        or to_float(report.get("graphInsidersDetected")) > 0
        or bool(report.get("insiderNetworks"))
        or any(
            kw in str(r.get("name", "")).lower()
            for r in risks
            for kw in ("bundl", "snip", "insider")
        )
    )
    if bundled:
        soft.append("أنماط Bundled/Sniped/Insider")

    if soft and not ALLOW_RISKY:
        reasons.extend(soft)
        soft = []

    return Security(
        passed=not reasons,
        reasons=reasons,
        warnings=soft,
        top10=top10,
        dev=dev,
        lp_locked=lp_locked,
        rug_norm=rug_norm,
        rug_raw=rug_raw,
        bundled=bundled,
    )


# ───────────────────────────── الخطط (Scalp / Momentum / Hold) ─────────────────────────────


@dataclass
class ScalpPlan:
    swing_low: float
    swing_high: float
    zone_low: float
    zone_high: float
    status: str            # IN_ZONE | ABOVE_ZONE | WAIT | MOMENTUM | HOLD
    above_pct: float
    entry: float
    tp1: float
    tp2: float
    ext1272: float
    ext1618: float
    sl: float
    risk_pct: float
    rr1: float
    rr2: float
    leg_pct: float
    kind: str = "SCALP"


def candle_tf(age_min: Optional[float]) -> int:
    """العملة الحديثة (<3 ساعات) لا تملك 20 شمعة 5د، فنستعمل شموع الدقيقة (كما يتداولها المستخدم عادة)."""
    return 1 if (age_min is not None and age_min < 180) else 5


def fib_params(tf: int):
    """(أقل عدد شموع، نافذة التحليل، نظرة الـ Swing) حسب الإطار الزمني."""
    return (25, 120, 60) if tf == 1 else (MIN_CANDLES, MAX_WINDOW_CANDLES, SWING_LOOKBACK_CANDLES)


async def fetch_candles(client: httpx.AsyncClient, pool: str, tf: int = 5, limit: Optional[int] = None) -> list:
    data = await fetch_json(
        client,
        f"{GECKO}/networks/solana/pools/{pool}/ohlcv/minute",
        params={"aggregate": tf, "limit": limit or (200 if tf == 1 else 100), "currency": "usd"},
    )
    rows = (((data or {}).get("data") or {}).get("attributes") or {}).get("ohlcv_list") if isinstance(data, dict) else None
    candles = []
    for row in rows or []:
        if isinstance(row, (list, tuple)) and len(row) >= 6:
            ts, o, h, l, c, v = (to_float(x) for x in row[:6])
            if h > 0 and l > 0 and h >= l:
                candles.append((ts, o, h, l, c, v))
    candles.sort(key=lambda x: x[0])
    return candles


def compute_scalp_plan(candles: list, price: float, tf: int = 5):
    """SCALP: يرجع (ScalpPlan, None) أو (None, سبب_الرفض). WAIT = السعر بعيد فوق المنطقة."""
    min_c, max_w, lookback = fib_params(tf)
    if len(candles) < min_c:
        return None, "شموع غير كافية لحساب Fibonacci"

    window = candles[-max_w:]
    hi_idx = max(range(len(window)), key=lambda i: window[i][2])
    high = window[hi_idx][2]
    seg = window[max(0, hi_idx - lookback): hi_idx + 1]
    lo_rel = min(range(len(seg)), key=lambda i: seg[i][3])
    low = seg[lo_rel][3]
    if lo_rel == len(seg) - 1:
        return None, "لا توجد موجة صاعدة واضحة (Swing Low/High)"
    if price > high:
        high = price

    rng = high - low
    if low <= 0 or rng / low * 100 < MIN_LEG_PCT:
        return None, "الموجة الصاعدة ضعيفة"

    zone_high = high - 0.5 * rng
    zone_low = high - 0.618 * rng
    sl = low * (1 - SL_BUFFER)

    if zone_low * 0.99 <= price <= zone_high * 1.01:
        status, entry, above_pct = "IN_ZONE", price, 0.0
    elif price > zone_high * 1.01:
        above_pct = (price / zone_high - 1) * 100
        status = "ABOVE_ZONE" if above_pct <= MAX_ABOVE_ZONE_PCT else "WAIT"
        entry = (zone_low + zone_high) / 2
    else:
        return None, "السعر كسر المنطقة الذهبية للأسفل"

    if entry <= sl:
        return None, "وقف الخسارة فوق نقطة الدخول"
    risk_pct = (entry - sl) / entry * 100
    if risk_pct > MAX_SL_RISK_PCT:
        return None, "وقف الخسارة بعيد جداً (مخاطرة عالية)"

    return ScalpPlan(
        swing_low=low,
        swing_high=high,
        zone_low=zone_low,
        zone_high=zone_high,
        status=status,
        above_pct=above_pct,
        entry=entry,
        tp1=entry * (1 + TP1_PCT),
        tp2=entry * (1 + TP2_PCT),
        ext1272=low + 1.272 * rng,
        ext1618=low + 1.618 * rng,
        sl=sl,
        risk_pct=risk_pct,
        rr1=(TP1_PCT * 100) / risk_pct,
        rr2=(TP2_PCT * 100) / risk_pct,
        leg_pct=rng / low * 100,
        kind="SCALP",
    ), None


def momentum_ok(m: dict) -> bool:
    if not (3 <= m["chg1"] <= MOMENTUM_MAX_H1_PCT) or m["chg5"] < -1 or m["buy_ratio5"] < 0.45:
        return False
    if m["vol_pace"] >= 0.8:                 # فوليوم متسارع
        return True
    # اندفاع سعري حديث بسيطرة واضحة للمشترين حتى لو تباطأ فوليوم آخر 5 دقائق عن متوسط الساعة
    return m["chg5"] >= 5 and m["buy_ratio5"] >= 0.55 and m["vol5"] > 0


def hold_ok(m: dict) -> bool:
    age = m["age_min"] or 0
    return (
        age >= 1440
        and m["liq"] >= 40_000
        and m["chg24"] > 0
        and m["chg1"] >= -5
        and m["mcap"] > 0
        and m["vol24"] / m["mcap"] >= 0.2
    )


def compute_alt_plan(kind: str, m: dict, candles: list) -> ScalpPlan:
    price = m["price"]
    if kind == "MOMENTUM":
        zone_low, zone_high = price * 0.97, price
        tp1, tp2 = price * 1.15, price * 1.30
        sl = price * 0.88
        if candles:
            recent_low = min(c[3] for c in candles[-8:])
            sl = clamp(recent_low * 0.985, price * 0.85, price * 0.93)
    else:  # HOLD
        zone_low, zone_high = price * 0.92, price
        tp1, tp2 = price * 1.50, price * 2.00
        sl = price * 0.75
    risk_pct = (price - sl) / price * 100
    return ScalpPlan(
        swing_low=0.0,
        swing_high=0.0,
        zone_low=zone_low,
        zone_high=zone_high,
        status=kind,
        above_pct=0.0,
        entry=price,
        tp1=tp1,
        tp2=tp2,
        ext1272=0.0,
        ext1618=0.0,
        sl=sl,
        risk_pct=risk_pct,
        rr1=((tp1 / price - 1) * 100) / risk_pct,
        rr2=((tp2 / price - 1) * 100) / risk_pct,
        leg_pct=0.0,
        kind=kind,
    )


def market_flags(m: dict) -> list:
    """عملة صغيرة/حديثة: تُقبل لكن بتحذير (فرصتها أكبر ومخاطرتها أعلى)."""
    f = []
    if m["liq"] < FLAG_LIQ_USD:
        f.append(f"سيولة منخفضة {fmt_usd(m['liq'])} — خطر انزلاق")
    if m["mcap"] < FLAG_MCAP_USD:
        f.append(f"ماركت كاب صغير جداً {fmt_usd(m['mcap'])}")
    if m["age_min"] is not None and m["age_min"] < FLAG_AGE_MIN:
        f.append(f"زوج حديث جداً ({m['age_min']:.0f} دقيقة)")
    if m["vol1"] > 0 and m["vol_pace"] < 0.8:
        f.append(f"فوليوم 5د ×{m['vol_pace']:.1f} من متوسط الساعة — الزخم قد يكون منتهياً")
    return f


def compute_score(m: dict, sec: Security, plan: ScalpPlan):
    """Score /100 = سيولة/MC (20) + فوليوم (20) + زخم (10) + جودة الدخول (25) + أمان (25) − عقوبة مخاطرة."""
    b: dict = {}
    b["liq_mcap"] = 20 * clamp(m["liq_mcap"] / 0.10, 0, 1)

    pace = 8 * clamp(m["vol_pace"] / 2.0, 0, 1)
    trend = 7 * clamp(m["h1_vs_h6"] / 3.0, 0, 1)
    buys = 5 * clamp((m["buy_ratio5"] - 0.5) / 0.2, 0, 1)
    b["volume"] = pace + trend + buys

    mom = 0.0
    if m["chg5"] > 0:
        mom += 4 if m["chg5"] <= 25 else 2
    if m["chg1"] > 0:
        mom += 6 if m["chg1"] <= 150 else 3
    b["momentum"] = mom

    if plan.kind == "SCALP":
        if plan.status == "IN_ZONE":
            b["fib"] = 25.0
        elif plan.status == "ABOVE_ZONE":
            b["fib"] = 18 * (1 - 0.5 * clamp(plan.above_pct / MAX_ABOVE_ZONE_PCT, 0, 1))
        else:
            b["fib"] = 6.0
    elif plan.kind == "MOMENTUM":
        b["fib"] = 14.0
    else:
        b["fib"] = 12.0

    safety = 10 * (1 - clamp(sec.top10 / MAX_TOP10_PCT, 0, 1))
    safety += 5 * (1 - clamp((sec.dev or 0.0) / MAX_DEV_PCT, 0, 1)) if sec.dev is not None else 2.5
    if sec.rug_norm is not None:
        safety += 10 * (1 - clamp(sec.rug_norm / RUGCHECK_MAX_NORM, 0, 1))
    else:
        safety += 5
    b["safety"] = safety
    b["risk_penalty"] = -RISK_PENALTY if sec.warnings else 0.0
    b["market_risk"] = -min(10, MARKET_FLAG_PENALTY * len(market_flags(m)))

    total = int(round(sum(b.values())))
    return int(clamp(total, 0, 100)), b


# ───────────────────────────── المسح الكامل ─────────────────────────────


@dataclass
class Candidate:
    m: dict
    sec: Security
    plan: ScalpPlan
    score: int
    breakdown: dict


@dataclass
class ScanResult:
    ranked: list = field(default_factory=list)       # كل الصفقات مرتبة بالـ Score
    stats: Counter = field(default_factory=Counter)
    sources: dict = field(default_factory=dict)
    signals: int = 0
    discovered: int = 0
    verdicts: dict = field(default_factory=dict)   # mint -> {sym, mcap, icon, text}
    watch: list = field(default_factory=list)      # (m, sec|None, kind) → قائمة المراقبة
    secs: dict = field(default_factory=dict)       # mint -> Security (لتسجيل الصفقات)
    unresolved: list = field(default_factory=list)  # عقود بلا بيانات سوق
    after_prefilter: int = 0
    after_security: int = 0
    finished_at: float = 0.0


def grade_of(c: Candidate) -> str:
    if not c.sec.warnings and not market_flags(c.m) and c.score >= MIN_SCORE:
        return "🟢 قوية"
    if c.score >= RISKY_MIN_SCORE:
        return "🟡 متوسطة"
    return "🟠 ضعيفة (مضاربة)"


async def run_scan(
    client: httpx.AsyncClient,
    progress=None,
    budget: float = SCAN_BUDGET_SEC,
    extra: Optional[list] = None,
) -> ScanResult:
    t0 = time.time()

    def left() -> float:
        return max(2.0, budget - (time.time() - t0))

    async def say(text: str) -> None:
        if progress:
            try:
                await progress(text)
            except Exception:
                pass

    res = ScanResult()

    def set_v(m: dict, icon: str, text: str) -> None:
        snap = {k: m.get(k) for k in ("price", "mcap", "liq", "age_min", "chg5", "chg1", "vol_pace",
                                      "buy_ratio5", "vol1", "pair", "dex", "symbol")}
        res.verdicts[m["mint"]] = {"sym": m["symbol"], "mcap": m["mcap"], "icon": icon, "text": text, "m": snap}

    # 1) قراءة القناة
    await say(f"📡 (1/4) قراءة آخر إشارات قناة @{SIGNAL_CHANNEL} ...")
    signals, res.sources = await discover_signals(client)
    if extra:  # عقود أرسلها المستخدم يدوياً (تُدمج مع عقود القناة، وعقد القناة له الأولوية)
        have = {s.mint for s in signals}
        signals = [e for e in extra if e.mint not in have] + signals
        signals = signals[:MAX_DISCOVERED_TOKENS]
        res.sources["manual"] = len(extra)
    res.signals = len(signals)
    if not signals:
        res.finished_at = time.time()
        return res
    sig_map = {s.mint: s for s in signals}
    addrs = [s.mint for s in signals]
    hints = [s.mint for s in signals if s.is_hint]

    # 2) بيانات الأزواج
    await say(f"📊 (2/4) جلب بيانات {len(addrs)} عقد من القناة...")
    sink: dict = {}
    try:
        await asyncio.wait_for(fetch_pairs(client, addrs, hints, sink), timeout=min(35.0, left()))
    except asyncio.TimeoutError:
        log.warning("fetch_pairs timed out (partial=%d)", len(sink))
    pairs = list(sink.values())
    res.discovered = len(pairs)

    pre = []
    matched: set = set()
    for p in pairs:
        m = extract_metrics(p)
        if m is None:
            res.stats["بيانات ناقصة"] += 1
            continue
        sig = sig_map.get(m["mint"]) or sig_map.get(m["pair"])
        if sig:
            m["signal_ts"], m["signal_post"], m["signal_manual"] = sig.ts, sig.post_id, sig.manual
            matched.add(sig.mint)
        reason = pre_filter(m)
        if reason:
            kind = watch_kind_for(reason)
            if kind in ("young", "bonding"):          # الفحص الأمني غير ممكن/مبكر → مراقبة ثم فحص تلقائي
                res.stats[reason] += 1
                res.watch.append((m, None, kind))
                set_v(m, "👁", reason_detail(m, reason) + " — أُضيفت للمراقبة وسأعيد فحصها تلقائياً")
            elif kind in ("dip", "extended"):         # تمر على الفحص الأمني ثم تُراقب (لا دخول فوري)
                m["watch_kind"], m["watch_reason"] = kind, reason_detail(m, reason)
                pre.append(m)
            else:
                res.stats[reason] += 1
                set_v(m, "❌", reason_detail(m, reason))
        else:
            pre.append(m)
    res.unresolved = [s.mint for s in signals if s.mint not in matched]
    if pairs and len(signals) > len(matched):
        res.stats["عقد بلا زوج على DexScreener (جديد جداً أو ليس عملة)"] += len(signals) - len(matched)
    res.after_prefilter = len(pre)
    pre.sort(key=prelim_rank, reverse=True)
    for m in pre[MAX_SECURITY_CHECKS:]:
        set_v(m, "⏭️", f"لم يُفحص أمنياً (حد الفحص {MAX_SECURITY_CHECKS} عملة) — ليس حكماً على العملة")
    pre = pre[:MAX_SECURITY_CHECKS]

    # 3) الأمان
    await say(f"🛡️ (3/4) فحص أمان {len(pre)} عملة عبر RugCheck...")
    sem = asyncio.Semaphore(4)

    async def check(m: dict):
        async with sem:
            report = await get_rugcheck_report(client, m["mint"])
        return m, (analyze_security(report, m["pair"]) if report else None)

    task_map = {asyncio.create_task(check(m)): m for m in pre}
    tasks = list(task_map)
    secure: list = []
    if tasks:
        done, pending = await asyncio.wait(tasks, timeout=max(5.0, min(45.0, left() - 25)))
        for t in pending:
            t.cancel()
            set_v(task_map[t], "⏱️", "انتهت مهلة RugCheck — لم تُفحص (ليس حكماً على العملة)")
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
            res.stats["انتهت مهلة فحص RugCheck (لم تُفحص)"] += len(pending)
        for t in done:
            if t.cancelled() or t.exception() is not None:
                res.stats["خطأ أثناء فحص RugCheck"] += 1
                set_v(task_map[t], "⚠️", "خطأ أثناء فحص RugCheck — ليس حكماً على العملة")
                continue
            m, sec = t.result()
            if sec is not None:
                res.secs[m['mint']] = sec
            if sec is None:
                res.stats["تعذر جلب تقرير RugCheck"] += 1
                set_v(m, "⚠️", "تعذر جلب تقرير RugCheck (حد طلبات/غير مفهرسة) — ليس حكماً على العملة")
            elif not sec.passed:
                for r in sec.reasons:
                    res.stats[r] += 1
                set_v(m, "🛡️", sec_detail(sec))
            elif m.get("watch_kind"):
                res.watch.append((m, sec, m["watch_kind"]))
                set_v(m, "👁", f"{m['watch_reason']} — اجتازت الأمان وأُضيفت للمراقبة بانتظار ارتداد مؤكد")
            else:
                secure.append((m, sec))
    res.after_security = len(secure)

    # 4) الخطط
    await say(f"📈 (4/4) تحليل {len(secure)} عملة آمنة وبناء الخطط...")
    secure.sort(key=lambda ms: prelim_rank(ms[0]), reverse=True)
    candle_map: dict = {}
    top = [m for m, _ in secure[:MAX_CANDLE_FETCH]]
    if top and left() > 6:
        csem = asyncio.Semaphore(2)

        async def get_candles(m: dict) -> list:
            async with csem:
                c = await fetch_candles(client, m["pair"], candle_tf(m["age_min"]))
                await asyncio.sleep(0.6)
                return c

        ctasks = {m["mint"]: asyncio.create_task(get_candles(m)) for m in top}
        done, pending = await asyncio.wait(ctasks.values(), timeout=max(4.0, left() - 3))
        for t in pending:
            t.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
        for mint, t in ctasks.items():
            if t in done and not t.cancelled() and t.exception() is None:
                candle_map[mint] = t.result()

    ranked: list = []
    for m, sec in secure:
        res.watch.append((m, sec, "nosetup"))          # كل عملة آمنة تُراقب لارتداد مؤكد
        candles = candle_map.get(m["mint"], [])
        plans = []
        scalp_reason = None
        if candles:
            tf = candle_tf(m["age_min"])
            plan, reject = compute_scalp_plan(candles, m["price"], tf)
            if plan is not None and plan.status != "WAIT":
                plans.append(plan)
            elif reject:
                res.stats[reject] += 1
                scalp_reason = reject
                if reject.startswith("شموع غير كافية"):
                    scalp_reason = f"{reject} ({len(candles)}/{fib_params(tf)[0]} شمعة {tf}د)"
            else:
                scalp_reason = "السعر بعيد فوق المنطقة الذهبية (انتظر تصحيحاً)"
        if momentum_ok(m):
            plans.append(compute_alt_plan("MOMENTUM", m, candles))
        if hold_ok(m):
            plans.append(compute_alt_plan("HOLD", m, candles))
        if not plans:
            res.stats["لا تطابق أي نمط (سكالبيج/زخم/احتفاظ)"] += 1
            detail = (
                f"اجتازت الأمان لكن بلا نمط دخول — 1h {m['chg1']:+.0f}% • 5د {m['chg5']:+.0f}% • "
                f"وتيرة الفوليوم {m['vol_pace']:.1f}x • شراء 5د {m['buy_ratio5'] * 100:.0f}%"
            )
            detail += f" • سكالبيج: {scalp_reason}" if scalp_reason else " • سكالبيج: لم تُجلب شموع"
            set_v(m, "👁", detail + " — في المراقبة بانتظار ارتداد")
            continue
        for plan in plans:
            score, breakdown = compute_score(m, sec, plan)
            ranked.append(Candidate(m, sec, plan, score, breakdown))

    ranked.sort(key=lambda c: c.score, reverse=True)
    res.ranked = ranked
    for c in ranked:
        v = res.verdicts.get(c.m["mint"])
        if v is None or v["icon"] != "✅":
            fl = market_flags(c.m)
            set_v(c.m, "✅", f"Score {c.score} — {KIND_SHORT[c.plan.kind]} ({grade_of(c)})" + (" ⚠️ " + "، ".join(fl) if fl else ""))
    res.finished_at = time.time()
    return res


# ───────────────────────────── تنسيق الرسائل ─────────────────────────────


def format_best(c: Candidate, alts: list) -> str:
    m, s, p = c.m, c.sec, c.plan
    name, sym = html.escape(m["name"]), html.escape(m["symbol"])
    grade = grade_of(c)

    def mc(price: float) -> str:
        return fmt_usd(m["mcap"] * price / m["price"])

    tp1_pct = (p.tp1 / p.entry - 1) * 100
    tp2_pct = (p.tp2 / p.entry - 1) * 100
    risky = bool(s.warnings)
    dev_txt = f"{s.dev:.1f}%" if s.dev is not None else "غير متاح"
    rug_txt = f"خطورة {s.rug_norm:.0f}/100" if s.rug_norm is not None else f"raw {s.rug_raw:.0f}"
    bundle_txt = "🚨 مرصودة أنماط Bundled/Insider" if s.bundled else "✅ لا يوجد"
    b = c.breakdown

    head = (
        f"{grade} — <b>صفقة {html.escape(KIND_LABEL[p.kind])}</b>\n"
        f"📊 التقييم: <b>{c.score}/100</b>\n\n"
    )
    warn_block = ""
    flags = market_flags(m)
    if risky or flags:
        items = list(s.warnings) + flags
        note = (
            "اجتازت الحظر الصلب (Mint/Freeze/LP/Danger) لكنها ليست نظيفة — حجم صغير جداً."
            if risky
            else "اجتازت الحظر الصلب، لكنها صغيرة/حديثة: فرصة أكبر ومخاطرة أعلى — قلّل حجم الصفقة."
        )
        warn_block = "🚨 <b>تحذيرات:</b> " + " • ".join(html.escape(w) for w in items) + f"\n<i>{note}</i>\n\n"

    ident = (
        f"🪙 <b>{name}</b> (${sym})\n"
        f"📋 العقد CA (اضغط للنسخ):\n<code>{m['mint']}</code>\n\n"
    )

    safety = (
        f"🛡️ <b>الأمان والسيولة</b>\n"
        f"• Mint ✅ ملغاة | Freeze ✅ ملغاة\n"
        f"• LP: {s.lp_locked:.0f}% محروقة/مقفولة 🔒\n"
        f"• RugCheck: {rug_txt}\n"
        f"• السيولة: {fmt_usd(m['liq'])} | MC: {fmt_usd(m['mcap'])} | Liq/MC: {m['liq_mcap'] * 100:.1f}%\n"
        f"• 🐋 Top10: {s.top10:.1f}% | Dev: {dev_txt} | Bundles: {bundle_txt}\n\n"
        f"⚡ <b>الزخم</b>: 5m {m['chg5']:+.1f}% | 1h {m['chg1']:+.1f}% | 24h {m['chg24']:+.0f}%\n"
        f"• فوليوم 5m ×{m['vol_pace']:.1f} | ضغط الشراء {m['buy_ratio5'] * 100:.0f}%\n\n"
    )

    if p.kind == "SCALP":
        status = (
            "✅ السعر داخل المنطقة الذهبية الآن"
            if p.status == "IN_ZONE"
            else f"⏳ السعر أعلى المنطقة بـ {p.above_pct:.1f}% — انتظر الارتداد (Limit داخل المنطقة)"
        )
        plan_txt = (
            f"📈 <b>الخطة</b>\n{status}\n"
            f"🟢 Golden Zone (0.5–0.618):\n"
            f"   <code>{fmt_price(p.zone_low)}</code> ← <code>{fmt_price(p.zone_high)}</code>\n"
            f"   (MC: {mc(p.zone_low)} ← {mc(p.zone_high)})\n"
            f"🎯 TP1 (+{tp1_pct:.0f}%): <code>{fmt_price(p.tp1)}</code> | MC {mc(p.tp1)} | Fib 1.272: <code>{fmt_price(p.ext1272)}</code>\n"
            f"🎯 TP2 (+{tp2_pct:.0f}%): <code>{fmt_price(p.tp2)}</code> | MC {mc(p.tp2)} | Fib 1.618: <code>{fmt_price(p.ext1618)}</code>\n"
            f"🛑 SL (تحت القاع بـ 1.5%): <code>{fmt_price(p.sl)}</code> | MC {mc(p.sl)}\n"
            f"⚖️ مخاطرة حتى SL: {p.risk_pct:.1f}% | R:R → TP1 {p.rr1:.2f} / TP2 {p.rr2:.2f}\n"
            f"📐 Swing: {fmt_price(p.swing_low)} → {fmt_price(p.swing_high)} (+{p.leg_pct:.0f}%)\n\n"
        )
    else:
        note = (
            "لا تطارد السعر إن ارتفع أكثر من 5% عن منطقة الدخول."
            if p.kind == "MOMENTUM"
            else "صفقة أبطأ: راقبها يومياً، ولا تعتمد على شمعة واحدة."
        )
        plan_txt = (
            f"📈 <b>الخطة</b>\n"
            f"🟢 منطقة الدخول: <code>{fmt_price(p.zone_low)}</code> ← <code>{fmt_price(p.zone_high)}</code>\n"
            f"   (MC: {mc(p.zone_low)} ← {mc(p.zone_high)})\n"
            f"🎯 TP1 (+{tp1_pct:.0f}%): <code>{fmt_price(p.tp1)}</code> | MC {mc(p.tp1)}\n"
            f"🎯 TP2 (+{tp2_pct:.0f}%): <code>{fmt_price(p.tp2)}</code> | MC {mc(p.tp2)}\n"
            f"🛑 SL: <code>{fmt_price(p.sl)}</code> | MC {mc(p.sl)}\n"
            f"⚖️ مخاطرة حتى SL: {p.risk_pct:.1f}% | R:R → TP1 {p.rr1:.2f} / TP2 {p.rr2:.2f}\n"
            f"<i>{note}</i>\n\n"
        )

    breakdown = (
        f"<i>سيولة/MC {b['liq_mcap']:.0f}/20 • فوليوم {b['volume']:.0f}/20 • زخم {b['momentum']:.0f}/10 "
        f"• دخول {b['fib']:.0f}/25 • أمان {b['safety']:.0f}/25"
        f"{' • عقوبة −' + str(RISK_PENALTY) if risky else ''}"
        f"{' • عقوبة سوق −' + str(int(-b.get('market_risk', 0))) if b.get('market_risk') else ''}</i>\n"
    )
    links = (
        f"🔗 <a href=\"https://dexscreener.com/solana/{m['pair']}\">DexScreener</a> • "
        f"<a href=\"https://rugcheck.xyz/tokens/{m['mint']}\">RugCheck</a>\n"
    )

    alt_txt = ""
    if alts:
        alt_txt = "\n📋 <b>بدائل:</b>\n"
        for a in alts:
            alt_txt += (
                f"• {KIND_SHORT[a.plan.kind]} {html.escape(a.m['name'])} (${html.escape(a.m['symbol'])})"
                f" — {a.score}/100 {grade_of(a)}\n<code>{a.m['mint']}</code>\n"
            )

    disclaimer = (
        "\n⚠️ <i>تحليل آلي وليس نصيحة مالية. لا ضمان للربح — لا تدخل بأكثر مما تتحمل خسارته.</i>"
    )
    sig_txt = ""
    post_id = m.get("signal_post")
    if m.get("signal_manual") and not post_id:
        ts = m.get("signal_ts")
        sig_txt = "📥 <b>المصدر</b>: عقد أرسلتَه يدوياً" + (f" — الإشارة {fmt_age((time.time() - ts) / 60)}" if ts else "") + "\n\n"
    elif post_id:
        ts = m.get("signal_ts")
        age_min = (time.time() - ts) / 60 if ts else None
        when = fmt_age(age_min) if age_min is not None else "وقت غير معروف"
        sig_txt = (
            f"📡 <b>إشارة القناة</b>: {when} — "
            f"<a href=\"https://t.me/{SIGNAL_CHANNEL}/{post_id}\">الرسالة الأصلية</a>\n"
        )
        if age_min is not None and age_min > 120:
            sig_txt += "<i>الإشارة قديمة نسبياً — اعتمد على السعر والمنطقة الحاليين لا على سعر الإشارة.</i>\n"
        sig_txt += "\n"
    return head + warn_block + ident + sig_txt + safety + plan_txt + breakdown + links + alt_txt + disclaimer


def format_no_result(res: ScanResult) -> str:
    lines = [
        f"🚫 <b>لا توجد صفقة من إشارات @{html.escape(SIGNAL_CHANNEL)} تجتاز الحظر الصلب الآن</b>",
        "",
        f"📡 عقود مفحوصة (القناة + يدوي): {res.signals} | وُجد لها زوج: {res.discovered}",
        f"✅ اجتازت الفلتر الأولي: {res.after_prefilter}",
        f"🛡️ اجتازت الحظر الأمني الصلب: {res.after_security}",
    ]
    if res.stats:
        lines += ["", "📋 <b>أسباب الاستبعاد:</b>"]
        for reason, n in res.stats.most_common(12):
            lines.append(f"• {html.escape(reason)}: {n}")
    lines += ["", "💡 جرّب /diag لتشخيص قراءة القناة، أو انتظر إشارات جديدة ثم أعد المحاولة."]
    return "\n".join(lines)


def format_verdicts(res: ScanResult, limit: int = 14) -> str:
    items = sorted(res.verdicts.values(), key=lambda v: (0 if v["icon"] == "✅" else 1, -v["mcap"]))
    total = len(items) + len(res.unresolved)
    lines = [f"📋 <b>تقرير كل عقد ({total})</b>", ""]
    for v in items[:limit]:
        lines.append(f"{v['icon']} <b>{html.escape(v['sym'])}</b> ({fmt_usd(v['mcap'])}) — {html.escape(v['text'])}")
    room = max(0, limit - len(items))
    for a in res.unresolved[:room]:
        lines.append(f"❔ <code>{a[:5]}…{a[-4:]}</code> — لا بيانات سوق من DexScreener/GeckoTerminal (جديدة جداً أو غير مدرجة)")
    shown = min(len(items), limit) + min(len(res.unresolved), room)
    if total > shown:
        lines.append(f"… و{total - shown} أخرى")
    lines += ["", "💡 كل حد أعلاه قابل للتعديل من متغيرات البيئة (MIN_PAIR_AGE_MIN، MIN_LIQUIDITY_USD، MIN_VOLUME_H1_USD…)."]
    return "\n".join(lines)


def format_no_signals(res: ScanResult) -> str:
    info = res.sources if isinstance(res.sources, dict) else {}
    lines = [f"📡 <b>لم أجد عقوداً صالحة في قناة @{html.escape(SIGNAL_CHANNEL)}</b>", ""]
    if info.get("error"):
        lines.append(f"⚠️ {html.escape(str(info['error']))}")
    else:
        lines.append(f"• رسائل مقروءة: {info.get('messages', 0)} | وصلنا لرسائل أقدم من {SIGNAL_MAX_AGE_HOURS:.0f}h: {'نعم' if info.get('expired') else 'لا'}")
        lines.append(f"• رسائل فيها عقود: {info.get('with_addr', 0)}")
    lines += ["", "📥 <b>الوضع اليدوي</b>: حوّل لي (Forward) رسائل الإشارات من القناة، أو الصق العقود هنا، ثم أرسل /best."]
    if not TG_SESSION:
        lines += ["🔑 (للقراءة التلقائية: TELETHON_SESSION بجلسة حساب مستخدم.)"]
    lines += ["", "💡 جرّب /diag للتفاصيل، أو ارفع SIGNAL_MAX_AGE_HOURS إن كانت إشارات القناة قليلة."]
    return "\n".join(lines)


def to_plain(text: str) -> str:
    return html.unescape(re.sub(r"<[^>]+>", "", text))


# ───────────────────────────── أوامر تلغرام ─────────────────────────────

WELCOME = (
    "🎯 <b>SolAlpha Sniper</b>\n\n"
    "يقرأ آخر إشارات قناة @solhousesignal ويفحص عقودها، ثم يعطيك أفضل صفقة متاحة مع نوعها (سكالبيج / زخم / احتفاظ).\n\n"
    "• /best — أفضل صفقة من إشارات القناة + بدائل\n"
    "• أرسل لي عقداً (أو حوّل رسالة إشارة من القناة) ثم /best لتحليله\n"
    "• /clear — مسح العقود التي أرسلتها\n"
    "• /best يحلل العقود الجديدة فقط؛ السابقة تبقى تحت المراقبة دون إعادة تحليل (/best all لإعادة تحليل الكل)\n"
    "• /watchlist — العملات تحت المراقبة • /unwatch — مسحها\n"
    "• /report — تقرير نتائج الصفقات (آخر 24 ساعة) مع أسباب الفشل وملف تفاصيل\n"
    "• /diag — تشخيص قراءة القناة (Telethon)\n"
    "• /help — شرح الأنواع والفلاتر\n\n"
    "⚠️ أداة تحليل وليست نصيحة مالية."
)

HELP = (
    "🤖 <b>مسح السوق 24/24</b> يعمل تلقائياً ومستقلاً عن قائمتك: يلتقط العملات الناشئة (دقائق، MC 30–250K) والمُثبّتة بفحص أمني صارم وزخم حقيقي ويرسل الصفقات بحسب نوع السوق. القائمة تُنظَّف تلقائياً من العملات المنهارة/المشبوهة. /scan الحالة، /scan_now فحص فوري، /scan_off إيقاف، /scan_on لتحويله إلى محادثة/مجموعة أخرى.\n\n"
    "📤 <b>/export</b>: ملف شموع الدقيقة والإشارات لمعايرة كاشف الارتداد على كل عملاتك (أرسله لي).\n\n"
    "📍 <b>الدعم</b>: كل ارتداد مؤكد يُرسل لك كما هو، ويُوسم بقوة الدعم (✅ قوي / ⚠️ ضعيف) مع أقرب دعم تحته، وأنبّهك عند اقتراب السعر من دعم قوي. "
    "العملات لا تُحذف من القائمة تلقائياً إلا بانتهاء المدة (8 ساعات).\n\n"
    "👁 <b>المراقبة</b>: كل عملة آمنة (وكذلك الحديثة جداً، التي على Bonding Curve، الهابطة، أو التي صعدت كثيراً) تدخل قائمة المراقبة، "
    "وأنبّهك عند ارتداد مؤكد بعد تصحيح، أو عند نضجها/هجرتها واجتيازها الفحص الأمني.\n\n"
    "📥 <b>الوضع اليدوي</b>: الصق عنوان عقد أو حوّل (Forward) رسالة إشارة إلى البوت، ثم أرسل /best. "
    f"تبقى العقود {MANUAL_TTL_HOURS:.0f} ساعة أو حتى /clear.\n\n"
    f"📡 <b>المصدر</b>: عقود آخر رسائل @{SIGNAL_CHANNEL} فقط (آخر {SIGNAL_MAX_AGE_HOURS:.0f} ساعة) — لا مسح عشوائي للسوق.\n\n"
    "📋 بعد كل /best تصلك رسالة «تقرير كل عقد» بسبب قبول/رفض كل عملة بالأرقام.\n\n"
    f"🔎 <b>فلتر أولي</b>: سيولة ≥ {fmt_usd(MIN_LIQUIDITY_USD)} • MC ≥ {fmt_usd(MIN_MCAP_USD)} • عمر ≥ {MIN_PAIR_AGE_MIN:.0f}د — وما دون {fmt_usd(FLAG_LIQ_USD)} سيولة أو {FLAG_AGE_MIN:.0f}د عمر يُقبل بتحذير وعقوبة.\n\n"
    "🛡️ <b>حظر صلب دائماً</b>: Mint/Freeze ملغاتان • LP محروقة/مقفولة • ليست Rugged • لا Danger في RugCheck.\n\n"
    f"🧼 <b>المستوى A (نظيف)</b>: Dev ≤ {MAX_DEV_PCT:.0f}% • Top10 ≤ {MAX_TOP10_PCT:.0f}% • بلا Bundled/Insider • RugCheck Good.\n"
    "⚠️ <b>المستوى B</b>: يظهر مع تحذيرات صريحة وعقوبة في التقييم.\n\n"
    "<b>الأنواع</b>\n"
    "⚡ سكالبيج: دخول من Golden Zone، TP +18%/+25%\n"
    "🚀 زخم: صعود بفوليوم متسارع أو اندفاع سعري بسيطرة مشترين، TP +15%/+30%\n"
    "🧭 احتفاظ: عملة 24h+ بسيولة وفوليوم جيدين، TP +50%/+100%\n\n"
    f"🟢 قوية: A وScore ≥ {MIN_SCORE:.0f} • 🟡 متوسطة: ≥ {RISKY_MIN_SCORE:.0f} • 🟠 ضعيفة: أقل."
)


async def post_init(application: Application) -> None:
    application.bot_data["http"] = httpx.AsyncClient(
        timeout=httpx.Timeout(12.0),
        headers={"User-Agent": "SolAlphaSniper/5.0", "Accept": "application/json"},
        follow_redirects=True,
    )
    application.bot_data["scan_lock"] = asyncio.Lock()
    application.bot_data["cooldowns"] = {}
    application.bot_data["last_scan"] = None
    application.bot_data["watch"] = load_watch()
    application.bot_data["scan"] = load_scan_state()
    application.bot_data["track"] = load_track()
    TL["client"], TL["error"] = await create_telethon()


async def post_shutdown(application: Application) -> None:
    client = application.bot_data.get("http")
    if client:
        await client.aclose()
    if TL.get("client"):
        try:
            await TL["client"].disconnect()
        except Exception:
            pass


async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.effective_message.reply_text(WELCOME, parse_mode=ParseMode.HTML)


async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.effective_message.reply_text(HELP, parse_mode=ParseMode.HTML)


def _cooldown_wait(bd: dict, user_id: int) -> int:
    now = time.time()
    last = bd["cooldowns"].get(user_id, 0)
    if now - last < USER_COOLDOWN_SEC:
        return int(USER_COOLDOWN_SEC - (now - last)) + 1
    bd["cooldowns"][user_id] = now
    return 0


async def safe_edit(status_msg, original_msg, text: str) -> None:
    """يضمن وصول الرد دائماً: HTML ثم نص عادي ثم رسالة جديدة."""
    try:
        await status_msg.edit_text(text, parse_mode=ParseMode.HTML, disable_web_page_preview=True)
        return
    except Exception:
        log.exception("HTML edit failed")
    plain = to_plain(text)[:4000]
    try:
        await status_msg.edit_text(plain, disable_web_page_preview=True)
        return
    except Exception:
        log.exception("Plain edit failed")
    try:
        await original_msg.reply_text(plain, disable_web_page_preview=True)
    except Exception:
        log.exception("Reply failed")


async def safe_send(msg, text: str) -> None:
    try:
        await msg.reply_text(text, parse_mode=ParseMode.HTML, disable_web_page_preview=True)
    except Exception:
        log.exception("HTML send failed")
        try:
            await msg.reply_text(to_plain(text)[:4000], disable_web_page_preview=True)
        except Exception:
            log.exception("Plain send failed")


# ───────────────────────────── المراقبة: انتظار الارتداد وتنبيهك ─────────────────────────────
WATCH_INTERVAL_SEC = _env_float("WATCH_INTERVAL_SEC", 45)
WATCH_TTL_HOURS = _env_float("WATCH_TTL_HOURS", 8)
WATCH_MAX_PER_USER = int(_env_float("WATCH_MAX_PER_USER", 30))
WATCH_CANDLES_PER_CYCLE = int(_env_float("WATCH_CANDLES_PER_CYCLE", 6))
WATCH_SEC_PER_CYCLE = int(_env_float("WATCH_SEC_PER_CYCLE", 4))
BOUNCE_MIN_PCT = _env_float("BOUNCE_MIN_PCT", 3)          # أقل ارتداد من القاع لاعتباره بدأ (كان 4)
BOUNCE_MAX_PCT = _env_float("BOUNCE_MAX_PCT", 25)         # أكثر منه = فات الدخول المبكر
BOUNCE_MIN_RR1 = _env_float("BOUNCE_MIN_RR1", 0)          # بوابة عائد/مخاطرة (0 = معطّلة: نُوسم الضعيف ولا نمنعه)
FAIL_STREAK = int(_env_float("FAIL_STREAK", 2))           # فشلان متتاليان → كتم عملة (0 = معطّل)
FAIL_MUTE_MIN = _env_float("FAIL_MUTE_MIN", 90)
SAME_ZONE_CD_MIN = _env_float("SAME_ZONE_CD_MIN", 45)     # بعد الفشل: لا تنبيه من نفس المنطقة
SUPPORT_ALERT_MIN_MC = _env_float("SUPPORT_ALERT_MIN_MC", 20000)
SUPPORT_ALERT_COOLDOWN_MIN = _env_float("SUPPORT_ALERT_COOLDOWN_MIN", 180)
HIST_MAX = 1500                                           # شموع دقيقة محفوظة لكل عملة للتصدير
BOUNCE_MIN_RETRACE = _env_float("BOUNCE_MIN_RETRACE", 0.382)
BOUNCE_SL_BUFFER = 0.02
WATCH_FILE = os.getenv("WATCH_FILE", "watchlist.json")
WATCH_DEAD_PCT = _env_float("WATCH_DEAD_PCT", 0)   # 0 = معطّل. >0: انهيار بهذه النسبة يوقف تنبيهات العملة (تبقى في القائمة)

WATCH_KIND_TXT = {
    "young": "⏳ حديثة — بانتظار نضج البيانات",
    "bonding": "⏳ على Bonding Curve — بانتظار الهجرة",
    "dip": "📉 هابطة — بانتظار قاع وارتداد",
    "extended": "📈 صعدت كثيراً — بانتظار تصحيح",
    "nosetup": "👁 آمنة — بانتظار ارتداد",
}


def watch_kind_for(reason: str) -> Optional[str]:
    if reason == "عمر الزوج قصير جداً":
        return "young"
    if reason.startswith("لم تهاجر"):
        return "bonding"
    if reason == "انهيار حاد في السعر":
        return "dip"
    if reason.startswith("ارتفاع مفرط"):
        return "extended"
    return None


def sec_brief(sec: "Security") -> str:
    parts = [f"Top10 {sec.top10:.0f}%"]
    if sec.dev is not None:
        parts.append(f"Dev {sec.dev:.1f}%")
    if sec.lp_locked is not None:
        parts.append(f"LP {sec.lp_locked:.0f}%")
    if sec.rug_norm is not None:
        parts.append(f"RugCheck {sec.rug_norm:.0f}/100")
    return " • ".join(parts)


def save_watch(bd: dict) -> None:
    try:
        tmp = WATCH_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(list(bd.get("watch", {}).values()), f, ensure_ascii=False)
        os.replace(tmp, WATCH_FILE)
    except Exception as e:
        log.warning("save_watch failed: %s", e)


def load_watch() -> dict:
    try:
        with open(WATCH_FILE, encoding="utf-8") as f:
            return {it["key"]: it for it in json.load(f)}
    except FileNotFoundError:
        return {}
    except Exception as e:
        log.warning("load_watch failed: %s", e)
        return {}


def add_watch(bd: dict, uid: int, chat_id: int, entries: list) -> list:
    store = bd.setdefault("watch", {})
    added = []
    for m, sec, kind in entries:
        key = f"{uid}:{m['mint']}"
        if key in store:
            continue
        mine = [k for k, it in store.items() if it["uid"] == uid]
        if len(mine) >= WATCH_MAX_PER_USER:
            store.pop(min(mine, key=lambda k: store[k]["added"]), None)
        store[key] = {
            "key": key, "uid": uid, "chat": chat_id, "mint": m["mint"], "pair": m["pair"],
            "sym": m["symbol"], "name": m["name"], "kind": kind,
            "mcap0": m["mcap"], "price0": m["price"], "price": m["price"], "mcap": m["mcap"], "dex": m["dex"],
            "factor": (m["mcap"] / m["price"]) if m["price"] > 0 else 0.0,
            "added": time.time(), "sec_ok": True if sec is not None else None,
            "sec_txt": sec_brief(sec) if sec is not None else "", "setup": None, "alerted": [],
            "candle_ts": 0.0, "sec_tries": 0, "why": "",
        }
        added.append(store[key])
    if added:
        save_watch(bd)
    return added


def format_watch_added(added: list) -> str:
    lines = [f"👁 <b>أضفت {len(added)} عملة لقائمة المراقبة</b>", ""]
    for it in added[:8]:
        lines.append(f"• <b>{html.escape(it['sym'])}</b> ({fmt_usd(it['mcap'])}) — {WATCH_KIND_TXT.get(it['kind'], '')}")
    if len(added) > 8:
        lines.append(f"… و{len(added) - 8} أخرى")
    lines += [
        "",
        "سأنبّهك هنا عندما:",
        "• 🔔 يتأكد ارتداد (قاع + شمعتان خضراوان + ضغط شراء) مع الدخول/الأهداف/الوقف",
        "• ✅ تنضج العملة الحديثة أو تهاجر وتجتاز الفحص الأمني",
        "• 🎯🛑 تتحقق الأهداف أو يُضرب الوقف",
        "",
        "/watchlist لعرضها • /unwatch لمسحها",
    ]
    return "\n".join(lines)


def detect_bounce(candles: list, m: dict):
    """يكشف ارتداداً مؤكداً بعد موجة صاعدة وتصحيح. يرجع (setup|None، سبب_عدم_الإشارة)."""
    if len(candles) < 30:
        return None, f"شموع قليلة ({len(candles)})"
    w = candles[:-1][-120:]                       # شموع مغلقة فقط
    price = m["price"]
    hi_i = max(range(len(w)), key=lambda i: w[i][2])
    peak = w[hi_i][2]
    if hi_i > len(w) - 6:
        return None, "القمة حديثة — لم يبدأ التصحيح بعد"
    leg_low = min(c[3] for c in w[max(0, hi_i - 60): hi_i + 1])
    if leg_low <= 0 or (peak / leg_low - 1) * 100 < MIN_LEG_PCT:
        return None, "لا موجة صاعدة واضحة قبل القمة"
    after = w[hi_i + 1:]
    k = min(range(len(after)), key=lambda i: after[i][3])
    trough = after[k][3]
    tr_i = hi_i + 1 + k
    if tr_i > len(w) - 3:
        return None, "القاع حديث جداً — ما زال يهبط أو لم يتأكد"
    retr = (peak - trough) / (peak - leg_low) if peak > leg_low else 0.0
    if retr < BOUNCE_MIN_RETRACE:
        return None, f"تصحيح ضحل ({retr:.0%})"
    if trough < leg_low * 0.9:
        return None, "هبط تحت بداية الموجة بوضوح — فشل الهيكل"
    bounce = (price / trough - 1) * 100
    if bounce < BOUNCE_MIN_PCT:
        return None, f"لم يرتدّ بعد (+{bounce:.1f}% من القاع)"
    if bounce > BOUNCE_MAX_PCT:
        return None, f"ارتد كثيراً (+{bounce:.0f}%) — فات الدخول المبكر"
    greens = sum(1 for c in w[-3:] if c[4] > c[1])
    if greens < 2:
        return None, "لا شمعتان خضراوان من آخر ثلاث"
    if m["buy_ratio5"] < 0.5:
        return None, f"ضغط البيع أعلى (شراء {m['buy_ratio5'] * 100:.0f}%)"
    sl = trough * (1 - BOUNCE_SL_BUFFER)
    risk_pct = (price - sl) / price * 100
    if risk_pct <= 0 or risk_pct > MAX_SL_RISK_PCT:
        return None, f"وقف الخسارة بعيد ({risk_pct:.0f}%)"
    tp1 = trough + 0.382 * (peak - trough)
    tp2 = trough + 0.618 * (peak - trough)
    if tp1 < price * 1.05:
        return None, "الهدف الأول قريب جداً من السعر"
    rr1_ = (tp1 - price) / (price - sl)
    if BOUNCE_MIN_RR1 > 0 and rr1_ < BOUNCE_MIN_RR1:
        return None, f"إشارة متأخرة: عائد/مخاطرة {rr1_:.1f} < {BOUNCE_MIN_RR1:.1f}"
    recent = [c[5] for c in w[-3:]]
    prev = [c[5] for c in w[-13:-3]]
    vol_up = bool(prev) and sum(prev) > 0 and (sum(recent) / len(recent)) > 1.2 * (sum(prev) / len(prev))
    stars = 1 + int(vol_up) + int(m["buy_ratio5"] >= 0.6) + int(bounce <= 12)
    return {
        "key": str(int(w[tr_i][0])), "peak": peak, "trough": trough, "leg_low": leg_low, "retr": retr,
        "bounce": bounce, "entry": price, "sl": sl, "tp1": tp1, "tp2": tp2, "tp3": peak,
        "risk_pct": risk_pct, "rr1": (tp1 - price) / (price - sl), "rr2": (tp2 - price) / (price - sl),
        "stars": stars, "vol_up": vol_up, "buy": m["buy_ratio5"], "at": time.time(),
        "hi_ratio": price / max(c[2] for c in candles), "trough_ts": w[tr_i][0],
    }, None


def format_bounce_alert(it: dict, m: dict, s: dict) -> str:
    f = it.get("factor") or ((m["mcap"] / m["price"]) if m["price"] > 0 else 0.0)

    def mc(p: float) -> str:
        return fmt_usd(p * f) if f else "—"

    stars = "★" * s["stars"] + "☆" * (4 - s["stars"])
    notes = ["قاع أعلى", "شمعتان خضراوان", f"شراء {s['buy'] * 100:.0f}%"] + (["فوليوم الارتداد أعلى"] if s["vol_up"] else [])
    return (
        f"🔔 <b>ارتداد محتمل — {html.escape(it['sym'])}</b>  {stars}\n\n"
        f"📋 <code>{it['mint']}</code>\n\n"
        f"📉 القمة {mc(s['peak'])} ← القاع {mc(s['trough'])} (تصحيح {s['retr']:.0%}) ← الآن {mc(s['entry'])} (+{s['bounce']:.0f}% من القاع)\n"
        f"✅ {' • '.join(notes)}\n\n"
        f"🟢 دخول: {fmt_price(s['entry'])} | MC {mc(s['entry'])}\n"
        f"🎯 TP1: {fmt_price(s['tp1'])} (+{(s['tp1'] / s['entry'] - 1) * 100:.0f}%) | MC {mc(s['tp1'])}\n"
        f"🎯 TP2: {fmt_price(s['tp2'])} (+{(s['tp2'] / s['entry'] - 1) * 100:.0f}%) | MC {mc(s['tp2'])}\n"
        f"🏁 القمة السابقة: {fmt_price(s['tp3'])} | MC {mc(s['tp3'])}\n"
        f"🛑 SL: {fmt_price(s['sl'])} (−{s['risk_pct']:.0f}%) | MC {mc(s['sl'])}\n"
        f"⚖️ R:R → TP1 {s['rr1']:.1f} / TP2 {s['rr2']:.1f}\n"
        f"🛡️ {html.escape(it.get('sec_txt') or '—')}\n\n"
        f"🔗 <a href=\"https://dexscreener.com/solana/{m['pair']}\">DexScreener</a> • "
        f"<a href=\"https://rugcheck.xyz/tokens/{it['mint']}\">RugCheck</a>\n\n"
        "⚠️ كشف آلي لارتداد بعد تصحيح — لا يضمن استمراره. لا تدخل بأكثر مما تتحمل خسارته."
    )


# ───────────────────────────── الدعم القوي (معلومة وتنبيه اقتراب — لا يمنع أي إشارة) ─────────────────────────────
SUPPORT_MIN_STRENGTH = _env_float("SUPPORT_MIN_STRENGTH", 3)      # قوة تُعدّ "قوية" للوسم والتنبيه
SUPPORT_TOL_PCT = _env_float("SUPPORT_TOL_PCT", 3)                # تسامح القاع مع حدود الدعم
SUPPORT_NEAR_PCT = _env_float("SUPPORT_NEAR_PCT", 6)              # تنبيه الاقتراب
SUPPORT_ALERTS = os.getenv("SUPPORT_ALERTS", "1") != "0"          # 0 = بلا تنبيه اقتراب
REGIME_MIN_RESOLVED = 6
REGIME_LABEL = {"good": "🟢 جيد", "neutral": "🟡 محايد", "bleeding": "🔴 نازف (الدعم يُكسر)"}


def to_5m(c1: list) -> list:
    out: list = []
    cur = None
    for c in c1:
        b = int(c[0] // 300)
        if cur is None or cur[0] != b:
            if cur:
                out.append((cur[0] * 300, cur[1], cur[2], cur[3], cur[4], cur[5]))
            cur = [b, c[1], c[2], c[3], c[4], c[5]]
        else:
            cur[2], cur[3], cur[4], cur[5] = max(cur[2], c[2]), min(cur[3], c[3]), c[4], cur[5] + c[5]
    if cur:
        out.append((cur[0] * 300, cur[1], cur[2], cur[3], cur[4], cur[5]))
    return out


def find_supports(candles: list, k: int = 3, min_sep: int = 6, launch: bool = False) -> list:
    """مناطق دعم أفقية من قيعان محورية مجمّعة بنسبة 3%.
    القوة = لمسات متباعدة (حتى 4) + فوليوم عند اللمس + أدنى قاع + قاعدة الإطلاق."""
    cs = candles[:-1]
    if len(cs) < 40:
        return []
    vols = sorted(c[5] for c in cs)
    med = vols[len(vols) // 2] or 1e-12
    gmin = min(c[3] for c in cs)
    piv = []
    for i in range(k, len(cs) - k):
        lo = cs[i][3]
        if lo > 0 and lo <= min(c[3] for c in cs[i - k:i + k + 1]):
            piv.append((i, lo, cs[i][5]))
    piv.sort(key=lambda t: t[1])
    clusters: list = []
    for p in piv:
        if clusters and p[1] <= clusters[-1][0][1] * 1.03:
            clusters[-1].append(p)
        else:
            clusters.append([p])
    zones = []
    for cl in clusters:
        idx = sorted(p[0] for p in cl)
        distinct = 1 + sum(1 for a, b in zip(idx, idx[1:]) if b - a >= min_sep)
        lows = [p[1] for p in cl]
        level = sum(lows) / len(lows)
        vol_x = (sum(p[2] for p in cl) / len(cl)) / med
        gl = level <= gmin * 1.03
        base = launch and idx[0] <= 8
        strength = min(distinct, 4) + int(vol_x >= 1.5) + int(gl) + int(base)
        zl, zh = min(min(lows), level * 0.985), max(max(lows), level * 1.015)
        rn = 24 if k == 3 else 60
        reactions = 0
        for p in cl:
            nxt = cs[p[0] + 1: p[0] + 1 + rn]
            if nxt and max(c[2] for c in nxt) >= level * 1.10:
                reactions += 1
        broken = any(c[4] < zl * 0.97 for c in cs[idx[0] + 1:])
        zones.append({
            "level": level, "low": zl, "high": zh, "touches": distinct, "strength": strength,
            "vol_x": round(vol_x, 1), "gl": gl, "base": base, "reactions": reactions, "broken": broken,
        })
    return sorted(zones, key=lambda z: -z["strength"])[:8]


def find_supports_1m(c1: list, age_min: Optional[float] = None) -> list:
    """دعم من نفس شموع الدقيقة المجلوبة للارتداد (بلا أي طلب إضافي): 5د مجمّعة محلياً إن توفر 10 ساعات."""
    launch = age_min is not None and age_min <= 960
    if len(c1) >= 600:
        return find_supports(to_5m(c1), 3, 6, launch)
    return find_supports(c1, 5, 12, launch)


def compute_regime(bd: dict) -> dict:
    """حالة السوق (للعرض فقط): نسبة نجاح آخر ارتدادات محسومة + اتساع الهبوط في قائمة المراقبة."""
    recs = sorted((r for r in (bd.get("track") or {}).values()
                   if r.get("decision") == "bounce" and r.get("first") in ("TP1", "SL")), key=lambda r: r["ts"])[-8:]
    wr = (sum(1 for r in recs if r["first"] == "TP1") / len(recs)) if len(recs) >= REGIME_MIN_RESOLVED else None
    items = [it for it in (bd.get("watch") or {}).values() if it.get("sec_ok") and it.get("chg1") is not None]
    breadth = (sum(1 for it in items if it["chg1"] <= -15) / len(items)) if len(items) >= 4 else None
    state = "neutral"
    if (wr is not None and wr <= 0.25) or (breadth is not None and breadth >= 0.7):
        state = "bleeding"
    elif wr is not None and wr >= 0.5 and (breadth is None or breadth < 0.4):
        state = "good"
    return {"state": state, "wr": wr, "breadth": breadth}


def annotate_setup(s: dict, zones: list, regime_state: str) -> None:
    """يضيف للإشارة معلومات الدعم دون تغيير شروطها أو منعها."""
    s["regime"] = regime_state
    tol = SUPPORT_TOL_PCT / 100
    near = [z for z in zones if z["low"] * (1 - tol) <= s["trough"] <= z["high"] * (1 + tol)]
    sup = max(near, key=lambda z: z["strength"]) if near else None
    s["support"] = sup
    s["weak"] = sup is None or sup["strength"] < SUPPORT_MIN_STRENGTH
    below = [z for z in zones if z["strength"] >= SUPPORT_MIN_STRENGTH and z["high"] < s["trough"] * 0.99]
    s["next_support"] = max(below, key=lambda z: z["high"]) if below else None


def _mc(it: dict, m: dict, p: float) -> str:
    f = it.get("factor") or ((m["mcap"] / m["price"]) if m.get("price") else 0.0)
    return fmt_usd(p * f) if f else "—"


def support_text(it: dict, m: dict, s: dict) -> str:
    sup = s.get("support")
    lines = []
    if sup and not s.get("weak"):
        why = [f"{sup['touches']} لمسات"]
        if sup.get("gl"):
            why.append("أدنى قاع")
        if sup.get("base"):
            why.append("قاعدة الإطلاق")
        if sup["vol_x"] >= 1.5:
            why.append(f"فوليوم ×{sup['vol_x']}")
        lines.append(f"📍 <b>ارتداد من دعم قوي</b> MC {_mc(it, m, sup['low'])}–{_mc(it, m, sup['high'])} "
                     f"(قوة {sup['strength']}: {'، '.join(why)})")
    else:
        lines.append("⚠️ <b>بلا دعم قوي مؤكد</b> — ارتداد أضعف، قلّل الحجم")
    ns = s.get("next_support")
    if ns:
        drop = (ns["high"] / s["entry"] - 1) * 100
        lines.append(f"↘️ إن كُسر الوقف: أقرب دعم قوي تحته MC {_mc(it, m, ns['low'])}–{_mc(it, m, ns['high'])} ({drop:+.0f}% عن الدخول)")
    if s.get("rr1") is not None and s["rr1"] < 1.2:
        lines.append(f"⚠️ عائد/مخاطرة TP1 = {s['rr1']:.1f} (ضعيف: الإشارة متأخرة أو الوقف بعيد)")
    hr = s.get("hi_ratio")
    if hr is not None and hr < 0.5:
        lines.append(f"📉 يبعد {(1 - hr) * 100:.0f}% عن قمة آخر 16 ساعة — ارتداد داخل هبوط عام")
    lines.append(f"🌡️ السوق: {REGIME_LABEL.get(s.get('regime', 'neutral'), '')}")
    return "\n".join(lines) + "\n\n"


def inject_support(text: str, it: dict, m: dict, s: dict) -> str:
    block = support_text(it, m, s)
    i = text.find("🟢 دخول:")
    return text[:i] + block + text[i:] if i > 0 else text + "\n" + block


def next_support_note(it: dict, s: dict) -> str:
    ns = s.get("next_support")
    if not ns:
        return ""
    f = it.get("factor") or 0.0
    if not f:
        return ""
    return f"\n📍 أقرب دعم قوي تحته: MC {fmt_usd(ns['low'] * f)}–{fmt_usd(ns['high'] * f)} — أنبّهك إن اقترب منه."


async def maybe_support_alert(app: Application, it: dict, m: dict, zones: list) -> None:
    """اقتراب من دعم قوي *مُثبت* (لمسات ارتدت ≥10% ولم يُكسر) في عملة غير منهارة وغير مكتومة."""
    price = m["price"]
    now = time.time()
    if not SUPPORT_ALERTS or price <= 0 or not zones:
        return
    if m["mcap"] < SUPPORT_ALERT_MIN_MC or now < it.get("mute_until", 0) or it.get("hi_ratio", 1.0) < 0.35:
        return
    if now - it.get("sup_alert_ts", 0) < SUPPORT_ALERT_COOLDOWN_MIN * 60:
        return
    near = [z for z in zones if z["strength"] >= SUPPORT_MIN_STRENGTH and z.get("reactions", 0) >= 2
            and not z.get("broken", False)
            and z["low"] * (1 - SUPPORT_TOL_PCT / 100) <= price <= z["high"] * (1 + SUPPORT_NEAR_PCT / 100)]
    if not near:
        return
    z = max(near, key=lambda x: x["strength"])
    it["sup_alert_ts"] = now
    dist = (price / z["high"] - 1) * 100
    where = "داخل الدعم" if dist <= 0 else f"يبعد {dist:.1f}% فوقه"
    await notify(app, it, (
        f"📍 <b>{html.escape(it['sym'])}</b> تقترب من دعم مُثبت (قوة {z['strength']} • {z['touches']} لمسات • "
        f"{z['reactions']} ارتدادات ≥10% • لم يُكسر)\n"
        f"الدعم: MC {_mc(it, m, z['low'])}–{_mc(it, m, z['high'])} • الآن {fmt_usd(m['mcap'])} ({where})\n"
        f"📋 <code>{it['mint']}</code>\n"
        "أراقب ارتداداً مؤكداً منه وأنبّهك — لا تدخل قبل التأكيد."))


def api_line() -> str:
    parts = []
    for host, name in (("api.dexscreener.com", "DexScreener"), ("api.geckoterminal.com", "GeckoTerminal"),
                       ("api.rugcheck.xyz", "RugCheck")):
        st = API_STATUS.get(host)
        if st:
            parts.append(f"{name} {st[0].replace('HTTP ', '')}" + ("" if st[0] == "HTTP 200" else " ⚠️"))
    return ("🌐 " + " • ".join(parts)) if parts else ""


def store_hist(bd: dict, it: dict, candles: list) -> None:
    if not candles:
        return
    h = bd.setdefault("hist", {})
    cur = h.get(it["mint"])
    d = {int(c[0]): c for c in (cur["c"] if cur else [])}
    for c in candles:
        d[int(c[0])] = c
    ts = sorted(d)[-HIST_MAX:]
    h[it["mint"]] = {"sym": it["sym"], "c": [list(d[t]) for t in ts]}


async def export_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """ملف بيانات كامل (شموع 1د مع الفوليوم + الإشارات + القائمة) لتحليل أعمق ومعايرة الكاشف."""
    msg = update.effective_message
    bd = context.application.bot_data
    uid = update.effective_user.id
    watch = [{k: v for k, v in it.items() if k != "px"} for it in (bd.get("watch") or {}).values() if it["uid"] == uid]
    mints = {it["mint"] for it in watch}
    track = [r for r in (bd.get("track") or {}).values() if r["uid"] == uid]
    mints |= {r["mint"] for r in track}
    hist = {m_: v for m_, v in (bd.get("hist") or {}).items() if m_ in mints}
    payload = {"version": "4.4", "ts": time.time(), "regime": bd.get("regime"), "watch": watch,
               "tracker": track, "hist": hist}
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    n = sum(len(v["c"]) for v in hist.values())
    try:
        await msg.reply_document(
            document=io.BytesIO(data), filename=f"solalpha_export_{time.strftime('%Y%m%d_%H%M')}.json",
            caption=f"📤 {len(hist)} عملة • {n} شمعة دقيقة • {len(track)} إشارة. أرسل لي الملف لنحلل كل الارتدادات ونعايرالكاشف.")
    except Exception:
        log.exception("export failed")
        await msg.reply_text("❌ تعذر إرسال الملف. راجع Logs.")
    if not hist:
        await msg.reply_text("ℹ️ لا شموع محفوظة بعد: تُجمع مع كل فحص للمراقبة (انتظر بضع دقائق بعد إضافة العملات).")


# ───────────────────────────── مسح السوق 24/24 (v5): ناشئة + مُثبّتة، مستقل تماماً عن قائمة المراقبة ─────────────────────────────
SCAN_STATE_FILE = os.getenv("SCAN_STATE_FILE", "scan_state.json")
SCAN_CHAT_ID = int(os.getenv("SCAN_CHAT_ID", "0") or 0)
SCAN_DEFAULT_ON = os.getenv("SCAN_DEFAULT_ON", "1") != "0"
SCAN_INTERVAL_SEC = _env_float("SCAN_INTERVAL_SEC", 120)          # أسرع ليلتقط العملات الجديدة
SCAN_HEARTBEAT_H = _env_float("SCAN_HEARTBEAT_H", 3)
SCAN_CYCLE_TIMEOUT = _env_float("SCAN_CYCLE_TIMEOUT", 170)        # لا تتجاوز الدورة هذا الحد أبداً
# مسار العملات المُثبّتة (جديدة نسبياً وكبيرة)
SCAN_MIN_MC = _env_float("SCAN_MIN_MC", 60_000)
SCAN_MAX_MC = _env_float("SCAN_MAX_MC", 100_000_000)
SCAN_MIN_LIQ = _env_float("SCAN_MIN_LIQ", 15_000)
SCAN_MIN_VOL1H = _env_float("SCAN_MIN_VOL1H", 8_000)
SCAN_MIN_AGE_MIN = _env_float("SCAN_MIN_AGE_MIN", 30)
SCAN_MAX_AGE_H = _env_float("SCAN_MAX_AGE_H", 72)
# مسار العملات الناشئة (دقائق معدودة، ماركت كاب صغير)
ALPHA_ON = os.getenv("ALPHA_ON", "1") != "0"
ALPHA_MIN_AGE_MIN = _env_float("ALPHA_MIN_AGE_MIN", 3)
ALPHA_MAX_AGE_MIN = _env_float("ALPHA_MAX_AGE_MIN", 240)
ALPHA_MIN_MC = _env_float("ALPHA_MIN_MC", 30_000)
ALPHA_MAX_MC = _env_float("ALPHA_MAX_MC", 250_000)
ALPHA_MIN_LIQ = _env_float("ALPHA_MIN_LIQ", 7_000)
ALPHA_MIN_VOL1H = _env_float("ALPHA_MIN_VOL1H", 4_000)
ALPHA_MIN_TXN5 = _env_float("ALPHA_MIN_TXN5", 15)
ALPHA_MIN_CHG5 = _env_float("ALPHA_MIN_CHG5", 2.0)
ALPHA_MIN_CHG1 = _env_float("ALPHA_MIN_CHG1", 8.0)
ALPHA_MAX_CHG1 = _env_float("ALPHA_MAX_CHG1", 250.0)
ALPHA_MIN_BUY = _env_float("ALPHA_MIN_BUY", 0.55)
ALPHA_MIN_SCORE = int(_env_float("ALPHA_MIN_SCORE", 55))
# أمان وسقوف
SCAN_MAX_TOP10 = _env_float("SCAN_MAX_TOP10", 35)
SCAN_MAX_ALERTS_HOUR = int(_env_float("SCAN_MAX_ALERTS_HOUR", 4))
SCAN_COIN_COOLDOWN_H = _env_float("SCAN_COIN_COOLDOWN_H", 4)
SCAN_RUG_PER_CYCLE = int(_env_float("SCAN_RUG_PER_CYCLE", 8))
SCAN_CANDLES_PER_CYCLE = int(_env_float("SCAN_CANDLES_PER_CYCLE", 8))
SCAN_MAX_DISCOVER = 240
SCAN_UNIVERSE_H = 12
SCAN_MOMENTUM = os.getenv("SCAN_MOMENTUM", "1") != "0"
SCAN_MIN_SCORE = int(_env_float("SCAN_MIN_SCORE", 75))
SCAN_REGIME_LABEL = {"hot": "🟢 صاعد", "neutral": "🟡 محايد", "bleeding": "🔴 نازف", "unknown": "⚪ غير كافٍ"}


def load_scan_state() -> dict:
    st = {"on": SCAN_DEFAULT_ON, "chat": 0, "off_by_user": False}
    try:
        with open(SCAN_STATE_FILE, encoding="utf-8") as f:
            d = json.load(f)
        st["chat"] = int(d.get("chat") or 0)
        st["off_by_user"] = bool(d.get("off_by_user"))
        st["on"] = bool(d.get("on")) if "on" in d else st["on"]
    except FileNotFoundError:
        pass
    except Exception as e:
        log.warning("load_scan_state failed: %s", e)
    if SCAN_CHAT_ID and not st["chat"]:
        st["chat"], st["on"] = SCAN_CHAT_ID, True
    return st


def save_scan_state(sc: dict) -> None:
    try:
        tmp = SCAN_STATE_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"on": bool(sc.get("on")), "chat": int(sc.get("chat") or 0),
                       "off_by_user": bool(sc.get("off_by_user"))}, f)
        os.replace(tmp, SCAN_STATE_FILE)
    except Exception as e:
        log.warning("save_scan_state failed: %s", e)


def _pool_mints(data: Any) -> list:
    out = []
    if isinstance(data, dict):
        for p in data.get("data") or []:
            try:
                bid = (((p.get("relationships") or {}).get("base_token") or {}).get("data") or {}).get("id", "")
                addr = bid.split("_", 1)[1] if "_" in bid else ""
                if addr:
                    out.append(addr)
            except Exception:
                continue
    return out


async def discover_candidates(client: httpx.AsyncClient):
    """مصادر مجانية متوازية بمهلة لكل مصدر: GeckoTerminal (أحدث الأزواج صفحات 1–3 + Trending 5د/1س)
    و DexScreener (ملفات/تعزيزات). فشل أي مصدر لا يوقف البقية. يرجع (قائمة فريدة، {مصدر: عدد أو -1})."""
    found: dict = {}
    stats: dict = {}

    async def dex(name: str, url: str):
        try:
            data = await asyncio.wait_for(fetch_json(client, url), timeout=25)
        except Exception:
            return name, None
        if not isinstance(data, list):
            return name, None
        return name, [str(x["tokenAddress"]) for x in data
                      if isinstance(x, dict) and x.get("chainId") == "solana" and x.get("tokenAddress")]

    async def gecko_all():
        res = []
        for name, path, params in (("gecko_new_1", "new_pools", {"page": 1}), ("gecko_new_2", "new_pools", {"page": 2}),
                                   ("gecko_new_3", "new_pools", {"page": 3}),
                                   ("gecko_trend_5m", "trending_pools", {"duration": "5m"}),
                                   ("gecko_trend_1h", "trending_pools", {"duration": "1h"})):
            try:
                data = await asyncio.wait_for(fetch_json(client, f"{GECKO}/networks/solana/{path}", params=params), timeout=30)
                res.append((name, _pool_mints(data) if isinstance(data, dict) else None))
            except Exception:
                res.append((name, None))
        return res

    tasks = [dex("dex_profiles", f"{DEX}/token-profiles/latest/v1"),
             dex("dex_boosts", f"{DEX}/token-boosts/latest/v1"),
             dex("dex_boosts_top", f"{DEX}/token-boosts/top/v1")]
    results = await asyncio.gather(*tasks, gecko_all(), return_exceptions=True)
    ordered = []
    for r in results:
        if isinstance(r, Exception):
            continue
        ordered += r if isinstance(r, list) else [r]
    # الأحدث أولاً: صفحات الأزواج الجديدة ثم Trending ثم DexScreener
    ordered.sort(key=lambda t: (0 if t[0].startswith("gecko_new") else 1 if t[0].startswith("gecko_trend") else 2))
    for name, mints in ordered:
        stats[name] = len(mints) if mints is not None else -1
        for a in mints or []:
            found.setdefault(a, name)
    return list(found), stats


def scan_lane(m: dict):
    """يصنّف العملة: ('alpha'|'est', None) أو (None, سبب الرفض)."""
    if m["dex"] == "pumpfun":
        return None, "لم تهاجر"
    if m["price"] <= 0:
        return None, "سعر غير صالح"
    age = m.get("age_min")
    if age is None:
        return None, "عمر مجهول"
    if ALPHA_ON and ALPHA_MIN_AGE_MIN <= age <= ALPHA_MAX_AGE_MIN and ALPHA_MIN_MC <= m["mcap"] <= ALPHA_MAX_MC:
        if m["liq"] < max(ALPHA_MIN_LIQ, 0.06 * m["mcap"]):
            return None, "سيولة ضعيفة (ناشئة)"
        if m["vol1"] < ALPHA_MIN_VOL1H:
            return None, "فوليوم ضعيف (ناشئة)"
        return "alpha", None
    if m["mcap"] < SCAN_MIN_MC:
        return None, "ماركت كاب صغير"
    if m["mcap"] > SCAN_MAX_MC:
        return None, "ماركت كاب ضخم"
    if m["liq"] < SCAN_MIN_LIQ or m["liq"] < 0.05 * m["mcap"]:
        return None, "سيولة ضعيفة"
    if m["vol1"] < SCAN_MIN_VOL1H:
        return None, "فوليوم ضعيف"
    if age < SCAN_MIN_AGE_MIN:
        return None, "حديثة جداً"
    if age > SCAN_MAX_AGE_H * 60:
        return None, "أقدم من النافذة"
    return "est", None


def scan_regime(ms: list) -> dict:
    import statistics as _st
    n = len(ms)
    if n < 6:
        return {"state": "unknown", "n": n, "up1": None, "up5": None, "med1": None}
    up1 = sum(1 for m in ms if m["chg1"] > 0) / n
    up5 = sum(1 for m in ms if m["chg5"] > 0) / n
    med1 = _st.median(m["chg1"] for m in ms)
    state = "neutral"
    if up1 >= 0.6 and med1 >= 3:
        state = "hot"
    elif up1 <= 0.3 or med1 <= -8:
        state = "bleeding"
    return {"state": state, "n": n, "up1": up1, "up5": up5, "med1": med1}


def bounce_prescreen(m: dict) -> float:
    if m["chg5"] < 1.0 or m["buy_ratio5"] < 0.52 or m["chg1"] > 12 or m.get("chg6", 0) < -45:
        return 0.0
    return m["chg5"] + max(0.0, -m["chg1"]) * 0.2 + (m["buy_ratio5"] - 0.5) * 20


def alpha_ok(m: dict) -> bool:
    """زخم حقيقي لعملة ناشئة: صعود 5د و1س، مشترون يسيطرون، وتداول فعلي. (تراجع الفوليوم/الرسوم لا يمنعها)."""
    txn5 = (m.get("buys5") or 0) + (m.get("sells5") or 0)
    return (m["chg5"] >= ALPHA_MIN_CHG5 and ALPHA_MIN_CHG1 <= m["chg1"] <= ALPHA_MAX_CHG1
            and m["buy_ratio5"] >= ALPHA_MIN_BUY and (txn5 >= ALPHA_MIN_TXN5 or txn5 == 0))


def alpha_prescreen(m: dict) -> float:
    return m["chg5"] + m["chg1"] / 5 + (m["buy_ratio5"] - 0.5) * 30


def scan_secure(sec: "Security") -> bool:
    return bool(sec.passed and not sec.warnings and sec.top10 <= SCAN_MAX_TOP10 and (sec.dev is None or sec.dev <= 5))


def scan_header(label: str, regime: dict) -> str:
    st = SCAN_REGIME_LABEL.get(regime["state"], "")
    extra = ""
    if regime.get("up1") is not None:
        extra = f" (صاعد {regime['up1']:.0%} من {regime['n']} عملة، وسيط 1س {regime['med1']:+.1f}%)"
    return f"🤖 <b>صفقة من مسح السوق — {label}</b>\nنوع السوق الآن: {st}{extra}\n\n"


def record_auto(bd: dict, it: dict, m: dict, sec: Optional["Security"], kind: str, score, grade: str,
                entry: float, sl: float, tp1: float, tp2: float, text: str) -> None:
    store = bd.setdefault("track", {})
    rec = _new_rec(it["uid"], it["chat"], m, "auto", kind, score, grade, text, None, sec)
    rec.update(entry=entry, sl=sl, tp1=tp1, tp2=tp2, risk_pct=(entry - sl) / entry * 100 if entry > 0 else None)
    store[rec["id"]] = rec
    save_track(bd)


def scan_it(chat: int, m: dict, sec: "Security") -> dict:
    """عنصر خفيف لتنسيق التنبيه — لا يدخل قائمة المراقبة إطلاقاً (فصل تام)."""
    return {"uid": chat, "chat": chat, "mint": m["mint"], "sym": m["symbol"], "name": m["name"], "pair": m["pair"],
            "factor": (m["mcap"] / m["price"]) if m["price"] > 0 else 0.0, "sec_txt": sec_brief(sec),
            "alerted": [], "zones": []}


def scan_can_alert(sc: dict, mint: str, now: float):
    sc["alerts"] = [t for t in sc.get("alerts", []) if now - t < 86400]
    if sum(1 for t in sc["alerts"] if now - t < 3600) >= SCAN_MAX_ALERTS_HOUR:
        return False, "سقف الساعة"
    if now - sc.setdefault("cool", {}).get(mint, 0) < SCAN_COIN_COOLDOWN_H * 3600:
        return False, "تهدئة العملة"
    return True, ""


def _add_pos(sc: dict, m: dict, kind: str, entry: float, sl: float, tp1: float, tp2: float) -> None:
    sc.setdefault("pos", {})[m["mint"]] = {"sym": m["symbol"], "kind": kind, "entry": entry, "sl": sl, "tp1": tp1,
                                           "tp2": tp2, "ts": time.time(), "hit1": False}


async def scan_track_positions(app: Application, sc: dict, metrics: dict) -> None:
    """متابعة صفقات المسح (TP1/TP2/SL) بذاتها، دون المرور بقائمة المراقبة."""
    pos = sc.get("pos") or {}
    chat = sc.get("chat")
    for mint, p in list(pos.items()):
        if time.time() - p["ts"] > 12 * 3600:
            pos.pop(mint, None)
            continue
        m = metrics.get(mint)
        if not m or m["price"] <= 0 or not chat:
            continue
        price, sym = m["price"], html.escape(p["sym"])
        try:
            if price <= p["sl"]:
                pos.pop(mint, None)
                await app.bot.send_message(chat_id=chat, parse_mode=ParseMode.HTML, disable_web_page_preview=True,
                                           text=f"🛑 <b>{sym}</b> (مسح السوق): ضُرب وقف الخسارة ({fmt_price(p['sl'])}).\n<code>{mint}</code>")
            elif price >= p["tp2"]:
                pos.pop(mint, None)
                await app.bot.send_message(chat_id=chat, parse_mode=ParseMode.HTML, disable_web_page_preview=True,
                                           text=f"🎯🎯 <b>{sym}</b> (مسح السوق): تحقق TP2 ({fmt_price(p['tp2'])}). فكّر بجني الأرباح.")
            elif price >= p["tp1"] and not p["hit1"]:
                p["hit1"] = True
                await app.bot.send_message(chat_id=chat, parse_mode=ParseMode.HTML, disable_web_page_preview=True,
                                           text=f"🎯 <b>{sym}</b> (مسح السوق): تحقق TP1 ({fmt_price(p['tp1'])}). فكّر بنقل الوقف إلى الدخول.")
        except Exception:
            log.exception("scan position notice failed")


async def _vet(client: httpx.AsyncClient, mint: str, m: dict, u: dict, st: dict) -> None:
    try:
        report = await asyncio.wait_for(get_rugcheck_report(client, mint), timeout=45)
    except Exception:
        report = None
    if not report:
        u["tries"] += 1
        if u["tries"] >= 6:
            u["sec_ok"], u["sec_txt"] = False, "تعذر جلب تقرير RugCheck"
            st["vet_fail"] += 1
        return
    sec = analyze_security(report, m["pair"])
    st["vetted"] += 1
    ok = scan_secure(sec)
    u["sec_ok"], u["sec_obj"], u["sec_txt"] = ok, sec, sec_brief(sec)
    if not ok:
        st["vet_fail"] += 1


async def scan_cycle(app: Application) -> dict:
    bd = app.bot_data
    sc = bd["scan"]
    client = bd["http"]
    chat = sc["chat"]
    t0 = now = time.time()
    st = {"ts": now, "sources": {}, "discovered": 0, "metrics": 0, "alpha": 0, "est": 0, "filter": {}, "vetted": 0,
          "vet_fail": 0, "candles": 0, "setups": 0, "alerts": 0, "skipped": {}, "regime": None, "phases": {}}
    uni: dict = sc.setdefault("uni", {})

    # 1) اكتشاف (مصادر متوازية، لا يوقفه فشل أي مصدر)
    try:
        addrs, st["sources"] = await asyncio.wait_for(discover_candidates(client), timeout=70)
    except Exception:
        log.exception("discovery failed")
        addrs, st["sources"] = [], {}
    addrs = [a for a in addrs if is_valid_solana_address(a) and a not in EXCLUDED_MINTS][:SCAN_MAX_DISCOVER]
    st["discovered"] = len(addrs)
    st["phases"]["discover"] = round(time.time() - t0, 1)

    # 2) بيانات السوق: المكتشف + الكون + صفقات المسح المفتوحة
    t1 = time.time()
    all_addrs = list(dict.fromkeys(addrs + list(uni) + list((sc.get("pos") or {}))))[:260]
    sink: dict = {}
    if all_addrs:
        try:
            await asyncio.wait_for(fetch_pairs(client, all_addrs, (), sink, True), timeout=45)
        except asyncio.TimeoutError:
            log.warning("scan fetch_pairs timed out (partial=%d)", len(sink))
    metrics = {}
    for p in sink.values():
        mm = extract_metrics(p)
        if mm:
            metrics[mm["mint"]] = mm
    st["metrics"] = len(metrics)
    st["phases"]["metrics"] = round(time.time() - t1, 1)
    await scan_track_positions(app, sc, metrics)

    # 3) تصنيف المسارات + إدارة الكون
    lanes: dict = {}
    reasons: Counter = Counter()
    for mint, m in metrics.items():
        lane, why = scan_lane(m)
        if not lane:
            reasons[why] += 1
            uni.pop(mint, None)
            continue
        lanes[mint] = lane
        u = uni.setdefault(mint, {"added": now, "sec_ok": None, "sec_obj": None, "sec_txt": "", "tries": 0,
                                  "candle_ts": 0.0, "sym": m["symbol"]})
        u["miss"] = 0
        u["lane"] = lane
    for mint in list(uni):
        if mint not in metrics:
            uni[mint]["miss"] = uni[mint].get("miss", 0) + 1
        if now - uni[mint]["added"] > SCAN_UNIVERSE_H * 3600 or uni[mint].get("miss", 0) >= 3:
            uni.pop(mint, None)
    st["alpha"] = sum(1 for v in lanes.values() if v == "alpha")
    st["est"] = sum(1 for v in lanes.values() if v == "est")
    st["filter"] = dict(reasons)
    big = [metrics[mt] for mt in lanes]
    regime = scan_regime(big)
    st["regime"] = regime
    sc["regime"] = regime

    # 4) فحص أمني أصرم متوازٍ (دفعة صغيرة + مهلة)، الناشئة أولاً
    t2 = time.time()
    todo = sorted((mt for mt, u in uni.items() if u["sec_ok"] is None and mt in lanes),
                  key=lambda mt: (0 if lanes[mt] == "alpha" else 1, -metrics[mt]["vol1"]))[:SCAN_RUG_PER_CYCLE]
    if todo:
        try:
            await asyncio.wait_for(
                asyncio.gather(*(_vet(client, mt, metrics[mt], uni[mt], st) for mt in todo), return_exceptions=True),
                timeout=70)
        except asyncio.TimeoutError:
            log.warning("scan vetting timed out")
    st["phases"]["vet"] = round(time.time() - t2, 1)

    # 5) اختيار من نجلب شموعه: الناشئة بزخم حقيقي أولاً ثم ارتداد/زخم المُثبّتة
    picks = []
    for mint, u in uni.items():
        m = metrics.get(mint)
        if not m or not u["sec_ok"] or now - u["candle_ts"] < 120:
            continue
        if lanes.get(mint) == "alpha":
            if alpha_ok(m):
                picks.append((100 + alpha_prescreen(m), mint))
            continue
        ps = bounce_prescreen(m)
        if SCAN_MOMENTUM and momentum_ok(m):
            ps = max(ps, 10.0)
        if ps > 0:
            picks.append((ps, mint))
    picks.sort(reverse=True)

    # 6) تحليل واستراتيجية حسب نوع السوق
    t3 = time.time()
    for _, mint in picks[:SCAN_CANDLES_PER_CYCLE]:
        if time.time() - t0 > SCAN_CYCLE_TIMEOUT - 25:
            st["skipped"]["نفاد وقت الدورة"] = st["skipped"].get("نفاد وقت الدورة", 0) + 1
            break
        u, m = uni[mint], metrics[mint]
        ok_, why_ = scan_can_alert(sc, mint, time.time())
        if not ok_:
            st["skipped"][why_] = st["skipped"].get(why_, 0) + 1
            continue
        u["candle_ts"] = time.time()
        try:
            candles = await asyncio.wait_for(fetch_candles(client, m["pair"], 1, 1000), timeout=30)
        except Exception:
            candles = []
        st["candles"] += 1
        sent = False
        try:
            if lanes.get(mint) == "alpha":
                if regime["state"] == "bleeding" and m["chg5"] < ALPHA_MIN_CHG5 * 2:
                    st["skipped"]["سوق نازف: زخم غير كافٍ"] = st["skipped"].get("سوق نازف: زخم غير كافٍ", 0) + 1
                else:
                    st["setups"] += 1
                    sent = await scan_send_alpha(app, sc, u, m, candles, regime, chat)
            else:
                setup, _why = detect_bounce(candles, m) if candles else (None, "")
                if setup:
                    st["setups"] += 1
                    if regime["state"] == "bleeding" and not (m["chg5"] >= 0 and setup["stars"] >= 3):
                        st["skipped"]["سوق نازف: تأكيد غير كافٍ"] = st["skipped"].get("سوق نازف: تأكيد غير كافٍ", 0) + 1
                    else:
                        sent = await scan_send_bounce(app, sc, u, m, setup, candles, regime, chat)
                if not sent and SCAN_MOMENTUM and regime["state"] != "bleeding" and momentum_ok(m) and candles:
                    sent = await scan_send_momentum(app, sc, u, m, candles, regime, chat)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("scan analysis failed for %s", mint)
        if sent:
            st["alerts"] += 1
            sc.setdefault("alerts", []).append(time.time())
            sc.setdefault("cool", {})[mint] = time.time()
        await asyncio.sleep(0.3)
    st["phases"]["analyze"] = round(time.time() - t3, 1)
    st["phases"]["total"] = round(time.time() - t0, 1)
    sc["last"] = st
    return st


async def scan_send_bounce(app, sc: dict, u: dict, m: dict, setup: dict, candles: list, regime: dict, chat: int) -> bool:
    bd = app.bot_data
    it = scan_it(chat, m, u["sec_obj"])
    try:
        zones = find_supports_1m(candles, m.get("age_min"))
        annotate_setup(setup, zones, {"hot": "good", "bleeding": "bleeding"}.get(regime["state"], "neutral"))
    except Exception:
        log.exception("scan annotate failed")
    text = format_bounce_alert(it, m, setup)
    try:
        text = inject_support(text, it, m, setup)
    except Exception:
        log.exception("scan inject failed")
    await notify(app, it, scan_header("🔔 ارتداد", regime) + text)
    _add_pos(sc, m, "AUTOB", setup["entry"], setup["sl"], setup["tp1"], setup["tp2"])
    record_auto(bd, it, m, u["sec_obj"], "AUTOB", None, "★" * setup["stars"], setup["entry"], setup["sl"],
                setup["tp1"], setup["tp2"], "ارتداد تلقائي من مسح السوق")
    return True


async def scan_send_momentum(app, sc: dict, u: dict, m: dict, candles: list, regime: dict, chat: int) -> bool:
    bd = app.bot_data
    plan = compute_alt_plan("MOMENTUM", m, candles)
    score, b = compute_score(m, u["sec_obj"], plan)
    need = SCAN_MIN_SCORE + (0 if regime["state"] == "hot" else 5)
    if score < need:
        return False
    it = scan_it(chat, m, u["sec_obj"])
    c = Candidate(m, u["sec_obj"], plan, score, b)
    await notify(app, it, scan_header("🚀 زخم", regime) + format_best(c, []))
    _add_pos(sc, m, "AUTOM", plan.entry, plan.sl, plan.tp1, plan.tp2)
    record_auto(bd, it, m, u["sec_obj"], "AUTOM", score, "", plan.entry, plan.sl, plan.tp1, plan.tp2,
                "زخم تلقائي من مسح السوق")
    return True


async def scan_send_alpha(app, sc: dict, u: dict, m: dict, candles: list, regime: dict, chat: int) -> bool:
    """عملة ناشئة: زخم حقيقي + أمان صارم. الشموع القليلة لا تمنعها (الوقف من آخر قاع أو −12%)."""
    bd = app.bot_data
    plan = compute_alt_plan("MOMENTUM", m, candles)
    score, b = compute_score(m, u["sec_obj"], plan)
    if score < ALPHA_MIN_SCORE:
        return False
    it = scan_it(chat, m, u["sec_obj"])
    c = Candidate(m, u["sec_obj"], plan, score, b)
    age = m.get("age_min") or 0
    head = scan_header("🆕 عملة ناشئة", regime) + (
        f"⏱ عمر الزوج {age:.0f} دقيقة • MC {fmt_usd(m['mcap'])} • سيولة {fmt_usd(m['liq'])} • "
        f"5د {m['chg5']:+.0f}% • 1س {m['chg1']:+.0f}% • شراء {m['buy_ratio5'] * 100:.0f}%\n"
        f"🛡️ {html.escape(u.get('sec_txt') or '')}\n\n")
    await notify(app, it, head + format_best(c, []))
    _add_pos(sc, m, "AUTOM", plan.entry, plan.sl, plan.tp1, plan.tp2)
    record_auto(bd, it, m, u["sec_obj"], "AUTOM", score, "", plan.entry, plan.sl, plan.tp1, plan.tp2,
                "عملة ناشئة من مسح السوق")
    return True


def scan_status_text(sc: dict) -> str:
    lines = [f"🤖 <b>مسح السوق</b>: {'✅ يعمل' if sc.get('on') and sc.get('chat') else ('⏹️ متوقف' if not sc.get('on') else '⏳ ينتظر تسجيل محادثة (أرسل أي رسالة للبوت)')}"]
    if sc.get("chat"):
        lines.append(f"📮 يُرسل إلى: <code>{sc['chat']}</code> (انسخ هذا الرقم في SCAN_CHAT_ID ليبقى بعد أي إعادة تشغيل)")
    ok_ts, err_ts = sc.get("ok_ts"), sc.get("err_ts")
    if ok_ts:
        lines.append(f"⏱ آخر مسح ناجح: قبل {int(time.time() - ok_ts)}ث")
    if err_ts and (not ok_ts or err_ts >= ok_ts):
        lines.append(f"⚠️ آخر خطأ: {html.escape(sc.get('err', ''))}")
    al = api_line()
    if al:
        lines.append(al)
    last = sc.get("last")
    if last:
        rg = last["regime"] or {}
        lines += ["", f"🌡️ نوع السوق: {SCAN_REGIME_LABEL.get(rg.get('state', 'unknown'), '')}"
                  + (f" — صاعد {rg['up1']:.0%} (5د {rg['up5']:.0%}) من {rg['n']} عملة، وسيط 1س {rg['med1']:+.1f}%"
                     if rg.get("up1") is not None else f" ({rg.get('n', 0)} عملة فقط)")]
        src = " • ".join(f"{k.replace('gecko_', 'g_').replace('dex_', 'd_')} {'✗' if v < 0 else v}" for k, v in last["sources"].items())
        ph = last.get("phases", {})
        lines += ["", "📊 <b>آخر دورة</b>:", f"• المصادر: {src}",
                  f"• مكتشفة {last['discovered']} ← بيانات سوق {last['metrics']} ← ناشئة {last['alpha']} + مُثبّتة {last['est']}",
                  f"• فحص أمني: {last['vetted']} (رُفض/فشل {last['vet_fail']}) • شموع: {last['candles']} • "
                  f"إعدادات وُجدت: {last['setups']} • تنبيهات: {last['alerts']}",
                  f"• الزمن: اكتشاف {ph.get('discover', 0)}ث • بيانات {ph.get('metrics', 0)}ث • أمان {ph.get('vet', 0)}ث • "
                  f"تحليل {ph.get('analyze', 0)}ث • المجموع {ph.get('total', 0)}ث"]
        if last["filter"]:
            lines.append("• رُفضت بالفلتر: " + "، ".join(f"{k} {v}" for k, v in sorted(last["filter"].items(), key=lambda kv: -kv[1])[:5]))
        if last["skipped"]:
            lines.append("• تخطّيت: " + "، ".join(f"{k} {v}" for k, v in last["skipped"].items()))
    uni = sc.get("uni") or {}
    vet = sum(1 for u in uni.values() if u.get("sec_ok"))
    lines.append(f"\n🗂️ الكون: {len(uni)} عملة ({vet} مفحوصة وسليمة) • صفقات مفتوحة: {len(sc.get('pos') or {})} • "
                 f"تنبيهات آخر ساعة: {sum(1 for t in sc.get('alerts', []) if time.time() - t < 3600)}/{SCAN_MAX_ALERTS_HOUR}")
    return "\n".join(lines)


async def scan_loop(app: Application) -> None:
    """حلقة مستقلة: كل دورة بمهلة صارمة، وأي فشل لا يوقفها، وتنام دائماً بين الدورات."""
    await asyncio.sleep(30)
    while True:
        sc = app.bot_data.setdefault("scan", {"on": SCAN_DEFAULT_ON, "chat": 0})
        try:
            if sc.get("on") and sc.get("chat"):
                first = not sc.get("ok_ts")
                await asyncio.wait_for(scan_cycle(app), timeout=SCAN_CYCLE_TIMEOUT)
                sc["ok_ts"] = time.time()
                sc["fails"] = 0
                if first or time.time() - sc.get("hb_ts", 0) > SCAN_HEARTBEAT_H * 3600:
                    sc["hb_ts"] = time.time()
                    head = ("✅ <b>بدأ مسح السوق 24/24</b> — يعمل وحده ويرسل الصفقات هنا.\n\n" if first
                            else "🤖 <b>ملخص دوري</b> — المسح حيّ.\n\n")
                    try:
                        await app.bot.send_message(chat_id=sc["chat"], text=head + scan_status_text(sc),
                                                   parse_mode=ParseMode.HTML, disable_web_page_preview=True)
                    except Exception:
                        log.exception("scan heartbeat failed")
        except asyncio.CancelledError:
            raise
        except asyncio.TimeoutError:
            sc["fails"] = sc.get("fails", 0) + 1
            sc["err"], sc["err_ts"] = f"انتهت مهلة الدورة ({SCAN_CYCLE_TIMEOUT:.0f}ث)", time.time()
            log.warning("scan cycle timed out")
        except Exception as e:
            sc["fails"] = sc.get("fails", 0) + 1
            log.exception("scan cycle failed")
            sc["err"], sc["err_ts"] = f"{type(e).__name__}: {e}"[:160], time.time()
        # تراجع أسّي عند الفشل المتكرر (حد أقصى 10 دقائق)، وإلا الفاصل العادي
        await asyncio.sleep(min(600.0, SCAN_INTERVAL_SEC * (2 ** min(sc.get("fails", 0), 3))) if sc.get("fails") else SCAN_INTERVAL_SEC)


async def register_chat(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    try:
        sc = context.application.bot_data.setdefault("scan", {"on": SCAN_DEFAULT_ON, "chat": 0})
        ch = update.effective_chat
        if ch is None or ch.type == "channel" or sc.get("chat") or sc.get("off_by_user") or not sc.get("on"):
            return
        sc["chat"] = ch.id
        save_scan_state(sc)
        await context.bot.send_message(
            chat_id=ch.id,
            text=("🤖 سجّلتُ هذه المحادثة لتلقّي صفقات مسح السوق 24/24 (تبدأ خلال دقائق). "
                  "لإيقافها: /scan_off • للحالة: /scan • لفحص فوري: /scan_now"))
    except Exception:
        log.exception("register_chat failed")


async def scan_on_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    sc = context.application.bot_data.setdefault("scan", {"on": SCAN_DEFAULT_ON, "chat": 0})
    sc["on"], sc["chat"], sc["off_by_user"] = True, update.effective_chat.id, False
    save_scan_state(sc)
    await update.effective_message.reply_text(
        "🤖 تم تفعيل مسح السوق 24/24 لهذه المحادثة (يصلح للمجموعات أيضاً: أرسل الأمر داخل المجموعة).\n"
        f"🆕 ناشئة: عمر {ALPHA_MIN_AGE_MIN:.0f}–{ALPHA_MAX_AGE_MIN:.0f} دقيقة • MC {fmt_usd(ALPHA_MIN_MC)}–{fmt_usd(ALPHA_MAX_MC)} • زخم حقيقي\n"
        f"🏛️ مُثبّتة: MC {fmt_usd(SCAN_MIN_MC)}–{fmt_usd(SCAN_MAX_MC)} • عمر {SCAN_MIN_AGE_MIN:.0f} دقيقة–{SCAN_MAX_AGE_H:.0f} ساعة • ارتداد/زخم\n"
        f"🛡️ فحص أمني صارم لكليهما • أقصى {SCAN_MAX_ALERTS_HOUR} صفقات/ساعة • مسح كل {SCAN_INTERVAL_SEC / 60:.0f} دقائق.\n"
        f"رقم هذه المحادثة: {update.effective_chat.id} — ضعه في SCAN_CHAT_ID على Render ليبقى بعد أي نشر.\n"
        "/scan الحالة • /scan_now فحص فوري • /scan_off إيقاف.")


async def scan_off_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    sc = context.application.bot_data.setdefault("scan", {"on": SCAN_DEFAULT_ON, "chat": 0})
    sc["on"], sc["off_by_user"] = False, True
    save_scan_state(sc)
    await update.effective_message.reply_text("⏹️ أوقفت مسح السوق. قائمتك ومراقبتك اليدوية لم تتأثر.")


async def scan_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    sc = context.application.bot_data.setdefault("scan", {"on": SCAN_DEFAULT_ON, "chat": 0})
    await safe_send(update.effective_message, scan_status_text(sc))


async def scan_now_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    bd = context.application.bot_data
    sc = bd.setdefault("scan", {"on": SCAN_DEFAULT_ON, "chat": 0})
    if not sc.get("chat"):
        sc["chat"] = update.effective_chat.id
        save_scan_state(sc)
    status = await update.effective_message.reply_text("🔎 دورة مسح فورية... (قد تستغرق دقيقة إلى دقيقتين)")
    try:
        await asyncio.wait_for(scan_cycle(context.application), timeout=SCAN_CYCLE_TIMEOUT)
        sc["ok_ts"] = time.time()
    except Exception as e:
        log.exception("scan_now failed")
        sc["err"], sc["err_ts"] = f"{type(e).__name__}: {e}"[:160], time.time()
    try:
        await status.edit_text("✅ انتهت الدورة")
    except Exception:
        pass
    await safe_send(update.effective_message, scan_status_text(sc))


async def notify(app: Application, it: dict, text: str) -> None:
    try:
        await app.bot.send_message(chat_id=it["chat"], text=text, parse_mode=ParseMode.HTML, disable_web_page_preview=True)
    except Exception:
        try:
            await app.bot.send_message(chat_id=it["chat"], text=to_plain(text)[:4000], disable_web_page_preview=True)
        except Exception:
            log.exception("notify failed")


async def track_setup(app: Application, it: dict, m: dict) -> None:
    s, p = it.get("setup"), m["price"]
    if not s:
        return
    if time.time() - s["at"] > 4 * 3600:
        it["setup"] = None
        return
    sym = html.escape(it["sym"])
    if p <= s["sl"]:
        it["setup"] = None
        it["fails"] = it.get("fails", 0) + 1
        it["sl_ts"], it["sl_trough"] = time.time(), s.get("trough", s["sl"])
        extra = ""
        if FAIL_STREAK > 0 and it["fails"] >= FAIL_STREAK:
            it["mute_until"] = time.time() + FAIL_MUTE_MIN * 60
            it["fails"] = 0
            extra = f"\n🔇 فشلان متتاليان: كتمتُ إشارات ارتداد هذه العملة {FAIL_MUTE_MIN:.0f} دقيقة (غالباً في انهيار)."
        await notify(app, it, f"🛑 <b>{sym}</b>: ضُرب وقف الخسارة ({fmt_price(s['sl'])}). الإعداد فشل — أواصل المراقبة لقاع جديد." + next_support_note(it, s) + extra)
    elif p >= s["tp2"]:
        it["setup"] = None
        await notify(app, it, f"🎯🎯 <b>{sym}</b>: تحقق TP2 ({fmt_price(s['tp2'])}). فكّر بجني الأرباح/تأمين الباقي.")
    elif p >= s["tp1"] and not s.get("hit1"):
        s["hit1"] = True
        it["fails"] = 0
        await notify(app, it, f"🎯 <b>{sym}</b>: تحقق TP1 ({fmt_price(s['tp1'])}). فكّر بنقل الوقف إلى نقطة الدخول.")


async def evaluate_pending(app: Application, it: dict, m: dict, store: dict) -> None:
    """عملة حديثة/على Bonding Curve نضجت: فلتر أولي ثم فحص أمني. تُسحب أو تصبح آمنة."""
    client = app.bot_data["http"]
    sym = html.escape(it["sym"])
    reason = pre_filter(m)
    if reason and watch_kind_for(reason) not in ("dip", "extended"):
        it["off"] = reason_detail(m, reason)
        await notify(app, it, f"⛔ <b>{sym}</b>: أوقفت تنبيهاتها (تبقى في القائمة) — {html.escape(it['off'])}")
        return
    report = await get_rugcheck_report(client, it["mint"])
    if not report:
        it["sec_tries"] += 1
        if it["sec_tries"] >= 6:
            it["off"] = "تعذر جلب تقرير RugCheck"
            await notify(app, it, f"⛔ <b>{sym}</b>: أوقفت تنبيهاتها (تبقى في القائمة) — تعذر جلب تقرير RugCheck بعد عدة محاولات.")
        return
    sec = analyze_security(report, m["pair"])
    if not sec.passed:
        it["off"] = sec_detail(sec)
        await notify(app, it, f"⛔ <b>{sym}</b> رُفضت أمنياً (تبقى في القائمة بلا تنبيهات) — {html.escape(it['off'])}")
        return
    it["sec_ok"], it["sec_txt"], it["kind"] = True, sec_brief(sec), "nosetup"
    await notify(
        app, it,
        f"✅ <b>{sym}</b> نضجت واجتازت الفحص الأمني ({html.escape(it['sec_txt'])})\n"
        f"MC الآن {fmt_usd(m['mcap'])} • 1h {m['chg1']:+.0f}% • 5د {m['chg5']:+.0f}%\n"
        "أراقبها الآن لارتداد مؤكد.",
    )


AUTO_PURGE = os.getenv("AUTO_PURGE", "1") != "0"
PURGE_COLLAPSE_PCT = _env_float("PURGE_COLLAPSE_PCT", 75)     # انهيار من أعلى سعر منذ المراقبة
PURGE_FLASH_PCT = _env_float("PURGE_FLASH_PCT", 45)           # هبوط حاد خلال 10 دقائق
PURGE_LIQ_DROP_PCT = _env_float("PURGE_LIQ_DROP_PCT", 60)     # سحب سيولة من أعلى سيولة
PURGE_MIN_LIQ = _env_float("PURGE_MIN_LIQ", 3000)
PURGE_MISS_CYCLES = int(_env_float("PURGE_MISS_CYCLES", 6))   # اختفاء الزوج من DexScreener
PURGE_RECHECK_MIN = _env_float("PURGE_RECHECK_MIN", 20)       # إعادة الفحص الأمني الدوري
WATCH_MAX_TOTAL = int(_env_float("WATCH_MAX_TOTAL", 80))


def purge_reason(it: dict, m: dict, now: float) -> Optional[str]:
    """سبب الحذف الفوري لعملة ميتة/مشبوهة (انهيار، هبوط حاد، سحب سيولة)، أو None."""
    price, liq = m["price"], m["liq"]
    it["peak_price"] = max(it.get("peak_price", 0.0), price)
    it["peak_liq"] = max(it.get("peak_liq", 0.0), liq)
    if it["peak_price"] > 0 and price <= it["peak_price"] * (1 - PURGE_COLLAPSE_PCT / 100):
        return f"انهيار {(1 - price / it['peak_price']) * 100:.0f}% من أعلى سعر منذ المراقبة"
    recent = [p for t, p in (it.get("px") or []) if now - t <= 600]
    if recent and price <= max(recent) * (1 - PURGE_FLASH_PCT / 100):
        return f"هبوط حاد {(1 - price / max(recent)) * 100:.0f}% خلال 10 دقائق"
    if liq < PURGE_MIN_LIQ:
        return f"سيولة منهارة (${liq:,.0f})"
    if it["peak_liq"] > 0 and liq <= it["peak_liq"] * (1 - PURGE_LIQ_DROP_PCT / 100):
        return f"سحب سيولة {(1 - liq / it['peak_liq']) * 100:.0f}% (شبهة Rug)"
    return None


async def purge_notify(app: Application, purged: list) -> None:
    """رسالة واحدة مجمّعة لكل محادثة بالعملات التي أُزيلت (حتى لا نغرقك بالرسائل)."""
    by_chat: dict = {}
    for it, why in purged:
        by_chat.setdefault(it["chat"], []).append(f"• {html.escape(it['sym'])}: {html.escape(why)}")
    for chat_id, lines in by_chat.items():
        try:
            await app.bot.send_message(chat_id=chat_id, parse_mode=ParseMode.HTML, disable_web_page_preview=True,
                                       text=f"🧹 <b>أزلتُ {len(lines)} عملة ميتة/مشبوهة من المراقبة</b> (لن تصلك عنها إشارات ارتداد):\n" + "\n".join(lines[:15]))
        except Exception:
            log.exception("purge notify failed")


async def watch_cycle(app: Application) -> None:
    bd = app.bot_data
    store: dict = bd.get("watch") or {}
    if not store:
        return
    client = bd["http"]
    now = time.time()
    for key, it in list(store.items()):
        if now - it["added"] > WATCH_TTL_HOURS * 3600:
            store.pop(key, None)
            await notify(app, it, f"⌛ انتهت مراقبة <b>{html.escape(it['sym'])}</b> بعد {WATCH_TTL_HOURS:.0f} ساعات دون إشارة ارتداد.")
    purged: list = []
    if AUTO_PURGE:
        for key, it in list(store.items()):
            if it.get("off") and now - it["added"] > 3600:
                store.pop(key, None)
                purged.append((it, "موقوفة التنبيه منذ أكثر من ساعة"))
        if len(store) > WATCH_MAX_TOTAL:
            for key, it in sorted(store.items(), key=lambda kv: kv[1]["added"])[:len(store) - WATCH_MAX_TOTAL]:
                store.pop(key, None)
                purged.append((it, "تجاوز سقف القائمة"))
    mints = list({it["mint"] for it in store.values() if not it.get("off")})
    if not mints:
        save_watch(bd)
        return
    sink: dict = {}
    try:
        await asyncio.wait_for(fetch_pairs(client, mints, (), sink, True), timeout=40)
    except asyncio.TimeoutError:
        log.warning("watch fetch_pairs timed out (partial=%d)", len(sink))
    metrics = {}
    for p in sink.values():
        mm = extract_metrics(p)
        if mm:
            metrics[mm["mint"]] = mm

    sec_budget, candle_budget = WATCH_SEC_PER_CYCLE, WATCH_CANDLES_PER_CYCLE
    rc_budget = 1
    try:
        regime = compute_regime(bd)
    except Exception:
        log.exception("compute_regime failed")
        regime = {"state": "neutral", "wr": None, "breadth": None}
    bd["regime"] = regime
    for it in store.values():                 # عملة "ساخنة": ارتفعت 1.5–15% عن أدنى سعر آخر 20 دقيقة → فحص أسرع
        mm = metrics.get(it["mint"])
        if mm and mm["price"] > 0 and not it.get("off"):
            px = it.setdefault("px", [])
            px.append([now, mm["price"]])
            del px[:-30]
            lows = [p for t, p in px if now - t <= 1200]
            it["hot"] = bool(it.get("sec_ok")) and len(lows) >= 3 and 1.015 <= mm["price"] / min(lows) <= 1.15
    for it in sorted(store.values(), key=lambda x: (0 if x.get("hot") else 1, x["candle_ts"])):
        if store.get(it["key"]) is not it or it.get("off"):
            continue
        m = metrics.get(it["mint"])
        if m is None:
            it["miss"] = it.get("miss", 0) + 1
            if AUTO_PURGE and it.get("mcap0", 0) > 0 and it["miss"] >= PURGE_MISS_CYCLES:
                store.pop(it["key"], None)
                purged.append((it, "اختفى الزوج من DexScreener (سحب/حذف محتمل)"))
            continue
        it["price"], it["mcap"], it["dex"], it["pair"] = m["price"], m["mcap"], m["dex"], m["pair"]
        it["chg1"] = m["chg1"]
        it["miss"] = 0
        if AUTO_PURGE:
            why_p = purge_reason(it, m, now)
            if why_p:
                store.pop(it["key"], None)
                purged.append((it, why_p))
                continue
        if WATCH_DEAD_PCT > 0 and it["mcap0"] > 0 and m["mcap"] < it["mcap0"] * (1 - WATCH_DEAD_PCT / 100) and not it.get("setup"):
            it["off"] = f"انهارت أكثر من {WATCH_DEAD_PCT:.0f}% منذ الإضافة"
            await notify(app, it, (f"☠️ <b>{html.escape(it['sym'])}</b> {it['off']} "
                                   f"(MC {fmt_usd(it['mcap0'])} ← {fmt_usd(m['mcap'])}) — أوقفت تنبيهاتها (تبقى في القائمة)."))
            continue
        if it.get("setup"):
            await track_setup(app, it, m)
        if it["sec_ok"] is None:
            ready = m["dex"] != "pumpfun" and m["age_min"] is not None and m["age_min"] >= MIN_PAIR_AGE_MIN
            it["why"] = "ينتظر الهجرة" if m["dex"] == "pumpfun" else ("ينتظر نضج البيانات" if not ready else "")
            if not ready or sec_budget <= 0:
                continue
            sec_budget -= 1
            await evaluate_pending(app, it, m, store)
            if store.get(it["key"]) is not it or it["sec_ok"] is not True:
                continue
        if AUTO_PURGE and it.get("sec_ok") and rc_budget > 0 and now - it.get("rc_ts", it["added"]) > PURGE_RECHECK_MIN * 60:
            rc_budget -= 1
            it["rc_ts"] = now
            try:
                rep_ = await asyncio.wait_for(get_rugcheck_report(client, it["mint"]), timeout=40)
                if rep_:
                    sec_ = analyze_security(rep_, m["pair"])
                    if not sec_.passed:
                        store.pop(it["key"], None)
                        purged.append((it, "تغيّر أمني: " + sec_detail(sec_)[:120]))
                        continue
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("rugcheck recheck failed")
        if it.get("setup") or candle_budget <= 0 or m["price"] <= 0:
            continue
        candle_budget -= 1
        it["candle_ts"] = time.time()
        candles = await fetch_candles(client, m["pair"], 1, 1000)     # طلب واحد كالسابق، بتاريخ أطول
        try:
            store_hist(bd, it, candles)
            if candles:
                it["hi_ratio"] = m["price"] / max(c[2] for c in candles)
        except Exception:
            log.exception("hist store failed")
        zones: list = []
        try:
            zones = find_supports_1m(candles, m.get("age_min"))
            it["zones"] = zones
            await maybe_support_alert(app, it, m, zones)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("support analysis failed (alerts continue)")
        setup, why = detect_bounce(candles, m)
        it["why"] = why or ""
        if time.time() < it.get("mute_until", 0):
            it["why"] = f"🔇 مكتومة بعد فشلين متتاليين حتى {time.strftime('%H:%M', time.gmtime(it['mute_until']))} UTC"
            await asyncio.sleep(0.5)
            continue
        if setup and it.get("sl_ts") and time.time() - it["sl_ts"] < SAME_ZONE_CD_MIN * 60 \
                and setup["trough"] >= it.get("sl_trough", 0) * 0.97:
            it["why"] = "نفس منطقة الفشل السابقة — لا إعادة تنبيه حتى قاع أدنى"
            setup = None
        if setup and setup["key"] not in it["alerted"]:
            it["alerted"] = (it["alerted"] + [setup["key"]])[-10:]
            try:
                annotate_setup(setup, zones, regime["state"])
            except Exception:
                log.exception("annotate_setup failed")
            it["setup"] = setup
            text = format_bounce_alert(it, m, setup)
            try:
                text = inject_support(text, it, m, setup)
            except Exception:
                log.exception("inject_support failed")
            await notify(app, it, text)
            record_bounce(bd, it, m, setup)
        await asyncio.sleep(0.5)
    if purged:
        await purge_notify(app, purged)
    save_watch(bd)


async def watch_loop(app: Application) -> None:
    bd = app.bot_data
    await asyncio.sleep(20)
    while True:
        try:
            await watch_cycle(app)
            bd["watch_ok_ts"] = time.time()
        except asyncio.CancelledError:
            raise
        except Exception as e:
            log.exception("watch cycle failed")
            bd["watch_err"] = f"{type(e).__name__}: {e}"[:160]
            bd["watch_err_ts"] = time.time()
        try:
            await tracker_cycle(app)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("tracker cycle failed")
        await asyncio.sleep(WATCH_INTERVAL_SEC)


async def watchlist_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    uid = update.effective_user.id
    items = sorted((it for it in context.application.bot_data.get("watch", {}).values() if it["uid"] == uid),
                   key=lambda x: x["added"])
    if not items:
        await update.effective_message.reply_text("قائمة المراقبة فارغة. شغّل /best بعد إرسال عقود، وكل عملة آمنة (أو قابلة للمراقبة) تُضاف تلقائياً.")
        return
    bd_ = context.application.bot_data
    try:
        reg = compute_regime(bd_)
    except Exception:
        reg = {"state": "neutral"}
    lines = [f"👁 <b>قائمة المراقبة ({len(items)})</b> • السوق: {REGIME_LABEL[reg['state']]}"]
    ok_ts, err_ts = bd_.get("watch_ok_ts"), bd_.get("watch_err_ts")
    lines.append(f"⏱ آخر فحص ناجح: {'قبل ' + str(int(time.time() - ok_ts)) + 'ث' if ok_ts else 'لم يكتمل بعد'}"
                 + (f" • ⚠️ آخر خطأ: {html.escape(bd_.get('watch_err', ''))}" if err_ts and (not ok_ts or err_ts >= ok_ts) else ""))
    al = api_line()
    if al:
        lines.append(al)
    lines.append("")
    for it in items[:20]:
        if it.get("off"):
            st = "⛔ موقوفة التنبيه"
        elif time.time() < it.get("mute_until", 0):
            st = "🔇 مكتومة (فشلان متتاليان)"
        elif it.get("setup"):
            st = "🟢 إشارة ارتداد نشطة"
        elif it["sec_ok"] is None:
            st = WATCH_KIND_TXT.get(it["kind"], "⏳ قيد الفحص")
        else:
            st = "👁 آمنة — بانتظار ارتداد"
        chg = (it["mcap"] / it["mcap0"] - 1) * 100 if it["mcap0"] > 0 else 0.0
        lines.append(f"• <b>{html.escape(it['sym'])}</b> {fmt_usd(it['mcap0'])} ← {fmt_usd(it['mcap'])} ({chg:+.0f}%) — {st}")
        if it.get("off"):
            lines.append(f"   <i>{html.escape(it['off'])}</i>")
        elif it.get("why") and not it.get("setup"):
            lines.append(f"   <i>{html.escape(it['why'])}</i>")
        zs = [z for z in (it.get("zones") or []) if z["strength"] >= SUPPORT_MIN_STRENGTH and it["price"] > 0
              and z["high"] <= it["price"] * 1.02]
        if zs and it.get("factor"):
            z = max(zs, key=lambda x: x["high"])
            lines.append(f"   📍 أقرب دعم قوي: MC {fmt_usd(z['low'] * it['factor'])}–{fmt_usd(z['high'] * it['factor'])} "
                         f"(قوة {z['strength']}، يبعد {max(0.0, (1 - z['high'] / it['price']) * 100):.0f}% تحتنا)")
    if len(items) > 20:
        lines.append(f"… و{len(items) - 20} أخرى")
    await safe_send(update.effective_message, "\n".join(lines))


async def unwatch_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    bd = context.application.bot_data
    uid = update.effective_user.id
    store = bd.get("watch", {})
    keys = [k for k, it in store.items() if it["uid"] == uid]
    for k in keys:
        store.pop(k, None)
    save_watch(bd)
    await update.effective_message.reply_text(f"🧹 أُزيلت {len(keys)} عملة من المراقبة.")


# ───────────────────────────── تتبع نتائج الصفقات + تقرير /report ─────────────────────────────
TRACK_FILE = os.getenv("TRACK_FILE", "tracker.json")
TRACK_WINDOW_HOURS = _env_float("TRACK_WINDOW_HOURS", 24)     # مدة تتبع كل سجل بعد الإشارة
TRACK_KEEP_HOURS = 48                                          # أطول مدة نحتفظ فيها بالسجلات
REPORT_MAX_SETTLE = int(_env_float("REPORT_MAX_SETTLE", 40))   # أقصى عدد سجلات تُحسم بالشموع في التقرير

KEY_LABEL = {
    "chase": "مطاردة بعد صعود قوي", "pace": "فوليوم يتباطأ", "sell": "ضغط بيع", "liq": "سيولة ضعيفة",
    "young": "عملة حديثة جداً", "top10": "تركّز كبار الملاك", "fast": "سقوط سريع بعد الدخول",
    "tight": "وقف ضيق", "flat": "لم تتحرك للأعلى أبداً", "almost": "قريبة من الهدف ثم انعكست",
    "amb": "وقف وهدف في الدقيقة نفسها", "unknown": "بلا مؤشر واضح",
}
KIND_LABEL = {"SCALP": "⚡سكالبيج", "MOMENTUM": "🚀زخم", "HOLD": "🧭احتفاظ", "BOUNCE": "🔔ارتداد", "AUTOB": "🤖ارتداد", "AUTOM": "🤖زخم", "-": "—"}
DECISION_LABEL = {"auto": "تلقائي", "main": "رئيسية", "alt": "بديل", "other": "مرتّبة", "bounce": "ارتداد", "watch": "مراقبة", "rej": "مرفوضة"}


def save_track(bd: dict) -> None:
    try:
        tmp = TRACK_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(list(bd.get("track", {}).values()), f, ensure_ascii=False)
        os.replace(tmp, TRACK_FILE)
    except Exception as e:
        log.warning("save_track failed: %s", e)


def load_track() -> dict:
    try:
        with open(TRACK_FILE, encoding="utf-8") as f:
            return {r["id"]: r for r in json.load(f)}
    except FileNotFoundError:
        return {}
    except Exception as e:
        log.warning("load_track failed: %s", e)
        return {}


def _feat(m: dict, sec: Optional["Security"] = None) -> dict:
    f = {
        "mcap": m.get("mcap"), "liq": m.get("liq"), "age_min": m.get("age_min"), "chg5": m.get("chg5"),
        "chg1": m.get("chg1"), "vol_pace": m.get("vol_pace"), "buy5": m.get("buy_ratio5"),
        "vol1": m.get("vol1"), "dex": m.get("dex"),
    }
    try:
        f["flags"] = market_flags(m)
    except Exception:
        f["flags"] = []
    if sec is not None:
        f.update({"top10": sec.top10, "dev": sec.dev, "lp": sec.lp_locked, "rug": sec.rug_norm})
    return f


def _new_rec(uid: int, chat_id: int, m: dict, decision: str, kind: str = "-", score=None, grade: str = "",
             text: str = "", plan: Optional["ScalpPlan"] = None, sec: Optional["Security"] = None) -> dict:
    ts, price = time.time(), m["price"]
    rec = {
        "id": f"{uid}:{m['mint']}:{decision}:{int(ts)}", "uid": uid, "chat": chat_id, "mint": m["mint"],
        "pair": m.get("pair"), "sym": m.get("symbol") or m["mint"][:5], "ts": ts, "decision": decision,
        "kind": kind, "score": score, "grade": grade, "text": text, "price0": price,
        "entry": None, "sl": None, "tp1": None, "tp2": None, "risk_pct": None, "zone_high": None,
        "f": _feat(m, sec), "last": price, "last_ts": ts, "hi": price, "lo": price, "t_hi": None, "t_lo": None,
        "first": None, "t_first": None, "hit2": False, "entered0": True, "entered": True, "t_enter": None,
        "closed": False, "hi_f": None, "lo_f": None,
    }
    if plan is not None:
        rec.update(entry=plan.entry, sl=plan.sl, tp1=plan.tp1, tp2=plan.tp2, risk_pct=plan.risk_pct,
                   zone_high=plan.zone_high)
        rec["entered0"] = rec["entered"] = not (plan.kind == "SCALP" and plan.status == "ABOVE_ZONE")
    return rec


def _dup(store: dict, uid: int, mint: str, decision: str, window_sec: float) -> bool:
    now = time.time()
    return any(r["uid"] == uid and r["mint"] == mint and r["decision"] == decision and now - r["ts"] < window_sec
               for r in store.values())


def record_scan(bd: dict, uid: int, chat_id: int, res: "ScanResult") -> int:
    """يسجّل كل ما حلّله /best: الرئيسية والبدائل والمرفوضة والمراقَبة، لقياس نتائجها لاحقاً."""
    store = bd.setdefault("track", {})
    seen: dict = {}
    for c in res.ranked:
        seen.setdefault(c.m["mint"], c)
    n = 0
    for i, c in enumerate(seen.values()):
        decision = "main" if i == 0 else ("alt" if i <= 2 else "other")
        if _dup(store, uid, c.m["mint"], decision, 3 * 3600):
            continue
        rec = _new_rec(uid, chat_id, c.m, decision, c.plan.kind, c.score, grade_of(c),
                       "، ".join(market_flags(c.m)), c.plan, c.sec)
        store[rec["id"]] = rec
        n += 1
    for mint, v in res.verdicts.items():
        m = v.get("m")
        if mint in seen or not m or (m.get("price") or 0) <= 0:
            continue
        decision = "watch" if v["icon"] == "👁" else "rej"
        if _dup(store, uid, mint, decision, 3 * 3600):
            continue
        rec = _new_rec(uid, chat_id, {**m, "mint": mint}, decision, "-", None, "", f"{v['icon']} {v['text']}",
                       None, res.secs.get(mint))
        store[rec["id"]] = rec
        n += 1
    if n:
        save_track(bd)
    return n


def record_bounce(bd: dict, it: dict, m: dict, s: dict) -> None:
    store = bd.setdefault("track", {})
    if _dup(store, it["uid"], it["mint"], "bounce", 1800):
        return
    rec = _new_rec(it["uid"], it["chat"], m, "bounce", "BOUNCE", None, "★" * s["stars"],
                   f"ارتداد +{s['bounce']:.0f}% من القاع بعد تصحيح {s['retr']:.0%}")
    rec.update(entry=s["entry"], sl=s["sl"], tp1=s["tp1"], tp2=s["tp2"], risk_pct=s["risk_pct"])
    rec["f"]["sec_txt"] = it.get("sec_txt", "")
    sup = s.get("support")
    rec["f"]["regime"] = s.get("regime")
    rec["f"]["support"] = ({k: sup[k] for k in ("level", "low", "high", "touches", "strength", "vol_x", "gl", "base")}
                           if sup else None)
    store[rec["id"]] = rec
    save_track(bd)


def track_update(r: dict, price: float, now: float) -> None:
    r["last"], r["last_ts"] = price, now
    if price > r["hi"]:
        r["hi"], r["t_hi"] = price, now
    if price < r["lo"]:
        r["lo"], r["t_lo"] = price, now
    if r["sl"] is None:
        return
    if not r["entered"]:
        if r["zone_high"] and price <= r["zone_high"]:
            r["entered"], r["t_enter"] = True, now
            r["hi"] = r["lo"] = price
        return
    if r["first"] is None:
        if price <= r["sl"]:
            r["first"], r["t_first"] = "SL", now
        elif price >= r["tp1"]:
            r["first"], r["t_first"] = "TP1", now
        if r["first"]:
            r["hi_f"], r["lo_f"] = r["hi"], r["lo"]            # أقصى/أدنى سعر حتى لحظة الحسم فقط
    if r["first"] == "TP1" and price >= r["tp2"]:
        r["hit2"] = True


async def tracker_cycle(app: Application) -> None:
    bd = app.bot_data
    store: dict = bd.get("track") or {}
    if not store:
        return
    now = time.time()
    for k, r in list(store.items()):
        if now - r["ts"] > TRACK_KEEP_HOURS * 3600:
            store.pop(k, None)
        elif not r["closed"] and now - r["ts"] > TRACK_WINDOW_HOURS * 3600:
            r["closed"] = True
    active = [r for r in store.values() if not r["closed"]]
    if not active:
        return
    sink: dict = {}
    try:
        await asyncio.wait_for(fetch_pairs(bd["http"], list({r["mint"] for r in active}), (), sink, True), timeout=40)
    except asyncio.TimeoutError:
        pass
    metrics = {}
    for p in sink.values():
        mm = extract_metrics(p)
        if mm:
            metrics[mm["mint"]] = mm
    for r in active:
        mm = metrics.get(r["mint"])
        if mm and mm["price"] > 0:
            r["pair"] = mm["pair"]
            track_update(r, mm["price"], now)
    save_track(bd)


REPORT_ENTRY_DELAY_SEC = _env_float("REPORT_ENTRY_DELAY_SEC", 60)   # زمن واقعي بين وصول التنبيه وتنفيذك
PATH_MINUTES = 240


def _settle_poll(r: dict) -> dict:
    p0 = r["price0"] or 1e-18
    if r["sl"] is None:
        return {"src": "poll", "max": r["hi"] / p0 - 1, "min": r["lo"] / p0 - 1, "end": r["last"] / p0 - 1}
    e = r["entry"] or p0
    if not r["entered"]:
        return {"src": "poll", "entered": False, "max": r["hi"] / p0 - 1, "end": r["last"] / p0 - 1}
    hi = r.get("hi_f") or r["hi"]
    lo = r.get("lo_f") or r["lo"]
    return {"src": "poll", "entered": True, "first": r["first"], "hit2": r["hit2"], "amb": False,
            "mfe": hi / e - 1, "mae": lo / e - 1, "max_after": r["hi"] / e - 1,
            "mins": ((r["t_first"] - r["ts"]) / 60) if r["t_first"] else None, "end": r["last"] / e - 1}


def _r5(v: float) -> float:
    return float(f"{v:.5g}")


def _walk(r: dict, cs: list, entry: float, sl: float, tp1: float, tp2: float) -> dict:
    first, t_first, amb, hit2 = None, None, False, False
    hi_b = lo_b = hi_a = entry
    for c in cs:
        h, lo = c[2], c[3]
        hi_a = max(hi_a, h)
        if first is None:
            hi_b, lo_b = max(hi_b, h), min(lo_b, lo)
            sl_hit, tp_hit = lo <= sl, h >= tp1
            if sl_hit:
                first, amb = "SL", tp_hit
            elif tp_hit:
                first = "TP1"
            if first:
                t_first = c[0]
        if first == "TP1" and h >= tp2:
            hit2 = True
    return {"src": "candles", "entered": True, "first": first, "hit2": hit2, "amb": amb,
            "mfe": hi_b / entry - 1, "mae": lo_b / entry - 1, "max_after": hi_a / entry - 1,
            "mins": ((t_first - r["ts"]) / 60) if t_first else None, "end": cs[-1][4] / entry - 1,
            "entry_eff": entry}


def _settle_candles(r: dict, candles: list, delay: Optional[float] = None) -> Optional[dict]:
    """يحسم من شموع 1د. الدخول بعد REPORT_ENTRY_DELAY_SEC من التنبيه (واقعي)، والوقف/الهدف بنفس نسبهما.
    ضرب الوقف والهدف في الدقيقة نفسها يُحسب خسارة (تحفظاً)."""
    delay = REPORT_ENTRY_DELAY_SEC if delay is None else delay
    after = [c for c in candles if c[0] >= r["ts"]]
    cs = [c for c in after if c[0] >= r["ts"] + delay]
    if len(cs) < 2:
        return None
    p0 = r["price0"] or 1e-18
    hi_all = max(c[2] for c in after)
    if r["sl"] is None:
        return {"src": "candles", "max": hi_all / p0 - 1, "min": min(c[3] for c in after) / p0 - 1,
                "end": after[-1][4] / p0 - 1}
    plan_e = r["entry"] or p0
    sl_pct, tp1_pct, tp2_pct = 1 - r["sl"] / plan_e, r["tp1"] / plan_e - 1, r["tp2"] / plan_e - 1
    if r["entered0"]:
        entry = cs[0][1]
        res = _walk(r, cs, entry, entry * (1 - sl_pct), entry * (1 + tp1_pct), entry * (1 + tp2_pct))
    else:
        start = next((i for i, c in enumerate(cs) if r["zone_high"] and c[3] <= r["zone_high"]), None)
        if start is None:
            return {"src": "candles", "entered": False, "max": hi_all / p0 - 1, "end": after[-1][4] / p0 - 1}
        res = _walk(r, cs[start:], plan_e, r["sl"], r["tp1"], r["tp2"])
    res["path"] = [[int(c[0] - r["ts"]), _r5(c[1]), _r5(c[2]), _r5(c[3]), _r5(c[4])]
                   for c in after if c[0] - r["ts"] <= PATH_MINUTES * 60]
    return res


async def settle_all(client: httpx.AsyncClient, recs: list, status=None) -> list:
    """يجلب شموع كل عملة (pool) مرة واحدة بفاصل يحترم حدّ GeckoTerminal (30/د)، ويحسم صفقات الخطة بها."""
    now = time.time()
    pools = list({r["pair"] for r in recs if r["sl"] is not None and r.get("pair") and now - r["ts"] < 16 * 3600})
    cache: dict = {}
    for i, pool in enumerate(pools, 1):
        try:
            cache[pool] = await fetch_candles(client, pool, 1, 1000)
        except Exception:
            log.exception("settle candles failed")
            cache[pool] = []
        if status is not None:
            try:
                await status.edit_text(f"📊 جارٍ جلب شموع الدقيقة ({i}/{len(pools)})...")
            except Exception:
                pass
        await asyncio.sleep(2.3)
    out = []
    for r in recs:
        res = None
        cs = cache.get(r.get("pair"))
        if r["sl"] is not None and cs:
            res = _settle_candles(r, cs)
        out.append((r, res or _settle_poll(r)))
    return out


def _simulate(r: dict, path: list, delay: float, sl_mult: float, tp_mult: float) -> Optional[float]:
    """يعيد R لصفقة بافتراضات مختلفة (تأخير دخول/اتساع الوقف/قرب الهدف) من المسار المحفوظ."""
    cs = [c for c in path if c[0] >= delay]
    if len(cs) < 2 or not r.get("entry"):
        return None
    entry = cs[0][1]
    if entry <= 0:
        return None
    tp_pct = r["tp1"] / r["entry"] - 1
    sl_pct = (1 - r["sl"] / r["entry"]) * sl_mult
    sl, tp = entry * (1 - sl_pct), entry * (1 + tp_pct * tp_mult)
    risk = entry - sl
    for c in cs:
        if c[3] <= sl:
            return -1.0
        if c[2] >= tp:
            return (tp - entry) / risk
    return (cs[-1][4] - entry) / risk


def whatif_text(settled: list) -> Optional[str]:
    items = [(r, x["path"]) for r, x in settled if r["sl"] is not None and x.get("path")]
    if len(items) < 5:
        return None
    coins = len({r["mint"] for r, _ in items})
    lines = [f"🧪 <b>ماذا لو؟ (محاكاة على {len(items)} صفقة من {coins} عملة)</b>", "",
             "متوسط R لكل صفقة (دخول بعد دقيقة) — الصفوف: اتساع الوقف، الأعمدة: بُعد الهدف:", "",
             "<code>وقف\\هدف   ×0.5    ×1    ×1.5</code>"]
    for slm in (1.0, 1.5, 2.0):
        cells = []
        for tpm in (0.5, 1.0, 1.5):
            vals = [v for v in (_simulate(r, p, 60, slm, tpm) for r, p in items) if v is not None]
            if vals:
                w = sum(1 for v in vals if v > 0) / len(vals)
                cells.append(f"{sum(vals) / len(vals):+.2f}({w:.0%})")
            else:
                cells.append("—")
        lines.append(f"<code>×{slm:<4} " + " ".join(f"{c:>9}" for c in cells) + "</code>")
    lines += ["", "أثر تأخير الدخول (وقف ×1، هدف ×1):"]
    for dl in (0, 60, 180, 300):
        vals = [v for v in (_simulate(r, p, dl, 1.0, 1.0) for r, p in items) if v is not None]
        if vals:
            lines.append(f"• بعد {dl // 60} د: {sum(vals) / len(vals):+.2f}R ({sum(1 for v in vals if v > 0) / len(vals):.0%} رابحة)")
    lines.append("\n⚠️ العملات المتكررة تُضخّم العينة؛ اعتبرها مؤشراً لا دليلاً.")
    return "\n".join(lines)


def _fail_reasons(r: dict, x: dict) -> list:
    f = r.get("f") or {}
    out = []
    if (f.get("chg1") or 0) >= 60:
        out.append(("chase", f"مطاردة: دخلت بعد صعود 1h {f['chg1']:+.0f}%"))
    if f.get("vol_pace") is not None and f["vol_pace"] < 0.8:
        out.append(("pace", f"فوليوم يتباطأ ×{f['vol_pace']:.1f}"))
    if f.get("buy5") is not None and f["buy5"] < 0.5:
        out.append(("sell", f"ضغط بيع (شراء {f['buy5'] * 100:.0f}%)"))
    if (f.get("liq") or 1e12) < 15000:
        out.append(("liq", f"سيولة ضعيفة {fmt_usd(f['liq'])}"))
    if f.get("age_min") is not None and f["age_min"] < 45:
        out.append(("young", f"حديثة جداً ({f['age_min']:.0f}د)"))
    if (f.get("top10") or 0) >= 35:
        out.append(("top10", f"تركّز Top10 {f['top10']:.0f}%"))
    mins = x.get("mins")
    if mins is not None and mins <= 8:
        out.append(("fast", f"سقطت بسرعة ({mins:.0f}د)"))
    if r.get("risk_pct") and r["risk_pct"] < 8 and mins is not None and mins <= 15:
        out.append(("tight", f"وقف ضيق (−{r['risk_pct']:.0f}%) مقابل تذبذب العملة"))
    mfe = x.get("mfe") or 0
    tp_dist = (r["tp1"] / r["entry"] - 1) if r.get("tp1") and r.get("entry") else 0
    if mfe < 0.03:
        out.append(("flat", "لم تتحرك للأعلى قبل الوقف"))
    elif tp_dist > 0 and mfe / tp_dist >= 0.6:
        out.append(("almost", f"وصلت {mfe / tp_dist:.0%} من الهدف الأول ثم انعكست"))
    if x.get("amb"):
        out.append(("amb", "ضُرب الوقف والهدف في الدقيقة نفسها (حُسبت خسارة)"))
    return out or [("unknown", "لا مؤشر واضح — راجع الرسم يدوياً")]


def _outcome(r: dict, x: dict) -> str:
    if x.get("entered") is False:
        return "noentry"
    if x.get("first") == "TP1":
        return "win"
    if x.get("first") == "SL":
        return "loss"
    return "open"


def _reason_key(text: str) -> str:
    return " ".join(re.sub(r"[\d.$%+\-—()\[\]•/:،]+|[KM]\b", " ", text).split()[:4])


def _mean(vals: list) -> Optional[float]:
    vals = [v for v in vals if v is not None]
    return sum(vals) / len(vals) if vals else None


def build_report(settled: list, hours: float):
    """يرجع (قائمة رسائل HTML، بيانات JSON bytes)."""
    trades = [(r, x) for r, x in settled if r["sl"] is not None and r["decision"] != "auto"]
    auto_t = [(r, x, _outcome(r, x)) for r, x in settled if r["sl"] is not None and r["decision"] == "auto"]
    others = [(r, x) for r, x in settled if r["sl"] is None]
    cls = [(r, x, _outcome(r, x)) for r, x in trades]
    wins = [t for t in cls if t[2] == "win"]
    losses = [t for t in cls if t[2] == "loss"]
    opens = [t for t in cls if t[2] == "open"]
    noent = [t for t in cls if t[2] == "noentry"]
    resolved = len(wins) + len(losses)

    def row(items: list) -> str:
        w = sum(1 for t in items if t[2] == "win")
        l = sum(1 for t in items if t[2] == "loss")
        o = len(items) - w - l
        return f"✅{w} ❌{l} ⏳{o}" + (f" ({w / (w + l):.0%})" if w + l else "")

    msgs = []
    head = [f"📊 <b>تقرير آخر {hours:.0f} ساعة</b>", ""]
    if not trades:
        head.append("لا توجد صفقات بخطة دخول/هدف/وقف في هذه الفترة.")
    else:
        head += [
            f"الصفقات التي أعطاها البوت: <b>{len(trades)}</b>",
            f"✅ نجحت (TP1 قبل الوقف): <b>{len(wins)}</b> • ❌ فشلت (وقف): <b>{len(losses)}</b> • "
            f"⏳ مفتوحة: {len(opens)} • لم تدخل المنطقة: {len(noent)}",
        ]
        if resolved:
            head.append(f"🎯 <b>نسبة النجاح من المحسومة: {len(wins) / resolved:.0%}</b> ({len(wins)}/{resolved})")
        t2 = sum(1 for t in wins if t[1].get("hit2"))
        if wins:
            head.append(f"TP2 تحقق أيضاً في {t2} من {len(wins)} ناجحة")
        mfe = _mean([t[1].get("mfe") for t in cls if t[2] != "noentry"])
        mae = _mean([t[1].get("mae") for t in cls if t[2] != "noentry"])
        if mfe is not None and mae is not None:
            head.append(f"متوسط أقصى ربح قبل الحسم {mfe * 100:+.0f}% • متوسط أقصى هبوط {mae * 100:+.0f}%")
        mains = [t for t in cls if t[0]["decision"] == "main"]
        if mains:
            head.append(f"الرئيسية فقط: {row(mains)}")
        coins = Counter(t[0]["sym"] for t in cls)
        head.append(f"⚠️ العينة الحقيقية: <b>{len(coins)}</b> عملة فقط ({len(cls)} تنبيهاً) — "
                    + "، ".join(f"{html.escape(s)}×{n}" for s, n in coins.most_common(4) if n > 1))
        rs = []
        for r, x, c in cls:
            if c in ("win", "loss"):
                sl_d = 1 - r["sl"] / r["entry"]
                rs.append(((r["tp1"] / r["entry"] - 1) / sl_d) if c == "win" else -1.0)
        if rs:
            head.append(f"💰 متوسط R للمحسومة: <b>{sum(rs) / len(rs):+.2f}R</b> (الفوز = TP1، الخسارة = −1R)")
        quick = [t for t in wins if (t[1].get("mins") or 99) < 2]
        if quick:
            head.append(f"⚡ {len(quick)} من الناجحة تحققت خلال أقل من دقيقتين — غالباً غير قابلة للتنفيذ عملياً")
        head += ["", "<b>حسب النوع:</b>"]
        for k in ("SCALP", "MOMENTUM", "HOLD", "BOUNCE"):
            sub = [t for t in cls if t[0]["kind"] == k]
            if sub:
                head.append(f"• {KIND_LABEL[k]}: {row(sub)}")
        scored = [t for t in cls if t[0].get("score") is not None]
        if scored:
            head += ["", "<b>حسب التقييم:</b>"]
            for lo_, hi_, lab in ((75, 101, "≥75"), (65, 75, "65–74"), (0, 65, "<65")):
                sub = [t for t in scored if lo_ <= t[0]["score"] < hi_]
                if sub:
                    head.append(f"• {lab}: {row(sub)}")
        flagged = [t for t in cls if (t[0].get("f") or {}).get("flags")]
        clean = [t for t in cls if not (t[0].get("f") or {}).get("flags")]
        head += ["", "<b>حسب التحذيرات:</b>", f"• بلا تحذيرات: {row(clean)}", f"• مع تحذيرات: {row(flagged)}"]
    msgs.append("\n".join(head))

    if trades:
        lines = ["📋 <b>تفاصيل الصفقات</b>", ""]
        for r, x, c in sorted(cls, key=lambda t: t[0]["ts"])[:25]:
            lab = f"{KIND_LABEL.get(r['kind'], r['kind'])}{' ' + str(r['score']) if r.get('score') is not None else ''}"
            mfe_t = f"أقصى {x['mfe'] * 100:+.0f}% / هبوط {x['mae'] * 100:+.0f}%" if x.get("mfe") is not None else ""
            if c == "win":
                line = f"✅ <b>{html.escape(r['sym'])}</b> {lab} — TP1 بعد {x.get('mins') or 0:.0f}د" + (" • TP2 ✓" if x.get("hit2") else "") + f" | {mfe_t}"
            elif c == "loss":
                line = f"❌ <b>{html.escape(r['sym'])}</b> {lab} — وقف بعد {x.get('mins') or 0:.0f}د | {mfe_t}"
                line += "\n   ↳ " + " • ".join(t for _, t in _fail_reasons(r, x)[:3])
            elif c == "open":
                line = f"⏳ <b>{html.escape(r['sym'])}</b> {lab} — لم تُحسم (الآن {x.get('end', 0) * 100:+.0f}%) | {mfe_t}"
            else:
                line = f"💤 <b>{html.escape(r['sym'])}</b> {lab} — لم تنزل لمنطقة الدخول (أقصى {x.get('max', 0) * 100:+.0f}%)"
            lines.append(line)
        if len(cls) > 25:
            lines.append(f"… و{len(cls) - 25} أخرى (التفاصيل الكاملة في ملف JSON)")
        msgs.append("\n".join(lines))

    if losses:
        cnt: Counter = Counter()
        for r, x, _ in losses:
            for k, _t in _fail_reasons(r, x):
                cnt[k] += 1
        diag = ["🔍 <b>تشخيص الفاشلة</b>", "", "أكثر الأسباب المحتملة تكراراً:"]
        for k, n in cnt.most_common(6):
            diag.append(f"• {KEY_LABEL.get(k, k)}: {n} من {len(losses)}")
        if wins:
            diag += ["", "<b>الناجحة مقابل الفاشلة (متوسطات عند الإشارة):</b>"]
            for name, fn, fmt in (
                ("التقييم", lambda r: r.get("score"), "{:.0f}"),
                ("وتيرة الفوليوم", lambda r: r["f"].get("vol_pace"), "×{:.1f}"),
                ("ضغط الشراء 5د", lambda r: (r["f"].get("buy5") or 0) * 100 if r["f"].get("buy5") is not None else None, "{:.0f}%"),
                ("صعود 1h", lambda r: r["f"].get("chg1"), "{:+.0f}%"),
                ("عمر الزوج (د)", lambda r: r["f"].get("age_min"), "{:.0f}"),
                ("Top10", lambda r: r["f"].get("top10"), "{:.0f}%"),
            ):
                a, b = _mean([fn(t[0]) for t in wins]), _mean([fn(t[0]) for t in losses])
                if a is not None and b is not None:
                    diag.append(f"• {name}: ناجحة {fmt.format(a)} ← فاشلة {fmt.format(b)}")
        msgs.append("\n".join(diag))

    if others:
        big = sorted(((r, x) for r, x in others), key=lambda t: -(t[1].get("max") or 0))
        missed = [t for t in big if (t[1].get("max") or 0) >= 1.0]
        mid = [t for t in big if 0.3 <= (t[1].get("max") or 0) < 1.0]
        fine = [t for t in big if (t[1].get("max") or 0) < 0.3]
        rl = [f"🕵️ <b>العملات التي لم يوصِ بها ({len(others)})</b>", "",
              f"• ركضت ≥+100% بعد تقييمها: <b>{len(missed)}</b> • +30–100%: {len(mid)} • أقل من +30% (قرار سليم): {len(fine)}"]
        if missed:
            rl += ["", "<b>الفرص التي فاتت (وسبب الرفض):</b>"]
            for r, x in missed[:8]:
                rl.append(f"• <b>{html.escape(r['sym'])}</b> {x['max'] * 100:+.0f}% (MC عند التقييم {fmt_usd(r['f'].get('mcap') or 0)}) — {html.escape(r['text'][:70])}")
            keys = Counter(_reason_key(r["text"]) for r, _ in missed)
            rl += ["", "<b>أكثر قاعدة فوّتت فرصاً:</b>"] + [f"• {k}: {n}" for k, n in keys.most_common(4)]
        msgs.append("\n".join(rl))

    given_b = [t for t in cls if t[0]["decision"] == "bounce"]
    if given_b:
        def _s(t):
            return ((t[0].get("f") or {}).get("support") or {}).get("strength", 0)

        def rstat(items: list) -> str:
            if not items:
                return "—"
            w = sum(1 for t in items if t[2] == "win")
            l = sum(1 for t in items if t[2] == "loss")
            rr = []
            for r, x, c in items:
                if c in ("win", "loss"):
                    sl_d = 1 - r["sl"] / r["entry"]
                    rr.append(((r["tp1"] / r["entry"] - 1) / sl_d) if c == "win" else -1.0)
            return f"✅{w} ❌{l} ⏳{len(items) - w - l}" + (f" • {sum(rr) / len(rr):+.2f}R" if rr else "")

        sec = ["📍 <b>جودة الدعم وحالة السوق (للارتدادات المُرسَلة)</b>", "",
               f"• من دعم قوي (قوة ≥{SUPPORT_MIN_STRENGTH:.0f}): {rstat([t for t in given_b if _s(t) >= SUPPORT_MIN_STRENGTH])}",
               f"• بلا دعم قوي: {rstat([t for t in given_b if _s(t) < SUPPORT_MIN_STRENGTH])}"]
        for st_, lab in (("good", "🟢 سوق جيد"), ("neutral", "🟡 محايد"), ("bleeding", "🔴 نازف")):
            sub = [t for t in given_b if (t[0].get("f") or {}).get("regime") == st_]
            if sub:
                sec.append(f"• {lab}: {rstat(sub)}")
        sec.append("\nإذا تفوّق الارتداد من دعم قوي بوضوح على غيره عبر عدة أيام، نجعله شرطاً. وإلا يبقى معلومة فقط.")
        msgs.append("\n".join(sec))
    if auto_t:
        aw = sum(1 for t in auto_t if t[2] == "win")
        al_ = sum(1 for t in auto_t if t[2] == "loss")
        rr_ = []
        for r, x, c in auto_t:
            if c in ("win", "loss"):
                sl_d = 1 - r["sl"] / r["entry"]
                rr_.append(((r["tp1"] / r["entry"] - 1) / sl_d) if c == "win" else -1.0)
        lines_ = ["🤖 <b>صفقات المسح التلقائي</b>", "",
                  f"✅ TP1 قبل الوقف: {aw} • ❌ وقف أولاً: {al_} • ⏳ غير محسومة: {len(auto_t) - aw - al_}"
                  + (f" • {sum(rr_) / len(rr_):+.2f}R" if rr_ else "")]
        for kd, lab in (("AUTOB", "ارتداد"), ("AUTOM", "زخم")):
            sub = [t for t in auto_t if t[0]["kind"] == kd]
            if sub:
                w_ = sum(1 for t in sub if t[2] == "win")
                l_ = sum(1 for t in sub if t[2] == "loss")
                lines_.append(f"• {lab}: ✅{w_} ❌{l_} ⏳{len(sub) - w_ - l_}")
        for r, x, c in sorted(auto_t, key=lambda t: t[0]["ts"])[:12]:
            ic = {"win": "✅", "loss": "❌", "open": "⏳", "noentry": "💤"}[c]
            lines_.append(f"{ic} <b>{html.escape(r['sym'])}</b> {KIND_LABEL.get(r['kind'], r['kind'])} "
                          f"(MC {fmt_usd((r.get('f') or {}).get('mcap') or 0)})")
        msgs.append("\n".join(lines_))
    wi = whatif_text(settled)
    if wi:
        msgs.append(wi)
    foot = ("ℹ️ النتائج تقريبية: حُسمت من شموع 1د (دخول بعد دقيقة من التنبيه) حيثما توفرت وإلا من لقطات كل 45 ثانية. "
            "الوقف والهدف في الدقيقة نفسها يُحسبان خسارة. العينة صغيرة — لا تبنِ قراراً على يوم واحد.")
    msgs.append(foot)

    data = []
    for r, x in settled:
        d = {k: v for k, v in r.items() if k not in ("chat",)}
        d["result"] = x
        if r["sl"] is not None and _outcome(r, x) == "loss":
            d["fail_reasons"] = [t for _, t in _fail_reasons(r, x)]
        data.append(d)
    return msgs, json.dumps(data, ensure_ascii=False, indent=1).encode("utf-8")


async def report_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.effective_message
    bd = context.application.bot_data
    uid = update.effective_user.id
    hours = 24.0
    if context.args:
        try:
            hours = max(1.0, min(48.0, float(context.args[0])))
        except ValueError:
            pass
    lock = bd.setdefault("report_lock", asyncio.Lock())
    if lock.locked():
        await msg.reply_text("⏳ أُعدّ تقريراً الآن، انتظر قليلاً.")
        return
    async with lock:
        status = await msg.reply_text("📊 جارٍ إعداد التقرير...")
        cutoff = time.time() - hours * 3600
        recs = sorted((r for r in (bd.get("track") or {}).values() if r["uid"] == uid and r["ts"] >= cutoff),
                      key=lambda r: r["ts"])
        if not recs:
            await status.edit_text("لا توجد صفقات مسجلة في هذه الفترة. التسجيل يبدأ تلقائياً مع كل /best وكل تنبيه ارتداد.")
            return
        try:
            settled = await settle_all(bd["http"], recs, status)
            texts, data = build_report(settled, hours)
        except Exception:
            log.exception("report failed")
            await status.edit_text("❌ فشل إعداد التقرير. راجع Logs في Render.")
            return
        try:
            await status.edit_text("✅ التقرير جاهز")
        except Exception:
            pass
        for t in texts:
            await safe_send(msg, t[:4000])
        try:
            await msg.reply_document(
                document=io.BytesIO(data),
                filename=f"solalpha_report_{time.strftime('%Y%m%d_%H%M')}.json",
                caption="📎 التفاصيل الكاملة (أرسل لي هذا الملف لنحلل الخلل ونحسّن البوت).",
            )
        except Exception:
            log.exception("send report file failed")


def _manual_store(bd: dict, uid: int) -> dict:
    store = bd.setdefault("manual", {}).setdefault(uid, {})
    cutoff = time.time() - MANUAL_TTL_HOURS * 3600
    for k in [k for k, s in store.items() if s.added < cutoff]:
        store.pop(k, None)
    return store


async def on_user_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """يلتقط العقود من رسالة نصية أو محوّلة (Forward) ويحفظها لهذا المستخدم."""
    msg = update.effective_message
    user = update.effective_user
    if msg is None or user is None:
        return
    parts = [msg.text or msg.caption or ""]
    for ent in list(msg.entities or []) + list(msg.caption_entities or []):
        if getattr(ent, "url", None):
            parts.append(ent.url)                       # رابط مخفي خلف نص
    rm = getattr(msg, "reply_markup", None)
    for row in getattr(rm, "inline_keyboard", None) or []:
        for b in row:
            if getattr(b, "url", None):
                parts.append(b.url)                     # أزرار الرسالة المحوّلة
    mints, hints = extract_addresses("\n".join(parts))
    if not (mints or hints):
        await msg.reply_text("لم أجد عنوان عقد Solana في هذه الرسالة. الصق العقد أو حوّل لي رسالة الإشارة، ثم أرسل /best.")
        return

    ts, post_id = None, 0                               # ts يبقى None للصق العادي
    fo = getattr(msg, "forward_origin", None)
    if fo is not None:
        d = getattr(fo, "date", None)
        ts = d.timestamp() if d else None
        chat = getattr(fo, "chat", None)
        if chat is not None and (getattr(chat, "username", "") or "").lower() == SIGNAL_CHANNEL.lower():
            post_id = getattr(fo, "message_id", 0) or 0
    elif getattr(msg, "forward_date", None):            # توافق مع إصدارات PTB الأقدم
        ts = msg.forward_date.timestamp()
        fc = getattr(msg, "forward_from_chat", None)
        if fc is not None and (getattr(fc, "username", "") or "").lower() == SIGNAL_CHANNEL.lower():
            post_id = getattr(msg, "forward_from_message_id", 0) or 0

    store = _manual_store(context.application.bot_data, user.id)
    now = time.time()
    fresh = already = 0
    for a in mints:
        prev = store.get(a)
        if prev is not None and prev.done:           # حُلِّلت سابقاً وهي تحت المراقبة
            already += 1
            continue
        store[a] = Signal(a, post_id, ts, False, True, now)
        fresh += 1
    for a in hints:
        if a not in store:
            store[a] = Signal(a, post_id, ts, True, True, now)
            fresh += 1
    while len(store) > MANUAL_MAX:
        store.pop(min(store, key=lambda k: store[k].added), None)
    pending = sum(1 for s in store.values() if not s.done)
    await msg.reply_text(
        (f"✅ استلمت {fresh} عقد جديد." if fresh else "ℹ️ لا عقود جديدة في هذه الرسالة.")
        + (f"\n♻️ {already} عقد محلَّل سابقاً وتحت المراقبة — لن أعيد تحليله." if already else "")
        + f"\nبانتظار التحليل: {pending}. أرسل /best لتحليل الجديد فقط (و/best all لإعادة تحليل الكل)."
    )


async def clear_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    store = _manual_store(context.application.bot_data, update.effective_user.id)
    n = len(store)
    store.clear()
    await update.effective_message.reply_text(f"🧹 مُسح {n} عقد.")


async def best_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.effective_message
    bd = context.application.bot_data

    wait = _cooldown_wait(bd, update.effective_user.id)
    if wait:
        await msg.reply_text(f"⏱️ انتظر {wait} ثانية قبل فحص جديد.")
        return

    status_msg = await msg.reply_text("🔎 بدأ المسح...")

    async def progress(text: str) -> None:
        await status_msg.edit_text(text)

    res = None
    try:
        uid = update.effective_user.id
        force_all = bool(context.args) and context.args[0].lower() in ("all", "الكل")
        mstore = _manual_store(bd, uid)
        extras = [s for s in mstore.values() if force_all or not s.done]   # الجديد فقط
        had_done = any(s.done for s in mstore.values())
        cached = bd.get("last_scan") if not extras else None
        if cached and time.time() - cached.finished_at < SCAN_CACHE_SEC:
            res = cached
        else:
            async with bd["scan_lock"]:
                res = await asyncio.wait_for(
                    run_scan(bd["http"], progress, SCAN_BUDGET_SEC, extras), timeout=SCAN_BUDGET_SEC + 30
                )
                bd["last_scan"] = res if (res.discovered > 0 and not extras) else None

        if res.discovered > 0:
            for s in extras:
                s.done = True
        if res.signals == 0 and had_done and not extras:
            text = (
                "ℹ️ لا توجد عقود جديدة منذ آخر تحليل.\n"
                "العقود السابقة ما زالت تحت المراقبة (/watchlist) ولن أعيد تحليلها.\n"
                "الصق عقوداً جديدة ثم /best، أو أرسل /best all لإعادة تحليل الكل."
            )
        elif res.signals == 0:
            text = format_no_signals(res)
        elif res.discovered == 0:
            text = (
                f"⚠️ وجدتُ {res.signals} عقداً لكن لم أحصل على بيانات سوق لأي منها.\n\n"
                f"حالة المصادر:\n{api_status_text()}\n\n"
                "• HTTP 403/429 أو timeout ⇒ المشكلة في اتصال سيرفر Render بالمصدر (جرّب بعد دقيقة).\n"
                "• HTTP 200 ⇒ العقود غير مدرجة بعد (جديدة جداً أو ما زالت قبل الإدراج).\n"
                "شغّل /diag لاختبار المصادر مباشرة."
            )
        elif res.ranked:
            best = res.ranked[0]
            alts, seen = [], {best.m["mint"]}
            for c in res.ranked[1:]:
                if c.m["mint"] in seen:
                    continue
                seen.add(c.m["mint"])
                alts.append(c)
                if len(alts) == 2:
                    break
            text = format_best(best, alts)
        else:
            text = format_no_result(res)
    except asyncio.TimeoutError:
        text = "⌛ انتهت مهلة المسح (المصادر بطيئة). حاول مجدداً بعد قليل."
    except Exception:
        log.exception("Scan failed")
        text = "❌ حدث خطأ أثناء المسح. حاول مجدداً بعد قليل."

    await safe_edit(status_msg, msg, text)
    if res is not None and (res.verdicts or res.unresolved):
        await safe_send(msg, format_verdicts(res))
    if res is not None and res.watch:
        added = add_watch(bd, update.effective_user.id, update.effective_chat.id, res.watch)
        if added:
            await safe_send(msg, format_watch_added(added))
    if res is not None and res.signals and res.discovered:
        try:
            record_scan(bd, update.effective_user.id, update.effective_chat.id, res)
        except Exception:
            log.exception("record_scan failed")


async def diag_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.effective_message
    bd = context.application.bot_data

    wait = _cooldown_wait(bd, update.effective_user.id)
    if wait:
        await msg.reply_text(f"⏱️ انتظر {wait} ثانية.")
        return

    status_msg = await msg.reply_text("🧪 جارٍ تشخيص قراءة القناة ...")
    try:
        async with bd["scan_lock"]:
            client = bd["http"]
            signals, info = await asyncio.wait_for(discover_signals(client), timeout=60)
            addrs = [s.mint for s in signals]
            hints = [s.mint for s in signals if s.is_hint]
            sink: dict = {}
            if addrs:
                try:
                    await asyncio.wait_for(fetch_pairs(client, addrs, hints, sink), timeout=40)
                except asyncio.TimeoutError:
                    pass
            pairs = list(sink.values())
            probes = await probe_apis(client)
        stats: Counter = Counter()
        passed = 0
        for p in pairs:
            m = extract_metrics(p)
            if m is None:
                stats["بيانات ناقصة"] += 1
                continue
            reason = pre_filter(m)
            if reason:
                stats[reason] += 1
            else:
                passed += 1

        manual_n = len(_manual_store(bd, update.effective_user.id))
        age = info.get("newest_age_min")
        lines = [f"🧪 <b>تشخيص قراءة القناة</b> (@{html.escape(SIGNAL_CHANNEL)})", ""]
        lines.append(f"• Telethon: {'✅ متصل (جلسة مستخدم)' if TL.get('client') else '❌ غير متصل'}")
        lines.append(f"• قراءة القناة: {'✅ نجحت عبر ' + str(info['source']) if info['ok'] else '❌ فشلت'}")
        shown = info["errors"] if not info["ok"] else [e for e in info["errors"] if not e.startswith("Telethon") or info["source"] != "Telethon"]
        for er in shown[:4]:
            lines.append(f"   ↳ {html.escape(str(er))}")
        lines += [
            f"• رسائل مقروءة: {info['messages']} ",
            f"• رسائل فيها عقود: {info['with_addr']}",
            f"• أحدث رسالة: {fmt_age(age) if age is not None else 'غير معروف'}",
            f"• عقود أرسلتها يدوياً (منتظرة): {manual_n}",
            "",
            f"🔢 عقود فريدة مستخرجة: {len(addrs)} | أزواج DexScreener: {len(pairs)}",
            f"✅ اجتازت الفلتر الأولي: {passed}",
        ]
        missing = len(addrs) - len(pairs)
        if missing > 0 and pairs:
            lines.append(f"• بلا زوج على DexScreener: ~{missing}")
        lines += ["", "🌐 <b>اختبار المصادر من السيرفر:</b>"] + [f"• {n}: {r}" for n, r in probes]
        if stats:
            lines += ["", "📋 <b>أسباب الاستبعاد:</b>"]
            for reason, n in stats.most_common(12):
                lines.append(f"• {html.escape(reason)}: {n}")
        text = "\n".join(lines)
    except Exception:
        log.exception("Diag failed")
        text = "❌ فشل التشخيص. راجع Logs في Render."

    await safe_edit(status_msg, msg, text)


async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    log.error("Unhandled exception", exc_info=context.error)


# ───────────────────────────── Health server + Keep-alive + التشغيل ─────────────────────────────


async def handle_health_check(request: web.Request) -> web.Response:
    return web.Response(text="OK", status=200)


async def start_web_server() -> web.AppRunner:
    app = web.Application()
    app.router.add_get("/", handle_health_check)
    app.router.add_get("/health", handle_health_check)
    runner = web.AppRunner(app)
    await runner.setup()
    port = int(os.getenv("PORT", "8080"))
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()
    log.info("Health check server running on port %s", port)
    return runner


async def keepalive_loop(client: httpx.AsyncClient) -> None:
    """يمنع نوم خدمة Render المجانية: طلب HTTP دوري على الرابط العام للخدمة."""
    url = os.getenv("RENDER_EXTERNAL_URL") or os.getenv("SELF_URL")
    if not url:
        log.info("Keep-alive disabled (no RENDER_EXTERNAL_URL / SELF_URL)")
        return
    target = url.rstrip("/") + "/health"
    log.info("Keep-alive pinging %s every %ss", target, KEEPALIVE_SEC)
    while True:
        await asyncio.sleep(KEEPALIVE_SEC)
        try:
            await client.get(target)
        except Exception as e:
            log.warning("Keep-alive failed: %s", e)


async def run_all() -> None:
    app = Application.builder().token(TELEGRAM_BOT_TOKEN).concurrent_updates(True).build()
    app.add_handler(CommandHandler("start", start_cmd))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CommandHandler("best", best_cmd))
    app.add_handler(CommandHandler("diag", diag_cmd))
    app.add_handler(CommandHandler("clear", clear_cmd))
    app.add_handler(CommandHandler("watchlist", watchlist_cmd))
    app.add_handler(CommandHandler("unwatch", unwatch_cmd))
    app.add_handler(CommandHandler("report", report_cmd))
    app.add_handler(CommandHandler("export", export_cmd))
    app.add_handler(CommandHandler("scan_on", scan_on_cmd))
    app.add_handler(CommandHandler("scan_off", scan_off_cmd))
    app.add_handler(CommandHandler("scan", scan_cmd))
    app.add_handler(CommandHandler("scan_now", scan_now_cmd))
    app.add_handler(MessageHandler(filters.ALL, register_chat), group=-1)
    app.add_handler(MessageHandler(
        filters.ChatType.PRIVATE & ~filters.COMMAND & (filters.TEXT | filters.CAPTION), on_user_message
    ))
    app.add_error_handler(on_error)

    runner = await start_web_server()
    await app.initialize()
    await post_init(app)
    await app.start()
    await app.updater.start_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=True)
    keepalive = asyncio.create_task(keepalive_loop(app.bot_data["http"]))
    watcher = asyncio.create_task(watch_loop(app))
    scanner = asyncio.create_task(scan_loop(app))
    log.info("SolAlpha Sniper v5.0 is running — signal channel: @%s", SIGNAL_CHANNEL)

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop.set)
        except (NotImplementedError, RuntimeError):
            pass

    try:
        await stop.wait()
    finally:
        log.info("Shutting down...")
        keepalive.cancel()
        watcher.cancel()
        scanner.cancel()
        try:
            await app.updater.stop()
            await app.stop()
            await post_shutdown(app)
            await app.shutdown()
        finally:
            await runner.cleanup()


def main() -> None:
    if not TELEGRAM_BOT_TOKEN:
        raise SystemExit("TELEGRAM_BOT_TOKEN غير مضبوط في متغيرات البيئة.")
    asyncio.run(run_all())


if __name__ == "__main__":
    main()
