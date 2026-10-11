# -*- coding: utf-8 -*-
"""
SolAlpha Sniper — Telegram bot (Render Background Worker)

الأمر /best يقوم بمسح مباشر لسوق Solana ويطبق خط فلترة صارم:
  1) اكتشاف العملات: DexScreener + GeckoTerminal (+ Birdeye اختياري)
  2) فلترة السيولة/الفوليوم/الزخم (Volume Spike)
  3) فلترة أمنية مطلقة عبر RugCheck (Mint/Freeze/LP/Dev/Top10/Bundles)
  4) خطة سكالبيج بمنطقة Fibonacci Golden Zone (0.5 - 0.618) من شموع GeckoTerminal
  5) Score /100 — أي عملة أقل من 80 تُحجب

متغيرات البيئة:
  TELEGRAM_BOT_TOKEN   (مطلوب)
  BIRDEYE_API_KEY      (اختياري — مصدر إضافي للعملات الرائجة)
  MIN_SCORE, MIN_LIQUIDITY_USD, MIN_VOLUME_H1_USD, MAX_TOP10_PCT, MAX_DEV_PCT,
  MIN_LP_LOCKED_PCT, RUGCHECK_MAX_NORM ... (كلها اختيارية، القيم الافتراضية أدناه)
"""

import asyncio
import html
import logging
import math
import os
import time
from collections import Counter
from dataclasses import dataclass, field
from typing import Any, Optional

import httpx
from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import Application, CommandHandler, ContextTypes

# ───────────────────────────── الإعدادات ─────────────────────────────

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
BIRDEYE_API_KEY = os.getenv("BIRDEYE_API_KEY", "").strip()


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return default


MIN_SCORE = _env_float("MIN_SCORE", 80)                      # حد القبول النهائي
MIN_LIQUIDITY_USD = _env_float("MIN_LIQUIDITY_USD", 20_000)
MIN_VOLUME_H1_USD = _env_float("MIN_VOLUME_H1_USD", 15_000)
MIN_MCAP_USD = _env_float("MIN_MCAP_USD", 50_000)
MAX_MCAP_USD = _env_float("MAX_MCAP_USD", 50_000_000)
MIN_PAIR_AGE_MIN = _env_float("MIN_PAIR_AGE_MIN", 60)
MAX_H1_CHANGE_PCT = _env_float("MAX_H1_CHANGE_PCT", 300)     # تجنب القمم البارابولية
MIN_VOL_PACE = _env_float("MIN_VOL_PACE", 1.0)               # فوليوم 5m*12 / فوليوم 1h
MIN_H1_VS_H6 = _env_float("MIN_H1_VS_H6", 1.3)               # مفاجأة الفوليوم

MAX_DEV_PCT = _env_float("MAX_DEV_PCT", 5)
MAX_TOP10_PCT = _env_float("MAX_TOP10_PCT", 25)
MIN_LP_LOCKED_PCT = _env_float("MIN_LP_LOCKED_PCT", 95)      # 100% مع هامش تقريب
RUGCHECK_MAX_NORM = _env_float("RUGCHECK_MAX_NORM", 20)      # score_normalised (0=آمن)
RUGCHECK_MAX_RAW = _env_float("RUGCHECK_MAX_RAW", 500)       # احتياطي إن غاب score_normalised

MIN_CANDLES = 20
MAX_WINDOW_CANDLES = 72           # آخر 6 ساعات (شموع 5 دقائق)
SWING_LOOKBACK_CANDLES = 36       # البحث عن قاع الموجة قبل القمة
MIN_LEG_PCT = _env_float("MIN_LEG_PCT", 12)                  # أقل حجم للموجة الصاعدة
MAX_ABOVE_ZONE_PCT = _env_float("MAX_ABOVE_ZONE_PCT", 10)    # أقصى ابتعاد فوق المنطقة
MAX_SL_RISK_PCT = _env_float("MAX_SL_RISK_PCT", 25)
SL_BUFFER = 0.015
TP1_PCT = 0.18
TP2_PCT = 0.25

MAX_DISCOVERED_TOKENS = 150
MAX_SECURITY_CHECKS = int(_env_float("MAX_SECURITY_CHECKS", 10))
SCAN_TIMEOUT_SEC = 110
USER_COOLDOWN_SEC = 30
SCAN_CACHE_SEC = 45
RUGCHECK_CACHE_SEC = 600

