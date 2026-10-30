# CI 失败详情查询 v1

状态：`PLANNED`

最后更新：2026-10-30

## 真实业务场景

CI 接入接口已经能够把原始 JUnit XML 保存到 `ci_failure_events`，管理员运行
pending CLI 后，解析服务会把 `failure` 和 `error` 节点保存到
`ci_failure_details`。但是这些证据目前只能通过 PostgreSQL 查询，HTTP 客户端还
不能读取。

本批提供一个受保护的只读接口。调用方已知事件内部主键 `event_id` 时，可以读取
该事件的运行身份、解析状态、解析错误摘要和失败明细。

```text
CI 报告入库 → event_id=42，parse_status=pending
运行 pending CLI → event_id=42 变成 parsed，写入两条失败明细
GET /api/v1/ci-runs/42/failures
→ 返回事件状态和两条失败证据
```

当前接入响应不返回 `event_id`，项目也没有事件列表接口。因此第一批通过数据库
取得 `event_id` 完成验收，不能把本功能描述成 Dashboard 已经能够发现并点击事件。

## 业务问题与验收目标

本批只解决一个问题：把已经可靠保存和解析的某次 CI 失败，安全地通过 HTTP
读取出来。

完成标准：

- 已授权请求可以按 `event_id` 读取事件状态和有序失败明细。
- 空明细必须结合 `parse_status` 正确解释，不能统一视为“没有失败”。
- 不存在的事件、无效读取凭据和数据库不可用具有稳定且不同的 HTTP 结果。
- GET 请求只读取数据，不触发 XML 解析，也不修改解析状态。

## 方案选择

采用按事件主键查询：

```http
GET /api/v1/ci-runs/{event_id}/failures
```

选择原因：

- `ci_failure_details.event_id` 已经指向原始事件，查询边界与现有数据模型一致。
- 第一版不需要同时设计分页、仓库筛选和跨事件排序。
- 能先证明真实失败证据可以离开数据库，经稳定 HTTP 契约被读取。

暂不采用仓库级失败列表。仓库级列表属于 Dashboard 的事件发现入口，需要独立设计
分页、排序和筛选。

## 读取权限

错误文本和 traceback 可能包含源码路径、参数或其他敏感信息，因此查询接口不能
作为无保护的公网接口。

第一版使用独立共享读取令牌：

```http
Authorization: Bearer <读取令牌>
```

读取令牌与 CI 接入 HMAC 密钥分离：HMAC 密钥证明 Actions 客户端可以写入报告，
读取令牌证明调用方可以查看失败证据。第一版不引入用户表、登录流程、JWT 或 RBAC。

认证依赖必须在数据库查询之前执行。令牌缺失、格式错误或内容不匹配统一返回
HTTP 401，不泄露事件是否存在。接口不承诺 FastAPI 在内部先解析路径参数还是先
执行认证依赖；对外契约只保证：没有有效令牌时返回 401，持有效令牌但
`event_id` 非法时返回 422。

## 响应契约

响应保持扁平和直白：

```json
{
  "event_id": 42,
  "repository_full_name": "owner/repository",
  "run_id": 1001,
  "run_attempt": 1,
  "job_key": "backend_tests",
  "workflow_name": "Backend CI",
  "job_name": "Backend tests (Python 3.12)",
  "commit_sha": "0123456789abcdef0123456789abcdef01234567",
  "head_branch": "main",
  "parse_status": "parsed",
  "parse_error_code": null,
  "parse_error_message": null,
  "parse_finished_at": "2026-10-30T12:00:00Z",
  "failures": [
    {
      "id": 100,
      "testcase_index": 3,
      "issue_index": 0,
      "suite_name": "backend",
      "classname": "tests.test_orders",
      "test_name": "test_create_order",
      "issue_type": "failure",
      "message": "预期 200，实际 500",
      "details": "完整错误文本和堆栈"
    }
  ]
}
```

状态解释：

- `pending` 加空列表：原始报告尚未处理。
- `parsed` 加空列表：报告处理成功，但没有 `failure` 或 `error` 节点。
- `parsed` 加非空列表：报告处理成功并保存了失败证据。
- `failed` 加空列表：报告处理失败，原因由错误代码和限长摘要说明。

明细固定按 `testcase_index`、`issue_index` 升序返回。

