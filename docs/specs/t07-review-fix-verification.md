# T07 复审修复与验证记录

日期：2026-10-16。状态：六项复审 P1 已修复；T07 整体验收未完成，不可据此部署。

## 隔离范围

- 实施分支：`codex/t07-reliability-fixes`。
- 实施工作区：`E:/Github/Pro_NLP/.codex-t07-reliability`。
- 冻结产品基点：`8e83a7296a534504f6f5ef4320046d734412142b`。
- 只读对照工作区：`E:/Github/Pro_NLP/.codex-t07-baseline`，detached HEAD 位于该基点。
- 原工作区的未提交文件没有被还原、暂存或混合提交。本工作区仅隔离 T07 相关文件。
- 未修改前端、RBAC、T06 入班业务或历史迁移；未 push、创建 PR 或部署。

## 六项 P1 对应实现

| 复审问题 | 修复与回归 |
|---|---|
| fencing 未覆盖写入 | `TurnWriteFence` 通过 task/to_thread 传递；SQL UPDATE/INSERT 条件校验 generation、owner、有效 lease 和状态；学习事务结束再校验。失去所有权先 revoke，再取消；不得补写失败或取消。 |
| Worker 整批等待与重复消息清理 | 等待 FIRST_COMPLETED 后补位；按空槽拉取；重复处理器共享原处理结果，不替换取消句柄；清理时比对 task 身份。生产启用 pending reclaim，瞬时失败不会永久搁置已接单任务。 |
| 同步连接池漏算 | Gateway、telemetry、memory、transcript 按进程与 URL/配置共享同步 engine；引用计数释放；显式 size、overflow、timeout。异步和同步池分别预算。 |
| handover 无法解码 | 添加 `GatewayEventType.TURN_HANDOVER`；真实 MySQL 插入恢复事件并从 HTTP 重放。 |
| 短信重发选旧码 | 最新有效 SMS 查询有确定排序；旧码转为 sms_history，不删除限流历史；消费后也保留历史；旧发送延迟完成不能取代新码。 |
| 压测误判通过 | 模式切换必须 200 且回显匹配；每个 Provider 场景必须 202 → failed、错误分类匹配、恰好一个终态、无序列缺口、Session 200；0 个终态和超时均失败。 |

补充修复：

- 终态与必要终态事件同事务提交，避免 HTTP 先读到终态、却缺失终态事件。
- `ensure_event` 锁定 Turn 后按类型定向查询；超过 2000 条事件后仍能去重，并发修复只产生一个事件。
- 取消与完成竞争时保留已提交终态；heartbeat 不得把完成状态改成取消。
- 已持久化取消由 heartbeat 兜底停止执行；过期 lease 不得由旧 heartbeat 复活。
- MySQL DATETIME 使用 naive UTC，避免 aware/naive 比较异常。
- 压测使用 `/auth/session` 轮换后的 CSRF，并在 ws-ticket 写请求中携带 CSRF。
- 长事件流分页读取，并允许旧版本终态事件短暂滞后；分页有截止时间。
- 幂等请求重试先返回既有 Turn，不能被当前 Worker 暂时离线挡住。
- in-process 开发模式发送终态也使用 `ensure_event`，避免与原子终态写入重复。
- baseline 同时校验本地冻结 checkout 和目标服务的产品 commit；旧版 health 只能由 test-only adapter 补元信息。

## 干净基点上的最小前置修复

隔离后暴露出三个基点本身不完整的问题，保留了最小修复，未引入对应功能扩展：

1. 已提交 Gateway 会传 `thinking_enabled`，但 TurnTask 不接受该字段：补任务字段与 codec 往返，执行器只向支持该参数的 engine 传递；没有混入模型配置或 thinking UI。
2. `MySQLSessionService.messages` 引用不存在的 transcript `created_at`：按已有 parent UUID 链排序；没有数据来源的 timestamp 返回 null，不伪造时间、不更改 schema。真实消息读取已覆盖。
3. checkpoint 集成测试使用未导入的 `uuid4`：改为已导入的 `uuid.uuid4`，保证 Ruff 与该真实数据库测试可执行。

## 数据库连接预算

以下只适用于当前 Compose 单 Web、双 Worker、单 monitor、单 sandbox-manager；增加进程/副本必须重算。

| 进程 | 异步池上限 | 共享同步池上限 | 总计 |
|---|---:|---:|---:|
| Web | 5+5 | 5+5 | 20 |
| 每个 Worker（runtime/checkpoint 两个异步 engine） | 2×(2+1) | 4+2 | 12 |
| Monitor | 2+1 | 1+1 | 5 |
| Sandbox manager | 2+2 | 3+3 | 10 |
| 双 Worker 拓扑合计 | | | 59 |
| 迁移/运维预留 | | | 10 |
| 含预留预算 | | | 69 / MySQL max_connections 100 |

pool timeout 为 5 秒。预算是理论上限核算，不是实际 pool-wait SLO 压测结果。
Compose 默认仍为 1 worker × 1 slot，4 槽与双副本仅供隔离环境按阶段验收。

## 已执行验证

使用根工作区已有 `.venv/Scripts/python.exe`，工作目录始终为实施工作区。
HTTP 测试由 `ManagedHttpEnvironment` 创建一次性 MySQL/Redis、执行 `alembic upgrade head`、预置账号并启动 Web/Worker；不访问真实模型或短信 Provider。测试结束只移除本次创建的临时容器。

### 定向回归

以下命令结果：**129 passed, 3 deselected**。这三个明确列出的真实数据库测试随后在临时 MySQL 中单独通过；没有使用 `-k "not mysql"`，文件名含 mysql 的 mock 测试均包含在本轮中。

