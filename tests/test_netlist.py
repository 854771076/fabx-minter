"""网表编解码、本地评估与预估口径的纯计算测试（不访问网络）。"""

from __future__ import annotations

import pytest

from fabx_minter.netlist import (
    NetlistError,
    decode_netlist,
    encode_gates,
    evaluate,
    netlist_stats,
    preview_netlist,
    vector_ok,
)


def test_encode_decode_roundtrip() -> None:
    gates = [["NAND", 2, 3], ["LATCH", 4], ["NAND", 5, 1]]
    netlist = encode_gates(gates)
    decoded = decode_netlist(netlist)
    assert [(g.op, g.args) for g in decoded] == [
        ("NAND", (2, 3)),
        ("LATCH", (4,)),
        ("NAND", (5, 1)),
    ]
    stats = netlist_stats(netlist)
    assert stats == {"nand": 2, "latch": 1, "ref": 0}


def test_encode_rejects_ref_and_bad_shape() -> None:
    # REF 电路不参与挖矿，编码阶段就应拒绝，避免白花 tapeout 费
    with pytest.raises(NetlistError):
        encode_gates([["REF", 1]])
    with pytest.raises(NetlistError):
        encode_gates([["NAND", 2]])
    with pytest.raises(NetlistError):
        encode_gates([["XOR", 2, 3]])
    with pytest.raises(NetlistError):
        encode_gates([["NAND", 2, 1 << 25]])


def test_decode_rejects_truncated_and_unknown() -> None:
    with pytest.raises(NetlistError):
        decode_netlist(b"\x00\x00\x00")  # NAND 缺少操作数
    with pytest.raises(NetlistError):
        decode_netlist(b"\x09\x00\x00\x00\x00\x00\x00")


def test_evaluate_inverter_matches_task1_vectors() -> None:
    # 任务 1 是单比特反相器：NAND(x, x) = NOT x，输入 1 -> 输出 0，输入 0 -> 输出 1
    netlist = encode_gates([["NAND", 2, 2]])
    assert evaluate(netlist, n_in=1, n_out=1, cycles=1, input_data=b"\x01") == b"\x00"
    assert evaluate(netlist, n_in=1, n_out=1, cycles=1, input_data=b"\x00") == b"\x01"
    assert vector_ok(
        netlist, n_in=1, n_out=1, cycles=1, input_hex="0x01", expected_hex="0x00"
    )
    assert not vector_ok(
        netlist, n_in=1, n_out=1, cycles=1, input_hex="0x01", expected_hex="0x01"
    )


def test_evaluate_latch_holds_previous_cycle() -> None:
    # LATCH(2) 抓输入；一拍后输出上一拍存下的值，用来验证跨拍状态位
    netlist = encode_gates([["LATCH", 2]])
    out = evaluate(netlist, n_in=1, n_out=1, cycles=2, input_data=b"\x01\x00")
    assert out == b"\x00\x01"


def test_preview_netlist_matches_frontend_formula() -> None:
    netlist = encode_gates([["NAND", 2, 2]])
    preview = preview_netlist(netlist, n_in=1)
    assert preview["nand"] == 1
    assert preview["latch"] == 0
    assert preview["depth"] == 1
    assert preview["bstar"] == 1
    assert preview["area"] == 1
    assert preview["cost"] == 1

    # LATCH 不增加深度但计入面积：lambdaArea=6 -> area = nand + 7*latch
    netlist = encode_gates([["NAND", 2, 2], ["LATCH", 3]])
    preview = preview_netlist(netlist, n_in=1)
    assert preview["nand"] == 1
    assert preview["latch"] == 1
    assert preview["bstar"] == 2
    assert preview["area"] == 8


def test_preview_rejects_forward_reference() -> None:
    netlist = encode_gates([["NAND", 9, 9]])
    with pytest.raises(NetlistError):
        preview_netlist(netlist, n_in=1)
