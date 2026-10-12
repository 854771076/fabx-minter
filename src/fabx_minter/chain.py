"""链上读写封装（X Layer / chainId 196）。

晶体管是 ERC1155：``mint(uint256 id, uint256 amount)`` 为 payable，
应付金额 = ``protocolFee() + mintPrice() * amount``；``id`` 由合约常量 ``NAND()`` /
``LATCH()`` 给出。

协议费是「每笔交易固定收取一次」的费用（与数量无关），链上实测交易样例：
``value = 650000000000000 + 313 * 1``。因此同一 token 买多份只收一次，
而每多拆分一笔独立交易（例如 NAND 与 LATCH 各发一笔）就会各收一次，
规划时必须把协议费算进单笔花费，否则交易 value 不足会直接 revert。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

import httpx
from web3 import Web3
from web3.contract import Contract

from . import abi as abi_mod
from .config import Settings
from .db import Database
from .models import CpuOut

log = logging.getLogger("fabx.chain")


class ChainReadError(ValueError):
    """链上读取失败（RPC 抖动、超时或合约调用回滚）。

    刻意继承 ``ValueError``：CLI 里既有的 ``except ValueError`` 仍能捕获并
    友好提示，无需逐个改调用点。Web 层则单独捕获本类型，把「上游读取失败」
    映射成 502（Bad Gateway），避免让用户误以为是自己填错了参数。
    """


def make_web3(settings: Settings) -> Web3:
    provider = Web3.HTTPProvider(
        settings.rpc_url,
        request_kwargs={"timeout": settings.rpc_timeout},
    )
    return Web3(provider)


def transistors_contract(w3: Web3, address: str) -> Contract:
    return w3.eth.contract(address=Web3.to_checksum_address(address), abi=abi_mod.load_abi("transistorsAbi"))


def _abi_function_names(abi_name: str) -> frozenset[str]:
    """列出某套 ABI 中的函数名，用于区分「合约未实现」与「调用失败」。"""
    return frozenset(
        item["name"]
        for item in abi_mod.load_abi(abi_name)
        if item.get("type") == "function" and item.get("name")
    )


def circuits_contract(w3: Web3, address: str) -> Contract:
    return w3.eth.contract(address=Web3.to_checksum_address(address), abi=abi_mod.load_abi("circuitsAbi"))


def factory_contract(w3: Web3, address: str) -> Contract:
    return w3.eth.contract(address=Web3.to_checksum_address(address), abi=abi_mod.load_abi("circuitFactoryAbi"))


@dataclass(slots=True)
class TransistorState:
    """单个晶体管合约的铸造状态快照。"""

    address: str
    nand_id: int
    latch_id: int
    mint_price_wei: int
    supply_cap: int
    minted: int
    minted_by_id: dict[int, int]
    type_cap: dict[int, int]
    # 每笔 mint 固定收取的协议费（与 mintPrice、数量无关）
    protocol_fee_wei: int = 0
    cpu_name: str | None = None

    def remaining(self, token_id: int, *, supply_consumed: int = 0) -> int | None:
        """返回该类型还可铸造的数量；完全无上限时返回 None。

        - ``typeCap`` > 0：该类型单独设限，取「类型剩余」与「全局供应剩余」的较小值；
        - ``typeCap`` 为 0 / 缺省：视为该类型未单独设限，但仍受全局 ``supplyCap``
          约束，因此返回 ``supplyCap - minted``，而不是「不限制」。否则在接近总上限
          时仍会按满额批量放行，超出的交易在链上必然 revert、白白烧 gas；
        - 两个上限都缺省时才是真正无上限，返回 None。

        ``supply_consumed`` 用于多目标共享全局供应额度：全局 ``supplyCap`` 是同一
        合约下所有 token 共用的预算，规划时把其他目标已占用的数量传进来，才能让
        多个目标的合计不超过总上限。

        注意：这里只做供「规划」使用的宽松估计，最终是否可铸仍由链上执行结果决定。
        """
        supply_left: int | None = None
        if self.supply_cap > 0:
            supply_left = max(0, self.supply_cap - self.minted - supply_consumed)
        cap = self.type_cap.get(token_id)
        if cap:
            type_left = max(0, cap - self.minted_by_id.get(token_id, 0))
            return min(type_left, supply_left) if supply_left is not None else type_left
        return supply_left

    def cost_wei(self, amount: int) -> int:
        """单笔 ``mint(id, amount)`` 需要随交易发送的 value。

        协议费按「每笔交易」固定收取一次，因此只加一份，不随数量放大。
        """
        return self.protocol_fee_wei + self.mint_price_wei * amount

def read_transistor_state(w3: Web3, address: str) -> TransistorState:
    contract = transistors_contract(w3, address)
    fns = _abi_function_names("transistorsAbi")
    nand_id = int(contract.functions.NAND().call())
    latch_id = int(contract.functions.LATCH().call())
    mint_price = int(contract.functions.mintPrice().call())
    supply_cap = int(contract.functions.supplyCap().call())
    minted = int(contract.functions.minted().call())

    minted_by_id: dict[int, int] = {}
    type_cap: dict[int, int] = {}
    for token_id in (nand_id, latch_id):
        # 只有当 ABI 里确实声明了该视图时才调用；调用失败（RPC 抖动 / 合约回滚）
        # 会向上抛出，绝不再吞成 0——否则 typeCap 读失败会被误判为「不限制」而放任超铸。
        if "mintedOf" in fns:
            minted_by_id[token_id] = int(contract.functions.mintedOf(token_id).call())
        if "typeCap" in fns:
            type_cap[token_id] = int(contract.functions.typeCap(token_id).call())

    # 协议费同样是铸造必需的 payable value 组成，但它是「每笔固定」而非按类型，
    # 因此只读一次；旧合约 ABI 无此视图时降级为 0（该合约也不收取协议费）。
    protocol_fee = 0
    if "protocolFee" in fns:
        protocol_fee = int(contract.functions.protocolFee().call())

    # 名称仅用于展示，读取失败不影响铸造判断，单独降级即可
    if "cpuName" in fns:
        try:
            name = contract.functions.cpuName().call()
        except Exception as exc:
            log.warning("读取 %s 的 cpuName 失败: %s", address, exc)
            name = None
    else:
        name = None

    return TransistorState(
        address=Web3.to_checksum_address(address),
        nand_id=nand_id,
        latch_id=latch_id,
        mint_price_wei=mint_price,
        supply_cap=supply_cap,
        minted=minted,
        minted_by_id=minted_by_id,
        type_cap=type_cap,
        protocol_fee_wei=protocol_fee,
        cpu_name=name,
    )


def fetch_processors(settings: Settings) -> list[dict[str, Any]]:
    """从 Fab X 官方接口拉取 CPU 列表（address/transistors/价格等）。"""
    url = f"{settings.api_url.rstrip('/')}/v1/processors"
    with httpx.Client(timeout=settings.rpc_timeout) as client:
        resp = client.get(url)
        resp.raise_for_status()
        payload = resp.json()
    return list(payload.get("cpus", []))


def sync_cpus(db: Database, settings: Settings) -> list[CpuOut]:
    """同步 CPU 列表：接口给元数据，链上给权威价格与铸造进度。"""
    w3 = make_web3(settings)
    synced: list[CpuOut] = []
    for item in fetch_processors(settings):
        transistors = Web3.to_checksum_address(item["transistors"])
        mint_price = int(item.get("mintPrice") or 0)
        minted = int(item.get("minted") or 0)
        supply_cap = int(item.get("supplyCap") or 0)
        try:
            state = read_transistor_state(w3, transistors)
            mint_price = state.mint_price_wei
            minted = state.minted
            supply_cap = state.supply_cap
        except Exception as exc:  # RPC 抖动时降级用接口数据
            log.warning("链上读取 %s 失败，使用接口数据: %s", transistors, exc)
        cpu = CpuOut(
            circuits=Web3.to_checksum_address(item["address"]),
            transistors=transistors,
            name=item.get("name"),
            creator=item.get("creator"),
            supply_cap=supply_cap,
            minted=minted,
            mint_price_wei=mint_price,
            circuit_count=int(item.get("circuitCount") or 0),
            synced_at=datetime.now(timezone.utc),
        )
        db.upsert_cpu(cpu)
        synced.append(cpu)
    return synced


def native_balance(w3: Web3, address: str) -> int:
    return int(w3.eth.get_balance(Web3.to_checksum_address(address)))


def dynamic_gas_price(w3: Web3, settings: Settings) -> int:
    """计算当前应付的 gas 单价（wei）。

    - ``FABX_GAS_PRICE_GWEI > 0`` 时用固定值，否则读链上实时 ``gasPrice``；
    - 乘 ``FABX_GAS_MULTIPLIER`` 作为拥堵缓冲，提高被打包进下一区块的概率。

    关键是「每次广播前都重新读一次」：链上拥堵会让 ``gasPrice`` 持续抬升，
    若只在一批任务开始时读一次，后续交易很容易因定价过低而长时间 pending。
    """
    if settings.gas_price_gwei > 0:
        base = Web3.to_wei(settings.gas_price_gwei, "gwei")
    else:
        base = int(w3.eth.gas_price)
    return int(base * settings.gas_multiplier)


def gas_price_cap_wei(settings: Settings) -> int:
    """``FABX_GAS_PRICE_MAX_GWEI`` 换算成 wei；0 表示不设上限。"""
    if settings.gas_price_max_gwei <= 0:
        return 0
    return int(Web3.to_wei(settings.gas_price_max_gwei, "gwei"))


def format_okb(wei: int) -> str:
    """按 18 位小数格式化，去掉无意义尾零。"""
    whole, frac = divmod(wei, 10**18)
    if frac == 0:
        return str(whole)
    text = f"{frac:018d}".rstrip("0")
    return f"{whole}.{text}"
