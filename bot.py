"""Флип-бот для Portals: сканер + меню на кнопках (python-telegram-bot v20+)."""
import asyncio
import html
import logging
import re
import sys
import time
from dataclasses import dataclass
from datetime import datetime

from curl_cffi.requests import AsyncSession
from telegram import BotCommand, InlineKeyboardButton, InlineKeyboardMarkup as Kb, Update
from telegram.constants import ParseMode
from telegram.error import BadRequest, TelegramError
from telegram.ext import (Application, ApplicationBuilder, CallbackQueryHandler,
                          CommandHandler, ContextTypes, MessageHandler, filters)

import analyzer
import config
import database as db

logging.basicConfig(format="%(asctime)s | %(levelname)-7s | %(message)s",
                    datefmt="%H:%M:%S", level=logging.INFO)
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)
logging.getLogger("apscheduler").setLevel(logging.WARNING)
log = logging.getLogger("flipbot")


def Btn(text, cb=None, url=None):
    return InlineKeyboardButton(text, callback_data=cb, url=url)


# ======================================================================
#  Глобальное состояние
# ======================================================================
class S:
    session: AsyncSession | None = None
    lock: asyncio.Lock | None = None
    started = time.time()
    alert_ts: dict[str, float] = {}
    auth_ok: bool | None = None       # None = неизвестно, False = нужен новый authData
    consec_errors = 0
    recent_names: dict[str, str] = {}  # id лота -> название (для кнопки "бан коллекции")


# ======================================================================
#  API Portals
# ======================================================================
class ApiError(Exception):
    pass


class AuthError(ApiError):
    pass


class CloudflareError(ApiError):
    pass


class RateLimitError(ApiError):
    pass


@dataclass
class Lot:
    id: str
    name: str
    number: str
    model: str
    backdrop: str
    price: float
    floor: float
    photo: str
    net: float | None = None      # чистая прибыль при продаже по флору (после комиссии)
    disc: float | None = None     # скидка к флору, %
    floor_ok: bool = False
    mono: float | None = None     # схожесть цвета, %
    listed_at: float | None = None
    slug: str = ""
    mono_price_ok: bool = True


SORT_MAP = {"latest": "listed_at desc", "price_asc": "price asc"}


def auth_header() -> str:
    a = db.get_str("auth_data").strip()
    if a.lower().startswith("authorization:"):
        a = a.split(":", 1)[1].strip()
    if not a:
        raise AuthError("authData не задан")
    if not a.lower().startswith("tma "):
        a = "tma " + a
    return a


async def api_search(sort: str, offset: int, limit: int, min_p: float = 0, max_p: float = 0):
    url = config.API_BASE.rstrip("/") + config.SEARCH_PATH
    auth = auth_header()
    headers = {
        "Authorization": auth,
        "Accept": "application/json",
        "Origin": "https://portal-market.com",
        "Referer": "https://portal-market.com/",
    }
    body = {"offset": offset, "limit": limit, "sort_by": SORT_MAP.get(sort, "listed_at desc"),
            "status": "listed"}
    if min_p:
        body["min_price"] = min_p
    if max_p:
        body["max_price"] = max_p

    steps = config.BACKOFF_STEPS
    for attempt in range(len(steps) + 1):
        try:
            if config.SEARCH_METHOD.upper() == "POST":
                payload = {"gift_name": "", "model": "", "backdrop": "", "authData": auth, **body}
                resp = await S.session.post(url, json=payload, headers=headers,
                                            timeout=config.HTTP_TIMEOUT)
            else:
                resp = await S.session.get(url, params=body, headers=headers,
                                           timeout=config.HTTP_TIMEOUT)
        except Exception as e:  # noqa: BLE001
            raise ApiError(f"Сетевая ошибка: {e}") from e

        code = resp.status_code
        if code == 429:
            await db.incr_stat("rate_limits")
            if attempt < len(steps):
                wait = steps[attempt]
                log.warning("429 Rate Limit — пауза %s сек (попытка %s/%s)", wait, attempt + 1, len(steps))
                await asyncio.sleep(wait)
                continue
            raise RateLimitError("429: лимит запросов не снялся после 3 пауз")
        if code in (401, 403):
            low = resp.text[:600].lower()
            if code == 403 and ("cloudflare" in low or "just a moment" in low or "cf-ray" in low):
                raise CloudflareError("Cloudflare заблокировал запрос (403)")
            raise AuthError(f"HTTP {code}")
        if code != 200:
            raise ApiError(f"HTTP {code}: {resp.text[:200]}")
        try:
            return resp.json()
        except Exception as e:  # noqa: BLE001
            raise ApiError("Ответ API не является JSON") from e
    raise RateLimitError("429")


def _f(x, default=0.0) -> float:
    try:
        return float(str(x).replace(",", "."))
    except (TypeError, ValueError):
        return default


def _attr(it: dict, kind: str) -> str:
    v = it.get(kind)
    if isinstance(v, str) and v:
        return v
    for a in it.get("attributes") or []:
        if isinstance(a, dict) and a.get("type") == kind:
            return str(a.get("value") or "")
    return ""


def _parse_ts(s) -> float | None:
    try:
        return datetime.fromisoformat(str(s).replace("Z", "+00:00")).timestamp()
    except Exception:  # noqa: BLE001
        return None


