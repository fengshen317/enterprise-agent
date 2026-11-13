# 知识库权限隔离与 RAGFlow 落地设计

日期：2026-11-11。状态：MVP 已实现；2026-11-12 补充详情页（预览下载/分块浏览/检索测试/解析配置模板/批量重解析）与自动检索注入。文档级 ACL、索引版本和生产加固未实现。目标 RAGFlow：本地 1.0 实例。实现与验收见 [知识库实现与验收](知识库实现与验收.md)。

## 一、结论

采用：

> **应用侧 PostgreSQL 作为权限与元数据主库；RAGFlow 作为文档解析、切分、索引和检索底座；所有 RAGFlow 访问只经 FastAPI 服务端代理。**

不推荐把 RAGFlow 原生团队权限作为第一权限系统，原因：

- 应用已有 User / Workspace / Membership 体系，重复维护两套身份会产生同步与越权风险。
- RAGFlow 1.0 的公开 API 与当前实例已有字段差异，团队/授权 API 是否稳定可用仍需单独验证。
- 检索 API 的可控边界主要是 `dataset_ids` / `document_ids`，应用侧必须自己裁剪。

RAGFlow 原生权限可作为第二层防御，但不是授权事实来源。

## 二、现有基础与约束

- 应用已有 `User`、`Workspace`、`WorkspaceMembership`、`admin/member` 角色。
- 私有对话按 `workspace_id + created_by_user_id` 隔离，管理员不能读取他人对话。
- 问答工具由 `ToolExecutor` 执行，身份来自当前 Run，模型不能传身份。
- 当前迁移 head 为 `0009`；知识库从 `0010` 开始。
- RAGFlow 1.0 已独立运行在 `http://localhost`，应用只通过 HTTP API 访问。
- 项目仍保持“本地单进程应用”边界；RAGFlow 是外部依赖，不随应用进程启动。

## 三、权限模型

### 3.1 主体与边界

| 主体 | 说明 |
|---|---|
| User | 当前登录账号 |
| Workspace | 资源隔离边界 |
| Membership | 用户在空间中的 `admin/member` 角色与状态 |
| KnowledgeBase | 一个知识库对应一个 RAGFlow dataset |
| Document | 知识库中的原始文档 |
| Run | 当前问答运行；工具身份由 Run 绑定 |

所有外部请求都必须先解析为可信 `ResourceScope(user_id, workspace_id)`；模型和前端不能提供 `ragflow_dataset_id`、`ragflow_tenant_id` 或任意用户身份。

### 3.2 知识库可见性

MVP 建议只做两级：

| 可见性 | 检索 | 管理 | 说明 |
|---|---|---|---|
| `workspace` | 当前空间所有有效成员 | 空间管理员 + 创建者 | 默认值 |
| `private` | 仅创建者 | 仅创建者 | 管理员也不能读取 |

规则：

- 管理员管理 `workspace` 知识库，但不能读取他人的 `private` 知识库。
- 成员被停用、空间归档后，不能再创建或检索。
- 删除知识库前必须二次确认；已有对话引用保留，但不再检索。
- 文档级 ACL 放到第二阶段，MVP 不实现。

### 3.3 数据集映射

> **一个应用知识库 = 一个 RAGFlow dataset。**

原因：

- 检索 API 直接支持 `dataset_ids`，权限裁剪天然落在数据集层。
- 每个知识库可独立配置 embedding、parser 和 chunk 参数。
- 删除、重建索引、迁移按知识库边界操作，影响面小。

应用数据库保存 `ragflow_dataset_id`，但不向前端返回；前端只看到应用知识库 UUID。

### 3.4 RAGFlow 原生权限的定位

- MVP：使用一个 RAGFlow 服务账号；应用不向终端用户暴露 RAGFlow UI。
- 所有 dataset 的 `permission` 保持 `me`，只对服务账号可见。
- 应用侧 ACL 是唯一授权入口；RAGFlow 团队权限不参与应用授权。
- 第二阶段可选：一个应用空间对应一个 RAGFlow team/tenant，使用 `permission: team` 和独立 API Key，降低服务账号爆炸半径。前提是 RAGFlow 1.0 的团队/成员/授权 API 经过验证且可稳定管理。

### 3.5 管理员边界

- 空间管理员可以创建、编辑、删除 `workspace` 知识库和文档。
- 空间管理员不能读取或检索他人的 `private` 知识库。
- 如未来需要合规审计，只展示知识库/文档元数据与检索统计，不展示正文。

