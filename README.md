# Execution as a Service

A Docker container that exposes an HTTP API for executing plans end-to-end. It supports two input formats: RMPL programs (native Kirk format) and PDDL domain/problem/plan triples (converted via [pddl_to_sp](pddl_to_sp/)). In both cases, planning runs through [Kirk](enterprise/kirk-v2/) and dispatch runs through [PyKirk](pykirk/).

## Architecture

```
Client
  │
  ├── POST /execute          (RMPL program text)
  │
  └── POST /execute-pddl     (PDDL domain + problem + temporal plan)
              │
              ▼
       pddl_to_sp  (Python, in-process)
       Converts PDDL inputs to a state plan JSON.
              │
              ▼
server.py  (FastAPI, port 8000)
  │
  ├─► kirk serve  (port 7000, internal)
  │     Lisp binary compiled from enterprise/kirk-v2.
  │     POST /plan                — accepts RMPL text, returns scheduled state plan JSON.
  │     POST /plan-from-state-plan — accepts state plan JSON, runs the Kirk planner,
  │                                  returns a new scheduled state plan JSON.
  │
  └─► PyKirk dispatcher  (port 9000, internal)
        Accepts a plan JSON at POST /plans and drives execution
        via a local agent (9001) and oracle (9002).
```

On container startup, `server.py` launches all four internal services and waits for them to become ready before accepting requests.

**RMPL path** (`POST /execute`) — two steps:

1. **Plan** — the RMPL program is forwarded to `kirk serve` at `POST /plan`. Kirk generates a scheduled state plan and returns it as JSON.
2. **Dispatch** — the plan JSON is forwarded to the PyKirk dispatcher at `POST /plans`.

**PDDL path** (`POST /execute-pddl`) — three steps:

1. **Convert** — the PDDL domain, problem, and temporal plan are passed to `pddl_to_sp` (in-process), which produces a state plan JSON.
2. **Plan** — the state plan JSON is forwarded to `kirk serve` at `POST /plan-from-state-plan`. Kirk deserializes it, runs the planner, and returns a new scheduled state plan JSON.
3. **Dispatch** — the plan JSON is forwarded to the PyKirk dispatcher at `POST /plans`.

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
3. Copies the `pddl_to_sp` converter module.
4. Installs the FastAPI wrapper dependencies.
5. Sets the entrypoint to `start.sh`, which launches `uvicorn server:app`.

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
| `KIRK_BINARY`       | `/app/kirk/kirk`    | Path to the Kirk executable     |
| `PYKIRK_DIR`        | `/app/pykirk`       | Path to the pykirk source tree  |
| `PDDL_TO_SP_DIR`    | `/app/pddl_to_sp`   | Path to the pddl_to_sp module   |
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

### `POST /execute-pddl`

Submit a PDDL problem for planning and execution. Accepts multipart form data with three fields:

| Field     | Type        | Description                                      |
|-----------|-------------|--------------------------------------------------|
| `domain`  | file upload | PDDL domain file (durative actions)              |
| `problem` | file upload | PDDL problem file (objects, init, goal)          |
| `plan`    | text field  | Temporal plan — one `START: action(args) [DUR]` line per action |

The temporal plan format follows the standard PDDL temporal planner output, e.g.:
```
0.0: load(package1, truck1, city1) [1.0]
1.0: drive(truck1, city1, city2) [3.0]
4.0: unload(package1, truck1, city2) [1.0]
```

**Response** — `202 Accepted` with `{"status": "dispatched", ...}` on success.

```bash
curl -X POST http://localhost:8000/execute-pddl \
     -F "domain=@my_domain.pddl" \
     -F "problem=@my_problem.pddl" \
     -F "plan=0.0: load(package1, truck1, city1) [1.0]
1.0: drive(truck1, city1, city2) [3.0]
4.0: unload(package1, truck1, city2) [1.0]"
```

**Processing steps:**
1. `pddl_to_sp` parses the domain and problem files and the temporal plan text, then assembles a state plan JSON (schema version `0.4-0`).
2. The state plan JSON is sent to Kirk's `POST /plan-from-state-plan` endpoint, which deserializes it, runs the planner, and returns a new scheduled state plan.
3. The scheduled state plan is dispatched to PyKirk via `POST /plans`.

### `GET /health`

Returns the liveness of the server and all internal services.

```bash
curl http://localhost:8000/health
# {"status": "ok", "services": {"kirk": "ok", "dispatcher": "ok", "agent": "ok", "oracle": "ok"}}
```

## Submodules

- [enterprise/](enterprise/) — Common Lisp source for Kirk. The planner is built from `enterprise/kirk-v2/`.
- [pykirk/](pykirk/) — Python dispatch layer. See `pykirk/scripts/demo.sh` for a standalone usage example.
- [pddl_to_sp/](pddl_to_sp/) — Python module that converts a PDDL domain, problem, and temporal plan into a state plan JSON compatible with Kirk's `POST /plan-from-state-plan` endpoint.