def _make_slug(name, num, photo) -> str:
    """Слаг подарка для ссылки t.me/nft/<slug>, например toybear-41771."""
    m = re.search(r"/gift/([A-Za-z0-9_\-]+-\d+)\.", str(photo or ""))
    if m:
        return m.group(1)
    clean = re.sub(r"[^A-Za-z0-9]", "", str(name or ""))
    return f"{clean}-{num}" if clean and num else ""


def parse_lots(data) -> list[Lot]:
    if isinstance(data, dict):
        items = data.get("results") or data.get("nfts") or data.get("items") or data.get("data") or []
    else:
        items = data or []
    lots: list[Lot] = []
    for it in items:
        if not isinstance(it, dict):
            continue
        gid = it.get("id")
        price = _f(it.get("price"), -1)
        if not gid or price <= 0:
            continue
        num = it.get("external_collection_number") or it.get("tg_id") or ""
        lots.append(Lot(
            id=str(gid), name=str(it.get("name") or "Unknown"), number=str(num),
            model=_attr(it, "model"), backdrop=_attr(it, "backdrop"),
            price=price, floor=_f(it.get("floor_price")), photo=str(it.get("photo_url") or ""),
            listed_at=_parse_ts(it.get("listed_at")),
            slug=_make_slug(it.get("name"), num, it.get("photo_url")),
        ))
    return lots


async def fetch_all_lots() -> dict[str, Lot]:
    sort_mode = db.get_str("scan_sort")
    sorts = ["latest", "price_asc"] if sort_mode == "both" else [sort_mode]
    pages = max(1, db.get_int("scan_pages"))
    delay = max(0.3, db.get_float("request_delay"))
    min_p, max_p = db.get_float("min_price"), db.get_float("max_price")
    out: dict[str, Lot] = {}
    for sort in sorts:
        for page in range(pages):
            data = await api_search(sort, page * config.PAGE_LIMIT, config.PAGE_LIMIT, min_p, max_p)
            lots = parse_lots(data)
            log.info("Запрос sort=%s стр.%d → %d лотов", sort, page + 1, len(lots))
            for lot in lots:
                out.setdefault(lot.id, lot)
            if len(lots) < config.PAGE_LIMIT:
                break
            await asyncio.sleep(delay)
        await asyncio.sleep(delay)
    return out


async def check_auth() -> tuple[bool, str]:
    try:
        data = await api_search("latest", 0, 1)
        n = len(parse_lots(data))
        S.auth_ok = True
        return True, f"✅ Авторизация работает (API ответил, лотов в выдаче: {n})."
    except AuthError as e:
        S.auth_ok = False
        return False, f"🔑 authData не принят ({e}). Обновите его."
    except CloudflareError as e:
        return False, f"🛡 {e}. Подождите или смените сеть/IMPERSONATE."
    except RateLimitError as e:
        return False, f"⏱ {e}"
    except ApiError as e:
        return False, f"⚠️ {e}"


# ======================================================================
#  Вспомогательные функции
# ======================================================================
def fmtnum(v: float) -> str:
    return f"{round(v, 2):g}"


def esc(s) -> str:
    return html.escape(str(s))


def lot_link(lot_id: str) -> str:
    return config.LOT_LINK_TEMPLATE.format(gift_id=lot_id)


def tg_link(lot) -> str:
    return config.TG_NFT_LINK_TEMPLATE.format(slug=lot.slug) if lot.slug else ""


async def safe_send(bot, text: str, kb: Kb | None = None, photo: str | None = None) -> bool:
    try:
        if photo:
            try:
                await bot.send_photo(config.OWNER_ID, photo=photo, caption=text,
                                     parse_mode=ParseMode.HTML, reply_markup=kb)
                return True
            except TelegramError as e:
                log.warning("send_photo не удался (%s) — отправляю текстом", e)
        await bot.send_message(config.OWNER_ID, text, parse_mode=ParseMode.HTML,
                               reply_markup=kb, disable_web_page_preview=True)
        return True
    except TelegramError as e:
        log.error("Не удалось отправить сообщение: %s", e)
        return False


async def alert(app: Application, key: str, text: str, kb: Kb | None = None, cooldown: int = 1800):
    now = time.time()
    if now - S.alert_ts.get(key, 0) < cooldown:
        return
    S.alert_ts[key] = now
    await safe_send(app.bot, text, kb)


AUTH_KB = Kb([[Btn("🔑 Обновить authData", "authset")]])
HOW_TO_AUTH = (
    "Как получить: откройте Portals в Telegram Web → F12 → Network → любой запрос к "
    "portal-market.com → заголовок <code>Authorization</code> (строка начинается с <code>tma </code>). "
    "Отправьте её командой <code>/auth строка</code>."
)


