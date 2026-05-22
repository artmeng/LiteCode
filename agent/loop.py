"""agent/loop.py

AgentLoop：系统装配 + CLI 主循环。

负责将所有子系统（记忆、工具、子代理、Team、MCP、控制流）
装配为一个可运行的 Agent 实例，并提供 CLI 交互入口。
"""
from __future__ import annotations

import shutil
from pathlib import Path

from dotenv import load_dotenv
from loguru import logger

from .compactor import Compactor
from .context import ContextBuilder
from .control import AskUserTool, ControlManager, ControlMode, InteractionKind, ProposePlanTool, TurnPaused
from .logger import configure as configure_logging
from .memory import MemoryStore
from .mcp import MCPClient
from .model_router import ModelRouter
from .runner import AgentRunner
from .scheduler import SchedulerService, SchedulerStore
from .skills import SkillsLoader
from .subagents import SubagentRegistry
from .team import (
    TeamBroadcastTool,
    TeamListTool,
    TeamManager,
    TeamReadInboxTool,
    TeamSendMessageTool,
    TeamShutdownTool,
    TeamSpawnTool,
)
from .telemetry import TokenTracker
from .tools import (
    DispatchSubagentTool,
    EditFileTool,
    GlobTool,
    GrepTool,
    LoadSkill,
    ReadFileTool,
    RunCommand,
    TodoStore,
    ToolRegistry,
    UpdateTodosTool,
    WebFetch,
    WriteFileTool,
)


