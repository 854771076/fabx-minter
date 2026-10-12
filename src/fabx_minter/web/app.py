"""Fab X 批量 mint Web 控制台。

职责边界：
- 账号、CPU、任务、事件全部通过 SQLite 持久化，进程重启不丢状态；
- 长耗时动作（任务执行、资金分发）放到后台线程，接口立即返回，进度由列表接口轮询；
- 私钥/口令只在请求内存与链上签名瞬间出现，不写日志。
"""

from __future__ import annotations

import hmac
import logging
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel
from starlette.requests import Request
from starlette.responses import Response

from ..accounts import import_account, import_many
from ..chain import (
    ChainReadError,
    format_okb,
    make_web3,
    native_balance,
    read_transistor_state,
    sync_cpus,
)
from ..config import Settings, get_settings, is_loopback_host
from ..crypto import SecretError, resolve_password
from ..db import Database
from ..funding import distribute_funds
from ..minter import MintEngine
from ..models import (
    AccountIn,
    AccountOut,
    CpuOut,
    FundRequest,
    FundResult,
    ImportRequest,
    JobIn,
    JobOut,
    RunRequest,
    TaskOut,
)

log = logging.getLogger("fabx.web")

TEMPLATE_DIR = Path(__file__).resolve().parent / "templates"


def _same_origin(origin: str, host: str | None) -> bool:
    """校验写请求的 Origin 与 Host 同源，作为 CSRF 的兜底防线。"""
    if not host:
        return False
    return urlsplit(origin).netloc.lower() == host.lower()


def _bearer_token(request: Request) -> str | None:
    header = request.headers.get("authorization", "")
    if header.lower().startswith("bearer "):
        return header[7:].strip() or None
    return None


class AccountPatch(BaseModel):
    """账号的部分更新：启用状态、备注、分组。"""

    enabled: bool | None = None
    label: str | None = None
    group: str | None = None


class AccountCreate(AccountIn):
    """新增单个账号的请求体。

    口令随请求体传入而非查询串：URL 会被服务器访问日志记录，
    把口令放进 URL 等于把明文口令写进日志。
    """

    password: str | None = None


def _balance_map(db: Database, settings: Settings) -> dict[str, int]:
    """并发读取所有账号的原生代币余额；单个失败不影响其余账号。"""
    try:
        w3 = make_web3(settings)
    except Exception as exc:  # pragma: no cover - 构造失败极少见
        log.warning("初始化 Web3 失败: %s", exc)
        return {}
    accounts = db.list_accounts()
    if not accounts:
        return {}
    out: dict[str, int] = {}

    def fetch(address: str) -> tuple[str, int | None]:
        try:
            return address, native_balance(w3, address)
        except Exception as exc:
            log.warning("读取 %s 余额失败: %s", address, exc)
            return address, None

    # 逐个串行查询在账号多时会明显拖慢列表接口，这里用线程池并行拉取
    with ThreadPoolExecutor(max_workers=min(8, len(accounts))) as pool:
        for address, balance in pool.map(fetch, [a.address for a in accounts]):
            if balance is not None:
                out[address] = balance
    return out


