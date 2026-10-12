"""命令行入口：账号管理、CPU 同步、任务编排、资金分发与 Web 控制台。"""

from __future__ import annotations

import logging
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console
from rich.table import Table

from .accounts import import_many
from .chain import (
    format_okb,
    make_web3,
    native_balance,
    read_transistor_state,
    sync_cpus,
)
from .config import get_settings, is_loopback_host
from .crypto import resolve_password
from .db import Database
from .funding import distribute_funds
from .minter import MintEngine
from .models import FundRequest, JobIn, MintTarget
from .scheduler import AutoConfig, resolve_start_time, run_auto

console = Console()

app = typer.Typer(
    help="Fab X（X Layer）晶体管批量 mint 工具：SQLite 存储 + Web 控制台。",
    no_args_is_help=True,
    add_completion=False,
)
account_app = typer.Typer(help="账号管理（私钥加密入库）。", no_args_is_help=True)
job_app = typer.Typer(help="铸造任务编排。", no_args_is_help=True)
cpu_app = typer.Typer(help="CPU / 合约信息。", no_args_is_help=True)
app.add_typer(account_app, name="account")
app.add_typer(job_app, name="job")
app.add_typer(cpu_app, name="cpu")


def _db() -> Database:
    return Database(get_settings().resolved_db_path())


def _setup_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )


# --------------------------------------------------------------------- 账号
@account_app.command("import")
def account_import(
    file: Annotated[Path, typer.Argument(help="私钥文件，每行一个；支持 `私钥,备注`")],
    group: Annotated[str | None, typer.Option("--group", "-g", help="统一分组")] = None,
    label_prefix: Annotated[
        str | None, typer.Option("--label-prefix", help="无单独备注时的备注前缀")
    ] = None,
    verbose: Annotated[bool, typer.Option("--verbose", "-v")] = False,
) -> None:
    """从文件批量导入私钥并加密入库。"""
    _setup_logging(verbose)
    if not file.exists():
        console.print(f"[red]文件不存在: {file}[/red]")
        raise typer.Exit(code=1)
    password = resolve_password(confirm=True)
    content = file.read_text(encoding="utf-8")
    imported, errors = import_many(
        _db(), content, password, label_prefix=label_prefix, group=group
    )
    console.print(f"[green]导入成功 {len(imported)} 个账号[/green]")
    for message in errors:
        console.print(f"[yellow]{message}[/yellow]")


@account_app.command("list")
def account_list(
    group: Annotated[str | None, typer.Option("--group", "-g")] = None,
    enabled_only: Annotated[bool, typer.Option("--enabled-only")] = False,
    with_balance: Annotated[bool, typer.Option("--balance", "-b", help="查询链上余额")] = False,
) -> None:
    """列出账号。"""
    db = _db()
    accounts = db.list_accounts(group=group, enabled_only=enabled_only)
    if not accounts:
        console.print("[yellow]还没有账号[/yellow]")
        return

    balances: dict[str, int] = {}
    if with_balance:
        w3 = make_web3(get_settings())
        for account in accounts:
            try:
                balances[account.address] = native_balance(w3, account.address)
            except Exception as exc:
                console.print(f"[yellow]{account.address} 余额查询失败: {exc}[/yellow]")

    table = Table("ID", "地址", "备注", "分组", "启用", *(["OKB"] if with_balance else []))
    for account in accounts:
        row = [
            str(account.id),
            account.address,
            account.label or "-",
            account.group or "-",
            "是" if account.enabled else "否",
        ]
        if with_balance:
            row.append(format_okb(balances[account.address]) if account.address in balances else "?")
        table.add_row(*row)
    console.print(table)


@account_app.command("toggle")
def account_toggle(
    account_id: int,
    enabled: Annotated[bool, typer.Option("--enabled/--disabled")] = True,
) -> None:
    """启用或停用账号。"""
    db = _db()
    if db.get_account(account_id) is None:
        console.print(f"[red]账号 {account_id} 不存在[/red]")
        raise typer.Exit(code=1)
    db.set_account_enabled(account_id, enabled)
    console.print(f"[green]账号 {account_id} 已{'启用' if enabled else '停用'}[/green]")


