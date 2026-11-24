from __future__ import annotations

import sqlite3

from fastapi import HTTPException

from app.services import occupancy
from app.services.core import current_band_price, get_setting, is_day_sealed, now_str, today


def _next_slip(conn) -> str:
    n = int(get_setting(conn, "会话序号", "1000")) + 1
    conn.execute(
        "INSERT INTO settings(key,value) VALUES('会话序号',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (str(n),),
    )
    return f"充{n}"


def _occupy_event(conn, session_id: int, pile_id: int, gun_id: int, kind: str, ts: str, note: str = "") -> None:
    conn.execute(
        """INSERT INTO occupancy_events(session_id,pile_id,gun_id,kind,created_at,note)
           VALUES(?,?,?,?,?,?)""",
        (session_id, pile_id, gun_id, kind, ts, note),
    )


def _session_event(conn, session_id: int, action: str, ts: str, note: str = "") -> None:
    conn.execute(
        "INSERT INTO session_events(session_id,action,created_at,note) VALUES(?,?,?,?)",
        (session_id, action, ts, note),
    )


def _unreleased_for_pile(conn, pile_id: int):
    return conn.execute(
        "SELECT * FROM sessions WHERE pile_id=? AND released_at IS NULL LIMIT 1",
        (pile_id,),
    ).fetchone()


def _unreleased_for_gun(conn, gun_id: int):
    return conn.execute(
        "SELECT * FROM sessions WHERE gun_id=? AND released_at IS NULL LIMIT 1",
        (gun_id,),
    ).fetchone()