DEX = "https://api.dexscreener.com"
GECKO = "https://api.geckoterminal.com/api/v2"
RUGCHECK = "https://api.rugcheck.xyz/v1"
BIRDEYE = "https://public-api.birdeye.so"

DEX_TOKEN_LIST_URLS = (
    f"{DEX}/token-boosts/latest/v1",
    f"{DEX}/token-boosts/top/v1",
    f"{DEX}/token-profiles/latest/v1",
)

EXCLUDED_MINTS = {
    "So11111111111111111111111111111111111111112",   # wSOL
    "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",  # USDC
    "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB",  # USDT
}

logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    level=logging.INFO,
)
logging.getLogger("httpx").setLevel(logging.WARNING)
log = logging.getLogger("solalpha")

# ───────────────────────────── أدوات مساعدة ─────────────────────────────


def to_float(v: Any, default: float = 0.0) -> float:
    """تحويل آمن: بعض الـ APIs (GeckoTerminal) تُرجع الأرقام كنصوص."""
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


async def fetch_json(
    client: httpx.AsyncClient,
    url: str,
    params: Optional[dict] = None,
    headers: Optional[dict] = None,
    retries: int = 2,
) -> Any:
    for attempt in range(retries + 1):
        try:
            r = await client.get(url, params=params, headers=headers)
            if r.status_code == 429:
                await asyncio.sleep(1.5 * (attempt + 1))
                continue
            if r.status_code != 200:
                log.warning("HTTP %s from %s", r.status_code, url)
                return None
            return r.json()
        except (httpx.HTTPError, ValueError) as e:
            log.warning("Request failed (%s): %s", url, e)
            await asyncio.sleep(0.8 * (attempt + 1))
    return None


# ───────────────────────────── اكتشاف العملات ─────────────────────────────


async def _dex_token_lists(client: httpx.AsyncClient) -> list:
    out = []
    for url in DEX_TOKEN_LIST_URLS:
        data = await fetch_json(client, url)
        if isinstance(data, list):
            for it in data:
                if isinstance(it, dict) and it.get("chainId") == "solana" and it.get("tokenAddress"):
                    out.append(it["tokenAddress"])
    return out


async def _gecko_tokens(client: httpx.AsyncClient, path: str, params: Optional[dict] = None) -> list:
    data = await fetch_json(client, f"{GECKO}/networks/solana/{path}", params=params)
    out = []
    items = data.get("data") if isinstance(data, dict) else None
    for item in items or []:
        try:
            tid = item["relationships"]["base_token"]["data"]["id"]  # "solana_<mint>"
            out.append(tid.split("_", 1)[1])
        except (KeyError, TypeError, IndexError):
            continue
    return out


async def _birdeye_tokens(client: httpx.AsyncClient) -> list:
    if not BIRDEYE_API_KEY:
        return []
    data = await fetch_json(
        client,
        f"{BIRDEYE}/defi/token_trending",
        params={"sort_by": "volume24hUSD", "sort_type": "desc", "offset": 0, "limit": 20},
        headers={"X-API-KEY": BIRDEYE_API_KEY, "x-chain": "solana", "accept": "application/json"},
    )
    tokens = ((data or {}).get("data") or {}).get("tokens") if isinstance(data, dict) else None
    return [t["address"] for t in (tokens or []) if isinstance(t, dict) and t.get("address")]


async def discover_token_addresses(client: httpx.AsyncClient) -> list:
    results = await asyncio.gather(
        _dex_token_lists(client),
        _gecko_tokens(client, "trending_pools", {"duration": "1h"}),
        _gecko_tokens(client, "trending_pools", {"duration": "5m"}),
        _gecko_tokens(client, "new_pools"),
        _birdeye_tokens(client),
        return_exceptions=True,
    )
    addrs = []
    for r in results:
        if isinstance(r, Exception):
            log.warning("Discovery source failed: %s", r)
            continue
        addrs.extend(r)
    return list(dict.fromkeys(a for a in addrs if a and a not in EXCLUDED_MINTS))[:MAX_DISCOVERED_TOKENS]


