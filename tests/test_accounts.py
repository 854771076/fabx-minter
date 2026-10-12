"""账号导入工具：文本解析与地址派生。"""

from __future__ import annotations

import pytest

from fabx_minter.accounts import import_many, parse_key_lines, private_key_to_address
from fabx_minter.db import Database


def test_parse_key_lines() -> None:
    content = """
    0xaaaa
    bbbb,钱包B
    # 这是注释

    cccc , 带空格
    """
    assert parse_key_lines(content) == [
        ("0xaaaa", None),
        ("bbbb", "钱包B"),
        ("cccc", "带空格"),
    ]


def test_private_key_validation() -> None:
    with pytest.raises(Exception):
        private_key_to_address("0x1234")  # 长度不足
    with pytest.raises(Exception):
        private_key_to_address("zz" * 32)  # 非十六进制


def test_address_is_deterministic() -> None:
    key = "11" * 32
    assert private_key_to_address(key) == private_key_to_address("0x" + key)
    assert len(private_key_to_address(key)) == 42
    # 不同私钥必须派生不同地址
    assert private_key_to_address(key) != private_key_to_address("22" * 32)


def test_known_address_vector() -> None:
    # 已知向量：私钥全 0x11 派生出的地址，用于锁定派生逻辑不被改动
    assert (
        private_key_to_address("11" * 32)
        == "0x19E7E376E7C213B7E7e7e46cc70A5dD086DAff2A"
    )


def test_import_many_reports_line_errors(db: Database) -> None:
    content = "\n".join(["11" * 32, "not-a-key", "22" * 32 + ",acc2"])
    imported, errors = import_many(db, content, "pw", group="g1")
    assert len(imported) == 2
    assert len(errors) == 1
    assert "第 2 条" in errors[0]
    assert db.list_accounts()[0].group == "g1"


def test_import_many_label_prefix_ignores_comments(db: Database) -> None:
    # 注释与空行不计入序号，label_prefix-N 必须连续，避免出现 acc-1、acc-3 这类跳号
    content = "\n".join(["# 表头", "", "11" * 32, "22" * 32, "33" * 32])
    imported, errors = import_many(db, content, "pw", label_prefix="acc")
    assert errors == []
    assert [a.label for a in imported] == ["acc-1", "acc-2", "acc-3"]
