"""链上状态对象的纯逻辑：额度判断与费用换算（不访问网络）。"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from fabx_minter import chain
from fabx_minter.chain import TransistorState, format_okb


def _state(**overrides) -> TransistorState:
    base = dict(
        address="0x" + "11" * 20,
        nand_id=0,
        latch_id=1,
        mint_price_wei=0,
        supply_cap=0,
        minted=0,
        minted_by_id={},
        type_cap={},
    )
    base.update(overrides)
    return TransistorState(**base)  # type: ignore[arg-type]


def test_remaining_without_type_cap_and_supply_cap_is_unlimited() -> None:
    # 既没有 typeCap 也没有 supplyCap 时才是真正不限制
    state = _state(supply_cap=0, type_cap={0: 0}, minted_by_id={0: 500})
    assert state.remaining(0) is None


def test_remaining_without_type_cap_uses_supply_headroom() -> None:
    # typeCap=0 表示该类型不单独设限，但仍受全局 supplyCap 约束：
    # 剩余应等于 supplyCap - minted，避免接近总上限时按满额放行
    state = _state(supply_cap=14000, minted=13375, type_cap={0: 0}, minted_by_id={0: 0})
    assert state.remaining(0) == 625


def test_remaining_type_cap_takes_min_with_supply_headroom() -> None:
    # 类型上限与全局上限同时存在时取更紧的一个
    state = _state(
        supply_cap=1000,
        minted=990,
        type_cap={0: 100},
        minted_by_id={0: 30},
    )
    assert state.remaining(0) == 10


def test_remaining_respects_type_cap() -> None:
    state = _state(type_cap={0: 100}, minted_by_id={0: 30})
    assert state.remaining(0) == 70
    assert state.remaining(999) is None  # 未记录的类型同样视为不限制


def test_remaining_zero_when_supply_cap_reached() -> None:
    state = _state(supply_cap=1000, minted=1000, type_cap={0: 100})
    assert state.remaining(0) == 0


def test_remaining_zero_when_supply_cap_reached_without_type_cap() -> None:
    # typeCap=0 但总量已满，同样应判定为售罄（返回 0 而非 None）
    state = _state(supply_cap=1000, minted=1000, type_cap={0: 0}, minted_by_id={0: 500})
    assert state.remaining(0) == 0


def test_cost_and_formatting() -> None:
    state = _state(mint_price_wei=10**14)  # 0.0001 OKB
    assert state.cost_wei(3) == 3 * 10**14
    assert format_okb(10**18) == "1"
    assert format_okb(10**14) == "0.0001"
    assert format_okb(1500000000000000000) == "1.5"
    assert format_okb(0) == "0"


def test_cost_includes_protocol_fee_once() -> None:
    # 协议费按「每笔交易」固定收取一次，不随铸造数量放大：
    # 链上样例 value = 650000000000000 + 313 * 1
    state = _state(mint_price_wei=1, protocol_fee_wei=650_000_000_000_000)
    assert state.cost_wei(1) == 650_000_000_000_001
    assert state.cost_wei(313) == 650_000_000_000_313
    assert state.cost_wei(500) == 650_000_000_000_500


def test_read_state_reads_protocol_fee_when_present(monkeypatch) -> None:
    """ABI 里存在 protocolFee 视图时应读出真实值，并计入 cost_wei。"""
    fake = _fake_contract(
        NAND=lambda: _Call(0),
        LATCH=lambda: _Call(1),
        mintPrice=lambda: _Call(10**14),
        supplyCap=lambda: _Call(1000),
        minted=lambda: _Call(0),
        mintedOf=lambda token_id: _Call(0),
        typeCap=lambda token_id: _Call(0),
        protocolFee=lambda: _Call(650_000_000_000_000),
    )
    monkeypatch.setattr(chain, "transistors_contract", lambda w3, addr: fake)
    state = chain.read_transistor_state(object(), "0x" + "11" * 20)
    assert state.protocol_fee_wei == 650_000_000_000_000
    assert state.cost_wei(2) == 650_000_000_000_000 + 2 * 10**14


def test_read_state_defaults_protocol_fee_zero_when_absent(monkeypatch) -> None:
    """旧合约 ABI 无 protocolFee 时降级为 0，不影响 mintPrice 计价。"""
    monkeypatch.setattr(
        chain,
        "_abi_function_names",
        lambda name: frozenset({"NAND", "LATCH", "mintPrice", "supplyCap", "minted"}),
    )
    fake = _fake_contract(
        NAND=lambda: _Call(0),
        LATCH=lambda: _Call(1),
        mintPrice=lambda: _Call(10**14),
        supplyCap=lambda: _Call(1000),
        minted=lambda: _Call(0),
    )
    monkeypatch.setattr(chain, "transistors_contract", lambda w3, addr: fake)
    state = chain.read_transistor_state(object(), "0x" + "11" * 20)
    assert state.protocol_fee_wei == 0
    assert state.cost_wei(2) == 2 * 10**14


class _Call:
    """模拟合约视图调用的返回对象（.call()）。"""

    def __init__(self, value=None, error: Exception | None = None) -> None:
        self._value = value
        self._error = error

    def call(self):
        if self._error is not None:
            raise self._error
        return self._value


def _fake_contract(**calls) -> SimpleNamespace:
    return SimpleNamespace(functions=SimpleNamespace(**calls))


def test_read_state_propagates_optional_view_failure(monkeypatch) -> None:
    """typeCap 调用失败必须抛出，绝不能吞成 0 而误判为「不限制」。"""

    def boom(_token_id: int) -> _Call:
        raise RuntimeError("rpc down")

    fake = _fake_contract(
        NAND=lambda: _Call(0),
        LATCH=lambda: _Call(1),
        mintPrice=lambda: _Call(10**15),
        supplyCap=lambda: _Call(1000),
        minted=lambda: _Call(0),
        mintedOf=lambda token_id: _Call(0),
        typeCap=boom,
    )
    monkeypatch.setattr(chain, "transistors_contract", lambda w3, addr: fake)
    with pytest.raises(RuntimeError):
        chain.read_transistor_state(object(), "0x" + "11" * 20)


def test_read_state_skips_views_absent_from_abi(monkeypatch) -> None:
    """ABI 里没有的可选视图（旧合约）直接跳过，不发起调用。"""
    monkeypatch.setattr(
        chain,
        "_abi_function_names",
        lambda name: frozenset({"NAND", "LATCH", "mintPrice", "supplyCap", "minted"}),
    )
    fake = _fake_contract(
        NAND=lambda: _Call(7),
        LATCH=lambda: _Call(8),
        mintPrice=lambda: _Call(10**15),
        supplyCap=lambda: _Call(1000),
        minted=lambda: _Call(5),
    )
    monkeypatch.setattr(chain, "transistors_contract", lambda w3, addr: fake)
    state = chain.read_transistor_state(object(), "0x" + "11" * 20)
    assert state.nand_id == 7
    assert state.latch_id == 8
    assert state.minted_by_id == {}
    assert state.type_cap == {}
    assert state.cpu_name is None