# ======================================================================
#  Сканер
# ======================================================================
async def scan_once(app: Application, manual: bool = False) -> str | None:
    if S.lock.locked():
        return "⏳ Сканирование уже идёт, подождите." if manual else None
    async with S.lock:
        if not manual and not db.get_bool("scan_enabled"):
            return None
        if not db.get_str("auth_data").strip():
            await alert(app, "noauth", "🔑 authData не задан. Задайте его, чтобы бот мог сканировать.\n\n"
                        + HOW_TO_AUTH, AUTH_KB)
            return "🔑 authData не задан."
        if S.auth_ok is False and not manual:
            log.info("Пропуск цикла: ждём новый authData")
            return None

        t0 = time.time()
        log.info("===== Старт сканирования =====")
        try:
            lots = await fetch_all_lots()
            S.auth_ok = True
            S.consec_errors = 0
        except AuthError as e:
            S.auth_ok = False
            await db.incr_stat("errors")
            log.error("Ошибка авторизации: %s", e)
            await alert(app, "auth", f"🔑 <b>Ошибка авторизации ({esc(e)})</b>\nauthData устарел или недействителен. "
                        f"Сканер приостановлен до обновления.\n\n{HOW_TO_AUTH}", AUTH_KB)
            return "🔑 Ошибка авторизации — обновите authData."
        except CloudflareError as e:
            await db.incr_stat("errors")
            log.error("%s", e)
            await alert(app, "cf", f"🛡 <b>{esc(e)}</b>\nЕсли повторяется — подождите или смените сеть "
                        "(Wi-Fi/мобильный интернет).", cooldown=900)
            return f"🛡 {e}"
        except RateLimitError as e:
            await db.incr_stat("errors")
            log.error("%s", e)
            await alert(app, "rl", f"⏱ <b>Rate limit</b>: {esc(e)}. Пропускаю цикл, попробую позже.", cooldown=600)
            return f"⏱ {e}"
        except ApiError as e:
            await db.incr_stat("errors")
            S.consec_errors += 1
            log.error("Ошибка API: %s", e)
            if S.consec_errors >= 5:
                await alert(app, "api", f"⚠️ Ошибка API {S.consec_errors} раз подряд: {esc(e)}", cooldown=900)
            return f"⚠️ {e}"

        await db.incr_stat("scans")
        await db.incr_stat("lots_seen", len(lots))

        # ---------- предварительный отбор ----------
        already = await db.notified_ids(list(lots.keys()))
        min_p, max_p = db.get_float("min_price"), db.get_float("max_price")
        banned, only = db.filters("ban"), db.filters("only")
        fee = config.MARKET_FEE_PERCENT / 100.0
        rel_pct = db.get_float("floor_rel_pct")
        req_profit = db.get_bool("require_profit")
        max_age = db.get_float("max_age_min")
        mono_use_rel = db.get_bool("mono_use_rel")
        mono_rel = db.get_float("mono_rel_pct")
        now = time.time()
        min_profit = db.get_float("min_profit")
        fe, me = db.get_bool("floor_enabled"), db.get_bool("mono_enabled")
        mode = db.get_str("signal_mode")
        thr = db.get_float("mono_threshold")

        cands: list[Lot] = []
        for lot in lots.values():
            S.recent_names[lot.id] = lot.name
            if lot.id in already:
                continue
            if max_age and lot.listed_at and now - lot.listed_at > max_age * 60:
                continue
            if min_p and lot.price < min_p:
                continue
            if max_p and lot.price > max_p:
                continue
            nm = lot.name.lower()
            if nm in banned or (only and nm not in only):
                continue
            if lot.floor > 0:
                lot.net = lot.floor * (1 - fee) - lot.price
                lot.disc = (lot.floor - lot.price) / lot.floor * 100
                lot.floor_ok = (lot.price <= lot.floor * (1 + rel_pct / 100.0)
                                and (not req_profit or (lot.net > 0 and lot.net >= min_profit)))
            lot.mono_price_ok = (not mono_use_rel) or lot.floor <= 0 or lot.price <= lot.floor * (1 + mono_rel / 100.0)
            cands.append(lot)
        if len(S.recent_names) > 1500:
            for k in list(S.recent_names)[:500]:
                S.recent_names.pop(k, None)

        # ---------- анализ монохрома ----------
        if me:
            need = [l for l in cands if l.mono_price_ok and (not (fe and mode == "all") or l.floor_ok)]
            need = need[:config.MAX_IMAGES_PER_SCAN]
            sem = asyncio.Semaphore(config.IMAGE_CONCURRENCY)

            async def job(l: Lot):
                async with sem:
                    res = await analyzer.analyze_url(S.session, l.photo)
                    if res:
                        l.mono = res["score"]

            await asyncio.gather(*(job(l) for l in need))
            await db.incr_stat("mono_checked", len(need))

        # ---------- решение ----------
        signals: list[tuple[Lot, list[str]]] = []
        for lot in cands:
            f_ok = fe and lot.floor_ok
            m_ok = me and lot.mono is not None and lot.mono >= thr and lot.mono_price_ok
            if fe and me:
                ok = (f_ok and m_ok) if mode == "all" else (f_ok or m_ok)
            elif fe:
                ok = f_ok
            elif me:
                ok = m_ok
            else:
                ok = False
            log.info("Лот %-18s #%-7s | цена %8.2f | флор %8.2f | прибыль %7s | моно %6s | %s",
                     lot.name[:18], lot.number, lot.price, lot.floor,
                     f"{lot.net:.2f}" if lot.net is not None else "—",
                     f"{lot.mono:.0f}%" if lot.mono is not None else "—",
                     "СИГНАЛ" if ok else "пропуск")
            if not ok:
                continue
            reasons = []
            if f_ok:
                reasons.append(f"📉 Цена {'ниже' if lot.disc >= 0 else 'выше'} флора на {abs(lot.disc):.1f}%")
            if m_ok:
                reasons.append(f"🎨 Монохром ({lot.mono:.0f}%)")
            signals.append((lot, reasons))

        signals.sort(key=lambda x: x[0].net if x[0].net is not None else -1e9, reverse=True)
        sent = 0
        for lot, reasons in signals[:max(1, db.get_int("max_signals"))]:
            if await send_signal(app, lot, reasons):
                sent += 1
                await asyncio.sleep(0.6)

        dt = time.time() - t0
        await db.set_stat("last_scan_ts", time.time())
        await db.set_stat("last_scan_sec", round(dt, 1))
        summary = (f"Лотов получено: {len(lots)}, новых к проверке: {len(cands)}, "
                   f"сигналов: {sent} ({dt:.1f} сек)")
        log.info("===== Конец: %s =====", summary)
        return "✅ " + summary