@account_app.command("remove")
def account_remove(account_id: int) -> None:
    """删除账号。"""
    db = _db()
    if db.get_account(account_id) is None:
        console.print(f"[red]账号 {account_id} 不存在[/red]")
        raise typer.Exit(code=1)
    db.delete_account(account_id)
    console.print(f"[green]账号 {account_id} 已删除[/green]")


# ---------------------------------------------------------------------- CPU
@cpu_app.command("sync")
def cpu_sync(
    verbose: Annotated[bool, typer.Option("--verbose", "-v")] = False,
) -> None:
    """同步官方 CPU 列表与链上价格。"""
    _setup_logging(verbose)
    synced = sync_cpus(_db(), get_settings())
    console.print(f"[green]已同步 {len(synced)} 个 CPU[/green]")


@cpu_app.command("list")
def cpu_list() -> None:
    """列出已同步的 CPU。"""
    cpus = _db().list_cpus()
    if not cpus:
        console.print("[yellow]尚未同步，先执行 fabx cpu sync[/yellow]")
        return
    table = Table("名称", "晶体管", "单价 OKB", "已铸", "上限", "电路数")
    for cpu in cpus:
        table.add_row(
            cpu.name or "-",
            cpu.transistors,
            format_okb(cpu.mint_price_wei or 0),
            str(cpu.minted if cpu.minted is not None else "-"),
            str(cpu.supply_cap if cpu.supply_cap is not None else "-"),
            str(cpu.circuit_count if cpu.circuit_count is not None else "-"),
        )
    console.print(table)


@cpu_app.command("state")
def cpu_state(address: str) -> None:
    """读取某个晶体管合约的链上铸造状态。"""
    state = read_transistor_state(make_web3(get_settings()), address)
    console.print(f"合约:       {state.address}")
    console.print(f"名称:       {state.cpu_name or '-'}")
    console.print(f"NAND id:    {state.nand_id}（剩余 {state.remaining(state.nand_id)}）")
    console.print(f"LATCH id:   {state.latch_id}（剩余 {state.remaining(state.latch_id)}）")
    console.print(f"单价:       {format_okb(state.mint_price_wei)} OKB")
    console.print(f"协议费:     {format_okb(state.protocol_fee_wei)} OKB（每笔固定）")
    console.print(f"总已铸:     {state.minted} / 上限 {state.supply_cap}")


# ---------------------------------------------------------------------- 任务
def _parse_ids(text: str | None) -> list[int] | None:
    if not text:
        return None
    ids: list[int] = []
    for part in text.replace(",", " ").split():
        try:
            ids.append(int(part))
        except ValueError as exc:
            # 账号 ID 必须可解析为整数；交由 Typer 统一报错，避免抛出裸 traceback
            raise typer.BadParameter(f"账号 ID 必须是整数：{part!r}") from exc
    return ids


@job_app.command("create")
def job_create(
    transistors: Annotated[str, typer.Option("--transistors", "-t", help="晶体管合约地址")],
    token_id: Annotated[int, typer.Option("--token-id", "-i", help="token id（NAND/LATCH 见 cpu state）")],
    amount: Annotated[int, typer.Option("--amount", "-n", help="每账号铸造数量")] = 1,
    name: Annotated[str | None, typer.Option("--name", help="任务名称")] = None,
    group: Annotated[str | None, typer.Option("--group", "-g")] = None,
    accounts: Annotated[str | None, typer.Option("--accounts", "-a", help="账号 ID，逗号分隔")] = None,
    dry_run: Annotated[bool, typer.Option("--dry-run")] = False,
    stop_on_first_failure: Annotated[bool, typer.Option("--stop-on-failure")] = False,
    verbose: Annotated[bool, typer.Option("--verbose", "-v")] = False,
) -> None:
    """创建铸造任务（只落库，不广播）。"""
    _setup_logging(verbose)
    job = JobIn(
        name=name or f"mint {transistors} id={token_id} x{amount}",
        targets=[MintTarget(transistors=transistors, token_id=token_id, amount=amount)],
        account_ids=_parse_ids(accounts),
        group=group,
        stop_on_first_failure=stop_on_first_failure,
        dry_run=dry_run,
    )
    engine = MintEngine(_db(), get_settings())
    try:
        job_id, notes = engine.create_job(job)
    except ValueError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(code=1) from exc
    console.print(f"[green]任务 #{job_id} 已创建[/green]")
    for note in notes:
        console.print(f"[yellow]{note}[/yellow]")


