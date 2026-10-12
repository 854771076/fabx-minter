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

    # 合约地址（fabx.tech 前端产物 config-DJaFdDfO.js）
    circuit_factory_address: str = "0x138fA3B38b1461B5908C455ABD30fC55029b7e71"
    marketplace_address: str = "0xF48DD28A992f891D781dFBEe59Ab03dDAfD5a02C"
    circuit_market_address: str = "0x1ca1C665a7630560E237c010F879b59086D715BB"
    pod_mining_address: str = "0x37E1518Dd3c5A173Ca4773F3b252440c10FAe9D1"
    xmake_address: str = "0x551630E595789b7e91948368c1b751404b5d75ab"
    multicall3_address: str = "0xcA11bde05977b3631167028862bE2a173976CA11"

    # 存储
    db_path: Path = Field(default=_project_root() / "var" / "fabx.db")

    # 密钥
    secret: SecretStr | None = None

    # 执行参数
    concurrency: int = 5
    gas_limit: int = 0  # 0 表示链上估算
    gas_price_gwei: float = 0.0  # 0 表示取链上 gasPrice
    gas_multiplier: float = 1.15
    tx_timeout: int = 180
    max_retries: int = 2
    rpc_timeout: float = 30.0

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

    @field_validator("gas_multiplier")
    @classmethod
    def _sane_multiplier(cls, v: float) -> float:
        if v < 1.0:
            raise ValueError("FABX_GAS_MULTIPLIER 必须 >= 1.0")
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
