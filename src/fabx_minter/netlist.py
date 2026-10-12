"""PoD 电路网表（netlist）编解码与本地评估。

逆向自 fabx.tech PoD 挖矿页打包产物 ``pod-B8Ab4M7D.js``：

- 网表是字节流，每个门一条指令；
- NAND：opcode ``0x00`` + 两个 3 字节大端信号号（共 7 字节），输出 ``NOT(a AND b)``；
- LATCH：opcode ``0x01`` + 一个 3 字节大端信号号（共 4 字节），输出「上一拍存下的值」；
- REF：opcode ``0x02``，引用别的电路；**含 REF 的电路不参与挖矿**，这里直接拒绝。

信号编号约定：

| 信号 | 含义 |
| --- | --- |
| 0 | 常量 0 |
| 1 | 常量 1 |
| 2 … nIn+1 | 第 i 个输入 = 信号 2+i |
| nIn+2+k | 第 k 个门的输出 |

电路输出取**最后 nOut 个信号**，顺序即输出引脚顺序。

本模块只做纯计算（编解码 / 评估 / 预估 burn 与 cost），不访问网络，
因此可以脱离链上环境单测。
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass

NAND_OP = 0
LATCH_OP = 1
REF_OP = 2

# 与前端 W 常量一致：depth 权重 beta=3，LATCH 折算系数 lambda=6，burn 系数 lambdaBurn=1。
LAMBDA_BURN = 1
LAMBDA_AREA = 6
BETA = 3

_SIGNAL_BYTES = 3
_SIGNAL_MAX = 1 << (8 * _SIGNAL_BYTES)


class NetlistError(ValueError):
    """网表非法（未知 opcode、信号越界或结构不完整）。"""


def _u24(netlist: bytes, pos: int) -> int:
    """读一个 3 字节大端信号号；越界直接报错而不是静默读 0。"""
    if pos + _SIGNAL_BYTES > len(netlist):
        raise NetlistError("网表在读取信号号时被截断")
    return (netlist[pos] << 16) | (netlist[pos + 1] << 8) | netlist[pos + 2]


def _pack_u24(value: int) -> bytes:
    if not 0 <= value < _SIGNAL_MAX:
        raise NetlistError(f"信号号 {value} 超出 3 字节范围")
    return value.to_bytes(_SIGNAL_BYTES, "big")


@dataclass(frozen=True, slots=True)
class Gate:
    """一个门：``op`` 为 ``nand`` / ``latch``，``args`` 为信号号。"""

    op: str
    args: tuple[int, ...]


def encode_gates(gates: Iterable[Sequence[object]]) -> bytes:
    """把 ``[["NAND", a, b], ["LATCH", d]]`` 编码成网表字节。

    输入既可来自官方 verify API 的设计格式，也可由本地设计器生成。
    """
    out = bytearray()
    for raw in gates:
        if not raw:
            raise NetlistError("空的门定义")
        op = str(raw[0]).strip().upper()
        if op == "NAND":
            if len(raw) != 3:
                raise NetlistError("NAND 需要恰好两个输入信号")
            out.append(NAND_OP)
            out += _pack_u24(int(raw[1]))
            out += _pack_u24(int(raw[2]))
        elif op == "LATCH":
            if len(raw) != 2:
                raise NetlistError("LATCH 需要恰好一个 d 信号")
            out.append(LATCH_OP)
            out += _pack_u24(int(raw[1]))
        elif op == "REF":
            raise NetlistError("含 REF 的电路不参与挖矿，拒绝编码")
        else:
            raise NetlistError(f"未知的门类型：{op!r}")
    return bytes(out)


def decode_netlist(netlist: bytes) -> list[Gate]:
    """把网表字节解回门列表，遇到非法结构抛出 ``NetlistError``。"""
    data = bytes(netlist)
    gates: list[Gate] = []
    pos = 0
    while pos < len(data):
        op = data[pos]
        pos += 1
        if op == NAND_OP:
            a = _u24(data, pos)
            b = _u24(data, pos + _SIGNAL_BYTES)
            gates.append(Gate("NAND", (a, b)))
            pos += 2 * _SIGNAL_BYTES
        elif op == LATCH_OP:
            d = _u24(data, pos)
            gates.append(Gate("LATCH", (d,)))
            pos += _SIGNAL_BYTES
        elif op == REF_OP:
            # REF 结构：剩余 30 字节 + 3 字节/引用项；这里只用于跳过以校验整体结构。
            if pos + 30 > len(data):
                raise NetlistError("REF 结构被截断")
            count = data[pos + 28]
            pos += 30 + count * _SIGNAL_BYTES
        else:
            raise NetlistError(f"未知 opcode {op}（位置 {pos - 1}）")
    return gates


def netlist_stats(netlist: bytes) -> dict[str, int]:
    """统计门数，并标记是否含 REF（含 REF 即失去挖矿资格）。"""
    nand = latch = 0
    has_ref = False
    for gate in decode_netlist(netlist):
        if gate.op == "NAND":
            nand += 1
        else:
            latch += 1
    # decode 已能识别 REF，这里再独立探测一次，避免调用方漏判。
    pos = 0
    data = bytes(netlist)
    while pos < len(data):
        op = data[pos]
        pos += 1
        if op == NAND_OP:
            pos += 2 * _SIGNAL_BYTES
        elif op == LATCH_OP:
            pos += _SIGNAL_BYTES
        elif op == REF_OP:
            has_ref = True
            count = data[pos + 28]
            pos += 30 + count * _SIGNAL_BYTES
        else:
            raise NetlistError(f"未知 opcode {op}")
    return {"nand": nand, "latch": latch, "ref": 1 if has_ref else 0}


def preview_netlist(netlist: bytes, *, n_in: int) -> dict[str, int]:
    """本地预估 b*（burn 等价）、area、depth 与 cost，口径与官方 ``previewScore`` 一致。

    公式（见官方 skill.md 与前端 ``Ee``）：

    - ``b* = nand + latch``；
    - ``area = nand + 7 * latch``；
    - ``depth`` = 从输入/常量/LATCH 输出到「输出或 LATCH 的 d」的最长 NAND 链；
    - ``cost = area * max(depth, 1) ** 3``。

    需要 ``n_in``：网表本身不携带输入个数，而前 ``2+n_in`` 个信号（常量与输入）
    的深度都记为 0，缺了它无法区分「输入」与「尚未定义的前向引用」。
    """
    data = bytes(netlist)
    gates = decode_netlist(data)
    # 首次出现未知量时先把 REF 拦掉：REF 电路无法用本地模型评估。
    if netlist_stats(data)["ref"]:
        raise NetlistError("含 REF 的电路不参与挖矿，无法预估")

    nand = latch = 0
    # levels[s] 记录信号 s 的 NAND 深度；常量与输入为 0，LATCH 输出重置为 0。
    levels: list[int] = [0] * (2 + max(0, n_in))
    latch_max = 0
    for gate in gates:
        if gate.op == "NAND":
            a, b = gate.args
            depth = 1 + max(_level(levels, a), _level(levels, b))
            levels.append(depth)
            nand += 1
        else:
            (d,) = gate.args
            # LATCH 的 d 允许前向引用（用于搭反馈环）；尚未定义的信号深度按 0 计，
            # 与前端 ``i[l(a)] ?? 0`` 的兜底一致。
            latch_max = max(latch_max, levels[d] if 0 <= d < len(levels) else 0)
            levels.append(0)
            latch += 1

    # 电路没有显式声明 nOut，深度取「所有信号里的最大值」即可覆盖输出；
    # 与前端只在最后 nOut 个信号上取 max 相比，这里更保守（不会低估 depth）。
    depth = max([latch_max, *levels] or [0])
    bstar = nand + LAMBDA_BURN * latch
    area = nand + latch + LAMBDA_AREA * latch
    cost = area * max(depth, 1) ** BETA
    return {
        "nand": nand,
        "latch": latch,
        "depth": depth,
        "bstar": bstar,
        "area": area,
        "cost": cost,
    }


def _level(levels: Sequence[int], signal: int) -> int:
    if signal < 0 or signal >= len(levels):
        raise NetlistError(f"信号 {signal} 尚未定义（必须引用更早的信号）")
    return levels[signal]


def _bit(data: bytes, index: int) -> int:
    return (data[index >> 3] if index >> 3 < len(data) else 0) >> (index & 7) & 1


def eval_step(
    netlist: bytes,
    n_in: int,
    n_out: int,
    latch_state: bytes,
    input_frame: bytes,
) -> tuple[bytes, bytes]:
    """单拍评估：返回 ``(新的 latch 状态, 本拍输出)``。

    与前端 ``Te`` 完全一致：按门顺序求值，LATCH 输出「上一拍存下的值」，
    本拍结束时把各自 ``d`` 信号的当前值写入新状态。
    """
    data = bytes(netlist)
    signals: list[int] = [0, 1]
    for i in range(n_in):
        signals.append(_bit(input_frame, i))

    latch_inputs: list[tuple[int, int]] = []  # (d 信号, 该 latch 的序号)
    latch_index = 0
    pos = 0
    while pos < len(data):
        op = data[pos]
        pos += 1
        if op == NAND_OP:
            a = _u24(data, pos)
            b = _u24(data, pos + _SIGNAL_BYTES)
            pos += 2 * _SIGNAL_BYTES
            signals.append(0 if (_level(signals, a) & _level(signals, b)) else 1)
        elif op == LATCH_OP:
            d = _u24(data, pos)
            pos += _SIGNAL_BYTES
            latch_inputs.append((d, latch_index))
            signals.append(_bit(latch_state, latch_index))
            latch_index += 1
        else:
            raise NetlistError(f"未知 opcode {op}")

    new_state = bytearray((latch_index + 7) >> 3)
    for d_signal, idx in latch_inputs:
        if _level(signals, d_signal) & 1:
            new_state[idx >> 3] |= 1 << (idx & 7)

    out = bytearray((n_out + 7) >> 3)
    for i in range(n_out):
        if _level(signals, len(signals) - n_out + i) & 1:
            out[i >> 3] |= 1 << (i & 7)
    return bytes(new_state), bytes(out)


def evaluate(
    netlist: bytes,
    *,
    n_in: int,
    n_out: int,
    cycles: int,
    input_data: bytes,
) -> bytes:
    """按官方口径评估一个向量，返回拼接后的输出帧。"""
    if cycles < 1:
        raise NetlistError("cycles 必须 >= 1")
    in_stride = (n_in + 7) >> 3
    state = b""
    out = bytearray()
    for cycle in range(cycles):
        frame = input_data[cycle * in_stride : (cycle + 1) * in_stride]
        state, frame_out = eval_step(netlist, n_in, n_out, state, frame)
        out += frame_out
    return bytes(out)


def vector_ok(
    netlist: bytes,
    *,
    n_in: int,
    n_out: int,
    cycles: int,
    input_hex: str,
    expected_hex: str,
) -> bool:
    """判断某个官方向量是否被该电路满足（本地预检，不替代链上校验）。"""
    got = evaluate(
        netlist,
        n_in=n_in,
        n_out=n_out,
        cycles=cycles,
        input_data=bytes.fromhex(_strip_hex(input_hex)),
    )
    return got == bytes.fromhex(_strip_hex(expected_hex))


def _strip_hex(value: str) -> str:
    text = value[2:] if value[:2].lower() == "0x" else value
    if len(text) % 2:
        text = "0" + text
    return text