async def fetch_pairs(client: httpx.AsyncClient, addresses: list) -> list:
    """جلب بيانات الأزواج من DexScreener (30 عنواناً لكل طلب) واختيار أعمق زوج لكل عملة."""
    chunks = [addresses[i:i + 30] for i in range(0, len(addresses), 30)]
    responses = await asyncio.gather(
        *(fetch_json(client, f"{DEX}/tokens/v1/solana/{','.join(c)}") for c in chunks)
    )
    best: dict = {}
    for data in responses:
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
    return list(best.values())


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
        "chg5": to_float(chg.get("m5")),
        "chg1": to_float(chg.get("h1")),
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
    """يرجع سبب الرفض أو None إن اجتازت الفلترة الأولية (مفاجأة فوليوم + زخم إيجابي)."""
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
    # شمعة 5 دقائق إيجابية = تأكيد ارتداد، و1h إيجابي = اتجاه صاعد
    if m["chg5"] <= 0 or m["chg1"] <= 0:
        return "الزخم غير إيجابي (5m/1h)"
    if m["chg1"] > MAX_H1_CHANGE_PCT:
        return "ارتفاع مفرط خلال ساعة (خطر قمة)"
    if m["vol_pace"] < MIN_VOL_PACE:
        return "وتيرة الفوليوم خامدة (5m أقل من متوسط الساعة)"
    if m["h1_vs_h6"] < MIN_H1_VS_H6:
        return "لا توجد مفاجأة في الفوليوم"
    if m["buy_ratio5"] < 0.5:
        return "ضغط بيع مسيطر"
    return None


def prelim_rank(m: dict) -> float:
    return min(m["vol_pace"], 4) + min(m["h1_vs_h6"], 6) / 2 + min(m["liq_mcap"], 0.2) * 10


# ───────────────────────────── الفلترة الأمنية (RugCheck) ─────────────────────────────


@dataclass
class Security:
    passed: bool
    reasons: list = field(default_factory=list)
    top10: float = 0.0
    dev: Optional[float] = None
    lp_locked: Optional[float] = None
    rug_norm: Optional[float] = None
    rug_raw: Optional[float] = None


_RUG_CACHE: dict = {}


async def get_rugcheck_report(client: httpx.AsyncClient, mint: str) -> Optional[dict]:
    cached = _RUG_CACHE.get(mint)
    if cached and time.time() - cached[0] < RUGCHECK_CACHE_SEC:
        return cached[1]
    data = await fetch_json(client, f"{RUGCHECK}/tokens/{mint}/report", retries=2)
    await asyncio.sleep(0.3)
    if isinstance(data, dict):
        _RUG_CACHE[mint] = (time.time(), data)
        return data
    return None


def analyze_security(report: dict, pair_address: str) -> Security:
    reasons: list = []
    token = report.get("token") if isinstance(report.get("token"), dict) else {}

    def authority_revoked(key: str) -> Optional[bool]:
        for src in (token, report):
            if key in src:
                return src[key] in (None, "", "null")
        return None  # غير معروف → يُعامل كفشل

    # 1) Mint / Freeze
    mint_rev = authority_revoked("mintAuthority")
    if mint_rev is not True:
        reasons.append("Mint Authority غير ملغاة" if mint_rev is False else "تعذر التحقق من Mint Authority")
    freeze_rev = authority_revoked("freezeAuthority")
    if freeze_rev is not True:
        reasons.append("Freeze Authority غير ملغاة" if freeze_rev is False else "تعذر التحقق من Freeze Authority")

    # 2) Rugged / Transfer fee / Danger risks
    if report.get("rugged") is True:
        reasons.append("التوكن مُعلَّم كـ Rugged")
    tf = report.get("transferFee")
    if isinstance(tf, dict) and to_float(tf.get("pct")) > 0:
        reasons.append("رسوم تحويل على التوكن")

    risks = [r for r in (report.get("risks") or []) if isinstance(r, dict)]
    if any(str(r.get("level", "")).lower() == "danger" for r in risks):
        reasons.append("RugCheck: مخاطر بمستوى Danger")

    # 3) RugCheck rating = Good
    norm = report.get("score_normalised")
    raw = report.get("score")
    rug_norm = to_float(norm, -1.0) if norm is not None else None
    rug_raw = to_float(raw, -1.0) if raw is not None else None
    if rug_norm is not None:
        rug_ok = rug_norm <= RUGCHECK_MAX_NORM
    elif rug_raw is not None:
        rug_ok = rug_raw <= RUGCHECK_MAX_RAW
    else:
        rug_ok = False
    if not rug_ok:
        reasons.append("تقييم RugCheck ليس Good")

    # 4) LP burned / locked
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

    # 5) الحيتان: Top10 (بعد استبعاد حسابات الـ Pool)، Dev، Bundles/Snipers
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
    elif top10 > MAX_TOP10_PCT:
        reasons.append("تركّز كبار الملاك Top10 مرتفع")

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
        if dev > MAX_DEV_PCT:
            reasons.append("حصة المطور Dev مرتفعة")
    # (إن لم يُعرف creator لا نحجب بسبب Dev — يُعرض كـ "غير متاح")

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
        reasons.append("أنماط Bundled/Sniped/Insider")

    return Security(
        passed=not reasons,
        reasons=reasons,
        top10=top10,
        dev=dev,
        lp_locked=lp_locked,
        rug_norm=rug_norm,
        rug_raw=rug_raw,
    )


