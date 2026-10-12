"""定时调度：时间解析、等待节奏与自动定位目标合约（不访问真实链）。"""

from __future__ import annotations

from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest

from fabx_minter import scheduler
from fabx_minter.chain import ChainReadError, TransistorState
from fabx_minter.minter import RunReport
from fabx_minter.scheduler import AutoConfig, resolve_start_time
from fabx_minter import scheduler as sched

SH = scheduler.SHANGHAI_TZ


def _at(hour: int, minute: int, second: int = 0, *, day: int = 12) -> datetime:
    return datetime(2026, 10, day, hour, minute, second, tzinfo=SH)


def test_resolve_empty_defaults_to_next_ten() -> None:
    assert resolve_start_time(None, now=_at(9, 0)) == _at(10, 0)
    assert resolve_start_time("", now=_at(10, 0)) == _at(10, 0, day=13)
    assert resolve_start_time("", now=_at(22, 30)) == _at(10, 0, day=13)


def test_resolve_hhmm_today_and_tomorrow() -> None:
    assert resolve_start_time("22:00", now=_at(21, 30)) == _at(22, 0)
    assert resolve_start_time("22:00", now=_at(22, 1)) == _at(22, 0, day=13)
    assert resolve_start_time("9:05", now=_at(9, 0)) == _at(9, 5)


def test_resolve_hhmm_rejects_invalid() -> None:
    with pytest.raises(ValueError):
        resolve_start_time("25:00", now=_at(9, 0))
    with pytest.raises(ValueError):
        resolve_start_time("10:60", now=_at(9, 0))


def test_resolve_iso8601() -> None:
    parsed = resolve_start_time("2026-10-12T22:00:00+08:00", now=_at(9, 0))
    assert parsed == _at(22, 0)
    assert parsed.tzinfo is not None
    naive = resolve_start_time("2026-10-12T22:00:00", now=_at(9, 0))
    assert naive == _at(22, 0)
    assert naive.tzinfo == SH


def test_sleep_seconds_sparse_when_far() -> None:
    cfg = AutoConfig(at=_at(22, 0), poll_interval=1.0)
    assert scheduler._sleep_seconds(_at(21, 0), cfg) == 15.0


def test_sleep_seconds_dense_near_trigger() -> None:
    cfg = AutoConfig(at=_at(22, 0), poll_interval=0.5)
    assert scheduler._sleep_seconds(_at(21, 59, 50), cfg) == 0.5


def test_sleep_seconds_capped_by_remaining() -> None:
    cfg = AutoConfig(at=_at(22, 0), poll_interval=5.0)
    assert scheduler._sleep_seconds(_at(21, 59, 58), cfg) == 2.0


def test_sleep_seconds_zero_after_trigger() -> None:
    cfg = AutoConfig(at=_at(22, 0))
    assert scheduler._sleep_seconds(_at(22, 0), cfg) == 0.0
    assert scheduler._sleep_seconds(_at(22, 1), cfg) == 0.0


def test_wait_until_ticks_then_returns() -> None:
    cfg = AutoConfig(at=_at(22, 0), poll_interval=1.0)
    timeline = iter([_at(21, 59, 58), _at(21, 59, 59), _at(22, 0)])
    ticks: list[datetime] = []
    sleeps: list[float] = []

    def now_fn() -> datetime:
        return next(timeline)

    def sleeper(sec: float) -> None:
        sleeps.append(sec)

    scheduler._wait_until(cfg, now_fn=now_fn, sleeper=sleeper, on_tick=ticks.append)
    assert len(ticks) == 2
    assert all(sec > 0 for sec in sleeps)


def test_detect_new_transistors_returns_new_address(monkeypatch) -> None:
    settings = SimpleNamespace()
    monkeypatch.setattr(scheduler, "cpu_count", lambda w3, st: 3)
    monkeypatch.setattr(scheduler, "latest_transistors", lambda w3, st: "0xNEW")
    said: list[str] = []
    result = scheduler._detect_new_transistors(
        object(), settings, baseline=2, cached=None, say=said.append
    )
    assert result == "0xNEW"
    assert any("新 CPU" in msg for msg in said)


def test_detect_new_transistors_keeps_cached_on_rpc_error(monkeypatch) -> None:
    def boom(w3, st):
        raise RuntimeError("rpc down")

    monkeypatch.setattr(scheduler, "cpu_count", boom)
    result = scheduler._detect_new_transistors(
        object(), SimpleNamespace(), baseline=2, cached="0xCACHED", say=lambda _m: None
    )
    assert result == "0xCACHED"


def test_detect_new_transistors_returns_cached_when_no_new_cpu(monkeypatch) -> None:
    monkeypatch.setattr(scheduler, "cpu_count", lambda w3, st: 2)
    result = scheduler._detect_new_transistors(
        object(), SimpleNamespace(), baseline=2, cached="0xCACHED", say=lambda _m: None
    )
    assert result == "0xCACHED"


def _state(address: str = "0x" + "ab" * 20, **overrides) -> TransistorState:
    base = dict(
        address=address,
        nand_id=0,
        latch_id=1,
        mint_price_wei=10**14,
        protocol_fee_wei=650_000_000_000_000,
        supply_cap=1000,
        minted=0,
        minted_by_id={},
        type_cap={},
        cpu_name="FAB_Test",
    )
    base.update(overrides)
    return TransistorState(**base)  # type: ignore[arg-type]


