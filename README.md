# Execution as a Service

A Docker container that exposes an HTTP API for executing RMPL programs end-to-end: planning via [Kirk](enterprise/kirk-v2/) and dispatch via [PyKirk](pykirk/).

## Architecture

```
Client
  │
  │  POST /execute  (RMPL program text)
  ▼
server.py  (FastAPI, port 8000)
  │
  ├─► kirk serve  (port 7000, internal)
  │     Lisp binary compiled from enterprise/kirk-v2.
  │     Accepts RMPL, returns a plan as JSON.
  │
  └─► PyKirk dispatcher  (port 9000, internal)
        Accepts a plan JSON at POST /plans and drives execution
        via a local agent (9001) and oracle (9002).
```

On container startup, `server.py` launches all four internal services and waits for them to become ready before accepting requests. Requests to `POST /execute` are processed in two steps:

1. **Plan** — the RMPL program is forwarded to `kirk serve` at `POST /plan`. Kirk generates a schedule and returns it as JSON.
2. **Dispatch** — the plan JSON is forwarded to the PyKirk dispatcher at `POST /plans`, which drives execution through the local agent and oracle.

## Building

The image uses a two-stage build.

**Stage 1 (`kirk-builder`)** compiles the Kirk binary from Common Lisp source:

1. Installs SBCL, ASDF, and Quicklisp inside a `clfoundation/sbcl` base image.
2. Copies the `enterprise/` source tree into `/common-lisp/enterprise/`.
3. Pre-fetches all Quicklisp dependencies (`build-deps.lisp`).
4. Loads `enterprise/kirk-v2/scripts/dependencies.lisp` (`:adopt`, `:safe-queue`, `:clingon`).
5. Runs `(asdf:make :kirk-v2)` with output translations disabled so the binary lands at `enterprise/kirk-v2/build/kirk/`.

**Stage 2 (`runtime`)** builds the Python runtime:

1. Installs `uv` and the `pykirk` package from source.
2. Copies the compiled Kirk binary bundle from stage 1.
3. Installs the FastAPI wrapper dependencies.
4. Sets the entrypoint to `start.sh`, which launches `uvicorn server:app`.

```bash
# Build (from the repo root)
docker build -t eaas .
```

## Running

```bash
docker run --rm -p 8000:8000 eaas
```

The server is ready when you see all four internal services report as ready in the logs.

### Environment variables

| Variable            | Default | Description                         |
|---------------------|---------|-------------------------------------|
| `KIRK_BINARY`       | `/app/kirk/kirk` | Path to the Kirk executable |
| `PYKIRK_DIR`        | `/app/pykirk`    | Path to the pykirk source tree |
| `KIRK_PORT`         | `7000`  | Internal port for `kirk serve`      |
| `DISPATCHER_PORT`   | `9000`  | Internal port for the dispatcher    |
| `LOCAL_AGENT_PORT`  | `9001`  | Internal port for the local agent   |
| `LOCAL_ORACLE_PORT` | `9002`  | Internal port for the local oracle  |
| `SERVER_PORT`       | `8000`  | External port for the HTTP API      |

## API

### `POST /execute`

Submit an RMPL program for planning and execution.

**Request body** — either:
- Raw RMPL text with `Content-Type: text/plain`
- JSON with `Content-Type: application/json` and an `"rmpl"` key

**Optional header:**
- `X-Package-Name` — RMPL package to plan (default: `main`)

**Response** — `202 Accepted` with `{"status": "dispatched", ...}` on success.

```bash
curl -X POST http://localhost:8000/execute \
     -H "Content-Type: text/plain" \
     --data-binary @my_program.rmpl
```

```bash
curl -X POST http://localhost:8000/execute \
     -H "Content-Type: application/json" \
     -d '{"rmpl": "(define-package main ...)"}'
```

### `GET /health`

Returns the liveness of the server and all internal services.

```bash
curl http://localhost:8000/health
# {"status": "ok", "services": {"kirk": "ok", "dispatcher": "ok", "agent": "ok", "oracle": "ok"}}
```

## Submodules

- [enterprise/](enterprise/) — Common Lisp source for Kirk. The planner is built from `enterprise/kirk-v2/`.
- [pykirk/](pykirk/) — Python dispatch layer. See `pykirk/scripts/demo.sh` for a standalone usage example.
