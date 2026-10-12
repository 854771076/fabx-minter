"""SQLite 存储层：账号、任务状态机与重试路径。"""

from __future__ import annotations

from fabx_minter.crypto import encrypt_private_key
from fabx_minter.db import Database
from fabx_minter.models import JobStatus, TaskStatus

ADDR_A = "0x" + "ab" * 20
ADDR_B = "0x" + "cd" * 20
TRANSISTORS = "0x" + "ef" * 20


def _cipher(key: str = "22" * 32) -> bytes:
    return encrypt_private_key(key, "pw")


def test_account_crud(db: Database) -> None:
    account_id = db.add_account(ADDR_A, _cipher(), label="wallet-a", group="g1")
    account = db.get_account(account_id)
    assert account is not None
    assert account.address == ADDR_A
    assert account.label == "wallet-a"
    assert account.group == "g1"
    assert account.enabled is True

    # 重复导入同一地址应更新而不是新增
    same_id = db.add_account(ADDR_A, _cipher("33" * 32), label="wallet-a2", group="g2")
    assert same_id == account_id
    assert len(db.list_accounts()) == 1
    assert db.list_accounts()[0].group == "g2"

    db.set_account_enabled(account_id, False)
    assert db.list_accounts(enabled_only=True) == []
    assert db.list_accounts(enabled_only=False)[0].enabled is False

    db.delete_account(account_id)
    assert db.list_accounts() == []


def test_group_and_id_filters(db: Database) -> None:
    a = db.add_account(ADDR_A, _cipher(), label=None, group="g1")
    b = db.add_account(ADDR_B, _cipher("44" * 32), label=None, group="g2")
    assert [x.id for x in db.list_accounts(group="g1")] == [a]
    assert [x.id for x in db.list_accounts(account_ids=[b])] == [b]
    assert {x.id for x in db.list_accounts(account_ids=[a, b])} == {a, b}
    # 空列表是「明确不选任何账号」，不能退化成「不筛选」而回退成全部账号
    assert db.list_accounts(account_ids=[]) == []


def test_job_and_task_lifecycle(db: Database) -> None:
    account_id = db.add_account(ADDR_A, _cipher(), label=None, group=None)
    job_id = db.create_job("smoke", {"targets": []}, dry_run=True)

    total = db.add_tasks(
        job_id,
        [
            {
                "account_id": account_id,
                "transistors": TRANSISTORS,
                "token_id": 0,
                "amount": 2,
                "value_wei": 10**15,
            }
        ],
    )
    assert total == 1

    job = db.get_job(job_id)
    assert job is not None
    assert job.status == JobStatus.DRAFT
    assert job.total == 1
    assert job.pending == 1
    assert job.progress == 0.0

    task = db.list_tasks(job_id=job_id)[0]
    assert task.status == TaskStatus.PENDING
    assert task.address == ADDR_A

    db.set_job_status(job_id, JobStatus.RUNNING)
    db.mark_task_sent(task.id, nonce=5, tx_hash="0xdeadbeef")
    sent = db.get_task(task.id)
    assert sent is not None and sent.status == TaskStatus.SENT and sent.attempts == 1

    db.mark_task_confirmed(
        task.id, block_number=100, gas_used=21000, tx_hash="0xdeadbeef"
    )
    job = db.get_job(job_id)
    assert job is not None and job.confirmed == 1 and job.progress == 1.0
    confirmed = db.get_task(task.id)
    assert confirmed is not None
    assert confirmed.block_number == 100
    assert confirmed.gas_used == 21000


