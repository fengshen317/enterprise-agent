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
)