def title_line(lot: Lot) -> str:
    title = esc(lot.name) + (f" #{esc(lot.number)}" if lot.number else "")
    if lot.slug:
        return f'🎁 <a href="{tg_link(lot)}"><b>{title}</b></a>'
    return f"🎁 <b>{title}</b>"


async def send_signal(app: Application, lot: Lot, reasons: list[str]) -> bool:
    fee = config.MARKET_FEE_PERCENT
    lines = [
        "🔥 <b>СИГНАЛ НА ПОКУПКУ!</b>",
        "",
        title_line(lot),
    ]
    if lot.model or lot.backdrop:
        lines.append(f"🧬 {esc(lot.model or '—')}  |  🖼 {esc(lot.backdrop or '—')}")
    lines.append(f"💰 Цена: <b>{lot.price:g} TON</b>")
    if lot.floor > 0:
        rel = "" if lot.disc is None else (f" (−{lot.disc:.1f}%)" if lot.disc > 0 else f" (+{abs(lot.disc):.1f}% выше)")
        lines.append(f"📊 Флор: {lot.floor:g} TON{rel}")
        sign = "+" if lot.net is not None and lot.net >= 0 else ""
        lines.append(f"💵 Чистая прибыль: <b>{sign}{lot.net:.2f} TON</b> (комиссия {fee:g}%)")
    lines += ["", "🎯 Причины:"] + [f"  • {r}" for r in reasons]
    lines += ["", f'🔗 <a href="{lot_link(lot.id)}">Открыть в Portals</a>']
    kb = Kb([
        [Btn("🛒 Лот в Portals", url=lot_link(lot.id))]
        + ([Btn("🎁 В Telegram", url=tg_link(lot))] if lot.slug else []),
        [Btn("🚫 В чёрный список коллекций", f"ban|{lot.id}"), Btn("🗑 Удалить", "del")],
    ])
    ok = await safe_send(app.bot, "\n".join(lines), kb, photo=lot.photo or None)
    if ok:
        await db.mark_notified(lot.id, lot.name, lot.number, lot.model, lot.backdrop, lot.price,
                               lot.floor, lot.net, lot.mono, " / ".join(reasons))
        await db.incr_stat("signals")
        if lot.net is not None:
            await db.max_stat("best_profit", lot.net)
        log.info(">>> Сигнал отправлен: %s #%s (%.2f TON)", lot.name, lot.number, lot.price)
    return ok


async def scan_job(context: ContextTypes.DEFAULT_TYPE):
    try:
        await scan_once(context.application)
    except Exception:  # noqa: BLE001
        log.exception("Необработанная ошибка в сканере")
        await db.incr_stat("errors")


def schedule_scan(app: Application):
    for j in app.job_queue.get_jobs_by_name("scan"):
        j.schedule_removal()
    interval = max(30, db.get_int("scan_interval"))
    app.job_queue.run_repeating(scan_job, interval=interval, first=5, name="scan")
    log.info("Сканер запланирован: каждые %d сек", interval)


# ======================================================================
#  Меню
# ======================================================================
META = {
    "scan_enabled": dict(kind="bool", label="Сканер"),
    "floor_enabled": dict(kind="bool", label="Поиск ниже флора"),
    "mono_enabled": dict(kind="bool", label="Поиск монохрома"),
    "floor_rel_pct": dict(kind="num", label="Цена лота к флору (− ниже, + выше)", unit="%", step=(1, 5), min=-90, max=200),
    "require_profit": dict(kind="bool", label="Требовать чистую прибыль > 0"),
    "max_age_min": dict(kind="num", label="Возраст лота до (0 = любой)", unit="мин", step=(1, 5), min=0, max=1440),
    "min_profit": dict(kind="num", label="Мин. чистая прибыль", unit="TON", step=(0.1, 1), min=0, max=100000),
    "mono_threshold": dict(kind="num", label="Порог схожести", unit="%", step=(1, 5), min=50, max=100),
    "mono_use_rel": dict(kind="bool", label="Ограничить цену монохрома флором"),
    "mono_rel_pct": dict(kind="num", label="Цена монохрома к флору (− ниже, + выше)", unit="%", step=(1, 5), min=-90, max=500),
    "min_price": dict(kind="num", label="Мин. цена", unit="TON", step=(1, 10), min=0, max=1000000),
    "max_price": dict(kind="num", label="Макс. цена (0 = без лимита)", unit="TON", step=(1, 10), min=0, max=1000000),
    "scan_interval": dict(kind="num", label="Интервал сканера", unit="сек", step=(15, 60), min=30, max=3600),
    "scan_pages": dict(kind="num", label="Страниц за запрос (×50 лотов)", unit="", step=(1, 1), min=1, max=5),
    "max_signals": dict(kind="num", label="Макс. сигналов за цикл", unit="", step=(1, 5), min=1, max=30),
    "request_delay": dict(kind="num", label="Пауза между запросами", unit="сек", step=(0.5, 1), min=0.5, max=10),
    "scan_sort": dict(kind="choice", label="Что сканировать",
                      choices=[("both", "новые + дешёвые"), ("latest", "только новые"),
                               ("price_asc", "только дешёвые")]),
    "signal_mode": dict(kind="choice", label="Режим сигнала",
                        choices=[("any", "любой критерий"), ("all", "оба критерия")]),
}