### 3.6 文档级 ACL（第二阶段）

RAGFlow 检索支持 `document_ids` 过滤，但：

- 文档数量大时请求体过大。
- 先检索后过滤不能作为安全边界。

因此第二阶段采用以下之一：

1. 按 ACL 分组拆分 dataset；
2. 对可访问文档数量设上限后使用 `document_ids` 白名单；
3. 仅对高敏感知识库启用文档级授权。

MVP 不承诺文档级权限。

## 四、数据模型

新增迁移 `0010`，所有表沿用现有命名与约束风格。

### 4.1 knowledge_bases

| 字段 | 类型 | 说明 |
|---|---|---|
| `id` | UUID PK | 应用知识库 ID |
| `workspace_id` | UUID | 空间 |
| `owner_user_id` | UUID | 创建者 |
| `name` | String(100) | 名称 |
| `description` | Text | 描述 |
| `visibility` | String(16) | `workspace` / `private` |
| `status` | String(20) | `creating` / `ready` / `configuring` / `deleting` / `delete_failed` |
| `ragflow_dataset_id` | String(64) unique | RAGFlow dataset ID，不返回前端 |
| `ragflow_tenant_id` | String(64) nullable | 第二阶段按空间隔离时使用 |
| `embedding_model` | String(100) | RAGFlow embedding 模型标识 |
| `parser_id` | String(64) | 例如 `general` |
| `parse_type` | Integer | RAGFlow 1.0 BuiltIn 使用 `1` |
| `parser_config` | JSONB | 完整解析配置快照 |
| `config_version` | Integer | 配置版本 |
| `created_at` / `updated_at` | timestamptz | 时间 |

约束：`(workspace_id, owner_user_id)` 绑定 `workspace_memberships`；`visibility`、`status` 使用 CHECK。

### 4.2 knowledge_documents

| 字段 | 类型 | 说明 |
|---|---|---|
| `id` | UUID PK | 应用文档 ID |
| `kb_id` | UUID | 知识库 |
| `workspace_id` | UUID | 冗余空间，便于权限过滤 |
| `uploaded_by_user_id` | UUID | 上传者 |
| `ragflow_document_id` | String(64) unique | RAGFlow 文档 ID，不返回前端 |
| `name` | String(255) | 文件名 |
| `mime` | String(100) | 类型 |
| `size` | BigInteger | 字节数 |
| `sha256` | String(64) | 内容哈希，用于去重与审计 |
| `status` | String(20) | `uploading` / `uploaded` / `configuring` / `parsing` / `ready` / `failed` / `deleting` / `delete_failed` |
| `error_code` | String(64) | 稳定错误码 |
| `parse_config` | JSONB | 本次实际下发的解析配置快照 |
| `parse_config_version` | Integer | 配置版本 |
| `chunk_count` | Integer | RAGFlow 返回的 chunk 数 |
| `token_count` | Integer | RAGFlow 返回的 token 数 |
| `created_at` / `updated_at` | timestamptz | 时间 |

唯一约束建议：`(kb_id, sha256)` 可选；如果允许重新上传同一文件，则不设唯一约束，由业务层提示。

### 4.3 检索审计

两种方案：

- **MVP 复用 `tool_calls`**：`tool_name = search_knowledge_base`，`result` 保存 query、KB ID、文档 ID、chunk ID、相似度、错误码，不保存完整正文。
- **第二阶段新增 `knowledge_retrieval_audits`**：run_id、user_id、workspace_id、query_hash、kb_ids、doc_ids、chunk_ids、top_k、threshold、result_count、latency_ms、error_code。

建议 MVP 复用 `tool_calls`，减少迁移；如果后续需要按知识库维度统计，再拆表。

### 4.4 索引版本（第二阶段）

当 embedding 模型变更时，不原地修改 dataset，而是：

- 新建 `knowledge_index_versions`：kb_id、version、ragflow_dataset_id、embedding_model、status。
- 新索引构建完成后切换 active version。
- 旧索引保留一段时间，避免旧引用立即失效。

MVP 只支持一个版本；修改 embedding 模型视为重建知识库。

## 五、RAGFlow 1.0 客户端契约

封装 `RagflowClient`，所有方法返回经过校验的业务对象，不把原始 JSON 直接透传。

