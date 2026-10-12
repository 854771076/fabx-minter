"""批量 mint 引擎。

执行模型：
- 一个任务 = 某账号对某晶体管合约的某个 tokenId 铸造固定数量；
- 同一账号的任务严格按序（nonce 单调），不同账号在线程池中并行；
- 每条任务的发送/确认/失败都会写回 SQLite，进程中断后可重跑。
"""

from __future__ import annotations

import logging
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from typing import Any

from web3 import Web3

from .chain import (
    ChainReadError,
    make_web3,
    read_transistor_state,
    transistors_contract,
)
from .config import Settings
from .crypto import decrypt_private_key, resolve_password
from .db import Database
from .models import JobIn, JobStatus, MintTarget, TaskStatus

log = logging.getLogger("fabx.minter")

# 进程内执行闸门：同一时刻只允许一个任务进入发送阶段。
# 不同任务可能选中同一批账号，如果并行执行会各自按各自读到的 pending nonce
# 发交易，导致 nonce 撞车（一笔被替换或直接失败）。串行化执行是最省的兜底。
_ENGINE_RUN_LOCK = threading.Lock()

# 余额预检用的最小 gas 预算（原生转账下限）。用它先拦掉明显没钱的账号，
# 免得多走一次 estimate_gas 被节点以 -32003 拒绝。
_PRELIM_GAS_FLOOR = 21_000

# 预估 gas 上限的额外余量：mint 的状态判断（是否售罄/额度）在估算与打包之间
# 可能变化，给 25% 余量可减少「估算通过但执行 OOG」的概率。
# 注意它只放大 gas 上限，价格余量由 settings.gas_multiplier 单独负责，两者不叠加。
_GAS_LIMIT_BUFFER = 1.25


@dataclass(slots=True)
class PlannedTask:
    account_id: int
    address: str
    transistors: str
    token_id: int
    amount: int
    value_wei: int
    note: str | None = None


@dataclass(slots=True)
class RunReport:
    job_id: int
    total: int = 0
    confirmed: int = 0
    failed: int = 0
    skipped: int = 0
    notes: list[str] = field(default_factory=list)


