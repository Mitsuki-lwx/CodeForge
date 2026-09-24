from dataclasses import dataclass, field


@dataclass
class ProviderConfig:
    """单个 LLM 服务提供商的配置。"""

    name: str
    protocol: str  # "anthropic" | "openai"
    model: str
    api_key: str
    base_url: str | None = None
    thinking: bool = False
    context_window: int = 0  # 上下文窗口大小（token），0 表示走协议默认
    vendor: str | None = None  # 上游厂商（deepseek/openai/anthropic…；None=自动识别）
    tier: str = ""  # 路由价位标记：cheap=便宜（入口复杂度判断用，见 spec_router）；留空=不参与路由
    # 模型别名 → 具体模型名（见 spec_model_resolution）。
    # 让角色 / Skill 用语义档位（haiku/sonnet/opus）声明而不绑定厂商；
    # 别名表按 provider 独立，未配的保留别名会退回主模型并告警。
    model_aliases: dict[str, str] = field(default_factory=dict)


@dataclass
class RouterConfig:
    """模型路由配置（spec_router）。默认关；开启后默认 B 两档。"""

    enabled: bool = False  # 默认关：配多模型也不自动路由
    judge_prompt: str = ""  # 自定义复杂度判断指令；空则用内置
    cheap_tier: str = "cheap"  # 便宜模型的路由价位标记


@dataclass
class HostConfig:
    """会话宿主配置（`features.host`）。

    默认全关：`enabled=False` 时主流程走原路径，行为与引入这项配置之前完全一致。
    `unattended_policy` 只管 host 里"没有人按键"时 `ask` 级决策怎么代答，
    合法取值见 `core.permissions.modes.UnattendedPolicy`（loader 会校验）。
    """

    enabled: bool = False
    port: int = 0  # 0 = 让内核挑随机端口
    token_file: str = ""  # 空 = 用默认的 .codeforge/host.token
    unattended_policy: str = "deny_all"


@dataclass
class JevConfig:
    """Jev（TypeSafe System One）决策模型的连接信息（`features.approval_review.jev`）。

    **刻意不是 `ProviderConfig`** —— Jev 不是 chat 模型（无流式、无对话历史、
    无工具调用），契约完全不同，所以单独一段配置，不塞进 `providers` 列表。
    """

    url: str = "https://api.typesafe.ai/v1/systemone"
    api_key: str = ""  # 空 = jev 后端不可用（装配时不启用审查，**不回落 llm**）
    model: str = "jev-latest"
    timeout_s: float = 15.0  # 实测延迟 1.2–9.7s（偶发 >90s）→ 15s 覆盖实测最慢


@dataclass
class ApprovalReviewConfig:
    """审批审查的后端选择（`features.approval_review`）。

    - `backend="jev"`（**默认**）：Jev 决策模型（见 `docs/spec_jev_reviewer.md`）
    - `backend="llm"`：调 chat 模型 + 解析 JSON 输出（显式可选；**不再作为默认或回落**）

    整段不配 = 走默认（`jev`）。**Jev 不可用时不回落到 llm** —— 见
    `docs/spec_jev_default.md`：宁可没有审查者（`REVIEW` 档会安全降级为
    `deny_all`），也不静默改用另一个后端。
    """

    backend: str = "jev"
    jev: JevConfig | None = None


@dataclass
class FeaturesConfig:
    """功能开关（团队系统等）。"""

    coordinator_mode: bool = False
    fork_teammate: bool = False
    router: RouterConfig | None = None
    loop: str = ""  # Agent 循环策略（spec_loop）：react 或自定义模块路径
    host: HostConfig | None = None  # 会话宿主（features.host）
    approval_review: ApprovalReviewConfig | None = None  # 审批审查后端
