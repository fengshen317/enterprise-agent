"""在低权限进程内调用脚本，异常仅输出类型名和源码行号。"""

import runpy
import sys
import traceback
from pathlib import Path

_PROGRAM = Path("/inputs/.program.py")


def main() -> int:
    """执行分析脚本，并避免在错误路径暴露输入数据。"""
    try:
        runpy.run_path(str(_PROGRAM), run_name="__main__")
    except SystemExit as exc:
        if exc.code is None or exc.code == 0:
            return 0
        return 1
    except BaseException as exc:
        # 模型只需要源码位置和异常分类来修改脚本，原始异常可能带有业务数据。
        if isinstance(exc, SyntaxError):
            line_number = exc.lineno or 0
        else:
            line_number = next(
                (
                    line or 0
                    for frame, line in traceback.walk_tb(exc.__traceback__)
                    if frame.f_code.co_filename == str(_PROGRAM)
                ),
                0,
            )
        print(f"EA_REPORT_ERROR {type(exc).__name__} {line_number}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