@job_app.command("list")
def job_list(limit: Annotated[int, typer.Option("--limit", "-l")] = 20) -> None:
    """列出任务。"""
    jobs = _db().list_jobs(limit=limit)
    if not jobs:
        console.print("[yellow]暂无任务[/yellow]")
        return
    table = Table("ID", "名称", "状态", "确认", "失败", "跳过", "待处理", "dry-run")
    for job in jobs:
        table.add_row(
            str(job.id),
            job.name,
            job.status.value,
            str(job.confirmed),
            str(job.failed),
            str(job.skipped),
            str(job.pending),
            "是" if job.dry_run else "否",
        )
    console.print(table)


@job_app.command("show")
def job_show(job_id: int) -> None:
    """查看任务明细。"""
    db = _db()
    job = db.get_job(job_id)
    if job is None:
        console.print(f"[red]任务 {job_id} 不存在[/red]")
        raise typer.Exit(code=1)
    console.print(
        f"任务 #{job.id} {job.name} 状态={job.status.value} "
        f"确认={job.confirmed}/{job.total} 失败={job.failed} "
        f"跳过={job.skipped} 待处理={job.pending}"
    )
    tasks = db.list_tasks(job_id=job_id, limit=5000)
    table = Table("任务", "账号", "token", "数量", "花费 OKB", "状态", "交易", "错误")
    for task in tasks:
        table.add_row(
            str(task.id),
            task.address or str(task.account_id),
            str(task.token_id),
            str(task.amount),
            format_okb(task.value_wei),
            task.status.value,
            (task.tx_hash[:12] + "…") if task.tx_hash else "-",
            (task.error or "")[:60],
        )
    console.print(table)


@job_app.command("run")
def job_run(
    job_id: int,
    verbose: Annotated[bool, typer.Option("--verbose", "-v")] = False,
) -> None:
    """执行任务（真实广播；dry-run 任务只做标记）。"""
    _setup_logging(verbose)
    engine = MintEngine(_db(), get_settings())
    try:
        report = engine.run_job(job_id)
    except ValueError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(code=1) from exc
    console.print(
        f"[green]任务 #{job_id} 结束：成功 {report.confirmed}，失败 {report.failed}，"
        f"跳过 {report.skipped}[/green]"
    )
    for note in report.notes:
        console.print(f"  · {note}")


@job_app.command("reset")
def job_reset(job_id: int) -> None:
    """把失败/跳过的任务重置为待执行，便于充值或售罄恢复后重跑。

    已广播（sent）与已确认（confirmed）的任务不在此重置：它们可能已经上链，
    重跑时会按 tx_hash 对账而不是重新广播，避免重复扣款。
    """
    db = _db()
    if db.get_job(job_id) is None:
        console.print(f"[red]任务 {job_id} 不存在[/red]")
        raise typer.Exit(code=1)
    count = db.reset_job_tasks(job_id)
    console.print(f"[green]已重置 {count} 条任务[/green]")


# ---------------------------------------------------------------------- 资金
@app.command("balance")
def balance(address: str) -> None:
    """查询地址余额。"""
    wei = native_balance(make_web3(get_settings()), address)
    console.print(f"{address}: {format_okb(wei)} OKB（{wei} wei）")


