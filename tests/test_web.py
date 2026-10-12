"""Web 接口冒烟测试：账号生命周期与任务创建（链上调用被替身拦截）。"""

from __future__ import annotations

from fastapi.testclient import TestClient
from web3 import Web3

from fabx_minter.chain import TransistorState
from fabx_minter.config import Settings
from fabx_minter.db import Database
from fabx_minter.web.app import create_app

KEY = "0x" + "11" * 32
TRANSISTORS = "0x" + "cd" * 20


def _client(settings: Settings) -> TestClient:
    app = create_app(settings, Database(settings.resolved_db_path()))
    return TestClient(app)


def _settings(settings: Settings, **overrides) -> Settings:
    """基于夹具配置派生新配置；重新构造以触发 pydantic 校验（SecretStr 等）。"""
    base = settings.model_dump()
    base.update(overrides)
    return Settings(**base)


def test_web_token_required_when_configured(settings: Settings) -> None:
    client = _client(_settings(settings, web_token="tok-123"))
    # 未带令牌：业务接口拒绝
    assert client.get("/api/accounts", params={"with_balance": "false"}).status_code == 401
    # 带对令牌：放行
    ok = client.get(
        "/api/accounts",
        params={"with_balance": "false"},
        headers={"X-FABX-Token": "tok-123"},
    )
    assert ok.status_code == 200
    # health 保持公开，便于探活
    assert client.get("/api/health").status_code == 200


def test_web_refuses_public_bind_without_token(settings: Settings) -> None:
    # 监听 0.0.0.0 却未配置令牌：业务接口一律拒绝，避免私钥管理接口裸奔
    client = _client(_settings(settings, web_host="0.0.0.0"))
    assert client.get("/api/accounts", params={"with_balance": "false"}).status_code == 403
    assert client.get("/api/health").status_code == 200


def test_web_rejects_cross_origin_write(settings: Settings) -> None:
    client = _client(settings)
    denied = client.post(
        "/api/accounts/import",
        json={"content": KEY},
        headers={"Origin": "http://evil.example"},
    )
    assert denied.status_code == 403
    # 同源写请求仍应放行
    allowed = client.post(
        "/api/accounts/import",
        json={"content": KEY},
        headers={"Origin": "http://testserver"},
    )
    assert allowed.status_code == 200


def test_account_import_and_lifecycle(settings: Settings) -> None:
    client = _client(settings)
    resp = client.post(
        "/api/accounts/import",
        json={"content": f"{KEY},acc1\n{'0x' + '22' * 32},acc2", "group": "g1"},
    )
    assert resp.status_code == 200
    payload = resp.json()
    assert len(payload["imported"]) == 2
    assert payload["errors"] == []

    listed = client.get("/api/accounts", params={"with_balance": "false"}).json()
    assert len(listed) == 2
    assert listed[0]["group"] == "g1"

    account_id = listed[0]["id"]
    patched = client.patch(f"/api/accounts/{account_id}", json={"enabled": False})
    assert patched.status_code == 200
    assert patched.json()["enabled"] is False

    assert client.delete(f"/api/accounts/{account_id}").status_code == 204
    assert len(client.get("/api/accounts", params={"with_balance": "false"}).json()) == 1


def test_import_requires_password(settings: Settings) -> None:
    # 未配置 FABX_SECRET 且未显式传 password 时，应给出明确的 400
    plain = settings.model_copy(update={"secret": None})
    client = _client(plain)
    resp = client.post("/api/accounts/import", json={"content": KEY})
    assert resp.status_code == 400
    assert "口令" in resp.json()["detail"]


def test_import_password_must_come_from_body(settings: Settings) -> None:
    # 口令不能走 URL 查询串：查询串会被服务器访问日志记录，等于把明文口令写进日志。
    plain = settings.model_copy(update={"secret": None})
    client = _client(plain)
    # 塞进查询串的口令应被忽略
    via_query = client.post(
        "/api/accounts/import",
        params={"password": "leaked-in-url"},
        json={"content": KEY},
    )
    assert via_query.status_code == 400
    # 放进请求体才生效
    via_body = client.post(
        "/api/accounts/import",
        json={"content": KEY, "password": "kept-in-body"},
    )
    assert via_body.status_code == 200
    assert len(via_body.json()["imported"]) == 1


