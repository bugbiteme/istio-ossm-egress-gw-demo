import os

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

API_FQDN = os.environ.get("API_FQDN", "localhost:8000")
API_SCHEME = os.environ.get("API_SCHEME", "https")
API_BASE_URL = f"{API_SCHEME}://{API_FQDN}"

# Optional second target, so one client can demonstrate calling multiple
# external systems through the same egress gateway side by side.
API_FQDN_2 = os.environ.get("API_FQDN_2")
API_SCHEME_2 = os.environ.get("API_SCHEME_2", "https")
API_BASE_URL_2 = f"{API_SCHEME_2}://{API_FQDN_2}" if API_FQDN_2 else None

app = FastAPI()
templates = Jinja2Templates(directory="templates")


async def call_api(base_url: str, path: str):
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            response = await client.get(f"{base_url}{path}")
            response.raise_for_status()
            return {"ok": True, "data": response.json()}
    except httpx.HTTPError as exc:
        return {"ok": False, "error": str(exc)}


async def call_target(base_url: str):
    return {
        "api_base_url": base_url,
        "root_result": await call_api(base_url, "/"),
        "headers_result": await call_api(base_url, "/headers"),
    }


@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    targets = [await call_target(API_BASE_URL)]
    if API_BASE_URL_2:
        targets.append(await call_target(API_BASE_URL_2))
    return templates.TemplateResponse(
        request=request,
        name="index.html",
        context={"targets": targets},
    )


@app.get("/healthz")
def healthz():
    return {"status": "ok"}
