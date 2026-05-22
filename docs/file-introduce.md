## 上下文注入全景

所有可能进入 LLM 上下文的内容、来源文件、注入位置、注入条件与优先级：

| 文件 | 内容 | 作用 | 注入位置 | 注入条件 | 优先级 / 备注 |
|---|---|---|---|---|---|
| `templates/SOUL.md` | 人格与语气 | 固定模型口吻、行为风格与自我认知 | system prompt 静态段 | 始终注入 | 最高，第一段 |
| `templates/TOOL.md` | 工具使用偏好 | 告知模型工具使用原则与默认决策边界 | system prompt 静态段 | 始终注入 | — |
| `templates/USER.local.md` | 用户偏好档案 | 让模型了解用户沟通风格、技术水平与工作背景，实现偏好自适应 | system prompt 静态段 | 文件存在时注入 | 优先于 `init/USER.md`；不存在则降级 |
| `templates/init/USER.md` | 用户偏好模板（空白） | 首次启动兜底，避免偏好档案缺失报错 | system prompt 静态段 | `USER.local.md` 不存在时降级注入 | 兜底 |
| `templates/agent/identity.md` | 工作区路径 + 文件结构说明 | 让模型知道当前工作区位置，正确拼接文件路径 | system prompt 静态段 | 始终注入（Jinja2 渲染） | — |
| `memory/MEMORY.local.md` | 长期记忆（跨会话知识） | 跨会话传递核心目标、未完成任务、架构决策等关键事实，避免重启后"失忆" | system prompt 静态段 | 文件存在且内容非空 | — |
| `skills/*/SKILL.md`（常驻） | 技能完整内容 | 高频技能常驻可用，无需每次 load，减少工具调用延迟 | system prompt 静态段 | frontmatter 中 `always: true` | 全文注入 |
| `skills/*/SKILL.md`（按需） | 技能名称 + 描述摘要 | 让模型知道有哪些技能可用，需要时主动调 `load_skill` 加载全文，节省 context | system prompt 静态段 | 有非 `always` 技能存在 | 仅摘要，不注入全文；用 `load_skill` 工具按需加载全文 |
| `control_manager.system_prompt()` | 当前权限模式指令 | 实时告知模型当前权限等级，约束其在 ask / auto / plan 模式下的行为边界 | system prompt 动态追加 | `control_manager` 存在（主 Agent 始终有） | 每轮 `_ask_model` 时追加在静态段末尾 |
| `clarification.prompt()` | Ask Guard 强制提问指令 | 检测到高影响歧义时，强制模型先调 `ask_user` 确认范围再执行写操作 | system prompt 动态追加 | `clarification.required == True`（用户消息命中 scope / risk / ui 关键词） | 仅触发时追加；同一 turn 只评估一次 |
| `memory/_checkpoint.json` | 上次未完成 turn 的完整 history | 进程崩溃恢复：重启后从中断点继续执行，不丢失已完成的工具调用 | messages 数组（对话历史） | 启动时文件存在（进程崩溃恢复） | **优先于热日志**；正常 turn 结束后删除 |
| `memory/history.jsonl`（热日志活跃段） | 最近未归档的对话行 | 正常重启后恢复最近几轮对话，保持会话连续性 | messages 数组（对话历史） | 启动时无 checkpoint | checkpoint 存在时跳过此文件 |
| 当前 turn 实时消息 | user / assistant / tool 消息 | 构成当前轮次的完整交互上下文，供模型推理和工具调用 | messages 数组（对话历史） | 本轮产生时实时追加 | — |
| `memory/YYYY-MM-DD.md` | 今日情景记忆 | 避免压缩时重复提炼今天已记录过的内容 | **仅压缩时**传给 Compactor | token 超阈值触发压缩 | 不进主 Agent context；让压缩模型知道今天已记录什么 |
| `memory/MEMORY.local.md` | 当前长期记忆完整内容 | 压缩时让模型在原有基础上更新，不误删已有条目 | **仅压缩时**传给 Compactor | token 超阈值触发压缩 | 让压缩模型保留已有条目，不误删 |
| `templates/USER.local.md` | 当前用户档案完整内容 | 压缩时让模型在原有基础上更新，只在有明确偏好信号时才修改 | **仅压缩时**传给 Compactor | token 超阈值触发压缩 | 让压缩模型保留已有偏好，有偏好信号才改 |
| `memory/history_archive/*.jsonl.gz` | 已归档原始对话行 | 冷存储归档，保留完整历史记录供人工查阅或未来 RAG 检索 | **永不注入** | — | 只归档留存；人工查阅 / 未来 RAG 扩展用 |

