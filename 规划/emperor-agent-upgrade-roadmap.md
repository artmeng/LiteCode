# Emperor Agent 升级路线图

> 基于现状（`agent/` 共 2449 行，CLI + WebUI 双入口，单用户本地工作台）
> 不照搬 nanobot，只挑对**御前对话**这种产品形态真正有价值的改造
> 版本：2026-05-08（Week 1 已落地）

---

## 进度总览

| 项 | 状态 | 落地版本 | 备注 |
|---|---|---|---|
| §1.1 上下文治理（cap + shrink） | ✅ 已合并 | `2cad8c8` | runner.py 新增 `_cap_tool_result` / `_shrink_old_tool_results`；常量 `_TOOL_RESULT_BUDGET=8000` / `_SHRINK_KEEP_RECENT=10` / `_SHRINK_MIN_BYTES=1500` |
| §1.3 LLM 错误恢复（空响应 + 截断重试） | ✅ 已合并 | `2cad8c8` | `step_async` 加 `empty_retries`(≤2) / `length_retries`(≤3)；providers/base.py 暴露 `is_truncated()` + `TRUNCATED_FINISH_REASONS`；**未新增** `LLMResponse.truncated` 字段（探查发现两个 provider 已透传 `finish_reason`） |
| §1.2 Checkpoint 中断恢复 | ✅ 已合并 | 待 commit | `MemoryStore.write/read/clear_checkpoint` → `memory/_checkpoint.json`；runner 在 turn 进入时和每个 tool 批次完成后写、turn 落地或达到 max_turns 时清；`AgentLoop` 启动优先恢复 checkpoint，再 fallback 到 unarchived。memory_store=None 的子代理 runner 自动跳过。 |
| §2.1 Subagent 取消按钮 | ⏸ 待办 | — | |
| §2.2 Runtime Context 注入 | ⏸ 待办 | — | |
| §2.3 记忆瘦身（半自动） | ⏸ 待办 | — | |
| §2.4 工具结果智能摘要 | ⏸ 待办 | — | 注：1.1 的硬截断已可用，2.4 是优化版 |
| §3.x | 🔵 不主动做 | — | 需求驱动 |

---

## 项目定位回顾

- **不是**多渠道客服机器人 / 多用户 SaaS — 不需要 Slack、MessageBus、跨会话信号量
- **是**一个跑在自己机器上、CLI 和 WebUI 共享同一份 `AgentLoop` 的本地代理
- **强项**：御前对话品牌叙事、模型条目化（per-entry apiKey）、皇家纸质 UI
- **当前痛点**（根据代码、不是脑补）：长对话容易撞 token 上限、子代理阻塞主代理、中断会丢 turn、记忆只增不减

下面所有建议都围绕"让单用户的长程对话更稳、更顺、更耐用"。

---

## 一、必做（高 ROI，每项 1-2 天）

### 1.1 上下文治理：在 runner.py 现有 `_pair_tool_calls` 后接两步 ✅ 已落地（commit `2cad8c8`）

> **实施摘要**：`agent/runner.py` 在 `_pair_tool_calls` 之后新增两个 staticmethod，`_ask_model` 按 `_pair_tool_calls → _cap_tool_result → _shrink_old_tool_results` 顺序治理；模块级常量 `_TOOL_RESULT_BUDGET=8000` / `_TOOL_RESULT_HEAD=7800` / `_TOOL_RESULT_TAIL=200` / `_SHRINK_KEEP_RECENT=10` / `_SHRINK_MIN_BYTES=1500`。离线验证：15×50KB 工具消息 cap 后单条 ≤8.5KB，shrink 后 5 条变 `[shrunk] name → N chars omitted` 摘要、最近 10 条保留原文。
>
> **下一阶段 §2.4 会做的优化**：让每个工具自己负责"语义友好的摘要"（比如 read_file 保留前 200 / 后 50 行 + `[skipped N lines]`），替代当前的字节级硬截断。

现在 `runner._ask_model`（`runner.py:148`）只做了"配对 tool_calls"。一个 `read_file` 把 30KB 塞进历史，再过几轮就把上下文撑爆。

