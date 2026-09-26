import sys
from pathlib import Path

import uvicorn
from dotenv import load_dotenv

if __name__ == "__main__":
    # Windows 上 stdout/stderr 重定向到文件时默认用 ANSI 编码（简体中文系统是
    # GBK），中文日志落盘后按 UTF-8 读全是乱码（“已从数据库装载…”变 ``��``）。
    # uvicorn 的 handler 绑定的是同一批流对象，在 run 之前 reconfigure 即可。
    # errors="replace"：编码不了的单字符不值得让整条日志消失。
    for _stream in (sys.stdout, sys.stderr):
        if hasattr(_stream, "reconfigure"):
            _stream.reconfigure(encoding="utf-8", errors="replace")

    load_dotenv(Path(__file__).resolve().parent.parent / ".env")
    from app.config import config_value

    address = str(config_value("server", "address", "127.0.0.1:8080", env_name="FUND_COMPASS_API_ADDR"))
    host, port = address.rsplit(":", 1)
    uvicorn.run("app.main:app", host=host, port=int(port), reload=False)
