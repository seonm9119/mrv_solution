import os

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.ai_analysis.router import router as ai_analysis_router
from app.report.router import router as report_router


SERVICE_NAME = os.environ.get("SERVICE_NAME", "mrv-solution-api")
SERVICE_VERSION = os.environ.get("SERVICE_VERSION", "0.1.0")
DEFAULT_CORS_ALLOW_ORIGINS = "http://localhost:3000,http://127.0.0.1:3000,null"


def get_cors_allow_origins():
    origins_text = os.environ.get("MRV_CORS_ALLOW_ORIGINS", DEFAULT_CORS_ALLOW_ORIGINS)
    return [origin.strip() for origin in origins_text.split(",") if origin.strip()]

app = FastAPI(
    title=SERVICE_NAME,
    version=SERVICE_VERSION,
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=get_cors_allow_origins(),
    allow_credentials=False,
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["*"],
)

app.include_router(report_router, prefix="/api")
app.include_router(ai_analysis_router, prefix="/api")


@app.get("/health")
def health():
    return {
        "service": SERVICE_NAME,
        "status": "ok",
    }