**改造方案**（不照搬 nanobot 5 步，只挑 2 步刀刀见血的）：

```python
# runner.py 新增方法
def _shrink_old_tool_results(self, history, keep_recent=10):
    """把 keep_recent 之外的大体积工具结果替换为一行摘要。"""
    out = []
    cutoff = len(history) - keep_recent
    for i, msg in enumerate(history):
        if msg.get("role") == "tool" and i < cutoff and len(str(msg.get("content", ""))) > 1500:
            name = msg.get("name", "tool")
            size = len(str(msg["content"]))
            out.append({**msg, "content": f"[shrunk] {name} → {size} chars omitted"})
        else:
            out.append(msg)
    return out

def _cap_tool_result(self, history, per_call_limit=8000):
    """单条工具结果硬截断，留头尾。"""
    out = []
    for msg in history:
        if msg.get("role") == "tool":
            text = str(msg.get("content", ""))
            if len(text) > per_call_limit:
                head = text[: per_call_limit - 200]
                tail = text[-200:]
                text = f"{head}\n...[truncated, total {len(text)} chars]...\n{tail}"
                msg = {**msg, "content": text}
        out.append(msg)
    return out
```

集成位置：`_ask_model` 的 messages 拼接处，按 `_pair_tool_calls → _cap_tool_result → _shrink_old_tool_results` 顺序。

**预期收益**：长对话从"跑 8-10 轮就撞 max_context"提到"跑 30+ 轮稳定"。代码增量 ~80 行。

---

### 1.2 中断恢复：写一个最小 checkpoint ✅ 已落地

> **实施摘要**：
> - `agent/memory.py` 加 `write_checkpoint(history)` / `read_checkpoint() -> list | None` / `clear_checkpoint()`，落到 `memory/_checkpoint.json`（已被 `memory/` gitignore 覆盖）；写入用 `tmp + replace` 原子化，所有 IO 失败静默不影响主流程。
> - `agent/runner.py` 三处 hook：(1) `step_async` 入口写一次（防"用户消息已收但 LLM 未回"被杀）；(2) 每个 tool 批次 `history.extend(tool_messages)` 后写（此刻 tool_calls/tool 严格配对，是最佳一致点）；(3) turn 正常落地或撞 `max_turns` 时清。memory_store=None 的子代理 runner 自动跳过所有 checkpoint 调用。
> - `agent/loop.py` 启动顺序改为：先 `read_checkpoint()` → 若有则替换 `self.history` 并 `clear_checkpoint`；否则回退到原来的 `load_unarchived_history` + 启动压缩路径。`runner._pair_tool_calls` 在每次 LLM 调用前已经会兜底任何 orphan tool_call，所以恢复路径无需额外占位逻辑。
>
> 离线验证三件：(B1) 正常 turn 结束 → `read_checkpoint() is None`；(B2) 模拟 tool 批次后 LLM 调用前 crash → checkpoint 含 3 条消息，assistant 的 1 个 tool_call 与 1 个 tool 消息严格配对；(B3) 把 B2 的 checkpoint 喂给 `_pair_tool_calls` → 无 orphan。

WebUI 关 tab、CLI Ctrl-C、模型超时——只要一个 turn 跑了一半就丢了，下次重启再发消息可能 hit "tool_call without matching tool_result"。

**改造方案**：

1. `MemoryStore` 加两个方法：
   ```python
   def write_checkpoint(self, history_tail: list, pending_tool_ids: list[str]) -> None: ...
   def read_checkpoint(self) -> dict | None: ...
   def clear_checkpoint(self) -> None: ...
   ```
   写到 `memory/_checkpoint.json`（gitignore）。
2. `runner.step_async` 在每次 tool 执行完后调一次 `write_checkpoint`；turn 正常结束时 `clear_checkpoint`。
3. `AgentLoop.__init__` 启动时 `read_checkpoint`，把未完成的 tool_calls 用 `"(interrupted by shutdown)"` 占位补齐再 merge 进 history。

代码增量 ~120 行。**收益**：用户第一次见到"我关了 tab 居然没丢"的时候会很惊喜。

---

