"""本机交互式账号管理，密码不通过命令行参数或日志传递。"""

import argparse
import asyncio

from app.core.config import Settings
from app.core.database import Database
from app.core.errors import ApplicationError
from app.services.accounts import AccountManager


async def execute(arguments: argparse.Namespace, password: str) -> None:
    """执行命令并始终释放连接池。

    Args:
        arguments: 命令与非敏感参数。
        password: 在本机终端输入的口令。
    """
    database = Database(Settings())
    manager = AccountManager(database.session_factory)
    try:
        if arguments.command == "claim-legacy":
            count = await manager.claim(arguments.username, arguments.workspace)
            print(f"历史对话认领完成：{count} 条")
        elif arguments.command == "reset-password":
            await manager.reset_password(arguments.username, password)
            print("密码已重置，此前会话已失效")
        else:
            _, workspace_id, count = await manager.create(
                arguments.username,
                password,
                arguments.display_name or ("管理员" if arguments.command == "init" else arguments.username),
                slug=arguments.workspace,
                workspace_name=arguments.workspace_name,
                role=arguments.role,
                initialize=arguments.command == "init",
                claim_legacy=arguments.claim_legacy,
            )
            print(f"账号已创建：{arguments.username}；工作空间：{workspace_id}；认领历史：{count} 条")
    finally:
        await database.close()


def main() -> int:
    """解析参数并交互输入口令，拒绝不一致的确认值。

    Returns:
        命令退出码。
    """
    parser = argparse.ArgumentParser(description="序列本机账号管理")
    parser.add_argument("command", choices=("init", "add-user", "claim-legacy", "reset-password"))
    parser.add_argument("--username", default="admin")
    parser.add_argument("--display-name", default="")
    parser.add_argument("--workspace", default="default")
    parser.add_argument("--workspace-name", default="默认工作空间")
    parser.add_argument("--role", choices=("admin", "member"), default="member")
    parser.add_argument("--claim-legacy", action="store_true")
    arguments = parser.parse_args()
    password = ""
    try:
        if arguments.command != "claim-legacy":
            password = input("请输入密码（明文显示）：")
            if password != input("请再次输入密码："):
                print("两次密码不一致，未修改数据")
                return 2
        asyncio.run(execute(arguments, password))
        return 0
    except ApplicationError as exc:
        print(f"{exc.code}：{exc.message}")
        return 1
    except (EOFError, KeyboardInterrupt):
        print("已取消，未继续执行")
        return 2
    except Exception as exc:
        print(f"操作失败（{type(exc).__name__}），请检查数据库和迁移状态")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