class AgentLoop:
    """Agent 主循环装配类。

    构造时完成全部子系统初始化：
    - MemoryStore：三层记忆 + checkpoint 恢复
    - ToolRegistry：内置工具 + MCP 外部工具注册
    - ControlManager：Ask / Plan 暂停恢复状态机
    - ModelRouter：主/次模型路由
    - AgentRunner：单轮 LLM 调用 + 工具执行引擎
    - TeamManager：持久队友协作
    """

    def __init__(
        self,
        root: Path | None = None,
        model: str | None = None,
        verbose: bool = True,
        startup_compaction: bool = True,
    ):
        load_dotenv()  # 加载 .env 文件中的环境变量
        self.root = root or Path(__file__).parent.parent  # 项目根目录
        configure_logging(self.root)  # 配置 loguru 日志
        self.verbose = verbose
        self._model_override = model  # CLI --model 参数覆盖
        self.user_file = self._ensure_local_user_file()  # 确保用户档案本地文件存在

        # 记忆系统：负责 history.jsonl / MEMORY.local.md / checkpoint 的读写
        self.memory = MemoryStore(
            memory_dir=self.root / "memory",
            user_file=self.user_file,
            memory_template=self.root / "templates" / "init" / "MEMORY.md",
        )
        self.token_tracker = TokenTracker(self.root / "memory" / "tokens.jsonl")  # Token 账本
        self.scheduler_store = SchedulerStore(self.root)  # 定时任务存储
        self.scheduler_service = SchedulerService(self.scheduler_store)  # 定时任务服务

        # 技能加载器：按需从 skills/ 目录加载 SKILL.md 能力包
        self.skills = SkillsLoader(self.root / "skills")
        # System prompt 构建器：合并 SOUL / TOOL / USER / MEMORY / Skills
        self.context_builder = ContextBuilder(
            self.root / "templates",
            self.skills,
            memory=self.memory,
        )

        workspace = self.root
        # 工具注册表：统一管理所有可调用工具
        self.registry = ToolRegistry()
        # 注册内置工具：命令、网络、技能、文件系统、搜索
        self.registry.register(RunCommand(workspace))
        self.registry.register(WebFetch())
        self.registry.register(LoadSkill(self.skills))
        self.registry.register(ReadFileTool(workspace))
        self.registry.register(WriteFileTool(workspace))
        self.registry.register(EditFileTool(workspace))
        self.registry.register(GlobTool(workspace))
        self.registry.register(GrepTool(workspace))

        # 控制流管理器：管理 Ask / Plan 状态与权限模式
        self.control_manager = ControlManager(self.root)
        # 注册 Ask / Plan 控制工具（可暂停 turn 的特殊工具）
        self.registry.register(AskUserTool(self.control_manager))
        self.registry.register(ProposePlanTool(self.control_manager))

        # Todolist 存储，runner 用它判断任务是否全部完成
        self.todos = TodoStore()
        self.registry.register(UpdateTodosTool(self.todos))

        # 子代理注册表：维护各子代理的 spec / system_prompt / 工具白名单
        self.subagent_registry = SubagentRegistry(
            self.root / "templates" / "subagents",
            skills_loader=self.skills,
        )
        self._install_subagent_tool()   # 注册 dispatch_subagent 工具
        self._install_team_tools()      # 注册 Team 系列工具并初始化 TeamManager

        # MCP 外部工具客户端：连接 stdio / SSE MCP 服务器，发现并注册外部工具
        self.mcp_client: MCPClient | None = None
        try:
            self.mcp_client = MCPClient(self.root)
        except Exception as exc:
            logger.warning(f"MCP client init failed: {exc}")

        # 加载模型配置，创建 provider / runner / compactor
        self.refresh_model_config(initial=True)

        # 优先恢复上次未完成 turn 的 checkpoint（含 tool_calls / tool 消息成对的中间状态）；
        # 没有 checkpoint 才回退到 history.jsonl 的未归档段。
        checkpoint = self.memory.read_checkpoint()
        if checkpoint:
            logger.info(f"[Startup: restored checkpoint with {len(checkpoint)} messages]")
            self.history: list = list(checkpoint)
            if self.control_manager.payload().get("pending"):
                logger.info("[Startup: control interaction pending; keeping checkpoint until resume/cancel]")
            else:
                self.memory.clear_checkpoint()
        else:
            unarchived = self.memory.load_unarchived_history()
            if startup_compaction and len(unarchived) >= 2:
                logger.info(f"[Startup: found {len(unarchived)} unarchived turns, compacting...]")
                try:
                    self.compactor.compact_startup(unarchived)
                except Exception as exc:
                    logger.warning(f"startup compaction failed: {exc}")
                unarchived = []
            self.history: list = list(unarchived)

    def _ensure_local_user_file(self) -> Path:
        """确保本地用户档案文件存在；首次启动时从模板复制生成。"""
        template = self.root / "templates" / "init" / "USER.md"
        local = self.root / "templates" / "USER.local.md"
        if not local.exists() and template.exists():
            local.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(template, local)
        return local

    def refresh_model_config(self, *, initial: bool = False) -> None:
        """重新加载 model_config.json 并热更新 provider / runner / compactor。

        initial=True 时创建新的 AgentRunner；否则就地更新 runner 的各字段，
        避免重建对象导致已有引用失效。
        """
        self.model_router = ModelRouter(self.root, model_override=self._model_override)
        main_route = self.model_router.route("main_agent")  # 路由主 Agent 用的模型
        snapshot = main_route.snapshot
        self.provider_snapshot = snapshot
        self.provider = snapshot.provider
        self.provider_name = snapshot.provider_name
        self.provider_label = snapshot.provider_label
        self.model = snapshot.model
        self.max_tokens = snapshot.generation.max_tokens
        self.temperature = snapshot.generation.temperature
        self.reasoning_effort = snapshot.generation.reasoning_effort
        self.max_context = snapshot.context_window_tokens
        self.supports_vision = snapshot.supports_vision
        self.entry_name = snapshot.entry_name
        self.model_role = snapshot.model_role
        self.route_reason = main_route.reason

        # 路由记忆压缩用的模型（通常是次模型，更经济）
        compactor_route = self.model_router.route("memory_compaction")
        compactor_snapshot = compactor_route.snapshot
        compactor_fallback = compactor_route.fallback
        self.compactor = Compactor(
            compactor_snapshot.provider,
            compactor_snapshot.model,
            self.memory,
            temperature=compactor_snapshot.generation.temperature,
            reasoning_effort=compactor_snapshot.generation.reasoning_effort,
            provider_name=compactor_snapshot.provider_name,
            token_tracker=self.token_tracker,
            model_role=compactor_snapshot.model_role,
            fallback_provider=compactor_fallback.provider if compactor_fallback else None,
            fallback_model=compactor_fallback.model if compactor_fallback else None,
            fallback_provider_name=compactor_fallback.provider_name if compactor_fallback else None,
            fallback_generation=compactor_fallback.generation if compactor_fallback else None,
            fallback_model_role=compactor_fallback.model_role if compactor_fallback else "main",
        )
        system_prompt = self.context_builder.build_system_prompt()
        if self.verbose and initial:
            logger.info(f"[System Prompt]\n{system_prompt}\n{'='*60}\n")

        if initial or not hasattr(self, "runner"):
            self.runner = AgentRunner(
                provider=self.provider,
                model=self.model,
                registry=self.registry,
                system_prompt=system_prompt,
                max_tokens=self.max_tokens,
                temperature=self.temperature,
                reasoning_effort=self.reasoning_effort,
                provider_name=self.provider_name,
                model_role=self.model_role,
                route_reason=self.route_reason,
                usage_type="main_agent",
                memory_store=self.memory,
                token_tracker=self.token_tracker,
                compactor=self.compactor,
                todo_store=self.todos,
                control_manager=self.control_manager,
                max_context=self.max_context,
            )
            return

        self.runner.provider = self.provider
        self.runner.model = self.model
        self.runner.system_prompt = system_prompt
        self.runner.max_tokens = self.max_tokens
        self.runner.temperature = self.temperature
        self.runner.reasoning_effort = self.reasoning_effort
        self.runner.provider_name = self.provider_name
        self.runner.model_role = self.model_role
        self.runner.route_reason = self.route_reason
        self.runner.fallback_provider = None
        self.runner.fallback_model = None
        self.runner.fallback_provider_name = None
        self.runner.fallback_generation = None
        self.runner.fallback_model_role = "main"
        self.runner.usage_type = "main_agent"
        self.runner.compactor = self.compactor
        self.runner.control_manager = self.control_manager
        self.runner.max_context = self.max_context

    def refresh_runtime_context(self) -> None:
        """重新构建 system prompt（技能列表或配置文件变更后调用）。"""
        self.skills.reload()
        self.runner.system_prompt = self.context_builder.build_system_prompt()

    def init_mcp(self) -> None:
        """同步初始化 MCP（CLI / WebUI 启动时调用）。"""
        if not self.mcp_client:
            return
        if getattr(self.mcp_client, "_initialized", False):
            return
        from .providers.base import run_sync
        try:
            run_sync(self.mcp_client.initialize())
            tools = self.mcp_client.get_tools()
            for tool in tools:
                self.registry.register(tool)
            logger.info(f"[MCP] registered {len(tools)} tools")
        except Exception as exc:
            logger.warning(f"MCP initialization failed: {exc}")

    def close_mcp(self) -> None:
        """关闭所有 MCP 连接。"""
        if self.mcp_client:
            from .providers.base import run_sync
            try:
                run_sync(self.mcp_client.close())
            except Exception:
                pass

    def run(self) -> None:
        """CLI 模式主循环：读取用户输入 → 执行一轮 step → 打印回复，循环往复。"""
        self.init_mcp()
        while True:
            user_input = input("You🫅 : ")
            # 以 / 开头的输入视为 CLI 命令，不走 LLM
            if self._handle_cli_command(user_input):
                continue
            self.history.append({"role": "user", "content": user_input})
            self.memory.append_history("user", user_input)
            try:
                reply = self.runner.step(self.history)
            except TurnPaused:
                # runner 触发了 Ask / Plan 暂停，进入 CLI 控制流驱动
                self._drive_cli_control()
                continue
            logger.info(f"LiteCode: {reply}")

    def _handle_cli_command(self, user_input: str) -> bool:
        """解析并执行 CLI 斜杠命令。返回 True 表示已处理，主循环应跳过 LLM 调用。"""
        text = user_input.strip()
        if not text.startswith("/"):
            return False
        command = text.split()[0].lower()
        if command in {"/help", "/commands"}:
            logger.info(
                "\n可用命令:\n"
                "  /help      显示命令列表\n"
                "  /status    查看模型、Provider、Token、工具和技能数量\n"
                "  /plan      查看 / 开关 Plan 模式：/plan on|off|status\n"
                "  /mode      查看 / 切换权限模式：/mode ask|auto|plan|status\n"
                "  /clear     清空当前 CLI 运行上下文, 不删除 memory/history.jsonl\n"
                "  /exit      退出 CLI\n"
            )
            return True
        if command == "/status":
            totals = self.token_tracker.totals()
            all_defs = self.registry.get_definitions()
            mcp_count = len([d for d in all_defs if d["name"].startswith("mcp_")])
            builtin_count = len(all_defs) - mcp_count
            logger.info(
                "\n当前状态:\n"
                f"  provider: {self.provider_name}\n"
                f"  model:    {self.model} (main)\n"
                f"  secondary:{self.model_router.secondary.model if self.model_router.secondary.model_role == 'secondary' else '-'}\n"
                f"  tokens:   {totals.get('total', 0)} total / {totals.get('calls', 0)} calls\n"
                f"  skills:   {len(self.skills.skills)}\n"
                f"  tools:    {len(all_defs)} (builtin: {builtin_count}, mcp: {mcp_count})\n"
                f"  history:  {len(self.history)} in-memory turns\n"
                f"  control:  {self.control_manager.mode}\n"
            )
            return True
        if command == "/plan":
            parts = text.split(maxsplit=1)
            arg = parts[1].strip().lower() if len(parts) > 1 else "status"
            if arg in {"on", "plan"}:
                self.control_manager.set_mode(ControlMode.PLAN.value)
                logger.info("\nPlan 模式已开启：只读探索 + 提问 + 计划预览；批准前不执行。")
                return True
            if arg in {"off", "normal"}:
                self.control_manager.set_mode(ControlMode.ASK_BEFORE_EDIT.value)
                logger.info("\nPlan 模式已关闭，已回到编辑前询问模式。")
                return True
            payload = self.control_manager.payload()
            pending = payload.get("pending")
            pending_label = "-"
            if pending:
                pending_label = f"{pending.get('kind')}:{pending.get('id')}"
            logger.info(
                "\nControl 状态:\n"
                f"  mode: {payload.get('mode')}\n"
                f"  previous: {payload.get('previous_mode') or '-'}\n"
                f"  pending: {pending_label}\n"
            )
            return True
        if command == "/mode":
            parts = text.split(maxsplit=1)
            arg = parts[1].strip().lower() if len(parts) > 1 else "status"
            if arg in {"ask", "ask_before_edit", "edit_before_ask"}:
                self.control_manager.set_mode(ControlMode.ASK_BEFORE_EDIT.value)
                logger.info("\n权限模式：编辑前询问。")
                return True
            if arg == "auto":
                self.control_manager.set_mode(ControlMode.AUTO.value)
                logger.info("\n权限模式：自动执行。")
                return True
            if arg == "plan":
                self.control_manager.set_mode(ControlMode.PLAN.value)
                logger.info("\n权限模式：计划模式。")
                return True
            payload = self.control_manager.payload()
            logger.info(
                "\n权限模式:\n"
                f"  mode: {payload.get('mode')}\n"
                f"  previous: {payload.get('previous_mode') or '-'}\n"
            )
            return True
        if command == "/clear":
            self.history.clear()
            self.todos.todos = []
            logger.info("\n已清空当前 CLI 屏幕上下文；长期记忆和 history.jsonl 未删除。")
            return True
        if command in {"/exit", "/quit"}:
            raise SystemExit(0)
        logger.warning(f"\n未知命令: {command}。输入 /help 查看可用命令。")
        return True

    def _drive_cli_control(self) -> None:
        """CLI 模式下驱动 Ask / Plan pending interaction 直到用户给出完整回答或取消。

        Ask 模式：逐题打印问题，读取用户文字回答。
        Plan 模式：渲染 PlanCard，等待 approve / comment / cancel 命令。
        """
        while True: # 防止再次列出选择清单
            pending = self.control_manager.payload().get("pending")
            if not pending:
                return
            if pending.get("kind") == InteractionKind.ASK.value:
                logger.info(self._render_cli_ask(pending))
                answers = {}
                for question in pending.get("questions") or []:
                    qid = question.get("id")
                    raw = input(f"回答 {question.get('header') or qid}: ").strip() # 用户选择清单问题
                    answers[qid] = {"choice": raw, "freeform": ""}
                resume = self.control_manager.answer(pending["id"], answers)
            elif pending.get("kind") == InteractionKind.PLAN.value:
                logger.info(self._render_cli_plan(pending))
                while True:
                    raw = input("plan> approve / comment <内容> / cancel: ").strip()
                    if raw == "approve":
                        resume = self.control_manager.approve(pending["id"])
                        break
                    if raw.startswith("comment "):
                        resume = self.control_manager.comment(pending["id"], raw[len("comment "):].strip())
                        break
                    if raw == "cancel":
                        event = self.control_manager.cancel(pending["id"])
                        message = str(event.get("message") or "")
                        if message:
                            self.history.append({"role": "user", "content": message})
                            self.memory.append_history("user", message, extra={"type": "control_response"})
                        self.memory.clear_checkpoint()
                        logger.info(f"已取消：{event.get('interaction', {}).get('id')}")
                        return
                    logger.info("请输入 approve、comment <内容> 或 cancel。")
            else:
                return

            self.history.append({"role": "user", "content": resume.message})
            self.memory.append_history("user", resume.message, extra={"type": "control_response"})
            try:
                reply = self.runner.step(self.history)
            except TurnPaused: # 配合while True 处理列出选择清单后，再次列出选择清单
                continue
            logger.info(f"LiteCode: {reply}")
            return # 没有继续列出选择清单，说明本轮结束，开始接受用户下一轮请求

    @staticmethod
    def _render_cli_ask(interaction: dict) -> str:
        lines = ["\n需要你先确认几个问题："]
        for question in interaction.get("questions") or []:
            lines.append(f"\n[{question.get('header')}] {question.get('question')}")
            for idx, option in enumerate(question.get("options") or [], start=1):
                lines.append(f"  {idx}. {option.get('label')} — {option.get('description')}")
            lines.append("  也可直接输入自由回答。")
        return "\n".join(lines)

    @staticmethod
    def _render_cli_plan(interaction: dict) -> str:
        assumptions = interaction.get("assumptions") or []
        lines = [
            "\n计划待预览：",
            f"# {interaction.get('title') or 'Plan'}",
            "",
            str(interaction.get("summary") or ""),
            "",
            str(interaction.get("plan_markdown") or ""),
        ]
        if assumptions:
            lines.extend(["", "Assumptions:", *[f"- {item}" for item in assumptions]])
        return "\n".join(lines)

    def _install_subagent_tool(self) -> None:
        """构造 dispatch_subagent 工具并注册到 registry。

        内部工厂函数 _make_subagent_runner 负责为每个子代理派遣请求创建
        独立的 AgentRunner（独立 history / 工具白名单 / token 计账）。
        """
        def _make_subagent_runner(*, spec, sub_registry, task: str | None = None):
            route = self.model_router.route("subagent", agent_type=spec.name, task=task)
            snapshot = route.snapshot
            fallback = route.fallback
            return AgentRunner(
                provider=snapshot.provider,
                model=snapshot.model,
                registry=sub_registry,
                system_prompt=spec.system_prompt,
                max_tokens=min(2000, snapshot.generation.max_tokens),
                temperature=snapshot.generation.temperature,
                reasoning_effort=snapshot.generation.reasoning_effort,
                provider_name=snapshot.provider_name,
                model_role=snapshot.model_role,
                route_reason=route.reason,
                fallback_provider=fallback.provider if fallback else None,
                fallback_model=fallback.model if fallback else None,
                fallback_provider_name=fallback.provider_name if fallback else None,
                fallback_generation=fallback.generation if fallback else None,
                fallback_model_role=fallback.model_role if fallback else "main",
                usage_type=f"subagent:{spec.name}",
                memory_store=None,
                token_tracker=self.token_tracker,
                compactor=None,
                max_turns=spec.max_turns,
            )

        self.registry.register(DispatchSubagentTool(
            client=None,
            model="",
            parent_registry=self.registry,
            subagent_registry=self.subagent_registry,
            runner_factory=_make_subagent_runner,
        ))

    def _install_team_tools(self) -> None:
        """初始化 TeamManager 并注册全部 Agent Team 工具到 registry。

        Team runner 工厂为每次 wake 创建独立 AgentRunner，
        注入队友身份 system_prompt 与协作规则。
        """
        def _make_team_runner(*, member, spec, sub_registry):
            route = self.model_router.route("team", agent_type=spec.name)
            snapshot = route.snapshot
            fallback = route.fallback
            system_prompt = (
                f"{spec.system_prompt}\n\n"
                "## Agent Team 协作规则\n\n"
                f"- 你的队友名是 `{member.name}`, role 是 `{member.role}`。\n"
                "- 你拥有自己的持久 thread 与 inbox, 不需要向用户直接发言。\n"
                "- 收到任务后先理解 inbox 内容, 必要时调用工具完成差事。\n"
                "- 完成后必须用 send_message(to=\"lead\", content=\"...\") 回禀关键结果。\n"
                "- 只能处理本队友职责内的任务, 不要创建或唤醒其他队友。\n"
            )
            return AgentRunner(
                provider=snapshot.provider,
                model=snapshot.model,
                registry=sub_registry,
                system_prompt=system_prompt,
                max_tokens=min(4000, snapshot.generation.max_tokens),
                temperature=snapshot.generation.temperature,
                reasoning_effort=snapshot.generation.reasoning_effort,
                provider_name=snapshot.provider_name,
                model_role=snapshot.model_role,
                route_reason=route.reason,
                fallback_provider=fallback.provider if fallback else None,
                fallback_model=fallback.model if fallback else None,
                fallback_provider_name=fallback.provider_name if fallback else None,
                fallback_generation=fallback.generation if fallback else None,
                fallback_model_role=fallback.model_role if fallback else "main",
                usage_type=f"team:{member.name}",
                memory_store=None,
                token_tracker=self.token_tracker,
                compactor=None,
                max_turns=spec.max_turns,
            )

        self.team_manager = TeamManager(
            root=self.root,
            parent_registry=self.registry,
            subagent_registry=self.subagent_registry,
            runner_factory=_make_team_runner,
        )
        self.registry.register(TeamSpawnTool(self.team_manager))
        self.registry.register(TeamListTool(self.team_manager))
        self.registry.register(TeamSendMessageTool(self.team_manager))
        self.registry.register(TeamReadInboxTool(self.team_manager))
        self.registry.register(TeamBroadcastTool(self.team_manager))
        self.registry.register(TeamShutdownTool(self.team_manager))