SECTIONS = {
    "floor": ("📉 Поиск ниже флора", ["floor_enabled", "floor_rel_pct", "require_profit", "min_profit"]),
    "mono": ("🎨 Поиск монохрома", ["mono_enabled", "mono_threshold", "mono_use_rel", "mono_rel_pct"]),
    "gen": ("⚙️ Общие настройки", ["min_price", "max_price", "signal_mode", "max_signals"]),
    "scan": ("🛰 Параметры сканера", ["scan_enabled", "max_age_min", "scan_interval", "scan_pages", "scan_sort", "request_delay"]),
}

SECTION_HELP = {
    "floor": "Лот проходит, если <b>цена ≤ флор × (1 + X%)</b>. Например −10% — только лоты на 10% ниже флора, "
             "+10% — до 10% выше флора. Если цена выше флора, выключите «Требовать чистую прибыль», "
             "иначе такие лоты не пройдут (прибыль будет отрицательной).",
    "mono": "Сравнивается цвет модели (центр картинки) и фона (края) в HSV. Чем выше порог — тем строже отбор. "
            "Если слишком много/мало сигналов — подстройте порог. "
            "Цена монохрома ограничивается флором отдельно от поиска ниже флора: например +10% — "
            "берутся монохромы до 10% дороже флора, −5% — только на 5% ниже. Лоты без известного флора не ограничиваются.",
    "gen": "Диапазон цен применяется ко всем критериям. Режим «оба критерия» требует и флор, и монохром одновременно.",
    "scan": "Чем чаще интервал и больше страниц — тем выше риск 429. Для старта достаточно 60 сек и 1 страницы.",
}


def choice_label(key: str) -> str:
    cur = db.get_str(key)
    for val, lab in META[key]["choices"]:
        if val == cur:
            return lab
    return cur


def kb_section(sec: str) -> Kb:
    _, keys = SECTIONS[sec]
    rows = []
    for k in keys:
        m = META[k]
        if m["kind"] == "bool":
            rows.append([Btn(f"{'✅' if db.get_bool(k) else '❌'} {m['label']}", f"tg|{sec}|{k}")])
        elif m["kind"] == "choice":
            rows.append([Btn(f"🔁 {m['label']}: {choice_label(k)}", f"cy|{sec}|{k}")])
        else:
            s1, s2 = m["step"]
            val = fmtnum(db.get_float(k))
            rows.append([Btn(f"{m['label']}: {val} {m['unit']}".strip(), "noop")])
            rows.append([
                Btn(f"−{fmtnum(s2)}", f"adj|{sec}|{k}|-{s2}"),
                Btn(f"−{fmtnum(s1)}", f"adj|{sec}|{k}|-{s1}"),
                Btn("✏️", f"inp|{sec}|{k}"),
                Btn(f"+{fmtnum(s1)}", f"adj|{sec}|{k}|{s1}"),
                Btn(f"+{fmtnum(s2)}", f"adj|{sec}|{k}|{s2}"),
            ])
    if sec == "gen":
        rows.append([Btn("🛰 Параметры сканера", "m|scan")])
        rows.append([Btn("🔑 Обновить authData", "authset"), Btn("🧪 Проверить", "authtest")])
        rows.append([Btn("🧹 Сбросить историю", "histclr"), Btn("♻️ Сброс настроек", "m|rst")])
    rows.append([Btn("⬅️ Назад", "m|gen" if sec == "scan" else "m|main")])
    return Kb(rows)


def screen(name: str) -> tuple[str, Kb]:
    if name in SECTIONS:
        title, _ = SECTIONS[name]
        return f"<b>{title}</b>\n\n{SECTION_HELP[name]}", kb_section(name)

    if name == "stats":
        return "", Kb([[Btn("⬅️ Назад", "m|main")]])  # заполняется в async-функции

    if name == "rst":
        return ("♻️ <b>Сбросить все настройки к значениям по умолчанию?</b>\n"
                "authData, история и фильтры коллекций сохранятся."), Kb([
            [Btn("✅ Да, сбросить", "rst2"), Btn("❌ Отмена", "m|gen")]])

    # main
    on = db.get_bool("scan_enabled")
    auth = "✅ задан" if db.get_str("auth_data").strip() else "❌ не задан"
    if S.auth_ok is False:
        auth = "⚠️ не принят — обновите"
    mx = db.get_float("max_price")
    text = (
        "🤖 <b>Portals Flip Bot</b>\n\n"
        f"Сканер: {'🟢 работает' if on else '🔴 остановлен'} (каждые {db.get_int('scan_interval')} сек)\n"
        f"📉 Ниже флора: {'✅' if db.get_bool('floor_enabled') else '❌'} "
        f"(цена к флору ≤ {db.get_float('floor_rel_pct'):+g}%, прибыль {'≥ ' + fmtnum(db.get_float('min_profit')) + ' TON' if db.get_bool('require_profit') else 'не нужна'})\n"
        f"🎨 Монохром: {'✅' if db.get_bool('mono_enabled') else '❌'} "
        f"(порог {fmtnum(db.get_float('mono_threshold'))}%, цена к флору "
        f"{('≤ ' + format(db.get_float('mono_rel_pct'), '+g') + '%') if db.get_bool('mono_use_rel') else 'без лимита'})\n"
        f"💰 Цена: {fmtnum(db.get_float('min_price'))} – {fmtnum(mx) if mx else '∞'} TON\n"
        f"🎯 Режим: {choice_label('signal_mode')}\n"
        f"💸 Комиссия маркета: {config.MARKET_FEE_PERCENT:g}%\n"
        f"🔑 authData: {auth}"
    )
    kb = Kb([
        [Btn("⏸ Остановить сканер" if on else "▶️ Запустить сканер", "tg|main|scan_enabled")],
        [Btn("📉 Поиск ниже флора", "m|floor"), Btn("🎨 Поиск монохрома", "m|mono")],
        [Btn("⚙️ Общие настройки", "m|gen"), Btn("📊 Статистика", "m|stats")],
        [Btn("🔍 Сканировать сейчас", "scan"), Btn("📜 История", "m|hist")],
        [Btn("🚫 Фильтры коллекций", "m|flt"), Btn("🧪 Проверка authData", "authtest")],
    ])
    return text, kb


