# 气候合作资金

国际气候合作项目的拨款管理后端：项目协议、里程碑、阶段材料、核验结论、
支付指令与付款回执各自留痕，按项目状态控制款项可申请 / 可冻结 / 可恢复，
并提供从批准到核销的全链路资金轨迹 API。

## 领域模型

```
项目 projects (DRAFT → ACTIVE ⇄ SUSPENDED → CLOSED)
 └─ 计划版本 plan_revisions      初始计划 / 灾情改址新计划，记录承接、收回、新增出资
     └─ 里程碑 milestones        PLANNED → … → VERIFIED → RECONCILED（或 FROZEN/CANCELLED）
         ├─ 阶段材料 evidence_submissions   按版本追加，驳回版本永不覆盖
         ├─ 核验 verifications              FIELD / FINANCIAL 双通道，各自独立到达
         └─ 支付指令 payment_instructions   PENDING → APPROVED → PARTIALLY_PAID/PAID → RECONCILED
             └─ 付款回执 payment_receipts   (指令, 外部回执号) 唯一，重复报送幂等
账本 ledger_entries             ALLOCATION(+) / RECOVERY(−) / PAYMENT(−) / WRITEOFF(核销台账)
审计 audit_events               每次状态变化：谁、以什么角色、何时、做了什么
```

金额一律为整数（最小货币单位），避免浮点误差。

## 核心规则

- **权限分离**：经办人 OFFICER（立项/交材料/发起指令）、审核人 REVIEWER
  （启停项目/审材料/核验/批准/核销）、财务 FINANCE（登记回执）互相不能代替；
  四眼原则：经办与审核、提交与审核不得为同一人（ADMIN 也受此约束）。
- **材料版本**：补交产生新版本；被驳回版本永久保留、不可再审改；
  核验绑定具体材料版本，换新版本后须重新核验。
- **回执幂等**：同一指令下外部回执号唯一。重复报送返回原回执且不重复记账；
  同号不同额视为冲突。
- **状态闸门**：仅 ACTIVE 可申请/付款；SUSPENDED 时未付资金全部冻结，
  申请、付款、交材料均被拒绝。
- **暂停恢复**：恢复时生成新计划版本，冻结指令作废（已付部分仍归属原计划、
  照常核销）；`承接资金 + 收回出资方 = 原计划未拨付余额`，
  `新计划里程碑总额 = 承接资金 + 新增出资`，新旧计划资金责任完全闭合。
- **对账不变量**（`reconciliation` 交叉校验）：
  `资金池 = 拨款 − 收回 − 支付 = 在途承诺 + 冻结未付 + 可申请余额`；
  `已付未核销 = 支付 − 核销 ≥ 0`；`Σ回执 = Σ支付流水 = Σ指令已付`。

## 并发与事务

所有写操作在 `BEGIN IMMEDIATE` 短事务内完成；状态迁移用条件 UPDATE
（`WHERE status = …`）的受影响行数做乐观并发控制——并发审批只有一方生效，
并发回执不会超付；回执靠数据库唯一约束保证并发下依然幂等。
所有时间戳来自注入时钟（`ManualClock` 可在测试中随意拨动）。

## 运行

```bash
python3 -m unittest discover -s tests -v   # 测试（含并发、部分支付、灾情改址交织对账）
python3 -m compileall -q src tests         # 编译检查
python3 main.py                            # 端到端演示（暂停→改址恢复→核销→对平）
```

启动 HTTP API（仅标准库，调用人经 `X-Actor-Id` 请求头标识）：

```python
from climate_fund import Database, GrantService, SystemClock, serve
serve(GrantService(Database("fund.db"), SystemClock()), port=8080)
```

主要端点：

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/projects` · `/projects/{id}/activate` | 立项、生效（资金入池） |
| POST | `/projects/{id}/suspend` · `/resume` · `/close` | 暂停冻结、改址恢复、结项收回 |
| POST | `/milestones/{id}/evidence` · `/evidence/{id}/review` | 提交阶段材料、审核 |
| POST | `/milestones/{id}/verifications` | 登记现场/财务核验结论 |
| POST | `/milestones/{id}/instructions` · `/instructions/{id}/approve` | 发起、批准支付指令 |
| POST | `/instructions/{id}/receipts` · `/writeoff` | 登记回执（幂等）、核销 |
| GET | `/projects/{id}/availability` · `/reconciliation` · `/ledger` | 可申请/冻结视图、对账、流水 |
| GET | `/projects/{id}/fund-trail` · `/instructions/{id}/trail` | 全链路资金轨迹与依据 |
| GET | `/projects/{id}/audit` | 审计流 |

## 目录

```
src/climate_fund/
  contracts.py   基础数据结构（既有）
  clock.py       可控时钟
  db.py          SQLite 模式与事务助手
  errors.py      领域错误（映射 HTTP 状态码）
  service.py     核心业务服务
  api.py         HTTP API（标准库）
main.py          端到端演示
tests/           服务层与 API 测试
```
