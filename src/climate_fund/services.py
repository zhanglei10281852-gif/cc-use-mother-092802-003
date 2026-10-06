"""领域服务：协议、证据版本、核验结论、支付指令、回执与计划修订。

所有写方法都在单个 BEGIN IMMEDIATE 事务内完成“读状态 -> 校验 -> 改状态 ->
追加事件 -> 分类账过账”，因此并发审批 / 并发回执由 SQLite 写锁串行化，
锁内复查杜绝超额批准与重复支付。

角色分工（互相不可代替）：
    officer  经办人：建协议/里程碑、交材料、申请款项、暂停/恢复、登记改址
    reviewer 审核人：受理或驳回材料、出具现场核验、批准/否决/冻结款项
    finance  财务：登记付款回执（放款）、核销
"""

from __future__ import annotations

import sqlite3
import uuid
from decimal import Decimal

from .clock import Clock
from .database import transaction
from .errors import ConflictError, DomainError, NotFoundError, PermissionError, ValidationError
from .ledger import log_event, post, reconciliation_report

D = Decimal
CENT = D("0.01")
ROLE_OFFICER, ROLE_REVIEWER, ROLE_FINANCE = "officer", "reviewer", "finance"


def _amount(value) -> Decimal:
    try:
        amount = D(str(value)).quantize(CENT)
    except Exception as exc:  # noqa: BLE001
        raise ValidationError(f"金额无法解析: {value!r}") from exc
    if amount <= 0:
        raise ValidationError("金额必须为正数")
    return amount


