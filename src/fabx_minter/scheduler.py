"""定时自动铸造：到点后自动发现最新晶体管合约并批量 mint。

设计取舍：
- 时间统一按上海时区解释：``HH:MM`` 若当天已过则顺延到次日，避免误触发；
- 目标合约默认取工厂当前最新的 CPU（``cpuAt(cpuCount - 1)``），也可选
  ``target="new"`` 等工厂新增 CPU 后再动手，适合「活动开新合约」的抢跑场景；
- 本模块只负责「等到点 + 找到目标 + 建任务」，真正的签名广播仍交给 MintEngine，
  因此自动模式与手动模式共用同一套 nonce 串行、余额预检、售罄跳过与链上对账逻辑。
"""

from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Callable
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from web3 import Web3

from .chain import (
    ChainReadError,
    circuits_contract,
    factory_contract,
    make_web3,
    read_transistor_state,
)
from .config import Settings
from .db import Database
from .minter import MintEngine, RunReport
from .models import JobIn, MintTarget

log = logging.getLogger("fabx.scheduler")

try:
    SHANGHAI_TZ = ZoneInfo("Asia/Shanghai")
except ZoneInfoNotFoundError:  # 极简环境下缺 tzdata 时回退固定 +8，保证功能可用
    SHANGHAI_TZ = timezone(timedelta(hours=8))

_HHMM_RE = re.compile(r"^(\d{1,2}):(\d{2})$")


def now_shanghai() -> datetime:
    """当前上海时间（带时区），供调度与测试注入。"""
    return datetime.now(SHANGHAI_TZ)


def resolve_start_time(text: str | None, *, now: datetime | None = None) -> datetime:
    """把外部传入的触发时间解析为带时区的绝对时刻。

    - 留空：下一个上海时间 10:00（口语「今天 10 点」的默认目标）；
    - ``HH:MM``：当天该时刻；若已经过去则顺延到次日，避免解析成过去时刻后立即开跑；
    - ISO8601：按原文解析；不带时区时视为上海时间。
    """
    current = now or now_shanghai()
    if current.tzinfo is None:
        current = current.replace(tzinfo=SHANGHAI_TZ)

    if not text or not text.strip():
        target = current.replace(hour=10, minute=0, second=0, microsecond=0)
        if target <= current:
            target += timedelta(days=1)
        return target

    text = text.strip()
    match = _HHMM_RE.match(text)
    if match:
        hour, minute = int(match.group(1)), int(match.group(2))
        if hour > 23 or minute > 59:
            raise ValueError(f"时间不合法：{text!r}")
        target = current.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if target <= current:
            target += timedelta(days=1)
        return target

    parsed = datetime.fromisoformat(text)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=SHANGHAI_TZ)
    return parsed


def cpu_count(w3: Web3, settings: Settings) -> int:
    """读取工厂当前已创建的 CPU 数量。"""
    factory = factory_contract(w3, settings.circuit_factory_address)
    return int(factory.functions.cpuCount().call())


def _circuits_transistors(w3: Web3, circuits_address: str) -> str:
    """从 CPU 合约取晶体管合约地址，兼容两种字段名。"""
    contract = circuits_contract(w3, circuits_address)
    for name in ("transistors", "transistorsContract"):
        try:
            return Web3.to_checksum_address(getattr(contract.functions, name)().call())
        except Exception as exc:  # 视图不存在或未初始化时换下一个候选
            log.debug("读取 %s.%s 失败: %s", circuits_address, name, exc)
    raise ChainReadError(f"无法从 {circuits_address} 读取晶体管合约地址")


def latest_transistors(w3: Web3, settings: Settings) -> str:
    """返回工厂中最新一个 CPU 对应的晶体管合约地址。"""
    factory = factory_contract(w3, settings.circuit_factory_address)
    count = int(factory.functions.cpuCount().call())
    if count <= 0:
        raise ChainReadError("工厂里还没有任何 CPU，无法确定目标合约")
    circuits_address = Web3.to_checksum_address(factory.functions.cpuAt(count - 1).call())
    return _circuits_transistors(w3, circuits_address)


@dataclass(slots=True)
class AutoConfig:
    """自动铸造的一次性配置。"""

    at: datetime
    amount: int = 1
    account_ids: list[int] | None = None
    group: str | None = None
    target: str = "latest"  # latest=当前最新合约；new=等工厂新增后再动手
    token_ids: list[int] | None = None  # 留空则对该合约的 NAND + LATCH 都铸
    poll_interval: float = 1.0
    lead_seconds: float = 0.0  # 提前多少秒开始探测新合约（抢跑），0 表示到点才探测
    dry_run: bool = False
    name: str | None = None


