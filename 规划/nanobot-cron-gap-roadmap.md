# Nanobot Cron 对比与 Emperor Agent 后续开发计划

日期：2026-05-20

## 阅读范围

本次重点阅读：

- `/Users/anhuike/Documents/workspace/nanobot/cron/jobs.json`
- `/Users/anhuike/Documents/workspace/nanobot/nanobot/cron/types.py`
- `/Users/anhuike/Documents/workspace/nanobot/nanobot/cron/service.py`
- `/Users/anhuike/Documents/workspace/nanobot/nanobot/agent/tools/cron.py`
- `/Users/anhuike/Documents/workspace/nanobot/nanobot/heartbeat/service.py`
- `/Users/anhuike/Documents/workspace/nanobot/nanobot/bus/events.py`
- `/Users/anhuike/Documents/workspace/nanobot/nanobot/bus/queue.py`
- `/Users/anhuike/Documents/workspace/nanobot/nanobot/session/manager.py`
- `/Users/anhuike/Documents/workspace/nanobot/nanobot/command/builtin.py`
- `/Users/anhuike/Documents/workspace/nanobot/tests/cron/*`

当前 `nanobot/cron/jobs.json` 只有一个系统任务示例：`dream`，每 2 小时触发一次 `system_event`，并记录 `nextRunAtMs`、`lastRunAtMs`、`lastStatus`、`lastError`、`runHistory` 等状态。真正有价值的不是这个 JSON 本身，而是 Nanobot 围绕它实现的持久化 Cron 子系统。

## 当前 Emperor Agent 已具备的基础

Emperor Agent 现在已经有较强的本地 Agent 基础设施：

- WebUI 流式 Chat、Runtime Event 冷记录、刷新后重放行为细节。
- Ask / Plan / Permissions 三模式控制。
- Agent Team 持久队友、inbox、thread、按消息唤醒。
- 双模型路由：主模型处理复杂任务，次模型处理轻量任务。
- `memory/history.jsonl` 热日志 + gzip 冷归档。
- Memory / Tokens / Model / Team / Skills 等正式 WebUI 页面。
- Web 后端已拆成 `agent/web/routes/*` 与 `agent/web/services/*`。

但它仍缺少一类很关键的能力：**本地长期自动运行的任务调度中枢**。现在的 Team wake 需要消息触发，Control/Plan 需要用户 turn 触发，Compactor 主要由上下文压力触发；系统还没有一个可管理、可恢复、可观察的 Cron/Scheduler 层。

## Nanobot 中值得吸收的模块

### 1. 持久 Cron Scheduler

Nanobot 的 `CronService` 支持三种 schedule：

- `at`：一次性时间点。
- `every`：固定间隔。
- `cron`：标准 cron 表达式，支持 IANA timezone。

每个 job 带：

- `id`、`name`、`enabled`
- `schedule`
- `payload`
- `state.nextRunAtMs`
- `state.lastRunAtMs`
- `state.lastStatus`
- `state.lastError`
- `state.runHistory`
- `createdAtMs`
- `updatedAtMs`
- `deleteAfterRun`

对 Emperor Agent 的价值：

- 让用户能说“每天 9 点检查 GitHub issue 并汇报”“30 分钟后提醒我看测试结果”“每 2 小时让 Team reviewer 巡检一次”。
- 能把 Memory compaction、Team wake、日报、周期巡检都纳入同一个调度视图。
- WebUI 可以有正式的 Automation/Scheduler 页面，而不是把定时行为藏在内部。

### 2. 文件级可靠性设计

Nanobot Cron 的持久化细节值得借鉴：

- `jobs.json` 使用临时文件 + `os.replace()` 原子写。
- 写入后可 `fsync` 文件与父目录，避免崩溃后文件截断。
- 发现 corrupt store 时不静默清空，而是移动为 `jobs.json.corrupt-<ts>` 并拒绝覆盖。
- 运行中如果磁盘 store 临时损坏，优先保留内存中最后一个健康 snapshot。
- `action.jsonl` 记录外部进程对 job 的 add/update/delete，运行服务定期 merge。
- `FileLock` 防止多实例写入互相踩踏。

对 Emperor Agent 的价值：

