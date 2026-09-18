from dataclasses import dataclass


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
class FeaturesConfig:
    """功能开关（团队系统等）。"""

    coordinator_mode: bool = False
    fork_teammate: bool = False
    router: RouterConfig | None = None
    loop: str = ""  # Agent 循环策略（spec_loop）：react 或自定义模块路径
    host: HostConfig | None = None  # 会话宿主（features.host）
