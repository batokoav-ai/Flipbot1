"""Анализ картинки подарка: совпадает ли цвет модели (центр) с фоном (края).

Работает в HSV. Прозрачные пиксели PNG игнорируются.
Возвращает схожесть 0..100 (100 = модель того же цвета, что и фон).
"""
import asyncio
import io
import logging
from collections import OrderedDict

import numpy as np
from PIL import Image

log = logging.getLogger("flipbot.analyzer")

SIZE = 96
_CACHE_MAX = 600
_cache: "OrderedDict[str, dict | None]" = OrderedDict()

# Маски считаются один раз
_yy, _xx = np.mgrid[0:SIZE, 0:SIZE]
_nx = (_xx + 0.5) / SIZE * 2 - 1
_ny = (_yy + 0.5) / SIZE * 2 - 1
_RING = np.maximum(np.abs(_nx), np.abs(_ny)) > 0.84      # края = фон
_CENTER = (_nx ** 2 + _ny ** 2) < 0.42 ** 2               # центр = модель


def _circular_mean_hue(h: np.ndarray, w: np.ndarray) -> float:
    ang = h * 2 * np.pi
    s = float(np.sum(w * np.sin(ang)))
    c = float(np.sum(w * np.cos(ang)))
    if abs(s) < 1e-9 and abs(c) < 1e-9:
        return 0.0
    return (np.arctan2(s, c) / (2 * np.pi)) % 1.0


def analyze_bytes(data: bytes) -> dict | None:
    """Синхронный анализ (вызывается через to_thread)."""
    img = Image.open(io.BytesIO(data)).convert("RGBA").resize((SIZE, SIZE), Image.Resampling.BILINEAR)
    alpha = np.asarray(img)[..., 3]
    hsv = np.asarray(img.convert("RGB").convert("HSV")).astype(np.float32) / 255.0
    h, s, v = hsv[..., 0], hsv[..., 1], hsv[..., 2]
    opaque = alpha > 128

    ring = _RING & opaque
    center = _CENTER & opaque
    if ring.sum() < 40 or center.sum() < 60:
        return None  # слишком много прозрачности — оценить нельзя

    # Цвет фона: круговое среднее оттенка (с весом по насыщенности) + медианы S и V
    bg_h = _circular_mean_hue(h[ring], s[ring] + 1e-3)
    bg_s = float(np.median(s[ring]))
    bg_v = float(np.median(v[ring]))

    # Расстояние каждого пикселя центра до цвета фона (0..1)
    ch, cs, cv = h[center], s[center], v[center]
    dh = np.abs(ch - bg_h)
    dh = np.minimum(dh, 1.0 - dh) * 2.0                       # 0..1
    sat_gate = np.sqrt(np.minimum(cs, bg_s))                  # у серых оттенок не важен
    hue_term = np.minimum(dh * 1.5, 1.0) * sat_gate
    sat_term = np.minimum(np.abs(cs - bg_s) * 2.0, 1.0)  # серый ↔ цветной = заметная разница
    d = 0.40 * hue_term + 0.30 * sat_term + 0.30 * np.abs(cv - bg_v)

    # Берём самые "непохожие на фон" 35% пикселей центра — это и есть модель.
    # Если даже они близки к фону — модель монохромная.
    k = max(10, int(len(d) * 0.35))
    top = np.sort(d)[-k:]
    model_dist = float(np.clip(top.mean(), 0.0, 1.0))
    score = round((1.0 - model_dist) * 100.0, 1)
    return {"score": score, "bg_hue": round(bg_h * 360), "bg_s": round(bg_s, 2), "bg_v": round(bg_v, 2)}


async def fetch_image(session, url: str) -> bytes:
    # Заголовки авторизации сюда НЕ передаём — картинки лежат на публичном CDN
    r = await session.get(url, timeout=20)
    if r.status_code != 200:
        raise ValueError(f"HTTP {r.status_code}")
    return r.content


async def analyze_url(session, url: str) -> dict | None:
    """Скачивает и анализирует картинку. Результат кэшируется по URL."""
    if not url:
        return None
    if url in _cache:
        _cache.move_to_end(url)
        return _cache[url]
    try:
        data = await fetch_image(session, url)
        result = await asyncio.to_thread(analyze_bytes, data)
    except Exception as e:  # noqa: BLE001
        log.warning("Не удалось проанализировать %s: %s", url, e)
        return None  # ошибки не кэшируем
    _cache[url] = result
    if len(_cache) > _CACHE_MAX:
        _cache.popitem(last=False)
    return result
