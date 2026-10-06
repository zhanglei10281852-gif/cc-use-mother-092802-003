"""复式记账与三方对账。

账户（均为资金占用类，除 appropriation 为来源类）：
    appropriation 拨款来源（余额为负）
    budget        可申请预算
    requested     已申请待审批
    payable       已批准待支付
    frozen        冻结
    disbursed     已支付
    reconciled    已核销

任何资金动作都是成对（或成组）的同额借贷，event 是记账凭证来源，
ledger_entries 是账本；支付指令/回执是业务单据。三方必须对平。
"""

from __future__ import annotations

import json
import sqlite3
from decimal import Decimal

D = Decimal
ZERO = D("0")
CENT = D("0.01")

BUSINESS_ACCOUNTS = ("budget", "requested", "payable", "frozen", "disbursed", "reconciled")
ALL_ACCOUNTS = ("appropriation",) + BUSINESS_ACCOUNTS

# 支付指令状态 -> 对应分类账账户
ORDER_STATUS_ACCOUNT = {
    "requested": "requested",
    "approved": "payable",
    "frozen": "frozen",
    "disbursed": "disbursed",
    "reconciled": "reconciled",
}


def _money(value: str | D) -> D:
    amount = value if isinstance(value, D) else D(str(value))
    # SQLite 对 TEXT 列求和会转成数值（可能带浮点尾差），统一量化到分
    return amount.quantize(CENT)


def post(
    conn: sqlite3.Connection,
    *,
    at: str,
    actor_id: str | None,
    project_id: str,
    entity: str,
    entity_id: str,
    action: str,
    legs: list[tuple[str, D]],
    memo: str,
    extra_payload: dict | None = None,
    payment_id: str | None = None,
) -> int:
    """在当前事务内：追加一条事件（记账凭证），并写入成对分录。

    legs 之和必须为 0（借贷平衡），否则抛 ValueError。
    返回事件 id。
    """
    total = sum((amount for _, amount in legs), ZERO)
    if total != ZERO:
        raise ValueError(f"unbalanced ledger legs for {action}: {total}")

    payload = {"memo": memo, "legs": [{"account": a, "amount": str(m)} for a, m in legs]}
    if extra_payload:
        payload.update(extra_payload)
    cur = conn.execute(
        "INSERT INTO events (at, actor_id, entity, entity_id, action, payload) "
        "VALUES (?,?,?,?,?,?)",
        (at, actor_id, entity, entity_id, action, json.dumps(payload, ensure_ascii=False)),
    )
    event_id = cur.lastrowid
    for account, amount in legs:
        conn.execute(
            "INSERT INTO ledger_entries (at, event_id, project_id, payment_id, account, amount, memo) "
            "VALUES (?,?,?,?,?,?,?)",
            (at, event_id, project_id, payment_id, account, str(amount), memo),
        )
    return event_id


def log_event(
    conn: sqlite3.Connection,
    *,
    at: str,
    actor_id: str | None,
    entity: str,
    entity_id: str,
    action: str,
    payload: dict | None = None,
) -> int:
    """只追加事件、不记账（状态变更、决策、幂等去重等无资金动作的留痕）。"""
    cur = conn.execute(
        "INSERT INTO events (at, actor_id, entity, entity_id, action, payload) "
        "VALUES (?,?,?,?,?,?)",
        (at, actor_id, entity, entity_id, action,
         json.dumps(payload or {}, ensure_ascii=False)),
    )
    return cur.lastrowid


def ledger_totals(conn: sqlite3.Connection, project_id: str) -> dict[str, D]:
    totals = {a: ZERO for a in ALL_ACCOUNTS}
    for row in conn.execute(
        "SELECT account, SUM(amount) AS s FROM ledger_entries WHERE project_id=? GROUP BY account",
        (project_id,),
    ):
        totals[row["account"]] = _money(row["s"])
    return totals


