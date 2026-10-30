"""aiosqlite: настройки, история сигналов, статистика, фильтры коллекций."""
import time

import aiosqlite

import config

_db: aiosqlite.Connection | None = None
_cache: dict[str, str] = {}
_filters: dict[str, set[str]] = {"ban": set(), "only": set()}

SCHEMA = """
CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY, value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS notified_gifts (
    gift_id TEXT PRIMARY KEY,
    name TEXT, number TEXT, model TEXT, backdrop TEXT,
    price REAL, floor REAL, profit REAL, mono_score REAL,
    reasons TEXT, ts INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS stats (
    key TEXT PRIMARY KEY, value REAL NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS collection_filters (
    kind TEXT NOT NULL, name TEXT NOT NULL,
    PRIMARY KEY (kind, name)
);
"""


async def init_db() -> None:
    global _db
    _db = await aiosqlite.connect(config.DB_PATH)
    _db.row_factory = aiosqlite.Row
    await _db.execute("PRAGMA journal_mode=WAL")
    await _db.executescript(SCHEMA)
    await _db.commit()

    _cache.clear()
    async with _db.execute("SELECT key, value FROM settings") as cur:
        async for row in cur:
            _cache[row["key"]] = row["value"]
    for k, v in config.DEFAULTS.items():
        if k not in _cache:
            _cache[k] = v
            await _db.execute("INSERT OR IGNORE INTO settings(key, value) VALUES(?, ?)", (k, v))
    if not _cache.get("auth_data") and config.PORTALS_AUTH_DATA:
        _cache["auth_data"] = config.PORTALS_AUTH_DATA
        await _db.execute("REPLACE INTO settings(key, value) VALUES('auth_data', ?)",
                          (config.PORTALS_AUTH_DATA,))

    for s in _filters.values():
        s.clear()
    async with _db.execute("SELECT kind, name FROM collection_filters") as cur:
        async for row in cur:
            _filters.setdefault(row["kind"], set()).add(row["name"])
    await _db.commit()


async def close_db() -> None:
    global _db
    if _db is not None:
        await _db.close()
        _db = None


# ---------------- Настройки ----------------
def get_str(key: str) -> str:
    return _cache.get(key, config.DEFAULTS.get(key, ""))


def get_float(key: str) -> float:
    try:
        return float(get_str(key).replace(",", "."))
    except ValueError:
        return float(config.DEFAULTS.get(key, "0") or 0)


def get_int(key: str) -> int:
    return int(round(get_float(key)))


def get_bool(key: str) -> bool:
    return get_str(key) in ("1", "true", "True", "yes", "on")


async def set_setting(key: str, value) -> None:
    value = str(value)
    _cache[key] = value
    await _db.execute("REPLACE INTO settings(key, value) VALUES(?, ?)", (key, value))
    await _db.commit()


async def reset_settings() -> None:
    """Сбрасывает все настройки, кроме authData."""
    auth = get_str("auth_data")
    await _db.execute("DELETE FROM settings")
    _cache.clear()
    for k, v in config.DEFAULTS.items():
        _cache[k] = v
        await _db.execute("INSERT INTO settings(key, value) VALUES(?, ?)", (k, v))
    _cache["auth_data"] = auth
    await _db.execute("REPLACE INTO settings(key, value) VALUES('auth_data', ?)", (auth,))
    await _db.commit()


# ---------------- Уведомления ----------------
async def notified_ids(ids: list[str]) -> set[str]:
    found: set[str] = set()
    for i in range(0, len(ids), 400):
        chunk = ids[i:i + 400]
        marks = ",".join("?" * len(chunk))
        async with _db.execute(
            f"SELECT gift_id FROM notified_gifts WHERE gift_id IN ({marks})", chunk
        ) as cur:
            async for row in cur:
                found.add(row["gift_id"])
    return found


async def mark_notified(gift_id, name, number, model, backdrop, price, floor,
                        profit, mono_score, reasons: str) -> None:
    await _db.execute(
        "REPLACE INTO notified_gifts(gift_id, name, number, model, backdrop, price, floor, "
        "profit, mono_score, reasons, ts) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
        (gift_id, name, number, model, backdrop, price, floor, profit, mono_score,
         reasons, int(time.time())),
    )
    await _db.commit()


async def history(limit: int = 10) -> list[aiosqlite.Row]:
    async with _db.execute(
        "SELECT * FROM notified_gifts ORDER BY ts DESC LIMIT ?", (limit,)
    ) as cur:
        return list(await cur.fetchall())


async def count_signals_since(ts: int) -> int:
    async with _db.execute("SELECT COUNT(*) AS c FROM notified_gifts WHERE ts >= ?", (ts,)) as cur:
        row = await cur.fetchone()
        return int(row["c"])


async def clear_notified() -> None:
    await _db.execute("DELETE FROM notified_gifts")
    await _db.commit()


# ---------------- Статистика ----------------
async def incr_stat(key: str, n: float = 1) -> None:
    await _db.execute(
        "INSERT INTO stats(key, value) VALUES(?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = value + excluded.value", (key, n))
    await _db.commit()


async def set_stat(key: str, value: float) -> None:
    await _db.execute("REPLACE INTO stats(key, value) VALUES(?, ?)", (key, value))
    await _db.commit()


async def max_stat(key: str, value: float) -> None:
    await _db.execute(
        "INSERT INTO stats(key, value) VALUES(?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = MAX(value, excluded.value)", (key, value))
    await _db.commit()


async def get_stats() -> dict[str, float]:
    async with _db.execute("SELECT key, value FROM stats") as cur:
        return {row["key"]: row["value"] async for row in cur}


# ---------------- Фильтры коллекций ----------------
def filters(kind: str) -> set[str]:
    return _filters.get(kind, set())


async def add_filter(kind: str, name: str) -> None:
    name = name.strip().lower()
    if not name:
        return
    _filters.setdefault(kind, set()).add(name)
    await _db.execute("INSERT OR IGNORE INTO collection_filters(kind, name) VALUES(?, ?)", (kind, name))
    await _db.commit()


async def remove_filter(kind: str, name: str) -> None:
    name = name.strip().lower()
    _filters.setdefault(kind, set()).discard(name)
    await _db.execute("DELETE FROM collection_filters WHERE kind=? AND name=?", (kind, name))
    await _db.commit()


async def clear_filters(kind: str) -> None:
    _filters.setdefault(kind, set()).clear()
    await _db.execute("DELETE FROM collection_filters WHERE kind=?", (kind,))
    await _db.commit()
