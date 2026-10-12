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
from .models import CpuOut, MinerInfo, MiningStatus

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


def pod_mining_contract(w3: Web3, settings: Settings) -> Contract:
    """PoD 挖矿主合约：arm / start / claim / stop 都在这里。"""
    return w3.eth.contract(
        address=Web3.to_checksum_address(settings.pod_mining_address),
        abi=abi_mod.load_abi("podMiningAbi"),
    )


def pod_lens_contract(w3: Web3, settings: Settings) -> Contract:
    """PoD 只读透镜：采样下标、任务参考、预估都在这里，不产生交易。"""
    return w3.eth.contract(
        address=Web3.to_checksum_address(settings.pod_lens_address),
        abi=abi_mod.load_abi("podLensAbi"),
    )


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


# ------------------------------------------------------------------ 挖矿读取

# 挖矿尚未开始时，``startTime`` 被合约初始化成 uint64 最大值占位。
NOT_STARTED_SENTINEL = (1 << 64) - 1

# Miner.status 的链上取值含义（见前端 pod 产物）。
MINER_STATUS_TEXT = {0: "none", 1: "active", 2: "revoked", 3: "cooling"}


def read_mining_status(w3: Web3, settings: Settings) -> MiningStatus:
    """汇总 PoD 挖矿的全局状态：是否已启动、当前产出速率、矿池权重。"""
    mining = pod_mining_contract(w3, settings)
    lens = pod_lens_contract(w3, settings)
    start_time = int(mining.functions.startTime().call())
    pool = lens.functions.poolInfo().call()
    started = start_time != NOT_STARTED_SENTINEL
    human = "未启动（等待官方开启）"
    if started:
        human = datetime.fromtimestamp(start_time, timezone.utc).isoformat(timespec="seconds")
    return MiningStatus(
        chain_id=settings.chain_id,
        mining=Web3.to_checksum_address(settings.pod_mining_address),
        token=Web3.to_checksum_address(mining.functions.token().call()),
        started=started,
        start_time=start_time,
        start_time_human=human,
        current_rate=int(mining.functions.currentRate().call()),
        total_mined=int(mining.functions.totalMined().call()),
        daily_emission=int(lens.functions.dailyEmission().call()),
        current_era=int(lens.functions.currentEra().call()),
        paused=bool(mining.functions.paused().call()),
        task_count=int(mining.functions.taskCount().call()),
        miner_count=int(mining.functions.minerCount().call()),
        pool_verif_weight=int(pool[0]),
        pool_unver_weight=int(pool[1]),
        pool_verif_miners=int(pool[2]),
        pool_miners=int(pool[3]),
        pool_unver_bps=int(pool[4]),
    )


def read_tapeout_fee(w3: Web3, circuits_address: str) -> int:
    """读取某电路合约当前的 tapeout 固定费用（wei）。"""
    contract = circuits_contract(w3, circuits_address)
    return int(contract.functions.TAPEOUT_FEE().call())


def read_circuit_info(w3: Web3, circuits_address: str, circuit_id: int) -> tuple[int, int, int, int]:
    """返回 ``(nIn, nOut, nState, gateCount)``；电路不存在时链上会直接 revert。"""
    contract = circuits_contract(w3, circuits_address)
    info = contract.functions.circuitInfo(int(circuit_id)).call()
    return int(info[0]), int(info[1]), int(info[2]), int(info[3])


def read_circuit_netlist(w3: Web3, circuits_address: str, circuit_id: int) -> bytes:
    contract = circuits_contract(w3, circuits_address)
    return bytes(contract.functions.netlist(int(circuit_id)).call())


def read_miner_key(w3: Web3, settings: Settings, circuits_address: str, circuit_id: int) -> str:
    """``minerKey`` 是纯函数（keccak(circuits, circuitId)），链上本地都能算。"""
    mining = pod_mining_contract(w3, settings)
    return Web3.to_hex(mining.functions.minerKey(
        Web3.to_checksum_address(circuits_address), int(circuit_id)
    ).call())