```powershell
python -m pytest tests/test_t07_review_regressions.py tests/test_sms_resend_state.py tests/test_t07_load_safety.py tests/test_gateway_core.py tests/test_mysql_gateway_repository.py tests/test_mysql_foundation.py tests/test_deployment_settings.py tests/test_redis_transport.py tests/test_worker_fencing.py tests/test_turn_execution.py tests/test_turn_reliability.py tests/test_database_error_mapping.py tests/test_tencent_sms.py -q `
  --deselect=tests/test_mysql_foundation.py::test_alembic_foundation_schema_is_available_in_mysql `
  --deselect=tests/test_mysql_foundation.py::test_mysql_table_comments_are_applied `
  --deselect=tests/test_mysql_foundation.py::test_session_factory_creates_explicit_transactions
```

`tests/test_mysql_foundation.py tests/test_mysql_langgraph_checkpointer.py` 在实际临时 MySQL 上：**14 passed**，包括迁移后表结构、注释、事务与 checkpoint 往返。

### HTTP/WebSocket

```powershell
$env:RUN_API_HTTP = '1'
# 此测试夹具拒绝调用者传入外部 DB/Redis 地址，必须让其自行创建临时容器。
python -m pytest tests/api_http/auth/test_registration_core.py tests/api_http/auth/test_session_lifecycle_core.py tests/api_http/chat/test_chat_worker_async_core.py tests/api_http/chat/test_t07_review_core.py tests/api_http/websocket/test_websocket_async_core.py -q
```

覆盖实际注册与短信重发、会话、运行中取消、Worker 异常、旧 generation SQL 拒绝、handover HTTP 重放、2100 条流事件后并发终态去重、WebSocket 认证与权限重连。
最终执行结果：**27 passed**。

### 静态与迁移图

- `python -m ruff check configs core gateway server tests scripts migrations`：通过。
- `python -m compileall -q configs core gateway server tests/load`：通过。
- `python -m alembic heads`：唯一 head 为 `20261016_50_t06`。
- `tests/test_migration_graph.py`：通过；实际临时 MySQL 已执行迁移至 head。
- `git diff --check`：通过。
- 本轮未修改前端，未运行前端类型检查/测试；全项目验收仍需补跑。

## 全量回归与旧版本对照

首轮占位 DSN 下的 `pytest -q` 出现大量数据库连接失败，不能作为有效集成结果。
随后在真实临时 MySQL 上重新执行完整 `pytest -q`；最终计数和失败清单保存在
`.artifacts/pytest-all-mysql-final.xml`。HTTP 单独结果在 `.artifacts/pytest-http-final.xml`。

全量测试中的 36 个失败已在冻结旧版本、另一套临时 MySQL 中逐一复现，均为相同原因：

最终全量快照：**910 passed, 37 failed, 121 skipped**。除下述 36 项外，
`test_web_api.py::test_websocket_reports_deleted_session_subscription_without_crashing`
本轮全量中偶发 `concurrent.futures.CancelledError`；前一轮全量及随后的单独重跑通过。
该时序失败没有被忽略或标为通过，仍需定位，不计入“环境阻塞”。
冻结旧版本的该单例连续重跑 8 次通过，未获得足以认定根因的证据；不能把它也归为已确认的旧失败。

| 测试 | 数量 | 原因 |
|---|---:|---|
| `test_feedback_api.py` 全部测试 | 29 | mock session factory 是普通 function，没有生产使用的 begin 接口 |
| `test_memory_runtime.py` 两项 | 2 | fixture 的 alice 并非数据库用户，违反 storage_accounts 外键 |
| `test_mysql_master_data.py::test_turn_event_service_delegates_serial_sequence_to_locked_turn_crud` | 1 | quota 查询返回 coroutine，mock 的结果不可迭代 |
| `test_storage_quota_integration.py::test_reconcile_counts_legacy_messages_and_tool_calls` | 1 | fixture 插入消息时 conversation 父记录尚不存在 |
| `test_uploads.py::test_database_authenticated_upload_uses_database_session_dependencies` | 1 | FakeAuthorizationSession 没有 begin 接口 |
| `test_user_management_v3.py::test_password_change_revokes_database_sessions` | 1 | 旧断言仅允许 invalid/expired，实际语义为 authorization changed |
| `test_web_api.py::test_learning_release_notes_route_requests_only_published` | 1 | mock session factory 缺 begin |

对照证据在 `.artifacts/pytest-baseline-existing-failures.xml`（36 failed）；这只是测试缺陷对照，**不是旧版本 50/30 负载 baseline**。
没有删除、跳过或放宽这些失败测试。它们仍使全量 gate 为红；本轮不能宣称全量通过。

## 未完成与未验证

1. 未实现 MySQL 权威队列容量、每用户并发限制及 reconcile。
2. 未实现模型 Reservation/Usage Ledger、跨任务传播及所有终态的结算释放。storage quota 不等于模型额度。
3. detached background worker resume 尚未有独立权威接单/lease 生命周期；不能绕过当前 fence 在完成的父 Turn 上继续写入。
4. checkpoint、transcript、工具副作用的完整 generation/幂等审计仍需补齐，不能据事件/终态 fencing 声称所有副作用 exactly-once。
5. 未补排队 ahead/waited UI 和完整 DB pool/queue/slot 指标。
6. 尚无旧版本 50 登录/30 Turn 基线、同参 final/comparison、1×1 → 1×4 → 2×4、强杀/Redis pause/DB delay/慢短信/额度耗尽全矩阵结果。
7. Docker 已可用，不能把以上缺口归因于环境阻塞；它们分别属于未实现、未执行验收和已复现的原有测试问题。

下一阶段应先完成权威 Admission/模型额度账本及 background 生命周期，再执行完整隔离故障验收；在此之前不扩容生产。