- 我们已经在 Team、Runtime、History 上做了原子写与 JSONL append，但还没有一个跨实例/运行中热更新的 Scheduler Store。
- 未来 WebUI、CLI、Agent 工具、后台服务都可能同时改 schedule，必须先把可靠存储模型打稳。

### 3. Agent 可调用的 Cron Tool

Nanobot 提供 `cron` tool，支持：

- `action=add/list/remove`
- `every_seconds`
- `cron_expr`
- `tz`
- `at`
- `deliver`
- `job_id`

工具 schema 刻意保持根级 `required=["action"]`，把 action-specific requirement 写进字段描述，并在 runtime 返回可行动错误，避免某些 provider 拒绝复杂 `oneOf/anyOf` schema。

对 Emperor Agent 的价值：

- Agent 不只被动执行用户任务，还能把用户意图转成长期任务。
- 可以作为 `ask_before_edit` 权限体系的一部分：创建长期任务属于高影响动作，默认需要 AskCard 确认。
- 可以让 Plan 模式输出“建议创建以下自动任务”，批准后落地。

### 4. 系统保护任务

Nanobot 的 `dream` 是 protected system job：

- 用户可以 list 查看。
- 不能 remove。
- list 时显示 purpose。
- 用于长期记忆整理。

对 Emperor Agent 的价值：

- 可把内部维护任务产品化展示：Memory compact、Runtime event rotation、Team stale recovery、Tokens ledger maintenance。
- 用户能看到系统“自己在维护什么”，但不会误删关键任务。

### 5. Heartbeat 文件驱动的主动任务检查

Nanobot 的 `HeartbeatService` 每隔一段时间读取 `HEARTBEAT.md`：

- Phase 1：让模型通过虚拟 `heartbeat` tool 判断 `skip/run`。
- Phase 2：只有发现 active tasks 才进入完整 Agent 执行。
- 输出还经过 deliverable 过滤和 evaluator，避免把内部推理或无意义结果推给用户。

对 Emperor Agent 的价值：

- 这比纯 Cron 更适合“持续关注但不一定每次执行”的任务。
- 可以变成 Emperor 的 `Watchlist` / `Awareness`：用户写下关注事项，系统定期判断是否需要行动。
- 可与 Team 结合：Heartbeat 发现任务后唤醒某个 teammate，而不是总让 Lead 亲自跑。

### 6. Channel / Session 解耦

Nanobot 有通用 `MessageBus`：

- `InboundMessage(channel, sender_id, chat_id, content, media, metadata, session_key_override)`
- `OutboundMessage(channel, chat_id, content, reply_to, media, metadata, buttons)`
- Channel 与 Agent Core 通过 async queue 解耦。

还有 `SessionManager`：

- 按 `channel:chat_id` 管理独立 session。
- 支持 proactive delivery 标记。
- 支持 session 文件容量上限、修复损坏 JSONL、原子保存、flush_all。

对 Emperor Agent 的价值：

- 当前 Emperor 主要围绕本地 WebUI 单上下文；未来若接入 Slack/Telegram/飞书/邮件，会需要 channel/session 抽象。
- Scheduler 的 `deliver` 需要明确投递到哪里，不能只广播到 WebUI。
- 即使暂不接社交频道，至少可以先抽象 `local_web` channel，为将来扩展留接口。

### 7. 命令层运维能力

Nanobot 的命令值得吸收：

- `/stop` 取消当前 session 的活跃任务与 subagent。
- `/restart` 进程内重启。
- `/status` 汇总模型、上下文、活跃任务、搜索用量。
- `/history [n]` 查看当前 session 历史。
- `/dream` 手动触发长期记忆整理。
- `/dream-log` 查看记忆改动 diff。
- `/dream-restore` 回滚记忆。

对 Emperor Agent 的价值：

- 我们已有 `/status`、`/mode`、`/plan`、`/skills` 等 WebUI 命令，但缺少统一命令路由、任务取消、手动维护命令和记忆版本命令。
- Runtime Event 已经有足够细节，下一步应该补“运维命令 + UI 面板”。

## Emperor Agent 当前缺失的有用功能模块

按价值排序：

