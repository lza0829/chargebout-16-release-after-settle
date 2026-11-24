"""并发与半截回滚夹具。"""
from __future__ import annotations

import threading

import pytest

from conftest import (
    assert_synced,
    free_gun,
    free_pile,
    gun_of,
    open_one,
    overview,
    pile_of,
    pulse,
)


def test_double_open_same_pile_concurrently(app_client):
    """并发开会话抢同一桩：一胜一败，绝不双占。"""
    pile = free_pile(app_client)
    gun = free_gun(app_client, pile["id"])
    results = []

    def attempt():
        r = app_client.post("/api/开会话", json={"桩位编号": pile["id"], "枪线编号": gun["id"]})
        results.append(r.status_code)

    threads = [threading.Thread(target=attempt) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert results.count(200) == 1, results
    assert all(code in (409, 400) for code in results if code != 200), results

    p = pile_of(app_client, pile["id"])
    assert p["占用"]
    opens = [s for s in app_client.get("/api/会话").json() if s["pile_id"] == pile["id"] and s["占用"]]
    assert len(opens) == 1, opens

    ck = app_client.get("/api/会话/一致性").json()
    assert ck["一致"], ck


def test_concurrent_settle_and_open_does_not_double_occupy(app_client):
    """结算关账与再开充并发：桩未腾空前，再开充必须失败。"""
    sid, pid, gid = open_one(app_client)
    pulse(app_client, sid, 8)

    results = {}

    def settle():
        results["settle"] = app_client.post(f"/api/会话/{sid}/结算").status_code

    def reopen():
        results["reopen"] = app_client.post(
            "/api/开会话", json={"桩位编号": pid, "枪线编号": gid}
        ).status_code

    ts = [threading.Thread(target=settle), threading.Thread(target=reopen)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()

    assert results["settle"] == 200, results
    assert results["reopen"] != 200, results
    assert pile_of(app_client, pid)["占用未清"]
    ck = app_client.get("/api/会话/一致性").json()
    assert ck["一致"], ck


def test_concurrent_unplug_and_open_never_leave_dirty_idle(app_client):
    """拔枪腾空与再开充并发：腾空成功后新会话独占，旧会话不再占用。

    关键：不允许「腾空瞬间的空档被两笔新会话同时抢到」。
    """
    sid, pid, gid = open_one(app_client)
    pulse(app_client, sid, 4)
    app_client.post(f"/api/会话/{sid}/结算")

    codes = []

    def unplug():
        codes.append(app_client.post(f"/api/会话/{sid}/拔枪确认", json={"备注": ""}).status_code)

    def reopen():
        codes.append(
            app_client.post("/api/开会话", json={"桩位编号": pid, "枪线编号": gid}).status_code
        )

    ts = [threading.Thread(target=unplug) for _ in range(3)]
    ts += [threading.Thread(target=reopen) for _ in range(5)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()

    # 陈旧会话已腾空；至多一笔新会话占住桩枪
    detail = app_client.get(f"/api/会话/{sid}").json()
    assert detail["占用"] is False

    pile = pile_of(app_client, pid)
    gun = gun_of(app_client, gid)
    assert pile["占用"] == gun["占用"]
    opens = [
        s
        for s in app_client.get("/api/会话").json()
        if s["pile_id"] == pid and s["占用"]
    ]
    assert len(opens) <= 1, opens
    ck = app_client.get("/api/会话/一致性").json()
    assert ck["一致"], ck


def test_settle_failure_rolls_back_to_pre_settle_snapshot(app_client, monkeypatch):
    """关账半截失败：状态、金额、占用事件整笔回滚，无脏空闲。"""
    sid, pid, gid = open_one(app_client)
    pulse(app_client, sid, 9)
    before = app_client.get(f"/api/会话/{sid}").json()

    from app.services import sessions as svc

    orig = svc._occupy_event

    def boom(conn, *args, **kwargs):
        if args and args[3] == "关账":
            raise RuntimeError("模拟关账写占用事件失败")
        return orig(conn, *args, **kwargs)

    monkeypatch.setattr(svc, "_occupy_event", boom)

    r = app_client.post(f"/api/会话/{sid}/结算")
    assert r.status_code == 500, r.text

    after = app_client.get(f"/api/会话/{sid}").json()
    assert after["status"] == "充电中"
    assert after["amount"] == before["amount"]
    assert after["closed_at"] is None
    assert after["占用"] is True

    # 占用事件无「关账」残留
    kinds = [e["kind"] for e in after["占用事件"]]
    assert "关账" not in kinds, kinds
    assert pile_of(app_client, pid)["占用"]


def test_unplug_failure_rolls_back_occupancy(app_client, monkeypatch):
    """拔枪确认半截失败：不会留下「腾空事件写了但 released_at 未写」的脏空闲。"""
    sid, pid, gid = open_one(app_client)
    pulse(app_client, sid, 2)
    app_client.post(f"/api/会话/{sid}/结算")

    from app.services import sessions as svc

    def boom(conn, *args, **kwargs):
        raise RuntimeError("模拟腾空写事件失败")

    monkeypatch.setattr(svc, "_occupy_event", boom)
    r = app_client.post(f"/api/会话/{sid}/拔枪确认", json={"备注": ""})
    assert r.status_code == 500, r.text

    detail = app_client.get(f"/api/会话/{sid}").json()
    assert detail["占用"] is True
    assert detail["腾空时间"] is None
    assert pile_of(app_client, pid)["占用未清"]
    assert gun_of(app_client, gid)["占用未清"]
    # 未腾空，不得可开充
    assert app_client.post("/api/开会话", json={"桩位编号": pid}).status_code == 409