def _uid(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


class FundService:
    def __init__(self, conn: sqlite3.Connection, clock: Clock):
        self.conn = conn
        self.clock = clock

    # ---------------------------------------------------------------- helpers
    def _now(self) -> str:
        return self.clock.now().isoformat()

    def _user(self, user_id: str) -> sqlite3.Row:
        row = self.conn.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()
        if row is None:
            raise PermissionError("用户不存在或未提供 X-User-Id", code="unauthorized")
        return row

    def _require_role(self, user_id: str, *roles: str) -> sqlite3.Row:
        user = self._user(user_id)
        if user["role"] not in roles:
            raise PermissionError(
                f"角色 {user['role']} 无权执行此操作（需要 {'/'.join(roles)}）"
            )
        return user

    def _project(self, project_id: str) -> sqlite3.Row:
        row = self.conn.execute("SELECT * FROM projects WHERE id=?", (project_id,)).fetchone()
        if row is None:
            raise NotFoundError(f"项目 {project_id} 不存在")
        return row

    def _milestone(self, milestone_id: str) -> sqlite3.Row:
        row = self.conn.execute("SELECT * FROM milestones WHERE id=?", (milestone_id,)).fetchone()
        if row is None:
            raise NotFoundError(f"里程碑 {milestone_id} 不存在")
        return row

    def _order(self, order_id: str) -> sqlite3.Row:
        row = self.conn.execute("SELECT * FROM payment_orders WHERE id=?", (order_id,)).fetchone()
        if row is None:
            raise NotFoundError(f"支付指令 {order_id} 不存在")
        return row

    def _balance(self, project_id: str, account: str) -> Decimal:
        row = self.conn.execute(
            "SELECT COALESCE(SUM(amount),'0') AS s FROM ledger_entries "
            "WHERE project_id=? AND account=?",
            (project_id, account),
        ).fetchone()
        return D(row["s"])

    def create_user(self, user_id: str, name: str, role: str) -> dict:
        if role not in (ROLE_OFFICER, ROLE_REVIEWER, ROLE_FINANCE):
            raise ValidationError(f"未知角色: {role}")
        with transaction(self.conn):
            try:
                self.conn.execute(
                    "INSERT INTO users (id,name,role,created_at) VALUES (?,?,?,?)",
                    (user_id, name, role, self._now()),
                )
            except sqlite3.IntegrityError as exc:
                raise ConflictError(f"用户 {user_id} 已存在") from exc
        return {"id": user_id, "name": name, "role": role}

    # ------------------------------------------------------------- 协议/项目
    def create_project(
        self, actor: str, *, title: str, grantee: str, site: str,
        total_amount, currency: str = "USD",
    ) -> dict:
        user = self._require_role(actor, ROLE_OFFICER)
        amount = _amount(total_amount)
        pid = _uid("proj")
        now = self._now()
        with transaction(self.conn):
            self.conn.execute(
                "INSERT INTO projects (id,title,grantee,currency,total_amount,original_amount,"
                "original_site,current_site,plan_revision,status,created_at) "
                "VALUES (?,?,?,?,?,?,?,?,1,'active',?)",
                (pid, title, grantee, currency, str(amount), str(amount), site, site, now),
            )
            # 开局过账：借 budget（可申请预算），贷 appropriation（拨款来源）
            post(
                self.conn, at=now, actor_id=user["id"], project_id=pid,
                entity="project", entity_id=pid, action="project_created",
                legs=[("budget", amount), ("appropriation", -amount)],
                memo=f"协议签订 {title}，拨款总额 {amount} {currency}，实施地点 {site}",
                extra_payload={"total_amount": str(amount), "currency": currency, "site": site},
            )
        return self.get_project(pid)

    def get_project(self, project_id: str) -> dict:
        p = self._project(project_id)
        totals = self._all_balances(project_id)
        return {
            "id": p["id"], "title": p["title"], "grantee": p["grantee"],
            "currency": p["currency"], "status": p["status"],
            "plan_revision": p["plan_revision"],
            "original_site": p["original_site"], "current_site": p["current_site"],
            "original_amount": p["original_amount"], "total_amount": p["total_amount"],
            "balances": totals,
        }

    def _all_balances(self, project_id: str) -> dict[str, str]:
        rows = self.conn.execute(
            "SELECT account, COALESCE(SUM(amount),'0') AS s FROM ledger_entries "
            "WHERE project_id=? GROUP BY account", (project_id,),
        ).fetchall()
        return {r["account"]: str(D(r["s"]).quantize(CENT)) for r in rows}

    # --------------------------------------------------------------- 里程碑
    def add_milestone(
        self, actor: str, project_id: str, *, sequence: int, title: str, planned_amount,
        plan_revision: int | None = None,
    ) -> dict:
        user = self._require_role(actor, ROLE_OFFICER)
        amount = _amount(planned_amount)
        with transaction(self.conn):
            p = self._project(project_id)
            revision = plan_revision or p["plan_revision"]
            if revision != p["plan_revision"]:
                raise ConflictError(
                    f"计划已修订至 r{p['plan_revision']}，不能在旧版本 r{revision} 下新建里程碑")
            mid = _uid("mile")
            try:
                self.conn.execute(
                    "INSERT INTO milestones (id,project_id,sequence,title,plan_revision,"
                    "planned_amount,state,created_at,updated_at) VALUES (?,?,?,?,?,?,'planned',?,?)",
                    (mid, project_id, sequence, title, revision, str(amount),
                     self._now(), self._now()),
                )
            except sqlite3.IntegrityError as exc:
                raise ConflictError(f"序号 {sequence} 在该计划版本下已存在") from exc
            log_event(
                self.conn, at=self._now(), actor_id=user["id"],
                entity="milestone", entity_id=mid, action="milestone_created",
                payload={"project_id": project_id, "sequence": sequence,
                         "plan_revision": revision, "planned_amount": str(amount), "title": title},
            )
        return self.get_milestone(mid)

    def get_milestone(self, milestone_id: str) -> dict:
        m = self._milestone(milestone_id)
        versions = self.conn.execute(
            "SELECT id,version,doc_ref,note,status,submitted_by,submitted_at,decided_by,"
            "decided_at,decision_note FROM evidence_versions WHERE milestone_id=? ORDER BY version",
            (milestone_id,),
        ).fetchall()
        verifs = self.conn.execute(
            "SELECT * FROM verifications WHERE milestone_id=? ORDER BY created_at", (milestone_id,),
        ).fetchall()
        committed = self.conn.execute(
            "SELECT COALESCE(SUM(amount),'0') AS s FROM payment_orders "
            "WHERE milestone_id=? AND status NOT IN ('rejected','cancelled')",
            (milestone_id,),
        ).fetchone()["s"]
        return {
            "id": m["id"], "project_id": m["project_id"], "sequence": m["sequence"],
            "title": m["title"], "plan_revision": m["plan_revision"],
            "planned_amount": m["planned_amount"], "state": m["state"],
            "frozen_reason": m["frozen_reason"],
            "committed_amount": committed,
            "evidence_versions": [dict(v) for v in versions],
            "verifications": [dict(v) for v in verifs],
        }

    # ----------------------------------------------------------- 证据（材料）
    def submit_evidence(self, actor: str, milestone_id: str, *, doc_ref: str, note: str = "") -> dict:
        """补交材料一律产生新版本；曾被驳回的版本原样保留，不会被覆盖。"""
        user = self._require_role(actor, ROLE_OFFICER)
        with transaction(self.conn):
            m = self._milestone(milestone_id)
            p = self._project(m["project_id"])
            if p["status"] != "active":
                raise ConflictError(f"项目状态为 {p['status']}，暂停期间不能提交材料")
            last = self.conn.execute(
                "SELECT version,status FROM evidence_versions WHERE milestone_id=? "
                "ORDER BY version DESC LIMIT 1", (milestone_id,),
            ).fetchone()
            version = (last["version"] + 1) if last else 1
            eid = _uid("evi")
            self.conn.execute(
                "INSERT INTO evidence_versions (id,milestone_id,version,doc_ref,note,status,"
                "submitted_by,submitted_at) VALUES (?,?,?,?,?, 'submitted',?,?)",
                (eid, milestone_id, version, doc_ref, note, user["id"], self._now()),
            )
            # 上一版若仍是 submitted（未决），标记为 superseded；rejected 永不改状态。
            if last and last["status"] == "submitted":
                self.conn.execute(
                    "UPDATE evidence_versions SET status='superseded' WHERE milestone_id=? "
                    "AND version=? AND status='submitted'",
                    (milestone_id, version - 1),
                )
            self._set_milestone_state(m["id"], "evidence_submitted")
            log_event(
                self.conn, at=self._now(), actor_id=user["id"],
                entity="evidence", entity_id=eid, action="evidence_submitted",
                payload={"milestone_id": milestone_id, "version": version, "doc_ref": doc_ref},
            )
        return self.get_milestone(milestone_id)

    def decide_evidence(
        self, actor: str, evidence_id: str, *, accept: bool, note: str = "",
    ) -> dict:
        """审核人受理/驳回某个**具体版本**：驳回不阻止后续补交，但该版本永远是 rejected。"""
        user = self._require_role(actor, ROLE_REVIEWER)
        with transaction(self.conn):
            ev = self.conn.execute(
                "SELECT * FROM evidence_versions WHERE id=?", (evidence_id,),
            ).fetchone()
            if ev is None:
                raise NotFoundError(f"材料版本 {evidence_id} 不存在")
            if ev["status"] in ("rejected", "accepted", "superseded"):
                raise ConflictError(f"该版本状态为 {ev['status']}，不可再决策（请以最新提交版本为准）")
            if ev["submitted_by"] == user["id"]:
                raise PermissionError("提交人与审核人不能为同一人")
            new_status = "accepted" if accept else "rejected"
            self.conn.execute(
                "UPDATE evidence_versions SET status=?, decided_by=?, decided_at=?, decision_note=? "
                "WHERE id=?",
                (new_status, user["id"], self._now(), note, evidence_id),
            )
            if not accept:
                self._set_milestone_state(ev["milestone_id"], "rework_requested")
            log_event(
                self.conn, at=self._now(), actor_id=user["id"],
                entity="evidence", entity_id=evidence_id,
                action="evidence_accepted" if accept else "evidence_rejected",
                payload={"milestone_id": ev["milestone_id"], "note": note},
            )
            return self.get_milestone(ev["milestone_id"])

    # ------------------------------------------------------------ 现场核验
    def record_verification(
        self, actor: str, milestone_id: str, *, evidence_id: str,
        result: str, site_actual: str, verified_amount=None, note: str = "",
    ) -> dict:
        """现场核验结论独立留痕；可晚于材料、晚于申请到达。"""
        user = self._require_role(actor, ROLE_REVIEWER)
        if result not in ("pass", "fail", "partial"):
            raise ValidationError("result 必须为 pass/fail/partial")
        with transaction(self.conn):
            m = self._milestone(milestone_id)
            ev = self.conn.execute(
                "SELECT * FROM evidence_versions WHERE id=? AND milestone_id=?",
                (evidence_id, milestone_id),
            ).fetchone()
            if ev is None:
                raise NotFoundError("核验所依据的材料版本不存在或不属于该里程碑")
            if ev["status"] != "accepted":
                raise ConflictError(f"只能对已受理的材料版本核验，当前状态 {ev['status']}")
            if result == "partial":
                if verified_amount is None:
                    raise ValidationError("partial 核验必须给出核定金额 verified_amount")
                va = _amount(verified_amount)
                if va >= D(m["planned_amount"]):
                    raise ValidationError("partial 核定金额应低于阶段计划金额，否则应为 pass")
            else:
                va = D(m["planned_amount"]) if result == "pass" else D("0")
            vid = _uid("veri")
            try:
                self.conn.execute(
                    "INSERT INTO verifications (id,milestone_id,evidence_id,result,"
                    "verified_amount,site_actual,inspector,note,created_at) "
                    "VALUES (?,?,?,?,?,?,?,?,?)",
                    (vid, milestone_id, evidence_id, result,
                     str(va) if va is not None else None, site_actual,
                     user["id"], note, self._now()),
                )
            except sqlite3.IntegrityError as exc:
                raise ConflictError("该材料版本已有核验结论") from exc
            self._set_milestone_state(
                milestone_id, "verified" if result in ("pass", "partial") else "rework_requested"
            )
            log_event(
                self.conn, at=self._now(), actor_id=user["id"],
                entity="verification", entity_id=vid, action=f"verification_{result}",
                payload={"milestone_id": milestone_id, "evidence_id": evidence_id,
                         "site_actual": site_actual, "verified_amount": str(va), "note": note},
            )
        return self.get_milestone(milestone_id)

    def _latest_verification(self, milestone_id: str) -> sqlite3.Row | None:
        return self.conn.execute(
            "SELECT * FROM verifications WHERE milestone_id=? ORDER BY created_at DESC, id DESC LIMIT 1",
            (milestone_id,),
        ).fetchone()

    def _set_milestone_state(self, milestone_id: str, state: str) -> None:
        self.conn.execute(
            "UPDATE milestones SET state=?, updated_at=?, frozen_reason=NULL WHERE id=?",
            (state, self._now(), milestone_id),
        )

    # ------------------------------------------------------------- 支付指令
    def request_payment(
        self, actor: str, project_id: str, milestone_id: str, amount, *, note: str = "",
    ) -> dict:
        """经办人申请款项。能否申请由项目状态、计划版本、预算余额与阶段上限共同决定。"""
        user = self._require_role(actor, ROLE_OFFICER)
        amount = _amount(amount)
        with transaction(self.conn):
            p = self._project(project_id)
            m = self._milestone(milestone_id)
            if m["project_id"] != project_id:
                raise ValidationError("里程碑不属于该项目")
            if p["status"] != "active":
                raise ConflictError(f"项目 {p['status']}：款项不可申请，先恢复项目")
            # 改址后，旧计划版本的里程碑原则上关闭；改址前已核验的除外。
            latest_amend = self.conn.execute(
                "SELECT * FROM plan_amendments WHERE project_id=? ORDER BY revision DESC LIMIT 1",
                (project_id,),
            ).fetchone()
            if latest_amend and m["plan_revision"] < p["plan_revision"]:
                v = self._latest_verification(milestone_id)
                if v is None or v["created_at"] >= latest_amend["created_at"]:
                    raise ConflictError("该里程碑属于已被灾情改址取代的旧计划，不可再申请；"
                                        "请在新计划版本下建立里程碑")
            # 至少有一份已受理的材料（核验可以尚未到达）
            accepted = self.conn.execute(
                "SELECT COUNT(*) AS c FROM evidence_versions WHERE milestone_id=? AND status='accepted'",
                (milestone_id,),
            ).fetchone()["c"]
            if not accepted:
                raise ConflictError("尚无可凭以申请的受理材料（现场核验可后补）")
            # 阶段上限
            committed = self.conn.execute(
                "SELECT COALESCE(SUM(amount),'0') AS s FROM payment_orders "
                "WHERE milestone_id=? AND status NOT IN ('rejected','cancelled')",
                (milestone_id,),
            ).fetchone()["s"]
            if D(committed) + amount > D(m["planned_amount"]):
                raise ConflictError(
                    f"超出阶段计划金额：已申请/在途 {committed}，本次 {amount}，"
                    f"上限 {m['planned_amount']}"
                )
            # 项目可申请预算
            budget = self._balance(project_id, "budget")
            if amount > budget:
                raise ConflictError(f"超出项目可申请预算：预算余额 {budget}，本次申请 {amount}")
            oid = _uid("pay")
            self.conn.execute(
                "INSERT INTO payment_orders (id,project_id,milestone_id,verification_id,amount,"
                "currency,status,requested_by,created_at,updated_at) "
                "VALUES (?,?,?,?,?,?, 'requested',?,?,?)",
                (oid, project_id, milestone_id, None, str(amount),
                 p["currency"], user["id"], self._now(), self._now()),
            )
            post(
                self.conn, at=self._now(), actor_id=user["id"], project_id=project_id,
                entity="payment", entity_id=oid, action="payment_requested",
                legs=[("budget", -amount), ("requested", amount)],
                memo=f"申请支付：{m['title']} {amount} {p['currency']}（待核验/审批）",
                payment_id=oid,
                extra_payload={"milestone_id": milestone_id, "note": note},
            )
        return self.get_payment_order(oid)

    def approve_payment(self, actor: str, order_id: str, *, note: str = "") -> dict:
        """审核人批准。并发两个批准由写锁串行，第二个复查状态后得到 Conflict。"""
        user = self._require_role(actor, ROLE_REVIEWER)
        with transaction(self.conn):
            o = self._order(order_id)
            p = self._project(o["project_id"])
            # 锁内复查：只有第一个批准能看到 requested
            if o["status"] != "requested":
                raise ConflictError(f"支付指令状态为 {o['status']}，不能批准")
            if p["status"] != "active":
                raise ConflictError(f"项目 {p['status']}：不能批准，请先恢复")
            if o["requested_by"] == user["id"]:
                raise PermissionError("申请与批准不能为同一人")
            m = self._milestone(o["milestone_id"])
            v = self._latest_verification(m["id"])
            if v is None or v["result"] not in ("pass", "partial"):
                raise ConflictError("现场核验结论未到达或未通过：不能批准")
            if D(o["amount"]) > D(v["verified_amount"]):
                raise ConflictError(
                    f"核定金额 {v['verified_amount']} 低于申请额 {o['amount']}：不能全额批准"
                )
            amount = D(o["amount"])
            self.conn.execute(
                "UPDATE payment_orders SET status='approved', approved_by=?, verification_id=?, "
                "updated_at=? WHERE id=?",
                (user["id"], v["id"], self._now(), order_id),
            )
            post(
                self.conn, at=self._now(), actor_id=user["id"], project_id=p["id"],
                entity="payment", entity_id=order_id, action="payment_approved",
                legs=[("requested", -amount), ("payable", amount)],
                memo=f"批准支付 {amount} {o['currency']}，依据核验 {v['id']}（{v['result']}）",
                payment_id=order_id,
                extra_payload={"verification_id": v["id"], "note": note},
            )
        return self.get_payment_order(order_id)

    def reject_payment(self, actor: str, order_id: str, *, note: str = "") -> dict:
        user = self._require_role(actor, ROLE_REVIEWER)
        with transaction(self.conn):
            o = self._order(order_id)
            if o["status"] not in ("requested",):
                raise ConflictError(f"支付指令状态为 {o['status']}，不能否决")
            if o["requested_by"] == user["id"]:
                raise PermissionError("申请与否决不能为同一人")
            amount = D(o["amount"])
            self.conn.execute(
                "UPDATE payment_orders SET status='rejected', approved_by=?, updated_at=? WHERE id=?",
                (user["id"], self._now(), order_id),
            )
            post(
                self.conn, at=self._now(), actor_id=user["id"], project_id=o["project_id"],
                entity="payment", entity_id=order_id, action="payment_rejected",
                legs=[("requested", -amount), ("budget", amount)],
                memo=f"否决支付，额度退回可申请预算。{note}",
                payment_id=order_id, extra_payload={"note": note},
            )
        return self.get_payment_order(order_id)

    def freeze_payment(self, actor: str, order_id: str, *, reason: str) -> dict:
        user = self._require_role(actor, ROLE_OFFICER, ROLE_REVIEWER)
        with transaction(self.conn):
            o = self._order(order_id)
            if o["status"] not in ("requested", "approved"):
                raise ConflictError(f"支付指令状态为 {o['status']}，不能冻结")
            amount = D(o["amount"])
            src = "payable" if o["status"] == "approved" else "requested"
            self.conn.execute(
                "UPDATE payment_orders SET status='frozen', frozen_reason=?, updated_at=? WHERE id=?",
                (reason, self._now(), order_id),
            )
            post(
                self.conn, at=self._now(), actor_id=user["id"], project_id=o["project_id"],
                entity="payment", entity_id=order_id, action="payment_frozen",
                legs=[(src, -amount), ("frozen", amount)],
                memo=f"冻结支付 {amount}：{reason}",
                payment_id=order_id, extra_payload={"reason": reason, "from_account": src},
            )
        return self.get_payment_order(order_id)

    def resume_payment(self, actor: str, order_id: str, *, note: str = "") -> dict:
        """恢复冻结款项：统一回到 requested，由审核人按**当前**计划重新批准。"""
        self._require_role(actor, ROLE_OFFICER)
        with transaction(self.conn):
            o = self._order(order_id)
            p = self._project(o["project_id"])
            if o["status"] != "frozen":
                raise ConflictError(f"支付指令状态为 {o['status']}，无需恢复")
            if p["status"] != "active":
                raise ConflictError("项目仍处暂停状态，需先恢复项目")
            amount = D(o["amount"])
            self.conn.execute(
                "UPDATE payment_orders SET status='requested', frozen_reason=NULL, "
                "updated_at=? WHERE id=?",
                (self._now(), order_id),
            )
            post(
                self.conn, at=self._now(), actor_id=self._user(actor)["id"], project_id=p["id"],
                entity="payment", entity_id=order_id, action="payment_resumed",
                legs=[("frozen", -amount), ("requested", amount)],
                memo=f"恢复支付申请，须按当前计划（修订 {p['plan_revision']}）重新审批。{note}",
                payment_id=order_id, extra_payload={"note": note},
            )
        return self.get_payment_order(order_id)

    def cancel_payment(self, actor: str, order_id: str, *, note: str = "") -> dict:
        """取消在途/冻结指令，额度退回预算；已支付的不可取消。"""
        self._require_role(actor, ROLE_OFFICER, ROLE_REVIEWER)
        with transaction(self.conn):
            o = self._order(order_id)
            if o["status"] in ("disbursed", "reconciled", "cancelled", "rejected"):
                raise ConflictError(f"支付指令状态为 {o['status']}，不能取消")
            amount = D(o["amount"])
            src = {"requested": "requested", "approved": "payable", "frozen": "frozen"}[o["status"]]
            self.conn.execute(
                "UPDATE payment_orders SET status='cancelled', frozen_reason=NULL, updated_at=? "
                "WHERE id=?", (self._now(), order_id),
            )
            post(
                self.conn, at=self._now(), actor_id=self._user(actor)["id"],
                project_id=o["project_id"],
                entity="payment", entity_id=order_id, action="payment_cancelled",
                legs=[(src, -amount), ("budget", amount)],
                memo=f"取消支付，额度退回预算。{note}",
                payment_id=order_id, extra_payload={"note": note},
            )
        return self.get_payment_order(order_id)

    def record_receipt(
        self, actor: str, order_id: str, *, receipt_no: str, amount,
        currency: str | None = None, note: str = "",
    ) -> dict:
        """财务登记放款回执（财务凭证）。

        receipt_no 全局唯一：重复提交同一回执号直接返回原记录，不重复放款（幂等）。
        回执可晚于批准很久到达；放款金额以回执为准。
        """
        user = self._require_role(actor, ROLE_FINANCE)
        amount = _amount(amount)
        with transaction(self.conn):
            # 幂等检查 1：回执号已存在 -> 原样返回，绝不二次过账
            existing = self.conn.execute(
                "SELECT * FROM payment_receipts WHERE receipt_no=?", (receipt_no,),
            ).fetchone()
            if existing is not None:
                if existing["payment_order_id"] != order_id or D(existing["amount"]) != amount:
                    raise ConflictError(
                        f"回执号 {receipt_no} 已用于另一笔/金额不符，拒绝冒充重复提交"
                    )
                log_event(
                    self.conn, at=self._now(), actor_id=user["id"],
                    entity="payment", entity_id=order_id, action="receipt_replayed_idempotent",
                    payload={"receipt_no": receipt_no},
                )
                return self.get_payment_order(order_id)

            o = self._order(order_id)
            p = self._project(o["project_id"])
            if currency and currency != o["currency"]:
                raise ValidationError("回执币种与指令币种不一致")
            if amount != D(o["amount"]):
                raise ConflictError(f"回执金额 {amount} 与批准金额 {o['amount']} 不符")
            if o["status"] != "approved":
                raise ConflictError(f"支付指令状态为 {o['status']}，只有已批准指令可放款")
            # 幂等检查 2：同一指令不得用不同回执号放款两次
            other = self.conn.execute(
                "SELECT receipt_no FROM payment_receipts WHERE payment_order_id=?", (order_id,),
            ).fetchone()
            if other is not None:
                raise ConflictError(f"该指令已有回执 {other['receipt_no']}，禁止重复放款")
            now = self._now()
            rid = _uid("rcpt")
            try:
                self.conn.execute(
                    "INSERT INTO payment_receipts (id,receipt_no,payment_order_id,amount,currency,"
                    "recorded_by,disbursed_at,created_at) VALUES (?,?,?,?,?,?,?,?)",
                    (rid, receipt_no, order_id, str(amount), o["currency"],
                     user["id"], now, now),
                )
            except sqlite3.IntegrityError as exc:
                # 并发下两个线程同时穿过上面的预检：UNIQUE 约束是最终防线。
                # 输家按幂等重放处理，而不是二次放款或报 500。
                existing = self.conn.execute(
                    "SELECT * FROM payment_receipts WHERE receipt_no=?", (receipt_no,),
                ).fetchone()
                if existing is None:
                    raise ConflictError("回执登记冲突，请重试") from exc
                if existing["payment_order_id"] != order_id or D(existing["amount"]) != amount:
                    raise ConflictError(
                        f"回执号 {receipt_no} 已用于另一笔/金额不符，拒绝冒充重复提交"
                    ) from exc
                log_event(
                    self.conn, at=now, actor_id=user["id"],
                    entity="payment", entity_id=order_id, action="receipt_replayed_idempotent",
                    payload={"receipt_no": receipt_no, "race": True},
                )
                return self.get_payment_order(order_id)
            self.conn.execute(
                "UPDATE payment_orders SET status='disbursed', updated_at=? WHERE id=?",
                (now, order_id),
            )
            post(
                self.conn, at=now, actor_id=user["id"], project_id=p["id"],
                entity="payment", entity_id=order_id, action="payment_disbursed",
                legs=[("payable", -amount), ("disbursed", amount)],
                memo=f"放款回执 {receipt_no}：{amount} {o['currency']}",
                payment_id=order_id,
                extra_payload={"receipt_no": receipt_no, "receipt_id": rid, "note": note},
            )
        return self.get_payment_order(order_id)

    def reconcile_payment(self, actor: str, order_id: str, *, note: str = "") -> dict:
        """财务核销：放款 -> 已核销。"""
        user = self._require_role(actor, ROLE_FINANCE)
        with transaction(self.conn):
            o = self._order(order_id)
            if o["status"] != "disbursed":
                raise ConflictError(f"支付指令状态为 {o['status']}，只有已放款可核销")
            amount = D(o["amount"])
            self.conn.execute(
                "UPDATE payment_orders SET status='reconciled', updated_at=? WHERE id=?",
                (self._now(), order_id),
            )
            post(
                self.conn, at=self._now(), actor_id=user["id"], project_id=o["project_id"],
                entity="payment", entity_id=order_id, action="payment_reconciled",
                legs=[("disbursed", -amount), ("reconciled", amount)],
                memo=f"核销 {amount} {o['currency']}。{note}",
                payment_id=order_id, extra_payload={"note": note},
            )
        return self.get_payment_order(order_id)

    # ------------------------------------------------------- 暂停 / 灾情改址
    def suspend_project(self, actor: str, project_id: str, *, reason: str) -> dict:
        """暂停：所有在途（requested/approved）款项连带冻结，项目停止接受新申请。"""
        user = self._require_role(actor, ROLE_OFFICER)
        with transaction(self.conn):
            p = self._project(project_id)
            if p["status"] != "active":
                raise ConflictError(f"项目已处于 {p['status']}")
            now = self._now()
            self.conn.execute(
                "UPDATE projects SET status='suspended' WHERE id=?", (project_id,),
            )
            orders = self.conn.execute(
                "SELECT * FROM payment_orders WHERE project_id=? AND status IN ('requested','approved')",
                (project_id,),
            ).fetchall()
            for o in orders:
                amount = D(o["amount"])
                src = "payable" if o["status"] == "approved" else "requested"
                self.conn.execute(
                    "UPDATE payment_orders SET status='frozen', frozen_reason=?, updated_at=? "
                    "WHERE id=?", (f"project_suspended: {reason}", now, o["id"]),
                )
                post(
                    self.conn, at=now, actor_id=user["id"], project_id=project_id,
                    entity="payment", entity_id=o["id"], action="payment_frozen",
                    legs=[(src, -amount), ("frozen", amount)],
                    memo=f"项目暂停连带冻结：{reason}",
                    payment_id=o["id"], extra_payload={"reason": reason, "cascade": True},
                )
            for m in self.conn.execute(
                "SELECT id FROM milestones WHERE project_id=? AND state IN "
                "('evidence_submitted','rework_requested','verified')", (project_id,),
            ).fetchall():
                self.conn.execute(
                    "UPDATE milestones SET state='frozen', frozen_reason=?, updated_at=? WHERE id=?",
                    (f"project_suspended: {reason}", now, m["id"]),
                )
            log_event(
                self.conn, at=now, actor_id=user["id"],
                entity="project", entity_id=project_id, action="project_suspended",
                payload={"reason": reason, "frozen_orders": len(orders)},
            )
        return self.get_project(project_id)

    def relocate_by_disaster(
        self, actor: str, project_id: str, *, new_site: str, new_total_amount,
        reason: str = "natural_disaster_relocation", note: str = "",
    ) -> dict:
        """灾情改址：登记计划修订，写清原计划/新计划各自的资金责任。

        必须在暂停状态下办理；改址前已锁定（申请/批准/冻结/已付/已核销）的
        资金按**原计划**责任落账，新计划只承担剩余及调整额。
        """
        user = self._require_role(actor, ROLE_OFFICER)
        new_total = _amount(new_total_amount)
        with transaction(self.conn):
            p = self._project(project_id)
            if p["status"] != "suspended":
                raise ConflictError("灾情改址必须先暂停项目，冻结在途资金后再修订计划")
            old_total = D(p["total_amount"])
            budget = self._balance(project_id, "budget")
            # 原计划已承担 = 总额 - 仍可申请预算；新计划责任 = 新总额 - 旧责任
            old_responsibility = (old_total - budget).quantize(CENT)
            new_responsibility = (new_total - old_responsibility).quantize(CENT)
            if new_responsibility < 0:
                raise ValidationError(
                    f"新总额 {new_total} 低于原计划已承担责任 {old_responsibility}，"
                    "无法界定资金责任"
                )
            revision = p["plan_revision"] + 1
            aid = _uid("amnd")
            self.conn.execute(
                "INSERT INTO plan_amendments (id,project_id,revision,reason,old_site,new_site,"
                "old_total_amount,new_total_amount,old_plan_responsibility,"
                "new_plan_responsibility,created_by,created_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (aid, project_id, revision, reason, p["current_site"], new_site,
                 str(old_total), str(new_total), str(old_responsibility),
                 str(new_responsibility), user["id"], self._now()),
            )
            self.conn.execute(
                "UPDATE projects SET current_site=?, total_amount=?, plan_revision=? WHERE id=?",
                (new_site, str(new_total), revision, project_id),
            )
            # 预算与拨款来源按差额调整（可增可减），不动在途/冻结/已付各账户
            delta = new_total - old_total
            if delta != 0:
                post(
                    self.conn, at=self._now(), actor_id=user["id"], project_id=project_id,
                    entity="project", entity_id=project_id, action="plan_amended",
                    legs=[("budget", delta), ("appropriation", -delta)],
                    memo=(f"灾情改址计划修订 r{revision}：{p['current_site']} -> {new_site}；"
                          f"原计划责任 {old_responsibility}，新计划责任 {new_responsibility}"),
                    extra_payload={
                        "amendment_id": aid, "revision": revision, "delta": str(delta),
                        "old_site": p["current_site"], "new_site": new_site,
                        "old_plan_responsibility": str(old_responsibility),
                        "new_plan_responsibility": str(new_responsibility),
                        "reason": reason, "note": note,
                    },
                )
            else:
                log_event(
                    self.conn, at=self._now(), actor_id=user["id"],
                    entity="project", entity_id=project_id, action="plan_amended",
                    payload={"amendment_id": aid, "revision": revision,
                             "old_plan_responsibility": str(old_responsibility),
                             "new_plan_responsibility": str(new_responsibility)},
                )
        return self.get_amendment(project_id, revision)

    def resume_project(self, actor: str, project_id: str, *, note: str = "") -> dict:
        """项目恢复：项目开门，但被冻结的款项不自动恢复，须逐笔明确资金责任后办理。"""
        user = self._require_role(actor, ROLE_OFFICER)
        with transaction(self.conn):
            p = self._project(project_id)
            if p["status"] != "suspended":
                raise ConflictError(f"项目状态为 {p['status']}，无需恢复")
            self.conn.execute(
                "UPDATE projects SET status='active' WHERE id=?", (project_id,),
            )
            # 里程碑状态依据其证据/核验事实重算（frozen 只是暂停期的临时外观）
            for m in self.conn.execute(
                "SELECT id FROM milestones WHERE project_id=? AND state='frozen'", (project_id,),
            ).fetchall():
                self._recompute_milestone_state(m["id"])
            frozen = self.conn.execute(
                "SELECT COUNT(*) AS c FROM payment_orders WHERE project_id=? AND status='frozen'",
                (project_id,),
            ).fetchone()["c"]
            log_event(
                self.conn, at=self._now(), actor_id=user["id"],
                entity="project", entity_id=project_id, action="project_resumed",
                payload={"note": note, "still_frozen_orders": frozen},
            )
        return self.get_project(project_id)

    def _recompute_milestone_state(self, milestone_id: str) -> None:
        latest_ev = self.conn.execute(
            "SELECT id,status FROM evidence_versions WHERE milestone_id=? "
            "ORDER BY version DESC LIMIT 1", (milestone_id,),
        ).fetchone()
        if latest_ev is None:
            state = "planned"
        elif latest_ev["status"] == "rejected":
            state = "rework_requested"
        elif latest_ev["status"] == "submitted":
            state = "evidence_submitted"
        else:  # accepted（或 superseded 等）：看针对该版本的核验结论
            v = self.conn.execute(
                "SELECT result FROM verifications WHERE evidence_id=? "
                "ORDER BY created_at DESC, id DESC LIMIT 1", (latest_ev["id"],),
            ).fetchone()
            if v and v["result"] in ("pass", "partial"):
                state = "verified"
            elif v and v["result"] == "fail":
                state = "rework_requested"
            else:
                state = "evidence_submitted"
        self.conn.execute(
            "UPDATE milestones SET state=?, frozen_reason=NULL, updated_at=? WHERE id=?",
            (state, self._now(), milestone_id),
        )

    # --------------------------------------------------------------- 查询面
    def get_amendment(self, project_id: str, revision: int) -> dict:
        row = self.conn.execute(
            "SELECT * FROM plan_amendments WHERE project_id=? AND revision=?",
            (project_id, revision),
        ).fetchone()
        if row is None:
            raise NotFoundError(f"修订 r{revision} 不存在")
        return dict(row)

    def list_amendments(self, project_id: str) -> list[dict]:
        return [dict(r) for r in self.conn.execute(
            "SELECT * FROM plan_amendments WHERE project_id=? ORDER BY revision", (project_id,),
        ).fetchall()]

    def get_payment_order(self, order_id: str) -> dict:
        """一笔资金从批准到核销的完整依据链与状态变化。"""
        o = self._order(order_id)
        out = dict(o)
        m = self.conn.execute("SELECT * FROM milestones WHERE id=?", (o["milestone_id"],)).fetchone()
        out["milestone"] = {"id": m["id"], "sequence": m["sequence"], "title": m["title"],
                            "plan_revision": m["plan_revision"], "planned_amount": m["planned_amount"],
                            "state": m["state"]}
        if o["verification_id"]:
            v = self.conn.execute(
                "SELECT * FROM verifications WHERE id=?", (o["verification_id"],),
            ).fetchone()
            out["verification"] = dict(v) if v else None
        else:
            out["verification"] = None
        receipts = self.conn.execute(
            "SELECT * FROM payment_receipts WHERE payment_order_id=? ORDER BY created_at",
            (order_id,),
        ).fetchall()
        out["receipts"] = [dict(r) for r in receipts]
        out["events"] = list(reversed(self.timeline("payment", order_id)))
        return out

    def list_payment_orders(self, project_id: str) -> list[dict]:
        return [dict(r) for r in self.conn.execute(
            "SELECT * FROM payment_orders WHERE project_id=? ORDER BY created_at, id", (project_id,),
        ).fetchall()]

    def timeline(self, entity: str | None = None, entity_id: str | None = None,
                 project_id: str | None = None, limit: int = 200) -> list[dict]:
        sql = ("SELECT e.id,e.at,e.actor_id,u.name AS actor,u.role,e.entity,e.entity_id,"
               "e.action,e.payload FROM events e LEFT JOIN users u ON u.id=e.actor_id")
        where, params = [], []
        if entity and entity_id:
            where.append("e.entity=? AND e.entity_id=?")
            params.extend([entity, entity_id])
        if project_id:
            where.append(
                "(e.entity_id IN (SELECT id FROM payment_orders WHERE project_id=?) "
                "OR e.entity_id IN (SELECT id FROM milestones WHERE project_id=?) "
                "OR (e.entity='project' AND e.entity_id=?))"
            )
            params.extend([project_id, project_id, project_id])
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY e.id DESC LIMIT ?"
        params.append(limit)
        return [dict(r) for r in self.conn.execute(sql, params).fetchall()]

    def project_fund_trace(self, project_id: str) -> dict:
        """项目级资金视图：每个支付指令的依据链 + 总对账。"""
        self._project(project_id)
        orders = [
            self.get_payment_order(r["id"])
            for r in self.conn.execute(
                "SELECT id FROM payment_orders WHERE project_id=? ORDER BY created_at, id",
                (project_id,),
            ).fetchall()
        ]
        return {
            "project": self.get_project(project_id),
            "amendments": self.list_amendments(project_id),
            "payment_orders": orders,
            "reconciliation": reconciliation_report(self.conn, project_id),
        }

    def reconciliation(self, project_id: str) -> dict:
        self._project(project_id)
        return reconciliation_report(self.conn, project_id)
