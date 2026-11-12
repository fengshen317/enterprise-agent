"""统一异常与公开错误响应。"""

import logging

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException

logger = logging.getLogger(__name__)


class ApplicationError(Exception):
    """具有稳定错误码的业务异常。"""

    def __init__(self, code: str, message: str, status_code: int = 400) -> None:
        """记录可公开的错误信息。

        Args:
            code: 稳定错误码。
            message: 可向用户展示的说明。
            status_code: HTTP 状态码。
        """
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code


def error_response(request: Request, code: str, message: str, status_code: int) -> JSONResponse:
    """生成不泄露内部异常细节的错误响应。

    Args:
        request: 当前请求。
        code: 稳定错误码。
        message: 公开说明。
        status_code: HTTP 状态码。

    Returns:
        带请求标识的 JSON 错误响应。
    """
    return JSONResponse(
        status_code=status_code,
        content={
            "error": {"code": code, "message": message},
            "request_id": request.state.request_id,
        },
        headers={"X-Request-ID": request.state.request_id},
    )


def register_error_handlers(app: FastAPI) -> None:
    """注册接口异常处理器。

    Args:
        app: FastAPI 应用。
    """
    app.add_exception_handler(ApplicationError, handle_application_error)
    app.add_exception_handler(HTTPException, handle_http_error)
    app.add_exception_handler(RequestValidationError, handle_validation_error)
    app.add_exception_handler(Exception, handle_unexpected_error)


async def handle_application_error(request: Request, exc: Exception) -> JSONResponse:
    """处理业务异常。

    Args:
        request: 当前请求。
        exc: 业务异常。

    Returns:
        统一错误响应。
    """
    assert isinstance(exc, ApplicationError)
    return error_response(request, exc.code, exc.message, exc.status_code)


async def handle_http_error(request: Request, exc: Exception) -> JSONResponse:
    """统一路由不存在等框架错误。

    Args:
        request: 当前请求。
        exc: HTTP 异常。

    Returns:
        统一错误响应。
    """
    assert isinstance(exc, HTTPException)
    return error_response(request, f"HTTP_{exc.status_code}", "请求路径或方法不可用", exc.status_code)


async def handle_validation_error(request: Request, exc: Exception) -> JSONResponse:
    """返回参数校验错误，不回显用户敏感输入。

    Args:
        request: 当前请求。
        exc: 参数校验异常。

    Returns:
        统一校验失败响应。
    """
    return error_response(request, "VALIDATION_ERROR", "请求参数不符合接口要求", 422)


async def handle_unexpected_error(request: Request, exc: Exception) -> JSONResponse:
    """隐藏未预期异常细节，并保留关联标识。

    Args:
        request: 当前请求。
        exc: 未预期异常。

    Returns:
        统一内部错误响应。
    """
    logger.error("unexpected_error", extra={"request_id": request.state.request_id, "error_type": type(exc).__name__})
    return error_response(request, "INTERNAL_ERROR", "服务暂时不可用", 500)
