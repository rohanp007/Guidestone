from fastapi import FastAPI

app = FastAPI(title="Guidestone")


@app.get("/health")
def health() -> dict:
    return {"status": "ok"}