### 1.3 LLM 错误恢复：空响应 + 截断重试 ✅ 已落地（commit `2cad8c8`）

> **实施摘要**：`agent/providers/base.py` 暴露 `TRUNCATED_FINISH_REASONS = {"length", "max_tokens", "model_max_tokens"}` + `is_truncated(finish_reason)`。**未**新增 `LLMResponse.truncated` 字段——探查发现 `openai_compat.py:177` 与 `anthropic_provider.py:253` 早就把 `choice.finish_reason` / `response.stop_reason` 透传，所以只需在 runner 端用 helper 识别。`step_async` 增加 `empty_retries`(≤2) / `length_retries`(≤3) 两个状态机：
>
> - **空响应**：`reply.strip()` 为空且无 tool_calls → 注入 `"（上一轮无任何输出，请继续推进或给出最终答复）"` 重试；前端发出 `tool_error name=_empty_response`
> - **截断续写**：`is_truncated(response.finish_reason)` → 把已输出 reply 暂存进 final_parts + history，注入 `"（上一轮被 max_tokens 截断，请从中断处续写...）"` 重试；前端发出 `tool_error name=_length_truncation`
> - 进入 `should_execute_tools` 分支时两个计数器自动归零
>
> 离线 mock 验证：空 → ok 自动恢复；`length+"part"` → `stop+"-rest"` 拼成 `"part-rest"`；连续 3 次空响应触顶后正常落地为空串、无死循环。

`_ask_model` 现在拿到空 content + 空 tool_calls 会直接 return 空串退出，用户看到"大内总管: "（空），体验极差。

**改造方案**：在 `step_async` 主循环里加状态机

```python
empty_retries = 0
length_retries = 0
while True:
    response = await self._ask_model(history, emit)
    if response.should_execute_tools:
        empty_retries = 0; length_retries = 0
        # ...原有逻辑
        continue
    # 空响应
    if not (response.content or "").strip() and empty_retries < 2:
        empty_retries += 1
        history.append({"role": "user", "content": "（上一轮无输出，请继续推进或给出最终答复）"})
        continue
    # 截断（需 provider 暴露 finish_reason）
    if getattr(response, "truncated", False) and length_retries < 3:
        length_retries += 1
        history.append({"role": "assistant", "content": response.content})
        history.append({"role": "user", "content": "（上一轮被 max_tokens 截断，请从中断处续写）"})
        continue
    # 正常落地
    ...
```

附带改动：`agent/providers/base.py` 的 `LLMResponse` 加 `truncated: bool` 字段，`openai_compat.py` / `anthropic_provider.py` 各自填充。

代码增量 ~60 行。**收益**：模型偶尔抽风不再让用户面对空白。

---

## 二、应做（中 ROI，每项 3-5 天）

### 2.1 Subagent 异步化（按需，不强求 mid-turn 注入）

当前 `DispatchSubagentTool` 在 `to_thread` 里阻塞等子代理跑完。对**单用户**而言这不是性能问题（用户也不会一边等子代理一边和主代理聊天），但有两个真实体验问题：

- 子代理跑 web_design_engineer 这种 5-10 分钟任务时，整个 chat 卡住，用户不能 `/clear` 也不能切页面发消息
- WebUI 看不到子代理的"我现在跑到第几步"，只能干等

**轻量改造**（不引入 nanobot 那套 pending_queue + injection_cycles）：

1. 子代理依旧"调用即等结果"，但等待期间 `AgentRunner` 暴露一个 `cancel_event: asyncio.Event`
2. WebUI 加一个 `/api/cancel-current-turn` 路由，触发 `cancel_event.set()`
3. 子代理的 LLM 调用 / tool 执行检查到 cancel 立即抛 `CancelledError`，主代理收到错误返回 `"[子代理被用户取消]"`
4. 前端 NavRail 加"中止当前差事"按钮（`actionAssets.statusBusy` 状态下显现）

代码增量 ~150 行（含前端按钮）。**不做** mid-turn 真正异步注入——单用户场景下"用户能取消"已经覆盖 90% 痛点。

---

### 2.2 Runtime Context 块（每 turn 注入轻量元信息）

