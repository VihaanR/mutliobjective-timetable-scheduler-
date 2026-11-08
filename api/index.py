import os
import sys
import traceback
from fastapi import FastAPI
from fastapi.responses import JSONResponse

app = FastAPI()

load_error = None
try:
    root_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    if root_dir not in sys.path:
        sys.path.insert(0, root_dir)

    from webapp.server import app as real_app
    app = real_app
except Exception as exc:
    load_error = {
        "status": "failed_import",
        "error": str(exc),
        "error_type": type(exc).__name__,
        "traceback": traceback.format_exc(),
        "sys_path": sys.path,
    }

if load_error is not None:
    @app.api_route("/{path:path}", methods=["GET", "POST", "PUT", "DELETE", "HEAD", "OPTIONS"])
    def catch_all_error(path: str = ""):
        return JSONResponse(status_code=500, content=load_error)