| 方法 | 接口 | 关键参数 |
|---|---|---|
| `create_dataset` | `POST /api/v1/datasets` | `name`, `parse_type`, `parser_id`, `parser_config`, `embedding_model`, `language`, `permission` |
| `update_dataset` | `PUT /api/v1/datasets/{id}` | `parse_type`, `parser_id`, `parser_config`, `embedding_model` |
| `upload_document` | `POST /api/v1/datasets/{id}/documents` | multipart `file`；**不传 `parser_config`** |
| `patch_document_parser` | `PATCH /api/v1/datasets/{ds}/documents/{doc}` | `parser_id`, `parse_type`, `parser_config` |
| `apply_dataset_parser_config` | `POST /api/v1/datasets/{ds}/documents/apply-parser-config` | 无 body；只同步配置，不 reparse |
| `parse_documents` | `POST /api/v1/datasets/{ds}/chunks` | `document_ids` |
| `list_documents` | `GET /api/v1/datasets/{ds}/documents` | `page`, `page_size` |
| `delete_documents` | `DELETE /api/v1/datasets/{ds}/documents` | `ids` |
| `retrieve` | `POST /api/v1/retrieval` | `question`, `dataset_ids`, `document_ids?`, `top_k`, `similarity_threshold`, `vector_similarity_weight?`, `keyword?`, `use_kg?`, `rerank_id?`, `page`, `size` |
| `delete_dataset` | `DELETE /api/v1/datasets` | `ids` |

### 5.1 已验证的 1.0 行为

- 创建 dataset 需要 `parse_type` + `parser_id`；`chunk_method`、`description` 会被拒绝。
- 更新 dataset 需要 `parse_type` + `parser_id`。
- 文档解析配置更新必须用 **PATCH**，旧脚本的 PUT 会 404。
- 上传接口的 `parser_config` 字段会被忽略；上传后文档继承 dataset 默认配置。
- 局部 `parser_config` 会深度合并，其他组件保留。
- `apply-parser-config` 会更新已有文档配置，但**不会自动重新解析**。
- 解析状态为 `UNSTART` / `RUNNING` / `COMPLETED` / `FAIL`，不是 `DONE`。
- 检索支持 `dataset_ids` 和 `document_ids` 过滤；未发现任意 metadata 过滤。
- 检索结果包含 `dataset_id`、`document_id`、`document_name`、`chunk_id`、`similarity`、`vector_similarity`、`term_similarity`、`positions`、`content`。

### 5.2 错误与超时

- RAGFlow `code != 0` 映射为 `ApplicationError`：
  - `RAGFLOW_UNAVAILABLE`：连接失败/超时
  - `RAGFLOW_REJECTED`：参数或版本不兼容
  - `RAGFLOW_PARSE_FAILED`：解析失败
  - `RAGFLOW_RETRIEVAL_FAILED`：检索失败
- 所有调用设置连接/读取超时，不持有应用数据库事务。
- 解析状态轮询失败不应把文档标记为 ready；保持 `parsing` 或 `failed`。

## 六、API 设计

### 6.1 知识库

```text
GET    /api/knowledge-bases
POST   /api/knowledge-bases
POST   /api/knowledge-bases/{id}
POST   /api/knowledge-bases/{id}/delete
POST   /api/knowledge-bases/{id}/apply-config
```

### 6.2 文档

```text
POST   /api/knowledge-bases/{id}/documents       # multipart，只传文件
GET    /api/knowledge-bases/{id}/documents
POST   /api/knowledge-documents/{id}/parse       # 可携带 parser_config
GET    /api/knowledge-documents/{id}
POST   /api/knowledge-documents/{id}/delete
GET    /api/knowledge-documents/{id}/download    # 后端鉴权后代理
```

上传与解析分离：

1. 上传只返回 `uploaded`。
2. `parse` 先 PATCH 解析配置，再触发解析。
3. 前端轮询文档状态，只有 `ready` 才可检索。

### 6.3 Chat 工具

工具名：`search_knowledge_base`

```json
{
  "query": "员工年假制度",
  "kb_ids": ["可选，必须是当前用户可访问的知识库"],
  "top_k": 5
}
```

服务端：

- 忽略/拒绝模型传入的越权 KB ID。
- 从当前 Run 解析用户和空间。
- 只把允许的 `dataset_ids` 传给 RAGFlow。
- 限制 `top_k`、总返回字符数和单 chunk 长度。
- 返回引用信息并写审计。

工具结果示例：