现在 `ContextBuilder.build_system_prompt` 是**启动时一次性**构建的，模型不知道当前是几点、是从 CLI 还是 WebUI 来的、todo 还剩几条。

**改造方案**：在 `runner._ask_model` 拼 messages 时，在最后一条 user message 之前 / 之后插入一条系统 message：

```python
def _runtime_context_block(self) -> str:
    now = datetime.now(_UTC8).strftime("%Y-%m-%d %H:%M")
    channel = self.usage_type  # "main_agent" / "subagent:xxx"
    todo_count = len(self.todo_store.todos) if self.todo_store else 0
    return (
        "[Runtime Context — metadata, not instructions]\n"
        f"Current Time: {now} (UTC+8)\n"
        f"Channel: {channel}\n"
        f"Pending Todos: {todo_count}\n"
        f"Active Model Entry: {self.provider_name}/{self.model}\n"
        "[/Runtime Context]"
    )
```

注入策略：每 3 个 turn 注入一次（避免每次都吃 token）。

代码增量 ~40 行。**收益**：模型回答时间相关问题不再瞎猜，子代理知道自己被谁调用、什么时候。

---

### 2.3 记忆瘦身：MEMORY.local.md 行龄 + 半自动整理

现在 `Compactor` 只**追加**到 MEMORY.local.md，跑半年文件会到几十 KB，每次主 prompt 都要带上。

**轻量方案**（不做 nanobot Dream 那套 LLM 自编辑）：

1. `memory/` 目录用 git 管理（首次启动 `git init` + 初次提交）
2. CLI 新增 `/memory-doctor` 命令、WebUI 新增"诊断记忆"按钮，逻辑：
   - 用 `git log --follow --format=%ai memory/MEMORY.local.md` 拿到每行最后修改时间
   - 标记 >30 天没动过的行为 `← stale 30d+`
   - 调一次 LLM，给出"建议删除/合并"列表，**让用户在 WebUI 一键确认**（不是 LLM 自己改）
3. Compactor 写入时附带 `[YYYY-MM-DD]` 行首日期，方便人眼/git blame 双轨

代码增量 ~200 行（含 git 集成 + 前端确认 modal）。**收益**：MEMORY.local.md 不再无限膨胀，且用户始终掌控"什么被删"。

---

### 2.4 工具结果智能摘要（替代 1.1 的硬截断）

1.1 的 `_cap_tool_result` 是一刀切。更聪明的做法：

- `read_file`：保留前 200 行 + 后 50 行 + `[skipped N lines]` 标记
- `bash`：保留 stdout 末尾 N KB（最近的输出最重要）
- `grep` / `glob`：超过 100 命中时只保留前 100 + 总数
- `web_fetch`：去 HTML 标签后再截

落到 `agent/tools/` 各个工具自己的 `_summarize_for_history(content) -> str` 方法上，runner 不再做格式判断。

代码增量 ~150 行（分散到 5-6 个工具文件）。**收益**：截断后模型还能正常引用，不像 1.1 那样可能截断在 JSON 中间。

---

## 三、可做（低优先，看心情）

### 3.1 技能依赖检查
SKILL.md frontmatter 加 `requires: { bins: [git], env: [API_KEY] }`，`SkillsLoader` 启动时检查，不满足的不进 always_skills 池。代码 ~40 行。

### 3.2 多会话支持（WebUI 多 tab）
当前 `WebUIState` 单 history 单 lock，多个 tab 打开实际是共享同一对话。如果想做"会话列表"侧栏，需要：
- `Session` 抽象（id / history / lock / tokens）
- WebUI 左侧加会话列表
- WebSocket 路由按 session_id 分发

代码增量 ~500+ 行，**只在你真有这个需求时再做**。单用户绝大多数场景一个会话够用。

### 3.3 Hook 系统抽象
把 runner 里散落的 `if emit:` 分支抽成 `AgentHook` 接口（`before_iteration / on_stream / before_tools / after_iteration`），WebUI 流和 CLI Rich 渲染各自实现一个。仅在你想给 CLI 加炫酷渲染时再做，否则只是"工程上更优雅"。

