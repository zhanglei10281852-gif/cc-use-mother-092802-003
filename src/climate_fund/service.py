"""拨款管理核心服务。

业务规则一览
------------
角色（权限不同，互相不能代替）：
- OFFICER  经办人：立项、代受援方提交阶段材料、发起支付指令；
- REVIEWER 审核人：启停项目、审核材料、登记核验结论、批准支付指令、核销；
- FINANCE  财务：  登记付款回执；
- ADMIN    可执行任意角色动作，但仍受“四眼原则”约束
           （经办与审核不得为同一人、材料提交人与审核人不得为同一人）。

项目状态机：DRAFT → ACTIVE ⇄ SUSPENDED → CLOSED。
- ACTIVE    可申请款项（发起支付指令）、可付款；
- SUSPENDED 全部未付资金冻结，禁止新的申请与付款，只可读；
- 恢复（resume）时生成新计划版本，明确承接资金、收回资金与新增出资。

阶段材料：按版本追加；被驳回的版本永久保留、不可再改；补交产生新版本，
不会覆盖历史版本。材料换新版本后，原核验结论自然失效（核验绑定具体版本）。

核验：现场核验(FIELD)与财务凭证(FINANCIAL)各自独立登记、到达时间可不同；
两者对“当前已批准版本”均为 PASS 时，里程碑进入 VERIFIED，方可申请付款。

支付指令：经办人发起(PENDING) → 审核人批准(APPROVED) → 财务登记回执
（PARTIALLY_PAID/PAID，允许部分支付）→ 核销(RECONCILED)。
回执按 (指令, 外部回执号) 幂等：重复报送返回原回执，不重复记账；
同一回执号金额不一致视为冲突。

账本不变量（reconciliation 校验）：
    资金池余额 = 累计拨款 - 累计收回 - 累计支付
    资金池余额 = 在途承诺(未付部分) + 冻结未付 + 可申请余额
    已付未核销 = 累计支付 - 累计核销 ≥ 0
    Σ回执金额 = ΣPAYMENT 流水 = Σ指令已付金额   （三处独立来源必须一致）
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from typing import Any, Iterable, Optional

from .clock import Clock
from .db import Database
from .errors import (
    ConflictError,
    NotFoundError,
    PermissionDenied,
    StateError,
    ValidationError,
)

ROLE_OFFICER = "OFFICER"    # 经办人
ROLE_REVIEWER = "REVIEWER"  # 审核人
ROLE_FINANCE = "FINANCE"    # 财务
ROLE_ADMIN = "ADMIN"

#: 占用资金池额度的指令状态（未付部分视为承诺/冻结）
OPEN_INSTRUCTION_STATUSES = ("PENDING", "APPROVED", "PARTIALLY_PAID", "FROZEN")
#: 里程碑终态
TERMINAL_MILESTONE_STATUSES = ("RECONCILED", "CANCELLED")


def _new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:16]}"


def _row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
    return {k: row[k] for k in row.keys()}


class GrantService:
    """拨款管理服务。所有写操作在单个 IMMEDIATE 事务内完成。"""

    def __init__(self, db: Database, clock: Clock):
        self.db = db
        self.clock = clock

    # ------------------------------------------------------------------ 工具
    def _now(self) -> str:
        return self.clock.now().isoformat(timespec="seconds")

    @staticmethod
    def _user(conn: sqlite3.Connection, actor_id: str) -> sqlite3.Row:
        row = conn.execute("SELECT * FROM users WHERE id = ?", (actor_id,)).fetchone()
        if row is None:
            raise NotFoundError(f"用户不存在: {actor_id}")
        return row

    @staticmethod
    def _require_role(user: sqlite3.Row, *roles: str) -> None:
        if user["role"] not in roles:
            raise PermissionDenied(
                f"角色 {user['role']} 无权执行该操作，需要 {'/'.join(roles)}"
            )

    @staticmethod
    def _check_amount(amount: Any, field: str = "amount") -> int:
        if isinstance(amount, bool) or not isinstance(amount, int) or amount <= 0:
            raise ValidationError(f"{field} 必须为正整数（最小货币单位）: {amount!r}")
        return amount

    def _audit(
        self,
        conn: sqlite3.Connection,
        *,
        project_id: Optional[str],
        entity_type: str,
        entity_id: str,
        action: str,
        actor: sqlite3.Row,
        detail: Any = None,
    ) -> None:
        conn.execute(
            "INSERT INTO audit_events"
            "(id, project_id, entity_type, entity_id, action, actor, role, detail, created_at)"
            " VALUES (?,?,?,?,?,?,?,?,?)",
            (
                _new_id("evt"),
                project_id,
                entity_type,
                entity_id,
                action,
                actor["id"],
                actor["role"],
                json.dumps(detail, ensure_ascii=False) if detail is not None else None,
                self._now(),
            ),
        )

    def _ledger(
        self,
        conn: sqlite3.Connection,
        *,
        project_id: str,
        kind: str,
        amount: int,
        actor: sqlite3.Row,
        revision_id: Optional[str] = None,
        milestone_id: Optional[str] = None,
        instruction_id: Optional[str] = None,
        receipt_id: Optional[str] = None,
        note: Optional[str] = None,
    ) -> None:
        conn.execute(
            "INSERT INTO ledger_entries"
            "(id, project_id, revision_id, milestone_id, instruction_id, receipt_id,"
            " kind, amount, actor, note, created_at)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (
                _new_id("led"),
                project_id,
                revision_id,
                milestone_id,
                instruction_id,
                receipt_id,
                kind,
                amount,
                actor["id"],
                note,
                self._now(),
            ),
        )

    # ------------------------------------------------------------ 行读取辅助
    @staticmethod
    def _get_project(conn: sqlite3.Connection, project_id: str) -> sqlite3.Row:
        row = conn.execute("SELECT * FROM projects WHERE id = ?", (project_id,)).fetchone()
        if row is None:
            raise NotFoundError(f"项目不存在: {project_id}")
        return row

    @staticmethod
    def _get_milestone(conn: sqlite3.Connection, milestone_id: str) -> sqlite3.Row:
        row = conn.execute(
            "SELECT * FROM milestones WHERE id = ?", (milestone_id,)
        ).fetchone()
        if row is None:
            raise NotFoundError(f"里程碑不存在: {milestone_id}")
        return row

    @staticmethod
    def _get_instruction(conn: sqlite3.Connection, instruction_id: str) -> sqlite3.Row:
        row = conn.execute(
            "SELECT * FROM payment_instructions WHERE id = ?", (instruction_id,)
        ).fetchone()
        if row is None:
            raise NotFoundError(f"支付指令不存在: {instruction_id}")
        return row

    @staticmethod
    def _get_evidence(conn: sqlite3.Connection, evidence_id: str) -> sqlite3.Row:
        row = conn.execute(
            "SELECT * FROM evidence_submissions WHERE id = ?", (evidence_id,)
        ).fetchone()
        if row is None:
            raise NotFoundError(f"阶段材料不存在: {evidence_id}")
        return row

    def _project_dict(self, conn: sqlite3.Connection, project_id: str) -> dict[str, Any]:
        """项目视图（含计划版本）。在调用方的事务/连接内读取。"""
        project = _row_to_dict(self._get_project(conn, project_id))
        revisions = conn.execute(
            "SELECT * FROM plan_revisions WHERE project_id = ? ORDER BY revision_no",
            (project_id,),
        ).fetchall()
        project["revisions"] = [_row_to_dict(r) for r in revisions]
        return project

    @staticmethod
    def _current_revision(conn: sqlite3.Connection, project: sqlite3.Row) -> sqlite3.Row:
        return conn.execute(
            "SELECT * FROM plan_revisions WHERE project_id = ? AND revision_no = ?",
            (project["id"], project["current_revision"]),
        ).fetchone()

    @staticmethod
    def _pool_balance(conn: sqlite3.Connection, project_id: str) -> int:
        """资金池余额 = 拨款 - 收回 - 支付。"""
        row = conn.execute(
            "SELECT kind, COALESCE(SUM(amount), 0) AS total FROM ledger_entries"
            " WHERE project_id = ? AND kind IN ('ALLOCATION', 'RECOVERY', 'PAYMENT')"
            " GROUP BY kind",
            (project_id,),
        ).fetchall()
        totals = {r["kind"]: r["total"] for r in row}
        return (
            totals.get("ALLOCATION", 0)
            - totals.get("RECOVERY", 0)
            - totals.get("PAYMENT", 0)
        )

    @staticmethod
    def _open_instruction_unpaid(conn: sqlite3.Connection, project_id: str) -> dict[str, int]:
        """在途承诺（不含冻结）与冻结未付，分列。"""
        rows = conn.execute(
            "SELECT status, COALESCE(SUM(amount - paid_amount), 0) AS unpaid"
            " FROM payment_instructions WHERE project_id = ? AND status IN (?,?,?,?)"
            " GROUP BY status",
            (project_id, *OPEN_INSTRUCTION_STATUSES),
        ).fetchall()
        committed, frozen = 0, 0
        for r in rows:
            if r["status"] == "FROZEN":
                frozen += r["unpaid"]
            else:
                committed += r["unpaid"]
        return {"committed": committed, "frozen": frozen}

    @staticmethod
    def _latest_evidence(
        conn: sqlite3.Connection, milestone_id: str, status: Optional[str] = None
    ) -> Optional[sqlite3.Row]:
        sql = "SELECT * FROM evidence_submissions WHERE milestone_id = ?"
        params: list[Any] = [milestone_id]
        if status:
            sql += " AND status = ?"
            params.append(status)
        sql += " ORDER BY version DESC LIMIT 1"
        return conn.execute(sql, params).fetchone()

    # ================================================================== 用户
    def create_user(self, user_id: str, name: str, role: str) -> dict[str, Any]:
        """登记用户（实际部署中由 IAM 供给，此处为本地演示简化）。"""
        if role not in (ROLE_OFFICER, ROLE_REVIEWER, ROLE_FINANCE, ROLE_ADMIN):
            raise ValidationError(f"未知角色: {role}")
        with self.db.tx() as conn:
            try:
                conn.execute(
                    "INSERT INTO users(id, name, role) VALUES (?,?,?)",
                    (user_id, name, role),
                )
            except sqlite3.IntegrityError:
                raise ConflictError(f"用户已存在: {user_id}") from None
            return {"id": user_id, "name": name, "role": role}

    # ================================================================== 项目
    def create_project(
        self,
        actor_id: str,
        *,
        code: str,
        name: str,
        partner: str,
        location: str,
        total_amount: int,
        milestones: list[dict[str, Any]],
        currency: str = "CNY",
    ) -> dict[str, Any]:
        """立项（DRAFT）。里程碑预算之和必须等于协议总额。"""
        self._check_amount(total_amount, "total_amount")
        if not milestones:
            raise ValidationError("至少需要一个里程碑")
        for m in milestones:
            self._check_amount(m.get("amount"), "milestone.amount")
            if not m.get("title"):
                raise ValidationError("里程碑缺少 title")
        if sum(m["amount"] for m in milestones) != total_amount:
            raise ValidationError("里程碑预算之和必须等于协议总额")

        with self.db.tx() as conn:
            actor = self._user(conn, actor_id)
            self._require_role(actor, ROLE_OFFICER, ROLE_ADMIN)
            now = self._now()
            project_id = _new_id("prj")
            revision_id = _new_id("rev")
            try:
                conn.execute(
                    "INSERT INTO projects(id, code, name, partner, location, total_amount,"
                    " currency, status, current_revision, created_by, created_at, updated_at)"
                    " VALUES (?,?,?,?,?,?,?,'DRAFT',1,?,?,?)",
                    (project_id, code, name, partner, location, total_amount,
                     currency, actor_id, now, now),
                )
            except sqlite3.IntegrityError:
                raise ConflictError(f"项目编号已存在: {code}") from None
            conn.execute(
                "INSERT INTO plan_revisions(id, project_id, revision_no, reason, location,"
                " status, carried_from_previous, recovered_to_donor, new_funds,"
                " created_by, created_at)"
                " VALUES (?,?,1,'INITIAL',?,'ACTIVE',0,0,0,?,?)",
                (revision_id, project_id, location, actor_id, now),
            )
            milestone_rows = []
            for seq, m in enumerate(milestones, start=1):
                mid = _new_id("ms")
                conn.execute(
                    "INSERT INTO milestones(id, project_id, revision_id, seq, title, amount,"
                    " status, created_at) VALUES (?,?,?,?,?,?,'PLANNED',?)",
                    (mid, project_id, revision_id, seq, m["title"], m["amount"], now),
                )
                milestone_rows.append(mid)
            self._audit(conn, project_id=project_id, entity_type="project",
                        entity_id=project_id, action="CREATE", actor=actor,
                        detail={"code": code, "total_amount": total_amount,
                                "location": location})
            return self._project_dict(conn, project_id)

    def activate_project(self, actor_id: str, project_id: str) -> dict[str, Any]:
        """协议生效：DRAFT → ACTIVE，出资方拨付承诺资金入池。"""
        with self.db.tx() as conn:
            actor = self._user(conn, actor_id)
            self._require_role(actor, ROLE_REVIEWER, ROLE_ADMIN)
            project = self._get_project(conn, project_id)
            cur = conn.execute(
                "UPDATE projects SET status = 'ACTIVE', updated_at = ?"
                " WHERE id = ? AND status = 'DRAFT'",
                (self._now(), project_id),
            )
            if cur.rowcount != 1:
                raise StateError(f"项目当前状态为 {project['status']}，不能生效")
            revision = self._current_revision(conn, project)
            self._ledger(conn, project_id=project_id, kind="ALLOCATION",
                         amount=project["total_amount"], actor=actor,
                         revision_id=revision["id"], note="协议生效，承诺资金入池")
            self._audit(conn, project_id=project_id, entity_type="project",
                        entity_id=project_id, action="ACTIVATE", actor=actor)
            return self._project_dict(conn, project_id)

    def suspend_project(self, actor_id: str, project_id: str, *, reason: str) -> dict[str, Any]:
        """暂停项目：未付资金全部冻结，禁止新的申请与付款。"""
        with self.db.tx() as conn:
            actor = self._user(conn, actor_id)
            self._require_role(actor, ROLE_REVIEWER, ROLE_ADMIN)
            project = self._get_project(conn, project_id)
            cur = conn.execute(
                "UPDATE projects SET status = 'SUSPENDED', updated_at = ?"
                " WHERE id = ? AND status = 'ACTIVE'",
                (self._now(), project_id),
            )
            if cur.rowcount != 1:
                raise StateError(f"项目当前状态为 {project['status']}，不能暂停")
            revision = self._current_revision(conn, project)
            conn.execute(
                "UPDATE plan_revisions SET status = 'SUSPENDED' WHERE id = ?",
                (revision["id"],),
            )
            frozen_instructions = conn.execute(
                "UPDATE payment_instructions SET status = 'FROZEN'"
                " WHERE project_id = ? AND status IN ('PENDING','APPROVED','PARTIALLY_PAID')"
                " RETURNING id",
                (project_id,),
            ).fetchall()
            conn.execute(
                "UPDATE milestones SET status = 'FROZEN'"
                " WHERE project_id = ? AND status NOT IN ('RECONCILED','CANCELLED','FROZEN')",
                (project_id,),
            )
            self._audit(conn, project_id=project_id, entity_type="project",
                        entity_id=project_id, action="SUSPEND", actor=actor,
                        detail={"reason": reason,
                                "frozen_instructions": [r["id"] for r in frozen_instructions]})
            return self._project_dict(conn, project_id)

    def resume_project(
        self,
        actor_id: str,
        project_id: str,
        *,
        new_location: str,
        reason: str,
        new_milestones: list[dict[str, Any]],
        recover_amount: int = 0,
        new_funds: int = 0,
    ) -> dict[str, Any]:
        """灾情等原因后恢复项目：生成新计划版本，明确新旧计划资金责任。

        - 被冻结的支付指令全部作废（已付部分仍归属原计划，后续照常核销）；
        - 原计划未拨付余额 = 资金池余额，其中 recover_amount 收回出资方，
          其余作为承接资金带入新计划；
        - 新计划里程碑总额必须 == 承接资金 + 新增出资（new_funds），
          使新旧计划之间的资金责任完全闭合。
        """
        if not new_milestones:
            raise ValidationError("新计划至少需要一个里程碑")
        for m in new_milestones:
            self._check_amount(m.get("amount"), "milestone.amount")
            if not m.get("title"):
                raise ValidationError("里程碑缺少 title")
        if recover_amount < 0 or new_funds < 0:
            raise ValidationError("recover_amount / new_funds 不得为负")

        with self.db.tx() as conn:
            actor = self._user(conn, actor_id)
            self._require_role(actor, ROLE_REVIEWER, ROLE_ADMIN)
            project = self._get_project(conn, project_id)
            if project["status"] != "SUSPENDED":
                raise StateError(f"项目当前状态为 {project['status']}，不能恢复")
            old_revision = self._current_revision(conn, project)

            pool = self._pool_balance(conn, project_id)
            if recover_amount > pool:
                raise ValidationError(
                    f"收回金额 {recover_amount} 超过未拨付余额 {pool}")
            carried = pool - recover_amount
            planned = sum(m["amount"] for m in new_milestones)
            if planned != carried + new_funds:
                raise ValidationError(
                    f"新计划里程碑总额({planned})必须等于 承接资金({carried})"
                    f" + 新增出资({new_funds})"
                )

            now = self._now()
            # 冻结指令作废，承诺额度释放回资金池
            cancelled = conn.execute(
                "UPDATE payment_instructions SET status = 'CANCELLED'"
                " WHERE project_id = ? AND status = 'FROZEN' RETURNING id",
                (project_id,),
            ).fetchall()
            conn.execute(
                "UPDATE milestones SET status = 'CANCELLED'"
                " WHERE revision_id = ? AND status NOT IN ('RECONCILED','CANCELLED')",
                (old_revision["id"],),
            )
            conn.execute(
                "UPDATE plan_revisions SET status = 'SUPERSEDED' WHERE id = ?",
                (old_revision["id"],),
            )
            if recover_amount:
                self._ledger(conn, project_id=project_id, kind="RECOVERY",
                             amount=recover_amount, actor=actor,
                             revision_id=old_revision["id"],
                             note="计划调整，收回出资方")
            new_revision_id = _new_id("rev")
            new_no = project["current_revision"] + 1
            if new_funds:
                self._ledger(conn, project_id=project_id, kind="ALLOCATION",
                             amount=new_funds, actor=actor,
                             revision_id=new_revision_id, note="计划调整，新增出资")
            conn.execute(
                "INSERT INTO plan_revisions(id, project_id, revision_no, reason, location,"
                " status, carried_from_previous, recovered_to_donor, new_funds,"
                " created_by, created_at)"
                " VALUES (?,?,?,?,?,'ACTIVE',?,?,?,?,?)",
                (new_revision_id, project_id, new_no, reason, new_location,
                 carried, recover_amount, new_funds, actor_id, now),
            )
            for seq, m in enumerate(new_milestones, start=1):
                conn.execute(
                    "INSERT INTO milestones(id, project_id, revision_id, seq, title, amount,"
                    " status, created_at) VALUES (?,?,?,?,?,?,'PLANNED',?)",
                    (_new_id("ms"), project_id, new_revision_id, seq,
                     m["title"], m["amount"], now),
                )
            conn.execute(
                "UPDATE projects SET status = 'ACTIVE', current_revision = ?,"
                " location = ?, updated_at = ? WHERE id = ?",
                (new_no, new_location, now, project_id),
            )
            self._audit(conn, project_id=project_id, entity_type="project",
                        entity_id=project_id, action="RESUME", actor=actor,
                        detail={"reason": reason, "new_location": new_location,
                                "carried_from_previous": carried,
                                "recovered_to_donor": recover_amount,
                                "new_funds": new_funds,
                                "cancelled_instructions": [r["id"] for r in cancelled],
                                "new_revision": new_no})
            return self._project_dict(conn, project_id)

    def close_project(self, actor_id: str, project_id: str) -> dict[str, Any]:
        """结项：要求无在途指令、已付款项全部核销；剩余资金收回出资方。"""
        with self.db.tx() as conn:
            actor = self._user(conn, actor_id)
            self._require_role(actor, ROLE_REVIEWER, ROLE_ADMIN)
            project = self._get_project(conn, project_id)
            if project["status"] not in ("ACTIVE", "SUSPENDED"):
                raise StateError(f"项目当前状态为 {project['status']}，不能结项")
            open_count = conn.execute(
                "SELECT COUNT(*) AS c FROM payment_instructions"
                " WHERE project_id = ? AND status IN ('PENDING','APPROVED','PARTIALLY_PAID')",
                (project_id,),
            ).fetchone()["c"]
            if open_count:
                raise StateError(f"尚有 {open_count} 笔未完成的支付指令，不能结项")
            totals = conn.execute(
                "SELECT kind, COALESCE(SUM(amount),0) AS t FROM ledger_entries"
                " WHERE project_id = ? GROUP BY kind",
                (project_id,),
            ).fetchall()
            sums = {r["kind"]: r["t"] for r in totals}
            outstanding = sums.get("PAYMENT", 0) - sums.get("WRITEOFF", 0)
            if outstanding:
                raise StateError(f"尚有已付未核销资金 {outstanding}，不能结项")
            # 暂停中结项：先释放冻结承诺
            conn.execute(
                "UPDATE payment_instructions SET status = 'CANCELLED'"
                " WHERE project_id = ? AND status = 'FROZEN'",
                (project_id,),
            )
            revision = self._current_revision(conn, project)
            pool = (sums.get("ALLOCATION", 0) - sums.get("RECOVERY", 0)
                    - sums.get("PAYMENT", 0))
            if pool:
                self._ledger(conn, project_id=project_id, kind="RECOVERY",
                             amount=pool, actor=actor, revision_id=revision["id"],
                             note="项目结项，剩余资金收回出资方")
            conn.execute(
                "UPDATE milestones SET status = 'CANCELLED'"
                " WHERE project_id = ? AND status NOT IN ('RECONCILED','CANCELLED')",
                (project_id,),
            )
            conn.execute(
                "UPDATE plan_revisions SET status = 'CLOSED' WHERE id = ?",
                (revision["id"],),
            )
            conn.execute(
                "UPDATE projects SET status = 'CLOSED', updated_at = ? WHERE id = ?",
                (self._now(), project_id),
            )
            self._audit(conn, project_id=project_id, entity_type="project",
                        entity_id=project_id, action="CLOSE", actor=actor,
                        detail={"recovered_to_donor": pool})
            return self._project_dict(conn, project_id)

    # ============================================================== 阶段材料
    def submit_evidence(
        self,
        actor_id: str,
        milestone_id: str,
        *,
        content_uri: str,
        note: str = "",
    ) -> dict[str, Any]:
        """提交阶段材料：追加新版本，历史版本（含被驳回的）永不覆盖。"""
        with self.db.tx() as conn:
            actor = self._user(conn, actor_id)
            self._require_role(actor, ROLE_OFFICER, ROLE_ADMIN)
            milestone = self._get_milestone(conn, milestone_id)
            project = self._get_project(conn, milestone["project_id"])
            if project["status"] != "ACTIVE":
                raise StateError(f"项目状态为 {project['status']}，不能提交材料")
            if milestone["status"] in ("FROZEN", "CANCELLED", "RECONCILED"):
                raise StateError(f"里程碑状态为 {milestone['status']}，不能提交材料")
            open_instr = conn.execute(
                "SELECT COUNT(*) AS c FROM payment_instructions"
                " WHERE milestone_id = ? AND status IN (?,?,?,?)",
                (milestone_id, *OPEN_INSTRUCTION_STATUSES),
            ).fetchone()["c"]
            if open_instr:
                raise StateError("该里程碑已有在途支付指令，不能再提交新材料")
            latest = self._latest_evidence(conn, milestone_id)
            if latest is not None and latest["status"] == "SUBMITTED":
                raise ConflictError("上一版材料仍在审核中，不能重复提交")
            version = 1 if latest is None else latest["version"] + 1
            evidence_id = _new_id("ev")
            conn.execute(
                "INSERT INTO evidence_submissions(id, milestone_id, version, content_uri,"
                " note, status, submitted_by, submitted_at)"
                " VALUES (?,?,?,?,?,'SUBMITTED',?,?)",
                (evidence_id, milestone_id, version, content_uri, note,
                 actor_id, self._now()),
            )
            conn.execute(
                "UPDATE milestones SET status = 'EVIDENCE_SUBMITTED' WHERE id = ?",
                (milestone_id,),
            )
            self._audit(conn, project_id=project["id"], entity_type="evidence",
                        entity_id=evidence_id, action="SUBMIT", actor=actor,
                        detail={"milestone_id": milestone_id, "version": version,
                                "content_uri": content_uri})
            return _row_to_dict(conn.execute(
                "SELECT * FROM evidence_submissions WHERE id = ?", (evidence_id,)
            ).fetchone())

    def review_evidence(
        self,
        actor_id: str,
        evidence_id: str,
        *,
        approve: bool,
        comment: str = "",
    ) -> dict[str, Any]:
        """审核阶段材料。已审核的版本不可再改；批准新版本时旧批准版本作废留痕。"""
        with self.db.tx() as conn:
            actor = self._user(conn, actor_id)
            self._require_role(actor, ROLE_REVIEWER, ROLE_ADMIN)
            evidence = self._get_evidence(conn, evidence_id)
            if evidence["submitted_by"] == actor_id:
                raise PermissionDenied("材料提交人与审核人不得为同一人")
            milestone = self._get_milestone(conn, evidence["milestone_id"])
            project = self._get_project(conn, milestone["project_id"])
            if project["status"] != "ACTIVE":
                raise StateError(f"项目状态为 {project['status']}，不能审核材料")
            new_status = "APPROVED" if approve else "REJECTED"
            cur = conn.execute(
                "UPDATE evidence_submissions SET status = ?, reviewed_by = ?,"
                " reviewed_at = ?, review_comment = ?"
                " WHERE id = ? AND status = 'SUBMITTED'",
                (new_status, actor_id, self._now(), comment, evidence_id),
            )
            if cur.rowcount != 1:
                raise ConflictError(
                    f"该材料版本已审核（当前 {evidence['status']}），审核结论不可更改")
            if approve:
                # 旧的批准版本作废（留痕），当前批准版本指向新版本
                conn.execute(
                    "UPDATE evidence_submissions SET status = 'SUPERSEDED'"
                    " WHERE milestone_id = ? AND status = 'APPROVED' AND id != ?",
                    (evidence["milestone_id"], evidence_id),
                )
                conn.execute(
                    "UPDATE milestones SET status = 'EVIDENCE_APPROVED' WHERE id = ?",
                    (evidence["milestone_id"],),
                )
            else:
                conn.execute(
                    "UPDATE milestones SET status = 'EVIDENCE_REJECTED' WHERE id = ?",
                    (evidence["milestone_id"],),
                )
            self._audit(conn, project_id=project["id"], entity_type="evidence",
                        entity_id=evidence_id,
                        action="APPROVE" if approve else "REJECT", actor=actor,
                        detail={"milestone_id": evidence["milestone_id"],
                                "version": evidence["version"], "comment": comment})
            return _row_to_dict(conn.execute(
                "SELECT * FROM evidence_submissions WHERE id = ?", (evidence_id,)
            ).fetchone())

    # ================================================================= 核验
    def record_verification(
        self,
        actor_id: str,
        milestone_id: str,
        *,
        kind: str,
        conclusion: str,
        detail: str = "",
    ) -> dict[str, Any]:
        """登记核验结论（现场/财务两通道独立到达）。两通道均 PASS 后里程碑 VERIFIED。"""
        if kind not in ("FIELD", "FINANCIAL"):
            raise ValidationError("kind 必须为 FIELD 或 FINANCIAL")
        if conclusion not in ("PASS", "FAIL"):
            raise ValidationError("conclusion 必须为 PASS 或 FAIL")
        with self.db.tx() as conn:
            actor = self._user(conn, actor_id)
            self._require_role(actor, ROLE_REVIEWER, ROLE_ADMIN)
            milestone = self._get_milestone(conn, milestone_id)
            project = self._get_project(conn, milestone["project_id"])
            if project["status"] != "ACTIVE":
                raise StateError(f"项目状态为 {project['status']}，不能登记核验")
            if milestone["status"] != "EVIDENCE_APPROVED":
                raise StateError(
                    f"里程碑状态为 {milestone['status']}，需先批准当前版本材料")
            evidence = self._latest_evidence(conn, milestone_id, status="APPROVED")
            verification_id = _new_id("ver")
            conn.execute(
                "INSERT INTO verifications(id, milestone_id, evidence_id, kind, conclusion,"
                " detail, verified_by, verified_at) VALUES (?,?,?,?,?,?,?,?)",
                (verification_id, milestone_id, evidence["id"], kind, conclusion,
                 detail, actor_id, self._now()),
            )
            # 以“当前批准版本”上各通道的最新结论判定
            verified = True
            for k in ("FIELD", "FINANCIAL"):
                row = conn.execute(
                    "SELECT conclusion FROM verifications"
                    " WHERE milestone_id = ? AND evidence_id = ? AND kind = ?"
                    " ORDER BY verified_at DESC, rowid DESC LIMIT 1",
                    (milestone_id, evidence["id"], k),
                ).fetchone()
                if row is None or row["conclusion"] != "PASS":
                    verified = False
                    break
            if verified:
                conn.execute(
                    "UPDATE milestones SET status = 'VERIFIED' WHERE id = ?",
                    (milestone_id,),
                )
            self._audit(conn, project_id=project["id"], entity_type="milestone",
                        entity_id=milestone_id, action="VERIFY", actor=actor,
                        detail={"kind": kind, "conclusion": conclusion,
                                "evidence_id": evidence["id"], "detail": detail})
            return _row_to_dict(conn.execute(
                "SELECT * FROM verifications WHERE id = ?", (verification_id,)
            ).fetchone())

    # ============================================================== 支付指令
    def create_instruction(
        self,
        actor_id: str,
        milestone_id: str,
        *,
        amount: int,
    ) -> dict[str, Any]:
        """经办人发起支付指令（PENDING）。受里程碑余额与项目资金池双重约束。"""
        self._check_amount(amount)
        with self.db.tx() as conn:
            actor = self._user(conn, actor_id)
            self._require_role(actor, ROLE_OFFICER, ROLE_ADMIN)
            milestone = self._get_milestone(conn, milestone_id)
            project = self._get_project(conn, milestone["project_id"])
            if project["status"] != "ACTIVE":
                raise StateError(f"项目状态为 {project['status']}，不能申请款项")
            if milestone["status"] != "VERIFIED":
                raise StateError(f"里程碑状态为 {milestone['status']}，核验通过后方可申请")
            revision = self._current_revision(conn, project)
            if milestone["revision_id"] != revision["id"]:
                raise StateError("该里程碑不属于当前计划版本，不能申请款项")
            used = conn.execute(
                "SELECT COALESCE(SUM(amount),0) AS t FROM payment_instructions"
                " WHERE milestone_id = ? AND status IN (?,?,?,?)",
                (milestone_id, *OPEN_INSTRUCTION_STATUSES),
            ).fetchone()["t"]
            if amount > milestone["amount"] - used:
                raise ValidationError(
                    f"申请金额 {amount} 超过里程碑剩余额度 {milestone['amount'] - used}")
            pool = self._pool_balance(conn, project["id"])
            open_unpaid = self._open_instruction_unpaid(conn, project["id"])
            available = pool - open_unpaid["committed"] - open_unpaid["frozen"]
            if amount > available:
                raise ValidationError(
                    f"申请金额 {amount} 超过项目可申请余额 {available}")
            instruction_id = _new_id("ins")
            conn.execute(
                "INSERT INTO payment_instructions(id, project_id, milestone_id, revision_id,"
                " amount, paid_amount, status, created_by, created_at)"
                " VALUES (?,?,?,?,?,0,'PENDING',?,?)",
                (instruction_id, project["id"], milestone_id, revision["id"],
                 amount, actor_id, self._now()),
            )
            self._audit(conn, project_id=project["id"], entity_type="instruction",
                        entity_id=instruction_id, action="CREATE", actor=actor,
                        detail={"milestone_id": milestone_id, "amount": amount})
            return _row_to_dict(self._get_instruction(conn, instruction_id))

    def approve_instruction(self, actor_id: str, instruction_id: str) -> dict[str, Any]:
        """审核人批准支付指令。经办与审核分离；并发批准只有一方生效。"""
        with self.db.tx() as conn:
            actor = self._user(conn, actor_id)
            self._require_role(actor, ROLE_REVIEWER, ROLE_ADMIN)
            instruction = self._get_instruction(conn, instruction_id)
            if instruction["created_by"] == actor_id:
                raise PermissionDenied("经办人与审核人不得为同一人")
            cur = conn.execute(
                "UPDATE payment_instructions SET status = 'APPROVED', approved_by = ?,"
                " approved_at = ? WHERE id = ? AND status = 'PENDING'",
                (actor_id, self._now(), instruction_id),
            )
            if cur.rowcount != 1:
                raise ConflictError(
                    f"指令当前状态为 {instruction['status']}，不能重复批准")
            self._audit(conn, project_id=instruction["project_id"],
                        entity_type="instruction", entity_id=instruction_id,
                        action="APPROVE", actor=actor,
                        detail={"amount": instruction["amount"]})
            return _row_to_dict(self._get_instruction(conn, instruction_id))

    def record_receipt(
        self,
        actor_id: str,
        instruction_id: str,
        *,
        external_ref: str,
        amount: int,
    ) -> tuple[dict[str, Any], bool]:
        """财务登记付款回执。

        幂等：同一 (指令, 外部回执号) 重复报送返回原回执且 created=False；
        同一回执号但金额不同视为冲突。返回 (回执, 是否新建)。
        """
        self._check_amount(amount)
        if not external_ref:
            raise ValidationError("external_ref 不能为空")
        with self.db.tx() as conn:
            actor = self._user(conn, actor_id)
            self._require_role(actor, ROLE_FINANCE, ROLE_ADMIN)
            instruction = self._get_instruction(conn, instruction_id)
            receipt_id = _new_id("rcp")
            cur = conn.execute(
                "INSERT OR IGNORE INTO payment_receipts(id, instruction_id, external_ref,"
                " amount, received_by, received_at) VALUES (?,?,?,?,?,?)",
                (receipt_id, instruction_id, external_ref, amount,
                 actor_id, self._now()),
            )
            if cur.rowcount == 0:
                existing = conn.execute(
                    "SELECT * FROM payment_receipts WHERE instruction_id = ?"
                    " AND external_ref = ?",
                    (instruction_id, external_ref),
                ).fetchone()
                if existing["amount"] != amount:
                    raise ConflictError(
                        f"回执号 {external_ref} 已登记金额 {existing['amount']}，"
                        f"与本次 {amount} 不一致")
                return _row_to_dict(existing), False
            # 条件更新：防超付、防并发叠加，状态同步推进
            cur = conn.execute(
                "UPDATE payment_instructions"
                " SET paid_amount = paid_amount + ?,"
                "     status = CASE WHEN paid_amount + ? >= amount THEN 'PAID'"
                "                   ELSE 'PARTIALLY_PAID' END"
                " WHERE id = ? AND status IN ('APPROVED','PARTIALLY_PAID')"
                "   AND paid_amount + ? <= amount",
                (amount, amount, instruction_id, amount),
            )
            if cur.rowcount != 1:
                raise StateError(
                    f"指令状态为 {instruction['status']} 或回执金额超出指令余额，"
                    "不能登记该回执")
            self._ledger(conn, project_id=instruction["project_id"], kind="PAYMENT",
                         amount=amount, actor=actor,
                         revision_id=instruction["revision_id"],
                         milestone_id=instruction["milestone_id"],
                         instruction_id=instruction_id, receipt_id=receipt_id,
                         note=f"银行回执 {external_ref}")
            self._audit(conn, project_id=instruction["project_id"],
                        entity_type="instruction", entity_id=instruction_id,
                        action="RECEIPT", actor=actor,
                        detail={"external_ref": external_ref, "amount": amount,
                                "receipt_id": receipt_id})
            return _row_to_dict(conn.execute(
                "SELECT * FROM payment_receipts WHERE id = ?", (receipt_id,)
            ).fetchone()), True

    def writeoff_instruction(self, actor_id: str, instruction_id: str) -> dict[str, Any]:
        """核销：已付资金确认用途合规。全额付清或作废指令的已付部分均可核销。"""
        with self.db.tx() as conn:
            actor = self._user(conn, actor_id)
            self._require_role(actor, ROLE_REVIEWER, ROLE_ADMIN)
            instruction = self._get_instruction(conn, instruction_id)
            if instruction["created_by"] == actor_id:
                raise PermissionDenied("经办人与核销人不得为同一人")
            payable = (
                instruction["status"] == "PAID"
                or (instruction["status"] == "CANCELLED" and instruction["paid_amount"] > 0)
            )
            if not payable:
                raise StateError(
                    f"指令状态为 {instruction['status']}，不能核销")
            cur = conn.execute(
                "UPDATE payment_instructions SET status = 'RECONCILED',"
                " written_off_by = ?, written_off_at = ?"
                " WHERE id = ? AND status = ?",
                (actor_id, self._now(), instruction_id, instruction["status"]),
            )
            if cur.rowcount != 1:
                raise ConflictError("指令状态已变化，核销失败")
            amount = instruction["paid_amount"]
            self._ledger(conn, project_id=instruction["project_id"], kind="WRITEOFF",
                         amount=amount, actor=actor,
                         revision_id=instruction["revision_id"],
                         milestone_id=instruction["milestone_id"],
                         instruction_id=instruction_id, note="支出合规，予以核销")
            self._audit(conn, project_id=instruction["project_id"],
                        entity_type="instruction", entity_id=instruction_id,
                        action="WRITEOFF", actor=actor, detail={"amount": amount})
            # 里程碑预算全部付清且指令均核销 → 里程碑核销完成
            milestone = self._get_milestone(conn, instruction["milestone_id"])
            if milestone["status"] not in TERMINAL_MILESTONE_STATUSES:
                rest = conn.execute(
                    "SELECT COUNT(*) AS c FROM payment_instructions"
                    " WHERE milestone_id = ? AND status != 'RECONCILED'",
                    (instruction["milestone_id"],),
                ).fetchone()["c"]
                done = conn.execute(
                    "SELECT COALESCE(SUM(amount),0) AS t FROM payment_instructions"
                    " WHERE milestone_id = ? AND status = 'RECONCILED'",
                    (instruction["milestone_id"],),
                ).fetchone()["t"]
                if rest == 0 and done == milestone["amount"]:
                    conn.execute(
                        "UPDATE milestones SET status = 'RECONCILED' WHERE id = ?",
                        (milestone["id"],),
                    )
            return _row_to_dict(self._get_instruction(conn, instruction_id))

    # ================================================================= 视图
    def get_project(self, project_id: str) -> dict[str, Any]:
        with self.db.read() as conn:
            return self._project_dict(conn, project_id)

    def list_milestones(self, project_id: str) -> list[dict[str, Any]]:
        with self.db.read() as conn:
            self._get_project(conn, project_id)
            rows = conn.execute(
                "SELECT m.*, r.revision_no FROM milestones m"
                " JOIN plan_revisions r ON r.id = m.revision_id"
                " WHERE m.project_id = ? ORDER BY r.revision_no, m.seq",
                (project_id,),
            ).fetchall()
            return [_row_to_dict(r) for r in rows]

    def get_instruction(self, instruction_id: str) -> dict[str, Any]:
        with self.db.read() as conn:
            return _row_to_dict(self._get_instruction(conn, instruction_id))

    def list_evidence(self, milestone_id: str) -> list[dict[str, Any]]:
        with self.db.read() as conn:
            self._get_milestone(conn, milestone_id)
            rows = conn.execute(
                "SELECT * FROM evidence_submissions WHERE milestone_id = ?"
                " ORDER BY version",
                (milestone_id,),
            ).fetchall()
            return [_row_to_dict(r) for r in rows]

    def availability(self, project_id: str) -> dict[str, Any]:
        """按项目状态给出可申请/已承诺/已冻结/已付等资金视图。"""
        with self.db.read() as conn:
            project = self._get_project(conn, project_id)
            pool = self._pool_balance(conn, project_id)
            open_unpaid = self._open_instruction_unpaid(conn, project_id)
            uncommitted = pool - open_unpaid["committed"] - open_unpaid["frozen"]
            milestones = []
            for m in conn.execute(
                "SELECT * FROM milestones WHERE project_id = ? ORDER BY seq",
                (project_id,),
            ).fetchall():
                row = conn.execute(
                    "SELECT COALESCE(SUM(amount),0) AS instructed,"
                    " COALESCE(SUM(paid_amount),0) AS paid FROM payment_instructions"
                    " WHERE milestone_id = ? AND status IN (?,?,?,?)",
                    (m["id"], *OPEN_INSTRUCTION_STATUSES),
                ).fetchone()
                paid_total = conn.execute(
                    "SELECT COALESCE(SUM(paid_amount),0) AS t FROM payment_instructions"
                    " WHERE milestone_id = ?",
                    (m["id"],),
                ).fetchone()["t"]
                milestones.append({
                    "milestone_id": m["id"],
                    "title": m["title"],
                    "status": m["status"],
                    "amount": m["amount"],
                    "instructed_open": row["instructed"],
                    "paid_total": paid_total,
                    "remaining_to_instruct": m["amount"] - row["instructed"],
                })
            return {
                "project_id": project_id,
                "project_status": project["status"],
                "pool_balance": pool,
                "committed_unpaid": open_unpaid["committed"],
                "frozen_unpaid": open_unpaid["frozen"],
                "pool_uncommitted": uncommitted,
                # 只有 ACTIVE 状态才允许申请款项
                "available_to_apply": uncommitted if project["status"] == "ACTIVE" else 0,
                "milestones": milestones,
            }

    def ledger(self, project_id: str) -> dict[str, Any]:
        with self.db.read() as conn:
            self._get_project(conn, project_id)
            rows = conn.execute(
                "SELECT * FROM ledger_entries WHERE project_id = ?"
                " ORDER BY created_at, rowid",
                (project_id,),
            ).fetchall()
            totals: dict[str, int] = {}
            for r in rows:
                totals[r["kind"]] = totals.get(r["kind"], 0) + r["amount"]
            return {"project_id": project_id, "totals": totals,
                    "entries": [_row_to_dict(r) for r in rows]}

    def reconciliation(self, project_id: str) -> dict[str, Any]:
        """对账：多来源交叉校验，确认账目对平。"""
        with self.db.read() as conn:
            self._get_project(conn, project_id)
            ledger_totals = {
                r["kind"]: r["t"]
                for r in conn.execute(
                    "SELECT kind, COALESCE(SUM(amount),0) AS t FROM ledger_entries"
                    " WHERE project_id = ? GROUP BY kind",
                    (project_id,),
                ).fetchall()
            }
            allocated = ledger_totals.get("ALLOCATION", 0)
            recovered = ledger_totals.get("RECOVERY", 0)
            paid = ledger_totals.get("PAYMENT", 0)
            written_off = ledger_totals.get("WRITEOFF", 0)

            receipts_sum = conn.execute(
                "SELECT COALESCE(SUM(r.amount),0) AS t FROM payment_receipts r"
                " JOIN payment_instructions i ON i.id = r.instruction_id"
                " WHERE i.project_id = ?",
                (project_id,),
            ).fetchone()["t"]
            instr = conn.execute(
                "SELECT COALESCE(SUM(paid_amount),0) AS paid,"
                " COALESCE(SUM(CASE WHEN status IN ('PENDING','APPROVED','PARTIALLY_PAID')"
                "   THEN amount - paid_amount ELSE 0 END),0) AS committed,"
                " COALESCE(SUM(CASE WHEN status = 'FROZEN'"
                "   THEN amount - paid_amount ELSE 0 END),0) AS frozen,"
                " COALESCE(SUM(CASE WHEN status = 'RECONCILED'"
                "   THEN paid_amount ELSE 0 END),0) AS reconciled_paid"
                " FROM payment_instructions WHERE project_id = ?",
                (project_id,),
            ).fetchone()
            pool = allocated - recovered - paid
            outstanding = paid - written_off
            milestone_breach = conn.execute(
                "SELECT COUNT(*) AS c FROM milestones m WHERE m.project_id = ?"
                " AND m.status != 'CANCELLED' AND ("
                "   SELECT COALESCE(SUM(pi.amount),0) FROM payment_instructions pi"
                "   WHERE pi.milestone_id = m.id AND pi.status IN (?,?,?,?)"
                " ) > m.amount",
                (project_id, *OPEN_INSTRUCTION_STATUSES),
            ).fetchone()["c"]
            checks = {
                "回执合计=支付流水合计": receipts_sum == paid,
                "指令已付合计=支付流水合计": instr["paid"] == paid,
                "核销流水=已核销指令付款额": written_off == instr["reconciled_paid"],
                "资金池余额非负": pool >= 0,
                "已付未核销非负": outstanding >= 0,
                "承诺与冻结不超资金池": instr["committed"] + instr["frozen"] <= pool,
                "里程碑额度未被超额占用": milestone_breach == 0,
            }
            # 分计划版本的资金责任汇总
            revisions = []
            for rev in conn.execute(
                "SELECT * FROM plan_revisions WHERE project_id = ? ORDER BY revision_no",
                (project_id,),
            ).fetchall():
                rev_paid = conn.execute(
                    "SELECT COALESCE(SUM(amount),0) AS t FROM ledger_entries"
                    " WHERE revision_id = ? AND kind = 'PAYMENT'",
                    (rev["id"],),
                ).fetchone()["t"]
                revisions.append({
                    "revision_no": rev["revision_no"],
                    "status": rev["status"],
                    "location": rev["location"],
                    "carried_from_previous": rev["carried_from_previous"],
                    "recovered_to_donor": rev["recovered_to_donor"],
                    "new_funds": rev["new_funds"],
                    "paid_under_revision": rev_paid,
                })
            return {
                "project_id": project_id,
                "allocated": allocated,
                "recovered": recovered,
                "paid": paid,
                "written_off": written_off,
                "pool_balance": pool,
                "committed_unpaid": instr["committed"],
                "frozen_unpaid": instr["frozen"],
                "available_to_apply": pool - instr["committed"] - instr["frozen"],
                "outstanding_unreconciled": outstanding,
                "checks": checks,
                "balanced": all(checks.values()),
                "revisions": revisions,
            }

    def fund_trail(self, project_id: str) -> dict[str, Any]:
        """全链路资金轨迹：计划版本 → 里程碑 → 材料版本 → 核验 → 指令 → 回执/核销。"""
        with self.db.read() as conn:
            project = _row_to_dict(self._get_project(conn, project_id))
            revisions = []
            for rev in conn.execute(
                "SELECT * FROM plan_revisions WHERE project_id = ? ORDER BY revision_no",
                (project_id,),
            ).fetchall():
                milestones = []
                for m in conn.execute(
                    "SELECT * FROM milestones WHERE revision_id = ? ORDER BY seq",
                    (rev["id"],),
                ).fetchall():
                    evidence = [_row_to_dict(r) for r in conn.execute(
                        "SELECT * FROM evidence_submissions WHERE milestone_id = ?"
                        " ORDER BY version",
                        (m["id"],),
                    ).fetchall()]
                    verifications = [_row_to_dict(r) for r in conn.execute(
                        "SELECT * FROM verifications WHERE milestone_id = ?"
                        " ORDER BY verified_at, rowid",
                        (m["id"],),
                    ).fetchall()]
                    instructions = []
                    for ins in conn.execute(
                        "SELECT * FROM payment_instructions WHERE milestone_id = ?"
                        " ORDER BY created_at, rowid",
                        (m["id"],),
                    ).fetchall():
                        receipts = [_row_to_dict(r) for r in conn.execute(
                            "SELECT * FROM payment_receipts WHERE instruction_id = ?"
                            " ORDER BY received_at, rowid",
                            (ins["id"],),
                        ).fetchall()]
                        writeoff = conn.execute(
                            "SELECT * FROM ledger_entries WHERE instruction_id = ?"
                            " AND kind = 'WRITEOFF'",
                            (ins["id"],),
                        ).fetchone()
                        instructions.append({
                            "instruction": _row_to_dict(ins),
                            "receipts": receipts,
                            "writeoff": _row_to_dict(writeoff) if writeoff else None,
                        })
                    milestones.append({
                        "milestone": _row_to_dict(m),
                        "evidence": evidence,
                        "verifications": verifications,
                        "instructions": instructions,
                    })
                revisions.append({"revision": _row_to_dict(rev),
                                  "milestones": milestones})
            return {"project": project, "revisions": revisions}

    def instruction_trail(self, instruction_id: str) -> dict[str, Any]:
        """单笔资金从批准到核销的依据与状态变化时间线。"""
        with self.db.read() as conn:
            instruction = _row_to_dict(self._get_instruction(conn, instruction_id))
            milestone = self._get_milestone(conn, instruction["milestone_id"])
            revision = conn.execute(
                "SELECT * FROM plan_revisions WHERE id = ?",
                (instruction["revision_id"],),
            ).fetchone()
            evidence = [_row_to_dict(r) for r in conn.execute(
                "SELECT * FROM evidence_submissions WHERE milestone_id = ?"
                " ORDER BY version",
                (milestone["id"],),
            ).fetchall()]
            verifications = [_row_to_dict(r) for r in conn.execute(
                "SELECT * FROM verifications WHERE milestone_id = ?"
                " ORDER BY verified_at, rowid",
                (milestone["id"],),
            ).fetchall()]
            receipts = [_row_to_dict(r) for r in conn.execute(
                "SELECT * FROM payment_receipts WHERE instruction_id = ?"
                " ORDER BY received_at, rowid",
                (instruction_id,),
            ).fetchall()]
            events = [_row_to_dict(r) for r in conn.execute(
                "SELECT * FROM audit_events WHERE entity_type = 'instruction'"
                " AND entity_id = ? ORDER BY created_at, rowid",
                (instruction_id,),
            ).fetchall()]
            return {
                "instruction": instruction,
                "basis": {
                    "revision": _row_to_dict(revision),
                    "milestone": _row_to_dict(milestone),
                    "evidence": evidence,
                    "verifications": verifications,
                },
                "receipts": receipts,
                "timeline": events,
            }

    def audit_trail(self, project_id: str) -> list[dict[str, Any]]:
        with self.db.read() as conn:
            self._get_project(conn, project_id)
            rows = conn.execute(
                "SELECT * FROM audit_events WHERE project_id = ?"
                " ORDER BY created_at, rowid",
                (project_id,),
            ).fetchall()
            return [_row_to_dict(r) for r in rows]
