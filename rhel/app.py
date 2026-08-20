# /opt/poc-api/app.py
from fastapi import FastAPI, Request
import socket, datetime

app = FastAPI()

@app.get("/")
def root():
    return {
        "host": socket.gethostname(),
        "time": datetime.datetime.utcnow().isoformat(),
        "message": "reached via egress gateway",
    }

@app.get("/headers")
def headers(request: Request):
    return dict(request.headers)