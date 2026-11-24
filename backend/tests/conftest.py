"""夹具共用的测试应用与数据库。

每个测试用独立临时库，避免夹具互相污染；所有断言都走 HTTP 读路径，
保证校验的是页面真正看到的口径，而不是数据库直读。
"""
from __future__ import annotations

import os
import sys
import tempfile
import uuid
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


@pytest.fixture()
def app_client(monkeypatch):
    tmpdir = tempfile.mkdtemp(prefix="chargebout-")
    db_path = Path(tmpdir) / f"{uuid.uuid4().hex}.db"
    monkeypatch.setenv("CHARGEBOUT_DB", str(db_path))

    # 清掉上一测试可能导入过的模块，让 config.DB_PATH 重新求值。
    for mod in [m for m in list(sys.modules) if m == "app" or m.startswith("app.")]:
        del sys.modules[mod]

    from fastapi.testclient import TestClient

    from app.main import app

    with TestClient(app, raise_server_exceptions=False) as client:
        yield client


@pytest.fixture()
def site_and_piles(app_client):
    """默认种子：河西 4 桩（河甲三号/四号双枪），城北 3 桩。"""
    piles = app_client.get("/api/桩位").json()
    guns = app_client.get("/api/枪线").json()
    return {"piles": piles, "guns": guns}


def free_pile(client, exclude=()):
    for p in client.get("/api/桩位").json():
        if p["可开充"] and p["id"] not in exclude:
            return p
    raise AssertionError("找不到可开充桩位")


def free_gun(client, pile_id):
    for g in client.get("/api/枪线", params={"桩位编号": pile_id}).json():
        if g["可开充"]:
            return g
    raise AssertionError("找不到可开充枪线")


def open_one(client, pile=None):
    pile = pile or free_pile(client)
    gun = free_gun(client, pile["id"])
    r = client.post("/api/开会话", json={"桩位编号": pile["id"], "枪线编号": gun["id"]})
    assert r.status_code == 200, r.text
    return r.json()["会话编号"], pile["id"], gun["id"]


def pulse(client, sid, kwh=10.0):
    r = client.post(f"/api/会话/{sid}/脉冲", json={"电量": kwh, "功率": 7})
    assert r.status_code == 200, r.text
    return r.json()


def pile_of(client, pile_id):
    for p in client.get("/api/桩位").json():
        if p["id"] == pile_id:
            return p
    raise AssertionError("桩位不存在")


def gun_of(client, gun_id):
    for g in client.get("/api/枪线").json():
        if g["id"] == gun_id:
            return g
    raise AssertionError("枪线不存在")


def overview(client):
    return client.get("/api/总览").json()


def assert_synced(client, sid):
    """核心不变式：会话、桩位、枪线、总览对同一笔会话同口径。"""
    detail = client.get(f"/api/会话/{sid}").json()
    pile = pile_of(client, detail["pile_id"])
    gun = gun_of(client, detail["gun_id"])
    ov = overview(client)

    # 桩位页与枪线页占用一致
    assert pile["占用"] == gun["占用"], (pile, gun)
    assert pile["占用未清"] == gun["占用未清"], (pile, gun)
    # 桩位页占用与会话详情一致
    assert pile["占用"] == detail["占用"], (pile, detail)
    # 未腾空时，枪线页不可开充、桩位页不可开充
    if detail["占用"]:
        assert not pile["可开充"] and not gun["可开充"], (pile, gun)
    else:
        assert pile["可开充"] == (pile["status"] == "空闲")
    # 已关账却不腾空时，必须标记占用未清，绝不可显示空闲
    if detail["status"] == "已关账" and detail["占用"]:
        assert pile["展示状态"] == "占用未清"
        assert gun["展示状态"] == "占用未清"
    # 总览空闲数与桩位页可开充数一致
    piles = client.get("/api/桩位").json()
    assert ov["桩位空闲"] == sum(1 for p in piles if p["展示状态"] == "空闲"), ov
    assert ov["桩位可开充"] == sum(1 for p in piles if p["可开充"]), ov
    assert ov["桩位占用"] == sum(1 for p in piles if p["展示状态"] == "占用"), ov
    assert ov["桩位占用未清"] == sum(1 for p in piles if p["展示状态"] == "占用未清"), ov
    # 时序重放必须能重建当前占用
    ck = client.get("/api/会话/一致性").json()
    assert ck["一致"], ck
    assert ck["当前占用"] == ck["时序重放占用"], ck
    return detail, pile, gun, ov