def _sleep_seconds(now: datetime, cfg: AutoConfig) -> float:
    """算下一次检查的间隔：离触发还远就稀疏轮询，临近或进入抢跑窗口再加密。"""
    remaining = (cfg.at - now).total_seconds()
    if remaining <= 0:
        return 0.0
    in_lead = cfg.lead_seconds > 0 and remaining <= cfg.lead_seconds
    if in_lead or remaining <= 30:
        return min(cfg.poll_interval, remaining)
    # 离触发还早，没必要每秒打扰 RPC；最多 15 秒检查一次即可
    return min(15.0, remaining)


def _detect_new_transistors(
    w3: Web3,
    settings: Settings,
    baseline: int,
    cached: str | None,
    say: Callable[[str], None],
) -> str | None:
    """探测工厂是否新增了 CPU；发现新合约就返回其晶体管地址。"""
    try:
        count = cpu_count(w3, settings)
    except Exception as exc:  # RPC 抖动不应结束等待，下个周期再试
        log.warning("读取工厂 CPU 数量失败: %s", exc)
        return cached
    if count <= baseline:
        return cached
    try:
        transistors = latest_transistors(w3, settings)
    except Exception as exc:
        log.warning("读取最新 CPU 失败: %s", exc)
        return cached
    if transistors != cached:
        say(f"发现新 CPU 合约: {transistors}")
    return transistors


def _wait_until(
    cfg: AutoConfig,
    *,
    now_fn: Callable[[], datetime],
    sleeper: Callable[[float], None],
    on_tick: Callable[[datetime], None] | None = None,
) -> None:
    """阻塞到触发时刻；期间按需回调（用于抢跑探测）。"""
    while True:
        now = now_fn()
        if now >= cfg.at:
            return
        if on_tick is not None:
            on_tick(now)
        sleeper(_sleep_seconds(now, cfg))


def run_auto(
    db: Database,
    settings: Settings,
    cfg: AutoConfig,
    *,
    password: str | None = None,
    now_fn: Callable[[], datetime] = now_shanghai,
    sleeper: Callable[[float], None] = time.sleep,
    progress: Callable[[str], None] | None = None,
) -> tuple[int, RunReport]:
    """等待到点，自动定位目标合约并执行一次批量 mint。

    返回 ``(job_id, report)``。口令请在调用前解析好传入：抢跑场景下到点后再要求
    输入口令，往往已经错过开场。
    """
    say = progress or (lambda _msg: None)
    w3 = make_web3(settings)

    baseline = cpu_count(w3, settings)
    say(f"工厂当前 CPU 数量: {baseline}；触发时间: {cfg.at.isoformat()}")

    discovered: str | None = None

    def tick(now: datetime) -> None:
        nonlocal discovered
        if cfg.lead_seconds <= 0 or cfg.target != "new":
            return
        if (cfg.at - now).total_seconds() <= cfg.lead_seconds:
            discovered = _detect_new_transistors(w3, settings, baseline, discovered, say)

    _wait_until(cfg, now_fn=now_fn, sleeper=sleeper, on_tick=tick)
    say("到达触发时间，开始铸造")

    if cfg.target == "new":
        if discovered is None:
            say(f"等待新 CPU 部署（当前 {baseline} 个）…")
            while discovered is None:
                discovered = _detect_new_transistors(w3, settings, baseline, None, say)
                if discovered is None:
                    sleeper(cfg.poll_interval)
        transistors = discovered
    elif cfg.target == "latest":
        transistors = latest_transistors(w3, settings)
    else:
        raise ValueError(f"未知的 target：{cfg.target!r}（可选 latest / new）")

    # 新合约刚部署时视图数据可能还没就绪，读状态失败就短暂重试，抢在额度被扫空之前。
    state = None
    deadline = now_fn() + timedelta(seconds=30)
    while True:
        try:
            state = read_transistor_state(w3, transistors)
            break
        except Exception as exc:
            if now_fn() >= deadline:
                raise ChainReadError(f"读取 {transistors} 链上状态失败: {exc}") from exc
            log.warning("读取 %s 状态失败，重试: %s", transistors, exc)
            sleeper(cfg.poll_interval)

    token_ids = cfg.token_ids or [state.nand_id, state.latch_id]
    targets = [
        MintTarget(transistors=state.address, token_id=int(token_id), amount=cfg.amount)
        for token_id in token_ids
    ]
    job = JobIn(
        name=cfg.name or f"auto {state.cpu_name or state.address}",
        targets=targets,
        account_ids=cfg.account_ids,
        group=cfg.group,
        dry_run=cfg.dry_run,
    )
    say(
        f"目标 {state.address}（{state.cpu_name or '-'}），"
        f"单价 {state.mint_price_wei} wei + 协议费 {state.protocol_fee_wei} wei/笔，"
        f"token {token_ids}"
    )

    engine = MintEngine(db, settings, password)
    job_id, notes = engine.create_job(job)
    for note in notes:
        say(note)
    report = engine.run_job(job_id)
    return job_id, report
