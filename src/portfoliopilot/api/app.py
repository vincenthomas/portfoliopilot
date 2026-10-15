"""FastAPI surface (Phase 4 fills this in). Health check only for now."""

from fastapi import FastAPI

app = FastAPI(title="PortfolioPilot", version="0.1.0")


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}