async def screen_async(name: str) -> tuple[str, Kb]:
    if name == "stats":
        st = await db.get_stats()
        day = await db.count_signals_since(int(time.time()) - 86400)
        last = st.get("last_scan_ts")
        last_s = time.strftime("%d.%m %H:%M:%S", time.localtime(last)) if last else "—"
        up = int(time.time() - S.started)
        text = (
            "📊 <b>Статистика</b>\n\n"
            f"🔄 Циклов сканирования: {int(st.get('scans', 0))}\n"
            f"👀 Лотов просмотрено: {int(st.get('lots_seen', 0))}\n"
            f"🖼 Картинок проанализировано: {int(st.get('mono_checked', 0))}\n"
            f"🔥 Сигналов всего: {int(st.get('signals', 0))} (за 24ч: {day})\n"
            f"🏆 Лучшая прибыль: {st.get('best_profit', 0):.2f} TON\n"
            f"⚠️ Ошибок: {int(st.get('errors', 0))} | 429: {int(st.get('rate_limits', 0))}\n"
            f"🕒 Последний цикл: {last_s} ({st.get('last_scan_sec', 0):g} сек)\n"
            f"⏱ Аптайм: {up // 3600}ч {up % 3600 // 60}м"
        )
        return text, Kb([[Btn("🔄 Обновить", "m|stats"), Btn("⬅️ Назад", "m|main")]])

    if name == "hist":
        rows = await db.history(10)
        if not rows:
            text = "📜 <b>История сигналов</b>\n\nПока пусто."
        else:
            parts = ["📜 <b>Последние сигналы</b>\n"]
            for r in rows:
                t = time.strftime("%d.%m %H:%M", time.localtime(r["ts"]))
                profit = f"{r['profit']:+.2f}" if r["profit"] is not None else "—"
                parts.append(f'• {t} <a href="{lot_link(r["gift_id"])}">{esc(r["name"])} #{esc(r["number"] or "")}</a> '
                             f'— {r["price"]:g} TON (прибыль {profit})')
            text = "\n".join(parts)
        return text, Kb([[Btn("🧹 Очистить историю", "histclr"), Btn("⬅️ Назад", "m|main")]])

    if name == "flt":
        ban, only = sorted(db.filters("ban")), sorted(db.filters("only"))
        text = (
            "🚫 <b>Фильтры коллекций</b>\n\n"
            f"<b>Чёрный список</b> (игнорируются): {esc(', '.join(ban)) if ban else 'пусто'}\n"
            f"<b>Белый список</b> (только они): {esc(', '.join(only)) if only else 'пусто — все коллекции'}\n\n"
            "Названия вводятся как на маркете, например <code>Toy Bear</code>. "
            "Можно несколько через запятую. Нажмите на ❌, чтобы убрать запись."
        )
        rows = [[Btn("➕ В чёрный список", "fadd|ban"), Btn("➕ В белый список", "fadd|only")]]
        for i, n in enumerate(ban[:10]):
            rows.append([Btn(f"❌ ЧС: {n}", f"fdel|ban|{i}")])
        for i, n in enumerate(only[:10]):
            rows.append([Btn(f"❌ БС: {n}", f"fdel|only|{i}")])
        rows.append([Btn("🧹 Очистить ЧС", "fclr|ban"), Btn("🧹 Очистить БС", "fclr|only")])
        rows.append([Btn("⬅️ Назад", "m|main")])
        return text, Kb(rows)

    return screen(name)


async def render(q, text: str, kb: Kb):
    try:
        await q.edit_message_text(text, reply_markup=kb, parse_mode=ParseMode.HTML,
                                  disable_web_page_preview=True)
    except BadRequest as e:
        if "not modified" not in str(e).lower():
            raise


def owner_only(update: Update) -> bool:
    u = update.effective_user
    return bool(u and u.id == config.OWNER_ID)


# ======================================================================
#  Обработчики
# ======================================================================
async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not owner_only(update):
        return
    text, kb = screen("main")
    await update.message.reply_text(text, reply_markup=kb, parse_mode=ParseMode.HTML)


