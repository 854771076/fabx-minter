"""PoD 挖矿链路的纯逻辑与交易编排测试（用假合约，不访问网络）。"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from web3 import Web3

from fabx_minter import chain, mining
from fabx_minter.mining import MiningEngine, MiningError, _normalize_key, _to_bytes32
from fabx_minter.models import TapeoutRequest
from fabx_minter.netlist import encode_gates


class _Call:
    """模拟 web3 的 ``ContractFunction``：先取到函数，再 ``()`` 取调用项，最后 ``.call()``。

    这里让同一个对象兼任「函数」与「调用项」，方便在测试里同时覆盖
    ``c.functions.foo()`` / ``c.functions.foo(arg)`` 两种写法。
    """

    def __init__(self, value):
        self._value = value

    def __call__(self, *args, **kwargs):
        return self

    def call(self):
        return self._value


class _FakeContract:
    """模拟 web3 Contract：同时支持 ``c.foo()`` 与 ``c.functions["foo"]()`` 两种取法。"""

    class _Ns:
        def __init__(self, **views):
            self.__dict__.update(views)

        def __getitem__(self, name):
            return self.__dict__[name]

        def __getattr__(self, name):
            # 视图既可能是可直接调用的 _Call，也可能是「入参 -> _Call」的 lambda，
            # 这里统一包一层，保证 ``functions.name(...)`` 后仍能得到 _Call。
            view = self.__dict__.get(name)
            if view is None:
                raise AttributeError(name)
            if callable(view):
                return view
            return lambda *a, **k: _Call(view)

    def __init__(self, **views):
        self.functions = self._Ns(**views)
        for key, value in views.items():
            setattr(self, key, value)


def _engine(settings, db=None) -> MiningEngine:
    """构造一个引擎并替换掉真实 Web3，避免构造时连网。"""
    engine = MiningEngine.__new__(MiningEngine)
    engine.db = db
    engine.settings = settings
    engine._password = "test-secret"
    engine.w3 = object()
    return engine


def test_design_commitment_matches_onchain_view(settings) -> None:
    # 本地 ABI 编码必须与链上 designCommitment 视图逐字节一致，否则 commit 不认账
    from fabx_minter.chain import make_web3, pod_mining_contract

    account = Web3.to_checksum_address("0x" + "ab" * 20)
    circuits = Web3.to_checksum_address(settings.default_circuits_address)
    netlist_hash = Web3.keccak(b"\x00\x00\x00\x02\x00\x00\x00\x02")
    salt = bytes(range(32))
    local = MiningEngine._design_commitment(account, circuits, netlist_hash, salt)
    w3 = make_web3(settings)
    onchain = pod_mining_contract(w3, settings).functions.designCommitment(
        account, circuits, netlist_hash, salt
    ).call()
    assert local == bytes(onchain)


def test_extract_circuit_id_from_transfer_log() -> None:
    circuits = "0x" + "11" * 20
    topic = Web3.keccak(text="Transfer(address,address,uint256)").hex()
    # from=0（铸造），to=某地址，tokenId=7
    token_id_topic = "0x" + "0" * 63 + "7"
    receipt = {
        "logs": [
            {
                "address": circuits,
                "topics": [topic, "0x" + "0" * 64, "0x" + "22" * 32, token_id_topic],
            }
        ]
    }
    assert MiningEngine._extract_circuit_id(receipt, circuits) == 7

    # tokenId=0 时同样要解出 0（0 是有效编号，不能被当成「没解到」）
    receipt["logs"][0]["topics"][3] = "0x" + "0" * 64
    assert MiningEngine._extract_circuit_id(receipt, circuits) == 0


def test_extract_circuit_id_ignores_non_mint_and_other_contracts() -> None:
    circuits = "0x" + "11" * 20
    topic = Web3.keccak(text="Transfer(address,address,uint256)").hex()
    other = "0x" + "33" * 20
    # 非零 from（普通转账）+ 其他合约地址，都不应被当作本次 tapeout 的 tokenId
    receipt = {
        "logs": [
            {"address": other, "topics": [topic, "0x" + "0" * 64, "0x" + "22" * 32, "0x" + "0" * 63 + "9"]},
            {"address": circuits, "topics": [topic, "0x" + "44" * 32, "0x" + "22" * 32, "0x" + "0" * 63 + "8"]},
        ]
    }
    assert MiningEngine._extract_circuit_id(receipt, circuits) is None


def test_to_bytes32_pads_and_rejects_oversize() -> None:
    assert _to_bytes32("0x01") == b"\x01" + b"\x00" * 31
    assert _normalize_key("0x01") == "0x01" + "00" * 31
    with pytest.raises(MiningError):
        _to_bytes32("0x" + "aa" * 33)


def test_ensure_transistor_balance_passes_when_enough(monkeypatch, settings) -> None:
    engine = _engine(settings)
    circuits = _FakeContract(transistors=_Call("0x" + "44" * 20))
    transistors = _FakeContract(
        NAND=_Call(0),
        LATCH=_Call(1),
        balanceOf=lambda addr, tid: _Call(100 if tid == 0 else 200),
    )
    monkeypatch.setattr(mining, "transistors_contract", lambda w3, addr: transistors)
    monkeypatch.setattr(engine, "_circuits_contract", lambda addr: circuits)
    engine._ensure_transistor_balance("0x" + "11" * 20, "0x" + "22" * 20, 5, 5)


def test_ensure_transistor_balance_blocks_when_short(monkeypatch, settings) -> None:
    engine = _engine(settings)
    circuits = _FakeContract(transistors=_Call("0x" + "44" * 20))
    transistors = _FakeContract(
        NAND=_Call(0),
        LATCH=_Call(1),
        balanceOf=lambda addr, tid: _Call(1 if tid == 0 else 100),
    )
    monkeypatch.setattr(mining, "transistors_contract", lambda w3, addr: transistors)
    monkeypatch.setattr(engine, "_circuits_contract", lambda addr: circuits)
    with pytest.raises(MiningError, match="晶体管不足"):
        engine._ensure_transistor_balance("0x" + "11" * 20, "0x" + "22" * 20, 5, 5)


def test_tapeout_dry_run_skips_broadcast(monkeypatch, settings) -> None:
    """dry-run 只做本地预估，不广播也不落库。"""
    engine = _engine(settings)
    engine._open_account = lambda account_id: {"id": account_id, "address": "0x" + "22" * 20, "private_key": "0x"}
    engine._preview_burn = lambda netlist: (1, 0)
    engine._ensure_transistor_balance = lambda *a, **k: None
    circuits = _FakeContract(TAPEOUT_FEE=_Call(10**15), nextId=_Call(5))
    monkeypatch.setattr(engine, "_circuits_contract", lambda addr: circuits)

    def _boom(*a, **k):  # pragma: no cover - 一旦被调说明 dry-run 仍尝试广播
        raise AssertionError("dry-run 不应广播交易")

    monkeypatch.setattr(engine, "_send_and_wait", _boom)

    result = engine.tapeout(
        TapeoutRequest(account_id=1, gates=[["NAND", 2, 2]], dry_run=True)
    )
    assert result.circuit_id == 5
    assert result.tx_hash == "0x" + "0" * 64
    assert result.nand_burn == 1


def test_resolve_salt_priority(monkeypatch, settings) -> None:
    # 入参 > 本地记录 > 配置 > 全零：start 必须与 commit 用同一 salt
    engine = _engine(settings, db=SimpleNamespace())
    record = SimpleNamespace(salt="0x" + "0b" * 32)
    engine.db = SimpleNamespace(get_circuit=lambda c, i: record)
    assert engine._resolve_salt("0x" + "11" * 20, 1, "0x" + "0a" * 32) == bytes.fromhex("0a" * 32)
    assert engine._resolve_salt("0x" + "11" * 20, 1, None) == bytes.fromhex("0b" * 32)
    engine.db = SimpleNamespace(get_circuit=lambda c, i: None)
    assert engine._resolve_salt("0x" + "11" * 20, 1, None) == b"\x00" * 32


def test_circuit_shape_falls_back_to_local_stats(monkeypatch, settings) -> None:
    engine = _engine(settings)

    class _Broken:
        class _Ns:
            @staticmethod
            def circuitInfo(_id):
                class _Revert:
                    def call(self):
                        raise RuntimeError("revert")

                return _Revert()

        functions = _Ns()

    monkeypatch.setattr(engine, "_circuits_contract", lambda addr: _Broken())
    netlist = encode_gates([["NAND", 2, 2]])  # 一个 NAND(2,2)，7 字节
    n_state, gate_count = engine._circuit_shape("0x" + "11" * 20, 1, netlist, object())
    assert n_state == 0
    assert gate_count == 1


def test_preflight_reports_local_vector_pass(monkeypatch, settings) -> None:
    """本地逐向量校验应能覆盖官方接口之外的「网表与任务不匹配」。"""
    engine = _engine(settings)
    # 用最小反相器：NAND(2,2)，输入 1 -> 输出 0
    monkeypatch.setattr(chain, "verify_design", lambda *a, **k: {"pass": True})
    engine.task_meta = lambda task_id: {"nIn": 1, "nOut": 1, "cycles": 1}
    engine.vectors = lambda task_id: {
        "vectors": [{"index": 0, "input": "0x01", "expectedOut": "0x00"}]
    }
    result = engine.preflight(1, [["NAND", 2, 2]])
    assert result["pass"] is True
    assert result["local_ok"] is True
    assert result["local_passed"] == 1
    assert result["local_total"] == 1
