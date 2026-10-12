"""账号导入：把私钥加密后写入 SQLite，地址由私钥派生。"""

from __future__ import annotations

from web3 import Web3

from .crypto import encrypt_private_key
from .db import Database
from .models import AccountIn, AccountOut


def private_key_to_address(private_key: str) -> str:
    """从私钥派生 checksum 地址；私钥格式在此统一规整。"""
    key = AccountIn(private_key=private_key).private_key
    return Web3.to_checksum_address(Web3().eth.account.from_key(key).address)


def import_account(
    db: Database,
    private_key: str,
    password: str,
    *,
    label: str | None = None,
    group: str | None = None,
) -> AccountOut:
    """导入单个账号；地址重复时更新密文与备注。"""
    normalized = AccountIn(private_key=private_key)
    address = private_key_to_address(normalized.private_key)
    cipher = encrypt_private_key(normalized.private_key, password)
    account_id = db.add_account(
        address,
        cipher,
        label=normalized.label or label,
        group=normalized.group or group,
    )
    account = db.get_account(account_id)
    assert account is not None  # add_account 后必然存在
    return account


def parse_key_lines(content: str) -> list[tuple[str, str | None]]:
    """解析导入文本：每行 ``私钥`` 或 ``私钥,标签``；忽略空行与 # 注释。"""
    out: list[tuple[str, str | None]] = []
    for raw in content.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if "," in line:
            key, label = line.split(",", 1)
            out.append((key.strip(), label.strip() or None))
        else:
            out.append((line, None))
    return out


def import_many(
    db: Database,
    content: str,
    password: str,
    *,
    label_prefix: str | None = None,
    group: str | None = None,
) -> tuple[list[AccountOut], list[str]]:
    """批量导入，返回 (成功列表, 错误说明列表)。单条失败不影响其余导入。

    ``index`` 统计的是「有效账号条目」的序号（空行与 # 注释不计数），
    因此 ``label_prefix-N`` 与错误提示中的序号始终连续，不会因注释而跳号。
    """
    imported: list[AccountOut] = []
    errors: list[str] = []
    for index, (key, label) in enumerate(parse_key_lines(content), start=1):
        merged_label = label
        if merged_label is None and label_prefix:
            merged_label = f"{label_prefix}-{index}"
        try:
            imported.append(
                import_account(
                    db, key, password, label=merged_label, group=group
                )
            )
        except Exception as exc:
            # 用「条」而非「行」：序号按有效账号计，与文件物理行号未必一致。
            errors.append(f"第 {index} 条导入失败: {exc}")
    return imported, errors
