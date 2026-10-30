import re

def patch(fname, pairs, regex_pairs=()):
    s = open(fname, encoding="utf-8").read()
    for old, new in pairs:
        if s.count(old) != 1:
            raise SystemExit(f"[{fname}] не найден фрагмент (или найден не 1 раз):\n{old[:80]}")
        s = s.replace(old, new)
    for pat, new in regex_pairs:
        s, n = re.subn(pat, new, s, count=1, flags=re.M)
        if n != 1:
            raise SystemExit(f"[{fname}] не найдена строка по шаблону: {pat}")
    open(fname, "w", encoding="utf-8").write(s)

# ---------------- config.py ----------------
patch("config.py", [], [
    (r'^LOT_LINK_TEMPLATE = .*$',
     'LOT_LINK_TEMPLATE = "https://t.me/portals/market?startapp=gift_{gift_id}"\n'
     '# Ссылка на подарок в самом Telegram (просмотр NFT)\n'
     'TG_NFT_LINK_TEMPLATE = "https://t.me/nft/{slug}"'),
    (r'^    "floor_min_discount": .*$',
     '    "floor_rel_pct": "-5",       # цена лота относительно флора, %: -10 = на 10% ниже, +10 = на 10% выше\n'
     '    "require_profit": "1",       # требовать чистую прибыль > 0\n'
     '    "max_age_min": "15",         # брать лоты не старше N минут (0 = любые)'),
    (r'^    "scan_sort": "both",.*$',
     '    "scan_sort": "latest",       # latest | price_asc | both'),
])

