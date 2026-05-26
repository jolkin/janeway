# Execution as a Service

A Docker container that exposes an HTTP API for executing plans end-to-end. It supports three input formats: RMPL programs (native Kirk format), PDDL domain/problem/plan triples (converted via [pddl_to_sp](pddl_to_sp/)), and raw state plan JSON (fed straight to Kirk). In all cases, planning runs through [Kirk](enterprise/kirk-v2/) and dispatch runs through [PyKirk](pykirk/).

## Building

This project uses git submodules. After cloning the repository, run the following command to initialize submodules:

```bash
# Initialize submodules:
git submodule update --init 
```

To build the image run the following line:

```bash
# Build (from the repo root)
docker build -t eaas .
```

## Quick Start

```bash
docker run --rm -p 8000:8000 -p 9004:9004 eaas
```

The server is ready when you see all internal services report as ready in the logs. The plan visualization is always available at `http://localhost:9004`.

To then submit a PDDL plan to Janeway, run:

```bash
curl -X POST http://localhost:8000/execute-pddl \
     -F "domain=@my_domain.pddl" \
     -F "problem=@my_problem.pddl" \
     -F "plan=my_pddl_plan_raw_text"
```

## Architecture

```
Client
  │
  ├── POST /execute            (RMPL program text)
  │
  ├── POST /execute-pddl       (PDDL domain + problem + temporal plan)
  │           │
  │           ▼
  │    pddl_to_sp  (Python, in-process)
  │    Converts PDDL inputs to a state plan JSON.
  │
  ├── POST /execute-state-plan (raw state plan JSON, bypasses pddl_to_sp)
  │
  └── POST /resume             (continuation of an in-progress mission;
                                preserves the monitor's observed state)
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
  ├─► PyKirk dispatcher  (port 9000, internal)  — default dispatch target
  │     Accepts a plan JSON at POST /plans and drives execution
  │     via a local agent (9001) and oracle (9002).
  │
  ├─► Magellan / MPCScotty  (port 5000, internal, when ENABLE_MAGELLAN=1)
  │     Replaces the PyKirk dispatcher when enabled.  Accepts
  │     POST /planner/start with {goalPlan, exoPlan, model} and runs
  │     MPC-based motion planning + execution against a YAML model.
  │
  ├─► Causal link monitor  (port 9003, internal)
  │     Monitors causal link integrity during execution.
  │     Receives state updates from the oracle and checks them
  │     against the expected causal link conditions.
  │
  └─► Plan visualization  (port 9004)
        Serves a d3-dag graph visualization of the plan.
        Displays events, temporal constraints, episode edges,
        and causal links in a left-to-right layered layout.
```

On container startup, `server.py` launches all internal services and waits for them to become ready before accepting requests.

**RMPL path** (`POST /execute`) — two steps:

1. **Plan** — the RMPL program is forwarded to `kirk serve` at `POST /plan`. Kirk generates a scheduled state plan and returns it as JSON.
2. **Dispatch** — the causal link monitor is initialized with the plan, the oracle extracts causal links, and the plan is forwarded to the PyKirk dispatcher at `POST /plans`.

**PDDL path** (`POST /execute-pddl`) — three steps:

1. **Convert** — the PDDL domain, problem, and temporal plan are passed to `pddl_to_sp` (in-process), which produces a state plan JSON.
2. **Plan** — the state plan JSON is forwarded to `kirk serve` at `POST /plan-from-state-plan`. Kirk deserializes it, runs the planner, and returns a new scheduled state plan JSON.
3. **Dispatch** — the causal link monitor is initialized with the plan, the oracle extracts causal links, and the plan is forwarded to the PyKirk dispatcher at `POST /plans`.

**State plan path** (`POST /execute-state-plan`) — two steps:

1. **Plan** — the provided state plan JSON is forwarded directly to `kirk serve` at `POST /plan-from-state-plan` (no pre-processing). Kirk deserializes it, runs the planner, and returns a new scheduled state plan JSON. Useful when a state plan is produced by some other upstream source (e.g. a pre-saved `pddl_to_sp` output, a handwritten plan, or a different planner).
2. **Dispatch** — the causal link monitor is initialized with the plan, the oracle extracts causal links, and the plan is forwarded to the PyKirk dispatcher at `POST /plans`.

In all paths, the generated plan JSON is saved to `generated_plans/` and loaded into the plan visualization server.

### Magellan dispatch (optional)

Set `ENABLE_MAGELLAN=1` to swap the PyKirk dispatcher for [Magellan / MPCScotty](MPCScotty/) — an MPC-based motion planning and execution service. When enabled:

- A Magellan server is launched as a subprocess on port `MAGELLAN_PORT` (default `5000`).
- All three execute endpoints route the scheduled state plan to Magellan's `POST /planner/start` instead of PyKirk's `POST /plans`.
- Each execute request **must** also include a YAML world/dynamics `model` file. Magellan reads this file to ground its motion-planning problem (obstacles, agent dynamics, etc.). The server saves the upload into Magellan's `problems/` directory and references it by filename in the request.

When `ENABLE_MAGELLAN=1` the request shapes change as follows:

| Endpoint | Body |
|---|---|
| `POST /execute` | `multipart/form-data` with fields `rmpl` (text or file) and `model` (YAML file) |
| `POST /execute-pddl` | existing fields (`domain`, `problem`, `plan`) plus `model` (YAML file) |
| `POST /execute-state-plan` | `multipart/form-data` with fields `state_plan` (JSON file) and `model` (YAML file) |

```bash
docker run --rm \
  -p 8000:8000 \
  -p 9004:9004 \
  -e ENABLE_MAGELLAN=1 \
  eaas
```

## Build Details

This triggers a two-stage build.

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



### Generated plans

Every plan produced by Kirk is saved to the `generated_plans/` directory inside the container, alongside per-service log files for postmortem inspection. Mount a volume to persist both on the host:

```bash
docker run --rm \
  -p 8000:8000 \
  -p 9004:9004 \
  -v $(pwd)/plans:/app/generated_plans \
  eaas
```

After mounting, the host directory contains:

| File | Description |
|---|---|
| `<timestamp>_<source>.json` | Plan JSON snapshots (RMPL/PDDL/state-plan inputs and Kirk outputs) |
| `janeway.log` | Janeway's own log (the `eaas` logger plus any uvicorn output from the parent process) |
| `kirk-serve.log` | The Kirk Lisp planning server's stdout/stderr |
| `dispatcher.log`, `local-agent.log`, `local-oracle.log` | PyKirk component logs |
| `monitor.log` | Causal-link monitor log |
| `plan-visualization.log` | Plan-vis server log |
| `telemetry.log` | Telemetry server log (when started) |
| `visualization.log` | Vite dev server log (`ENABLE_VIS=1` only) |
| `magellan.log` | MPCScotty/Magellan log (`ENABLE_MAGELLAN=1` only) |

All log files are truncated at container start. Use `tail -f plans/<service>.log` on the host to follow a specific service.

### Plan visualization

The plan visualization server (port `9004`) starts automatically and serves a d3-dag graph of the most recently dispatched plan. It shows events as nodes in a left-to-right layered layout with temporal constraints, episode edges, and causal links. The graph supports zoom/pan, node highlighting, and a causal link toggle.

Open `http://localhost:9004` in a browser after dispatching a plan.

### Causal link monitoring

The causal link monitor (port `9003`) is initialized automatically when a plan is dispatched. During execution, the oracle posts state updates to the monitor as causal link events fire. The monitor checks these updates against the expected causal link conditions from the plan.

When a causal link violation is detected, the monitor reports it in real time via the `GET /violations` SSE (Server-Sent Events) stream on the main API. Connect to this endpoint to receive violation alerts as they occur:

```bash
curl -N http://localhost:8000/violations
```

Each violation event is a JSON object:
```json
{
  "timestamp": "2026-03-26T14:30:00.000000+00:00",
  "source": "state-update",
  "violations": ["True violates active (Q=False): Start->Action2)"]
}
```

### Fault simulation

The oracle supports two complementary fault-injection modes.

**1. Blanket negation (`SIMULATE_FAULTS=1`)** — after posting the correct state update for any causal link, the oracle immediately posts a conflicting (negated) update for the same variables. Useful as a quick smoke test of the monitor's violation pipeline.

```bash
docker run --rm \
  -p 8000:8000 \
  -p 9004:9004 \
  -e SIMULATE_FAULTS=1 \
  eaas
```

**2. Spec-driven faults (`FAULT_SPEC_FILE=...`)** — pass a YAML (or JSON) file describing exactly which actions should fail, how many times, and what state assignment to inject. The oracle reads the file at startup, and whenever an action whose verb matches a spec executes, it posts the spec's `assignment` to the monitor — up to `times` times. After that the rule is inert.

```yaml
# faults.yaml
faults:
  - action: drive          # matches event names like DRIVE_END_8, drive_3_end
    times: 2               # fire the fault twice, then stop
    assignment:
      rover1.location: "lost"
  - action: science
    times: 1
    assignment:
      rover1.has_sample: false
```

Mount the file into the container and point `FAULT_SPEC_FILE` at it:

```bash
docker run --rm \
  -p 8000:8000 \
  -p 9004:9004 \
  -v $(pwd)/faults.yaml:/app/faults.yaml \
  -e FAULT_SPEC_FILE=/app/faults.yaml \
  eaas
```

The two modes compose: if both are set, an action with a spec uses the spec's assignment; everything else falls back to the negated-causal-link injection.

### PyKirk visualization

Pass `ENABLE_VIS=1` to also start the telemetry server and the Vite visualization frontend. Only the Vite port (default `5173`) needs to be exposed — the visualization's Vite dev server proxies `/ws` to the in-container telemetry server, so the browser reaches both the page and the WebSocket through the same origin. (Forwarding port `8002` is still supported if you want to connect external WS clients directly.)

```bash
# Visualization on localhost (default)
docker run --rm \
  -p 8000:8000 \
  -p 5173:5173 \
  -p 9004:9004 \
  -e ENABLE_VIS=1 \
  eaas
```

Open `http://localhost:5173` in a browser once the container is ready.

```bash
# Remote host or custom ports
docker run --rm \
  -p 8000:8000 \
  -p 5173:5173 \
  -p 9004:9004 \
  -e ENABLE_VIS=1 \
  eaas
```

If you'd rather have the browser connect to the telemetry server directly (e.g. when the visualization is served from a CDN/proxy and `/ws` can't be proxied), expose port `8002` and override `VITE_TELEMETRY_WS_URL` at build time, or set `VIS_WS_URL` (kept for backward compatibility) to the explicit ws/wss URL the browser should use.

### External execution (no oracle)

By default the container runs a local oracle that simulates execution acknowledgements. When integrating with a real robot system (e.g. via the ROS 2 bridge), disable the oracle so that execution reports come from outside the container:

```bash
docker run --rm \
  -p 8000:8000 \
  -p 9000:9000 \
  -e ENABLE_ORACLE=0 \
  eaas
```

When `ENABLE_ORACLE=0`:
- The local oracle service is **not started**.
- The dispatcher binds to `0.0.0.0` instead of `127.0.0.1`, making its `POST /handle_execution` endpoint reachable from outside the container.
- You must publish port `9000` (or your custom `DISPATCHER_PORT`) so external systems can send execution reports.

### Environment variables

