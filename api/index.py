import os
import sys
import traceback

root_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if root_dir not in sys.path:
    sys.path.insert(0, root_dir)

try:
    from webapp.server import app
except Exception as e:
    from fastapi import FastAPI
    from fastapi.responses import JSONResponse
    app = FastAPI()
    err_tb = traceback.format_exc()

    @app.api_route("/{full_path:path}", methods=["GET", "POST", "PUT", "DELETE", "HEAD", "OPTIONS"])
    def error_handler(full_path: str = ""):
        return JSONResponse(
            status_code=500,
            content={
                "error": str(e),
                "traceback": err_tb,
                "root_dir": root_dir,
                "listdir_root": os.listdir(root_dir) if os.path.exists(root_dir) else "missing",
                "sys_path": sys.path,
            }
        )
