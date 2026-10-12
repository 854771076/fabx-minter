"""PoD 深度挖矿执行引擎（tapeout / arm / start / claim / stop / reclaim）。

链路逆向自 fabx.tech PoD 挖矿页打包产物 ``work/fe/pod-B8Ab4M7D.js``，完整流程：

1. ``tapeout(netlist, nIn, nOut)``：付固定费用，并烧掉账号持有的 NAND / LATCH
   晶体管，铸出一枚电路 ERC721。产物是后续挖矿的唯一载体。
2. （可选）``commitDesign(circuits, commitment)``：先提交设计承诺，避免被抢跑。
3. ``arm(circuits, circuitId)``：拿一个「采样锚点区块」，该区块哈希决定本次要
   提交哪些样本；start 必须在 ``MAX_ANCHOR_AGE`` 个区块内完成。
4. ``sampleIndexes(...)``：用锚点区块算出要提交的样本下标（PodLens 只读）。
5. ``start(...)``：把这批样本的 input / expectedOut / proof 上链重放，矿机开始产出。
6. ``claim`` / ``claimMany`` 领取代币，``stop`` 停机，``reclaim`` 回收晶体管。

本模块只负责「签名 + 广播 + 等回执 + 落库」，所有纯计算（网表编解码、本地评估、
官方预检）分别落在 ``netlist.py`` / ``chain.py``，便于脱离网络单测。
"""

from __future__ import annotations

import logging
import os
import time
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Any

from web3 import Web3
from web3.contract import Contract

from . import abi as abi_mod
from .chain import (
    dynamic_gas_price,
    gas_price_cap_wei,
    make_web3,
    pod_lens_contract,
    pod_mining_contract,
    read_miner,
    read_miner_key,
    read_mining_status,
    sample_indexes,
    transistors_contract,
)
from .config import Settings
from .crypto import SecretError, decrypt_private_key, resolve_password
from .db import Database
from .models import (
    CircuitIn,
    MineActionResult,
    MinerInfo,
    MiningStatus,
    TapeoutRequest,
    TapeoutResult,
)
from .netlist import (
    NetlistError,
    encode_gates,
    netlist_stats,
    preview_netlist,
    vector_ok,
)

log = logging.getLogger("fabx.mining")

# 预估 gas 的安全余量：链上状态在「估算」与「打包」之间可能变化，留一点冗余，
# 避免估算刚好卡线却因状态漂移导致 OOG。价格余量由 settings.gas_multiplier 单独负责。
_GAS_LIMIT_BUFFER = 1.25

_ZERO_SALT = "0x" + "0" * 64

# ERC721 Transfer(address,address,uint256) 事件签名，用于从 tapeout 回执里解析 tokenId。
# 统一存成「不带 0x 的小写 hex」，与回执里的 topic 字符串口径一致。
_TRANSFER_TOPIC = Web3.to_hex(
    Web3.keccak(text="Transfer(address,address,uint256)")
).removeprefix("0x").lower()


class MiningError(RuntimeError):
    """挖矿链路执行失败：余额/晶体管不足、锚点过期、链上 revert 等。"""


def _strip_hex(value: str) -> str:
    text = value[2:] if value[:2].lower() == "0x" else value
    if len(text) % 2:
        text = "0" + text
    return text


def _to_bytes(value: Any) -> bytes:
    """把链上/接口返回的 0x 十六进制字符串统一转成 bytes。"""
    if isinstance(value, (bytes, bytearray)):
        return bytes(value)
    return bytes.fromhex(_strip_hex(str(value)))


def _to_bytes32(value: Any) -> bytes:
    """转换成 32 字节；短值（如全零盐）右侧补零，长值报错。"""
    raw = _to_bytes(value)
    if len(raw) > 32:
        raise MiningError(f"bytes32 长度非法：{len(raw)} > 32")
    return raw.ljust(32, b"\x00")


def _normalize_key(value: Any) -> str:
    """把矿机 key（bytes32）统一成 0x + 64 位小写十六进制。"""
    return Web3.to_hex(_to_bytes32(value))


