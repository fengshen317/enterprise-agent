"""预置的公开订阅源，地址由服务端统一维护。"""

RSS_SOURCE_PRESETS: tuple[dict[str, str], ...] = (
    {"name": "OpenAI 官方动态", "category": "官方动态", "url": "https://openai.com/news/rss.xml"},
    {"name": "Google AI 官方博客", "category": "官方动态", "url": "https://blog.google/technology/ai/rss/"},
    {"name": "Hugging Face 社区博客", "category": "模型社区", "url": "https://huggingface.co/blog/feed.xml"},
    {
        "name": "LangGraph 版本发布",
        "category": "开源发布",
        "url": "https://github.com/langchain-ai/langgraph/releases.atom",
    },
    {
        "name": "Deep Agents 版本发布",
        "category": "开源发布",
        "url": "https://github.com/langchain-ai/deepagents/releases.atom",
    },
    {
        "name": "Anthropic · Claude Code",
        "category": "厂商官方项目",
        "url": "https://github.com/anthropics/claude-code/releases.atom",
        "description": "Claude Code 的版本和更新说明，覆盖该产品的发布动态。",
    },
    {
        "name": "xAI · Python SDK",
        "category": "厂商官方项目",
        "url": "https://github.com/xai-org/xai-sdk-python/releases.atom",
        "description": "xAI 官方 Python SDK 版本发布，适合关注 API 接入变化。",
    },
    {
        "name": "DeepSeek · Harness",
        "category": "厂商官方项目",
        "url": "https://github.com/deepseek-ai/deepseek-harness/releases.atom",
        "description": "DeepSeek 官方 Agent 框架的版本发布，覆盖 Harness 项目动态。",
    },
    {
        "name": "豆包相关 · 火山引擎 SDK",
        "category": "厂商官方项目",
        "url": "https://github.com/volcengine/volcengine-python-sdk/releases.atom",
        "description": "包含方舟与豆包 API 接入相关组件，也会包含其他火山引擎服务的 SDK 更新。",
    },
    {
        "name": "Kimi · Kimi Code",
        "category": "厂商官方项目",
        "url": "https://github.com/MoonshotAI/kimi-code/releases.atom",
        "description": "Moonshot AI 官方 Kimi Code 的版本与更新说明。",
    },
    {
        "name": "X / Twitter · 账号动态",
        "category": "社交平台",
        "url": "",
        "description": "待配置：用可访问的 RSSHub 服务生成指定账号的订阅地址，再填到下方自定义订阅。"
        "回复、转发等内容可能需要在 RSSHub 侧配置凭据。",
    },
)
