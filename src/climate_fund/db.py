"""本地 SQLite 数据库：模式定义与事务助手。

设计要点：
- 金额一律为整数（最小货币单位，如“分”），避免浮点误差；
- 所有写操作走 BEGIN IMMEDIATE 短事务，配合条件 UPDATE 的受影响行数
  实现乐观并发控制；
- 付款回执以 (instruction_id, external_ref) 唯一约束保证幂等；
- 账本（ledger_entries）只记录资金池的真实变动：拨款、收回、支付、核销，
  冻结/解冻等结构性事件进入审计流（audit_events），不挪动资金。
"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from typing import Iterator

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id    TEXT PRIMARY KEY,
    name  TEXT NOT NULL,
    role  TEXT NOT NULL CHECK (role IN ('OFFICER', 'REVIEWER', 'FINANCE', 'ADMIN'))
);

CREATE TABLE IF NOT EXISTS projects (
    id               TEXT PRIMARY KEY,
    code             TEXT NOT NULL UNIQUE,
    name             TEXT NOT NULL,
    partner          TEXT NOT NULL,          -- 受援方
    location         TEXT NOT NULL,          -- 当前实施地点
    total_amount     INTEGER NOT NULL CHECK (total_amount > 0),
    currency         TEXT NOT NULL DEFAULT 'CNY',
    status           TEXT NOT NULL CHECK (status IN ('DRAFT', 'ACTIVE', 'SUSPENDED', 'CLOSED')),
    current_revision INTEGER NOT NULL DEFAULT 1,
    created_by       TEXT NOT NULL,
    created_at       TEXT NOT NULL,
    updated_at       TEXT NOT NULL
);

-- 计划版本：初始计划、灾情改址后的新计划……各自的资金责任留痕
CREATE TABLE IF NOT EXISTS plan_revisions (
    id                    TEXT PRIMARY KEY,
    project_id            TEXT NOT NULL REFERENCES projects (id),
    revision_no           INTEGER NOT NULL,
    reason                TEXT NOT NULL,      -- INITIAL / DISASTER_RELOCATION / ...
    location              TEXT NOT NULL,
    status                TEXT NOT NULL CHECK (status IN ('ACTIVE', 'SUSPENDED', 'SUPERSEDED', 'CLOSED')),
    carried_from_previous INTEGER NOT NULL DEFAULT 0,  -- 自上一计划承接的资金
    recovered_to_donor    INTEGER NOT NULL DEFAULT 0,  -- 本次调整收回出资方的资金
    new_funds             INTEGER NOT NULL DEFAULT 0,  -- 本次调整新增的出资
    note                  TEXT,
    created_by            TEXT NOT NULL,
    created_at            TEXT NOT NULL,
    UNIQUE (project_id, revision_no)
);

CREATE TABLE IF NOT EXISTS milestones (
    id          TEXT PRIMARY KEY,
    project_id  TEXT NOT NULL REFERENCES projects (id),
    revision_id TEXT NOT NULL REFERENCES plan_revisions (id),
    seq         INTEGER NOT NULL,
    title       TEXT NOT NULL,
    amount      INTEGER NOT NULL CHECK (amount > 0),
    status      TEXT NOT NULL CHECK (status IN (
        'PLANNED', 'EVIDENCE_SUBMITTED', 'EVIDENCE_REJECTED', 'EVIDENCE_APPROVED',
        'VERIFIED', 'FROZEN', 'RECONCILED', 'CANCELLED')),
    created_at  TEXT NOT NULL,
    UNIQUE (revision_id, seq)
);

-- 阶段材料：按版本追加，被驳回的版本永不覆盖
CREATE TABLE IF NOT EXISTS evidence_submissions (
    id             TEXT PRIMARY KEY,
    milestone_id   TEXT NOT NULL REFERENCES milestones (id),
    version        INTEGER NOT NULL,
    content_uri    TEXT NOT NULL,
    note           TEXT,
    status         TEXT NOT NULL CHECK (status IN ('SUBMITTED', 'APPROVED', 'REJECTED', 'SUPERSEDED')),
    submitted_by   TEXT NOT NULL,
    submitted_at   TEXT NOT NULL,
    reviewed_by    TEXT,
    reviewed_at    TEXT,
    review_comment TEXT,
    UNIQUE (milestone_id, version)
);

-- 核验结论：现场核验(FIELD)与财务凭证(FINANCIAL)分别到达、分别留痕
CREATE TABLE IF NOT EXISTS verifications (
    id           TEXT PRIMARY KEY,
    milestone_id TEXT NOT NULL REFERENCES milestones (id),
    evidence_id  TEXT NOT NULL REFERENCES evidence_submissions (id),
    kind         TEXT NOT NULL CHECK (kind IN ('FIELD', 'FINANCIAL')),
    conclusion   TEXT NOT NULL CHECK (conclusion IN ('PASS', 'FAIL')),
    detail       TEXT,
    verified_by  TEXT NOT NULL,
    verified_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS payment_instructions (
    id             TEXT PRIMARY KEY,
    project_id     TEXT NOT NULL REFERENCES projects (id),
    milestone_id   TEXT NOT NULL REFERENCES milestones (id),
    revision_id    TEXT NOT NULL REFERENCES plan_revisions (id),
    amount         INTEGER NOT NULL CHECK (amount > 0),
    paid_amount    INTEGER NOT NULL DEFAULT 0 CHECK (paid_amount >= 0),
    status         TEXT NOT NULL CHECK (status IN (
        'PENDING', 'APPROVED', 'PARTIALLY_PAID', 'PAID', 'FROZEN', 'CANCELLED', 'RECONCILED')),
    created_by     TEXT NOT NULL,   -- 经办人
    created_at     TEXT NOT NULL,
    approved_by    TEXT,            -- 审核人（不得与经办人相同）
    approved_at    TEXT,
    written_off_by TEXT,
    written_off_at TEXT
);

-- 付款回执：同一指令下外部回执号唯一，重复报送幂等
CREATE TABLE IF NOT EXISTS payment_receipts (
    id             TEXT PRIMARY KEY,
    instruction_id TEXT NOT NULL REFERENCES payment_instructions (id),
    external_ref   TEXT NOT NULL,
    amount         INTEGER NOT NULL CHECK (amount > 0),
    received_by    TEXT NOT NULL,
    received_at    TEXT NOT NULL,
    UNIQUE (instruction_id, external_ref)
);

-- 资金池流水：ALLOCATION(+) / RECOVERY(-) / PAYMENT(-) / WRITEOFF(核销台账)
CREATE TABLE IF NOT EXISTS ledger_entries (
    id             TEXT PRIMARY KEY,
    project_id     TEXT NOT NULL REFERENCES projects (id),
    revision_id    TEXT,
    milestone_id   TEXT,
    instruction_id TEXT,
    receipt_id     TEXT,
    kind           TEXT NOT NULL CHECK (kind IN ('ALLOCATION', 'RECOVERY', 'PAYMENT', 'WRITEOFF')),
    amount         INTEGER NOT NULL CHECK (amount > 0),
    actor          TEXT NOT NULL,
    note           TEXT,
    created_at     TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_ledger_project ON ledger_entries (project_id, kind);

-- 审计流：每一次状态变化留痕（谁、以什么角色、何时、做了什么）
CREATE TABLE IF NOT EXISTS audit_events (
    id          TEXT PRIMARY KEY,
    project_id  TEXT,
    entity_type TEXT NOT NULL,
    entity_id   TEXT NOT NULL,
    action      TEXT NOT NULL,
    actor       TEXT NOT NULL,
    role        TEXT NOT NULL,
    detail      TEXT,
    created_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_audit_project ON audit_events (project_id);
CREATE INDEX IF NOT EXISTS idx_audit_entity ON audit_events (entity_type, entity_id);
"""


class Database:
    """SQLite 本地库，每次操作使用独立短连接，线程安全。"""

    def __init__(self, path: str):
        self.path = path
        conn = sqlite3.connect(path)
        try:
            conn.execute("PRAGMA journal_mode = WAL")
            conn.executescript(SCHEMA)
            conn.commit()
        finally:
            conn.close()

    def connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA busy_timeout = 30000")
        return conn

    @contextmanager
    def tx(self) -> Iterator[sqlite3.Connection]:
        """写事务：BEGIN IMMEDIATE，提交或回滚后关闭连接。"""
        conn = self.connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            yield conn
            conn.execute("COMMIT")
        except BaseException:
            conn.execute("ROLLBACK")
            raise
        finally:
            conn.close()

    @contextmanager
    def read(self) -> Iterator[sqlite3.Connection]:
        """只读连接。"""
        conn = self.connect()
        try:
            yield conn
        finally:
            conn.close()
