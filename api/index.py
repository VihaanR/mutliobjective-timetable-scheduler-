import os
import sys

app = None

root_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if root_dir not in sys.path:
    sys.path.insert(0, root_dir)

try:
    from webapp.server import app as real_app
    app = real_app
except Exception as exc:
    import traceback
    from fastapi import FastAPI
    from fastapi.responses import JSONResponse

    tb = traceback.format_exc()
    fallback_app = FastAPI()

    @fallback_app.api_route("/{path:path}", methods=["GET", "POST", "PUT", "DELETE", "HEAD", "OPTIONS"])
    def err(path: str = ""):
        return JSONResponse(
            status_code=500,
            content={
                "error": str(exc),
                "traceback": tb,
                "root": root_dir,
                "files": os.listdir(root_dir) if os.path.exists(root_dir) else [],
            },
        )

    app = fallback_app
