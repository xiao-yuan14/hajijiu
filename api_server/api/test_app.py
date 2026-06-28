from fastapi import FastAPI
import os

app = FastAPI()
PORT = int(os.environ.get("PORT", "8080"))

@app.get("/")
def root():
    return {"message": "Hello from Railway!", "port": PORT}

@app.get("/health")
def health():
    return {"status": "ok"}
