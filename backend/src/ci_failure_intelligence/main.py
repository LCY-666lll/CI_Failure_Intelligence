"""创建 CI Failure Intelligence 的真实运行入口。"""

from ci_failure_intelligence.application import create_app
from ci_failure_intelligence.core.config import RetrievalSettings, Settings

# 从环境变量和项目根目录 .env 读取、校验后端运行配置。
runtime_settings = Settings()  # pyright: ignore[reportCallIssue]

# 单独读取历史检索配置；这里只创建配置对象，不加载模型。
runtime_retrieval_settings = RetrievalSettings()  # pyright: ignore[reportCallIssue]

# 显式提供检索配置，应用启动时才会准备共享检索资源。
app = create_app(
    settings=runtime_settings,
    retrieval_settings=runtime_retrieval_settings,
)