# ───────────────────────────── Fibonacci + الخطة + النتيجة ─────────────────────────────


@dataclass
class ScalpPlan:
    swing_low: float
    swing_high: float
    zone_low: float
    zone_high: float
    status: str            # IN_ZONE | ABOVE_ZONE
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


async def fetch_candles(client: httpx.AsyncClient, pool: str) -> list:
    data = await fetch_json(
        client,
        f"{GECKO}/networks/solana/pools/{pool}/ohlcv/minute",
        params={"aggregate": 5, "limit": 100, "currency": "usd"},
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


def compute_scalp_plan(candles: list, price: float):
    """يرجع (ScalpPlan, None) أو (None, سبب_الرفض)."""
    if len(candles) < MIN_CANDLES:
        return None, "شموع غير كافية لحساب Fibonacci"

    window = candles[-MAX_WINDOW_CANDLES:]
    hi_idx = max(range(len(window)), key=lambda i: window[i][2])
    high = window[hi_idx][2]
    seg = window[max(0, hi_idx - SWING_LOOKBACK_CANDLES): hi_idx + 1]
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
        if above_pct > MAX_ABOVE_ZONE_PCT:
            return None, "السعر بعيد فوق المنطقة الذهبية (مطاردة)"
        status, entry = "ABOVE_ZONE", (zone_low + zone_high) / 2
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
    ), None


def compute_score(m: dict, sec: Security, plan: ScalpPlan):
    """Score /100 = سيولة/MC (25) + فوليوم (25) + زخم سعر (10) + Fibonacci (20) + أمان (20)."""
    b: dict = {}
    b["liq_mcap"] = 25 * clamp(m["liq_mcap"] / 0.10, 0, 1)

    pace = 12 * clamp(m["vol_pace"] / 2.0, 0, 1)
    trend = 8 * clamp(m["h1_vs_h6"] / 3.0, 0, 1)
    buys = 5 * clamp((m["buy_ratio5"] - 0.5) / 0.2, 0, 1)
    b["volume"] = pace + trend + buys

    mom = 0.0
    if m["chg5"] > 0:
        mom += 4 if m["chg5"] <= 25 else 2
    if m["chg1"] > 0:
        mom += 6 if m["chg1"] <= 150 else 3
    b["momentum"] = mom

    if plan.status == "IN_ZONE":
        b["fib"] = 20.0
    else:
        b["fib"] = 14 * (1 - 0.5 * clamp(plan.above_pct / MAX_ABOVE_ZONE_PCT, 0, 1))

    safety = 8 * (1 - clamp(sec.top10 / MAX_TOP10_PCT, 0, 1))
    safety += 4 * (1 - clamp((sec.dev or 0.0) / MAX_DEV_PCT, 0, 1)) if sec.dev is not None else 2
    if sec.rug_norm is not None:
        safety += 8 * (1 - clamp(sec.rug_norm / RUGCHECK_MAX_NORM, 0, 1))
    else:
        safety += 4
    b["safety"] = safety

    total = int(round(sum(b.values())))
    return min(total, 100), b


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
    best: Optional[Candidate] = None
    closest: Optional[Candidate] = None
    stats: Counter = field(default_factory=Counter)
    discovered: int = 0
    after_prefilter: int = 0
    after_security: int = 0
    finished_at: float = 0.0


