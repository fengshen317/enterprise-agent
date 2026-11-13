# 项目开发约定

本文件仅适用于 enterprise-agent 项目。

- 使用中文沟通，代码注释说明原因。修改前阅读上下文。
- 每次代码开发同步更新受影响的技术文档，区分规划与已实现能力。
- 按 docs/项目阶段实施步骤.md 推进，验收证据齐备后再标记完成。
- Python 使用类型注解和 Google 风格 docstring，4 空格缩进，行宽 120。
- FastAPI 路由使用 async def；数据库使用 SQLAlchemy 异步会话，每个并发任务独立会话。
- 前端使用 Vue 3、TypeScript、Composition API 和 script setup；2 空格缩进。
- API 封装在 frontend/src/api/，SCSS 变量在 styles/variables.scss，CSS 使用 BEM。
- 不删除 .env，不提交密钥，不修改任务无关文件。保留 prototype/ 作为产品原型。
- 后端检查：uv run ruff check .、uv run mypy app/、uv run pytest。
- 前端检查：npm run lint、npm run typecheck、npm run test、npm run build。
- 不将原型模拟数据或测试替身作为真实数据库/模型验收证据。
