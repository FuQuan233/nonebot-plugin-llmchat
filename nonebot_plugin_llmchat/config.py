
from pydantic import BaseModel, Field


class PresetConfig(BaseModel):
    """API预设配置"""
    name: str = Field(..., description="预设名称（唯一标识）")
    api_base: str = Field(..., description="API基础地址")
    api_key: str = Field(..., description="API密钥")
    model_name: str = Field(..., description="模型名称")
    max_tokens: int = Field(2048, description="最大响应token数")
    temperature: float = Field(0.7, description="生成温度（0-2]")

class ScopedConfig(BaseModel):
    """LLM Chat Plugin配置"""
    api_presets: list[PresetConfig] = Field(...,description="API预设列表（至少配置1个预设）")
    history_size: int = Field(20, description="LLM上下文消息保留数量")
    past_events_size : int = Field(10, description="触发回复时发送的群消息数量")
    request_timeout: int = Field(30, description="API请求超时时间（秒）")
    default_preset: str = Field("off", description="默认使用的预设名称")
    random_trigger_prob: float = Field(0.05, ge=0.0, le=1.0, description="随机触发概率（0-1]")
    default_prompt: str = Field("你的回答应该尽量简洁、幽默、可以使用一些语气词、颜文字。你应该拒绝回答任何政治相关的问题。", description="默认提示词")

class Config(BaseModel):
    llmchat: ScopedConfig
