"""结算关账后腾空时序夹具。

全局口径：拔枪确认后置腾空。
关账 → 已关账 + 金额落定，桩/枪保持占用未清 → 拔枪确认同事务腾空。
"""
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


def test_settle_keeps_occupancy_until_unplug(app_client):
    """关账后仍占枪：金额落定但桩枪不腾空，读数标记占用未清。"""
    sid, pid, gid = open_one(app_client)
    pulse(app_client, sid, 12.5)

    r = app_client.post(f"/api/会话/{sid}/结算")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["状态"] == "已关账"
    assert body["占用"] == "占用未清"
    assert body["待拔枪确认"] is True

    detail, pile, gun, ov = assert_synced(app_client, sid)
    assert detail["status"] == "已关账"
    assert detail["占用"] is True
    assert detail["占用未清"] is True
    assert pile["展示状态"] == "占用未清"
    assert gun["展示状态"] == "占用未清"
    assert ov["待拔枪确认"] == 1

    # 关账后不腾空，未腾空前不可再开充
    r2 = app_client.post("/api/开会话", json={"桩位编号": pid, "枪线编号": gid})
    assert r2.status_code == 409, r2.text
    assert "拔枪确认" in r2.json()["detail"]


def test_unplug_releases_both_pile_and_gun(app_client):
    """拔枪确认后桩枪同刻腾空，桩位页/枪线页/总览同刷。"""
    sid, pid, gid = open_one(app_client)
    pulse(app_client, sid, 5.0)
    app_client.post(f"/api/会话/{sid}/结算")

    r = app_client.post(f"/api/会话/{sid}/拔枪确认", json={"备注": "拔枪"})
    assert r.status_code == 200, r.text
    assert r.json()["占用"] == "已腾空"

    detail, pile, gun, ov = assert_synced(app_client, sid)
    assert detail["腾空时间"]
    assert detail["占用"] is False
    assert pile["展示状态"] == "空闲"
    assert gun["展示状态"] == "可用"
    assert ov["待拔枪确认"] == 0

    # 腾空后可再开充
    r2 = app_client.post("/api/开会话", json={"桩位编号": pid, "枪线编号": gid})
    assert r2.status_code == 200, r2.text


def test_amount_fixed_at_settle_not_changed_by_unplug(app_client):
    sid, _, _ = open_one(app_client)
    pulse(app_client, sid, 10.0)
    settled = app_client.post(f"/api/会话/{sid}/结算").json()
    amount = settled["金额"]

    # 拔枪确认不可再改金额，也不可再记脉冲
    app_client.post(f"/api/会话/{sid}/拔枪确认", json={"备注": ""})
    detail = app_client.get(f"/api/会话/{sid}").json()
    assert detail["amount"] == amount
    r = app_client.post(f"/api/会话/{sid}/脉冲", json={"电量": 1.0})
    assert r.status_code == 400


def test_abort_also_defers_release(app_client):
    """中断同属关账性质：落金额但占用仍等拔枪确认。"""
    sid, pid, gid = open_one(app_client)
    pulse(app_client, sid, 3.0)
    r = app_client.post(f"/api/会话/{sid}/中断", json={"原因": "急停"})
    assert r.status_code == 200, r.text
    assert r.json()["占用"] == "占用未清"

    detail, pile, gun, _ = assert_synced(app_client, sid)
    assert detail["status"] == "已中断"
    assert pile["占用未清"] and gun["占用未清"]
    assert app_client.post("/api/开会话", json={"桩位编号": pid}).status_code == 409

    app_client.post(f"/api/会话/{sid}/拔枪确认", json={"备注": ""})
    assert pile_of(app_client, pid)["展示状态"] == "空闲"
    assert gun_of(app_client, gid)["展示状态"] == "可用"