def _replay_events(conn: sqlite3.Connection, project_id: str) -> dict[str, D]:
    """第三轨：只看事件流 payload 里的 legs 重算各账户余额。

    事件流是只增凭证链，账本可以从它完全重建；两边不一致说明有人绕过了记账。
    """
    totals = {a: ZERO for a in ALL_ACCOUNTS}
    for row in conn.execute(
        "SELECT e.payload FROM events e "
        "JOIN ledger_entries l ON l.event_id = e.id "
        "WHERE l.project_id=? GROUP BY e.id ORDER BY e.id",
        (project_id,),
    ):
        for leg in json.loads(row["payload"]).get("legs", []):
            totals[leg["account"]] += D(leg["amount"])
    return totals


def reconciliation_report(conn: sqlite3.Connection, project_id: str) -> dict:
    """三方对账：分类账 vs 业务单据 vs 事件流重算。任何一项不平即 balanced=False。"""
    project = conn.execute("SELECT * FROM projects WHERE id=?", (project_id,)).fetchone()
    if project is None:
        return {"project_id": project_id, "balanced": False, "errors": ["project_not_found"]}

    errors: list[str] = []

    # 第一轨：分类账
    ledger = ledger_totals(conn, project_id)

    # 1) 借贷守恒：含来源账户在内总和必须为 0
    if sum(ledger.values(), ZERO) != ZERO:
        errors.append(f"ledger_not_balanced: {sum(ledger.values(), ZERO)}")

    # 2) 业务各账户之和必须等于现行计划总额
    business_sum = sum((ledger[a] for a in BUSINESS_ACCOUNTS), ZERO)
    total = _money(project["total_amount"])
    if business_sum != total:
        errors.append(f"business_sum {business_sum} != project_total {total}")

    # 第二轨：业务单据（支付指令按状态、回执）
    doc_by_status: dict[str, D] = {}
    for row in conn.execute(
        "SELECT status, SUM(amount) AS s FROM payment_orders "
        "WHERE project_id=? AND status NOT IN ('rejected','cancelled') GROUP BY status",
        (project_id,),
    ):
        doc_by_status[row["status"]] = _money(row["s"])
    for status, account in ORDER_STATUS_ACCOUNT.items():
        if ledger[account] != doc_by_status.get(status, ZERO):
            errors.append(
                f"ledger[{account}] {ledger[account]} != orders[{status}] {doc_by_status.get(status, ZERO)}"
            )

    receipts_total = conn.execute(
        "SELECT COALESCE(SUM(r.amount),'0') AS s FROM payment_receipts r "
        "JOIN payment_orders o ON o.id = r.payment_order_id WHERE o.project_id=?",
        (project_id,),
    ).fetchone()["s"]
    paid_doc = _money(receipts_total)
    paid_ledger = ledger["disbursed"] + ledger["reconciled"]
    if paid_doc != paid_ledger:
        errors.append(f"receipts {paid_doc} != disbursed+reconciled {paid_ledger}")

    # 计划总额 = 原始额 + 各次修订增量
    original = _money(project["original_amount"])
    delta_sum = ZERO
    for row in conn.execute(
        "SELECT old_total_amount, new_total_amount FROM plan_amendments "
        "WHERE project_id=? ORDER BY revision",
        (project_id,),
    ):
        delta_sum += _money(row["new_total_amount"]) - _money(row["old_total_amount"])
    if original + delta_sum != total:
        errors.append("total_amount inconsistent with amendments")

    # 第三轨：事件流重算
    replayed = _replay_events(conn, project_id)
    for account in ALL_ACCOUNTS:
        if replayed[account] != ledger[account]:
            errors.append(
                f"event_replay[{account}] {replayed[account]} != ledger[{account}] {ledger[account]}"
            )

    return {
        "project_id": project_id,
        "balanced": not errors,
        "errors": errors,
        "ledger": {a: str(ledger[a]) for a in ALL_ACCOUNTS},
        "orders_by_status": {k: str(v) for k, v in doc_by_status.items()},
        "receipts_total": str(paid_doc),
        "project_total": str(total),
    }
