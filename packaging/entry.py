"""独立二进制的入口（PyInstaller）。和 `matrix-connect` 命令是同一个 main。"""
from matrix.connector.main import main

if __name__ == "__main__":
    main()