def test_reset_failed_tasks_for_retry(db: Database) -> None:
    account_id = db.add_account(ADDR_A, _cipher(), label=None, group=None)
    job_id = db.create_job("retry", {}, dry_run=False)
    db.add_tasks(
        job_id,
        [
            {
                "account_id": account_id,
                "transistors": TRANSISTORS,
                "token_id": 1,
                "amount": 1,
                "value_wei": 1,
            }
        ],
    )
    task = db.list_tasks(job_id=job_id)[0]
    db.set_job_status(job_id, JobStatus.FAILED, error="boom")
    db.mark_task_failed(task.id, "广播失败")

    reset = db.reset_job_tasks(job_id)
    assert reset == 1
    job = db.get_job(job_id)
    assert job is not None
    assert job.status == JobStatus.DRAFT
    assert job.pending == 1
    assert job.error is None
    assert db.get_task(task.id).status == TaskStatus.PENDING  # type: ignore[union-attr]


def test_reset_keeps_sent_tasks(db: Database) -> None:
    """sent 任务可能已上链，重置时必须保留，否则会重复广播、重复扣款。"""
    account_id = db.add_account(ADDR_A, _cipher(), label=None, group=None)
    job_id = db.create_job("keep-sent", {}, dry_run=False)
    db.add_tasks(
        job_id,
        [
            {"account_id": account_id, "transistors": TRANSISTORS, "token_id": 0, "amount": 1, "value_wei": 1},
            {"account_id": account_id, "transistors": TRANSISTORS, "token_id": 1, "amount": 1, "value_wei": 1},
        ],
    )
    tasks = db.list_tasks(job_id=job_id)
    db.mark_task_sent(tasks[0].id, nonce=0, tx_hash="0xaaa")
    db.mark_task_failed(tasks[1].id, "广播失败")

    reset = db.reset_job_tasks(job_id)
    assert reset == 1
    kept = db.get_task(tasks[0].id)
    assert kept is not None and kept.status == TaskStatus.SENT and kept.tx_hash == "0xaaa"
    retried = db.get_task(tasks[1].id)
    assert retried is not None
    assert retried.status == TaskStatus.PENDING
    assert retried.error is None


def test_reset_rearms_skipped_for_retry(db: Database) -> None:
    """余额不足/售罄被跳过的任务在重置后必须回到 pending，充值后才能重跑。"""
    account_id = db.add_account(ADDR_A, _cipher(), label=None, group=None)
    job_id = db.create_job("rearm-skipped", {}, dry_run=False)
    db.add_tasks(
        job_id,
        [
            {"account_id": account_id, "transistors": TRANSISTORS, "token_id": 0, "amount": 1, "value_wei": 1},
            {"account_id": account_id, "transistors": TRANSISTORS, "token_id": 1, "amount": 1, "value_wei": 1},
        ],
    )
    tasks = db.list_tasks(job_id=job_id)
    # index 0 已广播，index 1 因余额不足被跳过
    db.mark_task_sent(tasks[0].id, nonce=0, tx_hash="0xbbb")
    db.mark_task_failed(tasks[1].id, "余额不足", status=TaskStatus.SKIPPED)
    db.set_job_status(job_id, JobStatus.FAILED, error="boom")

    reset = db.reset_job_tasks(job_id)

    assert reset == 1
    kept = db.get_task(tasks[0].id)
    assert kept is not None and kept.status == TaskStatus.SENT
    retried = db.get_task(tasks[1].id)
    assert retried is not None
    assert retried.status == TaskStatus.PENDING
    assert retried.error is None


def test_mark_task_unconfirmed_keeps_sent(db: Database) -> None:
    """对账未命中回执时只记录提示，状态与交易哈希都不能被清掉。"""
    account_id = db.add_account(ADDR_A, _cipher(), label=None, group=None)
    job_id = db.create_job("unconfirmed", {}, dry_run=False)
    db.add_tasks(
        job_id,
        [{"account_id": account_id, "transistors": TRANSISTORS, "token_id": 0, "amount": 1, "value_wei": 1}],
    )
    task = db.list_tasks(job_id=job_id)[0]
    db.mark_task_sent(task.id, nonce=0, tx_hash="0xdead")

    db.mark_task_unconfirmed(task.id, "等待回执超时")
    row = db.get_task(task.id)
    assert row is not None
    assert row.status == TaskStatus.SENT
    assert row.tx_hash == "0xdead"
    assert row.error == "等待回执超时"


