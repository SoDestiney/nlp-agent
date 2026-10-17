# T07 复审修复与验证记录

日期：2026-10-17。状态：六项复审 P1 的历史修复与本轮开发侧补齐均有代码；T07 整体验收未完成，不可据此部署。

以下标注为此前日期的执行记录属于历史快照；当前代码与验证状态以“2026-10-17 开发侧补齐”及文末“未完成与未验证”为准。历史通过结果不代表本轮代码在真实环境复验通过。

## 2026-10-17 开发侧补齐

在 Docker/真实数据库验收之前，已继续实现以下开发内容：

- Alembic revision `20261016_51_t07` 新增 MySQL 全局 queued/running 与 per-user active 计数器及触发器；默认排队容量 200、用户 active 上限 3。接单、状态迁移、取消/完成和删除通过同一事务内的触发器维护；Worker 启动及周期任务执行计数 reconcile。
- Revision `20261016_52_t07` 新增模型额度账户、Turn Reservation、幂等 Usage Event；reservation 与 Turn/outbox 同事务写入，通过 Redis 任务传播 reservation ID；Reporter 将 Provider usage 按 operation ID 幂等入账；终态触发器释放预留，Worker 周期 reconcile 孤儿/漂移。
- 课堂消息 UI 展示排队位置和服务器等待时间，并在等待期间递增；重连后刷新服务端 Turn 历史，继续显示排队状态并可取消。
- 增加运行时观测接口与仓库指标，包括队列深度/最老等待、待投递 outbox、恢复 handover、额度预留/未知用量，以及同步/异步连接池占用和 Worker heartbeat/槽位。
- MySQL checkpoint 与 transcript 写入现在锁定并核对 Turn 状态、generation、worker owner 和有效 lease。Turn executor 完成或失去 lease 后撤销共享 fence；在 fenced Turn 内拒绝 `join=false` 的临时 Worker，避免它借父 Turn 写入。该限制是安全降级，不等于已实现独立持久 Worker 生命周期。
- 加入 Admission 错误码映射、并发/事务/重试与 quota reconcile 的真实数据库测试，以及 Worker 脱离父 Turn 和排队等待计时的回归用例；这些 MySQL/前端用例尚未在本轮运行。

本轮非 Docker 可执行验证：

- Ruff、Python `compileall`、`git diff --check`：通过。
- `tests/test_t07_admission_codes.py`、Worker 生命周期同步用例及 load safety 同步用例：**14 passed, 1 deselected**。另有无 DSN 的一轮选择性运行出现一个配置导入失败和两个临时目录 setup error，随后通过工作区临时目录和仅供导入的占位 DSN 重跑；不把首次失败计为通过。
- Alembic 图解析：唯一 head 为 `20261016_52_t07`。这不是执行迁移。
- 异步 Worker fence 测试在 Windows 测试运行器创建 asyncio event loop 时卡于系统 socketpair 初始化，已中断；不计作通过。前端测试/类型检查依赖在该隔离工作区不完整，未运行。
- 本轮未启动 Docker、MySQL、Redis、Provider 或真实课堂负载；未生成新的 baseline/final/comparison，也未修改 Compose 扩容配置。

## 后续推进：WebSocket 取消与真实基线

### 已修复并复测

此前偶发 `concurrent.futures.CancelledError` 已通过连续 WebSocket 连接/订阅失败/ping/断开复现。
根因是 transport 的 AnyIO level cancellation 可以打断连接清理；原实现先设置 `_closed`，
随后取消导致清理只完成一部分，再次 close 又因 `_closed` 提前返回。

修复采用连接自有的单一 `_close_task`；并发 close 共同等待该任务，调用者取消不取消清理。
端点在 accept、ready、receive 全阶段共用 finally，并用 AnyIO shield 保护清理。
`wait_closed()` 在资源清理完毕后通知。这里只吸收 ASGI 连接的取消，不吞掉 Turn/Worker 的取消。

回归 `tests/test_t07_websocket_shutdown.py`：

- 连续 100 次真实 TestClient WebSocket 订阅不存在会话、ping 后断开；修改前复现 CancelledError，修改后通过。
- 清理进行中取消调用者，第二次 close 必须等待原清理完成，且调用者仍收到取消。
- accept 阶段取消必须释放 Hub 名额；该测试也先复现失败再修复。