class _FakeEngine:
    instances: list["_FakeEngine"] = []

    def __init__(self, db, settings, password) -> None:  # noqa: ANN001
        self.password = password
        self.created_job = None
        self.run_job_id = None
        _FakeEngine.instances.append(self)

    def create_job(self, job):  # noqa: ANN001
        self.created_job = job
        return 7, ["已按链上剩余额度封顶"]

    def run_job(self, job_id: int) -> RunReport:
        self.run_job_id = job_id
        return RunReport(job_id=job_id, total=2, confirmed=2)


@pytest.fixture
def fake_engine(monkeypatch):
    _FakeEngine.instances = []
    monkeypatch.setattr(scheduler, "MintEngine", _FakeEngine)
    return _FakeEngine


def test_run_auto_latest_builds_nand_and_latch_job(monkeypatch, fake_engine) -> None:
    monkeypatch.setattr(scheduler, "make_web3", lambda settings: object())
    monkeypatch.setattr(scheduler, "cpu_count", lambda w3, st: 2)
    monkeypatch.setattr(scheduler, "latest_transistors", lambda w3, st: "0x" + "ab" * 20)
    monkeypatch.setattr(scheduler, "read_transistor_state", lambda w3, addr: _state(addr))

    cfg = AutoConfig(
        at=_at(21, 0),
        amount=3,
        account_ids=[4],
        target="latest",
        name="auto-test",
    )
    messages: list[str] = []
    job_id, report = sched.run_auto(
        object(),
        SimpleNamespace(),
        cfg,
        password="secret",
        now_fn=lambda: _at(22, 0),
        sleeper=lambda _s: None,
        progress=messages.append,
    )
    assert job_id == 7
    assert report.confirmed == 2
    engine = _FakeEngine.instances[-1]
    assert engine.password == "secret"
    assert engine.run_job_id == 7
    targets = engine.created_job.targets
    assert [t.token_id for t in targets] == [0, 1]
    assert all(t.amount == 3 for t in targets)
    assert engine.created_job.account_ids == [4]
    assert engine.created_job.name == "auto-test"
    assert any("到达触发时间" in m for m in messages)


def test_run_auto_respects_explicit_token_ids(monkeypatch, fake_engine) -> None:
    monkeypatch.setattr(scheduler, "make_web3", lambda settings: object())
    monkeypatch.setattr(scheduler, "cpu_count", lambda w3, st: 2)
    monkeypatch.setattr(scheduler, "latest_transistors", lambda w3, st: "0x" + "ab" * 20)
    monkeypatch.setattr(scheduler, "read_transistor_state", lambda w3, addr: _state(addr))

    cfg = AutoConfig(at=_at(21, 0), amount=1, token_ids=[1], target="latest")
    sched.run_auto(
        object(),
        SimpleNamespace(),
        cfg,
        password="secret",
        now_fn=lambda: _at(22, 0),
        sleeper=lambda _s: None,
    )
    targets = _FakeEngine.instances[-1].created_job.targets
    assert [t.token_id for t in targets] == [1]


def test_run_auto_new_waits_for_new_cpu(monkeypatch, fake_engine) -> None:
    monkeypatch.setattr(scheduler, "make_web3", lambda settings: object())
    counts = iter([2, 3])
    monkeypatch.setattr(scheduler, "cpu_count", lambda w3, st: next(counts))
    monkeypatch.setattr(scheduler, "latest_transistors", lambda w3, st: "0x" + "cd" * 20)
    monkeypatch.setattr(
        scheduler, "read_transistor_state", lambda w3, addr: _state(addr, cpu_name="New")
    )

    cfg = AutoConfig(at=_at(21, 0), amount=1, target="new")
    job_id, _report = sched.run_auto(
        object(),
        SimpleNamespace(),
        cfg,
        password="secret",
        now_fn=lambda: _at(22, 0),
        sleeper=lambda _s: None,
    )
    assert job_id == 7
    assert _FakeEngine.instances[-1].created_job.targets[0].transistors == "0x" + "cd" * 20


def test_run_auto_retries_state_read_until_timeout(monkeypatch, fake_engine) -> None:
    monkeypatch.setattr(scheduler, "make_web3", lambda settings: object())
    monkeypatch.setattr(scheduler, "cpu_count", lambda w3, st: 2)
    monkeypatch.setattr(scheduler, "latest_transistors", lambda w3, st: "0x" + "ab" * 20)

    attempts = {"n": 0}

    def flaky(w3, addr):  # noqa: ANN001
        attempts["n"] += 1
        raise RuntimeError("not ready")

    monkeypatch.setattr(scheduler, "read_transistor_state", flaky)

    clock = {"t": _at(22, 0)}

    def now_fn() -> datetime:
        clock["t"] = clock["t"] + timedelta(seconds=20)
        return clock["t"]

    cfg = AutoConfig(at=_at(21, 0), amount=1, target="latest")
    with pytest.raises(ChainReadError):
        sched.run_auto(
            object(),
            SimpleNamespace(),
            cfg,
            password="secret",
            now_fn=now_fn,
            sleeper=lambda _s: None,
        )
    assert attempts["n"] >= 2
