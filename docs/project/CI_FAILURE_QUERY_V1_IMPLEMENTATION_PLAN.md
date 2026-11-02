# CI 失败详情查询 v1 实施计划

状态：`MANUALLY_VERIFIED`

最后更新：2026-11-02

## 交付目标

在不修改数据库表的前提下，提供受独立读取令牌保护的
`GET /api/v1/ci-runs/{event_id}/failures`，读取一个已保存事件的解析状态和有序失败
明细。

本批不实现事件发现、仓库列表、指纹、历史关联、向量检索、AI 诊断或 Dashboard。

## 协作分工

- 用户编写生产代码并执行关键手工验证。
- AI 负责每批代码设计预讲、必要的自动化测试、结果分析和文档同步。
- 未经用户对具体文件明确授权，AI 不代写生产代码。

## Task 1：响应契约

生产文件：

- 新增 `backend/src/ci_failure_intelligence/schemas/ci_failure_query.py`。

测试文件：

- 新增 `backend/tests/test_ci_failure_query_schema.py`。

步骤：

1. AI 先解释响应字段、嵌套关系、时间类型和解析状态语义。
2. 用户定义失败明细响应模型和事件查询响应模型。
3. AI 编写测试，固定 `pending / parsed / failed` 与空明细的序列化契约。
4. 运行定向 pytest、Ruff 和 Pyright。

验收：响应模型可以稳定输出设计文档中的 JSON 形状，不包含 ORM 对象或数据库逻辑。

## Task 2：只读查询服务

生产文件：

- 新增 `backend/src/ci_failure_intelligence/service/ci_failure_query.py`。

测试文件：

- 新增 `backend/tests/test_ci_failure_query.py`。

步骤：

1. AI 解释 SQLAlchemy 两次查询、结果映射、排序和异常归属。
2. 用户实现按主键读取事件、按位置读取明细的纯查询服务。
3. 用户定义事件不存在和查询暂时不可用两种业务异常。
4. AI 编写测试，覆盖有序明细、三种解析状态、404 来源和 SQLAlchemy 异常翻译。
5. 运行定向 pytest、Ruff 和 Pyright。

验收：服务不依赖 FastAPI，不提交事务，不触发解析，不修改任何数据库字段。

## Task 3：读取令牌配置与认证依赖

生产文件：

- 修改 `backend/src/ci_failure_intelligence/core/config.py`。
- 修改 `backend/src/ci_failure_intelligence/api/dependencies.py`。
- 修改 `.env.example`。
- 用户在本机 `.env` 增加真实读取令牌，该文件不得进入 Git。

测试文件：

- 修改 `backend/tests/conftest.py`。
- 修改 `backend/tests/test_config.py`。
- 新增或修改依赖测试，固定 Bearer 认证契约。

步骤：

1. AI 解释 Bearer 请求头、`SecretStr` 和恒定时间比较各自解决的问题。
2. 用户增加必填读取令牌配置并同步本机 `.env`。
3. 用户实现读取认证依赖。
4. AI 更新测试配置并覆盖缺失、格式错误、错误令牌和正确令牌。
5. 运行配置、依赖、应用工厂回归以及 Ruff、Pyright。

验收：错误凭据统一得到 401，日志和响应不包含真实令牌；现有应用和测试仍可创建
`Settings`。

## Task 4：HTTP 路由和异常映射

生产文件：

- 新增 `backend/src/ci_failure_intelligence/api/routes/ci_failure_query.py`。
- 修改 `backend/src/ci_failure_intelligence/api/router.py`。
- 修改 `backend/src/ci_failure_intelligence/api/exception_handlers.py`。

测试文件：

- 新增 `backend/tests/test_ci_failure_query_api.py`。

步骤：

1. AI 按应用启动和请求运行顺序解释路由注册、依赖解析、查询调用和响应序列化。
2. 用户注册 GET 路由并调用查询服务。
3. 用户为事件不存在和查询不可用增加 404、503 映射。
4. AI 编写 HTTP 契约测试，覆盖 200、401、404、422、503，并证明认证失败时查询服务
   不执行。
5. 运行定向测试和完整后端回归。

验收：接口只读且受保护；不依赖内部校验顺序，只保证无有效令牌为 401、持有效令牌
但路径参数非法为 422。

## Task 5：真实链路手工验收

步骤：

1. 用户启动 Redis、PostgreSQL 和 Uvicorn。
2. 用户通过现有发送脚本投递包含真实 failure 的 JUnit XML。
3. 用户运行现有 pending CLI。
4. 从 PostgreSQL 读取新事件 `event_id`。
5. 使用正确读取令牌调用 GET，确认返回 parsed 和真实失败明细。
6. 分别验证错误令牌、未知事件和 parsed 零失败事件。
7. AI 根据真实输出更新状态、验证日志和风险登记。

验收：真实数据完成“接入 → 原始事件 → 解析明细 → 受保护读取”的闭环；没有把
本机验证写成公网 GitHub Runner 已经投递成功。

## 提交边界

建议分为两个业务提交：

1. 响应契约与只读查询服务。
2. 读取认证、HTTP 路由、真实验证和状态文档。

每次提交前必须通过相关定向测试、完整回归、Ruff、Pyright 和 `git diff --check`。