def read_miner(w3: Web3, settings: Settings, key: str) -> MinerInfo | None:
    """读一个矿机快照；矿机不存在（registrant 为零地址）时返回 None。"""
    mining = pod_mining_contract(w3, settings)
    row = mining.functions.getMiner(key).call()
    registrant = Web3.to_checksum_address(row[10])
    if int(row[9]) == 0 and registrant == Web3.to_checksum_address("0x" + "0" * 40):
        return None
    pending = 0
    try:
        pending = int(mining.functions.pending(key).call())
    except Exception as exc:  # 领取代币合约异常不应让状态查询整体失败
        log.warning("读取 %s 的 pending 失败: %s", key, exc)
    status = int(row[9])
    return MinerInfo(
        key=key,
        circuits=Web3.to_checksum_address(row[0]),
        circuit_id=int(row[1]),
        task_id=int(row[2]),
        gate_count=int(row[3]),
        state_count=int(row[4]),
        depth=int(row[5]),
        area=int(row[6]),
        mult=int(row[7]),
        since=int(row[8]),
        status=status,
        status_text=MINER_STATUS_TEXT.get(status, f"unknown({status})"),
        registrant=registrant,
        nand_burn=int(row[11]),
        latch_burn=int(row[12]),
        bstar=int(row[13]),
        bonus=int(row[14]),
        optimal=bool(row[15]),
        commit_block=int(row[16]),
        first_unused_id=int(row[17]),
        stop_block=int(row[18]),
        verif_weight=int(row[19]),
        unver_weight=int(row[20]),
        debt=int(row[21]),
        boost_bps=int(row[22]),
        pending_wei=pending,
    )


def sample_indexes(
    w3: Web3,
    settings: Settings,
    circuits_address: str,
    circuit_id: int,
    task_id: int,
    anchor_block: int,
) -> list[int]:
    """取该矿机本次要提交的样本下标（由 anchorBlock 的区块哈希派生，链上可复算）。"""
    lens = pod_lens_contract(w3, settings)
    idx = lens.functions.sampleIndexes(
        Web3.to_checksum_address(circuits_address),
        int(circuit_id),
        int(task_id),
        int(anchor_block),
    ).call()
    return [int(i) for i in idx]


def preview_burn_onchain(w3: Web3, settings: Settings, netlist: bytes) -> tuple[int, int]:
    """链上预估 tapeout 会烧掉多少 NAND / LATCH（``previewBurn``）。

    tapeout 只能由电路合约内部调用 ``transistors.burnFrom``，用户无法自行销毁晶体管，
    因此铸造前必须用这个只读接口确认余额够不够烧，否则交易必然 revert。
    """
    lens = pod_lens_contract(w3, settings)
    burn = lens.functions.previewBurn(bytes(netlist)).call()
    return int(burn[0]), int(burn[1])


def fetch_pod_tasks(settings: Settings) -> list[dict[str, Any]]:
    """官方挖矿任务表（与前端同源），含 nIn/nOut/cycles/参考电路/向量根。"""
    url = f"{settings.api_url.rstrip('/')}/v1/pod/tasks"
    with httpx.Client(timeout=settings.rpc_timeout) as client:
        resp = client.get(url, params={"chainId": settings.chain_id})
        resp.raise_for_status()
        payload = resp.json()
    return list(payload.get("data") or [])


def fetch_pod_vectors(settings: Settings, task_id: int) -> dict[str, Any]:
    """某个任务的全部官方向量（input/expectedOut/proof），用于本地预检与 start。"""
    url = f"{settings.api_url.rstrip('/')}/v1/pod/tasks/{int(task_id)}/vectors"
    with httpx.Client(timeout=settings.rpc_timeout) as client:
        resp = client.get(url, params={"chainId": settings.chain_id})
        resp.raise_for_status()
        return dict(resp.json())


def verify_design(settings: Settings, task_id: int, gates: list[Any]) -> dict[str, Any]:
    """调用官方 verify 接口做「提交前的权威预检」，返回 pass/得分/算力等信息。"""
    url = f"{settings.api_url.rstrip('/')}/v1/pod/verify"
    with httpx.Client(timeout=settings.rpc_timeout) as client:
        resp = client.post(
            url,
            params={"chainId": settings.chain_id},
            json={"taskId": int(task_id), "gates": gates},
        )
        resp.raise_for_status()
        return dict(resp.json())
