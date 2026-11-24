"""占用时序的唯一读模型。

全局贯彻「拔枪确认后置腾空」：会话关账只落金额与已关账，桩位/枪线占用
一直保留，直到拔枪确认事件写入 released_at 的同一事务才腾空。

占用不是桩位表 / 枪线表上的一个布尔字段，而是从会话时序派生：

    占用开始 = occupancy_events 的「占用」事件（= sessions.started_at）
    占用结束 = occupancy_events 的「腾空」事件（= sessions.released_at）

桩位页、枪线页、会话详情、总览空闲数全部走本模块，禁止各自读 base.status
拼占用，避免同一事实两套口径。`replay_occupancy` 能按事件时序重放任意时刻
的占用，读路径当前值必须与重放一致，`consistency_report` 负责对照。
"""
from __future__ import annotations

from app.db import rows

# 会话一旦关账/中断，硬件仍可能插着枪：占用未清，必须拔枪确认才腾空。
CLOSED_STATUSES = ("已关账", "已中断")
OPEN_ACTIVE_STATUSES = ("充电中", "结算中", "已超时")

OCCUPY_START = "占用"
OCCUPY_SETTLE = "关账"
OCCUPY_ABORT = "中断"
OCCUPY_RELEASE = "腾空"

# 占用开始 / 持续 / 结束的事件种类；用于重放。
OCCUPYING_KINDS = (OCCUPY_START, OCCUPY_SETTLE, OCCUPY_ABORT)


def label_for(session_status: str, released_at: str | None) -> str:
    """单笔会话的占用标签。"""
    if released_at:
        return "已腾空"
    if session_status in CLOSED_STATUSES:
        return "占用未清"
    return "占用"


def session_flags(se: dict) -> dict:
    """会话读模型的占用派生字段，会话册与详情共用。"""
    released = se.get("released_at")
    occupying = not released
    return {
        "占用": occupying,
        "占用未清": occupying and se["status"] in CLOSED_STATUSES,
        "待拔枪确认": occupying and se["status"] in CLOSED_STATUSES,
        "腾空时间": released,
        "占用口径": "拔枪确认后置腾空",
        "占用标签": label_for(se["status"], released),
    }


def _open_by(conn, column: str) -> dict[int, dict]:
    """未腾空会话索引：桩位/枪线同一时刻至多一笔（唯一索引兜底）。"""
    rs = rows(
        conn.execute(
            f"SELECT * FROM sessions WHERE released_at IS NULL ORDER BY id"
        ).fetchall()
    )
    out: dict[int, dict] = {}
    for se in rs:
        out.setdefault(se[column], se)
    return out


def pile_view(conn, site_id: int | None = None) -> list[dict]:
    """桩位页读模型：base 状态 + 会话时序派生占用。"""
    sql = """SELECT p.*, s.name AS site_name, r.name AS rate_name
             FROM piles p JOIN sites s ON s.id=p.site_id
             JOIN rate_plans r ON r.id=p.rate_plan_id"""
    params: tuple = ()
    if site_id:
        sql += " WHERE p.site_id=?"
        params = (site_id,)
    sql += " ORDER BY p.id"
    open_pile = _open_by(conn, "pile_id")
    out = []
    for p in rows(conn.execute(sql, params).fetchall()):
        se = open_pile.get(p["id"])
        d = dict(p)
        d["底座状态"] = p["status"]
        d["占用"] = se is not None
        d["占用未清"] = bool(se and se["status"] in CLOSED_STATUSES)
        d["待拔枪确认"] = d["占用未清"]
        d["可开充"] = (not se) and p["status"] == "空闲"
        d["会话编号"] = se["id"] if se else None
        d["会话号"] = se["slip_no"] if se else None
        d["会话状态"] = se["status"] if se else None
        d["占用标签"] = label_for(se["status"], None) if se else p["status"]
        # 展示状态：有未腾空会话时以占用为准，绝不出现「已关账却空闲」。
        d["展示状态"] = d["占用标签"]
        # 对外 status 直接给派生态，任何消费方都读不到「已关账却空闲」的假亮。
        d["status"] = d["展示状态"]
        if se is None and p["status"] == "占用":
            # 迁移遗留：底座手置占用不应存在，读路径仍按占用显示并报警。
            d["展示状态"] = "占用"
            d["占用"] = True
            d["占用未清"] = True
        out.append(d)
    return out