def test_job_restores_stop_on_first_failure(db: Database) -> None:
    """执行参数随 config_json 落库，读取任务时必须能还原。"""
    db.create_job("flag", {"stop_on_first_failure": True}, dry_run=False)
    db.create_job("no-flag", {}, dry_run=False)
    by_name = {job.name: job for job in db.list_jobs()}
    assert by_name["flag"].stop_on_first_failure is True
    assert by_name["no-flag"].stop_on_first_failure is False


def test_job_counts_skipped_as_finished(db: Database) -> None:
    """售罄 / 余额不足被跳过也是终态：进度不能因为跳过的任务停在 0。"""
    account_id = db.add_account(ADDR_A, _cipher(), label=None, group=None)
    job_id = db.create_job("skipped", {}, dry_run=False)
    db.add_tasks(
        job_id,
        [
            {
                "account_id": account_id,
                "transistors": TRANSISTORS,
                "token_id": 0,
                "amount": 1,
                "value_wei": 1,
            }
        ],
    )
    task = db.list_tasks(job_id=job_id)[0]
    db.set_job_status(job_id, JobStatus.DONE)
    db.mark_task_failed(task.id, "已售罄", status=TaskStatus.SKIPPED)

    job = db.get_job(job_id)
    assert job is not None
    assert job.skipped == 1
    assert job.pending == 0
    assert job.progress == 1.0


def test_circuit_upsert_and_miner_backfill(db: Database) -> None:
    """tapeout 落库要能写完整记录，start 后回填 task_id / miner_key。"""
    from fabx_minter.models import CircuitIn

    account_id = db.add_account(ADDR_A, _cipher(), label=None, group=None)
    circuits = "0x" + "11" * 20
    circuit_id = db.add_circuit(
        CircuitIn(
            account_id=account_id,
            circuits=circuits,
            circuit_id=7,
            n_in=1,
            n_out=1,
            n_state=0,
            gate_count=1,
            salt="0" * 64,
            tx_hash="0x" + "aa" * 32,
            block_number=123,
        )
    )
    assert circuit_id > 0
    record = db.get_circuit(circuits, 7)
    assert record is not None
    assert record.account_id == account_id
    assert record.salt == "0" * 64
    assert record.task_id is None

    # 同一 (circuits, circuitId) 重复写入只更新可重算字段，不能抹掉已用于 start 的 salt
    same_id = db.add_circuit(
        CircuitIn(
            account_id=account_id,
            circuits=circuits.upper(),
            circuit_id=7,
            n_in=2,
            n_out=2,
            n_state=1,
            gate_count=3,
            salt=None,
        )
    )
    assert same_id == circuit_id
    assert len(db.list_circuits(circuits=circuits)) == 1
    record = db.get_circuit(circuits, 7)
    assert record is not None
    assert record.n_in == 2
    assert record.salt == "0" * 64

    db.set_circuit_miner(
        circuits=circuits,
        circuit_id=7,
        task_id=1,
        miner_key="0x" + "22" * 32,
    )
    record = db.get_circuit(circuits, 7)
    assert record is not None
    assert record.task_id == 1
    assert record.miner_key == "0x" + "22" * 32
    assert [c.circuit_id for c in db.list_circuits(account_id=account_id)] == [7]


def test_cpu_upsert_and_listing(db: Database) -> None:
    from fabx_minter.models import CpuOut

    cpu = CpuOut(
        circuits="0x" + "11" * 20,
        transistors=TRANSISTORS,
        name="FAB_No1",
        mint_price_wei=10**15,
        supply_cap=1000,
        minted=3,
        circuit_count=1,
    )
    db.upsert_cpu(cpu)
    db.upsert_cpu(cpu.model_copy(update={"minted": 7}))
    cpus = db.list_cpus()
    assert len(cpus) == 1
    assert cpus[0].minted == 7
    assert cpus[0].name == "FAB_No1"
