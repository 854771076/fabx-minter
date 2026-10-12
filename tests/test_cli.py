"""CLI 参数解析：非法输入应给出明确提示，而不是抛裸异常。"""

from __future__ import annotations

import pytest
import typer

from fabx_minter.cli import _parse_ids


def test_parse_ids_accepts_comma_or_space() -> None:
    assert _parse_ids(None) is None
    assert _parse_ids("") is None
    assert _parse_ids("1, 2 3") == [1, 2, 3]


def test_parse_ids_rejects_non_integer() -> None:
    with pytest.raises(typer.BadParameter):
        _parse_ids("abc")
