"""兼容旧命令，转为同时启动前后端。"""

from start import main

if __name__ == "__main__":
    raise SystemExit(main())