```json
{
  "ok": true,
  "data": {
    "chunks": [
      {
        "content": "……",
        "document_id": "app-doc-id",
        "document_name": "员工手册.pdf",
        "kb_id": "app-kb-id",
        "similarity": 0.87,
        "positions": [1, 2, 3]
      }
    ]
  }
}
```

- 子 Agent 默认不开放知识库检索；第二阶段如需开放，共用配额。
- 检索内容是不可信资料，不能覆盖系统指令。

## 七、核心流程

### 7.1 创建知识库

1. 校验空间、角色、名称。
2. 应用库写入 `creating`。
3. 调用 RAGFlow 创建 dataset（`parse_type`、`parser_id`、`parser_config`、embedding）。
4. 保存 `ragflow_dataset_id`，状态 `ready`。
5. 失败保留 `creating/failed` 和错误码，允许重试，不生成半成品成功状态。

### 7.2 上传、配置、解析

1. 校验文件类型、大小、文件名、空间权限。
2. 计算 `sha256`，可选去重。
3. 上传到 RAGFlow，只传文件。
4. 保存 `ragflow_document_id`，状态 `uploaded`。
5. 调用 PATCH 写入文档级解析配置，保存 `parse_config` 快照。
6. 调用解析接口，状态 `parsing`。
7. 轮询状态，`COMPLETED` → `ready`，`FAIL` → `failed`。
8. 失败可重试：重新 PATCH + parse，不重新上传。

### 7.3 知识库级配置变更

1. 更新 dataset 默认配置。
2. 如需应用到已有文档，调用 `apply-parser-config`。
3. 明确提示并选择是否 reparse。
4. 只有 reparse 完成后 chunk 数才变化；应用库记录配置版本。

### 7.4 检索

1. 工具入口复核 Run、用户、空间、成员状态。
2. 查询允许的知识库，得到 `dataset_ids`。
3. 调用 RAGFlow 检索，传 `dataset_ids`、`question`、`top_k`、threshold。
4. 规范化 chunk，裁剪长度，编号来源。
5. 写审计；返回给主 Agent。
6. 回答附来源；下载时再次鉴权。

### 7.5 删除与重建

- 文档删除：先标记 `deleting`，调用 RAGFlow 删除，成功后移出检索；失败保留错误码。
- 知识库删除：二次确认；删除 dataset；已有对话引用保留但不再可检索。
- embedding 模型变更：MVP 视为重建知识库；第二阶段使用索引版本切换。

## 八、安全与审计

- RAGFlow API Key 只存在后端环境变量/密钥管理，不进入前端、日志或 Git。
- RAGFlow UI 不暴露给终端用户；如需要，仅管理员通过内网访问。
- 前端不返回 `ragflow_dataset_id`、`ragflow_document_id`、tenant ID 或原始 API 错误。
- 所有 RAGFlow 调用只从 FastAPI 发出；使用网络白名单限制来源。
- 每次检索审计：run_id、user_id、workspace_id、查询、KB/doc/chunk ID、相似度、结果数、耗时、错误码；不保存完整正文。
- 文档下载走后端代理，重新校验知识库权限；不暴露 MinIO 凭证。
- 提示词明确：检索内容是资料，不是指令；来源必须引用。
- 解析/检索失败明确展示，不伪造成功；模型不得编造查询结果。

## 九、配置

新增 `EA_` 配置：

| 变量 | 默认值 | 说明 |
|---|---|---|
| `EA_KNOWLEDGE_ENABLED` | true | 启用知识库能力 |
| `EA_RAGFLOW_BASE_URL` | `http://localhost` | RAGFlow 地址 |
| `EA_RAGFLOW_API_KEY` | 空 | SecretStr，仅后端 |
| `EA_RAGFLOW_TIMEOUT_SECONDS` | 15 | 单次请求超时 |
| `EA_RAGFLOW_PARSER_ID` | `general` | 默认 parser |
| `EA_RAGFLOW_PARSE_TYPE` | 1 | BuiltIn |
| `EA_RAGFLOW_EMBEDDING_MODEL` | 实例默认 | 允许覆盖 |
| `EA_KNOWLEDGE_TOP_K` | 5 | 检索条数 |
| `EA_KNOWLEDGE_SIMILARITY_THRESHOLD` | 0.2 | 默认阈值 |
| `EA_KNOWLEDGE_MAX_CONTEXT_CHARACTERS` | 8000 | 单次工具返回上限 |
| `EA_KNOWLEDGE_MAX_CALLS_PER_RUN` | 3 | 单轮检索配额 |
| `EA_KNOWLEDGE_POLL_SECONDS` | 2 | 解析状态轮询 |