1. `agent/scheduler/`：本地持久 Scheduler 核心，支持 `at/every/cron`、timezone、run history、enabled、manual run、protected job。
2. `SchedulerTool`：Agent 可创建、列出、暂停、恢复、删除、手动运行任务。
3. Scheduler WebUI 页面：任务列表、下一次运行、最近运行历史、错误、启停、手动运行。
4. Scheduler runtime events：`scheduler_job_update`、`scheduler_run_start`、`scheduler_run_done`、`scheduler_run_error`，支持刷新恢复。
5. Protected system jobs：Memory 维护、Runtime 冷记录治理、Token ledger maintenance、Team stale recovery。
6. Heartbeat / Watchlist：文件或 WebUI 管理的主动关注清单，定期判断是否需要执行。
7. Proactive delivery 抽象：把定时任务结果投递到 `local_web`，未来扩展到外部 channel。
8. Active task cancellation：统一取消当前 turn、子代理、Team wake、Scheduler run。
9. Session / conversation namespace：从单一本地上下文演进到多 session，为未来 channel 化准备。
10. Memory versioning / restore：对 `MEMORY.local.md`、`USER.local.md`、daily episodes 做内部版本记录与回滚。

## 不建议直接照搬的部分

不建议第一阶段照搬 Nanobot 的全部 channel 生态：

- Slack / Telegram / 飞书 / WhatsApp / 邮件等都很有价值，但会显著扩大配置、安全、投递失败、权限审批和 UI 复杂度。
- Emperor Agent 当前产品重心是本地 WebUI + 工程化 Agent，先把本地 Scheduler 做稳，再接 channel。

不建议直接复用 Nanobot 的 `dream` 命名：

- Emperor 已有自己的宫廷像素风与 memory 页面。
- 可以把 `dream` 抽象成 `Memory Maintenance` / `御史记忆巡检`，内部保留“长期记忆整理”的工程语义即可。

不建议让 cron job 直接无限递归创建 cron job：

- Nanobot 的 `CronTool` 已禁止在 cron execution context 内创建新 job，这条需要保留。
- Emperor 还应把它接入 `permissions`，默认 `ask_before_edit` 下创建长期任务需要审批。

## 建议开发路线

### Phase 1：Scheduler Core

新增后端模块：

```text
agent/scheduler/
├── models.py
├── store.py
├── service.py
├── tools.py
├── events.py
└── __init__.py
```

核心设计：

- `ScheduleKind = at | every | cron`
- `SchedulerPayload.kind = agent_turn | team_wake | system_event`
- `SchedulerJobState` 记录 next/last/status/error/runHistory。
- `memory/scheduler/jobs.json` 保存任务主表。
- `memory/scheduler/action.jsonl` 保存跨入口 action log。
- 所有 JSON 写入临时文件 + replace；重要写入可 fsync。
- corrupt store 不覆盖，备份为 `.corrupt-<ts>`。
- 服务启动时 recompute next run。
- 支持 `run_job(job_id, force=False)` 手动运行。

验收：

- 单测覆盖 add/list/update/remove/manual run/at/every/cron/timezone/corrupt store/action merge/run history。
- 启动 WebUI 不 500。
- 无任务时服务空跑稳定。

### Phase 2：Scheduler Tool 与权限接入

新增工具：

```text
scheduler(
  action: add | list | update | remove | pause | resume | run,
  name?,
  message?,
  every_seconds?,
  cron_expr?,
  tz?,
  at?,
  payload_kind?,
  target?,
  job_id?
)
```

权限策略：

- `ask_before_edit` 下：
  - `list` 直接允许。
  - `add/update/remove/run/resume` 默认 AskCard 审批。
  - system job 不能 remove。
- `plan` 下：
  - 只允许 `list`。
  - 创建任务必须通过 PlanCard 批准后执行。
- `auto` 下：
  - 允许创建和执行，但仍校验 schema、路径、安全边界。

验收：

- Agent 可根据自然语言创建一次性提醒和周期任务。
- 在 cron execution context 内调用 `scheduler(action=add)` 会返回明确错误。
- 审批拒绝后不会落地任务。

### Phase 3：WebUI Scheduler 页面

新增导航：

- `/scheduler` 或 `/automations`

页面布局：

- 左侧：任务列表，显示 name、enabled、kind、next run、last status。
- 中间：选中任务详情与运行历史。
- 右侧：操作区，pause/resume/run/remove/edit。

事件：

- `scheduler_job_update`
- `scheduler_run_start`
- `scheduler_run_done`
- `scheduler_run_error`

API：

