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

