# T07 develop 集成交付记录（2026-10-17）

状态：供审查的 Draft PR，不是部署批准。

## PR #252 首轮 CI 修复

- 旧 revision 02 原先读取实时 TurnModel，导致在额度表创建前声明 T07 外键。现冻结该 revision 的 Turn DDL，额度列/外键仍由 revision 52 创建。
- HTTP 测试收集不再导入会初始化数据库的运行时；选中测试启动后使用隔离环境生成的 DSN。
- MySQL 8.4 开启 binlog 时，非 SUPER 迁移账号创建触发器报 1419。仅对 CI/HTTP 临时 MySQL 启用 `log_bin_trust_function_creators`，应用继续使用普通账号。生产部署前应由 DBA 明确提供触发器迁移权限或配置此变量；应用不会自动扩大数据库权限。
- HTTP 启动器不再对永久 DDL 错误反复执行非事务迁移，以免把原始错误掩盖成重复建表。
- 新增回归先失败后修复；迁移与 CI 回归 21 项通过。真实 CI 结果需要后续单独记录。

## 基线与范围

- 分支：`codex/t07-reliability-develop`，从 `origin/develop` 的 `cbbfb6e` 创建。
- 隔离迁移原 T07 的 10 个提交；未带入原开发分支的 T06 / 文件传输祖先提交。
- 保留 develop 已有 Credits 额度服务、取消协议、邮件认证、知识本和运行时重置逻辑。
- T07 token reservation 与已有 Credits reservation 使用不同字段；持久 reporter 组合已有 reporter，不能互相覆盖或混用 reservation ID。
- T07 迁移接在 develop 的 `20261016_51_t06_phone_identity` 后，单一 head 为 revision 54。
- Compose 继续保持单 Worker、单执行槽，不宣称双 Worker 已验收。

## 本分支实际执行的检查

- 前端 `npm run typecheck`：通过。
- 前端 `npm run lint`：通过。
- 前端 `npm run build`：通过，有大 chunk 提醒。
- 前端 `npm test -- --exclude src/platform/http/api.integration.test.ts`：76 个文件、601 项通过；明确未执行真实 HTTP 集成文件。
- 后端最终扩大范围回归：310 项通过（8 个警告），覆盖额度、Gateway、Worker fencing/取消、模型调用、短信、负载脚本安全和迁移图/完整离线 SQL；使用 `.artifacts/pytest-focused-delivery` 独立临时目录。此前核心六文件的 133 项通过不是最终扩大回归计数。
- `ruff check configs core gateway server tests scripts migrations`、`compileall`、`git diff --check`：通过。
- 全量后端回归：1452 passed、61 failed、295 errors、149 skipped（341 秒），门禁为红。该轮未配置独立 basetemp，存在大量 Windows 共享 pytest 临时目录 WinError 5；运行期间又完成了最后两处定向修复，因此这不是最终提交的完整全量认证。独立目录下最终定向 310 项通过，不能替代全量重跑，也不能把其余失败一律归为旧问题或数据库不可用。

前端首次沙箱内执行因临时缓存 rename EPERM 没有运行测试；相同命令在沙箱外重跑，得到上述 601 项通过。

## 未验证 / 合并与部署前门槛

- 未在真实 MySQL 执行 revision 54、触发器、回滚、并发额度与 Worker 接管验收。
- 未完成真实 Redis 暂停、Worker 强杀、数据库延迟、取消竞态及课堂负载矩阵。
- 缺少本集成分支的真实旧版本负载 baseline/final/comparison，不得把本分支运行结果命名为修复前基线。
- 后台生命周期、副作用幂等和完整课堂观察仍需逐项审查与验收。
- 其余 T07 验证文档记录原隔离分支的历史验证，不能作为本 develop 集成分支的复跑结果。

本次交付目标是推送隔离的 T07 分支并创建面向 develop 的 Draft PR；不执行合并，不绕过审查或 CI。
