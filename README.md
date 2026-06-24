# Execution as a Service

A Docker container that exposes an HTTP API for executing plans end-to-end. It supports three input formats: RMPL programs (native Kirk format), PDDL domain/problem/plan triples (converted via [pddl_to_sp](pddl_to_sp/)), and raw state plan JSON (fed straight to Kirk). In all cases, planning runs through [Kirk](enterprise/kirk-v2/) and dispatch runs through [PyKirk](pykirk/).

## Getting started

This project uses git submodules and is orchestrated with Docker Compose.

```bash
git clone --recurse-submodules <repo-url>
cd janeway
docker compose up
```

The first invocation builds the image (5–10 minutes). Subsequent invocations reuse it. The API is ready when `compose` logs `Uvicorn running on http://0.0.0.0:8000`.

| Service | URL |
|---|---|
| HTTP API | `http://localhost:8000` |
| Plan visualization | `http://localhost:9004` |

Submit a PDDL plan:

```bash
curl -X POST http://localhost:8000/execute-pddl \
     -F "domain=@my_domain.pddl" \
     -F "problem=@my_problem.pddl" \
     -F "plan_file=@my_pddl_plan.txt"
```

The full API surface — RMPL, PDDL, raw state plan, resume, state query, violations stream — is documented under [API reference](#api-reference).

## Scenarios

Most users want one of these. Each scenario is enabled by setting an environment variable in front of `docker compose up`, or by creating an `.env` file (see [`.env.example`](.env.example)).

| Scenario | Command | What it adds |
|---|---|---|
| Headless API + plan visualization | `docker compose up` | the default — server (`:8000`) + plan vis (`:9004`) |
| Drone scene in a browser | `ENABLE_VIS=1 docker compose up` | Vite visualization frontend at `:5173` |
| External execution (ROS / robot) | `ENABLE_ORACLE=0 docker compose up` | disables the local oracle, exposes the dispatcher at `:9000` |
| Blanket fault injection | `SIMULATE_FAULTS=1 docker compose up` | oracle posts a conflicting state update for every causal link |
| Spec-driven fault injection | see [Fault injection](#fault-injection) | mount a YAML file describing which actions should fail |

Scenarios compose: `ENABLE_VIS=1 ENABLE_ORACLE=0 docker compose up` runs the visualization frontend and routes execution reports from outside the container.

### Browser visualization

Open `http://localhost:5173` once the container is ready. The visualization's Vite dev server proxies `/ws` to the in-container telemetry server, so the browser reaches both the page and the WebSocket through the same origin.

For the **drone scene** specifically, the in-browser visualization also acts as the execution oracle: each time the drone visually completes an action, the visualization POSTs the corresponding `*_end` event to the dispatcher and the action's effect to the causal-link monitor. This lets you run the drone scenario with `ENABLE_ORACLE=0` and still get a fully advancing mission. The mappings are:

| Verb | Monitor state update |
|---|---|
| `fly` | `DRONE1.DRONE-AT = <"to" arg>` |
| `scoop` | `DRONE1.HAS-WATER = true`, `DRONE1.TANK-EMPTY = false` |
| `deliver` | `DRONE1.HAS-WATER = false`, `DRONE1.TANK-EMPTY = true`, `HOUSEN.FIRE = false`, `HOUSEN.EXTINGUISHED = true` |

Two manual fault buttons in the visualization remain available and post separately to the monitor.

### External execution (ROS / real robot)

`ENABLE_ORACLE=0` disables the local oracle and binds the dispatcher to `0.0.0.0` so execution reports can come from outside the container. The [ROS 2 bridge](#ros-2-bridge) is the supported integration; any HTTP client that POSTs to `/handle_execution` will also work.

### Fault injection

Two modes; they compose.

**1. Blanket negation** (`SIMULATE_FAULTS=1`) — after posting the correct state update for any causal link, the oracle immediately posts a conflicting update for the same variables. Useful as a smoke test for the violation pipeline.

**2. Spec-driven** (`FAULT_SPEC_FILE=...`) — pass a YAML file listing exactly which actions should fail, how many times, and what state assignment to inject:

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

Because the spec file lives on the host, mount it in via a `docker-compose.override.yml` (auto-loaded by compose; the file is ignored from version control):

```yaml
# docker-compose.override.yml
services:
  eaas:
    volumes:
      - ./faults.yaml:/app/faults.yaml:ro
    environment:
      FAULT_SPEC_FILE: /app/faults.yaml
```

Then run `docker compose up` as normal.

If both modes are set, an action listed in the spec uses the spec's assignment; everything else falls back to blanket negation.

## Configuration

Most users don't need to set anything. The default `docker compose up` already exposes every user-facing port; the scenarios above flip the few user-facing toggles. If you need additional control:

| Variable | Default | When to set it |
|---|---|---|
| `ENABLE_VIS` | `0` | `1` to start the Vite visualization frontend at `:5173`. |
| `ENABLE_ORACLE` | `1` | `0` to disable the local oracle and expose the dispatcher at `:9000` for external execution reports. |
| `SIMULATE_FAULTS` | `0` | `1` for blanket-negation fault injection from the oracle. |
| `FAULT_SPEC_FILE` | _unset_ | Path inside the container to a YAML fault spec. See [Fault injection](#fault-injection). |
| `MISSION_STATUS_CALLBACK_URL` | _unset_ | URL to POST `{"status": "completed"\|"fail"}` to when a mission terminates. |
| `VIS_WS_URL` | derived | Override only when the browser can't reach the telemetry WebSocket through the Vite proxy (e.g. behind a CDN). |

All other internal ports, paths, and dev-only flags are baked into the Dockerfile and `server.py` defaults. See [CONTRIBUTING.md](CONTRIBUTING.md) for the advanced surface.

### Generated plans

Every plan produced by Kirk is saved to `generated_plans/` (mounted from your host) alongside per-service log files:

| File | Description |
|---|---|
| `<timestamp>_<source>.json` | Plan JSON snapshots (RMPL/PDDL/state-plan inputs and Kirk outputs) |
| `janeway.log` | The `eaas` logger plus any uvicorn output from the parent process |
| `kirk-serve.log` | The Kirk Lisp planning server's stdout/stderr |
| `dispatcher.log`, `monitor.log`, `local-oracle.log`, `local-agent.log`, `telemetry.log`, `plan-visualization.log` | Per-service stdout/stderr |

## API reference

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

Submit a PDDL problem for planning and execution. Accepts multipart form data with these fields:

| Field | Type | Description |
|---|---|---|
| `domain` | file upload | PDDL domain file (durative actions) |
| `problem` | file upload | PDDL problem file (objects, init, goal) |
| `plan` | text field | Temporal plan — one `START: action(args) [DUR]` line per action. **Either** `plan` **or** `plan_file` is required. |
| `plan_file` | file upload | Same temporal plan, uploaded as a file. Convenient for clients that pipe a planner's output to a file. |

The temporal plan format follows the standard PDDL temporal planner output:

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

### `POST /execute-state-plan`

Submit a state plan JSON directly for planning and execution. Skips `pddl_to_sp` and any RMPL-to-plan translation; the body is forwarded verbatim to Kirk's `POST /plan-from-state-plan`.

**Request body** — raw state plan JSON conforming to Odo's state-plan schema (version `0.3-0` or `0.4-0`).

**Response** — `202 Accepted` with `{"status": "dispatched", ...}` on success.

```bash
curl -X POST http://localhost:8000/execute-state-plan \
     -H "Content-Type: application/json" \
     --data-binary @my_state_plan.json
```

### `POST /resume`

Continue an in-progress mission with an updated plan. Unlike `/execute*` — which always starts a *new* mission and resets the causal-link monitor to a blank world state — `/resume` keeps the monitor's observed `current_state` across re-initialization. Use it after a fault halt: read the live world state from `GET /state`, hand it to your planner, then POST the resulting state plan here.

**Request body** — same shapes as `/execute-state-plan`:
- `application/json` — raw state plan JSON, or
- `multipart/form-data` with a `state_plan` JSON file.

**Response** — `202 Accepted` with `{"status": "resumed", ...}` on success.

```bash
# Typical resume flow after a fault halt
curl -X GET  http://localhost:8000/state > current_state.json
# ... planner generates a new state plan from current_state.json ...
curl -X POST http://localhost:8000/resume \
     -H "Content-Type: application/json" \
     --data-binary @new_state_plan.json
```

### `GET /state`

Return the current state of the world as tracked by the causal link monitor. The monitor maintains a running map of state-variable assignments that is updated each time the oracle or the ROS bridge posts an observation.

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

Server-Sent Events (SSE) stream of plan-execution outcomes — both causal link violations and terminal mission-status notifications. The connection stays open and delivers events in real time.

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

The `source` field indicates how the violation was detected: `"state-update"` (oracle state post), `"event-observation"` (action start/end event), or `"event:<timing>:<name>"` (telemetry).

**Mission-status event** (when the dispatcher finishes a plan):

```json
{
  "timestamp": "2026-05-26T20:43:36.300000+00:00",
  "source": "dispatcher:agent_0",
  "status": "completed"
}
```

`status` is `"completed"` when every event in the plan executed and `"fail"` when the dispatcher halted (e.g. due to a violation). Status events don't trigger a halt — they're informational.

## ROS 2 bridge

The [ros_bridge/](ros_bridge/) package is a standalone ROS 2 node that runs **outside** the Docker container and connects the EaaS dispatch loop to a ROS 2 system. Use it together with `ENABLE_ORACLE=0`.

**Outbound** (container → ROS) — the node connects to the in-container telemetry WebSocket and publishes every dispatch event on `/eaas/events` as a `std_msgs/String` containing JSON.

**Inbound** (ROS → container) — the node subscribes to `/eaas/execution_reports` and forwards each message as an HTTP POST to the dispatcher's `/handle_execution` endpoint.

### Installation

Requires a sourced ROS 2 workspace (Humble or later) with `rclpy` and `std_msgs`.

```bash
# From your ROS 2 workspace src/ directory
ln -s /path/to/execution-as-a-service/ros_bridge .
cd ..
pip install websockets aiohttp
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

| Parameter | Default | Description |
|---|---|---|
| `telemetry_ws_url` | `ws://localhost:8002/ws` | Telemetry WebSocket URL (inside the EaaS container) |
| `dispatcher_url` | `http://localhost:9000` | Dispatcher HTTP URL (inside the EaaS container) |
| `event_topic` | `/eaas/events` | ROS topic for outbound dispatch events |
| `report_topic` | `/eaas/execution_reports` | ROS topic for inbound execution reports |
| `reconnect_delay` | `3.0` | Seconds to wait before reconnecting after a WS drop |

### Sending execution reports from ROS

Publish a `std_msgs/String` to `/eaas/execution_reports` with a JSON body:

```json
{
  "event": "drive_1_end",
  "execution_time": 12.5,
  "is_controllable": true
}
```

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
  ├─► PyKirk dispatcher  (port 9000, internal)
  │     Accepts a plan JSON at POST /plans and drives execution
  │     via a local agent (9001) and oracle (9002).
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
1. **Convert** — `pddl_to_sp` produces a state plan JSON from the PDDL inputs.
2. **Plan** — the state plan JSON is forwarded to `kirk serve` at `POST /plan-from-state-plan`.
3. **Dispatch** — same as the RMPL path.

**State plan path** (`POST /execute-state-plan`) — two steps:
1. **Plan** — the provided state plan JSON is forwarded directly to `kirk serve` at `POST /plan-from-state-plan` (no pre-processing).
2. **Dispatch** — same as the RMPL path.

In all paths, the generated plan JSON is saved to `generated_plans/` and loaded into the plan visualization server.

## Submodules

- [enterprise/](enterprise/) — Common Lisp source for Kirk. The planner is built from `enterprise/kirk-v2/`.
- [pykirk/](pykirk/) — Python dispatch layer. See `pykirk/scripts/demo.sh` for a standalone usage example.
- [pddl_to_sp/](pddl_to_sp/) — Python module that converts a PDDL domain, problem, and temporal plan into a state plan JSON.
- [robust-execution/](robust-execution/) — Causal-link monitor. Tracks link integrity during plan execution and detects violations.
- [ros_bridge/](ros_bridge/) — ROS 2 bridge node. Relays dispatch events to ROS topics and execution reports back to the dispatcher.
