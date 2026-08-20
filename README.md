# istio-ossm-egress-gw-demo

## Running the API locally

The demo API (`api/app.py`) is a small FastAPI app. To run it locally:

```bash
cd api
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
uvicorn app:app --reload
```

The server listens on `http://127.0.0.1:8000` by default. Available endpoints:

- `GET /` — returns hostname, current UTC time, and a status message
- `GET /headers` — echoes back the request headers it received

Example calls with curl:

```bash
# Basic request
curl http://127.0.0.1:8000/

# Send a custom header and see it echoed back
curl -H "X-Custom-Test: hello" http://127.0.0.1:8000/headers

# Send multiple headers
curl -H "X-Custom-Test: hello" -H "X-Another-Header: world" http://127.0.0.1:8000/headers
```

## Running in a container locally

The `api/Dockerfile` builds the same app on a Red Hat UBI9 Python 3.11 base image (the same image OpenShift's S2I Python builder uses). To build and run it locally with Podman or Docker:

```bash
cd api
podman build -t poc-api:local .
podman run --rm -p 8080:8080 poc-api:local
```

(swap `podman` for `docker` if that's your runtime)

The container listens on port 8080 (unprivileged, matching OpenShift's default non-root security context). Test it the same way as the local venv version, just on port 8080:

```bash
curl http://127.0.0.1:8080/
curl -H "X-Custom-Test: hello" http://127.0.0.1:8080/headers
```

