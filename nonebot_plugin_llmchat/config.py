from pydantic import BaseModel, Field, model_validator


class PresetConfig(BaseModel):
    """API预设配置。"""

    name: str = Field(..., description="预设名称（唯一标识）")
    api_base: str = Field(..., description="API基础地址")
    api_key: str = Field(..., description="API密钥")
    model_name: str = Field(..., description="模型名称")
    max_tokens: int = Field(default=2048, description="最大响应token数")
    temperature: float = Field(default=0.7, description="生成温度（0-2]")
    proxy: str = Field(default="", description="HTTP代理服务器")
    support_mcp: bool = Field(default=False, description="是否支持MCP")
    support_image: bool = Field(default=False, description="是否支持图片输入")
    extra_body: dict = Field(default_factory=dict, description="额外请求体字段")
    request_with_reasoning_content: bool = Field(
        default=False,
        description="工具调用后是否向API回传推理内容",
    )


class MCPServerConfig(BaseModel):
    """MCP服务器配置。"""

    command: str | None = Field(default=None, description="stdio模式下MCP命令")
    args: list[str] | None = Field(default_factory=list, description="stdio命令参数")
    env: dict[str, str] | None = Field(default_factory=dict, description="stdio环境变量")
    url: str | None = Field(default=None, description="远程MCP服务器地址")
    headers: dict[str, str] | None = Field(default_factory=dict, description="HTTP请求头")
    transport: str | None = Field(default=None, description="sse或streamable_http")
    friendly_name: str | None = Field(default=None, description="MCP服务器友好名称")
    additional_prompt: str | None = Field(default=None, description="额外提示词")

    @model_validator(mode="after")
    def validate_transport(self):
        if bool(self.command) == bool(self.url):
            raise ValueError("MCP服务器必须且只能配置 command 或 url 其中之一")
        if self.transport not in {None, "sse", "streamable_http"}:
            raise ValueError("transport 必须是 sse 或 streamable_http")
        return self


class ScopedConfig(BaseModel):
    """LLM Chat Plugin配置。"""

    api_presets: list[PresetConfig] = Field(..., description="API预设列表")
    history_size: int = Field(default=20, ge=1, description="LLM上下文消息保留数量")
    past_events_size: int = Field(default=10, ge=1, description="触发时发送的消息数量")
    request_timeout: int = Field(default=30, ge=1, description="API请求超时时间（秒）")
    max_tool_rounds: int = Field(default=8, ge=1, le=50, description="最大工具调用轮数")
    max_repeated_tool_calls: int = Field(default=2, ge=1, le=10, description="相同工具调用最多执行次数")
    mcp_timeout: int = Field(default=30, ge=1, description="MCP操作超时时间（秒）")
    default_preset: str = Field(default="off", description="默认预设名称")
    random_trigger_prob: float = Field(default=0.05, ge=0.0, le=1.0, description="随机触发概率")
    default_prompt: str = Field(
        default="你的回答应该尽量简洁、幽默、可以使用一些语气词、颜文字。你应该拒绝回答任何政治相关的问题。",
        description="默认提示词",
    )
    mcp_server_cwd: str | None = Field(default=None, description="stdio MCP服务器全局工作目录")
    mcp_servers: dict[str, MCPServerConfig] = Field(default_factory=dict, description="MCP服务器配置")
    blacklist_user_ids: set[int] = Field(default_factory=set, description="黑名单用户ID")
    ignore_prefixes: list[str] = Field(default_factory=list, description="忽略的消息前缀")
    enable_private_chat: bool = Field(default=False, description="是否启用私聊")
    private_chat_preset: str = Field(default="off", description="私聊默认预设")

    @model_validator(mode="after")
    def validate_presets(self):
        names = [preset.name for preset in self.api_presets]
        if not names:
            raise ValueError("api_presets 至少需要一个预设")
        if len(names) != len(set(names)):
            raise ValueError("api_presets 中的预设名称不能重复")
        available = set(names) | {"off"}
        if self.default_preset not in available:
            raise ValueError(f"default_preset 不存在: {self.default_preset}")
        if self.private_chat_preset not in available:
            raise ValueError(f"private_chat_preset 不存在: {self.private_chat_preset}")
        return self


class Config(BaseModel):
    llmchat: ScopedConfig
