"""领域模型：Web/CLI 与存储层共用的 Pydantic 结构。"""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

ADDRESS_PATTERN = r"^0x[0-9a-fA-F]{40}$"


class JobStatus(str, Enum):
    DRAFT = "draft"
    RUNNING = "running"
    PAUSED = "paused"
    DONE = "done"
    FAILED = "failed"


class TaskStatus(str, Enum):
    PENDING = "pending"
    SENT = "sent"
    CONFIRMED = "confirmed"
    FAILED = "failed"
    SKIPPED = "skipped"


class AccountIn(BaseModel):
    """导入账号。私钥只在内存中出现，落库前先加密。"""

    private_key: str = Field(min_length=64)
    label: str | None = None
    group: str | None = None

    @field_validator("private_key")
    @classmethod
    def _normalize_key(cls, v: str) -> str:
        key = v.strip()
        if key.startswith("0x") or key.startswith("0X"):
            key = key[2:]
        if len(key) != 64:
            raise ValueError("私钥长度必须为 32 字节（64 位十六进制）")
        int(key, 16)  # 校验十六进制
        return key.lower()


class AccountOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    address: str
    label: str | None = None
    group: str | None = None
    enabled: bool = True
    created_at: datetime | None = None

    # 运行时补充，不落库
    balance_wei: int | None = None


class CpuOut(BaseModel):
    """一个 CPU = 一对 Contracts（电路 ERC721 + 晶体管 ERC1155）。"""

    model_config = ConfigDict(from_attributes=True)

    circuits: str
    transistors: str
    name: str | None = None
    creator: str | None = None
    supply_cap: int | None = None
    minted: int | None = None
    mint_price_wei: int | None = None
    circuit_count: int | None = None
    synced_at: datetime | None = None


class MintTarget(BaseModel):
    """批量 mint 的目标：某个晶体管合约的某个 tokenId。"""

    transistors: str = Field(pattern=ADDRESS_PATTERN)
    token_id: int = Field(ge=0)
    amount: int = Field(ge=0, description="每个账号铸造的数量；fill=true 时忽略")
    fill: bool = Field(
        default=False,
        description="吃满剩余额度：忽略 amount，按该合约当前全局剩余铸造尽可能多",
    )
    label: str | None = None

    @model_validator(mode="after")
    def _amount_or_fill(self) -> "MintTarget":
        # amount 与 fill 至少给一个：没有数量又没开 fill 的任务展开后是空计划，
        # 这里提前拦掉，避免用户以为「没填数量=全部」而静默铸出 0 份。
        if not self.fill and self.amount <= 0:
            raise ValueError("amount 必须 > 0，或设置 fill=true 自动吃满剩余额度")
        return self


class JobIn(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    targets: list[MintTarget] = Field(min_length=1)
    account_ids: list[int] | None = Field(
        default=None,
        description="缺省(null)表示全部启用账号；空列表 [] 表示未选中任何账号",
    )
    group: str | None = Field(default=None, description="按分组筛选账号")
    stop_on_first_failure: bool = False
    dry_run: bool = False


class TaskOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    job_id: int
    account_id: int
    address: str | None = None
    transistors: str
    token_id: int
    amount: int
    value_wei: int
    status: TaskStatus
    attempts: int = 0
    nonce: int | None = None
    tx_hash: str | None = None
    block_number: int | None = None
    gas_used: int | None = None
    error: str | None = None
    updated_at: datetime | None = None


class JobOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    status: JobStatus
    dry_run: bool = False
    # 从 jobs.config_json 回读，用于执行时决定「单账号首个失败即停止」
    stop_on_first_failure: bool = False
    created_at: datetime | None = None
    started_at: datetime | None = None
    finished_at: datetime | None = None
    total: int = 0
    confirmed: int = 0
    failed: int = 0
    pending: int = 0
    # 跳过（售罄 / 余额不足 / dry-run）也是终态，需单独计数，
    # 否则任务明明已结束，进度却永远停在 0。
    skipped: int = 0
    error: str | None = None
    progress: float = 0.0


class RunResult(BaseModel):
    job: JobOut
    tasks: list[TaskOut] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)
    meta: dict[str, Any] = Field(default_factory=dict)


class FundRequest(BaseModel):
    """从主钱包向账号分发 OKB。"""

    master_private_key: str = Field(min_length=64)
    amount_wei: int = Field(gt=0, description="每个账号补充到的目标余额")
    account_ids: list[int] | None = Field(
        default=None,
        description="缺省(null)表示全部启用账号；空列表 [] 表示未选中任何账号",
    )
    top_up: bool = Field(default=True, description="True=补足到目标余额，False=等额追加")
    dry_run: bool = False


class RunRequest(BaseModel):
    """触发任务执行时的可选参数。

    ``password`` 用于解密账号私钥；Web 场景下若未显式提供，则回退到
    环境变量 ``FABX_SECRET``。
    """

    password: str | None = None


