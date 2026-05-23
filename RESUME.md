## 1. 自进化记忆沉淀

设计三层记忆沉淀机制（工作记忆 / 情景记忆 / 长期记忆），将执行过程中的对话历史、工具交互轨迹与用户偏好信号通过 LLM 驱动的压缩流程自动提炼为情景摘要、长期记忆与用户档案三类可复用资产，配合热日志 / 冷归档分层存储与 system prompt 常驻注入，实现跨会话知识延续与用户偏好自适应。

### 三层记忆结构

[MemoryStore](cci:2://file:///Users/pencil/Desktop/emperor-agent/agent/memory.py:19:0-174:16) 类注释直接说明了三层：

```@/Users/pencil/Desktop/emperor-agent/agent/memory.py:20-28
class MemoryStore:
    """三层记忆存储管理器。

    - 工作记忆：由 AgentRunner 内存 history 列表承载，不涉及本类
    - 情景记忆：memory/YYYY-MM-DD.md，每次压缩时由 Compactor 写入
    - 长期记忆：memory/MEMORY.local.md，每轮注入 system prompt
    - Checkpoint：memory/_checkpoint.json，工具批次完成后原子写入
    - 用户档案：templates/USER.local.md，每轮注入 system prompt
    """
```

- **工作记忆**：[AgentRunner](cci:2://file:///Users/pencil/Desktop/emperor-agent/agent/runner.py:43:0-796:114) 内存 `history` 列表，每轮 `while True` 就地修改
- **情景记忆**：`memory/YYYY-MM-DD.md`，由 [append_episode](cci:1://file:///Users/pencil/Desktop/emperor-agent/agent/memory.py:78:4-82:48) 写入

```@/Users/pencil/Desktop/emperor-agent/agent/memory.py:79-83
    def append_episode(self, content: str) -> None:
        p = self.today_episode_path()
        existing = p.read_text(encoding="utf-8") if p.exists() else f"# {p.stem} 情景记忆\n"
        new_text = existing.rstrip() + "\n\n" + content.strip() + "\n"
        p.write_text(new_text, encoding="utf-8")
```

- **长期记忆**：`memory/MEMORY.local.md`，[write_memory](cci:1://file:///Users/pencil/Desktop/emperor-agent/agent/memory.py:88:4-89:77) / [read_memory](cci:1://file:///Users/pencil/Desktop/emperor-agent/agent/memory.py:85:4-86:96)

```@/Users/pencil/Desktop/emperor-agent/agent/memory.py:85-90
    def read_memory(self) -> str:
        return self.memory_file.read_text(encoding="utf-8") if self.memory_file.exists() else ""

    def write_memory(self, content: str) -> None:
        self.memory_file.write_text(content.strip() + "\n", encoding="utf-8")
```

### LLM 驱动压缩 → 提炼三类产物

核心压缩逻辑在 [_compact_messages](cci:1://file:///Users/pencil/Desktop/emperor-agent/agent/compactor.py:152:4-212:44)，解析模型输出的三个 XML 标签：

```@/Users/pencil/Desktop/emperor-agent/agent/compactor.py:153-213
    async def _compact_messages(self, messages: list[dict[str, Any]]) -> None:
        prompt = _PROMPT_TEMPLATE.format(
            old_conversation=_messages_to_text(messages),
            current_memory=self.memory.read_memory() or "(空)",
            current_user=self.memory.read_user() or "(空)",
            today_episode=self.memory.read_today_episode() or "(空)",
            now_hhmm=datetime.now(_UTC8).strftime("%H:%M"),
        )
        ...
        if episode := _extract("episode", text):
            self.memory.append_episode(episode)       # 情景摘要 → YYYY-MM-DD.md
        if new_memory := _extract("updated_memory", text):
            self.memory.write_memory(new_memory)      # 长期记忆 → MEMORY.local.md
        if new_user := _extract("updated_user", text):
            self.memory.write_user(new_user)          # 用户档案 → USER.local.md
```

压缩 prompt 要求模型输出三段 XML：

```@/Users/pencil/Desktop/emperor-agent/templates/agent/compact_prompt.md:20-44
<episode>         ← 情景摘要（今日关键事件，200字以内）
<updated_memory>  ← 长期记忆完整新版本
<updated_user>    ← 用户档案（仅有明确偏好信号时才改）
```

### 热日志 / 冷归档分层存储

[HistoryLog](cci:2://file:///Users/pencil/Desktop/emperor-agent/agent/memory_history.py:23:0-325:83) 负责热/冷分层：

```@/Users/pencil/Desktop/emperor-agent/agent/memory_history.py:1-6
- 热存储：history.jsonl，append-only，记录当前活跃段
- 冷存储：history_archive/YYYY-MM.jsonl.gz，已压缩原始行的 gzip 归档
- 索引：history_index.json，记录全局 seq 进度与归档统计
```

压缩时将旧行 gzip 写入月度归档，原子重写热日志：

```@/Users/pencil/Desktop/emperor-agent/agent/memory_history.py:56-80
    def compact(self, active_messages: list[dict[str, Any]]) -> None:
        ...
        if archived_rows:
            self._append_archive(archived_rows)
        self._rewrite_hot(active_rows)  # 原子重写热日志，只保留活跃行
```

### System Prompt 常驻注入

[ContextBuilder.build_system_prompt](cci:1://file:///Users/pencil/Desktop/emperor-agent/agent/context.py:54:4-92:40) 每轮将长期记忆和用户档案注入 system prompt：

```@/Users/pencil/Desktop/emperor-agent/agent/context.py:55-93
    def build_system_prompt(self) -> str:
        # 长期记忆（MEMORY.local.md 全文注入）
        if self.memory:
            memory = self.memory.read_memory().strip()
            if memory:
                parts.append(f"# Long-term Memory\n\n{memory}")
        return "\n\n---\n\n".join(parts)
```

`USER.local.md` 优先级高于模板：`USER.local.md > templates/init/USER.md`

**全链路**：压缩 → `_compact_messages` 调次模型 → 解析三段 XML → 分别写 `YYYY-MM-DD.md` / `MEMORY.local.md` / `USER.local.md` → 旧原始行 gzip 归档 → 下次启动 `build_system_prompt` 将长期记忆和用户档案重新注入 system prompt，实现跨会话延续。

---

## 2. 中心化多 Agent 协作

设计中心化多 Agent 协作架构，以主 Agent 统一规划、审批与质量控制，子 Agent 以 Tool Call 方式受控执行，避免引入复杂的 Agent 间协调与状态管理基础设施；通过不移交控制权、最小化结果传递、工具权限白名单与轮次上限保障安全性与可控性，并支持子代理并发派遣与持久队友按消息唤醒两种协作模式，在高质量、强约束的 Coding 场景下提升并行执行效率与任务稳定性。

| 设计点 | 代码位置 |
|--------|---------|
| 主 Agent 统一规划、审批与质量控制 | [AgentRunner](cci:2://file:///Users/pencil/Desktop/emperor-agent/agent/runner.py:43:0-796:114) 主循环 + [control/manager.py](cci:7://file:///Users/pencil/Desktop/emperor-agent/agent/control/manager.py:0:0-0:0) Ask/Plan 审批 |
| 子 Agent 以 Tool Call 方式受控执行 | [DispatchSubagentTool](cci:2://file:///Users/pencil/Desktop/emperor-agent/agent/tools/dispatch.py:11:0-149:20) 本身就是 tool |
| 避免复杂的 Agent 间协调基础设施 | 无消息队列/分布式状态，子代理用独立 history，Team 用本地文件 inbox |
| 不移交控制权 | `dispatch_subagent` 不在任何子代理白名单里，队友不能 [spawn_teammate](cci:1://file:///Users/pencil/Desktop/emperor-agent/agent/team/manager.py:82:4-139:9) |
| 最小化结果传递 | 子代理仅返回 `final` 摘要文本，主 history 只多一条 tool_result |
| 工具权限白名单 | `_BUILTIN_SPECS` 硬编码，注释写"安全设置不应被无意修改" |
| 轮次上限 | `max_turns` 8~20 |
| 子代理并发派遣 | `concurrency_safe = True`，runner 用 `asyncio.gather` |
| 持久队友按消息唤醒 | [TeamManager.wake_teammate](cci:1://file:///Users/pencil/Desktop/emperor-agent/agent/team/manager.py:224:4-248:26) + [TeamStore](cci:2://file:///Users/pencil/Desktop/emperor-agent/agent/team/store.py:11:0-247:39) 文件持久化 + checkpoint |

---

## 3. 分层上下文压缩

设计分层上下文压缩机制，将大工具结果截断、缓存友好型占位压缩与结构化笔记摘要结合，构建"摘要预览—占位替换—按需检索—超限兜底"的上下文治理闭环，在保证长会话稳定性的同时提升 Prompt Cache 收益并降低 Token 成本。

---

## 4. 权限与容错机制

构建"风险识别-分级授权-人工确认-失败恢复"闭环，融合断点续跑、空响应重试、截断续写与模型降级，保障高风险操作可控、长任务稳定执行。

### 风险识别（先判断能不能做）

在每个 turn 开始，`AgentRunner.step_async` 先做 `clarification assessment`。当识别到"高影响且目标不明确"任务时，`Ask Guard` 生效：
- 写工具会被 `_ask_guard_blocks_tool` 拦截
- 即使模型直接给了最终答复，也会 `_pause_for_clarification` 强制转为提问
- 只读探索可继续，避免误改

本质是把"先问清再动手"变成执行层硬约束，而不是提示词建议。

### 分级授权（按风险给权限）

控制层把权限分成 `ask_before_edit / auto / plan` 三档：
- `ask_before_edit`：高风险操作走审批，低风险可直通
- `auto`：不主动审批，但仍受工具安全边界约束
- `plan`：只暴露只读工具 + `ask_user/propose_plan`，写操作硬禁

具体在 `_run_tool` 中执行：先判模式可用性，再做权限评估；不通过直接拒绝，通过才执行。

### 人工确认（关键动作必须人拍板）

需要审批时，[PermissionManager.require_approval](cci:1://file:///Users/pencil/Desktop/emperor-agent/agent/permissions/manager.py:33:4-63:55) 会创建结构化 Ask 交互并暂停 turn；Runner 解析暂停标记后抛 `TurnPaused`，同时写 checkpoint、发 `ask_request/turn_paused` 事件。用户在 WebUI/CLI 回答后，`ControlManager.answer/comment/approve` 生成恢复消息回注 history，再继续原任务。

另外做了"**同参一次性授权**"：同工具同参数只放行一次，避免长期放权失控。

### 失败恢复（出错不中断）

- **断点续跑**：turn 开始和工具批次后都 [write_checkpoint](cci:1://file:///Users/pencil/Desktop/emperor-agent/agent/memory.py:146:4-157:12)，正常结束才 [clear_checkpoint](cci:1://file:///Users/pencil/Desktop/emperor-agent/agent/memory.py:170:4-175:12)；重启可从 checkpoint 恢复
- **空响应重试**：模型空输出时自动注入 nudge，最多重试 2 次
- **截断续写**：`finish_reason=length/max_tokens` 时自动续写，最多 3 次，并拼接完整结果
- **模型降级**：主模型失败时自动 fallback 次模型（主流程和压缩流程都做了），保证任务连续执行