"""SQLite 存储层：账号、CPU、任务与事件全部落库。

设计取舍：
- 用标准库 sqlite3，不引入 ORM，保持依赖面最小；
- 每次操作短连接 + WAL，配合多线程 mint 引擎；
- 账号私钥以密文 BLOB 存储，明文只在签名瞬间出现在内存。
"""

from __future__ import annotations

import json
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Iterator, Sequence

from .models import (
    AccountOut,
    CircuitIn,
    CircuitOut,
    CpuOut,
    JobOut,
    JobStatus,
    TaskOut,
    TaskStatus,
)

SCHEMA_VERSION = 2

_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS accounts (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    address     TEXT NOT NULL UNIQUE,
    label       TEXT,
    group_name  TEXT,
    cipher      BLOB NOT NULL,
    enabled     INTEGER NOT NULL DEFAULT 1,
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_accounts_group ON accounts(group_name);

CREATE TABLE IF NOT EXISTS cpus (
    circuits        TEXT PRIMARY KEY,
    transistors     TEXT NOT NULL UNIQUE,
    name            TEXT,
    creator         TEXT,
    supply_cap      INTEGER,
    minted          INTEGER,
    mint_price_wei  INTEGER,
    circuit_count   INTEGER,
    synced_at       TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS jobs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    name        TEXT NOT NULL,
    status      TEXT NOT NULL,
    dry_run     INTEGER NOT NULL DEFAULT 0,
    config_json TEXT NOT NULL,
    created_at  TEXT NOT NULL,
    started_at  TEXT,
    finished_at TEXT,
    error       TEXT
);

CREATE TABLE IF NOT EXISTS tasks (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id       INTEGER NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
    account_id   INTEGER NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
    transistors  TEXT NOT NULL,
    token_id     INTEGER NOT NULL,
    amount       INTEGER NOT NULL,
    value_wei    INTEGER NOT NULL DEFAULT 0,
    status       TEXT NOT NULL,
    attempts     INTEGER NOT NULL DEFAULT 0,
    nonce        INTEGER,
    tx_hash      TEXT,
    block_number INTEGER,
    gas_used     INTEGER,
    error        TEXT,
    created_at   TEXT NOT NULL,
    updated_at   TEXT NOT NULL,
    UNIQUE (job_id, account_id, transistors, token_id)
);
CREATE INDEX IF NOT EXISTS idx_tasks_job ON tasks(job_id);
CREATE INDEX IF NOT EXISTS idx_tasks_status ON tasks(status);

CREATE TABLE IF NOT EXISTS events (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id     INTEGER,
    task_id    INTEGER,
    level      TEXT NOT NULL,
    message    TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_events_job ON events(job_id);

-- tapeout 产出的电路 NFT（ERC721）。salt 必须随记录保存：commit 与 start
-- 必须使用同一个 salt，否则链上承诺校验失败。
CREATE TABLE IF NOT EXISTS circuits (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id   INTEGER NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
    circuits     TEXT NOT NULL,
    circuit_id   INTEGER NOT NULL,
    n_in         INTEGER NOT NULL,
    n_out        INTEGER NOT NULL,
    n_state      INTEGER NOT NULL,
    gate_count   INTEGER NOT NULL,
    label        TEXT,
    salt         TEXT,
    task_id      INTEGER,
    miner_key    TEXT,
    tx_hash      TEXT,
    block_number INTEGER,
    created_at   TEXT NOT NULL,
    updated_at   TEXT NOT NULL,
    UNIQUE (circuits, circuit_id)
);
CREATE INDEX IF NOT EXISTS idx_circuits_account ON circuits(account_id);
"""


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Database:
    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._write_lock = threading.Lock()
        with self._connect() as conn:
            conn.executescript(_SCHEMA)
            conn.execute(
                """
                INSERT INTO meta(key, value) VALUES ('schema_version', ?)
                ON CONFLICT(key) DO UPDATE SET value = excluded.value
                """,
                (str(SCHEMA_VERSION),),
            )

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.path, timeout=30.0, isolation_level=None)
        conn.row_factory = sqlite3.Row
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA foreign_keys=ON")
            conn.execute("PRAGMA busy_timeout=30000")
            yield conn
        finally:
            conn.close()

    # ---------------------------------------------------------------- accounts
    def add_account(
        self,
        address: str,
        cipher: bytes,
        *,
        label: str | None,
        group: str | None,
    ) -> int:
        """写入账号；地址已存在时更新密文与备注，返回账号 id。"""
        now = utcnow()
        with self._write_lock, self._connect() as conn:
            cur = conn.execute(
                """
                INSERT INTO accounts(address, label, group_name, cipher, enabled, created_at, updated_at)
                VALUES (?, ?, ?, ?, 1, ?, ?)
                ON CONFLICT(address) DO UPDATE SET
                    cipher      = excluded.cipher,
                    label       = COALESCE(excluded.label, accounts.label),
                    group_name  = COALESCE(excluded.group_name, accounts.group_name),
                    enabled     = 1,
                    updated_at  = excluded.updated_at
                """,
                (address.lower(), label, group, cipher, now, now),
            )
            if cur.lastrowid:
                return int(cur.lastrowid)
            row = conn.execute(
                "SELECT id FROM accounts WHERE address = ?", (address.lower(),)
            ).fetchone()
            return int(row["id"])

    def list_accounts(
        self,
        *,
        group: str | None = None,
        account_ids: Sequence[int] | None = None,
        enabled_only: bool = False,
    ) -> list[AccountOut]:
        sql = "SELECT * FROM accounts WHERE 1 = 1"
        params: list[Any] = []
        if group:
            sql += " AND group_name = ?"
            params.append(group)
        if account_ids is not None:
            # None 表示「不筛选」，空列表表示「明确不选任何账号」。
            # 二者必须区分：若把 [] 当成不筛选，上层显式传入空选择时会意外
            # 退化为对全部账号执行 mint/转账。
            if not account_ids:
                return []
            placeholders = ",".join("?" for _ in account_ids)
            sql += f" AND id IN ({placeholders})"
            params.extend(account_ids)
        if enabled_only:
            sql += " AND enabled = 1"
        sql += " ORDER BY id"
        with self._connect() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [self._row_to_account(r) for r in rows]

    def get_account(self, account_id: int) -> AccountOut | None:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM accounts WHERE id = ?", (account_id,)).fetchone()
        return self._row_to_account(row) if row else None

    def get_cipher(self, account_id: int) -> bytes | None:
        with self._connect() as conn:
            row = conn.execute("SELECT cipher FROM accounts WHERE id = ?", (account_id,)).fetchone()
        return bytes(row["cipher"]) if row else None

    def set_account_enabled(self, account_id: int, enabled: bool) -> None:
        with self._write_lock, self._connect() as conn:
            conn.execute(
                "UPDATE accounts SET enabled = ?, updated_at = ? WHERE id = ?",
                (1 if enabled else 0, utcnow(), account_id),
            )

    def update_account_meta(
        self, account_id: int, *, label: str | None, group: str | None
    ) -> None:
        """更新备注/分组；传入 None 表示保持原值（用 COALESCE 实现局部更新）。"""
        with self._write_lock, self._connect() as conn:
            conn.execute(
                """
                UPDATE accounts
                SET label = COALESCE(?, label),
                    group_name = COALESCE(?, group_name),
                    updated_at = ?
                WHERE id = ?
                """,
                (label, group, utcnow(), account_id),
            )

    def delete_account(self, account_id: int) -> None:
        with self._write_lock, self._connect() as conn:
            conn.execute("DELETE FROM accounts WHERE id = ?", (account_id,))

    @staticmethod
    def _row_to_account(row: sqlite3.Row) -> AccountOut:
        return AccountOut(
            id=int(row["id"]),
            address=row["address"],
            label=row["label"],
            group=row["group_name"],
            enabled=bool(row["enabled"]),
            created_at=datetime.fromisoformat(row["created_at"]),
        )

    # -------------------------------------------------------------------- cpus
    def upsert_cpu(self, cpu: CpuOut) -> None:
        now = cpu.synced_at or datetime.now(timezone.utc)
        with self._write_lock, self._connect() as conn:
            conn.execute(
                """
                INSERT INTO cpus(circuits, transistors, name, creator, supply_cap,
                                 minted, mint_price_wei, circuit_count, synced_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(circuits) DO UPDATE SET
                    transistors    = excluded.transistors,
                    name           = excluded.name,
                    creator        = excluded.creator,
                    supply_cap     = excluded.supply_cap,
                    minted         = excluded.minted,
                    mint_price_wei = excluded.mint_price_wei,
                    circuit_count  = excluded.circuit_count,
                    synced_at      = excluded.synced_at
                """,
                (
                    cpu.circuits.lower(),
                    cpu.transistors.lower(),
                    cpu.name,
                    cpu.creator,
                    cpu.supply_cap,
                    cpu.minted,
                    cpu.mint_price_wei,
                    cpu.circuit_count,
                    now.isoformat(timespec="seconds"),
                ),
            )

    def list_cpus(self) -> list[CpuOut]:
        with self._connect() as conn:
            # cpus 表以 circuits 为主键，没有自增 id，按名称/地址稳定排序即可
            rows = conn.execute("SELECT * FROM cpus ORDER BY name IS NULL, name, circuits").fetchall()
        return [
            CpuOut(
                circuits=r["circuits"],
                transistors=r["transistors"],
                name=r["name"],
                creator=r["creator"],
                supply_cap=r["supply_cap"],
                minted=r["minted"],
                mint_price_wei=r["mint_price_wei"],
                circuit_count=r["circuit_count"],
                synced_at=datetime.fromisoformat(r["synced_at"]),
            )
            for r in rows
        ]

    # ---------------------------------------------------------------- circuits
    def add_circuit(self, circuit: CircuitIn) -> int:
        """写入一条 tapeout 产物；(circuits, circuit_id) 重复时更新可变字段。

        同一电路不可能被重复 tapeout（链上 id 单调递增），重复多半来自「重跑同一批
        任务时补写记录」。此时保留已有 salt / task_id / miner_key，只覆盖
        n_in/n_out 这类可重算字段，避免把已经用于 start 的 salt 抹掉。
        """
        now = utcnow()
        with self._write_lock, self._connect() as conn:
            cur = conn.execute(
                """
                INSERT INTO circuits(account_id, circuits, circuit_id, n_in, n_out,
                                     n_state, gate_count, label, salt, task_id,
                                     miner_key, tx_hash, block_number,
                                     created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(circuits, circuit_id) DO UPDATE SET
                    account_id   = excluded.account_id,
                    n_in         = excluded.n_in,
                    n_out        = excluded.n_out,
                    n_state      = excluded.n_state,
                    gate_count   = excluded.gate_count,
                    label        = COALESCE(excluded.label, circuits.label),
                    salt         = COALESCE(excluded.salt, circuits.salt),
                    task_id      = COALESCE(excluded.task_id, circuits.task_id),
                    miner_key    = COALESCE(excluded.miner_key, circuits.miner_key),
                    tx_hash      = COALESCE(excluded.tx_hash, circuits.tx_hash),
                    block_number = COALESCE(excluded.block_number, circuits.block_number),
                    updated_at   = excluded.updated_at
                """,
                (
                    circuit.account_id,
                    circuit.circuits.lower(),
                    circuit.circuit_id,
                    circuit.n_in,
                    circuit.n_out,
                    circuit.n_state,
                    circuit.gate_count,
                    circuit.label,
                    circuit.salt,
                    circuit.task_id,
                    circuit.miner_key,
                    circuit.tx_hash,
                    circuit.block_number,
                    now,
                    now,
                ),
            )
            if cur.lastrowid:
                return int(cur.lastrowid)
            row = conn.execute(
                "SELECT id FROM circuits WHERE circuits = ? AND circuit_id = ?",
                (circuit.circuits.lower(), circuit.circuit_id),
            ).fetchone()
            return int(row["id"])

    def set_circuit_miner(
        self,
        *,
        circuits: str,
        circuit_id: int,
        task_id: int,
        miner_key: str,
    ) -> None:
        """start 成功后回填该电路对应的任务号与矿机 key，便于后续 claim/stop。"""
        with self._write_lock, self._connect() as conn:
            conn.execute(
                """
                UPDATE circuits
                SET task_id = ?, miner_key = ?, updated_at = ?
                WHERE circuits = ? AND circuit_id = ?
                """,
                (task_id, miner_key, utcnow(), circuits.lower(), circuit_id),
            )

    def list_circuits(
        self,
        *,
        account_id: int | None = None,
        circuits: str | None = None,
        limit: int = 500,
    ) -> list[CircuitOut]:
        sql = """
            SELECT c.*, a.address AS address
            FROM circuits c LEFT JOIN accounts a ON a.id = c.account_id
            WHERE 1 = 1
        """
        params: list[Any] = []
        if account_id is not None:
            sql += " AND c.account_id = ?"
            params.append(account_id)
        if circuits:
            sql += " AND c.circuits = ?"
            params.append(circuits.lower())
        sql += " ORDER BY c.id DESC LIMIT ?"
        params.append(limit)
        with self._connect() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [self._row_to_circuit(r) for r in rows]

    def get_circuit(self, circuits: str, circuit_id: int) -> CircuitOut | None:
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT c.*, a.address AS address
                FROM circuits c LEFT JOIN accounts a ON a.id = c.account_id
                WHERE c.circuits = ? AND c.circuit_id = ?
                """,
                (circuits.lower(), circuit_id),
            ).fetchone()
        return self._row_to_circuit(row) if row else None

    @staticmethod
    def _row_to_circuit(row: sqlite3.Row) -> CircuitOut:
        return CircuitOut(
            id=int(row["id"]),
            account_id=int(row["account_id"]),
            circuits=row["circuits"],
            circuit_id=int(row["circuit_id"]),
            n_in=int(row["n_in"]),
            n_out=int(row["n_out"]),
            n_state=int(row["n_state"]),
            gate_count=int(row["gate_count"]),
            label=row["label"],
            salt=row["salt"],
            tx_hash=row["tx_hash"],
            block_number=row["block_number"],
            address=row["address"] if "address" in row.keys() else None,
            created_at=datetime.fromisoformat(row["created_at"]),
            task_id=row["task_id"],
            miner_key=row["miner_key"],
        )

    # -------------------------------------------------------------------- jobs
    def create_job(self, name: str, config: dict[str, Any], *, dry_run: bool) -> int:
        with self._write_lock, self._connect() as conn:
            cur = conn.execute(
                """
                INSERT INTO jobs(name, status, dry_run, config_json, created_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    name,
                    JobStatus.DRAFT.value,
                    1 if dry_run else 0,
                    json.dumps(config, ensure_ascii=False),
                    utcnow(),
                ),
            )
            return int(cur.lastrowid)

    def add_tasks(self, job_id: int, rows: Iterable[dict[str, Any]]) -> int:
        """批量写入任务，返回该 job 当前的任务总数。

        用 INSERT OR IGNORE 配合 (job_id, account_id, transistors, token_id) 唯一约束做幂等：
        重复任务被跳过而非报错。返回值是「写入后该 job 的任务总数」，因此若同一 job 反复
        补写，返回的是累计总数（含此前已存在的任务），与本次实际新增条数可能不等。
        """
        now = utcnow()
        payload = [
            (
                job_id,
                r["account_id"],
                r["transistors"].lower(),
                r["token_id"],
                r["amount"],
                r["value_wei"],
                TaskStatus.PENDING.value,
                now,
                now,
            )
            for r in rows
        ]
        if not payload:
            return 0
        with self._write_lock, self._connect() as conn:
            conn.executemany(
                """
                INSERT OR IGNORE INTO tasks(job_id, account_id, transistors, token_id,
                                            amount, value_wei, status, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                payload,
            )
            row = conn.execute(
                "SELECT COUNT(*) AS n FROM tasks WHERE job_id = ?", (job_id,)
            ).fetchone()
        return int(row["n"])

    def set_job_status(self, job_id: int, status: JobStatus, *, error: str | None = None) -> None:
        now = utcnow()
        sets = ["status = ?"]
        params: list[Any] = [status.value]
        if status == JobStatus.RUNNING:
            sets.append("started_at = COALESCE(started_at, ?)")
            params.append(now)
        if status in (JobStatus.DONE, JobStatus.FAILED):
            sets.append("finished_at = ?")
            params.append(now)
        if error is not None:
            sets.append("error = ?")
            params.append(error)
        params.append(job_id)
        with self._write_lock, self._connect() as conn:
            conn.execute(f"UPDATE jobs SET {', '.join(sets)} WHERE id = ?", params)

    def get_job(self, job_id: int) -> JobOut | None:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
            if not row:
                return None
            stats = conn.execute(
                """
                SELECT
                    COUNT(*) AS total,
                    SUM(CASE WHEN status = 'confirmed' THEN 1 ELSE 0 END) AS confirmed,
                    SUM(CASE WHEN status = 'failed' THEN 1 ELSE 0 END) AS failed,
                    SUM(CASE WHEN status = 'skipped' THEN 1 ELSE 0 END) AS skipped,
                    SUM(CASE WHEN status IN ('pending', 'sent') THEN 1 ELSE 0 END) AS pending
                FROM tasks WHERE job_id = ?
                """,
                (job_id,),
            ).fetchone()
        return self._row_to_job(row, stats)

    def list_jobs(self, limit: int = 50) -> list[JobOut]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM jobs ORDER BY id DESC LIMIT ?", (limit,)
            ).fetchall()
            out: list[JobOut] = []
            for row in rows:
                stats = conn.execute(
                    """
                    SELECT
                        COUNT(*) AS total,
                        SUM(CASE WHEN status = 'confirmed' THEN 1 ELSE 0 END) AS confirmed,
                        SUM(CASE WHEN status = 'failed' THEN 1 ELSE 0 END) AS failed,
                        SUM(CASE WHEN status = 'skipped' THEN 1 ELSE 0 END) AS skipped,
                        SUM(CASE WHEN status IN ('pending', 'sent') THEN 1 ELSE 0 END) AS pending
                    FROM tasks WHERE job_id = ?
                    """,
                    (row["id"],),
                ).fetchone()
                out.append(self._row_to_job(row, stats))
        return out

    @staticmethod
    def _row_to_job(row: sqlite3.Row, stats: sqlite3.Row) -> JobOut:
        total = int(stats["total"] or 0)
        confirmed = int(stats["confirmed"] or 0)
        failed = int(stats["failed"] or 0)
        skipped = int(stats["skipped"] or 0)
        pending = int(stats["pending"] or 0)
        # 执行参数（如 stop_on_first_failure）随 config_json 一起回读，
        # 这样 get_job 才能还原出可执行的任务定义。
        config: dict[str, Any] = {}
        if "config_json" in row.keys() and row["config_json"]:
            try:
                config = json.loads(row["config_json"])
            except (TypeError, ValueError):
                config = {}
        return JobOut(
            id=int(row["id"]),
            name=row["name"],
            status=JobStatus(row["status"]),
            dry_run=bool(row["dry_run"]),
            stop_on_first_failure=bool(config.get("stop_on_first_failure", False)),
            created_at=datetime.fromisoformat(row["created_at"]),
            started_at=datetime.fromisoformat(row["started_at"]) if row["started_at"] else None,
            finished_at=datetime.fromisoformat(row["finished_at"]) if row["finished_at"] else None,
            total=total,
            confirmed=confirmed,
            failed=failed,
            pending=pending,
            skipped=skipped,
            error=row["error"],
            # 进度按「已收尾的任务」统计：confirmed/failed/skipped 都算走完，
            # pending/sent 仍待在途。分母用 total，避免出现 status=done 但进度 0。
            progress=round((confirmed + failed + skipped) / total, 4) if total else 0.0,
        )

    # ------------------------------------------------------------------- tasks
    def list_tasks(
        self,
        *,
        job_id: int | None = None,
        statuses: Sequence[TaskStatus] | None = None,
        limit: int = 500,
    ) -> list[TaskOut]:
        sql = """
            SELECT t.*, a.address AS address
            FROM tasks t LEFT JOIN accounts a ON a.id = t.account_id
            WHERE 1 = 1
        """
        params: list[Any] = []
        if job_id is not None:
            sql += " AND t.job_id = ?"
            params.append(job_id)
        if statuses:
            placeholders = ",".join("?" for _ in statuses)
            sql += f" AND t.status IN ({placeholders})"
            params.extend(s.value for s in statuses)
        sql += " ORDER BY t.id LIMIT ?"
        params.append(limit)
        with self._connect() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [self._row_to_task(r) for r in rows]

    def get_task(self, task_id: int) -> TaskOut | None:
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT t.*, a.address AS address
                FROM tasks t LEFT JOIN accounts a ON a.id = t.account_id
                WHERE t.id = ?
                """,
                (task_id,),
            ).fetchone()
        return self._row_to_task(row) if row else None

    def mark_task_sent(
        self, task_id: int, *, nonce: int, tx_hash: str, error: str | None = None
    ) -> None:
        with self._write_lock, self._connect() as conn:
            conn.execute(
                """
                UPDATE tasks
                SET status = ?, attempts = attempts + 1, nonce = ?, tx_hash = ?,
                    error = ?, updated_at = ?
                WHERE id = ?
                """,
                (TaskStatus.SENT.value, nonce, tx_hash, error, utcnow(), task_id),
            )

    def mark_task_confirmed(
        self, task_id: int, *, block_number: int, gas_used: int, tx_hash: str
    ) -> None:
        with self._write_lock, self._connect() as conn:
            conn.execute(
                """
                UPDATE tasks
                SET status = ?, block_number = ?, gas_used = ?, tx_hash = ?,
                    error = NULL, updated_at = ?
                WHERE id = ?
                """,
                (TaskStatus.CONFIRMED.value, block_number, gas_used, tx_hash, utcnow(), task_id),
            )

    def mark_task_unconfirmed(self, task_id: int, error: str) -> None:
        """记录「已广播但尚未确认」的提示，同时保留 SENT 状态与 tx_hash。

        这里刻意不把任务改回 pending：交易一旦广播就可能已上链，
        重置状态会让下次重跑重复广播、重复扣款。只允许在仍处于 SENT
        时写入提示，避免覆盖并发发生的确认结果。
        """
        with self._write_lock, self._connect() as conn:
            conn.execute(
                """
                UPDATE tasks
                SET status = ?, error = ?, updated_at = ?
                WHERE id = ? AND status = ?
                """,
                (TaskStatus.SENT.value, error[:2000], utcnow(), task_id, TaskStatus.SENT.value),
            )

    def mark_task_failed(
        self,
        task_id: int,
        error: str,
        *,
        status: TaskStatus = TaskStatus.FAILED,
        tx_hash: str | None = None,
    ) -> None:
        with self._write_lock, self._connect() as conn:
            conn.execute(
                """
                UPDATE tasks
                SET status = ?, error = ?, tx_hash = COALESCE(?, tx_hash), updated_at = ?
                WHERE id = ?
                """,
                (status.value, error[:2000], tx_hash, utcnow(), task_id),
            )

    def reset_job_tasks(self, job_id: int) -> int:
        """把「未广播的终态」任务重置为 pending，用于充值/售罄恢复后重跑。

        重置 failed 与 skipped：skipped 覆盖「余额不足」这类可恢复的跳过，
        充值后必须能再次尝试，否则账号会被永久跳过、钱到了却铸不出来。
        但不重置 sent / confirmed：sent 可能已上链，必须保留原 tx_hash 交给
        执行引擎对账，否则重新广播会造成同一笔铸造重复扣款。
        """
        with self._write_lock, self._connect() as conn:
            cur = conn.execute(
                """
                UPDATE tasks
                SET status = ?, error = NULL, updated_at = ?
                WHERE job_id = ? AND status IN ('failed', 'skipped')
                """,
                (TaskStatus.PENDING.value, utcnow(), job_id),
            )
            conn.execute(
                """
                UPDATE jobs SET status = ?, finished_at = NULL, error = NULL WHERE id = ?
                """,
                (JobStatus.DRAFT.value, job_id),
            )
            return int(cur.rowcount or 0)

    @staticmethod
    def _row_to_task(row: sqlite3.Row) -> TaskOut:
        return TaskOut(
            id=int(row["id"]),
            job_id=int(row["job_id"]),
            account_id=int(row["account_id"]),
            address=row["address"] if "address" in row.keys() else None,
            transistors=row["transistors"],
            token_id=int(row["token_id"]),
            amount=int(row["amount"]),
            value_wei=int(row["value_wei"]),
            status=TaskStatus(row["status"]),
            attempts=int(row["attempts"]),
            nonce=row["nonce"],
            tx_hash=row["tx_hash"],
            block_number=row["block_number"],
            gas_used=row["gas_used"],
            error=row["error"],
            updated_at=datetime.fromisoformat(row["updated_at"]),
        )

    # ------------------------------------------------------------------ events
    def log_event(
        self, message: str, *, level: str = "info", job_id: int | None = None,
        task_id: int | None = None,
    ) -> None:
        with self._write_lock, self._connect() as conn:
            conn.execute(
                "INSERT INTO events(job_id, task_id, level, message, created_at) VALUES (?, ?, ?, ?, ?)",
                (job_id, task_id, level, message[:2000], utcnow()),
            )

    def list_events(self, *, job_id: int | None = None, limit: int = 200) -> list[dict[str, Any]]:
        sql = "SELECT * FROM events"
        params: list[Any] = []
        if job_id is not None:
            sql += " WHERE job_id = ?"
            params.append(job_id)
        sql += " ORDER BY id DESC LIMIT ?"
        params.append(limit)
        with self._connect() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [dict(r) for r in rows]
