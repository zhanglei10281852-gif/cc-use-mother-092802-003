# 气候合作资金 · 拨款管理后端

面向国际气候合作项目的拨款管理服务：让**项目协议、里程碑证据、核验结论、支付指令、
财务回执各自留痕**，并依据项目状态决定款项的可申请 / 可冻结 / 可恢复。

仅依赖 Python 3.11 标准库（`sqlite3`、`http.server`），无需安装第三方包。

## 核心规则

- **证据按版本管理**：每次补交都是新版本；被驳回（`rejected`）的版本永久保留，
  补交不会覆盖它，已定论版本不可再次决策。
- **材料与现场核验解耦到达**：材料受理即可申请款项；现场核验结论未到或不通过，
  一律不能批准；核验可给出 `partial` 核定额，申请额不得高于核定额。
- **支付指令状态机**：
  `requested → approved → disbursed → reconciled`，
  任一步可 `frozen`，冻结恢复后回到 `requested` 按**当前计划**重审；
  否决/取消把额度退回可申请预算。
- **回执幂等**：`receipt_no` 全局唯一（数据库 UNIQUE 为最终防线），重复提交
  同一回执返回原单据且不二次放款；同一指令不得用不同回执号放款两次。
- **三权分立**：经办人 officer（建协议、交材料、申请、暂停/恢复、改址）、
  审核人 reviewer（受理/驳回材料、现场核验、批准/否决）、财务 finance
  （放款回执、核销），服务层强校验，且申请与批准不得为同一人。
- **暂停与灾情改址**：暂停时所有在途款项连带冻结、停止新申请；改址必须在暂停中
  办理，登记计划修订（`plan_amendments`），明确：
  - 原计划责任 = 改址时已锁定（申请/批准/冻结/已付/核销）的资金；
  - 新计划责任 = 新协议总额 − 原计划责任。

  恢复项目只开门不放款，冻结款项须逐笔确认资金责任后恢复、重审。
- **可申请性由项目状态 + 计划版本 + 阶段上限 + 项目预算四重约束共同决定。**

## 留痕与对账（三轨）

1. **业务单据**：`payment_orders` / `payment_receipts` / `verifications` /
   `evidence_versions` / `plan_amendments`；
2. **只增事件流** `events`：每次状态变化追加一行，构成 API 时间线；
3. **复式分类账** `ledger_entries`：`appropriation / budget / requested /
   payable / frozen / disbursed / reconciled` 账户成对过账，借贷必平。

`GET /projects/{id}/reconciliation` 做三方对账：分类账 ↔ 业务单据 ↔ 事件流重算，
任一项不一致即 `balanced=false`。

## 并发控制

HTTP 为线程服务器，每个工作线程持有独立 SQLite 连接（WAL）；所有写操作为
`BEGIN IMMEDIATE` 事务，在锁内完成“读状态→校验→改状态→记账”。并发审批只有
一个成功，并发重复回执只放一次款，并发申请不会超预算。时钟通过 `Clock` 注入，
可由 `/admin/clock/advance` 推进。

## API（均为 JSON，写操作 POST；身份用 `X-User-Id` 头）

```
POST /users
POST /projects
GET  /projects/{id}
POST /projects/{id}/milestones
GET  /milestones/{id}
POST /milestones/{id}/evidence
POST /evidence/{id}/decision
POST /milestones/{id}/verifications
POST /projects/{id}/payments
GET  /projects/{id}/payments
GET  /payments/{id}                    # 资金依据链 + 状态变化时间线
POST /payments/{id}/approve|reject|freeze|resume|cancel
POST /payments/{id}/receipts           # 放款回执（幂等）
POST /payments/{id}/reconcile
POST /projects/{id}/suspend
POST /projects/{id}/relocate
POST /projects/{id}/resume
GET  /projects/{id}/amendments
GET  /projects/{id}/trace              # 每笔资金从批准到核销的依据 + 对账
GET  /projects/{id}/reconciliation
GET  /projects/{id}/events
POST /admin/clock/advance
```

## 运行

```bash
python3 -m climate_fund.api                       # 默认 127.0.0.1:8080，需设 PYTHONPATH=src
PYTHONPATH=src python3 -m climate_fund.api        # 显式方式
```

## 测试（23 个）

```bash
python3 -m unittest discover -s tests -v
python3 -m compileall -q src tests
```

覆盖：证据版本不可覆盖、核验晚到、部分支付、partial 核减、角色越权、
并发审批竞争、并发回执幂等、预算竞争、暂停/灾情改址的新旧计划资金责任，
以及“并发审批 + 部分支付 + 灾情改址”交织全程每步对账（`test_interleave.py`）、
真实 HTTP 端到端（`test_api.py`）。

## 代码结构

```
src/climate_fund/
  contracts.py   原始阶段/回执数据结构（保留）
  clock.py       可控时钟
  errors.py      领域错误（带 HTTP 状态码）
  database.py    建表脚本、连接与 IMMEDIATE 事务
  ledger.py      复式过账与三方对账
  services.py    领域服务（状态机/权限/幂等/改址）
  app.py         装配（按线程连接）
  api.py         HTTP API
tests/           helpers + 7 个测试模块
```
