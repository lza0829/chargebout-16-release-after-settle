from __future__ import annotations
from datetime import datetime

from app.db import row_dict, rows
from app.services import occupancy


def now_str() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def today() -> str:
    return datetime.now().strftime("%Y-%m-%d")


def get_setting(conn, key: str, default: str = "") -> str:
    r = conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    return r["value"] if r else default


def set_setting(conn, key: str, value: str) -> None:
    conn.execute(
        "INSERT INTO settings(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, value),
    )


def current_band_price(conn, rate_plan_id: int) -> float:
    plan = conn.execute("SELECT * FROM rate_plans WHERE id=?", (rate_plan_id,)).fetchone()
    if not plan:
        return 0.0
    hour = datetime.now().hour
    if 8 <= hour < 11 or 18 <= hour < 21:
        return float(plan["peak_price"])
    if 23 <= hour or hour < 7:
        return float(plan["valley_price"])
    return float(plan["flat_price"])


def is_day_sealed(conn, day: str | None = None) -> bool:
    d = day or today()
    r = conn.execute("SELECT sealed FROM day_seals WHERE business_day=?", (d,)).fetchone()
    return bool(r and r["sealed"])


def overview(conn) -> dict:
    occ = occupancy.overview_counts(conn)
    sess = rows(conn.execute("SELECT status, COUNT(*) AS n FROM sessions GROUP BY status").fetchall())
    sess_map = {s["status"]: s["n"] for s in sess}
    open_kwh = conn.execute(
        "SELECT COALESCE(SUM(kwh),0) AS k FROM sessions WHERE status IN ('充电中','结算中')"
    ).fetchone()["k"]
    today_amount = conn.execute(
        "SELECT COALESCE(SUM(amount),0) AS a FROM sessions WHERE business_day=? AND status='已关账'",
        (today(),),
    ).fetchone()["a"]
    return {
        "桩位空闲": occ["桩位空闲"],
        "桩位占用": occ["桩位占用"],
        "桩位占用未清": occ["桩位占用未清"],
        "桩位故障": occ["桩位故障"],
        "桩位维护": occ["桩位维护"],
        "桩位可开充": occ["桩位可开充"],
        "枪线可用": occ["枪线可用"],
        "枪线占用": occ["枪线占用"],
        "枪线占用未清": occ["枪线占用未清"],
        "充电中": sess_map.get("充电中", 0),
        "结算中": sess_map.get("结算中", 0),
        "已关账": sess_map.get("已关账", 0),
        "已超时": sess_map.get("已超时", 0),
        "已中断": sess_map.get("已中断", 0),
        "待拔枪确认": len(occupancy.pending_release(conn)),
        "在充电量": round(float(open_kwh), 3),
        "本日关账金额": round(float(today_amount), 2),
        "营业日": today(),
        "充日已封印": is_day_sealed(conn),
        "口径": "拔枪确认后置腾空",
    }


def list_sites(conn):
    return rows(conn.execute("SELECT * FROM sites ORDER BY id").fetchall())


def list_piles(conn, site_id: int | None = None):
    return occupancy.pile_view(conn, site_id)


def list_guns(conn, pile_id: int | None = None):
    return occupancy.gun_view(conn, pile_id)


def list_rates(conn):
    return rows(conn.execute("SELECT * FROM rate_plans ORDER BY id").fetchall())


def _enrich_session(se: dict) -> dict:
    d = dict(se)
    d.update(occupancy.session_flags(se))
    return d


def list_sessions(conn, status: str | None = None):
    if status:
        rs = conn.execute(
            """SELECT se.*, p.name AS pile_name, g.name AS gun_name, s.name AS site_name
               FROM sessions se
               JOIN piles p ON p.id=se.pile_id
               JOIN guns g ON g.id=se.gun_id
               JOIN sites s ON s.id=se.site_id
               WHERE se.status=? ORDER BY se.id DESC""",
            (status,),
        ).fetchall()
    else:
        rs = conn.execute(
            """SELECT se.*, p.name AS pile_name, g.name AS gun_name, s.name AS site_name
               FROM sessions se
               JOIN piles p ON p.id=se.pile_id
               JOIN guns g ON g.id=se.gun_id
               JOIN sites s ON s.id=se.site_id
               ORDER BY se.id DESC LIMIT 200"""
        ).fetchall()
    return [_enrich_session(s) for s in rows(rs)]


def session_detail(conn, session_id: int):
    se = conn.execute(
        """SELECT se.*, p.name AS pile_name, g.name AS gun_name, s.name AS site_name, r.name AS rate_name
           FROM sessions se
           JOIN piles p ON p.id=se.pile_id
           JOIN guns g ON g.id=se.gun_id
           JOIN sites s ON s.id=se.site_id
           JOIN rate_plans r ON r.id=se.rate_plan_id
           WHERE se.id=?""",
        (session_id,),
    ).fetchone()
    if not se:
        return None
    pulses = rows(
        conn.execute(
            "SELECT * FROM pulses WHERE session_id=? ORDER BY id", (session_id,)
        ).fetchall()
    )
    events = rows(
        conn.execute(
            "SELECT * FROM session_events WHERE session_id=? ORDER BY id", (session_id,)
        ).fetchall()
    )
    occ_events = rows(
        conn.execute(
            "SELECT * FROM occupancy_events WHERE session_id=? ORDER BY id", (session_id,)
        ).fetchall()
    )
    d = row_dict(se)
    d.update(occupancy.session_flags(d))
    d["脉冲"] = pulses
    d["事件"] = events
    d["占用事件"] = occ_events
    return d


def pending_release(conn):
    return occupancy.pending_release(conn)


def consistency(conn):
    return occupancy.consistency_report(conn)
