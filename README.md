# CI Failure Intelligence

面向开发和测试团队的 CI 失败历史检索与证据化智能诊断平台。

当前已经完成 GitHub Actions 失败报告发送、安全接入、Redis 防重放、PostgreSQL
事实持久化、JUnit 失败明细解析、事件查询，以及基于 LangChain 与 DeepSeek 的
结构化诊断、数据库缓存和租约控制。

正在交付的最后一条核心产品链路是：使用 FastEmbed 与 Qdrant 检索同仓库历史相似
失败，再由 LangGraph 编排检索、诊断、降级和结果写回，最终通过轻量 Dashboard
展示。Qdrant 与 LangGraph 当前仍是计划能力，不应被视为已经实现。

## 核心原则

- PostgreSQL 是系统事实来源。
- Qdrant 是检索索引，不替代关系型数据库。
- Redis 只承担 nonce 等可丢失的短期运行状态，不保存业务事实。
- AI 只负责基于证据进行归纳与建议。
- 鉴权、幂等、状态、统计和引用真实性由确定性程序负责。

## 当前接口

- `POST /api/v1/ci-runs/ingest`：安全接收 CI 失败报告。
- `GET /api/v1/ci-runs`：列出最近 CI 事件。
- `GET /api/v1/ci-runs/{event_id}/failures`：读取结构化失败证据。
- `POST /api/v1/ci-runs/{event_id}/diagnosis`：使用独立触发凭据生成或复用 AI 诊断。
- `GET /api/v1/ci-runs/{event_id}/diagnosis`：使用读取凭据查询已有诊断及当前任务状态。
- `GET /health/live`：进程存活检查。

## MVP 截止时间

项目按 2026-11-30 的有效开发与部署硬截止日期推进。MVP 必须包含 Qdrant dense
历史检索、LangGraph 真实控制流、轻量 Dashboard 和公网 GitHub Actions 投递；Celery、
sparse/RRF、reranker、多 Agent 和复杂权限体系后移。

