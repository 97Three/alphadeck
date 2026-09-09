---
name: platform-ui-product-design
description: Design, refactor, review, and polish AlphaDeck's Vue workbench, including research tasks, strategy, reports, operations, settings, tables, dialogs, charts, and responsive states.
---

# AlphaDeck 平台 UI 产品设计

主平台使用 Vue 3、Vite、TypeScript，页面位于 `web/src/views/`，路由位于 `web/src/router.ts`，共用样式位于 `web/src/style.css`。生成工作空间里的金融看板由 `dashboard-visualization` 负责。

## 设计方向

- 以研究中心和任务轨道组织信息：首页发起研究，左侧轨道显示进度，任务页展示结果、过程、证据与文件。
- 清爽、可信、可扫描。暖白工作区、克制的琥珀色主动作、明确的状态颜色；优先保留真实数据和可执行入口。
- 桌面与移动端使用同一信息层级。移动端把轨道改为可滚动的横向导航，表格仅在自身区域滚动。
- 同一视图明确主要动作；危险操作显示作用对象和环境，再要求一次确认。
- A 股红涨绿跌只用于行情语境，不借用到运行状态或权限提示。

## 工作流程

1. 读取目标 Vue 页面、相邻路由、Python API 返回结构和现有样式。不要凭旧 React 页面猜测接口。
2. 明确信息层级：任务入口、当前状态、关键数据、结果细节、次级诊断。
3. 复用 `web/src/lib/api.ts` 的同源请求和 `web/src/style.css` 的控件样式，必要时抽取 Vue 组件。
4. 覆盖 loading、empty、error、success、disabled、pending、长文本与权限缺失状态。
5. 用 `npm --prefix web run build` 检查类型和构建，随后在 375、768、1440px 检查页面布局与交互。
6. 记录真实限制和未完成的接口，不把占位 JSON 页当作功能迁移完成。

## 交互与可读性

- 标题和说明让用户立即知道“现在能做什么”。错误要说明原因并提供重试入口。
- 按钮、表单有可见标签、焦点态和忙碌态；图标按钮有 `aria-label`。
- 图表同时提供数值摘要与表格或列表，避免颜色成为唯一信号。
- 表格中的日期、状态、数值、来源对齐一致，长 ID 和路径允许换行或查看详情。
- 数据来自当前项目或 Python API。前端不伪造研究结果、补数进度或验收通过状态。

详细页面状态与验收约束见 [平台 UI 交付契约](references/platform-ui-contract.md)。
具体设计检查见 [AlphaDeck UI/UX 适配规则](references/ui-ux-pro-max-adapter.md)。
维护者可使用 [状态矩阵校验脚本](scripts/validate_state_matrix.py) 核对 loading、empty、error 等状态覆盖；脚本结果仍需浏览器验证。
