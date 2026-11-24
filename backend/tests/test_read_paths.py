"""读路径口径与跨页对照夹具。

覆盖：空闲态与会话已关拧花、枪线页已归还桩位页仍占用、总览空闲数与占用列对不上。
"""
from __future__ import annotations

import pytest

from conftest import (
    assert_synced,
    gun_of,
    open_one,
    overview,
    pile_of,
    pulse,
)


def test_idle_state_cannot_contradict_closed_session(app_client):
    """空闲假亮：桩位页说空闲但会话未腾空，读路径必须禁止。"""
    sid, pid, gid = open_one(app_client)
    pulse(app_client, sid, 6)
    app_client.post(f"/api/会话/{sid}/结算")

    pile = pile_of(app_client, pid)
    # 已关账但未腾空 -> 展示占用未清，绝不是空闲
    assert pile["展示状态"] == "占用未清"
    assert pile["可开充"] is False

    # 直接手置空闲必须被拒
    r = app_client.post(f"/api/桩位/{pid}/状态", json={"状态": "空闲"})
    assert r.status_code == 409, r.text
    assert "拔枪确认" in r.json()["detail"]

    # 手置占用亦不允许（占用只能由会话派生）
    r2 = app_client.post(f"/api/桩位/{pid}/状态", json={"状态": "占用"})
    assert r2.status_code == 400, r2.text


def test_gun_page_and_pile_page_never_split(app_client):
    """枪线页已归还但桩位页仍占用之类的分裂，不可出现。"""
    sid, pid, gid = open_one(app_client)
    pulse(app_client, sid, 3)

    for stage in ("充电中", "已关账", "已腾空"):
        if stage == "已关账":
            app_client.post(f"/api/会话/{sid}/结算")
        if stage == "已腾空":
            app_client.post(f"/api/会话/{sid}/拔枪确认", json={"备注": ""})
        detail, pile, gun, _ = assert_synced(app_client, sid)
        assert pile["占用"] == gun["占用"]
        assert pile["占用未清"] == gun["占用未清"]
        assert pile["会话编号"] == gun["会话编号"]


def test_overview_counts_match_occupancy_columns(app_client):
    """总览空闲数、占用列、待拔枪数三处必须对齐。"""
    sid1, pid1, gid1 = open_one(app_client)
    sid2, pid2, gid2 = open_one(app_client)
    pulse(app_client, sid1, 2)
    pulse(app_client, sid2, 2)
    app_client.post(f"/api/会话/{sid1}/结算")  # 关账未腾空

    piles = app_client.get("/api/桩位").json()
    guns = app_client.get("/api/枪线").json()
    ov = overview(app_client)

    assert ov["桩位空闲"] == sum(1 for p in piles if p["展示状态"] == "空闲")
    assert ov["桩位占用"] == sum(1 for p in piles if p["展示状态"] == "占用")
    assert ov["桩位占用未清"] == sum(1 for p in piles if p["展示状态"] == "占用未清")
    assert ov["枪线占用"] == sum(1 for g in guns if g["展示状态"] == "占用")
    assert ov["枪线占用未清"] == sum(1 for g in guns if g["展示状态"] == "占用未清")
    assert ov["桩位可开充"] == sum(1 for p in piles if p["可开充"])
    # 已关账桶 = 会话已关账数，关账未腾空仍计入待拔枪确认
    assert ov["已关账"] == 1
    assert ov["待拔枪确认"] == 1
    assert ov["桩位占用未清"] == 1


def test_consistency_reports_dirty_row(app_client):
    """人为注入脏数据（未腾空却已释放等），一致性检查必须能抓到。"""
    sid, pid, gid = open_one(app_client)
    pulse(app_client, sid, 1)
    app_client.post(f"/api/会话/{sid}/结算")

    # 直接改库制造「已关账又被置空闲」的假亮（绕过服务层）
    from app.db import db

    with db() as conn:
        conn.execute("UPDATE piles SET status='占用' WHERE id=?", (pid,))

    ck = app_client.get("/api/会话/一致性").json()
    assert ck["一致"] is False
    types = {a["类型"] for a in ck["异常"]}
    assert "桩位手置占用" in types, ck


def test_replay_matches_current_at_multiple_timestamps(app_client):
    """读路径占用可由选定腾空时序重放：任意时刻重放与当时读模型一致。"""
    sid, pid, gid = open_one(app_client)
    pulse(app_client, sid, 5)

    detail_before = app_client.get(f"/api/会话/{sid}").json()
    start_event_id = detail_before["占用事件"][-1]["id"]
    replayed_open = app_client.get("/api/会话/一致性").json()
    assert any(o["会话编号"] == sid for o in replayed_open["时序重放占用"])

    app_client.post(f"/api/会话/{sid}/结算")
    settled = app_client.get(f"/api/会话/{sid}").json()
    settle_event_id = settled["占用事件"][-1]["id"]
    assert settled["占用"] is True

    app_client.post(f"/api/会话/{sid}/拔枪确认", json={"备注": ""})
    released = app_client.get(f"/api/会话/{sid}").json()
    release_event_id = released["占用事件"][-1]["id"]
    assert released["占用"] is False

    from app.db import db
    from app.services import occupancy

    with db() as conn:
        at_open = occupancy.replay_occupancy(conn, until_event_id=start_event_id)
        at_settle = occupancy.replay_occupancy(conn, until_event_id=settle_event_id)
        at_release = occupancy.replay_occupancy(conn, until_event_id=release_event_id)
        at_now = occupancy.replay_occupancy(conn)
    # 开充即占用；关账仍未腾空；拔枪确认同刻腾空；重放最新与当前一致
    assert any(o["会话编号"] == sid for o in at_open)
    assert any(o["会话编号"] == sid for o in at_settle)
    assert not any(o["会话编号"] == sid for o in at_release)
    assert not any(o["会话编号"] == sid for o in at_now)
    ck_final = app_client.get("/api/会话/一致性").json()
    assert ck_final["当前占用"] == at_now
    assert ck_final["一致"], ck_final
