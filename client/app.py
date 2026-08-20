import os

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

API_FQDN = os.environ.get("API_FQDN", "localhost:8000")
API_SCHEME = os.environ.get("API_SCHEME", "https")
API_BASE_URL = f"{API_SCHEME}://{API_FQDN}"

app = FastAPI()
templates = Jinja2Templates(directory="templates")


async def call_api(path: str):
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            response = await client.get(f"{API_BASE_URL}{path}")
            response.raise_for_status()
            return {"ok": True, "data": response.json()}
    except httpx.HTTPError as exc:
        return {"ok": False, "error": str(exc)}


@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    root_result = await call_api("/")
    headers_result = await call_api("/headers")
    return templates.TemplateResponse(
        request=request,
        name="index.html",
        context={
            "api_base_url": API_BASE_URL,
            "root_result": root_result,
            "headers_result": headers_result,
        },
    )


@app.get("/healthz")
def healthz():
    return {"status": "ok"}
