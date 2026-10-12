"""资金分发：入口参数校验在触网之前完成，非法私钥归一为请求错误。"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from web3 import Web3

from fabx_minter.config import Settings
from fabx_minter.crypto import encrypt_private_key
from fabx_minter.db import Database
from fabx_minter.funding import distribute_funds
from fabx_minter.models import FundRequest

MASTER_KEY = "11" * 32
MASTER_ADDRESS = Web3().eth.account.from_key(MASTER_KEY).address
ACCOUNT_KEY = "22" * 32
ACCOUNT_ADDRESS = Web3().eth.account.from_key(ACCOUNT_KEY).address


class _FakeEth:
    """最小化的 eth 命名空间替身：用于验证「广播成功但回执超时」的分支。"""

    account = Web3().eth.account

    def __init__(self, receipt_error: Exception | None = None) -> None:
        self.gas_price = 1_000_000_000
        self._receipt_error = receipt_error
        self.broadcast: list[bytes] = []

    def get_transaction_count(self, address: str, block: str = "latest") -> int:
        return 0

    def get_balance(self, address: str, block: str = "latest") -> int:
        # 主钱包充足，目标账号为空，确保不会在余额预检阶段被跳过
        return 10**18 if address.lower() == MASTER_ADDRESS.lower() else 0

    def estimate_gas(self, tx: dict) -> int:
        return 21_000

    def send_raw_transaction(self, raw: bytes) -> bytes:
        self.broadcast.append(raw)
        return b"\xab" * 32

    def wait_for_transaction_receipt(self, tx_hash, timeout: int = 0) -> dict:
        raise self._receipt_error or Exception("unexpected")


def test_distribute_funds_rejects_invalid_master_key(
    db: Database, settings: Settings
) -> None:
    # 长度合法但取值非法（全 0）的私钥应在发起任何 RPC 之前就被拒绝
    req = FundRequest(master_private_key="0" * 64, amount_wei=10**15)
    with pytest.raises(ValueError, match="主钱包私钥无效"):
        distribute_funds(db, settings, req)


def test_distribute_funds_marks_broadcast_timeout_as_pending(
    db: Database, settings: Settings, monkeypatch
) -> None:
    """广播已成功但回执超时时必须记为 pending，绝不能当成 failed 诱导重复转账。"""
    account_id = db.add_account(
        ACCOUNT_ADDRESS, encrypt_private_key(ACCOUNT_KEY, "pw"), label=None, group=None
    )
    eth = _FakeEth(receipt_error=Exception("timeout"))
    monkeypatch.setattr(
        "fabx_minter.funding.make_web3", lambda s: SimpleNamespace(eth=eth)
    )
    req = FundRequest(
        master_private_key=MASTER_KEY, amount_wei=10**15, account_ids=[account_id]
    )

    result = distribute_funds(db, settings, req)

    assert result.pending == 1
    assert result.failed == 0
    assert result.sent == 0
    item = result.items[0]
    assert item.status == "pending"
    assert item.tx_hash  # 保留 tx_hash 供用户核对
    assert len(eth.broadcast) == 1  # 只广播一次，绝不重发
