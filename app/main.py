from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import inspect, text

from app import models
from app.database import Base, engine
from app.routers.calls import router as calls_router
from app.routers.leads import router as leads_router
from app.routers.webhooks import router as webhooks_router


Base.metadata.create_all(bind=engine)


def _ensure_runtime_schema() -> None:
    inspector = inspect(engine)
    if not inspector.has_table("leads"):
        return

    columns = {column["name"] for column in inspector.get_columns("leads")}
    if "whatsapp_sent" in columns:
        return

    default = "FALSE" if engine.dialect.name == "postgresql" else "0"
    with engine.begin() as connection:
        connection.execute(
            text(
                "ALTER TABLE leads "
                f"ADD COLUMN whatsapp_sent BOOLEAN NOT NULL DEFAULT {default}"
            )
        )


_ensure_runtime_schema()

app = FastAPI(
    title="voice-lead-tracker",
    version="0.1.0",
    description="Local/internal API for tracking sales leads from voice calls.",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/health", tags=["health"])
def health():
    return {"status": "ok"}


app.include_router(calls_router)
app.include_router(leads_router)
app.include_router(webhooks_router)