async def run_scan(client: httpx.AsyncClient) -> ScanResult:
    res = ScanResult()
    addrs = await discover_token_addresses(client)
    pairs = await fetch_pairs(client, addrs)
    res.discovered = len(pairs)

    pre = []
    for p in pairs:
        m = extract_metrics(p)
        if m is None:
            res.stats["بيانات ناقصة"] += 1
            continue
        reason = pre_filter(m)
        if reason:
            res.stats[reason] += 1
        else:
            pre.append(m)
    res.after_prefilter = len(pre)
    pre.sort(key=prelim_rank, reverse=True)
    pre = pre[:MAX_SECURITY_CHECKS]

    sem = asyncio.Semaphore(3)

    async def check(m: dict):
        async with sem:
            report = await get_rugcheck_report(client, m["mint"])
        return m, (analyze_security(report, m["pair"]) if report else None)

    checked = await asyncio.gather(*(check(m) for m in pre))

    secure = []
    for m, sec in checked:
        if sec is None:
            res.stats["تعذر جلب تقرير RugCheck"] += 1
        elif not sec.passed:
            for r in sec.reasons:
                res.stats[r] += 1
        else:
            secure.append((m, sec))
    res.after_security = len(secure)

    candidates = []
    for i, (m, sec) in enumerate(secure):
        if i > 0:
            await asyncio.sleep(1.2)  # حد GeckoTerminal المجاني
        candles = await fetch_candles(client, m["pair"])
        plan, reject = compute_scalp_plan(candles, m["price"])
        if plan is None:
            res.stats[reject or "فشل حساب الخطة"] += 1
            continue
        score, breakdown = compute_score(m, sec, plan)
        cand = Candidate(m, sec, plan, score, breakdown)
        candidates.append(cand)
        if score < MIN_SCORE:
            res.stats[f"Score أقل من {MIN_SCORE:.0f}"] += 1

    if candidates:
        top = max(candidates, key=lambda c: c.score)
        if top.score >= MIN_SCORE:
            res.best = top
        else:
            res.closest = top
    res.finished_at = time.time()
    return res


# ───────────────────────────── تنسيق الرسائل ─────────────────────────────


