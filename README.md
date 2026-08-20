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

## Running the client locally

The `client/app.py` is a small FastAPI web app that calls the API and displays the results in a browser. It's configured via environment variables rather than a hardcoded URL, since it's meant to eventually run in a separate cluster from the API:

- `API_FQDN` — hostname of the API to call (required; e.g. `external-api.apps.cluster-bdvkn.bdvkn.sandbox980.opentlc.com`)
- `API_SCHEME` — `http` or `https` (optional, defaults to `https`)

To run it locally against the API running in the cluster:

```bash
cd client
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
API_FQDN=external-api.apps.cluster-bdvkn.bdvkn.sandbox980.opentlc.com uvicorn app:app --reload --port 9000
```

Then open `http://127.0.0.1:9000/` in a browser. The page shows the results of calling the API's `/` and `/headers` endpoints, with a Refresh link to re-run the calls. If the API is unreachable, the page shows an error instead of crashing.

To point it at a locally-running API instead (see above), use `API_FQDN=127.0.0.1:8000 API_SCHEME=http`.

A `/healthz` endpoint is also available for health checks once this runs in a cluster.