| Variable            | Default | Description                         |
|---------------------|---------|-------------------------------------|
| `KIRK_BINARY`       | `/app/kirk/kirk`    | Path to the Kirk executable     |
| `PYKIRK_DIR`        | `/app/pykirk`       | Path to the pykirk source tree  |
| `PDDL_TO_SP_DIR`    | `/app/pddl_to_sp`   | Path to the pddl_to_sp module   |
| `ROBUST_EXEC_DIR`   | `/app/robust-execution` | Path to the robust-execution source tree |
| `PLAN_VIS_DIR`      | `/app/plan_visualization` | Path to the plan visualization directory |
| `KIRK_PORT`         | `7000`  | Internal port for `kirk serve`      |
| `DISPATCHER_PORT`   | `9000`  | Port for the dispatcher (exposed when oracle is disabled) |
| `LOCAL_AGENT_PORT`  | `9001`  | Internal port for the local agent   |
| `LOCAL_ORACLE_PORT` | `9002`  | Internal port for the local oracle  |
| `MONITOR_PORT`      | `9003`  | Internal port for the causal link monitor |
| `PLAN_VIS_PORT`     | `9004`  | Port for the plan visualization server |
| `SERVER_PORT`       | `8000`  | External port for the HTTP API      |
| `ENABLE_ORACLE`     | `1`     | Set to `0` to disable the local oracle and expose the dispatcher for external execution reports |
| `ENABLE_VIS`        | `0`     | Set to `1` to enable PyKirk visualization |
| `ENABLE_MAGELLAN`   | `0`     | Set to `1` to start Magellan/MPCScotty and route plans there instead of PyKirk |
| `MAGELLAN_PORT`     | `5000`  | Internal port for Magellan (when `ENABLE_MAGELLAN=1`) |
| `MPCSCOTTY_DIR`     | `/app/MPCScotty` | Path to the MPCScotty source tree |
| `MAGELLAN_PROBLEMS_DIR` | `${MPCSCOTTY_DIR}/problems` | Where uploaded YAML model files are written |
| `SIMULATE_FAULTS`   | `0`     | Set to `1` to have the oracle inject causal link violations |
| `FAULT_SPEC_FILE`   | _(empty)_ | Path (inside the container) to a YAML/JSON file describing per-action fault injections. See the [Fault simulation](#fault-simulation) section for the file format. |
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
     -H "X-Package-Name: my-package-name" \
     --data-binary @my_program.rmpl
```

```bash
curl -X POST http://localhost:8000/execute \
     -H "Content-Type: application/json" \
     -d '{"rmpl": "(define-package main ...)"}'
```

### `POST /execute-pddl`

Submit a PDDL problem for planning and execution. Accepts multipart form data with three fields:

| Field       | Type         | Description                                                                                            |
|-------------|--------------|--------------------------------------------------------------------------------------------------------|
| `domain`    | file upload  | PDDL domain file (durative actions)                                                                    |
| `problem`   | file upload  | PDDL problem file (objects, init, goal)                                                                |
| `plan`      | text field   | Temporal plan — one `START: action(args) [DUR]` line per action. **Either** `plan` **or** `plan_file` is required. |
| `plan_file` | file upload  | Same temporal plan, uploaded as a file. Convenient for clients that pipe a planner's output to a file. |
| `model`     | file upload  | (required when `ENABLE_MAGELLAN=1`) YAML world/dynamics model.                                         |

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

### `POST /execute-state-plan`

Submit a state plan JSON directly for planning and execution. This skips `pddl_to_sp` and any RMPL-to-plan translation; the body is forwarded verbatim to Kirk's `POST /plan-from-state-plan`.

**Request body** — raw state plan JSON with `Content-Type: application/json`. The JSON must conform to Odo's state plan schema (version `0.3-0` or `0.4-0`), the same shape produced by `pddl_to_sp` and by Kirk's own planning output.

**Response** — `202 Accepted` with `{"status": "dispatched", ...}` on success.

```bash
curl -X POST http://localhost:8000/execute-state-plan \
     -H "Content-Type: application/json" \
     --data-binary @my_state_plan.json
```

**Processing steps:**
1. The state plan JSON is saved to `generated_plans/<timestamp>_state_plan_input.json`.
2. The JSON is sent to Kirk's `POST /plan-from-state-plan` endpoint, which runs the planner and returns a new scheduled state plan.
3. The scheduled state plan is dispatched to PyKirk via `POST /plans`.

### `POST /resume`

Continue an in-progress mission with an updated plan. Unlike the `/execute*` endpoints — which always start a *new* mission and reset the causal link monitor to a blank world state — `/resume` keeps the monitor's observed `current_state` across re-initialization. Use it after a fault halt: read the live world state from `GET /state`, hand it to your planner, then POST the resulting state plan here.

**Request body** — same shapes as `/execute-state-plan`:
- `application/json` — raw state plan JSON, or
- `multipart/form-data` with a `state_plan` JSON file and (when `ENABLE_MAGELLAN=1`) a `model` YAML file.

**Response** — `202 Accepted` with `{"status": "resumed", ...}` on success.

**Processing steps:**
1. The incoming state plan is saved to `generated_plans/<timestamp>_resume_input.json`.
2. The JSON is sent to Kirk's `POST /plan-from-state-plan`.
3. The causal link monitor is re-initialized via its `POST /resume-state-plan` route, which copies the previous monitor's `current_state.assignments` into the new one.
4. The plan is dispatched to the active downstream service (PyKirk or Magellan). The dispatcher itself already handles restart via `initialize_rte_data_given_replan`.

```bash
# Typical resume flow after a fault halt
curl -X GET  http://localhost:8000/state             > current_state.json
# ... planner generates a new state plan from current_state.json ...
curl -X POST http://localhost:8000/resume \
     -H "Content-Type: application/json" \
     --data-binary @new_state_plan.json
```

### `GET /state`

Return the current state of the world as tracked by the causal link monitor. The monitor maintains a running map of state-variable assignments that is updated each time the oracle or the ROS bridge posts an observation. The response is the JSON form of the monitor's `current_state` object; an empty object is returned if no plan has been dispatched yet.

```bash
curl http://localhost:8000/state
# {"assignments": {"rover1.location": {"variable": "rover1.location", "value": "science1"},
#                  "rover1.has_sample": {"variable": "rover1.has_sample", "value": true}}}
```

### `GET /health`

Returns the liveness of the server and all internal services.

```bash
curl http://localhost:8000/health
# {"status": "ok", "services": {"kirk": "ok", "dispatcher": "ok", "agent": "ok", "oracle": "ok"}}
```

### `GET /violations`

Server-Sent Events (SSE) stream of plan-execution outcomes — both causal link violations and terminal mission-status notifications. The connection stays open and delivers events in real time. The stream currently emits two payload shapes:

```bash
curl -N http://localhost:8000/violations
```

**Violation event** (when the monitor detects a causal-link conflict):
```json
{
  "timestamp": "2026-03-26T14:30:00.000000+00:00",
  "source": "state-update",
  "violations": ["True violates active (Q=False): Start->Action2)"]
}
```

The `source` field indicates how the violation was detected: `"state-update"` (from an oracle state post), `"event-observation"` (from an action start/end event), or `"event:<timing>:<name>"` (from telemetry).

**Mission-status event** (when the dispatcher finishes a plan):
```json
{
  "timestamp": "2026-05-26T20:43:36.300000+00:00",
  "source": "dispatcher:agent_0",
  "status": "completed"
}
```

`status` is `"completed"` when every event in the plan executed and `"fail"` when the dispatcher halted (e.g. due to a violation). Unlike violation events, status events don't trigger a dispatcher halt — they're informational.

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
- [robust-execution/](robust-execution/) — Causal link monitor. Tracks causal link integrity during plan execution and detects violations.
- [ros_bridge/](ros_bridge/) — ROS 2 bridge node. Relays dispatch events to ROS topics and execution reports back to the dispatcher.
