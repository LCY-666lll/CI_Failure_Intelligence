"""根据运行配置创建 DeepSeek LangChain 客户端。"""

from langchain_deepseek import ChatDeepSeek

from ci_failure_intelligence.core.config import DeepSeekSettings


def create_deepseek_client(settings: DeepSeekSettings) -> ChatDeepSeek:
    """创建配置完成但尚未发起网络请求的 DeepSeek 客户端。

    Args:
        settings: 已完成环境读取和字段校验的 DeepSeek 配置，
            包含官方 API Key、模型、超时、重试和输出上限。

    Returns:
        ChatDeepSeek: 使用 Chat Completions、非思考模式和有界输出配置的
            LangChain DeepSeek 客户端。创建对象本身不会调用模型 API。
    """

    return ChatDeepSeek(
        model=settings.deepseek_model,
        # ChatDeepSeek 原生接受 SecretStr，不需要在项目代码里提前展开真实 Key。
        api_key=settings.deepseek_api_key,
        base_url=settings.deepseek_api_base,
        timeout=settings.deepseek_timeout_seconds,
        max_retries=settings.deepseek_max_retries,
        max_tokens=settings.deepseek_max_output_tokens,
        # v1 固定使用 Chat Completions，避免依赖自动切换请求协议。
        use_responses_api=False,
        # DeepSeek 默认开启 thinking；MVP 显式关闭以控制延迟和输出规模。
        extra_body={
            "thinking": {
                "type": "disabled",
            }
        },
    )
