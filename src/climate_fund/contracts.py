"""项目阶段与资金申请的基本数据结构。"""

from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum


class MilestoneState(StrEnum):
    PLANNED = "planned"
    EVIDENCE_SUBMITTED = "evidence_submitted"
    VERIFIED = "verified"
    FROZEN = "frozen"


@dataclass(frozen=True)
class Milestone:
    project_id: str
    sequence: int
    state: MilestoneState
    plan_revision: int


@dataclass(frozen=True)
class PaymentReceipt:
    receipt_id: str
    project_id: str
    amount: Decimal
    currency: str
