from pathlib import Path

import uvicorn
from dotenv import load_dotenv

if __name__ == "__main__":
    load_dotenv(Path(__file__).resolve().parent.parent / ".env")
    from app.config import config_value

    address = str(config_value("server", "address", "127.0.0.1:8080", env_name="FUND_COMPASS_API_ADDR"))
    host, port = address.rsplit(":", 1)
    uvicorn.run("app.main:app", host=host, port=int(port), reload=False)