---

#### control_manager
| 模式 | 行为 |
|------|------|
| ask_before_edit | 默认。读操作直接跑，写/危险操作先进 AskCard 审批 |
| auto | 最高权限，工具层不主动审批 |
| plan | 只读探索 + ask_user + propose_plan，写操作全部禁用 |

**不同模式下的提示词**
`manager.py`
```python
    def system_prompt(self) -> str:
        if self.mode == ControlMode.PLAN.value:
            return (
                "# Control Mode: Plan\n\n"
                "- 当前处于 Plan 模式。你必须先通过只读探索理解环境，不允许修改文件、运行命令执行变更、派遣子代理或创建队友。\n"
                "- 若需求存在会影响方案的偏好或取舍，调用 `ask_user` 提问。\n"
                "- 当方案足够明确时，必须调用 `propose_plan` 提交完整计划，等待用户评论或批准。\n"
                "- 用户批准前不要执行计划。\n"
                "- 不允许用普通最终回复替代计划卡；最终必须通过 `propose_plan` 进入 PlanCard。"
            )
        return (
            "# Control Tools\n\n"
            f"- 当前权限模式：{self.mode}。\n"
            "- `ask_before_edit` 模式下，危险、不确定或高影响操作会触发权限审批；低风险读操作和普通编辑可继续执行。\n"
            "- `auto` 模式下，工具层不主动审批，但仍受路径安全、schema 校验和工具自身安全策略约束。\n"
            "- 当用户目标存在高影响歧义且无法通过读文件/搜索等方式确定时，调用 `ask_user` 提出结构化问题。\n"
            "- 高影响歧义包括范围/验收不清的大改动、架构/重构/UI 取舍、提交推送、删除覆盖、发布部署、成本/权限/安全边界。\n"
            "- 可通过只读探索确认的事实先探索；但在写入、高影响操作或最终答复前仍有关键取舍时，必须提问。\n"
            "- 只有在用户显式开启 Plan 模式后，才使用 `propose_plan` 提交等待批准的计划。"
        )
```

#### 工具前置处理
这三步是**发给 LLM 前的 history 治理管道**，依次执行：

##### 1. [_pair_tool_calls](cci:1://file:///Users/pencil/Desktop/emperor-agent/agent/runner.py:359:4-397:22) — 修复配对

确保每个 `assistant` 的 `tool_calls` 后面都有对应的 `tool` 消息，避免 OpenAI API 报"tool messages 不足"错误。

- 孤立的 `tool` 消息（没有对应 `tool_call`）→ 丢掉
- 有 `tool_call` 但缺少回复 → 补一条占位 `"（工具执行被中断）"`

##### 2. [_cap_tool_result](cci:1://file:///Users/pencil/Desktop/emperor-agent/agent/runner.py:412:4-432:18) — 单条硬截断

每条工具结果超过 **8000 字符**就截断，保留头 7800 + 尾 200。

- 防止单条超大输出（如读了一个巨型文件）直接撑爆 context
- 截断只在发给 LLM 的副本上，原 `history` 不动

##### 3. [_shrink_old_tool_results](cci:1://file:///Users/pencil/Desktop/emperor-agent/agent/runner.py:434:4-453:18) — 旧条目摘要化

最近 10 条工具消息保留原文，**更早的**大于 1500 字节的工具结果整条替换为一行摘要：

```
[已摘要] read_file → 原文 12000 字符已省略
```

- 防止历史工具结果累积占满 context
- 最近的保留原文（LLM 当前轮可能还需要参考）

---

**执行顺序有意义**：先修复结构 → 再截单条 → 再压缩旧历史，三步做完才把 `governed` 发给模型。


#### ClarificationPolicy


[ClarificationAssessment](cci:2://file:///Users/pencil/Desktop/emperor-agent/agent/control/clarification.py:31:0-48:31) 是 **Ask Guard 的评估结果对象**，决定当前 turn 是否必须强制先问用户。