class MintEngine:
    def __init__(self, db: Database, settings: Settings, password: str | None = None) -> None:
        self.db = db
        self.settings = settings
        self._password = password

    # ----------------------------------------------------------------- 规划
    def build_plan(self, job: JobIn) -> tuple[list[PlannedTask], list[str]]:
        """把任务定义展开为逐账号任务，并绑定当前链上单价。"""
        w3 = make_web3(self.settings)
        accounts = self.db.list_accounts(
            group=job.group, account_ids=job.account_ids, enabled_only=True
        )
        if not accounts:
            raise ValueError("没有可用账号；先导入账号或检查筛选条件")

        notes: list[str] = []
        states: dict[str, Any] = {}
        for target in job.targets:
            key = target.transistors.lower()
            if key not in states:
                try:
                    states[key] = read_transistor_state(w3, target.transistors)
                except Exception as exc:
                    # 读不到链上额度时明确报错，避免把「未知」当成「可铸」放行。
                    # 用 ChainReadError 让 Web 层能把它归为「上游读取失败」(502)，
                    # 而不是客户端的参数错误 (400)。
                    raise ChainReadError(
                        f"读取 {target.transistors} 链上状态失败: {exc}"
                    ) from exc

        planned: list[PlannedTask] = []
        seen: set[tuple[str, int]] = set()
        # 全局 supplyCap 是同一合约下所有 token 共享的预算：多目标（如 NAND+LATCH）
        # 若各自按「总剩余」封顶，合计会突破总上限。这里按合约累计已规划数量，
        # 规划后续目标时从全局剩余里扣除，保证跨目标合计不超过 supplyCap。
        supply_used: dict[str, int] = {}
        for target in job.targets:
            # 同一 (合约, tokenId) 重复出现时只保留一份：任务表对
            # (job_id, account_id, transistors, token_id) 有唯一约束，重复行会被
            # 静默去重，若这里不先去重，预览的数量会与实际落库结果不一致。
            dedupe_key = (target.transistors.lower(), target.token_id)
            if dedupe_key in seen:
                notes.append(
                    f"{target.transistors} tokenId={target.token_id} 重复出现，已忽略"
                )
                continue
            seen.add(dedupe_key)
            address_key = target.transistors.lower()
            state = states[address_key]
            consumed = supply_used.get(address_key, 0)
            remaining = state.remaining(target.token_id, supply_consumed=consumed)
            if remaining is not None and remaining <= 0:
                notes.append(
                    f"{target.transistors} tokenId={target.token_id} "
                    f"已售罄/已达上限，跳过"
                )
                continue
            # 剩余额度是「整批任务共享」的预算，必须跨账号合计扣减：
            # 只按单个账号封顶会让总量超过 typeCap，多出来的交易必然 revert、白白烧 gas。
            # None 表示该类型未单独设限（typeCap=0），此时不封顶。
            budget = remaining
            planned_here = 0
            for account in accounts:
                amount = target.amount if budget is None else min(target.amount, budget)
                if amount <= 0:
                    break
                planned.append(
                    PlannedTask(
                        account_id=account.id,
                        address=account.address,
                        transistors=state.address,
                        token_id=target.token_id,
                        amount=amount,
                        value_wei=state.cost_wei(amount),
                        note=target.label,
                    )
                )
                planned_here += amount
                if budget is not None:
                    budget -= amount
            # 每个已规划的 mint 都会占用一份全局供应额度，无论它是否受 typeCap 约束，
            # 因此这里统一累计，供同一合约的后续目标扣减。
            supply_used[address_key] = consumed + planned_here
            if remaining is not None and planned_here < target.amount * len(accounts):
                notes.append(
                    f"{target.transistors} tokenId={target.token_id} 剩余 {remaining}，"
                    f"本次合计安排 {planned_here} 笔（按剩余额度封顶）"
                )
        if not planned:
            raise ValueError("没有可执行的铸造任务（目标可能都已到达上限）")
        return planned, notes

    def create_job(self, job: JobIn) -> tuple[int, list[str]]:
        planned, notes = self.build_plan(job)
        job_id = self.db.create_job(
            job.name,
            job.model_dump(mode="json"),
            dry_run=job.dry_run,
        )
        self.db.add_tasks(
            job_id,
            [
                {
                    "account_id": t.account_id,
                    "transistors": t.transistors,
                    "token_id": t.token_id,
                    "amount": t.amount,
                    "value_wei": t.value_wei,
                }
                for t in planned
            ],
        )
        for note in notes:
            self.db.log_event(note, level="warn", job_id=job_id)
        return job_id, notes

    # ----------------------------------------------------------------- 执行
    def run_job(self, job_id: int) -> RunReport:
        # 非阻塞抢占执行闸门：抢不到说明本进程已有任务在跑，直接让调用方稍后再来，
        # 避免两个任务在同一账号上并发广播造成 nonce 冲突。
        if not _ENGINE_RUN_LOCK.acquire(blocking=False):
            raise ValueError("本进程已有其他任务正在执行，请等待其结束后再试")
        try:
            return self._run_job(job_id)
        finally:
            _ENGINE_RUN_LOCK.release()

    def _run_job(self, job_id: int) -> RunReport:
        job = self.db.get_job(job_id)
        if not job:
            raise ValueError(f"任务 {job_id} 不存在")
        if job.status == JobStatus.RUNNING:
            # 走到这里说明本进程没有正在跑的任务（执行闸门已持有），
            # 该 running 只是上次进程崩溃 / 被强制中断留下的残留状态。
            # 允许重跑：已广播（sent）的任务只做 tx_hash 对账、不会重复广播，
            # 未广播（pending）的任务才会真正发送。
            log.warning("任务 %s 处于 running 残留状态，按上次中断重新对账执行", job_id)

        report = RunReport(job_id=job_id)
        self.db.set_job_status(job_id, JobStatus.RUNNING)

        tasks = self.db.list_tasks(
            job_id=job_id,
            statuses=[TaskStatus.PENDING, TaskStatus.SENT],
            limit=100_000,
        )
        report.total = len(tasks)
        if not tasks:
            self.db.set_job_status(job_id, JobStatus.DONE)
            report.notes.append("没有待执行任务")
            return report

        if job.dry_run:
            for task in tasks:
                self.db.mark_task_failed(
                    task.id, "dry-run: 未广播", status=TaskStatus.SKIPPED
                )
            self.db.set_job_status(job_id, JobStatus.DONE)
            report.skipped = len(tasks)
            report.notes.append("dry-run 模式，仅生成任务未广播")
            return report

        # 每个账号线程都会用到口令；在这里统一解析一次，避免并发场景下重复交互输入
        self._password = resolve_password(self._password)

        # 执行前读取链上剩余额度：已售罄/超上限的目标给出明确提示并直接跳过，
        # 避免把「已售罄」误报成「预估 gas 失败」。
        # 注意：仅对尚未广播（pending）的任务做售罄判断；已广播（sent）的任务
        # 可能已经上链，必须走对账流程，绝不能因为当前额度为 0 就判为跳过。
        remaining = self._remaining_by_target(tasks)
        runnable: list[Any] = []
        for task in tasks:
            if task.status == TaskStatus.PENDING:
                rem = remaining.get((task.transistors.lower(), task.token_id))
                if rem is not None and rem <= 0:
                    message = (
                        f"已售罄/已达上限（remaining=0），跳过："
                        f"{task.transistors} id={task.token_id}"
                    )
                    self.db.mark_task_failed(
                        task.id, message, status=TaskStatus.SKIPPED
                    )
                    self.db.log_event(
                        message, level="warn", job_id=job_id, task_id=task.id
                    )
                    report.skipped += 1
                    continue
            runnable.append(task)

        if not runnable:
            self.db.set_job_status(job_id, JobStatus.DONE)
            report.notes.append("所有目标均已售罄或到达上限，未广播任何交易")
            return report

        # 按账号分组，保证同一账号 nonce 顺序
        by_account: dict[int, list[Any]] = {}
        for task in runnable:
            by_account.setdefault(task.account_id, []).append(task)

        total_value = sum(t.value_wei for t in runnable)
        report.notes.append(f"待铸造 {len(runnable)} 笔，合计 {total_value} wei")

        lock = threading.Lock()
        with ThreadPoolExecutor(max_workers=min(self.settings.concurrency, len(by_account))) as pool:
            futures = {
                pool.submit(
                    self._run_account_tasks,
                    account_id,
                    account_tasks,
                    job.stop_on_first_failure,
                ): account_id
                for account_id, account_tasks in by_account.items()
            }
            for future in as_completed(futures):
                account_id = futures[future]
                try:
                    confirmed, failed, skipped, notes = future.result()
                except Exception as exc:  # 单账号异常不影响其他账号
                    log.exception("账号 %s 执行异常", account_id)
                    with lock:
                        report.failed += len(by_account[account_id])
                    self.db.log_event(
                        f"账号 {account_id} 异常: {exc}", level="error", job_id=job_id
                    )
                    continue
                with lock:
                    report.confirmed += confirmed
                    report.failed += failed
                    report.skipped += skipped
                    report.notes.extend(notes)

        status = JobStatus.DONE
        if report.failed and not report.confirmed:
            status = JobStatus.FAILED
        self.db.set_job_status(job_id, status)
        return report

    def _remaining_by_target(self, tasks: list[Any]) -> dict[tuple[str, int], int | None]:
        """读取每个 pending 目标当前的链上剩余额度；读取失败按未知（None）处理。

        只对 pending 任务取状态，避免对已广播交易产生额外 RPC 开销。
        """
        wanted = [t for t in tasks if t.status == TaskStatus.PENDING]
        if not wanted:
            return {}
        w3 = make_web3(self.settings)
        cache: dict[str, Any] = {}
        out: dict[tuple[str, int], int | None] = {}
        for task in wanted:
            key = task.transistors.lower()
            if key not in cache:
                try:
                    cache[key] = read_transistor_state(w3, task.transistors)
                except Exception as exc:  # RPC 抖动时不下发售罄结论，交给后续预估兜底
                    log.warning("读取 %s 链上状态失败: %s", task.transistors, exc)
                    cache[key] = None
            state = cache[key]
            out[(key, task.token_id)] = (
                state.remaining(task.token_id) if state is not None else None
            )
        return out

    def _run_account_tasks(
        self,
        account_id: int,
        tasks: list[Any],
        stop_on_first_failure: bool = False,
    ) -> tuple[int, int, int, list[str]]:
        db = self.db
        settings = self.settings
        w3 = make_web3(settings)

        cipher = db.get_cipher(account_id)
        if cipher is None:
            raise ValueError(f"账号 {account_id} 不存在")
        password = self._password or resolve_password(None)
        private_key = decrypt_private_key(cipher, password)
        account = w3.eth.account.from_key(private_key)
        address = account.address

        confirmed = failed = skipped = 0
        notes: list[str] = []
        nonce = int(w3.eth.get_transaction_count(address, "pending"))
        gas_price = self._gas_price(w3)

        for task in tasks:
            # 已广播但未确认的交易：只做链上对账，绝不重复广播，避免重复扣款。
            if task.status == TaskStatus.SENT and task.tx_hash:
                outcome = self._reconcile_sent(w3, db, task)
                if outcome == "confirmed":
                    confirmed += 1
                elif outcome == "failed":
                    failed += 1
                    if stop_on_first_failure:
                        break
                else:
                    skipped += 1
                notes.append(
                    f"{address} 完成 {confirmed + failed + skipped}/{len(tasks)}"
                )
                continue

            # 余额预检：节点在 estimate_gas 阶段就会因「gas*price+value 超过余额」
            # 直接拒绝（-32003 insufficient funds）。若先估算，会把「余额不足」
            # 误报成「预估失败 / 合约拒绝」。这里先用保守 gas 预算拦一道，
            # 让缺钱账号落到 skipped，而不是 failed。
            balance = int(w3.eth.get_balance(address))
            prelim_budget = int(settings.gas_limit or 0) or _PRELIM_GAS_FLOOR
            if balance < task.value_wei + prelim_budget * gas_price:
                reason = (
                    f"余额不足: {balance} wei < 需 {task.value_wei} + "
                    f"gas≥{prelim_budget * gas_price} wei"
                )
                db.mark_task_failed(task.id, reason, status=TaskStatus.SKIPPED)
                db.log_event(reason, level="warn", job_id=task.job_id, task_id=task.id)
                skipped += 1
                continue

            # 预估 gas：金额校验与交易 gas 上限共用同一数值，避免固定预算造成误判。
            try:
                gas_limit = int(settings.gas_limit or 0) or self._estimate_gas(
                    w3, task, address
                )
            except Exception as exc:
                if "insufficient funds" in str(exc).lower():
                    reason = f"余额不足以支付 gas/金额（节点预估拒绝）: {exc}"
                    db.mark_task_failed(task.id, reason, status=TaskStatus.SKIPPED)
                    db.log_event(reason, level="warn", job_id=task.job_id, task_id=task.id)
                    skipped += 1
                    continue
                message = (
                    f"链上预估失败（可能已售罄/超限、合约拒绝或参数非法）: {exc}"
                )
                db.mark_task_failed(task.id, message)
                db.log_event(message, level="error", job_id=task.job_id, task_id=task.id)
                failed += 1
                if stop_on_first_failure:
                    break
                continue

            gas_budget = gas_limit * gas_price
            if balance < task.value_wei + gas_budget:
                reason = (
                    f"余额不足: {balance} wei < 需 {task.value_wei} + gas≈{gas_budget} wei"
                )
                db.mark_task_failed(task.id, reason, status=TaskStatus.SKIPPED)
                db.log_event(reason, level="warn", job_id=task.job_id, task_id=task.id)
                skipped += 1
                continue

            # 广播：按 max_retries 重试；广播失败可能已消耗 nonce，重试前与链上重新对齐。
            tx_hash: str | None = None
            used_nonce = nonce
            last_error: Exception | None = None
            for _attempt in range(settings.max_retries + 1):
                try:
                    tx_hash, used_nonce = self._send_mint(
                        w3, private_key, address, task, nonce, gas_price, gas_limit
                    )
                    break
                except Exception as exc:
                    last_error = exc
                    nonce = int(
                        w3.eth.get_transaction_count(address, "pending")
                    )
            if tx_hash is None:
                message = (
                    f"广播失败（已重试 {settings.max_retries} 次）: {last_error}"
                )
                db.mark_task_failed(task.id, message)
                db.log_event(message, level="error", job_id=task.job_id, task_id=task.id)
                failed += 1
                if stop_on_first_failure:
                    break
                continue

            db.mark_task_sent(task.id, nonce=used_nonce, tx_hash=tx_hash)
            nonce = used_nonce + 1
            try:
                receipt = w3.eth.wait_for_transaction_receipt(
                    tx_hash, timeout=settings.tx_timeout
                )
            except Exception as exc:
                # 超时不等于失败：交易可能仍在链上 pending。保留 SENT 状态并记录提示，
                # 下次重跑会按 tx_hash 对账，不会重复广播。
                message = f"等待回执超时，交易可能仍在链上，可重跑对账: {exc}"
                db.mark_task_unconfirmed(task.id, message)
                db.log_event(message, level="warn", job_id=task.job_id, task_id=task.id)
                skipped += 1
                continue

            if receipt["status"] == 1:
                db.mark_task_confirmed(
                    task.id,
                    block_number=int(receipt["blockNumber"]),
                    gas_used=int(receipt["gasUsed"]),
                    tx_hash=tx_hash,
                )
                db.log_event(
                    f"mint 成功 {task.transistors} id={task.token_id} x{task.amount}",
                    job_id=task.job_id,
                    task_id=task.id,
                )
                confirmed += 1
            else:
                message = "交易回执 status=0（链上 revert）"
                db.mark_task_failed(task.id, message, tx_hash=tx_hash)
                db.log_event(message, level="error", job_id=task.job_id, task_id=task.id)
                failed += 1
                if stop_on_first_failure:
                    break
            notes.append(f"{address} 完成 {confirmed + failed + skipped}/{len(tasks)}")
        return confirmed, failed, skipped, notes

    def _reconcile_sent(self, w3: Web3, db: Database, task: Any) -> str:
        """对账一条已广播任务，返回 confirmed/failed/pending。

        只要存在 tx_hash 就绝不重新签名广播：命中回执则更新状态，
        未命中则保守地保留 SENT 等待下次对账。
        """
        tx_hash = task.tx_hash
        receipt = None
        try:
            receipt = w3.eth.get_transaction_receipt(tx_hash)
        except Exception:  # 未上链（TransactionNotFound）或节点暂时查不到
            receipt = None
        if receipt is not None:
            if int(receipt["status"]) == 1:
                db.mark_task_confirmed(
                    task.id,
                    block_number=int(receipt["blockNumber"]),
                    gas_used=int(receipt["gasUsed"]),
                    tx_hash=tx_hash,
                )
                db.log_event(
                    f"对账确认 {tx_hash}", job_id=task.job_id, task_id=task.id
                )
                return "confirmed"
            db.mark_task_failed(
                task.id, "对账发现链上 revert（status=0）", tx_hash=tx_hash
            )
            db.log_event(
                f"对账发现链上 revert {tx_hash}",
                level="error",
                job_id=task.job_id,
                task_id=task.id,
            )
            return "failed"
        message = "尚未确认，保留 SENT 状态；重跑继续对账，不会重复广播"
        db.mark_task_unconfirmed(task.id, message)
        db.log_event(
            f"对账未命中回执 {tx_hash}：{message}",
            level="warn",
            job_id=task.job_id,
            task_id=task.id,
        )
        return "pending"

    def _estimate_gas(self, w3: Web3, task: Any, address: str) -> int:
        contract = transistors_contract(w3, task.transistors)
        fn = contract.functions.mint(task.token_id, task.amount)
        estimate = fn.estimate_gas({"from": address, "value": task.value_wei})
        return int(estimate * _GAS_LIMIT_BUFFER)

    def _gas_price(self, w3: Web3) -> int:
        if self.settings.gas_price_gwei > 0:
            base = Web3.to_wei(self.settings.gas_price_gwei, "gwei")
        else:
            base = int(w3.eth.gas_price)
        return int(base * self.settings.gas_multiplier)

    def _send_mint(
        self,
        w3: Web3,
        private_key: str,
        address: str,
        task: Any,
        nonce: int,
        gas_price: int,
        gas_limit: int,
    ) -> tuple[str, int]:
        contract = transistors_contract(w3, task.transistors)
        fn = contract.functions.mint(task.token_id, task.amount)
        tx_params: dict[str, Any] = {
            "from": address,
            "value": task.value_wei,
            "nonce": nonce,
            "chainId": self.settings.chain_id,
            "gasPrice": gas_price,
            "gas": gas_limit,
        }
        tx = fn.build_transaction(tx_params)
        signed = w3.eth.account.sign_transaction(tx, private_key)
        raw = getattr(signed, "raw_transaction", None) or getattr(signed, "rawTransaction")
        tx_hash = w3.eth.send_raw_transaction(raw)
        return tx_hash.hex(), nonce
