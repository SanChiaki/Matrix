"""Matrix：以 DeepSeek Harness 为运行时的 Agent 授权委托网络（包名 matrix）。"""


def utf8_stdio() -> None:
    """命令行入口一开始调用：输出到管道或文件时（被别的程序读、重定向成日志）用 UTF-8。
    Windows 上 Python 3.14 按区域编码（cp1252 / GBK）写管道，帮助与提示里的中文、全角标点会让 print 抛
    UnicodeEncodeError；Python 3.15 起默认就是 UTF-8，这里让 3.14 一致。控制台不动（走宽字符接口，本来就没问题）；
    pythonw 下没有 stdout / stderr，跳过。"""
    import sys
    for stream in (sys.stdout, sys.stderr):
        if (stream is not None and hasattr(stream, "reconfigure") and not stream.isatty()
                and (stream.encoding or "").lower().replace("-", "") != "utf8"):
            stream.reconfigure(encoding="utf-8", errors="replace")
