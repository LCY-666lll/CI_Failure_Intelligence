"""
定义并校验 CI Failure Intelligence 的运行配置。
环境变量
    ↓
配置对象读取
    ↓
启动时校验
    ↓
通过后才能创建FastAPI应用
"""

# field_validator 用来登记一个字段校验函数。
from pydantic import Field, SecretStr, field_validator
# BaseSettings 负责从环境变量和 .env 创建配置对象。
# SettingsConfigDict 负责声明读取环境变量时采用的规则。
from pydantic_settings import BaseSettings,SettingsConfigDict


DEFAULT_INGEST_MAX_BODY_BYTES = 8 * 1024 * 1024

DEFAULT_INGEST_TIMESTAMP_TOLERANCE_SECONDS = 300

# 继承 BaseSettings：获得读取运行配置的能力，把环境变量和 .env 作为字段数据来源
class Settings(BaseSettings):
    """集中描述后端从环境变量读取的运行配置。"""

    # 告诉 BaseSettings：按什么规则寻找和读取配置
    model_config = SettingsConfigDict(
        # 本地开发时读取当前工作目录中的 .env
        env_file=".env",
        # 按 UTF-8 解码 .env，避免中文注释或内容产生编码问题
        env_file_encoding="utf-8",
        # 所有字段对应的环境变量都增加 CFI_ 前缀
        env_prefix="CFI_",
        # .env 还有当前类没声明的配置，就忽略，不会导致报错
        extra="ignore",
    )

    # 打印时遮住内容的字符串，SecretStr：让打印配置对象时只显示星号
    ingest_hmac_secret: SecretStr

    # 这个属性是整数，没有提供时使用默认值，提供的值必须大于 0
    ingest_max_body_bytes: int = Field(
        default=DEFAULT_INGEST_MAX_BODY_BYTES,
        gt=0,
    )

    # 控制请求 timestamp 与服务器当前时间允许相差的最大秒数。
    ingest_timestamp_tolerance_seconds:int = Field(
        default=DEFAULT_INGEST_TIMESTAMP_TOLERANCE_SECONDS,
        gt = 0,
        le = DEFAULT_INGEST_TIMESTAMP_TOLERANCE_SECONDS
    )

    # 向 Pydantic 登记：处理 ingest_hmac_secret 字段时，调用下面这个方法检查它。
    @field_validator("ingest_hmac_secret")
    # 表示这是类方法：第一个参数由 Python 自动传入当前类 Settings
    @classmethod
    def validate_ingest_hmac_secret(
        # 当前类
        cls,
        value: SecretStr,
    ) -> SecretStr:
        """确保 CI 接入密钥具有足够的 UTF-8 字节长度。

            validate_ingest_hmac_secret函数
                ↓ @classmethod
            变成类方法
                ↓ @field_validator
            登记为Pydantic字段校验器

        Args:
            value: 从环境变量读取并转换为 SecretStr 的 HMAC 密钥。

        Returns:
            SecretStr: 通过最小字节长度校验的原始密钥对象。

        Raises:
            ValueError: 密钥经过 UTF-8 编码后不足 32 字节。
        """

        # 取真实内容，再转成字节
        secret_bytes = value.get_secret_value().encode("utf-8")

        if len(secret_bytes) < 32:
            raise ValueError("CI 接入 HMAC 密钥的 UTF-8 编码长度必须至少为 32 字节")

        return value
