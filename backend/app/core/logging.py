"""JSON 日志与请求关联。"""

import json
import logging
from datetime import UTC, datetime


class JsonFormatter(logging.Formatter):
    """输出可检索的结构化日志，不记录请求体和连接凭证。"""

    def format(self, record: logging.LogRecord) -> str:
        """将日志转换为 JSON。

        Args:
            record: 日志记录。

        Returns:
            JSON 字符串。
        """
        fields: dict[str, object] = {
            "timestamp": datetime.now(UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "event": record.getMessage(),
        }
        for key in ("request_id", "method", "path", "status_code", "duration_ms", "error_type", "run_id"):
            if hasattr(record, key):
                fields[key] = getattr(record, key)
        return json.dumps(fields, ensure_ascii=False)


def configure_logging(level: str) -> None:
    """配置本应用独立日志处理器，避免重复安装。

    Args:
        level: 日志级别。
    """
    handler = logging.StreamHandler()
    handler.setFormatter(JsonFormatter())
    logger = logging.getLogger("app")
    logger.handlers.clear()
    logger.addHandler(handler)
    logger.setLevel(level)
    logger.propagate = False