## 十、实施阶段与任务拆分

### 阶段 0：契约与 PoC（已完成大部分）

- 已验证创建/更新 dataset、上传、PATCH 文档配置、解析、检索隔离、阈值和状态。
- 待补：跨用户/团队隔离、PDF/OCR 质量、失败重试、删除清理、Ingestion Pipeline。

### 阶段 1：后端基础（建议 5–7 天）

1. 迁移 `0010`：知识库、文档、审计字段。
2. `RagflowClient` 与错误/状态映射。
3. 知识库 CRUD 与权限。
4. 文档上传、PATCH 配置、解析、状态轮询、删除、下载。
5. 工具 `search_knowledge_base` 与 `ToolExecutor` 集成。
6. 单元测试 + FakeRagflowClient；可选真实实例契约测试。

### 阶段 2：前端（建议 2–3 天）

1. 知识库列表/详情/创建/编辑。
2. 文档上传、解析状态、失败重试。
3. 解析配置：预设模板 + 高级 JSON（管理员）。
4. 回答中的来源卡片与下载入口。

### 阶段 3：检索质量与权限增强（建议 3–5 天）

1. 固定评测集与召回率记录。
2. 文档级 ACL（按需）。
3. 配置应用与 reparse 流程。
4. 索引版本与 embedding 变更。
5. 备份、恢复、删除对账。

### 阶段 4：生产加固（建议 2–3 周）

1. 每空间 RAGFlow team/API Key（如验证可行）。
2. 监控、限流、超时、重试。
3. 备份恢复演练。
4. 权限越权测试与审计复核。

## 十一、验收标准

- 用户 A 不能列出、检索、下载用户 B 或其他空间的知识库。
- 空间管理员不能读取他人的 `private` 知识库。
- 成员被停用或空间归档后，不能创建/检索。
- 模型无法通过伪造 `kb_ids` / `dataset_ids` 越权。
- 只有 `ready` 文档可检索；失败/解析中文档不返回。
- 删除文档后检索不再返回；删除失败有明确状态。
- 检索结果带可追溯来源；下载再次鉴权。
- 解析配置 PATCH 生效，reparse 后 chunk 数变化可复现。
- `apply-parser-config` 后明确提示需要 reparse。
- RAGFlow 不可用时返回明确错误，不编造结果。
- 所有工具调用有审计记录，且不包含敏感正文。

## 十二、风险与未决问题

| 风险/问题 | 影响 | 处理 |
|---|---|---|
| RAGFlow 1.0 API 与公开文档不一致 | 高 | 锁定版本、RagflowClient 适配、契约测试 |
| 单服务账号爆炸半径大 | 高 | MVP 网络隔离；第二阶段按空间分团队/Key |
| 无任意 metadata 过滤 | 中高 | ACL 落在 dataset/document ID 层 |
| 上传时 parser_config 被忽略 | 中 | 上传后 PATCH，再解析 |
| apply-parser-config 不 reparse | 中 | 明确提示并显式 reparse |
| embedding 变更成本高 | 中 | MVP 重建知识库；第二阶段索引版本 |
| 检索阈值召回不稳定 | 中 | 评测集调参，暴露 threshold/vector weight |
| RAGFlow 部署/备份 | 中高 | 独立部署、备份演练、恢复流程 |

需要确认的决策：

1. 是否接受“单 RAGFlow 服务账号 + 应用侧 ACL”作为 MVP？
2. 空间知识库由管理员创建，普通成员只能建私有知识库，是否合适？
3. 是否需要文档级 ACL？如果需要，是否接受拆分 dataset 的复杂度？
4. 子 Agent 是否允许检索知识库？
5. embedding/抽取 LLM 使用实例现有的 SiliconFlow/DeepSeek，数据边界是否可接受？
6. RAGFlow 是否只允许后端内网访问，不向终端用户开放 UI？

## 十三、PoC 现状

- 临时知识库 `EA-POC-20261111`（`b2dae196d4a44481811ed6b0f63e0a58`）按用户要求保留。
- 该知识库仅用于验证，不应被正式应用引用。
- 已观察：128/256/64 token 分别得到 41/23/82 个 chunk；上传时 `parser_config` 被忽略；PATCH 生效；`apply-parser-config` 不自动 reparse；`dataset_ids` / `document_ids` 检索隔离有效。