def open_session(conn, pile_id: int, gun_id: int | None = None) -> dict:
    if is_day_sealed(conn):
        raise HTTPException(400, detail="充日已封印，禁止新开充")
    pile = conn.execute("SELECT * FROM piles WHERE id=?", (pile_id,)).fetchone()
    if not pile:
        raise HTTPException(404, detail="桩位不存在")
    # 占用由会话时序派生：只认「未腾空会话」，不信任 piles.status 的旧布尔。
    busy = _unreleased_for_pile(conn, pile_id)
    if busy:
        label = occupancy.label_for(busy["status"], busy["released_at"])
        raise HTTPException(
            409,
            detail=f"桩位仍被会话 {busy['slip_no']} 占用（{label}），须先拔枪确认腾空",
        )
    if pile["status"] != "空闲":
        raise HTTPException(400, detail=f"桩位当前为{pile['status']}，不可开充")
    site = conn.execute("SELECT * FROM sites WHERE id=?", (pile["site_id"],)).fetchone()
    if not site or not site["active"]:
        raise HTTPException(400, detail="站点未启用")

    if gun_id is None:
        gun = conn.execute(
            "SELECT * FROM guns WHERE pile_id=? AND status='可用' ORDER BY id LIMIT 1",
            (pile_id,),
        ).fetchone()
    else:
        gun = conn.execute(
            "SELECT * FROM guns WHERE id=? AND pile_id=?", (gun_id, pile_id)
        ).fetchone()
    if not gun:
        raise HTTPException(400, detail="无可用枪线")
    busy_gun = _unreleased_for_gun(conn, gun["id"])
    if busy_gun:
        raise HTTPException(
            409,
            detail=f"枪线仍被会话 {busy_gun['slip_no']} 占用，须先拔枪确认腾空",
        )
    if gun["status"] != "可用":
        raise HTTPException(400, detail=f"枪线当前为{gun['status']}")

    price = current_band_price(conn, pile["rate_plan_id"])
    slip = _next_slip(conn)
    ts = now_str()
    try:
        cur = conn.execute(
            """INSERT INTO sessions(slip_no,site_id,pile_id,gun_id,rate_plan_id,business_day,status,kwh,amount,price_locked,started_at,note)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                slip,
                pile["site_id"],
                pile_id,
                gun["id"],
                pile["rate_plan_id"],
                today(),
                "充电中",
                0,
                0,
                price,
                ts,
                "",
            ),
        )
    except sqlite3.IntegrityError as e:
        # 唯一索引兜住并发：同一桩/枪已有未腾空会话。
        raise HTTPException(409, detail="桩位或枪线已被并发占用，请刷新后重试") from e
    sid = cur.lastrowid
    _session_event(conn, sid, "开会话", ts, f"锁定电价{price}")
    _occupy_event(conn, sid, pile_id, gun["id"], occupancy.OCCUPY_START, ts, "占用开始")
    return {
        "会话号": slip,
        "会话编号": sid,
        "锁定电价": price,
        "状态": "充电中",
        "占用": "占用",
        "口径": "拔枪确认后置腾空",
    }


def add_pulse(conn, session_id: int, delta_kwh: float, power_kw: float = 0) -> dict:
    if delta_kwh <= 0:
        raise HTTPException(400, detail="脉冲电量须大于零")
    se = conn.execute("SELECT * FROM sessions WHERE id=?", (session_id,)).fetchone()
    if not se:
        raise HTTPException(404, detail="会话不存在")
    if se["status"] != "充电中":
        raise HTTPException(400, detail=f"会话状态为{se['status']}，不可记脉冲")
    ts = now_str()
    conn.execute(
        "INSERT INTO pulses(session_id,delta_kwh,power_kw,created_at) VALUES(?,?,?,?)",
        (session_id, delta_kwh, power_kw, ts),
    )
    new_kwh = float(se["kwh"]) + float(delta_kwh)
    price = float(se["price_locked"] or current_band_price(conn, se["rate_plan_id"]))
    amount = round(new_kwh * price, 2)
    conn.execute(
        "UPDATE sessions SET kwh=?, amount=? WHERE id=?",
        (round(new_kwh, 3), amount, session_id),
    )
    _session_event(conn, session_id, "脉冲", ts, f"+{delta_kwh}")
    return {"会话编号": session_id, "累计电量": round(new_kwh, 3), "预估金额": amount}


def settle(conn, session_id: int) -> dict:
    """关账：落已关账 + 金额，但桩/枪占用保持，等拔枪确认腾空。

    整个动作在一个事务内：会状态、金额、关账事件、关账占用事件同成败；
    任何一步失败由外层 tx 回滚到关账前快照，不会出现「金额落了状态没落」。
    """
    se = conn.execute("SELECT * FROM sessions WHERE id=?", (session_id,)).fetchone()
    if not se:
        raise HTTPException(404, detail="会话不存在")
    if se["status"] not in ("充电中", "结算中", "已超时"):
        raise HTTPException(400, detail=f"会话状态为{se['status']}，不可关账")
    if se["released_at"] is not None:
        raise HTTPException(409, detail="会话桩枪已腾空，不可再关账")
    ts = now_str()
    price = float(se["price_locked"] or current_band_price(conn, se["rate_plan_id"]))
    amount = round(float(se["kwh"]) * price, 2)
    conn.execute(
        "UPDATE sessions SET status='已关账', amount=?, closed_at=?, price_locked=? WHERE id=?",
        (amount, ts, price, session_id),
    )
    _session_event(conn, session_id, "结算关账", ts, f"金额{amount}")
    _occupy_event(
        conn, session_id, se["pile_id"], se["gun_id"], occupancy.OCCUPY_SETTLE, ts, "已关账，占用待拔枪确认"
    )
    return {
        "会话编号": session_id,
        "电量": float(se["kwh"]),
        "金额": amount,
        "状态": "已关账",
        "占用": "占用未清",
        "待拔枪确认": True,
        "口径": "拔枪确认后置腾空",
    }


def confirm_unplug(conn, session_id: int, note: str = "") -> dict:
    """拔枪确认：唯一允许腾空桩/枪的写口。同一事务写 released_at + 腾空事件。"""
    se = conn.execute("SELECT * FROM sessions WHERE id=?", (session_id,)).fetchone()
    if not se:
        raise HTTPException(404, detail="会话不存在")
    if se["status"] not in occupancy.CLOSED_STATUSES:
        raise HTTPException(
            400, detail=f"会话状态为{se['status']}，须先关账或中断才能拔枪确认"
        )
    if se["released_at"] is not None:
        return {
            "会话编号": session_id,
            "状态": se["status"],
            "占用": "已腾空",
            "腾空时间": se["released_at"],
            "重复": True,
        }
    ts = now_str()
    conn.execute(
        "UPDATE sessions SET released_at=? WHERE id=?", (ts, session_id)
    )
    _session_event(conn, session_id, "拔枪确认", ts, note or "桩枪腾空")
    _occupy_event(
        conn, session_id, se["pile_id"], se["gun_id"], occupancy.OCCUPY_RELEASE, ts, note or "桩枪腾空"
    )
    return {
        "会话编号": session_id,
        "状态": se["status"],
        "占用": "已腾空",
        "腾空时间": ts,
        "桩位编号": se["pile_id"],
        "枪线编号": se["gun_id"],
        "口径": "拔枪确认后置腾空",
    }


def mark_timeout(conn, session_id: int) -> dict:
    se = conn.execute("SELECT * FROM sessions WHERE id=?", (session_id,)).fetchone()
    if not se:
        raise HTTPException(404, detail="会话不存在")
    if se["status"] != "充电中":
        raise HTTPException(400, detail=f"会话状态为{se['status']}，不可标超时")
    minutes = int(get_setting(conn, "超时分钟", "120"))
    ts = now_str()
    conn.execute(
        "UPDATE sessions SET status='已超时', note=? WHERE id=?",
        (f"超过{minutes}分钟未关", session_id),
    )
    _session_event(conn, session_id, "超时", ts, f"阈值{minutes}分钟")
    return {"会话编号": session_id, "状态": "已超时", "占用": "占用"}


def abort_session(conn, session_id: int, reason: str = "急停") -> dict:
    """中断同属关账性质：只落终止与金额不动，占用等拔枪确认后腾空。"""
    se = conn.execute("SELECT * FROM sessions WHERE id=?", (session_id,)).fetchone()
    if not se:
        raise HTTPException(404, detail="会话不存在")
    if se["status"] not in ("充电中", "结算中", "已超时"):
        raise HTTPException(400, detail=f"会话状态为{se['status']}，不可中断")
    ts = now_str()
    # 中断残额另立需求：此处只落终止与关账时间，不结算金额。
    conn.execute(
        "UPDATE sessions SET status='已中断', closed_at=?, note=? WHERE id=?",
        (ts, reason, session_id),
    )
    _session_event(conn, session_id, "异常中断", ts, reason)
    _occupy_event(
        conn, session_id, se["pile_id"], se["gun_id"], occupancy.OCCUPY_ABORT, ts, "已中断，占用待拔枪确认"
    )
    return {
        "会话编号": session_id,
        "状态": "已中断",
        "原因": reason,
        "占用": "占用未清",
        "待拔枪确认": True,
    }


def set_pile_status(conn, pile_id: int, status: str) -> dict:
    if status not in ("空闲", "故障", "维护"):
        if status == "占用":
            raise HTTPException(400, detail="占用由会话时序派生，不可手置；请开会话占用")
        raise HTTPException(400, detail="非法桩位状态")
    pile = conn.execute("SELECT * FROM piles WHERE id=?", (pile_id,)).fetchone()
    if not pile:
        raise HTTPException(404, detail="桩位不存在")
    busy = _unreleased_for_pile(conn, pile_id)
    if busy:
        label = occupancy.label_for(busy["status"], busy["released_at"])
        raise HTTPException(
            409,
            detail=f"桩位仍被会话 {busy['slip_no']} 占用（{label}），不可改为{status}；请先拔枪确认腾空",
        )
    conn.execute("UPDATE piles SET status=? WHERE id=?", (status, pile_id))
    return {"桩位编号": pile_id, "状态": status, "占用": False}