明细 `id` 是当前数据库行的内部主键。现有重新解析会整体删除并重建明细，因此
该值可能变化，客户端不得把它当作跨重新解析的稳定身份。当前事件内的位置由
`event_id + testcase_index + issue_index` 表达。

## 模块边界

### `schemas/ci_failure_query.py`

定义事件详情、失败明细及整个响应的 Pydantic 模型。只描述 HTTP 数据形状，不查询
数据库。

### `service/ci_failure_query.py`

输入为已经连接项目 PostgreSQL 的 `AsyncSession` 和正整数 `event_id`。服务先读取
原始事件，再读取并排序所属明细，最后返回与 FastAPI 无关的查询结果。

服务不检查 HTTP 令牌、不触发 XML 解析、不更新数据库。

查询服务负责捕获 SQLAlchemy 数据库异常，并转换为本模块稳定的“查询暂时不可用”
异常。现有写入服务的异常处理器不会自动处理新的查询错误，API 异常处理层必须为
查询异常单独映射 HTTP 503。

### `api/routes/ci_failure_query.py`

注册 GET 路由，声明响应模型，接收路径参数，并通过 FastAPI 依赖取得读取授权和
数据库 Session。路由调用查询服务后返回结果，不直接编写 SQL。

### `api/dependencies.py`

增加读取令牌依赖。依赖从应用配置取得 `SecretStr`，解析 `Authorization` 请求头，
使用恒定时间比较核对令牌。认证失败统一产生 HTTP 401。

### `core/config.py` 与 `.env.example`

增加独立读取令牌配置。真实令牌只来自运行环境，不写入仓库或日志。
由于该令牌是应用必填配置，实施时必须同步更新本机 `.env` 和所有显式构造
`Settings` 的测试配置，避免应用在注册 GET 路由前就因缺少配置启动失败。

## 完整调用链

```text
应用启动
→ Settings 读取读取令牌和现有数据库配置
→ create_app() 注册查询路由和依赖

GET /api/v1/ci-runs/42/failures 到达
→ FastAPI 解析路径参数并解析路由依赖
→ 读取令牌依赖检查 Authorization
  ├─ 缺失或错误：HTTP 401，数据库查询不执行
  └─ 正确：继续
→ 持有效令牌但 event_id 不是正整数：HTTP 422
→ get_database_session() 创建本次请求的 AsyncSession
→ 查询服务读取 ci_failure_events.id=42
  ├─ 不存在：HTTP 404
  └─ 存在：继续
→ 查询服务按 event_id=42 读取 ci_failure_details
→ 按 testcase_index、issue_index 排序
→ Pydantic 响应模型验证并序列化结果
→ HTTP 200
```

## 失败处理

| 情况 | HTTP 结果 | 说明 |
|---|---:|---|
| 读取令牌缺失或错误 | 401 | 不泄露事件是否存在 |
| 持有效令牌，但 `event_id` 不是正整数 | 422 | 在进入查询服务前拒绝，不规定框架内部校验顺序 |
| 事件不存在 | 404 | 读取权限已经通过，但没有该事件 |
| PostgreSQL 无法可靠查询 | 503 | 不返回底层 SQL 或连接细节 |
| 查询成功 | 200 | 返回解析状态和完整有序明细 |

## 小批次实施顺序

1. 定义响应模型和查询结果契约，并由自动化测试固定字段与状态语义。
2. 实现纯读取查询服务，覆盖事件存在、空明细、有序明细、不存在和数据库异常。
3. 加入读取令牌配置与认证依赖，证明认证失败时不会调用查询服务。
4. 注册 GET 路由，完成 HTTP 200、401、404、422 和 503 契约测试。
5. 使用真实失败 XML 完成“HTTP 入库 → pending CLI → GET 查询”手工验收。
6. 更新当前状态、验证日志及发生变化的风险记录。

## 不在本批范围

- failure fingerprint、历史重复判断和失败簇。
- Embedding、Qdrant、DeepSeek、LangChain 和 LangGraph。
- 仓库级事件列表、分页、搜索和 Dashboard。
- GET 请求自动触发 pending 解析。
- 完整用户系统、JWT、RBAC 或多租户权限。
- 修改现有 `202 accepted` 接入响应。