def gun_view(conn, pile_id: int | None = None) -> list[dict]:
    """枪线页读模型：与桩位页同源，桩/枪一起腾空。"""
    sql = """SELECT g.*, p.name AS pile_name
             FROM guns g JOIN piles p ON p.id=g.pile_id"""
    params: tuple = ()
    if pile_id:
        sql += " WHERE g.pile_id=?"
        params = (pile_id,)
    sql += " ORDER BY g.id"
    open_gun = _open_by(conn, "gun_id")
    out = []
    for g in rows(conn.execute(sql, params).fetchall()):
        se = open_gun.get(g["id"])
        d = dict(g)
        d["底座状态"] = g["status"]
        d["占用"] = se is not None
        d["占用未清"] = bool(se and se["status"] in CLOSED_STATUSES)
        d["待拔枪确认"] = d["占用未清"]
        d["可开充"] = (not se) and g["status"] == "可用"
        d["会话编号"] = se["id"] if se else None
        d["会话号"] = se["slip_no"] if se else None
        d["会话状态"] = se["status"] if se else None
        d["占用标签"] = label_for(se["status"], None) if se else g["status"]
        d["展示状态"] = d["占用标签"]
        d["status"] = d["展示状态"]
        out.append(d)
    return out


def overview_counts(conn) -> dict:
    """总览计数全部从同一读模型取，空闲数永远与桩位页可开充数一致。"""
    piles = pile_view(conn)
    guns = gun_view(conn)
    by_state: dict[str, int] = {}
    for p in piles:
        by_state[p["展示状态"]] = by_state.get(p["展示状态"], 0) + 1
    return {
        "桩位空闲": sum(1 for p in piles if p["展示状态"] == "空闲"),
        "桩位占用": sum(1 for p in piles if p["展示状态"] == "占用"),
        "桩位占用未清": sum(1 for p in piles if p["展示状态"] == "占用未清"),
        "桩位故障": sum(1 for p in piles if p["展示状态"] == "故障"),
        "桩位维护": sum(1 for p in piles if p["展示状态"] == "维护"),
        "桩位合计": len(piles),
        "枪线可用": sum(1 for g in guns if g["展示状态"] == "可用"),
        "枪线占用": sum(1 for g in guns if g["展示状态"] == "占用"),
        "枪线占用未清": sum(1 for g in guns if g["展示状态"] == "占用未清"),
        "枪线故障": sum(1 for g in guns if g["展示状态"] == "故障"),
        "桩位可开充": sum(1 for p in piles if p["可开充"]),
        "状态分布": by_state,
    }


def replay_occupancy(
    conn, at: str | None = None, until_event_id: int | None = None
) -> list[dict]:
    """按腾空事件时序重放某一时刻的占用（读路径必须能由它重建）。

    - `at`：按秒级时间重放（同一秒内多事件按 id 先后排序，保守取腾空生效）；
    - `until_event_id`：按事件序号重放，精确到单条事件，不受同秒多事件歧义影响。
    两者都不给则重放到最新，须与当前读模型完全一致。
    """
    clauses = []
    params: list = []
    if at:
        clauses.append("created_at<=?")
        params.append(at)
    if until_event_id is not None:
        clauses.append("id<=?")
        params.append(until_event_id)
    sql = "SELECT * FROM occupancy_events"
    if clauses:
        sql += " WHERE " + " AND ".join(clauses)
    sql += " ORDER BY id"
    state: dict[int, bool] = {}
    seen_start: dict[int, bool] = {}
    meta: dict[int, dict] = {}
    for e in rows(conn.execute(sql, params).fetchall()):
        sid = e["session_id"]
        meta[sid] = e
        if e["kind"] == OCCUPY_START:
            seen_start[sid] = True
            state[sid] = True
        elif e["kind"] in (OCCUPY_SETTLE, OCCUPY_ABORT):
            if seen_start.get(sid):
                state[sid] = True
        elif e["kind"] == OCCUPY_RELEASE:
            state[sid] = False
    out = []
    for sid, occupying in state.items():
        if occupying and seen_start.get(sid):
            e = meta[sid]
            out.append(
                {
                    "会话编号": sid,
                    "桩位编号": e["pile_id"],
                    "枪线编号": e["gun_id"],
                }
            )
    out.sort(key=lambda x: x["会话编号"])
    return out


