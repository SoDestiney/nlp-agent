# T07 测试部署错误 1419 修复

失败运行：`39087367853`，PR #252 合并后创建 `trg_nlp_turns_capacity_ai` 时触发 MySQL 1419。CI 的临时数据库配置了触发器权限，但实际部署路径遗漏了对应准备。

## 修复

- 应用部署前，先验证已有 MySQL/Redis 的 Compose project、健康状态和数据卷。
- 仅 `nova-test` 在已有 MySQL 容器内通过 root 设置 `log_bin_trust_function_creators=1` 并校验。密码从容器环境传递给客户端，不出现在命令参数或输出中；不授予应用 SUPER 权限。
- `nova-prod` 只读检查；若 binlog 与权限配置不满足要求，停止迁移，交由 DBA 配置。
- revision 51 支持重跑已创建的容量表、已插入的容量行和已存在的触发器；保留队列容量等原配置，不删业务数据，不手动 stamp/跳过迁移。
- 迁移失败仍不会重建应用容器，不重建 MySQL/Redis，不删除数据卷。

## 恢复步骤

1. 合并本修复到 develop，使用新镜像和新部署脚本执行测试环境部署。
2. 不要直接重跑原失败 SHA：旧脚本没有权限准备，旧镜像也不能安全续跑 revision 51。
3. 部署将先准备测试 MySQL 权限，再从当前 Alembic checkpoint 续跑，成功后才更新应用。
4. 观察 `nova-migrate` 退出码、Web/Worker/Monitor 健康检查和实际课堂操作。

真实 MySQL 回归使用独立前缀影子表，注入触发器创建失败后连续重跑两次，核对计数、原配置和三个触发器。该回归不等于实际测试环境已经重新部署成功。
