"""领域模型：Web/CLI 与存储层共用的 Pydantic 结构。"""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

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
    amount: int = Field(gt=0, description="每个账号铸造的数量")
    label: str | None = None


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
