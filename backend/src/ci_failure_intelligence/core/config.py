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
from pydantic_settings import BaseSettings, SettingsConfigDict

DEFAULT_INGEST_MAX_BODY_BYTES = 8 * 1024 * 1024

DEFAULT_INGEST_TIMESTAMP_TOLERANCE_SECONDS = 300
# nonce 至少覆盖 timestamp 前后时间窗口，并保留额外边界余量。
MINIMUM_INGEST_NONCE_TTL_SECONDS = 660
# 当前默认安全周期同时用于 nonce TTL 和 Redis 最短连续运行时间。
DEFAULT_INGEST_NONCE_TTL_SECONDS = MINIMUM_INGEST_NONCE_TTL_SECONDS

DEFAULT_REDIS_HOST = "127.0.0.1"
DEFAULT_REDIS_PORT = 6379
DEFAULT_REDIS_USERNAME = "cfi_ingest"
DEFAULT_REDIS_DATABASE = 0
DEFAULT_REDIS_CONNECT_TIMEOUT_SECONDS = 1.0
DEFAULT_REDIS_SOCKET_TIMEOUT_SECONDS = 1.0
DEFAULT_REDIS_MAX_CONNECTIONS = 10

# PostgreSQL 默认通过 Docker 映射端口供本机 FastAPI 访问。
DEFAULT_POSTGRES_HOST = "127.0.0.1"
DEFAULT_POSTGRES_PORT = 5432
DEFAULT_POSTGRES_DATABASE = "ci_failure_intelligence"
DEFAULT_POSTGRES_USERNAME = "cfi_app"

# 第一版限制单个后端进程能够占用的数据库连接数量。
DEFAULT_POSTGRES_POOL_SIZE = 5
DEFAULT_POSTGRES_MAX_OVERFLOW = 5

# 数据库不可达时快速失败，避免请求长时间卡住。
DEFAULT_POSTGRES_CONNECT_TIMEOUT_SECONDS = 3


class PostgresSettings(BaseSettings):
    """描述数据库工具和后端共同使用的 PostgreSQL 配置。"""

    # PostgreSQL 专用配置和完整后端配置共用同一套环境变量读取规则。
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

    # Windows 上运行的进程通过本机映射端口连接 Docker PostgreSQL。
    postgres_host: str = Field(
        default=DEFAULT_POSTGRES_HOST,
        min_length=1,
    )

    postgres_port: int = Field(
        default=DEFAULT_POSTGRES_PORT,
        ge=1,
        le=65535,
    )

    postgres_database: str = Field(
        default=DEFAULT_POSTGRES_DATABASE,
        min_length=1,
    )

    # 应用和迁移目前使用受限账号 cfi_app，不使用数据库超级用户。
    postgres_username: str = Field(
        default=DEFAULT_POSTGRES_USERNAME,
        min_length=1,
    )

    # 没有默认密码：运行环境必须显式提供 CFI_POSTGRES_PASSWORD。
    postgres_password: SecretStr

    postgres_pool_size: int = Field(
        default=DEFAULT_POSTGRES_POOL_SIZE,
        ge=1,
        le=20,
    )

    postgres_max_overflow: int = Field(
        default=DEFAULT_POSTGRES_MAX_OVERFLOW,
        ge=0,
        le=20,
    )

    postgres_connect_timeout_seconds: int = Field(
        default=DEFAULT_POSTGRES_CONNECT_TIMEOUT_SECONDS,
        ge=1,
        le=30,
    )

    @field_validator("postgres_password")
    @classmethod
    def validate_postgres_password(
        cls,
        value: SecretStr,
    ) -> SecretStr:
        """确保 PostgreSQL 应用密码具有足够的 UTF-8 字节长度。

        Args:
            value: 从运行环境读取并转换为 SecretStr 的数据库密码。

        Returns:
            SecretStr: 通过最小字节长度校验的原始密码对象。

        Raises:
            ValueError: 密码经过 UTF-8 编码后不足 32 字节。
        """

        password_bytes = value.get_secret_value().encode("utf-8")

        if len(password_bytes) < 32:
            raise ValueError("PostgreSQL 应用密码的 UTF-8 编码长度必须至少为 32 字节")

        return value