def format_best(c: Candidate) -> str:
    m, s, p = c.m, c.sec, c.plan
    name, sym = html.escape(m["name"]), html.escape(m["symbol"])

    def mc(price: float) -> str:
        return fmt_usd(m["mcap"] * price / m["price"])

    if p.status == "IN_ZONE":
        status = "✅ السعر داخل المنطقة الذهبية الآن"
    else:
        status = f"⏳ السعر أعلى المنطقة بـ {p.above_pct:.1f}% — انتظر الارتداد (Limit داخل المنطقة)"

    dev_txt = f"{s.dev:.1f}%" if s.dev is not None else "غير متاح"
    rug_txt = f"Good (خطورة {s.rug_norm:.0f}/100)" if s.rug_norm is not None else f"Good (raw {s.rug_raw:.0f})"
    b = c.breakdown

    return (
        f"🎯 <b>SolAlpha Sniper — أفضل فرصة حالياً</b>\n\n"
        f"🪙 <b>{name}</b> (${sym})\n"
        f"📋 العقد CA (اضغط للنسخ):\n<code>{m['mint']}</code>\n\n"
        f"📊 <b>التقييم: {c.score}/100</b>\n"
        f"<i>سيولة/MC {b['liq_mcap']:.0f}/25 • فوليوم {b['volume']:.0f}/25 • زخم {b['momentum']:.0f}/10 "
        f"• Fib {b['fib']:.0f}/20 • أمان {b['safety']:.0f}/20</i>\n\n"
        f"🛡️ <b>الأمان والسيولة</b>\n"
        f"• Mint ✅ ملغاة | Freeze ✅ ملغاة\n"
        f"• LP: {s.lp_locked:.0f}% محروقة/مقفولة 🔒\n"
        f"• RugCheck: {rug_txt}\n"
        f"• السيولة: {fmt_usd(m['liq'])} | MC: {fmt_usd(m['mcap'])} | Liq/MC: {m['liq_mcap'] * 100:.1f}%\n\n"
        f"🐋 <b>الحيتان</b>\n"
        f"• Top10: {s.top10:.1f}% | Dev: {dev_txt}\n"
        f"• Bundled/Snipers: ✅ لا يوجد\n\n"
        f"⚡ <b>الزخم</b>\n"
        f"• 5m: {m['chg5']:+.1f}% | 1h: {m['chg1']:+.1f}%\n"
        f"• وتيرة فوليوم 5m: ×{m['vol_pace']:.1f} | الساعة مقابل متوسط 6h: ×{m['h1_vs_h6']:.1f}\n"
        f"• ضغط الشراء (5m): {m['buy_ratio5'] * 100:.0f}%\n\n"
        f"📈 <b>خطة السكالبيج (Scalp Setup)</b>\n"
        f"{status}\n"
        f"🟢 منطقة الدخول Golden Zone (0.5–0.618):\n"
        f"   <code>{fmt_price(p.zone_low)}</code> ← <code>{fmt_price(p.zone_high)}</code>\n"
        f"   (MC: {mc(p.zone_low)} ← {mc(p.zone_high)})\n"
        f"🎯 TP1 (+{TP1_PCT * 100:.0f}%): <code>{fmt_price(p.tp1)}</code> | MC {mc(p.tp1)}\n"
        f"   Fib 1.272: <code>{fmt_price(p.ext1272)}</code>\n"
        f"🎯 TP2 (+{TP2_PCT * 100:.0f}%): <code>{fmt_price(p.tp2)}</code> | MC {mc(p.tp2)}\n"
        f"   Fib 1.618: <code>{fmt_price(p.ext1618)}</code>\n"
        f"🛑 SL (تحت القاع بـ 1.5%): <code>{fmt_price(p.sl)}</code> | MC {mc(p.sl)}\n"
        f"⚖️ مخاطرة حتى SL: {p.risk_pct:.1f}% | R:R → TP1 {p.rr1:.2f} / TP2 {p.rr2:.2f}\n"
        f"📐 Swing: {fmt_price(p.swing_low)} → {fmt_price(p.swing_high)} (+{p.leg_pct:.0f}%)\n\n"
        f"🔗 <a href=\"https://dexscreener.com/solana/{m['pair']}\">DexScreener</a> • "
        f"<a href=\"https://rugcheck.xyz/tokens/{m['mint']}\">RugCheck</a>\n\n"
        f"⚠️ <i>تحليل آلي وليس نصيحة مالية. العملات الصغيرة عالية المخاطر — لا تدخل بأكثر مما تتحمل خسارته.</i>"
    )


def format_no_result(res: ScanResult) -> str:
    lines = [
        "🚫 <b>لا توجد فرصة تستوفي كل الشروط الآن</b>",
        "",
        f"🔎 عملات تم فحصها: {res.discovered}",
        f"⚡ اجتازت فلتر الفوليوم/الزخم: {res.after_prefilter}",
        f"🛡️ اجتازت الفلترة الأمنية: {res.after_security}",
    ]
    if res.closest:
        c = res.closest
        lines.append(
            f"📉 أقرب مرشح: {html.escape(c.m['name'])} بتقييم {c.score}/100 (أقل من {MIN_SCORE:.0f}) — محجوب."
        )
    if res.stats:
        lines += ["", "📋 <b>أبرز أسباب الاستبعاد:</b>"]
        for reason, n in res.stats.most_common(6):
            lines.append(f"• {html.escape(reason)}: {n}")
    lines += ["", "💡 عدم وجود صفقة أفضل من صفقة سيئة. أعد المحاولة بعد دقائق."]
    return "\n".join(lines)


# ───────────────────────────── أوامر تلغرام ─────────────────────────────

WELCOME = (
    "🎯 <b>SolAlpha Sniper</b>\n\n"
    "بوت مسح لحظي لعملات Solana بفلترة أمنية صارمة وخطة سكالبيج على Fibonacci Golden Zone.\n\n"
    "• /best — أفضل فرصة حالياً (إن وُجدت)\n"
    "• /help — شرح الفلاتر\n\n"
    "⚠️ أداة تحليل وليست نصيحة مالية."
)

