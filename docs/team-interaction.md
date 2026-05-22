# Agent Team 交互流程文档

## 1. 角色定义

| 角色 | 说明 |
|------|------|
| **Lead** | 主 Agent，驱动整个 Team，拥有全部 Team 工具 |
| **Teammate** | 持久队友，被动唤醒执行，只能回禀 Lead |

---

## 2. 存储结构

```
.team/
├── config.json              # 成员名册（name/role/agent_type/status）
├── inbox/
│   ├── lead.jsonl           # Lead 的收件箱（append-only）
│   └── {name}.jsonl         # 每个队友的收件箱（append-only）
├── threads/
│   └── {name}.json          # 队友独立对话历史（每次执行后覆写）
├── cursors/
│   └── {name}.json          # 已读消息指针（防重复处理）
└── checkpoints/
    └── {name}.json          # 执行中快照（正常完成后删除，崩溃时保留）
```

消息类型（`type` 字段）：

| type | 含义 |
|------|------|
| `task` | Lead 分配的初始任务 |
| `message` | 普通消息 / broadcast |
| `result` | 队友执行完毕后自动回禀 |
| `error` | 队友执行异常时回禀 |

---

## 3. 工具清单

### Lead 可用的 Team 工具

| 工具 | 说明 |
|------|------|
| `spawn_teammate(name, role, task?)` | 创建/唤回队友，可选立即分配任务并唤醒 |
| `send_message(to, content, wake?)` | 点对点发消息，`wake=true` 立即唤醒目标执行 |
| `broadcast(content, recipients?, wake?)` | 广播给多个/全部队友 |
| `read_inbox(limit?, mark_read?)` | 读取 lead 自己的 inbox（收队友回禀） |
| `list_teammates()` | 查看所有队友状态、未读数、最近消息 |
| `shutdown_teammate(name)` | 关闭队友（记录保留，不再接受任务） |

### Teammate 可用的 Team 工具

| 工具 | 说明 |
|------|------|
| `send_message(to, content)` | 只能投递消息（`wake` 强制为 false，不能递归唤醒） |
| `read_inbox(limit?, mark_read?)` | 读取自己的 inbox |

---

## 4. 交互流程

### 4.1 创建队友并分配任务（spawn_teammate）

```
Lead
 │
 ├─ spawn_teammate("alice", "coder", task="写登录模块")
 │       │
 │       ├─ 写入 .team/config.json（注册 alice）
 │       ├─ bus.send(from=lead, to=alice, type=task, content="写登录模块")
 │       │       └─ 追加到 .team/inbox/alice.jsonl
 │       │
 │       └─ wake_teammate("alice")          ← 同步阻塞
 │               │
 │               ├─ alice.status → working
 │               ├─ 读 .team/threads/alice.json（历史上下文）
 │               ├─ 读 inbox 未读消息，拼入 history
 │               ├─ 写 checkpoint（防崩溃）
 │               │
 │               ├─ runner.step(history)    ← LLM 执行
 │               │       └─ alice 可调用工具、send_message 回禀
 │               │
 │               ├─ 写回 .team/threads/alice.json
 │               ├─ 清除 checkpoint
 │               ├─ 推进 cursor
 │               ├─ alice.status → idle
 │               │
 │               └─ 若 alice 未主动 send_message：
 │                       bus.send(from=alice, to=lead, type=result, content=执行结果)
 │                               └─ 追加到 .team/inbox/lead.jsonl
 │
 └─ 返回 {created, message, result}
```

### 4.2 发消息唤醒（send_message + wake=true）

```
Lead
 │
 ├─ send_message(to="alice", content="再加注册模块", wake=true)
 │       │
 │       ├─ bus.send(from=lead, to=alice, type=message)
 │       └─ wake_teammate("alice")    ← 同样流程，alice 读新消息继续干
 │
 └─ 返回 {message, result}
```

### 4.3 广播（broadcast）

```
Lead
 │
 └─ broadcast(content="今日目标：完成认证模块", wake=true)
         │
         ├─ for alice in members:        ← 串行，逐个执行
         │       bus.send(to=alice)
         │       wake_teammate(alice)
         │
         └─ 返回 {sent: [...], results: [...]}
```

### 4.4 队友回禀

```
Teammate（alice）执行中
 │
 ├─ 方式一：主动调用 send_message(to="lead", content="已完成，结果如下...")
 │               └─ 追加到 .team/inbox/lead.jsonl（type=message）
 │
 └─ 方式二：不调用 send_message
                 → runner 结束后，manager 自动发送 type=result 回禀
```

### 4.5 Lead 读取回禀

```
Lead
 │
 └─ read_inbox(limit=20)
         │
         └─ 读 .team/inbox/lead.jsonl，返回未读消息列表
```

---

## 5. 队友生命周期与状态机

```
           spawn_teammate
                │
                ▼
             [idle]
                │
        wake_teammate()
                │
                ▼
           [working]  ──── Lock 防并发重入
                │
        ┌───────┴──────────┐
        │ 正常完成          │ 异常
        ▼                  ▼
      [idle]            [error]
                           │
                    下次 wake 时恢复
                    checkpoint 继续执行

   shutdown_teammate()
        │
        ▼
    [shutdown]  ← 可通过 spawn_teammate 唤回（status 重置为 idle）

   重启时
        │
        ▼
   [working] → [offline]  ← 下次 wake 正常恢复
```

---

## 6. 角色 → 子代理类型映射

| role | agent_type | 使用模型 |
|------|-----------|----------|
| `coder` | `neiguan_yingzao` | **主模型** |
| `reviewer` | `shangbao_dianbu` | 次模型 |
| `researcher` | `dongchang_tanshi` | 次模型 |
| `reader` | `sili_suitang` | 次模型 |
| `runner` | `xiaohuangmen` | 次模型 |
| 其他 | `sili_suitang`（默认） | 次模型 |

可通过 `agent_type` 参数覆盖默认映射。

---

## 7. 关键约束

- **单向驱动**：只有 Lead 能唤醒 Teammate，Teammate 不能唤醒其他 Teammate
- **串行执行**：`broadcast` 是 for 循环串行，无并行
- **消息持久**：inbox 是 append-only，历史永不删除，cursor 防重复
- **上下文连续**：thread 文件保证跨次唤醒的对话历史不丢失
- **崩溃恢复**：checkpoint 在执行前写入，正常完成后删除，崩溃后下次唤醒从断点继续

---

## 8. WebSocket 事件（前端实时展示）

队友执行时，后端通过 WebSocket 推送以下事件：

| 事件 | 触发时机 |
|------|----------|
| `team_member_update` | 队友状态变更（idle/working/error） |
| `team_message` | 有新消息投入任意 inbox |
| `team_run_start` | 队友开始执行 |
| `team_run_delta` | 队友 LLM 流式输出片段 |
| `team_run_tool_call` | 队友调用工具 |
| `team_run_tool_result` | 工具返回结果 |
| `team_run_tool_error` | 工具执行报错 |
| `team_run_done` | 队友执行完成 |
| `team_run_error` | 队友执行异常 |