@app.command("fund")
def fund(
    master_key_file: Annotated[
        Path, typer.Option("--key-file", help="主钱包私钥文件（仅含一行私钥）")
    ],
    amount: Annotated[str, typer.Option("--amount", "-a", help="每个账号目标余额（OKB）")],
    group: Annotated[str | None, typer.Option("--group", "-g")] = None,
    accounts: Annotated[str | None, typer.Option("--accounts", help="账号 ID，逗号分隔")] = None,
    append: Annotated[bool, typer.Option("--append", help="等额追加而非补足到目标余额")] = False,
    dry_run: Annotated[bool, typer.Option("--dry-run")] = False,
    verbose: Annotated[bool, typer.Option("--verbose", "-v")] = False,
) -> None:
    """从主钱包向账号分发 OKB。"""
    _setup_logging(verbose)
    if not master_key_file.exists():
        console.print(f"[red]私钥文件不存在: {master_key_file}[/red]")
        raise typer.Exit(code=1)
    master_key = master_key_file.read_text(encoding="utf-8").strip()
    key_body = master_key[2:] if master_key[:2].lower() == "0x" else master_key
    try:
        int(key_body, 16)
    except ValueError:
        console.print(
            "[red]主钱包私钥格式不正确（应为 64 位十六进制，可带 0x 前缀）[/red]"
        )
        raise typer.Exit(code=1) from None
    if len(key_body) != 64:
        console.print("[red]主钱包私钥长度不正确（应为 32 字节 / 64 位十六进制）[/red]")
        raise typer.Exit(code=1)
    try:
        # 用 Decimal 做定点换算，避免 float 在 18 位小数上的精度丢失
        decimal_amount = Decimal(amount)
        if decimal_amount.as_tuple().exponent < -18:
            raise InvalidOperation("OKB 金额最多支持 18 位小数")
        amount_wei = int(decimal_amount * Decimal(10**18))
    except (InvalidOperation, ValueError) as exc:
        console.print(f"[red]金额格式不正确: {amount}[/red]")
        raise typer.Exit(code=1) from exc
    if amount_wei <= 0:
        console.print("[red]金额必须大于 0[/red]")
        raise typer.Exit(code=1)
    request = FundRequest(
        master_private_key=master_key,
        amount_wei=amount_wei,
        account_ids=_parse_ids(accounts),
        top_up=not append,
        dry_run=dry_run,
    )
    try:
        result = distribute_funds(_db(), get_settings(), request)
    except ValueError as exc:
        # 目标账号为空 / 主钱包私钥非法等属于请求问题，给出清晰提示而非 traceback
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(code=1) from exc
    console.print(
        f"[green]分发完成：成功 {result.sent}，跳过 {result.skipped}，失败 {result.failed}，"
        f"待确认 {result.pending}，合计 {format_okb(result.total_wei)} OKB[/green]"
    )
    for note in result.notes:
        console.print(f"  · {note}")