async def apply_auth(update: Update, raw: str):
    raw = raw.strip()
    if raw.lower().startswith("authorization:"):
        raw = raw.split(":", 1)[1].strip()
    if len(raw) < 30:
        await update.effective_chat.send_message("❌ Строка слишком короткая. Нужна полная строка authData.")
        return
    if not raw.lower().startswith("tma "):
        raw = "tma " + raw
    await db.set_setting("auth_data", raw)
    S.auth_ok = None
    S.alert_ts.clear()
    try:  # сообщение с токеном лучше удалить из чата
        if update.message:
            await update.message.delete()
    except TelegramError:
        pass
    warn = "" if "hash=" in raw else "\n⚠️ В строке нет <code>hash=</code> — возможно, скопировано не полностью."
    ok, msg = await check_auth()
    await update.effective_chat.send_message(f"🔑 authData сохранён.{warn}\n{msg}", parse_mode=ParseMode.HTML,
                                             reply_markup=Kb([[Btn("📋 В меню", "m|main")]]))


async def cmd_auth(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not owner_only(update):
        return
    if ctx.args:
        await apply_auth(update, " ".join(ctx.args))
    else:
        ctx.user_data["await"] = ("auth",)
        await update.message.reply_text("Отправьте строку authData следующим сообщением (или /auth строка).")


async def cmd_scan(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not owner_only(update):
        return
    await update.message.reply_text("🔍 Запускаю сканирование…")
    ctx.application.create_task(run_manual_scan(ctx.application))


async def run_manual_scan(app: Application):
    res = await scan_once(app, manual=True)
    if res:
        await safe_send(app.bot, res)


async def cmd_ban(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not owner_only(update):
        return
    name = " ".join(ctx.args).strip()
    if not name:
        await update.message.reply_text("Использование: /ban Toy Bear")
        return
    await db.add_filter("ban", name)
    await update.message.reply_text(f"🚫 «{name}» добавлена в чёрный список.")


async def cmd_unban(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not owner_only(update):
        return
    name = " ".join(ctx.args).strip()
    if not name:
        await update.message.reply_text("Использование: /unban Toy Bear")
        return
    await db.remove_filter("ban", name)
    await update.message.reply_text(f"✅ «{name}» убрана из чёрного списка.")


async def cmd_help(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not owner_only(update):
        return
    await update.message.reply_text(
        "<b>Команды</b>\n"
        "/menu — главное меню\n"
        "/auth &lt;строка&gt; — задать authData\n"
        "/scan — сканировать сейчас\n"
        "/ban &lt;название&gt; — в чёрный список коллекций\n"
        "/unban &lt;название&gt; — убрать из чёрного списка\n\n"
        "Всё остальное настраивается кнопками в меню.", parse_mode=ParseMode.HTML)


async def on_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not owner_only(update) or not update.message or not update.message.text:
        return
    aw = ctx.user_data.pop("await", None)
    if not aw:
        return
    text = update.message.text.strip()
    kind = aw[0]

    if kind == "auth":
        await apply_auth(update, text)
        return

    if kind in ("ban", "only"):
        names = [n.strip() for n in text.replace("\n", ",").split(",") if n.strip()]
        for n in names:
            await db.add_filter(kind, n)
        await update.message.reply_text(f"✅ Добавлено: {', '.join(names)}",
                                        reply_markup=Kb([[Btn("🚫 К фильтрам", "m|flt")]]))
        return

    if kind == "setting":
        _, sec, key = aw
        m = META[key]
        try:
            val = float(text.replace(",", "."))
        except ValueError:
            await update.message.reply_text("❌ Нужно число. Попробуйте ещё раз через ✏️.")
            return
        val = round(min(max(val, m["min"]), m["max"]), 2)
        await db.set_setting(key, val)
        if key == "scan_interval":
            schedule_scan(ctx.application)
        await update.message.reply_text(f"✅ {m['label']}: {fmtnum(val)} {m['unit']}".strip(),
                                        reply_markup=Kb([[Btn("⬅️ К настройкам", f"m|{sec}")]]))


async def on_cb(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    if q.from_user.id != config.OWNER_ID:
        await q.answer("Нет доступа", show_alert=True)
        return
    p = q.data.split("|")
    cmd = p[0]
    app = ctx.application

    if cmd == "noop":
        await q.answer()
        return

    if cmd == "m":
        await q.answer()
        text, kb = await screen_async(p[1])
        await render(q, text, kb)
        return

    if cmd == "tg":
        sec, key = p[1], p[2]
        await db.set_setting(key, "0" if db.get_bool(key) else "1")
        await q.answer("Включено" if db.get_bool(key) else "Выключено")
        text, kb = screen(sec)
        await render(q, text, kb)
        return

    if cmd == "cy":
        sec, key = p[1], p[2]
        vals = [v for v, _ in META[key]["choices"]]
        cur = db.get_str(key)
        nxt = vals[(vals.index(cur) + 1) % len(vals)] if cur in vals else vals[0]
        await db.set_setting(key, nxt)
        await q.answer(choice_label(key))
        text, kb = screen(sec)
        await render(q, text, kb)
        return

    if cmd == "adj":
        sec, key, delta = p[1], p[2], float(p[3])
        m = META[key]
        val = round(min(max(db.get_float(key) + delta, m["min"]), m["max"]), 2)
        await db.set_setting(key, val)
        if key == "scan_interval":
            schedule_scan(app)
        await q.answer(f"{fmtnum(val)} {m['unit']}".strip())
        text, kb = screen(sec)
        await render(q, text, kb)
        return

    if cmd == "inp":
        sec, key = p[1], p[2]
        m = META[key]
        ctx.user_data["await"] = ("setting", sec, key)
        await q.answer()
        await q.message.reply_text(f"✏️ Введите значение для «{m['label']}» ({m['min']:g}…{m['max']:g}):")
        return

    if cmd == "scan":
        await q.answer("Сканирую…")
        app.create_task(run_manual_scan(app))
        return

    if cmd == "authset":
        ctx.user_data["await"] = ("auth",)
        await q.answer()
        await q.message.reply_text("🔑 Отправьте строку authData следующим сообщением.\n\n" + HOW_TO_AUTH,
                                   parse_mode=ParseMode.HTML)
        return

    if cmd == "authtest":
        await q.answer("Проверяю…")
        if not db.get_str("auth_data").strip():
            await q.message.reply_text("🔑 authData не задан.", reply_markup=AUTH_KB)
            return
        ok, msg = await check_auth()
        await q.message.reply_text(msg, reply_markup=None if ok else AUTH_KB)
        return

    if cmd == "histclr":
        await db.clear_notified()
        await q.answer("История очищена — лоты могут прийти повторно", show_alert=True)
        text, kb = await screen_async("hist")
        await render(q, text, kb)
        return

    if cmd == "rst2":
        await db.reset_settings()
        schedule_scan(app)
        await q.answer("Настройки сброшены", show_alert=True)
        text, kb = screen("main")
        await render(q, text, kb)
        return

    if cmd == "fadd":
        ctx.user_data["await"] = (p[1],)
        await q.answer()
        label = "чёрный" if p[1] == "ban" else "белый"
        await q.message.reply_text(f"Введите названия коллекций в {label} список (через запятую):")
        return

    if cmd == "fdel":
        kind, idx = p[1], int(p[2])
        names = sorted(db.filters(kind))
        if idx < len(names):
            await db.remove_filter(kind, names[idx])
        await q.answer("Удалено")
        text, kb = await screen_async("flt")
        await render(q, text, kb)
        return

    if cmd == "fclr":
        await db.clear_filters(p[1])
        await q.answer("Очищено")
        text, kb = await screen_async("flt")
        await render(q, text, kb)
        return

    if cmd == "ban":  # кнопка под сигналом
        name = S.recent_names.get(p[1])
        if not name:
            await q.answer("Название не найдено (бот перезапускался)", show_alert=True)
            return
        await db.add_filter("ban", name)
        await q.answer(f"«{name}» в чёрном списке", show_alert=True)
        return

    if cmd == "del":
        await q.answer()
        try:
            await q.message.delete()
        except TelegramError:
            pass
        return

    await q.answer()


async def on_error(update, ctx: ContextTypes.DEFAULT_TYPE):
    log.error("Ошибка обработчика: %s", ctx.error, exc_info=ctx.error)


# ======================================================================
#  Жизненный цикл
# ======================================================================
async def post_init(app: Application):
    await db.init_db()
    if db.get_str("migr_v2") != "1":  # однократный перенос старых настроек
        await db.set_setting("floor_rel_pct", -db.get_float("floor_min_discount") if db.get_str("floor_min_discount") else -5)
        await db.set_setting("scan_sort", "latest")
        await db.set_setting("migr_v2", "1")
    S.lock = asyncio.Lock()
    S.session = AsyncSession(impersonate=config.IMPERSONATE)
    schedule_scan(app)
    try:
        await app.bot.set_my_commands([
            BotCommand("menu", "Главное меню"),
            BotCommand("scan", "Сканировать сейчас"),
            BotCommand("auth", "Задать authData"),
            BotCommand("ban", "В чёрный список коллекций"),
            BotCommand("unban", "Убрать из чёрного списка"),
            BotCommand("help", "Помощь"),
        ])
    except TelegramError as e:
        log.warning("set_my_commands: %s", e)
    text, kb = screen("main")
    await safe_send(app.bot, "✅ Бот запущен.\n\n" + text, kb)
    log.info("Бот запущен. Владелец: %s", config.OWNER_ID)


async def post_shutdown(app: Application):
    if S.session is not None:
        try:
            await S.session.close()
        except Exception:  # noqa: BLE001
            pass
    await db.close_db()
    log.info("Бот остановлен.")


def main():
    if not config.BOT_TOKEN or "PASTE" in config.BOT_TOKEN:
        sys.exit("❌ Укажите BOT_TOKEN в config.py или переменной окружения BOT_TOKEN")
    if not config.OWNER_ID:
        sys.exit("❌ Укажите OWNER_ID в config.py или переменной окружения OWNER_ID")

    app = (ApplicationBuilder()
           .token(config.BOT_TOKEN)
           .post_init(post_init)
           .post_shutdown(post_shutdown)
           .concurrent_updates(True)
           .build())
    if app.job_queue is None:
        sys.exit('❌ Установите: pip install "python-telegram-bot[job-queue]"')

    owner = filters.User(user_id=config.OWNER_ID)
    app.add_handler(CommandHandler(["start", "menu"], cmd_start, filters=owner))
    app.add_handler(CommandHandler("auth", cmd_auth, filters=owner))
    app.add_handler(CommandHandler("scan", cmd_scan, filters=owner))
    app.add_handler(CommandHandler("ban", cmd_ban, filters=owner))
    app.add_handler(CommandHandler("unban", cmd_unban, filters=owner))
    app.add_handler(CommandHandler("help", cmd_help, filters=owner))
    app.add_handler(CallbackQueryHandler(on_cb))
    app.add_handler(MessageHandler(owner & filters.TEXT & ~filters.COMMAND, on_text))
    app.add_error_handler(on_error)

    app.run_polling(close_loop=False, allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