与既有 T07 定向回归合跑：**132 passed, 3 deselected**，三个明确排除项仍为真实 MySQL foundation 测试。
另执行 `pytest tests/test_web_api.py -q --tb=line`：**23 passed, 2 failed**。
失败分别为已确认旧问题 `test_learning_release_notes_route_requests_only_published`
（mock factory 缺 begin），及 `test_http_lifecycle_sessions_chat_settings_and_csrf`
（本次占位 DSN 无真实数据库，删除会话时观测库清理返回 503）。没有跳过它们或改断言。
本次尚未重跑真实 HTTP/MySQL、迁移与完整后端门禁；上文历史结果不可冒充本次结果。
独立只读审查未发现该限定修复的 P1/P2；未审查未验证的 Admission/负载草稿。

### 基线尝试与新环境阻塞

已准备隔离负载启动草稿 `tests/load/run_isolated.py`、真实 Worker/模型 entrypoint，
以及 Admission 并发/事务/429 回归草稿。它们**尚未完成真实验证，也没有 Admission 数据库实现**，
不属于已完成交付。冻结工作区仅复制 tests 工具；旧产品 core/gateway/server/configs/migrations 未修改。

冻结旧版本启动命令：

```powershell
python -m tests.load.run_isolated --run-kind baseline --output artifacts/load-tests/t07/baseline.json --concurrency 1 --replicas 1
```

该命令在 Docker 前置检查失败，没有进入 50/30 负载，因此没有生成 baseline/final/comparison。
当前 Docker Desktop 4.85.0 启动日志明确报告：

```text
starting services: initializing Inference manager:
listening on unix://C:/Users/桂林/AppData/Local/Docker/run/dockerInference:
The file cannot be accessed by the system.
```

`dockerInference` 是零长度 ReparsePoint；WSL `docker-desktop` 为 Stopped，
Docker API 无法响应。已尝试正常启动和 `docker desktop stop --timeout 20` 后重启，
正常停止也失败。未删除/重命名该运行时节点，未修改 Docker 全局设置，未工厂重置或清理卷。
已申请隔离权限尝试把该零长度节点重命名为备份，但系统返回“系统无法访问此文件”，没有发生文件变更；
未进一步删除 reparse point、重置 WSL 或修改 Docker Model Runner 全局设置。
本次真实 Admission 测试为 **3 个 setup errors（Docker info 超时）**，不是测试通过，也没有证明实现行为。

继续真实基线前需恢复 Docker 引擎；涉及运行目录节点或全局配置的修复需用户确认。
Compose 保持 1×1。容量/额度账本/后台生命周期/监控及扩容验收仍按下文未完成清单执行。

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

1. Admission 触发器、额度 Reservation/Usage ledger、触发器终态释放以及 pool/runtime 指标均尚未在真实 MySQL 执行迁移或集成验证；静态检查和单测不能证明 SQL 并发语义、触发器回滚或容量计数无漂移。
2. 后台 Worker 目前在 fenced Turn 中禁止脱离式运行，但尚无独立持久任务表、接单/lease/取消/终态及恢复链路。工具副作用也尚未全面绑定持久 operation ID 与 generation，因此不满足双 Worker 的 exactly-once 启用前提。
3. 指标提供当前 pool 使用量和配置 timeout，但尚未采集数据库连接等待时长/分位数；队列深度、最老任务、执行槽、恢复和额度泄漏指标也尚未接入生产监控告警。
4. 前端排队显示/取消/重连恢复已实现代码，但本轮没有可用完整的前端依赖目录，未运行类型检查、测试或 build。
5. 尚无冻结旧版本 50 登录/30 Turn 基线、同参 final/comparison、`1×1 → 1×4 → 2×4`、Worker 强杀、Redis pause、DB delay、慢短信、额度耗尽和 Provider 错误完整故障矩阵结果。
6. 本地 pytest 的异步用例受 Windows 事件循环 socketpair 初始化阻塞；真实数据库/HTTP 测试尚未运行。历史全量回归曾有 36 项在冻结版本复现的旧失败，另外存在过时记录的偶发项；需按当前代码重新采集全量结果。
7. 因上述实现/验证缺口，T07 仍不可部署或扩容。Compose 继续保持 1×1；不得把未运行的迁移、前端或真实负载验证描述为通过。

下一阶段应先完成权威 Admission/模型额度账本及 background 生命周期，再执行完整隔离故障验收；在此之前不扩容生产。
