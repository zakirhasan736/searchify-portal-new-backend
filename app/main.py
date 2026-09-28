from contextlib import asynccontextmanager
import asyncio

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware

from app.auto_sync import auto_sync_loop
from app.config import CORS_ORIGINS, PUBLIC_SITE_URL
from app.database import Base, SessionLocal, engine, ensure_indexes
from app.routers import auth, catalog, crawl, features, google_connect, operator, product, projects, social_auth, workflow
from app.seed import seed


@asynccontextmanager
async def lifespan(_app: FastAPI):
    Base.metadata.create_all(bind=engine)
    ensure_indexes()
    db = SessionLocal()
    try:
        seed(db)
    finally:
        db.close()
    stop = asyncio.Event()
    task = asyncio.create_task(auto_sync_loop(stop))
    try:
        yield
    finally:
        stop.set()
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass


app = FastAPI(title="Searchify API", version="1.0.0", lifespan=lifespan)
app.add_middleware(GZipMiddleware, minimum_size=800)
app.add_middleware(
    CORSMiddleware,
    allow_origins=CORS_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(auth.router)
app.include_router(social_auth.router)
app.include_router(projects.router)
app.include_router(catalog.router)
app.include_router(crawl.router)
app.include_router(workflow.router)
app.include_router(features.router)
app.include_router(google_connect.router)
app.include_router(operator.router)
app.include_router(product.router)


@app.get("/api/v1/health")
def health():
    return {"ok": True, "service": "searchify-api", "version": "v1", "site": PUBLIC_SITE_URL}