# ---------------- bot.py ----------------
patch("bot.py", [
    ("import logging\nimport sys\n", "import logging\nimport re\nimport sys\n"),
    ("from dataclasses import dataclass\n", "from dataclasses import dataclass\nfrom datetime import datetime\n"),
    ("    mono: float | None = None     # схожесть цвета, %\n",
     "    mono: float | None = None     # схожесть цвета, %\n    listed_at: float | None = None\n    slug: str = \"\"\n"),
    ("def parse_lots(data) -> list[Lot]:",
     'def _parse_ts(s) -> float | None:\n'
     '    try:\n'
     '        return datetime.fromisoformat(str(s).replace("Z", "+00:00")).timestamp()\n'
     '    except Exception:  # noqa: BLE001\n'
     '        return None\n\n\n'
     'def _make_slug(name, num, photo) -> str:\n'
     '    """Слаг подарка для ссылки t.me/nft/<slug>, например toybear-41771."""\n'
     '    m = re.search(r"/gift/([A-Za-z0-9_\\-]+-\\d+)\\.", str(photo or ""))\n'
     '    if m:\n'
     '        return m.group(1)\n'
     '    clean = re.sub(r"[^A-Za-z0-9]", "", str(name or ""))\n'
     '    return f"{clean}-{num}" if clean and num else ""\n\n\n'
     'def parse_lots(data) -> list[Lot]:'),
    ('            price=price, floor=_f(it.get("floor_price")), photo=str(it.get("photo_url") or ""),\n',
     '            price=price, floor=_f(it.get("floor_price")), photo=str(it.get("photo_url") or ""),\n'
     '            listed_at=_parse_ts(it.get("listed_at")),\n'
     '            slug=_make_slug(it.get("name"), num, it.get("photo_url")),\n'),
    ("def lot_link(lot_id: str) -> str:\n    return config.LOT_LINK_TEMPLATE.format(gift_id=lot_id)\n",
     "def lot_link(lot_id: str) -> str:\n    return config.LOT_LINK_TEMPLATE.format(gift_id=lot_id)\n\n\n"
     "def tg_link(lot) -> str:\n    return config.TG_NFT_LINK_TEMPLATE.format(slug=lot.slug) if lot.slug else \"\"\n"),
    ('        min_disc = db.get_float("floor_min_discount")\n',
     '        rel_pct = db.get_float("floor_rel_pct")\n        req_profit = db.get_bool("require_profit")\n'
     '        max_age = db.get_float("max_age_min")\n        now = time.time()\n'),
    ("            if lot.id in already:\n                continue\n",
     "            if lot.id in already:\n                continue\n"
     "            if max_age and lot.listed_at and now - lot.listed_at > max_age * 60:\n                continue\n"),
    ("                lot.floor_ok = lot.net > 0 and lot.disc >= min_disc and lot.net >= min_profit\n",
     "                lot.floor_ok = (lot.price <= lot.floor * (1 + rel_pct / 100.0)\n"
     "                                and (not req_profit or (lot.net > 0 and lot.net >= min_profit)))\n"),
    ('                reasons.append(f"📉 Ниже флора (−{lot.disc:.1f}%)")\n',
     '                reasons.append(f"📉 Цена {\'ниже\' if lot.disc >= 0 else \'выше\'} флора на {abs(lot.disc):.1f}%")\n'),
    ('        f"🎁 <b>{esc(lot.name)}</b>" + (f" #{esc(lot.number)}" if lot.number else ""),\n',
     '        title_line(lot),\n'),
    ('        lines.append(f"📊 Флор: {lot.floor:g} TON" + (f" (−{lot.disc:.1f}%)" if lot.disc and lot.disc > 0 else ""))\n',
     '        rel = "" if lot.disc is None else (f" (−{lot.disc:.1f}%)" if lot.disc > 0 else f" (+{abs(lot.disc):.1f}% выше)")\n'
     '        lines.append(f"📊 Флор: {lot.floor:g} TON{rel}")\n'),
    ('    lines += ["", f\'🔗 <a href="{lot_link(lot.id)}">Открыть лот</a>\']\n',
     '    lines += ["", f\'🔗 <a href="{lot_link(lot.id)}">Открыть в Portals</a>\']\n'),
    ('        [Btn("🛒 Открыть лот", url=lot_link(lot.id))],\n',
     '        [Btn("🛒 Лот в Portals", url=lot_link(lot.id))]\n'
     '        + ([Btn("🎁 В Telegram", url=tg_link(lot))] if lot.slug else []),\n'),
    ("async def send_signal(app: Application, lot: Lot, reasons: list[str]) -> bool:\n",
     "def title_line(lot: Lot) -> str:\n"
     "    title = esc(lot.name) + (f\" #{esc(lot.number)}\" if lot.number else \"\")\n"
     "    if lot.slug:\n"
     "        return f'🎁 <a href=\"{tg_link(lot)}\"><b>{title}</b></a>'\n"
     "    return f\"🎁 <b>{title}</b>\"\n\n\n"
     "async def send_signal(app: Application, lot: Lot, reasons: list[str]) -> bool:\n"),
    ('    "floor_min_discount": dict(kind="num", label="Мин. скидка к флору", unit="%", step=(1, 5), min=0, max=90),\n',
     '    "floor_rel_pct": dict(kind="num", label="Цена лота к флору (− ниже, + выше)", unit="%", step=(1, 5), min=-90, max=200),\n'
     '    "require_profit": dict(kind="bool", label="Требовать чистую прибыль > 0"),\n'
     '    "max_age_min": dict(kind="num", label="Возраст лота до (0 = любой)", unit="мин", step=(1, 5), min=0, max=1440),\n'),
    ('["floor_enabled", "floor_min_discount", "min_profit"]', '["floor_enabled", "floor_rel_pct", "require_profit", "min_profit"]'),
    ('["scan_enabled", "scan_interval", "scan_pages", "scan_sort", "request_delay"]',
     '["scan_enabled", "max_age_min", "scan_interval", "scan_pages", "scan_sort", "request_delay"]'),
    ('    "floor": "Лот попадает в сигнал, если <b>чистая прибыль &gt; 0</b> (флор минус комиссия маркета минус цена), "\n'
     '             "скидка к флору не меньше заданной, а прибыль не меньше минимальной.",\n',
     '    "floor": "Лот проходит, если <b>цена ≤ флор × (1 + X%)</b>. Например −10% — только лоты на 10% ниже флора, "\n'
     '             "+10% — до 10% выше флора. Если цена выше флора, выключите «Требовать чистую прибыль», "\n'
     '             "иначе такие лоты не пройдут (прибыль будет отрицательной).",\n'),
    ('        f"(скидка ≥ {fmtnum(db.get_float(\'floor_min_discount\'))}%, прибыль ≥ {fmtnum(db.get_float(\'min_profit\'))} TON)\\n"\n',
     '        f"(цена к флору ≤ {db.get_float(\'floor_rel_pct\'):+g}%, прибыль {\'≥ \' + fmtnum(db.get_float(\'min_profit\')) + \' TON\' if db.get_bool(\'require_profit\') else \'не нужна\'})\\n"\n'),
    ("    await db.init_db()\n    S.lock = asyncio.Lock()\n",
     "    await db.init_db()\n"
     "    if db.get_str(\"migr_v2\") != \"1\":  # однократный перенос старых настроек\n"
     "        await db.set_setting(\"floor_rel_pct\", -db.get_float(\"floor_min_discount\") if db.get_str(\"floor_min_discount\") else -5)\n"
     "        await db.set_setting(\"scan_sort\", \"latest\")\n"
     "        await db.set_setting(\"migr_v2\", \"1\")\n"
     "    S.lock = asyncio.Lock()\n"),
])
print("✅ Патч применён. Запускайте: python bot.py")
