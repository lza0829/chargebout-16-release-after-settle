from __future__ import annotations
import logging
import sqlite3
from contextlib import contextmanager

from app import config

logger = logging.getLogger(__name__)

SCHEMA = """
CREATE TABLE IF NOT EXISTS sites (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  name TEXT NOT NULL UNIQUE,
  address TEXT NOT NULL DEFAULT '',
  active INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE IF NOT EXISTS rate_plans (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  name TEXT NOT NULL UNIQUE,
  peak_price REAL NOT NULL,
  flat_price REAL NOT NULL,
  valley_price REAL NOT NULL,
  active INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE IF NOT EXISTS piles (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  site_id INTEGER NOT NULL,
  name TEXT NOT NULL,
  rated_kw REAL NOT NULL DEFAULT 7,
  status TEXT NOT NULL DEFAULT '空闲',
  rate_plan_id INTEGER NOT NULL,
  UNIQUE(site_id, name),
  FOREIGN KEY(site_id) REFERENCES sites(id),
  FOREIGN KEY(rate_plan_id) REFERENCES rate_plans(id)
);
CREATE TABLE IF NOT EXISTS guns (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  pile_id INTEGER NOT NULL,
  name TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT '可用',
  UNIQUE(pile_id, name),
  FOREIGN KEY(pile_id) REFERENCES piles(id)
);
CREATE TABLE IF NOT EXISTS sessions (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  slip_no TEXT NOT NULL UNIQUE,
  site_id INTEGER NOT NULL,
  pile_id INTEGER NOT NULL,
  gun_id INTEGER NOT NULL,
  rate_plan_id INTEGER NOT NULL,
  business_day TEXT NOT NULL,
  status TEXT NOT NULL,
  kwh REAL NOT NULL DEFAULT 0,
  amount REAL NOT NULL DEFAULT 0,
  price_locked REAL,
  started_at TEXT NOT NULL,
  closed_at TEXT,
  released_at TEXT,
  note TEXT NOT NULL DEFAULT '',
  FOREIGN KEY(site_id) REFERENCES sites(id),
  FOREIGN KEY(pile_id) REFERENCES piles(id),
  FOREIGN KEY(gun_id) REFERENCES guns(id),
  FOREIGN KEY(rate_plan_id) REFERENCES rate_plans(id)
);
CREATE TABLE IF NOT EXISTS pulses (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  session_id INTEGER NOT NULL,
  delta_kwh REAL NOT NULL,
  power_kw REAL NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL,
  note TEXT NOT NULL DEFAULT '',
  FOREIGN KEY(session_id) REFERENCES sessions(id)
);
CREATE TABLE IF NOT EXISTS session_events (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  session_id INTEGER NOT NULL,
  action TEXT NOT NULL,
  created_at TEXT NOT NULL,
  note TEXT NOT NULL DEFAULT '',
  FOREIGN KEY(session_id) REFERENCES sessions(id)
);
CREATE TABLE IF NOT EXISTS occupancy_events (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  session_id INTEGER NOT NULL,
  pile_id INTEGER NOT NULL,
  gun_id INTEGER NOT NULL,
  kind TEXT NOT NULL,
  created_at TEXT NOT NULL,
  note TEXT NOT NULL DEFAULT '',
  FOREIGN KEY(session_id) REFERENCES sessions(id),
  FOREIGN KEY(pile_id) REFERENCES piles(id),
  FOREIGN KEY(gun_id) REFERENCES guns(id)
);
CREATE TABLE IF NOT EXISTS day_seals (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  business_day TEXT NOT NULL UNIQUE,
  sealed INTEGER NOT NULL DEFAULT 0,
  sealed_at TEXT,
  note TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS settings (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL
);
"""

# 一个桩位/枪线在同一时刻只允许一笔未腾空会话占住。
# 这是「先关账、后拔枪确认腾空」时序的存储层硬约束，防并发双占。
OPEN_OCCUPANCY_INDEXES = """
CREATE UNIQUE INDEX IF NOT EXISTS ux_sessions_open_pile
  ON sessions(pile_id) WHERE released_at IS NULL;
CREATE UNIQUE INDEX IF NOT EXISTS ux_sessions_open_gun
  ON sessions(gun_id) WHERE released_at IS NULL;
"""

MIGRATION_KEY = "占用时序迁移版本"


def connect() -> sqlite3.Connection:
    conn = sqlite3.connect(config.DB_PATH, check_same_thread=False, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=10000")
    return conn


@contextmanager
def db():
    """只读或单次提交；异常回滚，绝不留下半截写。"""
    conn = connect()
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


@contextmanager
def tx():
    """写事务。BEGIN IMMEDIATE 让并发写串行，配合唯一索引杜绝双占。

    任何一步抛错都 ROLLBACK 到事务开始前快照。
    """
    conn = connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        yield conn
        conn.execute("COMMIT")
    except Exception:
        try:
            conn.execute("ROLLBACK")
        except sqlite3.Error:
            # BEGIN 本身失败（如取锁超时）时无事务可回滚，保留原始异常。
            pass
        raise
    finally:
        conn.close()


def _columns(conn, table: str) -> set[str]:
    return {r["name"] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()}


def _migrate(conn) -> None:
    """一次性把旧库迁到「拔枪确认后置腾空」模型。

    旧模型关账即腾空；迁移把已关账/已中断会话视为已腾空，避免历史单据
    永远占用。用 settings 版本键保证只执行一次。
    """
    cols = _columns(conn, "sessions")
    if "released_at" not in cols:
        conn.execute("ALTER TABLE sessions ADD COLUMN released_at TEXT")

    r = conn.execute("SELECT value FROM settings WHERE key=?", (MIGRATION_KEY,)).fetchone()
    if r and r["value"] == "1":
        return

    # 底座状态不再承载占用：占用完全由 sessions.released_at 派生。
    # 仅在迁移那一次清理旧模型的占用标记，之后绝不覆盖运营手置的故障/维护。
    conn.execute("UPDATE piles SET status='空闲' WHERE status='占用'")
    conn.execute("UPDATE guns SET status='可用' WHERE status='占用'")
    # 旧模型在关账/中断时就释放了桩枪，迁移按同一时刻补记腾空时间。
    conn.execute(
        """UPDATE sessions SET released_at=COALESCE(closed_at, started_at)
           WHERE released_at IS NULL AND status IN ('已关账','已中断')"""
    )
    conn.execute(
        "INSERT INTO settings(key,value) VALUES(?, '1') ON CONFLICT(key) DO UPDATE SET value='1'",
        (MIGRATION_KEY,),
    )


def init_db() -> None:
    conn = connect()
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.executescript(SCHEMA)
        _migrate(conn)
        try:
            conn.executescript(OPEN_OCCUPANCY_INDEXES)
        except sqlite3.IntegrityError:
            # 旧库若已存在双占脏数据，不静默篡改订单，只告警；代码层仍会拒绝双占。
            logger.warning("无法建立未腾空占用唯一索引：库内已有重复未腾空会话，请人工核对")
        conn.commit()
    finally:
        conn.close()


def row_dict(row: sqlite3.Row | None) -> dict | None:
    if row is None:
        return None
    return {k: row[k] for k in row.keys()}


def rows(rs) -> list[dict]:
    return [row_dict(r) for r in rs]
