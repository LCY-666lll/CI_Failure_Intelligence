"""创建 CI Failure Intelligence 的真实运行入口。"""

from ci_failure_intelligence.application import create_app
from ci_failure_intelligence.core.config import Settings

# Settings负责从环境变量和项目根目录.env读取真实运行配置。
# 配置缺失或非法时，这一行会直接终止应用启动。
# BaseSettings会从环境来源补齐必填字段，
runtime_settings = Settings()  # pyright: ignore[reportCallIssue]

# 把已经验证的真实配置显式注入应用工厂。。
# Uvicorn 接管这个 app，启动应用生命周期，随后才处理请求
app = create_app(settings=runtime_settings)
