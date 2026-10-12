"""执行引擎的核心保护：售罄不广播、已广播只对账不重播。"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
from web3 import Web3

from fabx_minter import minter as minter_mod
from fabx_minter.chain import TransistorState
from fabx_minter.config import Settings
from fabx_minter.crypto import encrypt_private_key
from fabx_minter.db import Database
from fabx_minter.minter import MintEngine
from fabx_minter.models import JobIn, JobStatus, MintTarget, TaskStatus

TRANSISTORS = "0x" + "ef" * 20
PRIVATE_KEY = "22" * 32
ADDRESS = Web3().eth.account.from_key(PRIVATE_KEY).address
PRIVATE_KEY_2 = "44" * 32
ADDRESS_2 = Web3().eth.account.from_key(PRIVATE_KEY_2).address


class _FakeEth:
    """最小化的 eth 命名空间替身，只实现执行引擎会用到的方法。"""

    account = Web3().eth.account

    def __init__(
        self,
        receipt: dict[str, Any] | None = None,
        error: Exception | None = None,
        balance: int = 10**18,
    ) -> None:
        self.gas_price = 1_000_000_000
        self._receipt = receipt
        self._error = error
        self.balance = balance
        self.broadcast: list[bytes] = []

    def get_transaction_count(self, address: str, block: str = "latest") -> int:
        return 0

    def get_balance(self, address: str, block: str = "latest") -> int:
        return self.balance

    def get_transaction_receipt(self, tx_hash: str) -> dict[str, Any]:
        if self._error is not None:
            raise self._error
        assert self._receipt is not None
        return self._receipt

    def send_raw_transaction(self, raw: bytes) -> Any:
        # 一旦走到这里说明发生了「重复广播」，测试会立刻失败
        self.broadcast.append(raw)
        raise AssertionError("已广播过的任务不允许再次广播")


def _fake_web3(eth: _FakeEth) -> Any:
    return SimpleNamespace(eth=eth)


def _seed_job(db: Database) -> tuple[int, Any]:
    account_id = db.add_account(
        ADDRESS, encrypt_private_key(PRIVATE_KEY, "pw"), label=None, group=None
    )
    job_id = db.create_job("engine", {"stop_on_first_failure": False}, dry_run=False)
    db.add_tasks(
        job_id,
        [
            {
                "account_id": account_id,
                "transistors": TRANSISTORS,
                "token_id": 0,
                "amount": 1,
                "value_wei": 10**15,
            }
        ],
    )
    return job_id, db.list_tasks(job_id=job_id)[0]


def test_run_job_skips_sold_out_without_broadcast(
    db: Database, settings: Settings, monkeypatch
) -> None:
    job_id, _task = _seed_job(db)
    sold_out = TransistorState(
        address=Web3.to_checksum_address(TRANSISTORS),
        nand_id=0,
        latch_id=1,
        mint_price_wei=10**15,
        supply_cap=1000,
        minted=1000,
        minted_by_id={0: 500, 1: 500},
        type_cap={0: 500, 1: 500},
    )
    monkeypatch.setattr(
        "fabx_minter.minter.read_transistor_state", lambda w3, addr: sold_out
    )
    eth = _FakeEth()
    monkeypatch.setattr("fabx_minter.minter.make_web3", lambda settings: _fake_web3(eth))

    report = MintEngine(db, settings, password="pw").run_job(job_id)

    assert report.confirmed == 0
    assert report.skipped == 1
    assert eth.broadcast == []
    task = db.list_tasks(job_id=job_id)[0]
    assert task.status == TaskStatus.SKIPPED


def test_run_job_reconciles_sent_receipt(
    db: Database, settings: Settings, monkeypatch
) -> None:
    job_id, task = _seed_job(db)
    db.mark_task_sent(task.id, nonce=0, tx_hash="0xabc")

    eth = _FakeEth(receipt={"status": 1, "blockNumber": 123, "gasUsed": 21000})
    monkeypatch.setattr("fabx_minter.minter.make_web3", lambda settings: _fake_web3(eth))

    report = MintEngine(db, settings, password="pw").run_job(job_id)

    assert report.confirmed == 1
    assert report.failed == 0
    assert eth.broadcast == []
    confirmed = db.get_task(task.id)
    assert confirmed is not None
    assert confirmed.status == TaskStatus.CONFIRMED
    assert confirmed.tx_hash == "0xabc"
    assert confirmed.block_number == 123


def test_run_job_keeps_sent_when_receipt_missing(
    db: Database, settings: Settings, monkeypatch
) -> None:
    job_id, task = _seed_job(db)
    db.mark_task_sent(task.id, nonce=0, tx_hash="0xabc")

    eth = _FakeEth(error=Exception("TransactionNotFound"))
    monkeypatch.setattr("fabx_minter.minter.make_web3", lambda settings: _fake_web3(eth))

    report = MintEngine(db, settings, password="pw").run_job(job_id)

    assert report.skipped == 1
    assert eth.broadcast == []
    kept = db.get_task(task.id)
    assert kept is not None
    assert kept.status == TaskStatus.SENT
    assert kept.tx_hash == "0xabc"
    assert "对账" in (kept.error or "")


def test_run_job_rejects_when_engine_busy(
    db: Database, settings: Settings, monkeypatch
) -> None:
    """执行闸门被占用时应立即拒绝，避免两个任务在同一账号上并发广播。"""
    job_id, _task = _seed_job(db)
    assert minter_mod._ENGINE_RUN_LOCK.acquire(blocking=False)
    try:
        with pytest.raises(ValueError):
            MintEngine(db, settings, password="pw").run_job(job_id)
    finally:
        minter_mod._ENGINE_RUN_LOCK.release()
    # 闸门释放后应能重新获取，说明拒绝路径没有把锁泄漏出去
    assert minter_mod._ENGINE_RUN_LOCK.acquire(blocking=False)
    minter_mod._ENGINE_RUN_LOCK.release()


def test_run_job_recovers_after_stale_running(
    db: Database, settings: Settings, monkeypatch
) -> None:
    """上次崩溃遗留的 running 状态不应让任务永久卡死，重跑按 tx_hash 对账。"""
    job_id, task = _seed_job(db)
    db.mark_task_sent(task.id, nonce=0, tx_hash="0xabc")
    db.set_job_status(job_id, JobStatus.RUNNING)  # 模拟进程中断残留

    eth = _FakeEth(receipt={"status": 1, "blockNumber": 7, "gasUsed": 21000})
    monkeypatch.setattr("fabx_minter.minter.make_web3", lambda settings: _fake_web3(eth))

    report = MintEngine(db, settings, password="pw").run_job(job_id)

    assert report.confirmed == 1
    assert eth.broadcast == []
    job = db.get_job(job_id)
    assert job is not None
    assert job.status == JobStatus.DONE


def _mintable_state() -> TransistorState:
    return TransistorState(
        address=Web3.to_checksum_address(TRANSISTORS),
        nand_id=0,
        latch_id=1,
        mint_price_wei=10**15,
        supply_cap=1000,
        minted=0,
        minted_by_id={0: 0, 1: 0},
        type_cap={0: 1000, 1: 1000},
    )


def test_run_job_skips_zero_balance_before_estimate(
    db: Database, settings: Settings, monkeypatch
) -> None:
    """余额在预检阶段就被判不足时，应直接跳过且不发起 estimate/broadcast。"""
    job_id, task = _seed_job(db)
    monkeypatch.setattr(
        "fabx_minter.minter.read_transistor_state", lambda w3, addr: _mintable_state()
    )
    eth = _FakeEth(balance=0)
    monkeypatch.setattr("fabx_minter.minter.make_web3", lambda settings: _fake_web3(eth))

    def _no_estimate(self, w3, t, addr):
        raise AssertionError("余额不足时不应再调用 estimate_gas")

    monkeypatch.setattr(MintEngine, "_estimate_gas", _no_estimate)

    report = MintEngine(db, settings, password="pw").run_job(job_id)

    assert report.skipped == 1
    assert report.failed == 0
    assert eth.broadcast == []
    kept = db.get_task(task.id)
    assert kept is not None
    assert kept.status == TaskStatus.SKIPPED
    assert "余额不足" in (kept.error or "")


def _seed_two_accounts(db: Database) -> None:
    """导入两个启用账号，用于验证跨账号共享额度的规划逻辑。"""
    db.add_account(ADDRESS, encrypt_private_key(PRIVATE_KEY, "pw"), label=None, group=None)
    db.add_account(
        ADDRESS_2, encrypt_private_key(PRIVATE_KEY_2, "pw"), label=None, group=None
    )


def test_build_plan_caps_total_by_shared_remaining(
    db: Database, settings: Settings, monkeypatch
) -> None:
    """typeCap 只剩 5 时，两个账号合计不得规划超过 5 笔，避免多余交易 revert。"""
    _seed_two_accounts(db)
    state = TransistorState(
        address=Web3.to_checksum_address(TRANSISTORS),
        nand_id=0,
        latch_id=1,
        mint_price_wei=10**15,
        supply_cap=1000,
        minted=0,
        minted_by_id={0: 0, 1: 0},
        type_cap={0: 5, 1: 5},
    )
    monkeypatch.setattr("fabx_minter.minter.read_transistor_state", lambda w3, addr: state)
    monkeypatch.setattr("fabx_minter.minter.make_web3", lambda settings: _fake_web3(_FakeEth()))

    planned, notes = MintEngine(db, settings, password="pw").build_plan(
        JobIn(name="cap", targets=[MintTarget(transistors=TRANSISTORS, token_id=0, amount=3)])
    )

    assert sum(p.amount for p in planned) == 5
    assert len(planned) == 2
    assert notes  # 被封顶时应给出说明


def test_build_plan_uncapped_when_type_cap_zero(
    db: Database, settings: Settings, monkeypatch
) -> None:
    """typeCap 与 supplyCap 都缺省（=0）时才真正不限制，规划不应被截断。"""
    _seed_two_accounts(db)
    state = TransistorState(
        address=Web3.to_checksum_address(TRANSISTORS),
        nand_id=0,
        latch_id=1,
        mint_price_wei=10**15,
        supply_cap=0,  # 无全局上限
        minted=0,
        minted_by_id={0: 0, 1: 0},
        type_cap={0: 0, 1: 0},  # typeCap=0 表示不单独设限
    )
    monkeypatch.setattr("fabx_minter.minter.read_transistor_state", lambda w3, addr: state)
    monkeypatch.setattr("fabx_minter.minter.make_web3", lambda settings: _fake_web3(_FakeEth()))

    planned, _notes = MintEngine(db, settings, password="pw").build_plan(
        JobIn(name="nocap", targets=[MintTarget(transistors=TRANSISTORS, token_id=0, amount=3)])
    )

    assert sum(p.amount for p in planned) == 6
    assert len(planned) == 2


def test_build_plan_caps_by_supply_headroom_when_type_cap_zero(
    db: Database, settings: Settings, monkeypatch
) -> None:
    """typeCap=0 但 supplyCap 只剩 4 时，应按总上限封顶，避免多余交易 revert。"""
    _seed_two_accounts(db)
    state = TransistorState(
        address=Web3.to_checksum_address(TRANSISTORS),
        nand_id=0,
        latch_id=1,
        mint_price_wei=10**15,
        supply_cap=1000,
        minted=996,
        minted_by_id={0: 996, 1: 0},
        type_cap={0: 0, 1: 0},
    )
    monkeypatch.setattr("fabx_minter.minter.read_transistor_state", lambda w3, addr: state)
    monkeypatch.setattr("fabx_minter.minter.make_web3", lambda settings: _fake_web3(_FakeEth()))

    planned, notes = MintEngine(db, settings, password="pw").build_plan(
        JobIn(name="headroom", targets=[MintTarget(transistors=TRANSISTORS, token_id=0, amount=3)])
    )

    assert sum(p.amount for p in planned) == 4
    assert len(planned) == 2
    assert notes  # 被封顶时应给出说明


def test_build_plan_shares_supply_headroom_across_targets(
    db: Database, settings: Settings, monkeypatch
) -> None:
    """同一合约的 supplyCap 是所有 token 共享的预算：多目标合计不得超过总剩余。

    两个目标（NAND + LATCH）各自读到的「全局剩余」都是 4，若各自封顶就会规划出
    8 笔，超出总上限 4 笔。这里按合约累计已规划数量，确保合计仍为 4。
    """
    _seed_two_accounts(db)
    state = TransistorState(
        address=Web3.to_checksum_address(TRANSISTORS),
        nand_id=0,
        latch_id=1,
        mint_price_wei=10**15,
        supply_cap=1000,
        minted=996,
        minted_by_id={0: 0, 1: 0},
        type_cap={0: 0, 1: 0},  # typeCap=0：只受全局 supplyCap 约束
    )
    monkeypatch.setattr("fabx_minter.minter.read_transistor_state", lambda w3, addr: state)
    monkeypatch.setattr("fabx_minter.minter.make_web3", lambda settings: _fake_web3(_FakeEth()))

    planned, notes = MintEngine(db, settings, password="pw").build_plan(
        JobIn(
            name="shared",
            targets=[
                MintTarget(transistors=TRANSISTORS, token_id=0, amount=3),
                MintTarget(transistors=TRANSISTORS, token_id=1, amount=3),
            ],
        )
    )

    assert sum(p.amount for p in planned) == 4
    assert notes  # 第二个目标因全局额度耗尽被封顶，应给出说明


def test_build_plan_dedupes_identical_targets(
    db: Database, settings: Settings, monkeypatch
) -> None:
    """重复的 (合约, tokenId) 目标只保留一份，预览数量必须与落库结果一致。"""
    _seed_two_accounts(db)
    state = TransistorState(
        address=Web3.to_checksum_address(TRANSISTORS),
        nand_id=0,
        latch_id=1,
        mint_price_wei=10**15,
        supply_cap=1000,
        minted=0,
        minted_by_id={0: 0, 1: 0},
        type_cap={0: 1000, 1: 1000},
    )
    monkeypatch.setattr("fabx_minter.minter.read_transistor_state", lambda w3, addr: state)
    monkeypatch.setattr("fabx_minter.minter.make_web3", lambda settings: _fake_web3(_FakeEth()))

    target = MintTarget(transistors=TRANSISTORS, token_id=0, amount=3)
    planned, notes = MintEngine(db, settings, password="pw").build_plan(
        JobIn(name="dup", targets=[target, target])
    )

    assert len(planned) == 2  # 两个账号各一条，重复目标被忽略
    assert sum(p.amount for p in planned) == 6
    assert any("重复" in n for n in notes)


def test_build_plan_rejects_empty_account_selection(
    db: Database, settings: Settings
) -> None:
    """显式传入空账号列表时不得退化为「全部账号」，应在触链前直接报错。"""
    _seed_two_accounts(db)
    with pytest.raises(ValueError):
        MintEngine(db, settings, password="pw").build_plan(
            JobIn(
                name="empty",
                account_ids=[],
                targets=[MintTarget(transistors=TRANSISTORS, token_id=0, amount=1)],
            )
        )


def test_run_job_maps_node_insufficient_funds_to_skipped(
    db: Database, settings: Settings, monkeypatch
) -> None:
    """节点在 estimate_gas 返回 insufficient funds 时应归为 skipped 而非 failed。"""
    job_id, task = _seed_job(db)
    monkeypatch.setattr(
        "fabx_minter.minter.read_transistor_state", lambda w3, addr: _mintable_state()
    )
    # 余额足以通过预检，但估算阶段节点仍报 insufficient funds
    eth = _FakeEth(balance=10**18)
    monkeypatch.setattr("fabx_minter.minter.make_web3", lambda settings: _fake_web3(eth))

    def _insufficient(self, w3, t, addr):
        raise Exception(
            "{'code': -32003, 'message': 'insufficient funds for gas * price + value'}"
        )

    monkeypatch.setattr(MintEngine, "_estimate_gas", _insufficient)

    report = MintEngine(db, settings, password="pw").run_job(job_id)

    assert report.skipped == 1
    assert report.failed == 0
    assert eth.broadcast == []
    kept = db.get_task(task.id)
    assert kept is not None
    assert kept.status == TaskStatus.SKIPPED