- `GET /api/scheduler`
- `POST /api/scheduler/jobs`
- `PATCH /api/scheduler/jobs/{id}`
- `POST /api/scheduler/jobs/{id}/run`
- `POST /api/scheduler/jobs/{id}/pause`
- `POST /api/scheduler/jobs/{id}/resume`
- `DELETE /api/scheduler/jobs/{id}`

验收：

- 刷新后任务状态不丢。
- 运行历史可见。
- 长任务名、长错误、移动端不撑破布局。

### Phase 4：System Jobs

第一批系统任务：

- `memory-maintenance`：定期检查是否需要压缩/归档/整理 Memory。
- `runtime-maintenance`：定期统计 runtime events 大小，未来可做冷归档。
- `team-stale-recovery`：扫描 `.team` 中 stale working/offline 状态，给出恢复建议。
- `token-ledger-maintenance`：检查 token ledger 是否可读、是否有异常行。

规则：

- system job 可见。
- system job 不可删除，只能在设置中关闭对应功能。
- list 时展示 purpose 和 protected 标记。

### Phase 5：Heartbeat / Watchlist

新增：

```text
memory/watchlist.md
agent/watchlist/
├── models.py
├── service.py
├── tools.py
└── events.py
```

行为：

- 定期读取 watchlist。
- 先用次模型做 `skip/run` 决策。
- 只有 `run` 才进入完整 Agent turn 或 Team wake。
- 输出经过 deliverability filter，避免内部推理泄露。

WebUI：

- Memory 或 Scheduler 页面新增 Watchlist 区块。
- 支持编辑关注事项、查看上次检查结果、手动检查。

### Phase 6：Proactive Delivery 与 Session Namespace

先做最小本地 channel：

- `local_web`：定时任务结果写入 runtime event，并在 Chat 里作为 proactive message 显示。
- `session_key`：默认为当前 WebUI conversation，后续可扩展多 conversation。

以后再接：

- Slack / Telegram / 飞书 / 邮件。

不要在 Phase 6 前贸然加入外部 channel，否则权限、秘钥、失败重试和 UI 会一起膨胀。

### Phase 7：Memory Versioning / Restore

借鉴 Nanobot `GitStore`，但先不要依赖系统 Git 命令，可选：

- 轻量 snapshot store：`memory/versions/*.json`
- 或 dulwich/git-backed store。

对象：

- `memory/MEMORY.local.md`
- `templates/USER.local.md`
- daily episodes
- history index

能力：

- `/memory-log`
- `/memory-restore <id>`
- Memory 页面 diff 预览与恢复。

## 推荐优先级

第一优先级：

1. Scheduler Core
2. Scheduler Tool + Permissions
3. Scheduler WebUI

第二优先级：

4. System Jobs
5. Heartbeat / Watchlist

第三优先级：

6. Proactive Delivery / Session Namespace
7. Memory Versioning / Restore

## 与当前架构的落点

建议映射：

- `agent/scheduler/service.py` 对齐 `agent/team/manager.py` 的长期子系统风格。
- `agent/scheduler/store.py` 对齐 `agent/team/store.py` 与 `agent/memory_history.py` 的原子写习惯。
- `agent/scheduler/events.py` 对齐 `agent/runtime/events.py`，不要在 service 里手写 WebSocket payload。
- `agent/web/services/scheduler_service.py` 承接 HTTP API。
- `agent/web/routes/scheduler.py` 注册路由。
- `webui/src/views/SchedulerView.vue` + `components/panels/SchedulerPanel.vue` 承接 UI。
- `webui/src/runtime/events.ts`、`reducer.ts`、`useRuntime.ts` 增加 scheduler event replay。
- `AGENTS.md` 和 `README.md` 同步新增 Scheduler 章节。

## 结论

Nanobot Cron 给 Emperor Agent 最有价值的启发是：

```text
长期自主性 = 持久 Scheduler + 可观察运行历史 + 对话可管理工具 + 系统保护任务 + 主动 Heartbeat
```

Emperor Agent 现在已经有强执行与强可视化，但还缺“什么时候自动执行、执行后怎么追踪、失败后怎么恢复”的长期运行层。下一步不应先接一堆外部频道，而应先把本地 Scheduler 做成正式产品模块；它会自然成为 Agent Team、Memory、Tokens、Watchlist 和未来 Channel 的共同地基。
