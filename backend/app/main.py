import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from . import __version__
from .api import cicd, incidents, platform, webhooks
from .config import get_settings
from .db import init_db

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logging.getLogger("httpx").setLevel(logging.WARNING)
settings = get_settings()


@asynccontextmanager
async def lifespan(_app: FastAPI):
    from .seed import seed_all

    init_db()
    seed_all(history=settings.seed_demo_data and not settings.is_live)
    yield


app = FastAPI(
    lifespan=lifespan,
    title="DevNexa API",
    version=__version__,
    description="Agentic DevOps Intelligence Platform: multi-agent CI/CD, telemetry-based incident diagnosis "
                "with LLM + RAG, graded remediation with verification, and feedback into test prioritisation.",
)
app.add_middleware(
    CORSMiddleware,
    allow_origins=[o.strip() for o in settings.cors_origins.split(",")],
    allow_methods=["*"],
    allow_headers=["*"],
)
for r in (platform.router, cicd.router, incidents.router, webhooks.router):
    app.include_router(r)