# ----------------------------------------------------------------------- 定时
@app.command("auto")
def auto(
    at: Annotated[
        str,
        typer.Option("--at", help="触发时间：HH:MM（上海时区）或 ISO8601；留空=下一个 10:00"),
    ] = "",
    amount: Annotated[int, typer.Option("--amount", "-n", help="每账号铸造数量")] = 1,
    accounts: Annotated[str | None, typer.Option("--accounts", "-a", help="账号 ID，逗号分隔")] = None,
    group: Annotated[str | None, typer.Option("--group", "-g", help="按分组筛选账号")] = None,
    target: Annotated[
        str,
        typer.Option("--target", help="latest=当前最新合约；new=等工厂新增后再动手"),
    ] = "latest",
    token_ids: Annotated[
        str | None,
        typer.Option("--token-ids", help="token id，逗号分隔；留空=该合约 NAND+LATCH"),
    ] = None,
    lead_seconds: Annotated[
        float, typer.Option("--lead-seconds", help="提前多少秒开始探测新合约（new 抢跑窗口）")
    ] = 0.0,
    poll_interval: Annotated[
        float, typer.Option("--poll-interval", help="探测/重试间隔秒数")
    ] = 1.0,
    dry_run: Annotated[bool, typer.Option("--dry-run")] = False,
    name: Annotated[str | None, typer.Option("--name", help="任务名称")] = None,
    verbose: Annotated[bool, typer.Option("--verbose", "-v")] = False,
) -> None:
    """到点自动发现最新（或新增的）合约并批量铸造。

    口令会在启动时先解析好（环境变量或交互输入），避免到点再输入而错过开场。
    """
    _setup_logging(verbose)
    if target not in {"latest", "new"}:
        console.print(f"[red]未知的 target：{target}（可选 latest / new）[/red]")
        raise typer.Exit(code=1)
    if amount <= 0:
        console.print("[red]数量必须大于 0[/red]")
        raise typer.Exit(code=1)
    try:
        start_at = resolve_start_time(at)
    except ValueError as exc:
        console.print(f"[red]触发时间解析失败：{exc}[/red]")
        raise typer.Exit(code=1) from exc
    # 抢跑需要口令在等待前就绪：先解析（必要时交互输入），再阻塞等点。
    password = resolve_password(None)
    cfg = AutoConfig(
        at=start_at,
        amount=amount,
        account_ids=_parse_ids(accounts),
        group=group,
        target=target,
        token_ids=_parse_ids(token_ids),
        poll_interval=poll_interval,
        lead_seconds=lead_seconds,
        dry_run=dry_run,
        name=name,
    )
    console.print(
        f"[green]已排定：{start_at.isoformat()}（上海时区）触发，"
        f"目标={target}，每账号 {amount} 份。等待中…[/green]"
    )
    try:
        job_id, report = run_auto(
            _db(),
            get_settings(),
            cfg,
            password=password,
            progress=lambda msg: console.print(f"  · {msg}"),
        )
    except ValueError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(code=1) from exc
    console.print(
        f"[green]任务 #{job_id} 结束：成功 {report.confirmed}，失败 {report.failed}，"
        f"跳过 {report.skipped}[/green]"
    )
    for note in report.notes:
        console.print(f"  · {note}")


# ----------------------------------------------------------------------- Web
@app.command("web")
def web(
    host: Annotated[str | None, typer.Option("--host")] = None,
    port: Annotated[int | None, typer.Option("--port", "-p")] = None,
    reload: Annotated[bool, typer.Option("--reload")] = False,
) -> None:
    """启动 Web 控制台。"""
    import uvicorn

    settings = get_settings()
    bind_host = host or settings.web_host
    bind_port = port or settings.web_port
    # 监听非回环地址时必须配置访问令牌，否则控制台会暴露私钥管理接口
    if not is_loopback_host(bind_host) and settings.web_token is None:
        console.print(
            "[red]监听非本机地址时必须设置 FABX_WEB_TOKEN，"
            "否则 Web 控制台会拒绝所有 /api 请求。[/red]"
        )
        raise typer.Exit(code=1)
    console.print(f"[green]Web 控制台: http://{bind_host}:{bind_port}[/green]")
    uvicorn.run(
        "fabx_minter.web.app:app",
        host=bind_host,
        port=bind_port,
        reload=reload,
        log_level="info",
    )


@app.command("sync")
def sync(verbose: Annotated[bool, typer.Option("--verbose", "-v")] = False) -> None:
    """同步 CPU 列表（`fabx cpu sync` 的简写）。"""
    _setup_logging(verbose)
    synced = sync_cpus(_db(), get_settings())
    console.print(f"[green]已同步 {len(synced)} 个 CPU[/green]")


if __name__ == "__main__":  # pragma: no cover
    app()