# 继承 BaseSettings：获得读取运行配置的能力，把环境变量和 .env 作为字段数据来源
class Settings(PostgresSettings):
    """集中描述完整 FastAPI 后端的运行配置:
    Settings 自己声明 HMAC 和 Redis 字段 +
    继承 PostgresSettings 中的数据库字段 = FastAPI 完整运行配置
    """

    # 打印时遮住内容的字符串，SecretStr：让打印配置对象时只显示星号
    ingest_hmac_secret: SecretStr

    # 失败详情读取接口使用独立令牌，不能与负责CI报告写入认证的HMAC密钥复用。
    failure_read_token: SecretStr

    # 这个属性是整数，没有提供时使用默认值，提供的值必须大于 0
    ingest_max_body_bytes: int = Field(
        default=DEFAULT_INGEST_MAX_BODY_BYTES,
        gt=0,
    )

    # 控制请求 timestamp 与服务器当前时间允许相差的最大秒数。
    ingest_timestamp_tolerance_seconds: int = Field(
        default=DEFAULT_INGEST_TIMESTAMP_TOLERANCE_SECONDS,
        gt=0,
        le=DEFAULT_INGEST_TIMESTAMP_TOLERANCE_SECONDS,
    )

    # 同一个安全周期既控制 nonce 在 Redis 中的保存时间，
    # 也控制 Redis 新进程允许接收 CI 请求前必须连续运行的时间。
    ingest_nonce_ttl_seconds: int = Field(
        default=DEFAULT_INGEST_NONCE_TTL_SECONDS,
        ge=MINIMUM_INGEST_NONCE_TTL_SECONDS,
    )

    redis_host: str = Field(default=DEFAULT_REDIS_HOST, min_length=1)

    # Redis端口必须位于合法的TCP端口范围。
    redis_port: int = Field(
        default=DEFAULT_REDIS_PORT,
        ge=1,
        le=65535,
    )

    # 使用Compose中创建的受限ACL用户，不使用default用户。
    redis_username: str = Field(
        default=DEFAULT_REDIS_USERNAME,
        min_length=1,
    )

    # Redis密码必须由运行环境提供，并在普通打印中遮蔽真实内容。
    redis_password: SecretStr

    # 第一版只使用Redis逻辑数据库0。
    redis_database: int = Field(
        default=DEFAULT_REDIS_DATABASE,
        ge=0,
    )

    # 限制建立Redis TCP连接的最长等待时间。
    redis_connect_timeout_seconds: float = Field(
        default=DEFAULT_REDIS_CONNECT_TIMEOUT_SECONDS,
        gt=0,
        le=10,
    )

    # 限制单条Redis命令等待响应的最长时间。
    redis_socket_timeout_seconds: float = Field(
        default=DEFAULT_REDIS_SOCKET_TIMEOUT_SECONDS,
        gt=0,
        le=10,
    )

    # 限制当前后端进程的Redis连接池规模。
    redis_max_connections: int = Field(
        default=DEFAULT_REDIS_MAX_CONNECTIONS,
        ge=1,
        le=100,
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

    @field_validator("failure_read_token")
    @classmethod
    def validate_failure_read_token(
        cls,
        value: SecretStr,
    ) -> SecretStr:
        """确保失败详情读取令牌具有足够的UTF-8字节长度。

        Args:
            value: 从运行环境读取并转换为SecretStr的读取令牌。

        Returns:
            SecretStr: 通过最小字节长度校验的原始令牌对象。

        Raises:
            ValueError: 令牌经过UTF-8编码后不足32字节。
        """
        token_bytes = value.get_secret_value().encode("utf-8")

        if len(token_bytes) < 32:
            raise ValueError("失败详情读取令牌的UTF-8编码长度必须至少为32字节")

        return value

    # Redis密码使用独立校验器，错误信息可以准确指出具体配置。
    @field_validator("redis_password")
    @classmethod
    def validate_redis_password(
        cls,
        value: SecretStr,
    ) -> SecretStr:
        """确保 Redis 密码具有足够的 UTF-8 字节长度。

        Args:
            value: 从运行环境读取并转换为 SecretStr 的 Redis 密码。

        Returns:
            SecretStr: 通过最小字节长度校验的原始密码对象。

        Raises:
            ValueError: 密码经过 UTF-8 编码后不足 32 字节。
        """

        password_bytes = value.get_secret_value().encode("utf-8")

        if len(password_bytes) < 32:
            raise ValueError("Redis 密码的 UTF-8 编码长度必须至少为 32 字节")

        return value