def create_app(
    settings: Settings | None = None, db: Database | None = None
) -> FastAPI:
    settings = settings or get_settings()
    database = db or Database(settings.resolved_db_path())

    app = FastAPI(title="Fab X 批量 Mint 控制台", version="0.1.0")
    templates = Jinja2Templates(directory=str(TEMPLATE_DIR))

    expected_token = settings.web_token.get_secret_value() if settings.web_token else None
    trusted_local = is_loopback_host(settings.web_host)

    @app.middleware("http")
    async def _guard(request: Request, call_next: Any) -> Any:
        """统一入口防护：访问令牌 + 同源写请求 + 非回环暴露兜底。

        - 配置了 FABX_WEB_TOKEN：/api 业务接口必须携带匹配的 X-FABX-Token 或 Bearer；
        - 未配置且监听非回环地址：直接拒绝 /api 业务接口，避免控制台裸奔到公网；
        - 写请求校验 Origin 与 Host 同源，拦截网页发起的跨站请求。
        """
        path = request.url.path
        if path.startswith("/api/") and path != "/api/health":
            if expected_token is not None:
                provided = request.headers.get("x-fabx-token") or _bearer_token(request)
                if not provided or not hmac.compare_digest(provided, expected_token):
                    return JSONResponse(
                        {"detail": "缺少或错误的访问令牌（X-FABX-Token）"},
                        status_code=401,
                    )
            elif not trusted_local:
                return JSONResponse(
                    {"detail": "服务监听非回环地址但未配置 FABX_WEB_TOKEN，已拒绝访问"},
                    status_code=403,
                )
        if request.method in {"POST", "PUT", "PATCH", "DELETE"}:
            origin = request.headers.get("origin")
            if origin and not _same_origin(origin, request.headers.get("host")):
                return JSONResponse({"detail": "跨站请求被拒绝"}, status_code=403)
        return await call_next(request)

    # 正在执行的任务/资金请求，避免同一任务被重复触发
    running_jobs: set[int] = set()
    run_lock = threading.Lock()

    def _password(explicit: str | None) -> str | None:
        if explicit:
            return explicit
        if settings.secret is not None:
            return settings.secret.get_secret_value()
        return None

    def require_account(account_id: int) -> AccountOut:
        account = database.get_account(account_id)
        if account is None:
            raise HTTPException(status_code=404, detail=f"账号 {account_id} 不存在")
        return account

    # ------------------------------------------------------------------ 页面
    # 首页返回的是 HTML 模板，声明成 HTMLResponse 才能让 OpenAPI 文档与真实响应一致
    @app.get("/", response_class=HTMLResponse)
    def index(request: Request) -> Any:
        response = templates.TemplateResponse(
            request,
            "index.html",
            {
                "chain_id": settings.chain_id,
                "rpc_url": settings.rpc_url,
                "api_url": settings.api_url,
            },
        )
        return response

    # ------------------------------------------------------------------ 概览
    @app.get("/api/health")
    def health() -> dict[str, Any]:
        payload: dict[str, Any] = {
            "status": "ok",
            "chain_id": settings.chain_id,
            "rpc_url": settings.rpc_url,
            "db": str(settings.resolved_db_path()),
        }
        try:
            payload["rpc_chain_id"] = int(make_web3(settings).eth.chain_id)
        except Exception as exc:
            payload["status"] = "degraded"
            payload["rpc_error"] = str(exc)
        return payload

    # ------------------------------------------------------------------ 账号
    @app.get("/api/accounts", response_model=list[AccountOut])
    def list_accounts(
        group: str | None = None,
        enabled_only: bool = False,
        with_balance: bool = True,
    ) -> list[AccountOut]:
        accounts = database.list_accounts(group=group, enabled_only=enabled_only)
        # with_balance=false 用于离线/测试场景，避免逐个账号发起 RPC 查询
        balances = _balance_map(database, settings) if with_balance else {}
        for account in accounts:
            account.balance_wei = balances.get(account.address)
        return accounts

    @app.post("/api/accounts", response_model=AccountOut, status_code=201)
    def add_account(payload: AccountCreate) -> AccountOut:
        secret = _password(payload.password)
        if not secret:
            raise HTTPException(
                status_code=400,
                detail="缺少加密口令：请在请求体传入 password 或设置 FABX_SECRET",
            )
        try:
            return import_account(
                database,
                payload.private_key,
                secret,
                label=payload.label,
                group=payload.group,
            )
        except SecretError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except Exception as exc:
            raise HTTPException(status_code=400, detail=f"导入失败: {exc}") from exc

    @app.post("/api/accounts/import")
    def bulk_import(payload: ImportRequest) -> dict[str, Any]:
        secret = _password(payload.password)
        if not secret:
            raise HTTPException(
                status_code=400,
                detail="缺少加密口令：请在请求体传入 password 或设置 FABX_SECRET",
            )
        imported, errors = import_many(
            database,
            payload.content,
            secret,
            label_prefix=payload.label_prefix,
            group=payload.group,
        )
        database.log_event(
            f"批量导入账号：成功 {len(imported)}，失败 {len(errors)}"
        )
        return {
            "imported": [a.model_dump(mode="json") for a in imported],
            "errors": errors,
        }

    @app.patch("/api/accounts/{account_id}", response_model=AccountOut)
    def patch_account(account_id: int, payload: AccountPatch) -> AccountOut:
        require_account(account_id)
        if payload.enabled is not None:
            database.set_account_enabled(account_id, payload.enabled)
        if payload.label is not None or payload.group is not None:
            database.update_account_meta(
                account_id, label=payload.label, group=payload.group
            )
        return require_account(account_id)

    @app.delete("/api/accounts/{account_id}", status_code=204)
    def delete_account(account_id: int) -> Response:
        require_account(account_id)
        database.delete_account(account_id)
        # 204 语义上不能带响应体，用空 Response 而非 JSONResponse(None) 避免发 "null"
        return Response(status_code=204)

    # ------------------------------------------------------------------- CPU
    @app.get("/api/cpus", response_model=list[CpuOut])
    def list_cpus() -> list[CpuOut]:
        return database.list_cpus()

    @app.post("/api/cpus/sync", response_model=list[CpuOut])
    def sync() -> list[CpuOut]:
        try:
            synced = sync_cpus(database, settings)
        except Exception as exc:
            raise HTTPException(status_code=502, detail=f"同步失败: {exc}") from exc
        database.log_event(f"同步 CPU：{len(synced)} 条")
        return synced

    @app.get("/api/transistors/{address}")
    def transistor_state(address: str) -> dict[str, Any]:
        try:
            state = read_transistor_state(make_web3(settings), address)
        except Exception as exc:
            raise HTTPException(status_code=502, detail=f"读取链上状态失败: {exc}") from exc
        return {
            "address": state.address,
            "nand_id": state.nand_id,
            "latch_id": state.latch_id,
            "mint_price_wei": state.mint_price_wei,
            "mint_price_okb": format_okb(state.mint_price_wei),
            "supply_cap": state.supply_cap,
            "minted": state.minted,
            "remaining": {
                str(state.nand_id): state.remaining(state.nand_id),
                str(state.latch_id): state.remaining(state.latch_id),
            },
            "cpu_name": state.cpu_name,
        }

    # ------------------------------------------------------------------ 预览
    @app.post("/api/plan")
    def preview_plan(payload: JobIn) -> dict[str, Any]:
        engine = MintEngine(database, settings, password=_password(None))
        try:
            planned, notes = engine.build_plan(payload)
        except ChainReadError as exc:
            # 链上状态读不到属于上游故障（RPC 抖动/超时），用 502 与会话级 400 区分开
            raise HTTPException(status_code=502, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {
            "count": len(planned),
            "total_value_wei": sum(p.value_wei for p in planned),
            "notes": notes,
            "sample": [
                {
                    "account_id": p.account_id,
                    "address": p.address,
                    "token_id": p.token_id,
                    "amount": p.amount,
                    "value_wei": p.value_wei,
                    "value_okb": format_okb(p.value_wei),
                }
                for p in planned[:50]
            ],
        }

    # ------------------------------------------------------------------ 任务
    @app.post("/api/jobs", response_model=JobOut, status_code=201)
    def create_job(payload: JobIn) -> JobOut:
        engine = MintEngine(database, settings, password=_password(None))
        try:
            job_id, _notes = engine.create_job(payload)
        except ChainReadError as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        job = database.get_job(job_id)
        assert job is not None
        return job

    @app.get("/api/jobs", response_model=list[JobOut])
    def list_jobs(limit: int = 50) -> list[JobOut]:
        return database.list_jobs(limit=limit)

    @app.get("/api/jobs/{job_id}")
    def job_detail(job_id: int) -> dict[str, Any]:
        job = database.get_job(job_id)
        if job is None:
            raise HTTPException(status_code=404, detail=f"任务 {job_id} 不存在")
        tasks = database.list_tasks(job_id=job_id, limit=2000)
        return {
            "job": job.model_dump(mode="json"),
            "running": job_id in running_jobs,
            "tasks": [t.model_dump(mode="json") for t in tasks],
            "events": database.list_events(job_id=job_id, limit=100),
        }

    def _run_job_background(job_id: int, password: str | None) -> None:
        try:
            engine = MintEngine(database, settings, password=password)
            report = engine.run_job(job_id)
            database.log_event(
                f"任务 {job_id} 结束：成功 {report.confirmed}，失败 {report.failed}，"
                f"跳过 {report.skipped}",
                job_id=job_id,
            )
        except Exception as exc:
            log.exception("任务 %s 执行异常", job_id)
            database.log_event(f"任务 {job_id} 异常: {exc}", level="error", job_id=job_id)
        finally:
            with run_lock:
                running_jobs.discard(job_id)

    @app.post("/api/jobs/{job_id}/run", status_code=202)
    def run_job(job_id: int, payload: RunRequest | None = None) -> dict[str, Any]:
        job = database.get_job(job_id)
        if job is None:
            raise HTTPException(status_code=404, detail=f"任务 {job_id} 不存在")
        secret = _password(payload.password if payload else None)
        if not job.dry_run and not secret:
            raise HTTPException(
                status_code=400,
                detail="缺少解密口令：请在页面填写或设置 FABX_SECRET 环境变量",
            )
        with run_lock:
            if job_id in running_jobs:
                raise HTTPException(status_code=409, detail="该任务正在运行")
            # 执行引擎同一时刻只处理一个任务（避免同账号并发广播导致 nonce 冲突），
            # 这里提前拦截，返回清晰的 409 而不是让后台线程静默失败。
            if running_jobs:
                raise HTTPException(
                    status_code=409, detail="已有任务正在运行，请等待其结束后再试"
                )
            running_jobs.add(job_id)
        thread = threading.Thread(
            target=_run_job_background, args=(job_id, secret), daemon=True
        )
        thread.start()
        return {"job_id": job_id, "status": "started"}

    @app.post("/api/jobs/{job_id}/reset", response_model=JobOut)
    def reset_job(job_id: int) -> JobOut:
        job = database.get_job(job_id)
        if job is None:
            raise HTTPException(status_code=404, detail=f"任务 {job_id} 不存在")
        with run_lock:
            if job_id in running_jobs:
                raise HTTPException(status_code=409, detail="任务运行中，无法重置")
        database.reset_job_tasks(job_id)
        reset = database.get_job(job_id)
        assert reset is not None
        return reset

    # ------------------------------------------------------------------ 资金
    @app.post("/api/fund", response_model=FundResult)
    def fund(payload: FundRequest) -> FundResult:
        try:
            return distribute_funds(database, settings, payload)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except Exception as exc:
            raise HTTPException(status_code=502, detail=f"分发失败: {exc}") from exc

    # ------------------------------------------------------------------ 事件
    @app.get("/api/events")
    def list_events(job_id: int | None = None, limit: int = 100) -> list[dict[str, Any]]:
        return database.list_events(job_id=job_id, limit=limit)

    @app.get("/api/format")
    def format_wei(wei: int) -> dict[str, str]:
        return {"wei": str(wei), "okb": format_okb(wei)}

    return app


app = create_app()
