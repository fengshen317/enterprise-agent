# 问答报表与 Python 分析

## 在问答中使用

上传 CSV 或 XLSX 后，可以直接输入「按车间汇总合格数，画柱状图并导出 Excel」。默认助手在部署启用分析工具时可以自动检查并计算数据。自定义 Agent 可绑定「检查报表数据」和「运行 Python 生成报表」。报表在回答正文下方显示图表、明细、来源、计算口径和实际运行的代码。可以继续问「基于上次报表，只看二车间」。需要新的字段或维度时，可以重新读取本对话中的原文件计算。

生成报表属于对话内读取与计算。明确要求导出时，沿用「生成可下载文件」审批。批准后的 CSV 或 Excel 使用持久化报表中的同一份明细，文件出现在原回答及「我的材料」。

## 执行与存储

- `inspect_analysis_data` 列出当前对话最近上传的表格及历史报表。指定 `source_ids` 后由固定脚本读取行数、字段类型及少量样例。CSV 检视支持 UTF-8 和 GB18030。Excel 检视最多报告前 20 个工作表名称并检查前 5 个工作表，实际分析可指定任意工作表。
- `run_python_analysis` 接收逻辑 ID 和 Python 源码。后端按运行身份校验文件或报表归属后，才将原件或上一份结果交给沙箱。源文件按参数顺序指派 `/inputs/input_1.csv`、`/inputs/input_2.xlsx` 等路径。历史报表使用 `.json`，包含 `columns` 和 `rows`。表格预览和检视样例沿用模型上下文，原文件通过部署内的执行器传输。
- 每次调用最多 4 份输入，合计 16 MiB，代码最多 12,000 字符。任务使用独立容器，输入只读，网络关闭，凭证和 Docker socket 留在执行环境外。容器限制为 1 CPU、512 MiB、48 个进程，执行超时默认 30 秒，输出上限 512 KiB。脚本失败或返回结构无效时，工具返回可供模型修正的错误，成功结果才生成报表引用。
- 结果契约包含 `title`、`explanation`、`columns`、`rows` 和 `charts`。每份报表最多 249 行、30 列、4 张图，支持柱状图和折线图。后端检查图表列名及数值，缺失数值用 `null`。Vue 用预定义参数构造 ECharts 图表，文件及标签按文本显示。
- 迁移 `0027` 新增 `chat_reports`，报表绑定成功执行的工具调用 ID 和助手消息。历史及 SSE 快照在 `message.reports` 返回 `{id, title}`。报表正文由 `GET /api/conversations/{conversation_id}/reports/{report_id}` 读取，校验当前用户、空间和对话所有权。每次导出的审批预览及原件由相同 `report_id` 解析。

## 本地开发启用

从项目根目录构建执行镜像，Docker Desktop 需使用 Linux 容器。

```bash
docker build --target runner -f backend/analysis/Dockerfile -t enterprise-agent-report-sandbox:local backend
```

在 `backend/.env` 设置 `EA_REPORT_SANDBOX_IMAGE=enterprise-agent-report-sandbox:local` 后重启后端。`EA_REPORT_SANDBOX_RUNTIME` 可以指定本机已安装的运行时。生产部署使用 [独立执行代理](../../deploy/README.md#启用问答报表分析)。

## 验收

- `tests/test_reports_integration.py` 使用独立 PostgreSQL 验证历史还原、跨对话拒绝、追问复用和审批导出，沙箱执行使用可控替身。
- `tests/test_report_acceptance.py` 在独立测试库和已启用的沙箱上执行真实模型验收。设置 `EA_VERIFY_REAL_REPORT=1` 后运行 `uv run --frozen pytest --no-cov tests/test_report_acceptance.py`。验收表格含 350 行，第二个车间只出现在附件预览上限之后，用于核实实际读取了 Excel 原件。
- 2026-11-24 用真实模型、独立 PostgreSQL 和 Docker Desktop 执行上述验收。报表与重新打开的导出 Excel 均为一车间 600、二车间 250。随后用原对话追问仅显示二车间，新报表只含二车间 250，历史可分别读取两份报表。Docker 代理真实验收覆盖鉴权、执行及连接断开后清理，运行时为 Docker Desktop 默认配置。
- Chromium 使用模拟接口在 1440 和 390 像素宽度验证柱状图、折线图、明细表和刷新恢复。Linux 服务器上的 `runsc` 配置需在部署环境验收。
- 全量后端回归 523 项通过，7 项按运行条件跳过，覆盖率 81.50%。另行启用真实 Docker 测试后，沙箱与代理的 22 项测试全部通过。前端 197 项测试、ESLint、类型检查及构建通过。
