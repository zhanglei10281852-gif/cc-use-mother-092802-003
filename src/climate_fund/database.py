"""SQLite 数据访问层：建表脚本、连接工厂与事务工具。

只依赖标准库 sqlite3。金额统一用 Decimal 以字符串存入 TEXT 列；
所有写操作在 BEGIN IMMEDIATE 事务中进行，靠数据库写锁串行化
并发审批 / 并发支付，应用层在锁内复查状态。
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;

-- 用户与角色 --------------------------------------------------------------
CREATE TABLE IF NOT EXISTS users (
    id          TEXT PRIMARY KEY,
    name        TEXT NOT NULL,
    role        TEXT NOT NULL CHECK (role IN ('officer','reviewer','finance')),
    created_at  TEXT NOT NULL
);

-- 合作协议 ----------------------------------------------------------------
CREATE TABLE IF NOT EXISTS projects (
    id               TEXT PRIMARY KEY,
    title            TEXT NOT NULL,
    grantee          TEXT NOT NULL,
    currency         TEXT NOT NULL DEFAULT 'USD',
    total_amount     TEXT NOT NULL,           -- 协议总额（当前有效计划）
    original_amount  TEXT NOT NULL,           -- 原始协议额
    original_site    TEXT NOT NULL,
    current_site     TEXT NOT NULL,
    plan_revision    INTEGER NOT NULL DEFAULT 1,
    status           TEXT NOT NULL CHECK (
                         status IN ('active','suspended','completed','cancelled')),
    created_at       TEXT NOT NULL
);

-- 计划修订（灾情改址等）：明确原计划与新计划的资金责任 ---------------------
CREATE TABLE IF NOT EXISTS plan_amendments (
    id                  TEXT PRIMARY KEY,
    project_id          TEXT NOT NULL REFERENCES projects(id),
    revision            INTEGER NOT NULL,
    reason              TEXT NOT NULL,         -- 如 natural_disaster_relocation
    old_site            TEXT NOT NULL,
    new_site            TEXT NOT NULL,
    old_total_amount    TEXT NOT NULL,
    new_total_amount    TEXT NOT NULL,
    -- 改址前已承担/已付金额按原计划责任；新增/追加减额按新计划责任
    old_plan_responsibility  TEXT NOT NULL,    -- 截至改址已锁定的资金责任
    new_plan_responsibility  TEXT NOT NULL,    -- 新计划承担的剩余/调整额
    created_by          TEXT NOT NULL REFERENCES users(id),
    created_at          TEXT NOT NULL,
    UNIQUE (project_id, revision)
);

-- 里程碑（阶段）-----------------------------------------------------------
CREATE TABLE IF NOT EXISTS milestones (
    id              TEXT PRIMARY KEY,
    project_id      TEXT NOT NULL REFERENCES projects(id),
    sequence        INTEGER NOT NULL,
    title           TEXT NOT NULL,
    plan_revision   INTEGER NOT NULL,         -- 归属的计划版本
    planned_amount  TEXT NOT NULL,            -- 该阶段计划可申请上限
    state           TEXT NOT NULL CHECK (state IN (
                        'planned','evidence_submitted','rework_requested',
                        'verified','frozen')),
    frozen_reason   TEXT,
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL,
    UNIQUE (project_id, sequence, plan_revision)
);

-- 证据（阶段材料）按版本保存，驳回的版本永远保留、不可被补交覆盖 -----------
CREATE TABLE IF NOT EXISTS evidence_versions (
    id              TEXT PRIMARY KEY,
    milestone_id    TEXT NOT NULL REFERENCES milestones(id),
    version         INTEGER NOT NULL,
    doc_ref         TEXT NOT NULL,            -- 材料标识/哈希/URI
    note            TEXT,
    status          TEXT NOT NULL CHECK (status IN (
                        'submitted','superseded','rejected','accepted')),
    submitted_by    TEXT NOT NULL REFERENCES users(id),
    submitted_at    TEXT NOT NULL,
    decided_by      TEXT REFERENCES users(id),
    decided_at      TEXT,
    decision_note   TEXT,
    UNIQUE (milestone_id, version)
);

-- 现场核验结论（可能晚于材料到达）-----------------------------------------
CREATE TABLE IF NOT EXISTS verifications (
    id              TEXT PRIMARY KEY,
    milestone_id    TEXT NOT NULL REFERENCES milestones(id),
    evidence_id     TEXT NOT NULL REFERENCES evidence_versions(id),
    result          TEXT NOT NULL CHECK (result IN ('pass','fail','partial')),
    verified_amount TEXT,                     -- partial 时实际核定金额
    site_actual     TEXT,                     -- 实际实施地点
    inspector       TEXT NOT NULL REFERENCES users(id),
    note            TEXT,
    created_at      TEXT NOT NULL,
    UNIQUE (milestone_id, evidence_id)
);

-- 支付指令（资金申请），状态机：
-- requested -> approved -> disbursed -> reconciled(核销)
-- requested/approved -> frozen；frozen -> approved（恢复）；* -> rejected/cancelled
CREATE TABLE IF NOT EXISTS payment_orders (
    id              TEXT PRIMARY KEY,
    project_id      TEXT NOT NULL REFERENCES projects(id),
    milestone_id    TEXT NOT NULL REFERENCES milestones(id),
    verification_id TEXT REFERENCES verifications(id),  -- 可后补
    amount          TEXT NOT NULL,
    currency        TEXT NOT NULL,
    status          TEXT NOT NULL CHECK (status IN (
                        'requested','approved','rejected',
                        'frozen','disbursed','reconciled','cancelled')),
    requested_by    TEXT NOT NULL REFERENCES users(id),
    approved_by     TEXT REFERENCES users(id),
    frozen_reason   TEXT,
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL,
    CHECK (status <> 'approved' OR approved_by IS NOT NULL)
);

-- 银行/财务回执：receipt_no 全局唯一，重复回执必须幂等 ---------------------
CREATE TABLE IF NOT EXISTS payment_receipts (
    id                TEXT PRIMARY KEY,
    receipt_no        TEXT NOT NULL UNIQUE,
    payment_order_id  TEXT NOT NULL REFERENCES payment_orders(id),
    amount            TEXT NOT NULL,
    currency          TEXT NOT NULL,
    recorded_by      TEXT NOT NULL REFERENCES users(id),
    disbursed_at      TEXT NOT NULL,
    created_at        TEXT NOT NULL
);

-- 只增事件流：任何状态变化都追加一行，是审计与 API 时间线的依据 -------------
CREATE TABLE IF NOT EXISTS events (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    at          TEXT NOT NULL,
    actor_id    TEXT REFERENCES users(id),
    entity      TEXT NOT NULL,   -- project / milestone / evidence / verification / payment
    entity_id   TEXT NOT NULL,
    action      TEXT NOT NULL,
    payload     TEXT            -- JSON
);

-- 复式记账式分类账（单货币内，单位为分的整数思想；这里用 Decimal 字符串）：
-- 每个项目一行项目账户，每个支付指令一行在途账户。
-- 金额带符号，所有过账必须成对平衡。
CREATE TABLE IF NOT EXISTS ledger_entries (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    at              TEXT NOT NULL,
    event_id        INTEGER NOT NULL REFERENCES events(id),
    project_id      TEXT NOT NULL REFERENCES projects(id),
    payment_id      TEXT REFERENCES payment_orders(id),
    account         TEXT NOT NULL CHECK (account IN (
                        'appropriation',  -- 拨款来源（负数表示额度）
                        'budget',         -- 可申请预算
                        'requested',      -- 已申请在途
                        'payable',        -- 已批准待支付
                        'frozen',         -- 冻结
                        'disbursed',      -- 已支付
                        'reconciled')),   -- 已核销
    amount          TEXT NOT NULL,       -- 带符号
    memo            TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_milestones_project ON milestones(project_id);
CREATE INDEX IF NOT EXISTS idx_orders_project ON payment_orders(project_id);
CREATE INDEX IF NOT EXISTS idx_receipts_order ON payment_receipts(payment_order_id);
CREATE INDEX IF NOT EXISTS idx_events_entity ON events(entity, entity_id);
CREATE INDEX IF NOT EXISTS idx_ledger_project ON ledger_entries(project_id);
"""


def connect(db_path: str | Path = ":memory:") -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path), isolation_level=None, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)


@contextmanager
def transaction(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """BEGIN IMMEDIATE：一进入即取写锁，消除审批并发的读-写竞争窗口。"""
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