class ImportRequest(BaseModel):
    """批量导入：``content`` 每行一个私钥，可选的 ``label`` 与 ``group`` 作为统一备注。

    ``password`` 放在请求体而非 URL：口令若走查询串会被服务器访问日志原样记录，
    违反「口令不落日志」的约定。
    """

    content: str = Field(min_length=1, description="每行一个私钥，允许带 0x 前缀")
    label_prefix: str | None = None
    group: str | None = None
    password: str | None = Field(
        default=None, description="加密口令；留空则使用服务端 FABX_SECRET"
    )


class FundResultItem(BaseModel):
    """单笔资金分发的执行结果。"""

    account_id: int
    address: str
    amount_wei: int
    status: str
    tx_hash: str | None = None
    error: str | None = None


class FundResult(BaseModel):
    master_address: str
    total_wei: int = 0
    sent: int = 0
    skipped: int = 0
    failed: int = 0
    # 已广播但未在超时内拿到回执：资金可能已经到账，绝不能当成失败让用户重发
    pending: int = 0
    items: list[FundResultItem] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)


# --------------------------------------------------------------------- 挖矿


class TapeoutRequest(BaseModel):
    """把电路网表 tapeout 成 ERC721 电路 NFT，需消耗账号持有的晶体管。"""

    account_id: int
    gates: list[list[Any]] = Field(
        min_length=1, description='门列表，如 [["NAND",2,2]]（REF 会被拒绝）'
    )
    n_in: int = Field(default=1, ge=0, description="输入引脚数")
    n_out: int = Field(default=1, ge=1, description="输出引脚数")
    circuits: str | None = Field(
        default=None, pattern=ADDRESS_PATTERN, description="电路合约；留空用默认 RefBench"
    )
    label: str | None = None
    commit: bool = Field(
        default=False,
        description="先 commitDesign 再 tapeout，用于防抢跑（多花一笔 gas）",
    )
    dry_run: bool = False


class TapeoutResult(BaseModel):
    account_id: int
    address: str
    circuits: str
    circuit_id: int
    tx_hash: str
    block_number: int
    gas_used: int
    fee_wei: int
    nand_burn: int
    latch_burn: int
    bstar: int
    area: int
    depth: int
    cost: int
    netlist_hex: str
    commit_tx_hash: str | None = None
    notes: list[str] = Field(default_factory=list)


class MinerInfo(BaseModel):
    """一个矿机的链上快照（``getMiner`` + 待领取代币）。"""

    key: str
    circuits: str
    circuit_id: int
    task_id: int
    registrant: str
    status: int
    status_text: str
    gate_count: int
    state_count: int
    depth: int
    area: int
    mult: int
    bstar: int
    bonus: int
    optimal: bool
    since: int
    commit_block: int
    # getMiner 返回体中保留的「首个未占用样本槽位」，claim/诊断时可能需要
    first_unused_id: int = 0
    stop_block: int
    nand_burn: int
    latch_burn: int
    verif_weight: int
    unver_weight: int
    debt: int
    boost_bps: int
    pending_wei: int = 0


class MiningStatus(BaseModel):
    """整条挖矿链路的链上状态概览。"""

    chain_id: int
    mining: str
    token: str
    started: bool
    start_time: int
    start_time_human: str
    current_rate: int
    total_mined: int
    daily_emission: int
    current_era: int
    paused: bool
    task_count: int
    miner_count: int
    pool_verif_weight: int
    pool_unver_weight: int
    pool_verif_miners: int
    pool_miners: int
    pool_unver_bps: int


class MineActionResult(BaseModel):
    """arm / start / claim / stop / reclaim 这类「发一笔交易」动作的通用结果。"""

    action: str
    status: str
    account_id: int | None = None
    address: str | None = None
    circuits: str | None = None
    circuit_id: int | None = None
    task_id: int | None = None
    key: str | None = None
    tx_hash: str | None = None
    block_number: int | None = None
    gas_used: int | None = None
    samples: int = 0
    # arm 的锚点区块：start 必须用同一个锚点区块做采样，rearm 场景要回传给调用方
    anchor_block: int | None = None
    detail: str | None = None


class CircuitIn(BaseModel):
    """本地记录的电路 NFT（tapeout 产物），用于后续 arm/start/claim。"""

    account_id: int
    circuits: str
    circuit_id: int
    n_in: int
    n_out: int
    n_state: int
    gate_count: int
    label: str | None = None
    salt: str | None = None
    tx_hash: str | None = None
    block_number: int | None = None
    # task_id / miner_key 在 tapeout 时还是空的，start 成功后由 set_circuit_miner 回填；
    # 允许在这里带上是为了「重放 / 迁移时一次性写入完整记录」。
    task_id: int | None = None
    miner_key: str | None = None


class CircuitOut(CircuitIn):
    model_config = ConfigDict(from_attributes=True)

    id: int
    address: str | None = None
    created_at: datetime | None = None
    task_id: int | None = None
    miner_key: str | None = None