@dataclass(slots=True)
class _Signed:
    """一笔已签名交易的元信息。"""

    tx_hash: str
    nonce: int
    gas_price: int
    gas_limit: int


@dataclass(slots=True)
class _Outcome:
    """一笔已确认交易的回执摘要。"""

    tx_hash: str
    block_number: int
    gas_used: int
    status: int
    receipt: dict[str, Any]


class MiningEngine:
    """PoD 挖矿执行引擎。

    与 ``MintEngine`` 一样，从 ``db`` 取密文、按需解密；口令只存在于内存，
    不写日志、不落库。所有读写都按「发一笔交易 -> 等回执 -> 落库」推进，
    进程中断后可用 tx_hash 对账。
    """

    def __init__(self, db: Database, settings: Settings, password: str | None = None) -> None:
        self.db = db
        self.settings = settings
        self._password = password
        self.w3 = make_web3(settings)

    # ------------------------------------------------------------ 只读状态
    def global_status(self) -> MiningStatus:
        """整条挖矿链路的全局状态（是否启动、速率、矿池权重等）。"""
        return read_mining_status(self.w3, self.settings)

    # 别名：调用方习惯写 status()，与 CLI 子命令同名
    status = global_status

    def task_meta(self, task_id: int) -> dict[str, Any] | None:
        """从官方任务表里取某个任务的元信息（nIn/nOut/cycles/vectorCount 等）。"""
        from .chain import fetch_pod_tasks

        for row in fetch_pod_tasks(self.settings):
            if int(row.get("taskId", -1)) == int(task_id):
                return dict(row)
        return None

    def vectors(self, task_id: int) -> dict[str, Any]:
        """取某个任务的全部官方向量（input / expectedOut / proof）。"""
        from .chain import fetch_pod_vectors

        return fetch_pod_vectors(self.settings, task_id)

    def miner(self, circuits: str, circuit_id: int) -> MinerInfo | None:
        """读取某电路的矿机快照；未注册时返回 None。"""
        key = read_miner_key(self.w3, self.settings, circuits, circuit_id)
        return read_miner(self.w3, self.settings, key)

    def preflight(
        self,
        task_id: int,
        gates: Sequence[Sequence[Any]],
        *,
        local_check: bool = True,
    ) -> dict[str, Any]:
        """提交前的权威预检：官方 verify + 本地逐向量校验。

        官方接口是唯一权威（它知道链上会用哪些向量），本地校验只是提前暴露
        「网表与任务不匹配」，避免白花 tapeout 费与 gas。
        """
        from .chain import verify_design

        netlist = encode_gates(gates)
        result = verify_design(self.settings, task_id, [list(g) for g in gates])
        result["netlist_hex"] = Web3.to_hex(netlist)
        result["netlist_bytes"] = len(netlist)

        if local_check:
            meta = self.task_meta(task_id)
            if meta is None:
                result["local_error"] = f"任务 {task_id} 不存在"
                return result
            payload = self.vectors(task_id)
            vectors = list(payload.get("vectors") or [])
            n_in = int(meta.get("nIn", 0))
            n_out = int(meta.get("nOut", 0))
            cycles = int(meta.get("cycles", 1))
            passed = 0
            for vec in vectors:
                try:
                    ok = vector_ok(
                        netlist,
                        n_in=n_in,
                        n_out=n_out,
                        cycles=cycles,
                        input_hex=str(vec.get("input", "0x")),
                        expected_hex=str(vec.get("expectedOut", "0x")),
                    )
                except NetlistError:
                    ok = False
                passed += 1 if ok else 0
            result["local_passed"] = passed
            result["local_total"] = len(vectors)
            result["local_ok"] = bool(vectors) and passed == len(vectors)
        return result

    # --------------------------------------------------------------- tapeout
    def tapeout(self, req: TapeoutRequest) -> TapeoutResult:
        """把网表铸成电路 NFT，并烧掉对应晶体管。

        顺序与前端一致：先算本地预估与扣费，再（可选）commit，最后发 tapeout。
        晶体管不足会在发交易前拦下，避免白白付出 gas。
        """
        circuits_addr = req.circuits or self.settings.default_circuits_address
        try:
            netlist = encode_gates(req.gates)
        except NetlistError as exc:
            raise MiningError(f"网表非法：{exc}") from exc

        try:
            preview = preview_netlist(netlist, n_in=req.n_in)
        except NetlistError as exc:
            raise MiningError(f"网表无法预估：{exc}") from exc

        circuits = self._circuits_contract(circuits_addr)
        fee = int(circuits.functions.TAPEOUT_FEE().call())
        nand_burn, latch_burn = self._preview_burn(netlist)

        account = self._open_account(req.account_id)
        self._ensure_transistor_balance(
            circuits_addr, account["address"], nand_burn, latch_burn
        )

        salt = self._new_salt()
        notes: list[str] = []
        commit_tx: str | None = None
        if req.dry_run:
            next_id = int(circuits.functions.nextId().call())
            notes.append("dry-run：仅做本地预估，未广播任何交易")
            return TapeoutResult(
                account_id=req.account_id,
                address=account["address"],
                circuits=circuits_addr,
                circuit_id=next_id,
                tx_hash="0x" + "0" * 64,
                block_number=0,
                gas_used=0,
                fee_wei=fee,
                nand_burn=nand_burn,
                latch_burn=latch_burn,
                bstar=preview["bstar"],
                area=preview["area"],
                depth=preview["depth"],
                cost=preview["cost"],
                netlist_hex=Web3.to_hex(netlist),
                notes=notes,
            )

        netlist_hash = Web3.keccak(netlist)
        if req.commit:
            commitment = self._design_commitment(
                account["address"], circuits_addr, netlist_hash, salt
            )
            outcome = self._send_and_wait(
                circuits.functions.commitDesign(circuits_addr, commitment),
                account,
                gas_limit=self.settings.mine_gas_limit,
            )
            commit_tx = outcome.tx_hash
            notes.append(f"已提交设计承诺 {Web3.to_hex(commitment)}，防抢跑")
            # 承诺是在特定区块被记录的，必须等它确认后再 tapeout，链上才会认账。
            self._wait_for_block_after(outcome.block_number)

        next_id = int(circuits.functions.nextId().call())
        outcome = self._send_and_wait(
            circuits.functions.tapeout(netlist, int(req.n_in), int(req.n_out)),
            account,
            value=fee,
            gas_limit=self.settings.mine_gas_limit,
        )

        extracted = self._extract_circuit_id(outcome.receipt, circuits_addr)
        # tokenId 合法上从 0 起；0 是有效值，只有解不出日志时才退回预估的 nextId。
        circuit_id = next_id if extracted is None else extracted
        n_state, gate_count = self._circuit_shape(circuits_addr, circuit_id, netlist, req)
        self.db.add_circuit(
            CircuitIn(
                account_id=req.account_id,
                circuits=circuits_addr,
                circuit_id=circuit_id,
                n_in=req.n_in,
                n_out=req.n_out,
                n_state=n_state,
                gate_count=gate_count,
                label=req.label,
                salt=salt.hex(),
                tx_hash=outcome.tx_hash,
                block_number=outcome.block_number,
            )
        )
        return TapeoutResult(
            account_id=req.account_id,
            address=account["address"],
            circuits=circuits_addr,
            circuit_id=circuit_id,
            tx_hash=outcome.tx_hash,
            block_number=outcome.block_number,
            gas_used=outcome.gas_used,
            fee_wei=fee,
            nand_burn=nand_burn,
            latch_burn=latch_burn,
            bstar=preview["bstar"],
            area=preview["area"],
            depth=preview["depth"],
            cost=preview["cost"],
            netlist_hex=Web3.to_hex(netlist),
            commit_tx_hash=commit_tx,
            notes=notes,
        )

    # ------------------------------------------------------------------- arm
    def arm(
        self,
        account_id: int,
        circuits_addr: str,
        circuit_id: int,
    ) -> MineActionResult:
        """给电路「上膛」：拿一个锚点区块，返回该区块号供 start 使用。"""
        account = self._open_account(account_id)
        mining = pod_mining_contract(self.w3, self.settings)
        outcome = self._send_and_wait(
            mining.functions.arm(Web3.to_checksum_address(circuits_addr), int(circuit_id)),
            account,
            gas_limit=self.settings.mine_gas_limit,
        )
        key = read_miner_key(self.w3, self.settings, circuits_addr, circuit_id)
        return MineActionResult(
            action="arm",
            status="confirmed",
            account_id=account_id,
            address=account["address"],
            circuits=circuits_addr,
            circuit_id=circuit_id,
            key=key,
            tx_hash=outcome.tx_hash,
            block_number=outcome.block_number,
            gas_used=outcome.gas_used,
            anchor_block=outcome.block_number,
            detail="锚点区块即 arm 所在区块",
        )

    # ----------------------------------------------------------------- start
    def start(
        self,
        account_id: int,
        circuits_addr: str,
        circuit_id: int,
        task_id: int,
        *,
        anchor_block: int | None = None,
        salt: str | None = None,
    ) -> MineActionResult:
        """启动挖矿：arm（若未给锚点）-> 采样 -> start 提交样本。

        ``anchor_block`` 缺省时本方法内部先 arm；若外部已经 arm 过（例如为了把
        「arm 与 start 的时间窗」压到最短），把当时的区块号传进来即可复用。
        """
        account = self._open_account(account_id)
        mining = pod_mining_contract(self.w3, self.settings)

        if anchor_block is None:
            anchor_block = self.arm(account_id, circuits_addr, circuit_id).anchor_block or 0

        self._wait_for_block_after(int(anchor_block))

        meta = self.task_meta(task_id)
        if meta is None:
            raise MiningError(f"任务 {task_id} 不存在")
        payload = self.vectors(task_id)
        entries = list(payload.get("vectors") or [])
        by_index = {int(v["index"]): v for v in entries}

        indexes = sample_indexes(
            self.w3,
            self.settings,
            circuits_addr,
            circuit_id,
            task_id,
            int(anchor_block),
        )
        samples = []
        for idx in indexes:
            vec = by_index.get(int(idx))
            if vec is None:
                raise MiningError(f"官方向量缺少下标 {idx}（任务 {task_id}）")
            samples.append(vec)

        inputs = [_to_bytes(v["input"]) for v in samples]
        expected = [_to_bytes(v["expectedOut"]) for v in samples]
        proofs = [[_to_bytes32(p) for p in (v.get("proof") or [])] for v in samples]
        design_salt = self._resolve_salt(circuits_addr, circuit_id, salt)

        outcome = self._send_and_wait(
            mining.functions.start(
                Web3.to_checksum_address(circuits_addr),
                int(circuit_id),
                int(task_id),
                int(anchor_block),
                inputs,
                expected,
                proofs,
                design_salt,
            ),
            account,
            gas_limit=self.settings.mine_start_gas_limit,
        )
        key = read_miner_key(self.w3, self.settings, circuits_addr, circuit_id)
        self.db.set_circuit_miner(
            circuits=circuits_addr,
            circuit_id=circuit_id,
            task_id=task_id,
            miner_key=key,
        )
        return MineActionResult(
            action="start",
            status="confirmed",
            account_id=account_id,
            address=account["address"],
            circuits=circuits_addr,
            circuit_id=circuit_id,
            task_id=task_id,
            key=key,
            tx_hash=outcome.tx_hash,
            block_number=outcome.block_number,
            gas_used=outcome.gas_used,
            samples=len(samples),
            anchor_block=int(anchor_block),
            detail=f"提交 {len(samples)} 个样本",
        )

    # ------------------------------------------------------------ claim / stop
    def claim(self, account_id: int, keys: Iterable[str]) -> MineActionResult:
        """领取矿机产出的代币；多把 key 合并成 claimMany 一笔，省 gas。"""
        normalized = [_normalize_key(k) for k in keys]
        if not normalized:
            raise MiningError("claim 至少需要一把矿机 key")
        account = self._open_account(account_id)
        mining = pod_mining_contract(self.w3, self.settings)
        if len(normalized) == 1:
            fn = mining.functions.claim(_to_bytes32(normalized[0]))
        else:
            fn = mining.functions.claimMany([_to_bytes32(k) for k in normalized])
        outcome = self._send_and_wait(fn, account, gas_limit=self.settings.mine_gas_limit)
        return MineActionResult(
            action="claim",
            status="confirmed",
            account_id=account_id,
            address=account["address"],
            key=normalized[0] if len(normalized) == 1 else None,
            tx_hash=outcome.tx_hash,
            block_number=outcome.block_number,
            gas_used=outcome.gas_used,
            samples=len(normalized),
            detail=f"领取 {len(normalized)} 台矿机的产出",
        )

    def stop(self, account_id: int, key: str) -> MineActionResult:
        """停机：停止挖矿，进入冷却；冷却后可用 reclaim 收回晶体管。"""
        account = self._open_account(account_id)
        mining = pod_mining_contract(self.w3, self.settings)
        outcome = self._send_and_wait(
            mining.functions.stop(_to_bytes32(key)),
            account,
            gas_limit=self.settings.mine_gas_limit,
        )
        return MineActionResult(
            action="stop",
            status="confirmed",
            account_id=account_id,
            address=account["address"],
            key=_normalize_key(key),
            tx_hash=outcome.tx_hash,
            block_number=outcome.block_number,
            gas_used=outcome.gas_used,
        )

    def reclaim(self, account_id: int, key: str) -> MineActionResult:
        """回收：停机冷却结束后取回投入的晶体管。"""
        account = self._open_account(account_id)
        mining = pod_mining_contract(self.w3, self.settings)
        outcome = self._send_and_wait(
            mining.functions.reclaim(_to_bytes32(key)),
            account,
            gas_limit=self.settings.mine_gas_limit,
        )
        return MineActionResult(
            action="reclaim",
            status="confirmed",
            account_id=account_id,
            address=account["address"],
            key=_normalize_key(key),
            tx_hash=outcome.tx_hash,
            block_number=outcome.block_number,
            gas_used=outcome.gas_used,
        )

    # ------------------------------------------------------------ 内部辅助
    def _circuits_contract(self, circuits_addr: str) -> Contract:
        return self.w3.eth.contract(
            address=Web3.to_checksum_address(circuits_addr),
            abi=abi_mod.load_abi("circuitsAbi"),
        )

    def _open_account(self, account_id: int) -> dict[str, Any]:
        """取出账号并解密私钥；失败统一抛 MiningError，避免裸异常泄漏。"""
        cipher = self.db.get_cipher(account_id)
        if cipher is None:
            raise MiningError(f"账号 {account_id} 不存在")
        password = self._password or resolve_password(None)
        try:
            private_key = decrypt_private_key(cipher, password)
        except SecretError as exc:
            raise MiningError(f"账号 {account_id} 私钥解密失败：{exc}") from exc
        address = self.w3.eth.account.from_key(private_key).address
        return {"id": account_id, "address": address, "private_key": private_key}

    def _preview_burn(self, netlist: bytes) -> tuple[int, int]:
        try:
            lens = pod_lens_contract(self.w3, self.settings)
            burn = lens.functions.previewBurn(bytes(netlist)).call()
            return int(burn[0]), int(burn[1])
        except Exception as exc:
            raise MiningError(f"读取 tapeout 预估烧毁量失败：{exc}") from exc

    def _ensure_transistor_balance(
        self,
        circuits_addr: str,
        address: str,
        nand_burn: int,
        latch_burn: int,
    ) -> None:
        """tapeout 会烧晶体管，余额不足则提前拦下，避免交易 revert 白烧 gas。"""
        circuits = self._circuits_contract(circuits_addr)
        transistors_addr = None
        for name in ("transistors", "transistorsContract"):
            try:
                # web3 的 ContractFunction 命名空间同时支持属性与下标取法；
                # 不同版本合约只有一个视图名，因此逐个尝试并在缺失时回退。
                view = getattr(circuits.functions, name, None)
                if view is None:
                    view = circuits.functions[name]
                transistors_addr = view().call()
                break
            except Exception:  # 不同版本合约只有一个视图名
                continue
        if not transistors_addr:
            raise MiningError(f"无法从 {circuits_addr} 读取晶体管合约地址")
        transistors = transistors_contract(self.w3, transistors_addr)
        nand_id = int(transistors.functions.NAND().call())
        latch_id = int(transistors.functions.LATCH().call())
        nand_bal = int(transistors.functions.balanceOf(address, nand_id).call())
        latch_bal = int(transistors.functions.balanceOf(address, latch_id).call())
        if nand_burn > nand_bal or latch_burn > latch_bal:
            raise MiningError(
                f"晶体管不足：需 NAND {nand_burn} / LATCH {latch_burn}，"
                f"实际 {nand_bal} / {latch_bal}；先 mint 晶体管再 tapeout"
            )

    def _circuit_shape(
        self,
        circuits_addr: str,
        circuit_id: int,
        netlist: bytes,
        req: TapeoutRequest,
    ) -> tuple[int, int]:
        """读取链上 circuitInfo 的 (nState, gateCount)；失败退回本地统计。"""
        try:
            circuits = self._circuits_contract(circuits_addr)
            info = circuits.functions.circuitInfo(int(circuit_id)).call()
            return int(info[2]), int(info[3])
        except Exception:
            stats = netlist_stats(netlist)
            return 0, stats["nand"] + stats["latch"]

    def _resolve_salt(self, circuits_addr: str, circuit_id: int, salt: str | None) -> bytes:
        """start 必须用与 commit 相同的 salt；优先级：入参 > 本地记录 > 配置 > 全零。"""
        if salt:
            return _to_bytes32(salt)
        record = self.db.get_circuit(circuits_addr, circuit_id)
        if record and record.salt:
            return _to_bytes32(record.salt)
        configured = self.settings.mine_salt
        if configured is not None:
            return _to_bytes32(configured.get_secret_value())
        return b"\x00" * 32

    @staticmethod
    def _new_salt() -> bytes:
        return os.urandom(32)

    @staticmethod
    def _design_commitment(
        account: str,
        circuits_addr: str,
        netlist_hash: bytes,
        salt: bytes,
    ) -> bytes:
        """keccak(abi.encode(account, circuits, netlistHash, salt))，与前端一致。"""
        encoded = (
            bytes(12)
            + bytes.fromhex(_strip_hex(account))
            + bytes(12)
            + bytes.fromhex(_strip_hex(circuits_addr))
            + bytes(netlist_hash).rjust(32, b"\x00")
            + bytes(salt).rjust(32, b"\x00")
        )
        return Web3.keccak(encoded)

    @staticmethod
    def _extract_circuit_id(receipt: dict[str, Any], circuits_addr: str) -> int | None:
        """从 tapeout 回执的 ERC721 Transfer(from=0) 日志里解析 tokenId。"""
        want = Web3.to_checksum_address(circuits_addr)
        for entry in receipt.get("logs", []) or []:
            try:
                if Web3.to_checksum_address(entry["address"]) != want:
                    continue
                topics = entry.get("topics") or []
                if len(topics) < 4:
                    continue
                topic0 = topics[0]
                # 回执里的 topic 可能是 HexBytes 或 "0x..." 字符串，统一成纯 hex 再比。
                topic0_hex = (
                    bytes(topic0).hex()
                    if isinstance(topic0, (bytes, bytearray))
                    else _strip_hex(str(topic0))
                ).lower()
                if topic0_hex != _TRANSFER_TOPIC:
                    continue
                if int.from_bytes(_to_bytes(topics[1]), "big") != 0:
                    continue
                return int.from_bytes(_to_bytes(topics[3]), "big")
            except Exception:
                continue
        return None

    # ------------------------------------------------------------------ 交易
    def _gas_price(self) -> int:
        """每次发送前重新取价，让定价跟随链上拥堵动态变化。"""
        return dynamic_gas_price(self.w3, self.settings)

    def _send_and_wait(
        self,
        fn: Any,
        account: dict[str, Any],
        *,
        value: int = 0,
        gas_limit: int = 0,
    ) -> _Outcome:
        """签名并广播一笔交易，等待回执；广播失败按 max_retries 重试。

        每次重试都重新读取 pending nonce：广播失败可能已经消耗掉 nonce，
        继续沿用旧值会与链上状态错位。
        """
        last_error: Exception | None = None
        for _attempt in range(self.settings.max_retries + 1):
            try:
                signed = self._broadcast(fn, account, value=value, gas_limit=gas_limit)
                receipt = self._wait_receipt(signed.tx_hash)
                status = int(receipt.get("status", 0))
                if status != 1:
                    raise MiningError(
                        f"交易回执 status=0（链上 revert）：{signed.tx_hash}"
                    )
                return _Outcome(
                    tx_hash=signed.tx_hash,
                    block_number=int(receipt["blockNumber"]),
                    gas_used=int(receipt["gasUsed"]),
                    status=status,
                    receipt=dict(receipt),
                )
            except MiningError:
                raise
            except Exception as exc:  # 广播/等待异常：重试前与链上对齐 nonce
                last_error = exc
                log.warning("挖矿交易失败，准备重试：%s", exc)
        raise MiningError(f"交易广播失败（已重试 {self.settings.max_retries} 次）：{last_error}")

    def _broadcast(
        self,
        fn: Any,
        account: dict[str, Any],
        *,
        value: int,
        gas_limit: int,
    ) -> _Signed:
        address = account["address"]
        private_key = account["private_key"]
        gas_price = self._gas_price()
        cap = gas_price_cap_wei(self.settings)
        if cap and gas_price > cap:
            raise MiningError(
                f"gas 价 {gas_price} wei 超过上限 {cap} wei（链上拥堵），已跳过；"
                f"如需继续可调高 FABX_GAS_PRICE_MAX_GWEI"
            )
        nonce = int(self.w3.eth.get_transaction_count(address, "pending"))
        limit = int(gas_limit or self.settings.gas_limit or 0)
        if not limit:
            limit = self._estimate_gas(fn, address, value)

        balance = int(self.w3.eth.get_balance(address))
        if balance < value + limit * gas_price:
            raise MiningError(
                f"余额不足：{balance} wei < 需 value {value} + gas≈{limit * gas_price} wei"
            )

        tx = fn.build_transaction(
            {
                "from": address,
                "value": value,
                "nonce": nonce,
                "chainId": self.settings.chain_id,
                "gasPrice": gas_price,
                "gas": limit,
            }
        )
        signed = self.w3.eth.account.sign_transaction(tx, private_key)
        raw = getattr(signed, "raw_transaction", None) or signed.rawTransaction
        tx_hash = self.w3.eth.send_raw_transaction(raw).hex()
        if not tx_hash.startswith("0x"):
            tx_hash = "0x" + tx_hash
        return _Signed(tx_hash=tx_hash, nonce=nonce, gas_price=gas_price, gas_limit=limit)

    def _estimate_gas(self, fn: Any, address: str, value: int) -> int:
        estimate = fn.estimate_gas({"from": address, "value": value})
        return int(estimate * _GAS_LIMIT_BUFFER)

    def _wait_receipt(self, tx_hash: str) -> dict[str, Any]:
        return dict(
            self.w3.eth.wait_for_transaction_receipt(
                tx_hash, timeout=self.settings.tx_timeout
            )
        )

    def _wait_for_block_after(self, block_number: int) -> None:
        """等待链上出现「严格晚于锚点」的区块。

        前端要求锚点区块之后至少有一个新区块才能采样，否则 sampleIndexes 用到的
        区块哈希还不稳定。超时不直接失败：锚点是否过期由链上 start 校验，
        这里只负责给足时间。
        """
        deadline = time.monotonic() + self.settings.mine_arm_timeout
        while time.monotonic() < deadline:
            try:
                current = int(self.w3.eth.get_block_number())
                if current > block_number:
                    return
            except Exception as exc:  # RPC 抖动不应中断整条链路，继续轮询
                log.warning("读取最新区块失败：%s", exc)
            time.sleep(self.settings.mine_poll_interval)
        log.warning("等待区块 %s 之后的确认超时（链上仍可能接受 start）", block_number)
