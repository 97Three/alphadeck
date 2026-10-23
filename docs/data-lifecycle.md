# 数据生命周期与安全清理

本文是 AlphaDeck 本地数据保留、测试隔离、备份和清理的权威入口。删除前先判断数据所有权；
“时间较早”不等于“无价值”。生产环境禁止照搬本地清理 SQL。

## 数据分类

| 数据 | 权威位置 | 默认策略 |
| --- | --- | --- |
| 用户、权限、项目和任务 | PostgreSQL `public` | 保留；只通过应用 API 删除真实项目 |
| 行情、证券、财务和时序数据 | PostgreSQL `quant` / TimescaleDB | 保留；按数据源补数/修复，不作为测试垃圾清理 |
| 财报历史版本 | `quant.financial_report_versions` | 只追加真实观测；修订追加新版本，禁止更新、删除或清空；保留历史查询证据 |
| 生成 Workspace | `data/projects/<Project.id>` | 与 Project 同生命周期；创建先写 initializing 行并从隔离 staging 原子发布，删除前验证 canonical path，数据库删除成功后再清文件 |
| 配额与用量账本 | PostgreSQL quota/usage 表 | 保留审计；项目删除后允许引用被置空，不能伪造回收额度 |
| Memory/AKEP 使用回执 | PostgreSQL integration ledger | 按真实消费者审计保留；测试 Scope 随测试批次清理 |
| 构建缓存和临时报告 | `.next`、`tmp`、coverage 等 | 可重建；使用 `npm run clean:local` |

## 有限行情维护

本地行情维护先检查 `npm run check:market-freshness`，再运行 `npm run market:maintain:dry-run`。dry-run 只读取股票池数量，输出日历区间、行情区间、批次和最大行数，不写行情。默认上限为 300 只证券；大于上限的池直接拒绝。`--calendar-only --dry-run` 可单独查看日历范围。

补数窗口必须覆盖实际缺口。例如停更超过默认 14 天时，先明确扩大窗口，并在 dry-run 和执行时使用相同参数：

```bash
ALPHADECK_MARKET_HISTORY_LOOKBACK_DAYS=30 npm run market:maintain:dry-run
# 核实目标为本地开发库、范围与上限后执行
ALPHADECK_MARKET_HISTORY_LOOKBACK_DAYS=30 npm run market:maintain
```

`ALPHADECK_MARKET_MAINTENANCE_MAX_SYMBOLS` 和 `ALPHADECK_MARKET_MAINTENANCE_BATCH_SIZE` 共同限制批次数；不能整除时向下取整，避免越过证券上限。日历/行情窗口最多 366 天。未知命令行选项、非法数字、不同范围的正在运行任务，以及 partial/stopped/failed 结果均报错；不把部分完成视为维护成功。日志保留范围与任务 ID，明细在 ingestion job 中可查。超时只向本次创建的任务发停止请求，已有任务不会被取消。

维护当前覆盖 `daily/qfq`，不代表未复权历史价格、财报版本或完整 PIT 均已补齐。最后的新鲜度门禁只证明最新日的覆盖，不能证明区间无缺口。生产补数仍须遵循[发布 Skill](../.agents/skills/alphadeck-production-release/SKILL.md) 的独立范围审查与授权，不随代码发布自动执行。

## 测试隔离

财报版本数据库回归只允许显式传入 `MARKET_TEST_DATABASE_URL` 且数据库名以 `_test` 结尾；使用独立本地 Docker PostgreSQL 和临时存储。未配置时跳过，不能借用应用的 `DATABASE_URL`。测试结束只销毁该测试容器，不清理真实归档记录。

真实任务 E2E 的 Project ID 固定为 `project-e2e-<campaign>-<case>`，标题固定带
`[E2E <CAMPAIGN>/<CASE>]`。`npm run check:task-e2e` 会先验证任务抽屉和报告；完整 30 题全部
通过后自动调用 Project DELETE API 清理该批数据库记录、预览和 Workspace。部分运行或失败会保留，
便于同 campaign 重试。

```bash
# 人工复核通过后仍保留看板
npm run check:task-e2e -- --campaign=review01 --retain-projects

# 明确清理失败或部分批次
npm run check:task-e2e -- --campaign=review01 --only=C01,C02 --cleanup
```

不要把普通用户 Project 仅凭标题内容判断为测试数据；自动清理只接受严格的 campaign ID 前缀。

`npm run auth:verify` 每次生成独立的 `authz-e2e-<uuid>` 用户和两个项目，在空库也会验证跨项目授权。项目清理通过应用 DELETE API 完成；清理失败时保留测试用户与项目并返回失败。认证烟测保留审计记录与共享限流记录，仅退出自己创建的管理员浏览器会话，不修改管理员密码或撤销其他会话。

## 备份与清理顺序

1. 运行 `npm run db:doctor`，确认数据库和 migration 正常。
2. 用 `npm run db:backup:release` 生成可恢复备份，并在独立目录保存校验值。
3. 停止目标 Project 的生成和预览，再通过 Project API 删除。
4. 对认证过期数据运行 `npm run auth:cleanup -- --dry-run`，复核后再去掉 `--dry-run`。
5. Memory 用 `(tenant_id, subject_id)` Scope erasure；AKEP 用 revoke/erase 生命周期；ModelPort 的
   append-only 预算事件不得从 AlphaDeck 侧删除。
6. 复查项目数、任务抽屉、孤立 Workspace、数据库 readiness 和四平台健康检查。

`npm run clean:local` 会删除 `tmp`，所以需要长期保留的数据库备份不能放在 `tmp` 中。

## 禁止事项

- 不执行无筛选的 `TRUNCATE`，不删除 `quant` 行情表或 Timescale chunk。
- 不在数据库事务成功前删除 Workspace；否则会产生不可恢复的“有记录、无文件”状态。
- 不绕过配额结算删除 active reservation，不修改已结算 usage event。
- 不把 Memory、AKEP 或 ModelPort 的测试清理扩大到其他 tenant/project/environment。
- 不把本机 `.env.local`、API Key、原始用户证据写进清理报告。

跨平台作用域和回执所有权见[联合上下文与项目隔离](context-composition.md)，生产备份/恢复见
[发布运行手册](release-runbook.md)。
