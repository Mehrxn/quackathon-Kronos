"""Kronos entry point. Run with: python main.py  (or uvicorn kronos.api.app:app)"""
from __future__ import annotations

import os
from dotenv import load_dotenv
import uvicorn

load_dotenv()

if __name__ == "__main__":
    host = os.environ.get("KRONOS_HOST", "0.0.0.0")
    port = int(os.environ.get("KRONOS_PORT", "8000"))
    uvicorn.run("kronos.api.app:app", host=host, port=port,
                reload=bool(os.environ.get("KRONOS_RELOAD")))