### 3.4 SSRF / Workspace 越界硬保护
你现在是本地单用户工具，不是公开服务，**SSRF 优先级很低**。如果未来要把 WebUI 发布给别人用，再补这块。

---

## 四、明确不做的（避免过度设计）

| Nanobot 有 | 我们不做 | 理由 |
|---|---|---|
| MessageBus + 多渠道 | ❌ | 单用户 CLI+WebUI 已够 |
| `provider="auto"` 自动路由 | ❌ | ModelPanel 已经手动选 entry，自动路由是冗余复杂度 |
| 跨会话 `asyncio.Semaphore` | ❌ | 单用户无并发会话需求 |
| Dream LLM 自编辑记忆 | ❌（改用 2.3 半自动） | 让 LLM 自动改 MEMORY 风险高，用户失控 |
| `_MAX_INJECTION_CYCLES=5` 多轮注入 | ❌（改用 2.1 取消按钮） | 单用户场景下"取消"覆盖 90% 痛点 |
| 31 家 OAuth Provider | ❌ | 已经在 ModelPanel 重构里覆盖到 30 家足够 |

---

## 五、推荐落地节奏

```
Week 1  ─ 1.1 上下文治理 + 1.3 错误恢复       ✅ 已合并 (2cad8c8, 2026-05-08)
Week 2  ─ 1.2 Checkpoint                       ✅ 已合并 (2026-05-08, 同周补齐)
Week 3  ─ 2.2 Runtime Context + 2.4 工具摘要   ⏳ 下一站   ← 模型输出质量
Week 4  ─ 2.1 Subagent 取消按钮                ⏸ 待办     ← 长任务可控
Week 5  ─ 2.3 记忆瘦身（半自动）               ⏸ 待办     ← 长期投资
此后    ─ 视使用频率再决定是否做 3.x
```

---

## 六、每项改动的验证 checklist

| 改动 | 怎么验证它真的工作 | 状态 |
|---|---|---|
| 1.1 上下文治理 | 让模型连续 read_file 30 个 1KB 文件，第 31 轮时 `_ask_model` 实际发出去的 messages JSON 序列化后总长 < 之前的 60%；离线纯函数测：15×50KB 工具消息 cap+shrink 后 5 条变摘要、其余 ≤8.5KB | ✅ 通过 |
| 1.2 Checkpoint | 跑到一半 Ctrl-C kill `webui.py`，重启后 `memory/_checkpoint.json` 存在；发新消息不报 tool_call mismatch；离线 mock：tool 批次后 crash → checkpoint 持久化且 tool_calls/tools 严格配对，再喂 `_pair_tool_calls` 无 orphan | ✅ 通过 |
| 1.3 错误恢复 | mock provider 第一次返回空 content，runner 自动注入 nudge 重试，第二次拿到正常回复；mock 返回 `length+"part"` 后 `stop+"-rest"` → 拼成 `"part-rest"`；连 3 次空响应触顶不死循环 | ✅ 通过 |
| 2.1 Subagent 取消 | 跑 `dispatch_subagent` 给一个故意 sleep 30s 的任务，10s 时点中止按钮，子代理在 1s 内退出 |
| 2.2 Runtime Context | 问"现在几点"，模型答出实际系统时间而不是训练时的胡乱时间 |
| 2.3 记忆瘦身 | 跑 `/memory-doctor`，前端 modal 列出 stale 行；用户取消 = 文件不变；用户确认 = git diff 显示删行 |
| 2.4 工具摘要 | `read_file` 一个 5000 行的文件，传给下次 LLM 的 tool message 中能看到 `[skipped 4750 lines]` 标记，开头结尾完整 |

---

## 七、一句话总结

**Emperor Agent 不缺功能，缺"长程稳定性"和"中断后能续上"**。把 §1 的三件事做完，体验会从"能用"跨到"愿意每天用"。§2 是锦上添花，§3 留给真有需求那天。

**Week 1+2 已落地（§1.1 + §1.2 + §1.3）→ 长对话稳定性 + 模型抽风自动恢复 + 中断恢复 三件全齐**。下一站 §2.2 Runtime Context + §2.4 工具摘要，进入"模型输出质量"阶段。
