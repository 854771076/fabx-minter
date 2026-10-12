"""资金分发：从主钱包向多个账号补充 OKB，用于支付 mint 的 gas 与铸造费。

执行约定：
- 主钱包私钥只在该请求内使用，不落库、不写日志；
- 同一主钱包的 nonce 由本地单调递增维护，逐笔广播后等待回执；
- ``top_up=True`` 表示补足到目标余额（不足才转），``False`` 表示等额追加。
"""

from __future__ import annotations

import logging
from typing import Any

from web3 import Web3

from .chain import make_web3
from .config import Settings
from .db import Database
from .models import FundRequest, FundResult, FundResultItem

log = logging.getLogger("fabx.funding")


def _gas_price(w3: Web3, settings: Settings) -> int:
    base = (
        Web3.to_wei(settings.gas_price_gwei, "gwei")
        if settings.gas_price_gwei > 0
        else int(w3.eth.gas_price)
    )
    return int(base * settings.gas_multiplier)


def _transfer_gas_limit(
    w3: Web3, settings: Settings, sender: str, recipient: str, value: int
) -> int:
    """确定原生转账的 gas 上限。

    优先用链上估算（对带 data 的收款合约更稳妥），保留 21,000 作为兜底，
    避免某些节点不提供 estimate_gas 时整个分发流程不可用。
    """
    if settings.gas_limit:
        return int(settings.gas_limit)
    try:
        estimate = int(
            w3.eth.estimate_gas(
                {"from": sender, "to": recipient, "value": value}
            )
        )
        return int(estimate * settings.gas_multiplier)
    except Exception as exc:  # pragma: no cover - 依赖具体节点行为
        log.warning("估算转账 gas 失败，回退 21000: %s", exc)
        return 21_000


def distribute_funds(
    db: Database, settings: Settings, req: FundRequest
) -> FundResult:
    """按请求向账号分发 OKB；返回逐账号明细。"""
    w3 = make_web3(settings)
    master_key = req.master_private_key.strip()
    if master_key.startswith(("0x", "0X")):
        master_key = master_key[2:]
    try:
        master = w3.eth.account.from_key(master_key)
    except Exception as exc:
        # 私钥非法属于「请求有误」，归一成 ValueError 让接口返回 400 而不是 502
        raise ValueError("主钱包私钥无效，请检查是否粘贴完整") from exc
    master_address = master.address

    result = FundResult(master_address=master_address)

    accounts = db.list_accounts(account_ids=req.account_ids, enabled_only=True)
    if not accounts:
        raise ValueError("没有可用的目标账号")

    gas_price = _gas_price(w3, settings)

    nonce = int(w3.eth.get_transaction_count(master_address, "pending"))
    master_balance = int(w3.eth.get_balance(master_address))
    result.notes.append(
        f"主钱包 {master_address} 余额 {master_balance} wei，gas 单价 {gas_price} wei"
    )

    for account in accounts:
        target = Web3.to_checksum_address(account.address)
        current = int(w3.eth.get_balance(target))
        if req.top_up:
            amount = max(0, req.amount_wei - current)
        else:
            amount = req.amount_wei

        if amount <= 0:
            result.skipped += 1
            result.items.append(
                FundResultItem(
                    account_id=account.id,
                    address=target,
                    amount_wei=0,
                    status="skipped",
                    error="余额已达标",
                )
            )
            continue

        if req.dry_run:
            result.skipped += 1
            result.items.append(
                FundResultItem(
                    account_id=account.id,
                    address=target,
                    amount_wei=amount,
                    status="dry-run",
                )
            )
            continue

        # 每笔转账按实际目标金额估算 gas，比固定 21,000 更贴近真实消耗
        gas_limit = _transfer_gas_limit(w3, settings, master_address, target, amount)
        gas_cost = gas_limit * gas_price
        if master_balance < amount + gas_cost:
            reason = f"主钱包余额不足以再转 {amount} wei（含 gas≈{gas_cost}）"
            result.failed += 1
            result.items.append(
                FundResultItem(
                    account_id=account.id,
                    address=target,
                    amount_wei=amount,
                    status="failed",
                    error=reason,
                )
            )
            db.log_event(f"资金分发失败 {target}: {reason}", level="error")
            continue

        try:
            tx: dict[str, Any] = {
                "from": master_address,
                "to": target,
                "value": amount,
                "nonce": nonce,
                "chainId": settings.chain_id,
                "gas": gas_limit,
                "gasPrice": gas_price,
            }
            signed = w3.eth.account.sign_transaction(tx, master_key)
            raw = getattr(signed, "raw_transaction", None) or getattr(
                signed, "rawTransaction"
            )
            raw_hash = w3.eth.send_raw_transaction(raw)
        except Exception as exc:
            # 广播本身失败：交易未进入内存池，nonce 可能仍被占用，重新与链上同步，
            # 避免后续交易因 nonce 卡住而全部失败。
            message = str(exc)[:500]
            result.failed += 1
            result.items.append(
                FundResultItem(
                    account_id=account.id,
                    address=target,
                    amount_wei=amount,
                    status="failed",
                    error=message,
                )
            )
            db.log_event(f"资金分发广播失败 {target}: {message}", level="error")
            nonce = int(w3.eth.get_transaction_count(master_address, "pending"))
            continue

        # 广播已成功：这笔交易已经进入内存池，之后无论回执如何都不能再用同一 nonce 重发
        nonce += 1
        tx_hash = raw_hash
        try:
            receipt = w3.eth.wait_for_transaction_receipt(
                tx_hash, timeout=settings.tx_timeout
            )
        except Exception as exc:
            # 超时不等于失败：资金可能已经到账。保守记为 pending 供用户自行核对，
            # 绝不归入 failed，否则用户可能据此重发而重复转账。
            message = str(exc)[:500]
            result.pending += 1
            result.items.append(
                FundResultItem(
                    account_id=account.id,
                    address=target,
                    amount_wei=amount,
                    status="pending",
                    tx_hash=tx_hash.hex(),
                    error=f"已广播但未在超时内确认，请核对交易: {message}",
                )
            )
            db.log_event(
                f"资金分发待确认 {target} {tx_hash.hex()}: {message}", level="warn"
            )
            continue

        if receipt["status"] != 1:
            reason = "回执 status=0（链上 revert）"
            result.failed += 1
            result.items.append(
                FundResultItem(
                    account_id=account.id,
                    address=target,
                    amount_wei=amount,
                    status="failed",
                    tx_hash=tx_hash.hex(),
                    error=reason,
                )
            )
            db.log_event(f"资金分发 revert {target}", level="error")
            # revert 的交易同样消耗 gas，记为已花费，避免高估主钱包余额而继续超额分发
            master_balance -= gas_cost
            continue

        master_balance -= amount + gas_cost
        result.sent += 1
        result.total_wei += amount
        result.items.append(
            FundResultItem(
                account_id=account.id,
                address=target,
                amount_wei=amount,
                status="confirmed",
                tx_hash=tx_hash.hex(),
            )
        )
        db.log_event(f"已向 {target} 转入 {amount} wei")

    result.notes.append(
        f"完成：成功 {result.sent}，跳过 {result.skipped}，失败 {result.failed}，"
        f"待确认 {result.pending}，合计转出 {result.total_wei} wei"
    )
    return result