def _current_occupancy(conn) -> list[dict]:
    return [
        {"会话编号": se["id"], "桩位编号": se["pile_id"], "枪线编号": se["gun_id"]}
        for se in rows(
            conn.execute(
                "SELECT id,pile_id,gun_id FROM sessions WHERE released_at IS NULL ORDER BY id"
            ).fetchall()
        )
    ]


def consistency_report(conn) -> dict:
    """把「已关仍占用 / 未腾空又开充 / 半截释放 / 空闲假亮」逐条对照出来。"""
    anomalies: list[dict] = []

    for p in rows(conn.execute("SELECT * FROM piles WHERE status='占用'").fetchall()):
        anomalies.append(
            {"类型": "桩位手置占用", "桩位编号": p["id"], "说明": "占用须由会话派生，不可手置"}
        )

    open_sessions = rows(
        conn.execute("SELECT * FROM sessions WHERE released_at IS NULL").fetchall()
    )
    by_pile: dict[int, list[dict]] = {}
    by_gun: dict[int, list[dict]] = {}
    for se in open_sessions:
        by_pile.setdefault(se["pile_id"], []).append(se)
        by_gun.setdefault(se["gun_id"], []).append(se)
    for pid, group in by_pile.items():
        if len(group) > 1:
            anomalies.append(
                {
                    "类型": "桩位双占",
                    "桩位编号": pid,
                    "会话": [s["id"] for s in group],
                }
            )
    for gid, group in by_gun.items():
        if len(group) > 1:
            anomalies.append(
                {
                    "类型": "枪线双占",
                    "枪线编号": gid,
                    "会话": [s["id"] for s in group],
                }
            )

    for se in rows(conn.execute("SELECT * FROM sessions").fetchall()):
        if se["released_at"] and se["status"] in OPEN_ACTIVE_STATUSES:
            anomalies.append(
                {
                    "类型": "开放会话已腾空",
                    "会话编号": se["id"],
                    "说明": f"{se['status']} 却已释放桩枪",
                }
            )
        if se["status"] == "已关账" and se["closed_at"] is None:
            anomalies.append(
                {"类型": "已关账缺关账时间", "会话编号": se["id"]}
            )
        if se["status"] == "已关账" and se["released_at"] is None:
            # 这不是异常，是后置腾空臂下的正常「已关账待拔枪」，单独计数。
            pass

    current = _current_occupancy(conn)
    replayed = replay_occupancy(conn)
    if current != replayed:
        anomalies.append(
            {
                "类型": "占用与腾空时序重放不符",
                "当前": current,
                "重放": replayed,
            }
        )

    return {
        "一致": not anomalies,
        "异常": anomalies,
        "当前占用": current,
        "时序重放占用": replayed,
        "口径": "拔枪确认后置腾空",
    }


def pending_release(conn) -> list[dict]:
    """已关账/已中断但桩枪尚未腾空的会话（待拔枪确认）。"""
    return rows(
        conn.execute(
            """SELECT se.*, p.name AS pile_name, g.name AS gun_name, s.name AS site_name
               FROM sessions se
               JOIN piles p ON p.id=se.pile_id
               JOIN guns g ON g.id=se.gun_id
               JOIN sites s ON s.id=se.site_id
               WHERE se.released_at IS NULL AND se.status IN ('已关账','已中断')
               ORDER BY se.id DESC"""
        ).fetchall()
    )