HELP = (
    "🛡️ <b>الفلاتر الإلزامية (فشل واحد = استبعاد)</b>\n"
    "• Mint & Freeze Authority ملغاتان\n"
    f"• LP محروقة/مقفولة (≥ {MIN_LP_LOCKED_PCT:.0f}%)\n"
    f"• Dev ≤ {MAX_DEV_PCT:.0f}% و Top10 ≤ {MAX_TOP10_PCT:.0f}%\n"
    "• لا Bundled / Sniped / Insider wallets\n"
    "• تقييم RugCheck = Good\n"
    f"• Score ≥ {MIN_SCORE:.0f}/100\n\n"
    "📈 <b>الخطة:</b> دخول من Golden Zone (0.5–0.618)، TP1 +18%، TP2 +25%، SL تحت القاع بـ 1.5%."
)


async def post_init(application: Application) -> None:
    application.bot_data["http"] = httpx.AsyncClient(
        timeout=httpx.Timeout(15.0),
        headers={"User-Agent": "SolAlphaSniper/1.0", "Accept": "application/json"},
        follow_redirects=True,
    )
    application.bot_data["scan_lock"] = asyncio.Lock()
    application.bot_data["cooldowns"] = {}
    application.bot_data["last_scan"] = None


async def post_shutdown(application: Application) -> None:
    client = application.bot_data.get("http")
    if client:
        await client.aclose()


async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.effective_message.reply_text(WELCOME, parse_mode=ParseMode.HTML)


async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.effective_message.reply_text(HELP, parse_mode=ParseMode.HTML)


async def best_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.effective_message
    user = update.effective_user
    bd = context.application.bot_data

    now = time.time()
    last = bd["cooldowns"].get(user.id, 0)
    if now - last < USER_COOLDOWN_SEC:
        wait = int(USER_COOLDOWN_SEC - (now - last)) + 1
        await msg.reply_text(f"⏱️ انتظر {wait} ثانية قبل فحص جديد.")
        return
    bd["cooldowns"][user.id] = now

    lock: asyncio.Lock = bd["scan_lock"]
    status_msg = await msg.reply_text("🔎 جارٍ المسح اللحظي وتطبيق الفلاتر الأمنية... (قد يستغرق حتى دقيقة)")

    try:
        cached = bd.get("last_scan")
        if cached and time.time() - cached.finished_at < SCAN_CACHE_SEC:
            res = cached
        else:
            async with lock:
                cached = bd.get("last_scan")
                if cached and time.time() - cached.finished_at < SCAN_CACHE_SEC:
                    res = cached
                else:
                    res = await asyncio.wait_for(run_scan(bd["http"]), timeout=SCAN_TIMEOUT_SEC)
                    bd["last_scan"] = res
        text = format_best(res.best) if res.best else format_no_result(res)
    except asyncio.TimeoutError:
        text = "⌛ انتهت مهلة المسح (المصادر بطيئة). حاول مجدداً بعد قليل."
    except Exception:
        log.exception("Scan failed")
        text = "❌ حدث خطأ أثناء المسح. حاول مجدداً بعد قليل."

    try:
        await status_msg.edit_text(text, parse_mode=ParseMode.HTML, disable_web_page_preview=True)
    except Exception:
        log.exception("edit_text failed, sending a new message")
        await msg.reply_text(text, parse_mode=ParseMode.HTML, disable_web_page_preview=True)


async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    log.error("Unhandled exception", exc_info=context.error)


def main() -> None:
    if not TELEGRAM_BOT_TOKEN:
        raise SystemExit("TELEGRAM_BOT_TOKEN غير مضبوط في متغيرات البيئة.")
    app = (
        Application.builder()
        .token(TELEGRAM_BOT_TOKEN)
        .concurrent_updates(True)
        .post_init(post_init)
        .post_shutdown(post_shutdown)
        .build()
    )
    app.add_handler(CommandHandler("start", start_cmd))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CommandHandler("best", best_cmd))
    app.add_error_handler(on_error)
    log.info("SolAlpha Sniper is running (polling)...")
    app.run_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=True)


if __name__ == "__main__":
    main()