def test_add_single_account_password_in_body(settings: Settings) -> None:
    # 新增单个账号同样要求口令走请求体，未配置 FABX_SECRET 时也能显式覆盖
    plain = settings.model_copy(update={"secret": None})
    client = _client(plain)
    resp = client.post(
        "/api/accounts",
        json={"private_key": KEY, "label": "solo", "password": "pw"},
    )
    assert resp.status_code == 201, resp.text
    assert resp.json()["label"] == "solo"


def test_create_job_with_mocked_chain(settings: Settings, monkeypatch) -> None:
    client = _client(settings)
    client.post("/api/accounts/import", json={"content": KEY})

    state = TransistorState(
        address=Web3.to_checksum_address(TRANSISTORS),
        nand_id=0,
        latch_id=1,
        mint_price_wei=10**15,
        supply_cap=1000,
        minted=0,
        minted_by_id={0: 0, 1: 0},
        type_cap={0: 500, 1: 500},
        cpu_name="Mock CPU",
    )
    # 拦掉真实 RPC，只验证编排逻辑
    monkeypatch.setattr("fabx_minter.minter.read_transistor_state", lambda w3, addr: state)

    resp = client.post(
        "/api/jobs",
        json={
            "name": "smoke",
            "targets": [{"transistors": TRANSISTORS, "token_id": 0, "amount": 2}],
        },
    )
    assert resp.status_code == 201, resp.text
    job = resp.json()
    assert job["total"] == 1

    detail = client.get(f"/api/jobs/{job['id']}").json()
    assert len(detail["tasks"]) == 1
    assert detail["tasks"][0]["amount"] == 2
    assert detail["tasks"][0]["value_wei"] == 2 * 10**15

    plan = client.post(
        "/api/plan",
        json={
            "name": "smoke",
            "targets": [{"transistors": TRANSISTORS, "token_id": 1, "amount": 3}],
        },
    )
    assert plan.status_code == 200
    assert plan.json()["count"] == 1
    assert plan.json()["total_value_wei"] == 3 * 10**15


def test_create_job_without_accounts_is_rejected(settings: Settings, monkeypatch) -> None:
    client = _client(settings)
    state = TransistorState(
        address=Web3.to_checksum_address(TRANSISTORS),
        nand_id=0,
        latch_id=1,
        mint_price_wei=1,
        supply_cap=1,
        minted=0,
        minted_by_id={},
        type_cap={},
    )
    monkeypatch.setattr("fabx_minter.minter.read_transistor_state", lambda w3, addr: state)
    resp = client.post(
        "/api/jobs",
        json={
            "name": "empty",
            "targets": [{"transistors": TRANSISTORS, "token_id": 0, "amount": 1}],
        },
    )
    assert resp.status_code == 400
    assert "账号" in resp.json()["detail"]


def test_chain_read_failure_maps_to_502(settings: Settings, monkeypatch) -> None:
    """链上状态读不到属于上游故障，应返回 502，而不是让用户以为参数填错了（400）。"""
    client = _client(settings)
    client.post("/api/accounts/import", json={"content": KEY})

    def _boom(w3, addr):
        raise RuntimeError("rpc timeout")

    monkeypatch.setattr("fabx_minter.minter.read_transistor_state", _boom)
    payload = {
        "name": "rpc-down",
        "targets": [{"transistors": TRANSISTORS, "token_id": 0, "amount": 1}],
    }
    plan = client.post("/api/plan", json=payload)
    assert plan.status_code == 502, plan.text
    job = client.post("/api/jobs", json=payload)
    assert job.status_code == 502, job.text


def test_index_serves_html_template(settings: Settings) -> None:
    """首页必须返回 HTML 控制台页面，而不是 JSON。"""
    client = _client(settings)
    resp = client.get("/")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/html")
    assert "<!DOCTYPE html>" in resp.text or "<html" in resp.text.lower()
