"""运行配置：全部通过环境变量 / .env 注入，使用 pydantic-settings 校验。"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


def _project_root() -> Path:
    # src/fabx_minter/config.py -> 项目根目录
    return Path(__file__).resolve().parents[2]


def is_loopback_host(host: str) -> bool:
    """判断监听地址是否只对本机可见（用于 Web 暴露前的安全检查）。"""
    value = (host or "").strip().lower()
    return value in {"localhost", "::1", "[::1]"} or value.startswith("127.")


class Settings(BaseSettings):
    """全局配置。

    设计约定：
    - 私钥口令只从环境变量或交互输入读取，不落库、不写日志。
    - 合约地址集中在此处，方便链上升级 / 换部署时替换。
    """

    model_config = SettingsConfigDict(
        env_prefix="FABX_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    rpc_url: str = "https://rpc.xmake.org/rpc/196"
    chain_id: int = 196
    api_url: str = "https://api.fabx.tech"

    # 合约地址取自 fabx.tech 前端产物 config-BhXPazbF.js（2026-10 v2 部署）。
    # 站点会「整站重新部署」并更换全套地址：旧工厂 0x138fA3B3… 的两个 CPU 已售罄，
    # 新工厂下才有额度。地址变了就覆盖 FABX_CIRCUIT_FACTORY_ADDRESS 或直接改这里的默认值。
    circuit_factory_address: str = "0x82935793a854fA2F2e8A9d2B3F17BbfBf81D6C05"
    marketplace_address: str = "0x7F955C233FDb17e98FB26F330D778D3E30fe6Ee6"
    circuit_market_address: str = "0x0BC37F95aE57bb20e995D425d46D6df9f22Bd7a1"
    pod_mining_address: str = "0xB6d4d03e9DfD3aD4889241e23F12EA4557696018"
    xmake_address: str = "0xC8c0D268563F59D6cA3A42C48450A6A8Af399843"
    multicall3_address: str = "0xcA11bde05977b3631167028862bE2a173976CA11"
    # PoD 挖矿（深度挖矿）配套地址，与上面同批取自 config-Ctx28aMr.js。
    # PodLens 是只读透镜（任务参考、采样、预估），PodMining 才是真正收 `arm/start/claim` 的合约。
    pod_lens_address: str = "0xf63aFD2D7AFCff789882d9C2Ee3a56efffc2301d"
    emission_switch_address: str = "0xb8eA0c234b85Ee76e580240726345ea5C0844945"
    # 默认挖矿目标：RefBench 是当前唯一「晶体管还能铸、挖矿倍率为 1」的处理器族。
    default_circuits_address: str = "0x265E2580cB9c3FC059f2AA3cf20ad30F3F0A653C"
    default_transistors_address: str = "0x158341E42F00f658C86e3d42bC4c397Df16795DA"

    # 存储
    db_path: Path = Field(default=_project_root() / "var" / "fabx.db")

    # 密钥
    secret: SecretStr | None = None

    # 执行参数
    concurrency: int = 5
    gas_limit: int = 0  # 0 表示链上估算
    gas_price_gwei: float = 0.0  # 0 表示取链上 gasPrice
    gas_multiplier: float = 1.15
    # 动态 gas 上限：链上拥堵时 gasPrice 会抬升，这里设一个硬上限。
    # 计算出的价格超过上限时不再盲目加价，而是跳过该笔并提示，避免在拥堵中付出离谱手续费。
    # 0 表示不设上限。
    gas_price_max_gwei: float = 0.0
    # 交易广播后若在 pending_timeout 秒内仍未打包，视为「卡住」：
    # 按 gas_bump_percent 加价、用同一 nonce 重发替换交易来解拥堵。
    # pending_timeout<=0 或 gas_bump_max_attempts<=0 表示关闭加价替换，退回只等 tx_timeout。
    pending_timeout: int = 45
    gas_bump_percent: float = 20.0
    gas_bump_max_attempts: int = 2
    tx_timeout: int = 180
    max_retries: int = 2
    rpc_timeout: float = 30.0

    # 同一账号内「连续广播、稍后统一等回执」的交易条数。
    # 1 = 传统逐笔等待（每笔确认后才发下一笔）；>1 时同一账号按 nonce 顺序连发多笔再收执。
    # 因为 nonce 严格递增，连发不会撞车，只是把多笔交易的等待时间重叠起来，
    # 所以在账号内待发交易 >1（例如 NAND + LATCH 各一笔）时能明显提速。
    pipeline_txs: int = 4

    # 挖矿（tapeout / arm / start / claim / stop）执行参数
    # tapeout 需要随交易支付的固定费用；0 表示不本地校验，直接按合约实时 TAPEOUT_FEE 付。
    tapeout_fee_wei: int = 0
    # tapeout / arm / claim / stop 的 gas 上限；0 表示走链上估算。
    mine_gas_limit: int = 0
    # start 要在链上重放全部样本，gas 波动大且估算容易失败，这里给固定上限兜底。
    mine_start_gas_limit: int = 1_500_000
    # 防 MEV 的设计提交盐（bytes32 hex）。留空表示使用全零盐，不做提前提交。
    mine_salt: SecretStr | None = None
    # arm 之后等待「采样锚点区块」出现的轮询间隔与超时秒数。
    mine_poll_interval: float = 2.0
    mine_arm_timeout: int = 60

    # Web
    web_host: str = "127.0.0.1"
    web_port: int = 8787
    # 非回环部署时必须配置访问令牌，否则 /api 业务接口一律拒绝；
    # 回环（本机）部署可留空，方便日常使用。
    web_token: SecretStr | None = None

    @field_validator("concurrency")
    @classmethod
    def _positive_concurrency(cls, v: int) -> int:
        if v < 1:
            raise ValueError("FABX_CONCURRENCY 必须 >= 1")
        return v

    @field_validator("pipeline_txs")
    @classmethod
    def _positive_pipeline(cls, v: int) -> int:
        if v < 1:
            raise ValueError("FABX_PIPELINE_TXS 必须 >= 1")
        return v

    @field_validator("mine_poll_interval")
    @classmethod
    def _positive_poll(cls, v: float) -> float:
        if v <= 0:
            raise ValueError("FABX_MINE_POLL_INTERVAL 必须 > 0")
        return v

    @field_validator("gas_multiplier")
    @classmethod
    def _sane_multiplier(cls, v: float) -> float:
        if v < 1.0:
            raise ValueError("FABX_GAS_MULTIPLIER 必须 >= 1.0")
        return v

    @field_validator("gas_price_max_gwei")
    @classmethod
    def _non_negative_cap(cls, v: float) -> float:
        if v < 0:
            raise ValueError("FABX_GAS_PRICE_MAX_GWEI 不能为负数")
        return v

    @field_validator("pending_timeout")
    @classmethod
    def _non_negative_pending(cls, v: int) -> int:
        if v < 0:
            raise ValueError("FABX_PENDING_TIMEOUT 不能为负数")
        return v

    @field_validator("gas_bump_percent")
    @classmethod
    def _non_negative_bump(cls, v: float) -> float:
        if v < 0:
            raise ValueError("FABX_GAS_BUMP_PERCENT 不能为负数")
        return v

    @field_validator("gas_bump_max_attempts")
    @classmethod
    def _non_negative_bump_attempts(cls, v: int) -> int:
        if v < 0:
            raise ValueError("FABX_GAS_BUMP_MAX_ATTEMPTS 不能为负数")
        return v

    def resolved_db_path(self) -> Path:
        """把相对路径解析到项目根目录，避免受启动目录影响。"""
        path = self.db_path
        if not path.is_absolute():
            path = _project_root() / path
        path.parent.mkdir(parents=True, exist_ok=True)
        return path


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
