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

1. Installs `uv`, Node.js 20 (for the optional visualization), and the `pykirk` package from source.
2. Copies the compiled Kirk binary bundle from stage 1.
3. Copies the `pddl_to_sp` converter module.
4. Pre-installs visualization npm dependencies (`npm install` in `pykirk/visualization/`).
5. Installs the FastAPI wrapper dependencies.
6. Sets the entrypoint to `start.sh`, which launches `uvicorn server:app`.

```bash
# Build (from the repo root)
docker build -t eaas .
```

## Running

```bash
docker run --rm -p 8000:8000 eaas
```

The server is ready when you see all four internal services report as ready in the logs.

### Visualization

Pass `ENABLE_VIS=1` to also start the telemetry server and the Vite visualization frontend. Both the telemetry port (default `8002`) and the Vite port (default `5173`) must be exposed. Because the visualization runs in the browser, `VIS_WS_URL` must be the WebSocket address that **the browser** can reach — adjust it to match whatever hostname/IP you expose the container on.

```bash
# Visualization on localhost (default)
docker run --rm \
  -p 8000:8000 \
  -p 8002:8002 \
  -p 5173:5173 \
  -e ENABLE_VIS=1 \
  eaas
```

Open `http://localhost:5173` in a browser once the container is ready.

```bash
# Remote host or custom ports
docker run --rm \
  -p 8000:8000 \
  -p 8002:8002 \
  -p 5173:5173 \
  -e ENABLE_VIS=1 \
  -e VIS_WS_URL=ws://192.168.1.10:8002/ws \
  eaas
```

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
| `ENABLE_VIS`        | `0`     | Set to `1` to enable visualization  |
| `TELEMETRY_PORT`    | `8002`  | Port for the PyKirk telemetry server (vis only) |
| `VIS_PORT`          | `5173`  | Port for the Vite visualization frontend (vis only) |
| `VIS_WS_URL`        | `ws://localhost:8002/ws` | WebSocket URL the **browser** uses to reach the telemetry server. Must be publicly reachable. |

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

## ROS 2 Bridge

The [ros_bridge/](ros_bridge/) package is a standalone ROS 2 node that runs **outside** the Docker container and connects the EaaS dispatch loop to a ROS 2 system.

**Outbound** (container → ROS): the node connects to the telemetry WebSocket inside the container and publishes every dispatch event on the `/eaas/events` ROS topic as a `std_msgs/String` containing JSON.

**Inbound** (ROS → container): the node subscribes to `/eaas/execution_reports`. When a message arrives it is forwarded as an HTTP POST to the dispatcher's `/handle_execution` endpoint so the dispatch cycle can advance based on real-world acknowledgements instead of the simulated oracle.

### Installation

The bridge requires a sourced ROS 2 workspace (Humble or later) with `rclpy` and `std_msgs`.

```bash
# From your ROS 2 workspace src/ directory
ln -s /path/to/execution-as-a-service/ros_bridge .
cd ..
pip install websockets aiohttp   # Python deps for the bridge
colcon build --packages-select ros_bridge
source install/setup.bash
```

### Usage

```bash
# Default — connects to localhost:8002 (telemetry) and localhost:9000 (dispatcher)
ros2 run ros_bridge bridge_node

# Custom URLs
ros2 run ros_bridge bridge_node --ros-args \
  -p telemetry_ws_url:=ws://192.168.1.10:8002/ws \
  -p dispatcher_url:=http://192.168.1.10:9000

# Or via launch file
ros2 launch ros_bridge bridge.launch.py \
  telemetry_ws_url:=ws://192.168.1.10:8002/ws
```

### Parameters

| Parameter           | Default                    | Description                              |
|---------------------|----------------------------|------------------------------------------|
| `telemetry_ws_url`  | `ws://localhost:8002/ws`   | Telemetry WebSocket URL (inside the EaaS container) |
| `dispatcher_url`    | `http://localhost:9000`    | Dispatcher HTTP URL (inside the EaaS container)     |
| `event_topic`       | `/eaas/events`             | ROS topic for outbound dispatch events   |
| `report_topic`      | `/eaas/execution_reports`  | ROS topic for inbound execution reports  |
| `reconnect_delay`   | `3.0`                      | Seconds to wait before reconnecting after a WS drop |

### Sending execution reports from ROS

Publish a `std_msgs/String` to `/eaas/execution_reports` with a JSON body:

```json
{
  "event": "drive_1_end",
  "execution_time": 12.5,
  "is_controllable": true
}
```

The bridge will POST this to the dispatcher's `POST /handle_execution` as a `ReportExecutionPayloadDTO`.

### Listening for dispatch events

Subscribe to `/eaas/events`. Each message is a JSON string with the telemetry event format:

```json
{
  "type": "execution",
  "timestamp": "2026-03-23T14:30:00.000",
  "source": "agent",
  "data": {
    "agent_id": "agent_0",
    "event": "drive_1_start",
    "verb": "drive",
    "time": 4.0,
    "controllable": true
  }
}
```

## Submodules

- [enterprise/](enterprise/) — Common Lisp source for Kirk. The planner is built from `enterprise/kirk-v2/`.
- [pykirk/](pykirk/) — Python dispatch layer. See `pykirk/scripts/demo.sh` for a standalone usage example.
- [pddl_to_sp/](pddl_to_sp/) — Python module that converts a PDDL domain, problem, and temporal plan into a state plan JSON compatible with Kirk's `POST /plan-from-state-plan` endpoint.
- [ros_bridge/](ros_bridge/) — ROS 2 bridge node. Relays dispatch events to ROS topics and execution reports back to the dispatcher.
