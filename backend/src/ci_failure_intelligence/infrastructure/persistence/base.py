"""定义所有 PostgreSQL ORM 模型共享的 Declarative Base。"""

# MetaData 保存项目全部表、列和约束的结构信息。
from sqlalchemy import MetaData

# DeclarativeBase 让继承它的 Python 类能够声明为 SQLAlchemy ORM 模型。
from sqlalchemy.orm import DeclarativeBase

# 统一生成约束和索引名称，让数据库错误和 Alembic 迁移具有稳定名称。
CONSTRAINT_NAMING_CONVENTION = {
    # ix 表示普通索引；名称包含索引首列的标签。
    "ix": "ix_%(column_0_label)s",
    # uq 表示唯一约束；默认名称包含表名和约束首列。
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    # ck 表示 CHECK 约束；名称包含表名和模型提供的规则名称。
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    # fk 表示外键；名称包含当前表、当前列和被引用表。
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    # pk 表示主键；每张表使用表名生成稳定主键名称。
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    """为全部 ORM 模型提供共享 MetaData 和约束命名规则。"""

    # 所有继承 Base 的模型都会把表结构注册到这个共享对象。
    metadata = MetaData(
        naming_convention=CONSTRAINT_NAMING_CONVENTION,
    )
