"""
Execution-as-a-Service HTTP server.

Accepts RMPL programs via POST /execute, generates a plan using the kirk
planning server, and dispatches it through the pykirk dispatcher.
"""

import asyncio
import importlib
import json
import logging
import os
import subprocess
import sys
import tempfile
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import httpx
from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import JSONResponse
from starlette.responses import StreamingResponse

GENERATED_PLANS_DIR = Path(__file__).parent / "generated_plans"
GENERATED_PLANS_DIR.mkdir(exist_ok=True)

# Mirror Janeway's own logs to a file inside generated_plans/ so the host can
# pick them up via the same bind mount used for plan JSON.  Truncate on each
# container start; rotate-or-keep is the operator's responsibility.
LOG_FILE_PATH = GENERATED_PLANS_DIR / "janeway.log"
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(LOG_FILE_PATH, mode="w"),
    ],
)
log = logging.getLogger("eaas")

KIRK_SERVE_PORT = int(os.environ.get("KIRK_PORT", "7000"))
DISPATCHER_PORT = int(os.environ.get("DISPATCHER_PORT", "9000"))
AGENT_PORT = int(os.environ.get("LOCAL_AGENT_PORT", "9001"))
ORACLE_PORT = int(os.environ.get("LOCAL_ORACLE_PORT", "9002"))

KIRK_BINARY = os.environ.get("KIRK_BINARY", "/app/kirk/kirk")
PYKIRK_DIR = os.environ.get("PYKIRK_DIR", "/app/pykirk")
PDDL_TO_SP_DIR = os.environ.get("PDDL_TO_SP_DIR", "/app/pddl_to_sp")
PDDL_TO_SP_SRC_DIR = os.environ.get("PDDL_TO_SP_SRC_DIR", f"{PDDL_TO_SP_DIR}/src")
ROBUST_EXEC_DIR = os.environ.get("ROBUST_EXEC_DIR", "/app/robust-execution")
MONITOR_PORT = int(os.environ.get("MONITOR_PORT", "9003"))
SERVER_PORT = int(os.environ.get("SERVER_PORT", "8000"))
ENABLE_ORACLE = os.environ.get("ENABLE_ORACLE", "0").strip() not in ("0", "", "false", "False")
ENABLE_VIS = os.environ.get("ENABLE_VIS", "0").strip() not in ("0", "", "false", "False")
ENABLE_MAGELLAN = os.environ.get("ENABLE_MAGELLAN", "0").strip() not in ("0", "", "false", "False")
SIMULATE_FAULTS = os.environ.get("SIMULATE_FAULTS", "0")
FAULT_SPEC_FILE = os.environ.get("FAULT_SPEC_FILE", "")
# Optional external URL told when a mission terminates.  The dispatcher itself
# always reports to our /violations (so the SSE stream sees it); that handler
# forwards {"status": "completed"|"fail"} here.
MISSION_STATUS_CALLBACK_URL = os.environ.get("MISSION_STATUS_CALLBACK_URL", "").strip()
# Online replanning: on a causal-link violation or a temporal inconsistency,
# pause the dispatcher, ask Kirk to re-solve the mission's TPN around what has
# already executed, and resume from the new plan.  When disabled, violations
# halt the mission as before.
ENABLE_REPLANNING = os.environ.get("ENABLE_REPLANNING", "1").strip() not in ("0", "", "false", "False")
MAX_REPLANS = int(os.environ.get("MAX_REPLANS", "5"))
# Seconds to wait after pausing before reading the executed schedule, so that
# execution reports already in flight land in the dispatcher's history.
REPLAN_SETTLE_SECONDS = float(os.environ.get("REPLAN_SETTLE_SECONDS", "0.5"))
# Seconds of local-agent execution delay to simulate (0 = perfect execution).
AGENT_MAX_DELAY = os.environ.get("AGENT_MAX_DELAY", "")
TELEMETRY_PORT = int(os.environ.get("TELEMETRY_PORT", "8002"))
VIS_PORT = int(os.environ.get("VIS_PORT", "5173"))
PLAN_VIS_PORT = int(os.environ.get("PLAN_VIS_PORT", "9004"))
PLAN_VIS_DIR = os.environ.get("PLAN_VIS_DIR", str(Path(__file__).parent / "plan_visualization"))
MAGELLAN_PORT = int(os.environ.get("MAGELLAN_PORT", "5000"))
MPCSCOTTY_DIR = os.environ.get("MPCSCOTTY_DIR", str(Path(__file__).parent / "MPCScotty"))
MAGELLAN_PROBLEMS_DIR = os.environ.get(
    "MAGELLAN_PROBLEMS_DIR", str(Path(MPCSCOTTY_DIR) / "problems")
)
# Public WebSocket URL used by the browser to reach the telemetry server.
# Must be reachable from the client machine, not from inside the container.
VIS_WS_URL = os.environ.get("VIS_WS_URL", f"ws://localhost:{TELEMETRY_PORT}/ws")
# Drone-scene preset selector.  Picks between the `single` (1 drone, 2
# houses) and `multi` (2 drones, 3 houses) preset defined under
# scenes.drone.presets in pykirk/visualization/src/config/scene-config.json.
# Empty string leaves the choice to the JSON's `preset` field (currently
# `multi`).  Passed through to the Vite dev server as VITE_VIS_DRONE_PRESET
# so the React app sees it at startup.
VIS_DRONE_PRESET = os.environ.get("VIS_DRONE_PRESET", "")

# Make pddl_to_sp importable (uses bare imports internally — all of its
# submodules live under pddl_to_sp/src/ after the recent refactor).
if PDDL_TO_SP_SRC_DIR not in sys.path:
    sys.path.insert(0, PDDL_TO_SP_SRC_DIR)

_processes: list[subprocess.Popen] = []
_violation_subscribers: list[asyncio.Queue] = []


class _MissionState:
    """Per-mission replanning bookkeeping (reset by every /execute*)."""

    def __init__(self):
        self.lock = asyncio.Lock()
        self.replans = 0
        self.in_progress = False
        self.pending_reason: dict | None = None
        self.last_replan: dict | None = None
        self.model_yaml: str | None = None
        self.halted = False

    def reset(self, model_yaml: str | None = None):
        self.replans = 0
        self.pending_reason = None
        self.last_replan = None
        self.model_yaml = model_yaml
        self.halted = False


_mission = _MissionState()


async def wait_for_http(url: str, timeout: float = 60.0) -> bool:
    """Poll url until it responds with any HTTP status or timeout expires."""
    deadline = asyncio.get_event_loop().time() + timeout
    async with httpx.AsyncClient() as client:
        while asyncio.get_event_loop().time() < deadline:
            try:
                await client.get(url, timeout=2.0)
                return True
            except Exception:
                await asyncio.sleep(0.5)
    return False


_log_files: list = []


def _start_process(cmd: list[str], cwd: str | None, env: dict, name: str,
                   append: bool = False) -> subprocess.Popen:
    """Start a subprocess with its stdout/stderr redirected to a per-service
    log file inside ``generated_plans/`` (so the host can read it via the
    same bind mount used for plan JSON).  The file is truncated on each
    container start (pass ``append=True`` on a supervised restart to keep
    the pre-crash log); rotation is the operator's responsibility.
    """
    log_path = GENERATED_PLANS_DIR / f"{name}.log"
    log.info("Starting %s: %s (log -> %s)", name, " ".join(cmd), log_path)
    log_fh = open(log_path, "a" if append else "w", buffering=1)  # line-buffered
    _log_files.append(log_fh)
    proc = subprocess.Popen(
        cmd,
        cwd=cwd,
        env=env,
        stdout=log_fh,
        stderr=subprocess.STDOUT,
    )
    _processes.append(proc)
    return proc


@asynccontextmanager
async def lifespan(app: FastAPI):
    base_env = os.environ.copy()
    # Expose the generated_plans directory so the Kirk binary can drop
    # visualization artefacts (e.g. STNU checker PDFs) alongside plan JSON.
    base_env["GENERATED_PLANS_DIR"] = str(GENERATED_PLANS_DIR)

    # ── Kirk planning server ───────────────────────────────────────────────
    # LD_LIBRARY_PATH is scoped to the kirk process only: /app/kirk/ipopt-libs
    # holds the build image's IPOPT numeric stack (see Dockerfile), which must
    # not leak into the Python/Node services' library resolution.
    kirk_env = {**base_env, "LD_LIBRARY_PATH": "/app/kirk/ipopt-libs"}
    kirk_proc = _start_process(
        [KIRK_BINARY, "serve", "--port", str(KIRK_SERVE_PORT)],
        cwd=None,
        env=kirk_env,
        name="kirk-serve",
    )

    # ── PyKirk services ────────────────────────────────────────────────────
    pykirk_env = {
        **base_env,
        "HOST": "127.0.0.1",
        "AGENT_ID": "agent_0",
        "DISPATCHER_PORT": str(DISPATCHER_PORT),
        "LOCAL_AGENT_PORT": str(AGENT_PORT),
        "LOCAL_ORACLE_PORT": str(ORACLE_PORT),
        "TELEMETRY_PORT": str(TELEMETRY_PORT),
        "ENVIRONMENT": "dev" if ENABLE_ORACLE else "prod",
        "MONITOR_URL": f"http://127.0.0.1:{MONITOR_PORT}",
        "SIMULATE_FAULTS": SIMULATE_FAULTS,
        "FAULT_SPEC_FILE": FAULT_SPEC_FILE,
        # Dispatcher posts terminal mission-status notifications (and replan
        # requests) here so they flow out through the same /violations SSE
        # stream consumers already listen to.
        "MISSION_STATUS_CALLBACK_URL": f"http://127.0.0.1:{SERVER_PORT}/violations",
        "ENABLE_REPLANNING": "1" if ENABLE_REPLANNING else "0",
        "AGENT_MAX_DELAY": AGENT_MAX_DELAY,
    }

    # The dispatcher and monitor always bind 0.0.0.0: in-container binding is
    # not the exposure boundary — docker compose `ports:` is.  Binding
    # unconditionally lets external systems (a ROS bridge posting execution
    # reports, fault injection against the monitor) reach them by simply
    # publishing the port, without restarting with ENABLE_ORACLE=0.
    services = [
        ("src.pykirk.dispatch.api.dispatcher.main:app", DISPATCHER_PORT, "0.0.0.0", "dispatcher"),
        ("src.pykirk.dispatch.api.local.agent.main:app", AGENT_PORT, "127.0.0.1", "local-agent"),
    ]
    if ENABLE_ORACLE:
        services.append(
            ("src.pykirk.dispatch.api.local.oracle.main:app", ORACLE_PORT, "127.0.0.1", "local-oracle"),
        )
    else:
        log.info("Oracle disabled — external systems provide execution reports on :%s", DISPATCHER_PORT)

    for uvicorn_app, port, host, name in services:
        _start_process(
            ["uv", "run", "uvicorn", uvicorn_app,
             "--host", host, "--port", str(port)],
            cwd=PYKIRK_DIR,
            env={**pykirk_env, "PORT": str(port)},
            name=name,
        )

    # ── Causal link monitor server ─────────────────────────────────────────
    # Always 0.0.0.0 (see the dispatcher note above): external state updates
    # and fault injection only need a compose port mapping, not a restart.
    _start_process(
        ["uv", "run", "uvicorn",
         "planexecutive.monitor.server.server:app",
         "--host", "0.0.0.0", "--port", str(MONITOR_PORT)],
        cwd=ROBUST_EXEC_DIR,
        env={
            **base_env,
            "PORT": str(MONITOR_PORT),
            "TELEMETRY_WS_URL": f"ws://127.0.0.1:{TELEMETRY_PORT}/ws",
            "VIOLATION_CALLBACK_URL": f"http://127.0.0.1:{SERVER_PORT}/violations",
            "PLAN_VIS_URL": f"http://127.0.0.1:{PLAN_VIS_PORT}",
        },
        name="monitor",
    )

    # ── Telemetry server ──────────────────────────────────────────────────
    # Always started: the causal-link monitor learns which events have
    # executed (START consumes a link, END activates one) only through the
    # telemetry WebSocket, so without it links are never active and no
    # violation can be detected.  The visualization and the ROS bridge use
    # the same stream.
    if True:
        log.info("Starting telemetry server (monitor event feed%s)",
                 ", visualization" if ENABLE_VIS else "")
        _start_process(
            ["uv", "run", "uvicorn",
             "src.pykirk.dispatch.api.telemetry.main:app",
             "--host", "0.0.0.0", "--port", str(TELEMETRY_PORT)],
            cwd=PYKIRK_DIR,
            env={**pykirk_env, "PORT": str(TELEMETRY_PORT)},
            name="telemetry",
        )

    # ── Plan visualization server ─────────────────────────────────────────
    _start_process(
        ["uvicorn", "plan_visualization.server:app",
         "--host", "0.0.0.0", "--port", str(PLAN_VIS_PORT)],
        cwd=str(Path(__file__).parent),
        env={
            **base_env,
            "TELEMETRY_WS_URL": f"ws://127.0.0.1:{TELEMETRY_PORT}/ws",
        },
        name="plan-visualization",
    )

    # ── Magellan motion-planning service (optional) ──────────────────────
    if ENABLE_MAGELLAN:
        log.info("Magellan enabled — starting MPCScotty server")
        Path(MAGELLAN_PROBLEMS_DIR).mkdir(parents=True, exist_ok=True)
        _start_process(
            ["python", "magellan/server.py",
             "-host", "127.0.0.1",
             "-port", str(MAGELLAN_PORT)],
            cwd=MPCSCOTTY_DIR,
            env=base_env,
            name="magellan",
        )

    # ── Visualization frontend (optional) ────────────────────────────────
    if ENABLE_VIS:
        log.info(
            "Visualization enabled — starting Vite dev server "
            f"(drone preset: {VIS_DRONE_PRESET or '<json default>'})"
        )
        vis_env = {**base_env, "VITE_TELEMETRY_WS_URL": VIS_WS_URL}
        if VIS_DRONE_PRESET:
            vis_env["VITE_VIS_DRONE_PRESET"] = VIS_DRONE_PRESET
        _start_process(
            ["npm", "run", "dev", "--",
             "--host", "0.0.0.0",
             "--port", str(VIS_PORT)],
            cwd=f"{PYKIRK_DIR}/visualization",
            env=vis_env,
            name="visualization",
        )

    # ── Wait for all services to be ready ──────────────────────────────────
    log.info("Waiting for services to become ready...")
    checks = [
        (f"http://127.0.0.1:{KIRK_SERVE_PORT}/health", "kirk-serve"),
        (f"http://127.0.0.1:{DISPATCHER_PORT}/docs", "dispatcher"),
        (f"http://127.0.0.1:{AGENT_PORT}/docs", "local-agent"),
        (f"http://127.0.0.1:{MONITOR_PORT}/docs", "monitor"),
        (f"http://127.0.0.1:{PLAN_VIS_PORT}/docs", "plan-visualization"),
    ]
    if ENABLE_ORACLE:
        checks.append((f"http://127.0.0.1:{ORACLE_PORT}/docs", "local-oracle"))
    checks.append((f"http://127.0.0.1:{TELEMETRY_PORT}/docs", "telemetry"))
    if ENABLE_VIS:
        checks.append((f"http://127.0.0.1:{VIS_PORT}/", "visualization"))
    if ENABLE_MAGELLAN:
        checks.append((f"http://127.0.0.1:{MAGELLAN_PORT}/", "magellan"))
    for url, name in checks:
        ready = await wait_for_http(url, timeout=120.0)
        if ready:
            log.info("%s is ready at %s", name, url)
        else:
            log.warning("%s did not become ready at %s within timeout", name, url)

    # ── Kirk supervision ──────────────────────────────────────────────────
    # Defense-in-depth for issue #2: if the kirk binary ever exits (e.g. an
    # unhandled condition with the debugger disabled), restart it instead of
    # answering 503 for every request until the container is restarted.
    async def _supervise_kirk():
        nonlocal kirk_proc
        while True:
            await asyncio.sleep(2.0)
            if kirk_proc.poll() is not None:
                log.error(
                    "kirk-serve exited with code %s — restarting", kirk_proc.returncode
                )
                try:
                    _processes.remove(kirk_proc)
                except ValueError:
                    pass
                kirk_proc = _start_process(
                    [KIRK_BINARY, "serve", "--port", str(KIRK_SERVE_PORT)],
                    cwd=None,
                    env=kirk_env,
                    name="kirk-serve",
                    append=True,
                )
                ready = await wait_for_http(
                    f"http://127.0.0.1:{KIRK_SERVE_PORT}/health", timeout=120.0
                )
                log.info("kirk-serve restart %s", "healthy" if ready else "NOT healthy")

    kirk_supervisor = asyncio.create_task(_supervise_kirk())

    yield

    log.info("Shutting down services...")
    kirk_supervisor.cancel()
    for proc in _processes:
        proc.terminate()
    for proc in _processes:
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
    for fh in _log_files:
        try:
            fh.close()
        except Exception:
            pass


def _save_plan(plan_payload: dict, source: str):
    """Save a plan received from Kirk to the generated_plans folder."""
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    filename = f"{timestamp}_{source}.json"
    path = GENERATED_PLANS_DIR / filename
    try:
        path.write_text(json.dumps(plan_payload, indent=2))
        log.info("Saved plan to %s", path)
    except Exception as exc:
        log.warning("Failed to save plan: %s", exc)


async def _load_plan_visualization(plan_payload: dict):
    """Send the plan to the plan visualization server."""
    log.info("Loading plan into visualization server")
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.post(
                f"http://127.0.0.1:{PLAN_VIS_PORT}/load",
                json={"plan": plan_payload, "executions": []},
                headers={"Content-Type": "application/json"},
            )
        if resp.status_code == 200:
            log.info("Plan visualization loaded successfully")
        else:
            log.warning("Plan visualization load returned %s: %s", resp.status_code, resp.text)
    except httpx.RequestError as exc:
        log.warning("Could not reach plan visualization server: %s", exc)


async def _load_oracle_plan(plan_payload: dict, resume: bool = False):
    """Send the plan to the oracle so it can extract causal links for state updates.

    With ``resume=True`` (online replan of the running mission) the oracle keeps
    its execution history and fault-injection counters.
    """
    if not ENABLE_ORACLE:
        return
    log.info("Loading plan into oracle for causal link extraction (resume=%s)", resume)
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.post(
                f"http://127.0.0.1:{ORACLE_PORT}/plan",
                json=plan_payload,
                headers={"Content-Type": "application/json"},
                params={"resume": "true"} if resume else None,
            )
        if resp.status_code == 200:
            log.info("Oracle plan loaded: %s", resp.json())
        else:
            log.warning("Oracle plan load returned %s: %s", resp.status_code, resp.text)
    except httpx.RequestError as exc:
        log.warning("Could not reach oracle: %s", exc)


async def _initialize_monitor(
    plan_payload: dict, resume: bool = False, executed_events: list | None = None
):
    """Send the plan to the causal link monitor for initialization.

    When ``resume`` is True, the monitor preserves its observed
    ``current_state`` across the re-initialization (so a mid-execution
    continuation keeps the post-fault world view).  ``executed_events``
    (online replanning) are replayed into the new plan so links produced by
    already-executed events are active again.
    """
    route = "resume-state-plan" if resume else "initialize-state-plan"
    log.info("Initializing causal link monitor (route=%s)", route)
    body = plan_payload
    if resume and executed_events:
        body = {"plan": plan_payload, "executedEvents": executed_events}
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.post(
                f"http://127.0.0.1:{MONITOR_PORT}/{route}",
                json=body,
                headers={"Content-Type": "application/json"},
            )
        if resp.status_code == 200:
            log.info("Causal link monitor initialized successfully: %s", resp.text[:300])
        else:
            log.warning("Monitor initialization returned %s: %s", resp.status_code, resp.text)
    except httpx.RequestError as exc:
        log.warning("Could not reach causal link monitor: %s", exc)


def _save_magellan_model(model_yaml: str | bytes) -> str:
    """Persist a YAML model into Magellan's problems/ directory and return the
    filename Magellan should reference (relative to that directory)."""
    Path(MAGELLAN_PROBLEMS_DIR).mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    filename = f"model_{timestamp}.yaml"
    path = Path(MAGELLAN_PROBLEMS_DIR) / filename
    if isinstance(model_yaml, bytes):
        path.write_bytes(model_yaml)
    else:
        path.write_text(model_yaml)
    log.info("Wrote Magellan model to %s", path)
    return filename


async def _read_model_yaml(model: UploadFile | None) -> str:
    if model is None:
        raise HTTPException(
            status_code=400,
            detail="ENABLE_MAGELLAN=1: a YAML 'model' file is required for execution.",
        )
    raw = await model.read()
    if not raw:
        raise HTTPException(status_code=400, detail="Uploaded model file is empty.")
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise HTTPException(status_code=400, detail=f"Model file is not valid UTF-8: {exc}")


async def _dispatch_plan(
    plan_payload: dict,
    model_yaml: str | None,
    reset_dispatch_state: bool = False,
) -> dict:
    """Send a scheduled state plan to the active downstream service.

    When ENABLE_MAGELLAN=1, the plan is forwarded to Magellan's
    POST /planner/start endpoint with the supplied YAML model; otherwise it is
    forwarded to PyKirk's POST /plans endpoint.  When ``reset_dispatch_state``
    is True (used by ``/resume``), the PyKirk request includes a
    ``reset_dispatch_state=true`` query parameter that tells the dispatcher to
    discard its prior bookkeeping before initializing against the new plan.
    Returns the parsed JSON response from whichever service was invoked.
    """
    if ENABLE_MAGELLAN:
        if model_yaml is None:
            raise HTTPException(
                status_code=400,
                detail="ENABLE_MAGELLAN=1: a YAML 'model' file is required.",
            )
        model_filename = _save_magellan_model(model_yaml)
        magellan_payload = {
            "goalPlan": plan_payload,
            "exoPlan": None,
            "model": model_filename,
        }
        url = f"http://127.0.0.1:{MAGELLAN_PORT}/planner/start"
        log.info("Dispatching plan to Magellan at %s (model=%s)", url, model_filename)
        try:
            async with httpx.AsyncClient(timeout=300.0) as client:
                resp = await client.post(
                    url,
                    json=magellan_payload,
                    headers={"Content-Type": "application/json"},
                )
        except httpx.RequestError as exc:
            raise HTTPException(status_code=503, detail=f"Magellan unreachable: {exc}")
        if resp.status_code != 200:
            raise HTTPException(
                status_code=502,
                detail=f"Magellan error ({resp.status_code}): {resp.text}",
            )
        try:
            return resp.json()
        except Exception:
            # Magellan may return a raw plan string; surface it as-is.
            return {"plan": resp.text}

    url = f"http://127.0.0.1:{DISPATCHER_PORT}/plans"
    params = {"reset_dispatch_state": "true"} if reset_dispatch_state else None
    if reset_dispatch_state:
        # A reset is a new mission: restart the replanning budget.
        _mission.reset(model_yaml)
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            dispatch_resp = await client.post(
                url,
                json=plan_payload,
                headers={"Content-Type": "application/json"},
                params=params,
            )
    except httpx.RequestError as exc:
        raise HTTPException(status_code=503, detail=f"PyKirk dispatcher unreachable: {exc}")
    if dispatch_resp.status_code != 200:
        raise HTTPException(
            status_code=502,
            detail=f"PyKirk dispatcher error ({dispatch_resp.status_code}): {dispatch_resp.text}",
        )
    return dispatch_resp.json()


# ═══════════════════════════════════════════════════════════════════════════════
# Online replanning
# ═══════════════════════════════════════════════════════════════════════════════


async def _broadcast(payload: dict) -> None:
    """Fan a payload out to every /violations SSE subscriber."""
    for queue in list(_violation_subscribers):
        await queue.put(payload)


async def _notify_mission_status(payload: dict) -> None:
    """POST a terminal mission status to MISSION_STATUS_CALLBACK_URL, if set."""
    if not MISSION_STATUS_CALLBACK_URL:
        return
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.post(MISSION_STATUS_CALLBACK_URL, json=payload)
        log.info("Mission status forwarded to %s (status=%s)",
                 MISSION_STATUS_CALLBACK_URL, resp.status_code)
    except httpx.RequestError as exc:
        log.warning("Could not reach MISSION_STATUS_CALLBACK_URL %s: %s",
                    MISSION_STATUS_CALLBACK_URL, exc)


async def _pause_dispatcher(reason: str) -> bool:
    async with httpx.AsyncClient(timeout=5.0) as client:
        resp = await client.post(f"http://127.0.0.1:{DISPATCHER_PORT}/pause", json={"reason": reason})
    if resp.status_code == 200:
        return True
    log.warning("Dispatcher pause returned %s: %s", resp.status_code, resp.text)
    return False


async def _halt_dispatcher(reason: str) -> None:
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.post(f"http://127.0.0.1:{DISPATCHER_PORT}/halt", json={"reason": reason})
            log.warning("Dispatcher halt requested (status=%s)", resp.status_code)
    except Exception as exc:
        log.error("Failed to halt dispatcher: %s", exc)


async def _get_dispatch_history() -> dict:
    async with httpx.AsyncClient(timeout=5.0) as client:
        resp = await client.get(f"http://127.0.0.1:{DISPATCHER_PORT}/history")
    resp.raise_for_status()
    return resp.json()


async def _get_world_state() -> dict:
    """Flat {VARIABLE: value} view of the monitor's current state."""
    async with httpx.AsyncClient(timeout=5.0) as client:
        resp = await client.get(f"http://127.0.0.1:{MONITOR_PORT}/current-state")
    resp.raise_for_status()
    assignments = resp.json().get("assignments", {})
    return {
        var: (entry.get("value") if isinstance(entry, dict) else entry)
        for var, entry in assignments.items()
    }


class _ReplanInfeasible(Exception):
    pass


async def _kirk_replan(history: dict, world_state: dict) -> dict:
    """Ask Kirk to re-solve the mission's TPN clamped to the executed schedule
    and the observed world state.  Raises _ReplanInfeasible on a 422/409."""
    body = {
        "executedEvents": [
            {"event": e["event"], "time": e["time"]} for e in history.get("executed", [])
        ],
        "dispatchedEvents": [
            {"event": e["event"], "time": e["time"]} for e in history.get("dispatched", [])
        ],
        "worldState": world_state,
        "now": history.get("now", 0.0),
    }
    _save_plan(body, "replan_request")
    try:
        async with httpx.AsyncClient(timeout=300.0) as client:
            resp = await client.post(
                f"http://127.0.0.1:{KIRK_SERVE_PORT}/replan",
                json=body,
                headers={"Content-Type": "application/json"},
            )
    except httpx.RequestError as exc:
        raise HTTPException(status_code=503, detail=f"Kirk planning server unreachable: {exc}")
    if resp.status_code in (409, 422):
        raise _ReplanInfeasible(f"Kirk replan ({resp.status_code}): {resp.text}")
    if resp.status_code != 200:
        raise HTTPException(
            status_code=502,
            detail=f"Kirk planning server error ({resp.status_code}): {resp.text}",
        )
    try:
        return resp.json()
    except Exception:
        raise HTTPException(status_code=502, detail="Kirk returned non-JSON response")


async def _request_replan(reason: dict) -> dict:
    """Run one online replan cycle:

      pause dispatcher -> read executed schedule -> read world state ->
      Kirk /replan -> re-init monitor (state preserved, executed events
      replayed), oracle (history preserved), visualization ->
      POST the new plan to the dispatcher WITHOUT a reset (merge + resume).

    Replans are serialized; a violation that arrives while one is running is
    coalesced into a single follow-up cycle.  On infeasibility or budget
    exhaustion the dispatcher is halted (terminal failure), as before.
    Returns a summary dict (also broadcast on the /violations SSE stream).
    """
    if _mission.in_progress:
        # Coalesce: remember the newest reason and let the running cycle
        # re-run once it finishes.
        _mission.pending_reason = reason
        log.info("Replan already in progress; queued follow-up (%s)", reason.get("source"))
        return {"status": "replan-queued"}

    async with _mission.lock:
        _mission.in_progress = True
        try:
            summary = await _replan_once(reason)
            while _mission.pending_reason is not None and summary.get("status") == "replanned":
                follow_up = _mission.pending_reason
                _mission.pending_reason = None
                log.info("Running queued follow-up replan (%s)", follow_up.get("source"))
                summary = await _replan_once(follow_up)
            _mission.pending_reason = None
            return summary
        finally:
            _mission.in_progress = False


async def _fail_mission(reason: str) -> dict:
    log.error("Online replanning gave up: %s", reason)
    _mission.halted = True
    await _halt_dispatcher(reason)
    summary = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "source": "janeway:replan",
        "status": "replan-failed",
        "reason": reason,
        "replans": _mission.replans,
    }
    _mission.last_replan = summary
    await _broadcast(summary)
    return summary


async def _replan_once(reason: dict) -> dict:
    if _mission.halted:
        return {"status": "halted", "reason": "mission already halted"}
    if _mission.replans >= MAX_REPLANS:
        return await _fail_mission(f"replan budget exhausted ({MAX_REPLANS})")

    attempt = _mission.replans + 1
    log.warning("── Online replan #%d triggered by %s ──", attempt, reason)
    await _broadcast({
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "source": "janeway:replan",
        "status": "replanning",
        "attempt": attempt,
        "trigger": reason,
    })

    # 1. Pause (idempotent; the dispatcher may already have paused itself).
    try:
        paused = await _pause_dispatcher(f"replan #{attempt}: {reason.get('source')}")
    except Exception as exc:
        return await _fail_mission(f"could not pause dispatcher: {exc}")
    if not paused:
        return await _fail_mission("dispatcher refused to pause (mission already ended)")

    # 2. Let in-flight execution reports land, then read the executed schedule.
    await asyncio.sleep(REPLAN_SETTLE_SECONDS)
    try:
        history = await _get_dispatch_history()
        world_state = await _get_world_state()
    except Exception as exc:
        return await _fail_mission(f"could not read execution state: {exc}")
    log.info("Replan #%d input: %d executed, %d dispatched, now=%.3f, world=%s",
             attempt, len(history.get("executed", [])), len(history.get("dispatched", [])),
             history.get("now", 0.0), world_state)

    # 3. Kirk re-solves the clamped TPN.
    try:
        plan_payload = await _kirk_replan(history, world_state)
    except _ReplanInfeasible as exc:
        return await _fail_mission(str(exc))
    except HTTPException as exc:
        return await _fail_mission(f"Kirk replan error: {exc.detail}")
    _mission.replans = attempt
    _save_plan(plan_payload, f"replan{attempt}")

    # 4. Re-init the monitor (state preserved, executed events replayed), the
    #    oracle (history + fault counters preserved) and the visualization.
    executed_events = [
        {"event": e["event"], "time": e["time"]} for e in history.get("executed", [])
    ]
    await _initialize_monitor(plan_payload, resume=True, executed_events=executed_events)
    await _load_oracle_plan(plan_payload, resume=True)
    await _load_plan_visualization(plan_payload)

    # 5. Merge the new plan into the running mission (no reset: the executed
    #    prefix keeps its ids and stays in the dispatcher's history).
    try:
        detail = await _dispatch_plan(plan_payload, _mission.model_yaml, reset_dispatch_state=False)
    except HTTPException as exc:
        return await _fail_mission(f"dispatcher rejected replanned plan: {exc.detail}")

    summary = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "source": "janeway:replan",
        "status": "replanned",
        "attempt": attempt,
        "trigger": reason,
        "executed": len(executed_events),
        "world_state": world_state,
        "detail": detail,
    }
    _mission.last_replan = summary
    log.warning("── Online replan #%d complete; mission resumed ──", attempt)
    await _broadcast(summary)
    return summary


def _spawn_replan(reason: dict) -> None:
    """Fire-and-forget replan cycle (callbacks must return immediately)."""

    async def _run():
        try:
            await _request_replan(reason)
        except Exception as exc:  # pragma: no cover - last-resort logging
            log.exception("Unhandled error during online replan: %s", exc)
            await _fail_mission(f"unhandled replan error: {exc}")

    asyncio.get_event_loop().create_task(_run())


app = FastAPI(
    title="Execution as a Service",
    description=(
        "Submit an RMPL program to be planned by Kirk and dispatched by PyKirk. "
        "POST the raw RMPL text to /execute."
    ),
    lifespan=lifespan,
)


@app.post("/execute")
async def execute(request: Request):
    """
    Accept an RMPL program, generate a plan with Kirk, and dispatch it.

    Request body:
      • Raw RMPL program text (Content-Type: text/plain), or
      • JSON with an \"rmpl\" field (Content-Type: application/json), or
      • multipart/form-data with an \"rmpl\" field (text or file) and, when
        ENABLE_MAGELLAN=1, a \"model\" YAML file.

    Optional header:
      X-Package-Name: RMPL package name to plan (default: main)
    """
    content_type = request.headers.get("content-type", "")
    model_yaml: str | None = None
    if "application/json" in content_type:
        body = await request.json()
        if isinstance(body, dict) and "rmpl" in body:
            rmpl_text = body["rmpl"]
        else:
            raise HTTPException(status_code=400, detail="JSON body must contain an 'rmpl' key")
    elif "multipart/form-data" in content_type:
        form = await request.form()
        rmpl_field = form.get("rmpl")
        if rmpl_field is None:
            raise HTTPException(status_code=400, detail="multipart body must contain an 'rmpl' field")
        if isinstance(rmpl_field, UploadFile):
            rmpl_text = (await rmpl_field.read()).decode("utf-8")
        else:
            rmpl_text = str(rmpl_field)
        model_field = form.get("model")
        if isinstance(model_field, UploadFile):
            model_yaml = await _read_model_yaml(model_field)
    else:
        raw = await request.body()
        if not raw:
            raise HTTPException(status_code=400, detail="Request body must contain an RMPL program")
        rmpl_text = raw.decode("utf-8")

    if ENABLE_MAGELLAN and model_yaml is None:
        raise HTTPException(
            status_code=400,
            detail="ENABLE_MAGELLAN=1: send /execute as multipart/form-data with a 'model' YAML file.",
        )

    package_name = request.headers.get("x-package-name", "main")

    # ── Step 1: Generate plan via kirk-serve ──────────────────────────────
    log.info("Sending RMPL to kirk-serve for planning (package=%s)", package_name)
    try:
        async with httpx.AsyncClient(timeout=300.0) as client:
            resp = await client.post(
                f"http://127.0.0.1:{KIRK_SERVE_PORT}/plan",
                content=rmpl_text.encode(),
                headers={
                    "Content-Type": "text/plain",
                    "X-Package-Name": package_name,
                },
            )
    except httpx.RequestError as exc:
        raise HTTPException(status_code=503, detail=f"Kirk planning server unreachable: {exc}")

    if resp.status_code == 422:
        log.error("Kirk planning failed (422) for RMPL:\n%s", resp.text)
        raise HTTPException(status_code=422, detail="No feasible plan found for the given RMPL program")
    if resp.status_code != 200:
        log.error("Kirk planning error (%s) for RMPL:\n%s", resp.status_code, resp.text)
        raise HTTPException(
            status_code=502,
            detail=f"Kirk planning server error ({resp.status_code}): {resp.text}",
        )

    try:
        plan_payload = resp.json()
    except Exception:
        raise HTTPException(status_code=502, detail="Kirk returned non-JSON response")

    log.info("Plan received from kirk-serve")
    _save_plan(plan_payload, "rmpl")

    # ── Step 2: Initialize causal link monitor, oracle & plan visualization ─
    await _initialize_monitor(plan_payload)
    await _load_oracle_plan(plan_payload)
    await _load_plan_visualization(plan_payload)

    # ── Step 3: Dispatch plan to active downstream service ────────────────
    # /execute* starts a NEW mission: reset the dispatcher's state so event
    # ids overlapping a previous mission aren't filtered out as already-run
    # (issue #5) and no stale terminal status is replayed (issue #7).
    # /resume is the only endpoint that continues an existing mission.
    detail = await _dispatch_plan(plan_payload, model_yaml, reset_dispatch_state=True)
    log.info("Plan dispatched successfully (%s)", "magellan" if ENABLE_MAGELLAN else "pykirk")
    return JSONResponse(
        status_code=202,
        content={
            "status": "dispatched",
            "target": "magellan" if ENABLE_MAGELLAN else "pykirk",
            "detail": detail,
        },
    )


@app.post("/execute-pddl")
async def execute_pddl(
    domain: UploadFile = File(..., description="PDDL domain file"),
    problem: UploadFile = File(..., description="PDDL problem file"),
    plan: Optional[str] = Form(None, description="Temporal PDDL plan as plain text"),
    plan_file: Optional[UploadFile] = File(None, description="Temporal PDDL plan as a file upload"),
    model: Optional[UploadFile] = File(None, description="(Magellan only) YAML world/dynamics model"),
):
    """
    Accept a PDDL domain, problem, and temporal plan; convert to a state plan
    via pddl_to_sp; plan it through Kirk; and dispatch.

    Form fields:
      domain     – PDDL domain file upload
      problem    – PDDL problem file upload
      plan       – temporal plan as plain text (one '0.0: action(args) [dur]'
                   line per action). Either this OR ``plan_file`` is required.
      plan_file  – same temporal plan, uploaded as a file. Useful for clients
                   that pipe planner output without inlining it as a form
                   string.
      model      – (required when ENABLE_MAGELLAN=1) YAML model file describing
                   the world/dynamics for Magellan motion planning
    """
    if plan is None and plan_file is None:
        raise HTTPException(
            status_code=422,
            detail=(
                "Provide the temporal plan either as a 'plan' form field "
                "or as a 'plan_file' upload."
            ),
        )
    if plan is None:
        plan = (await plan_file.read()).decode("utf-8")

    model_yaml = await _read_model_yaml(model) if ENABLE_MAGELLAN else None

    domain_text = (await domain.read()).decode("utf-8")
    problem_text = (await problem.read()).decode("utf-8")

    # ── Step 1: Convert PDDL → state plan JSON via pddl_to_sp ────────────────
    log.info("Converting PDDL inputs to state plan JSON")
    try:
        # pddl_to_sp functions expect file paths, so write to temp files.
        with (
            tempfile.NamedTemporaryFile(mode="w", suffix=".pddl", delete=False) as df,
            tempfile.NamedTemporaryFile(mode="w", suffix=".pddl", delete=False) as pf,
        ):
            df.write(domain_text)
            pf.write(problem_text)
            domain_path = df.name
            problem_path = pf.name

        from json_skeleton import create_initial_json
        from populate import (
            populate_state_space,
            populate_constraints,
            populate_goal_episodes,
            populate_value_episodes,
        )
        import io_utils

        state_plan = create_initial_json()
        action_counts = populate_state_space(state_plan, plan, domain_path, problem_path)
        populate_constraints(state_plan, plan, domain_path, problem_path, action_counts)
        populate_goal_episodes(state_plan, plan, domain_path, problem_path, action_counts)
        populate_value_episodes(state_plan, plan, domain_path, problem_path, action_counts)
        state_plan_json = json.dumps(state_plan)
        _save_plan(state_plan, "pddl_to_sp")
        log.info("Generated state plan JSON from PDDL")
    except Exception as exc:
        log.exception("PDDL conversion error")
        raise HTTPException(status_code=422, detail=f"PDDL conversion error: {exc}")
    finally:
        for p in (domain_path, problem_path):
            try:
                os.unlink(p)
            except Exception:
                pass

    # ── Step 2: Send state plan to Kirk for planning ──────────────────────────
    log.info("Sending state plan to kirk for planning")
    try:
        async with httpx.AsyncClient(timeout=300.0) as client:
            resp = await client.post(
                f"http://127.0.0.1:{KIRK_SERVE_PORT}/plan-from-state-plan",
                content=state_plan_json.encode(),
                headers={"Content-Type": "application/json"},
            )
    except httpx.RequestError as exc:
        raise HTTPException(status_code=503, detail=f"Kirk planning server unreachable: {exc}")

    if resp.status_code == 422:
        log.error("Kirk planning failed (422) for state plan:\n%s", resp.text)
        raise HTTPException(status_code=422, detail="No feasible plan found for the given state plan")
    if resp.status_code != 200:
        log.error("Kirk planning error (%s) for state plan:\n%s", resp.status_code, resp.text)
        raise HTTPException(
            status_code=502,
            detail=f"Kirk planning server error ({resp.status_code}): {resp.text}",
        )

    try:
        plan_payload = resp.json()
    except Exception:
        raise HTTPException(status_code=502, detail="Kirk returned non-JSON response")

    log.info("Plan received from kirk")
    _save_plan(plan_payload, "pddl")

    # ── Step 3: Initialize causal link monitor, oracle & plan visualization ────
    await _initialize_monitor(plan_payload)
    await _load_oracle_plan(plan_payload)
    await _load_plan_visualization(plan_payload)

    # ── Step 4: Dispatch plan to active downstream service ──────────────────
    # New mission: reset dispatch state (see /execute).
    detail = await _dispatch_plan(plan_payload, model_yaml, reset_dispatch_state=True)
    log.info("PDDL plan dispatched successfully (%s)", "magellan" if ENABLE_MAGELLAN else "pykirk")
    return JSONResponse(
        status_code=202,
        content={
            "status": "dispatched",
            "target": "magellan" if ENABLE_MAGELLAN else "pykirk",
            "detail": detail,
        },
    )


@app.post("/execute-state-plan")
async def execute_state_plan(request: Request):
    """
    Accept a state plan JSON (same shape as the output of pddl_to_sp or any
    upstream planner that produces an Odo state plan), send it to Kirk for
    planning, and continue through the usual downstream pipeline (monitor
    initialization, oracle load, visualization, dispatch).

    Request body:
      • application/json — raw state plan JSON, or
      • multipart/form-data with a 'state_plan' file (JSON) and, when
        ENABLE_MAGELLAN=1, a 'model' YAML file describing the world/dynamics.
    """
    content_type = request.headers.get("content-type", "")
    model_yaml: str | None = None
    if "application/json" in content_type:
        try:
            state_plan = await request.json()
        except Exception as exc:
            raise HTTPException(status_code=400, detail=f"Invalid JSON body: {exc}")
    elif "multipart/form-data" in content_type:
        form = await request.form()
        state_plan_field = form.get("state_plan")
        if state_plan_field is None:
            raise HTTPException(
                status_code=400,
                detail="multipart body must contain a 'state_plan' field (JSON file or text)",
            )
        if isinstance(state_plan_field, UploadFile):
            raw = await state_plan_field.read()
        else:
            raw = str(state_plan_field).encode("utf-8")
        try:
            state_plan = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise HTTPException(status_code=400, detail=f"state_plan is not valid JSON: {exc}")
        model_field = form.get("model")
        if isinstance(model_field, UploadFile):
            model_yaml = await _read_model_yaml(model_field)
    else:
        raise HTTPException(
            status_code=400,
            detail=(
                "Request must be application/json or multipart/form-data "
                "(with 'state_plan' and, when ENABLE_MAGELLAN=1, 'model')"
            ),
        )

    if not isinstance(state_plan, dict):
        raise HTTPException(status_code=400, detail="State plan must be a JSON object")

    if ENABLE_MAGELLAN and model_yaml is None:
        raise HTTPException(
            status_code=400,
            detail=(
                "ENABLE_MAGELLAN=1: send /execute-state-plan as multipart/form-data "
                "with a 'state_plan' JSON file and a 'model' YAML file."
            ),
        )

    _save_plan(state_plan, "state_plan_input")
    state_plan_json = json.dumps(state_plan)

    # ── Step 1: Send state plan to Kirk for planning ──────────────────────────
    log.info("Sending state plan to kirk for planning")
    try:
        async with httpx.AsyncClient(timeout=300.0) as client:
            resp = await client.post(
                f"http://127.0.0.1:{KIRK_SERVE_PORT}/plan-from-state-plan",
                content=state_plan_json.encode(),
                headers={"Content-Type": "application/json"},
            )
    except httpx.RequestError as exc:
        raise HTTPException(status_code=503, detail=f"Kirk planning server unreachable: {exc}")

    if resp.status_code == 422:
        log.error("Kirk planning failed (422) for state plan:\n%s", resp.text)
        raise HTTPException(status_code=422, detail="No feasible plan found for the given state plan")
    if resp.status_code != 200:
        log.error("Kirk planning error (%s) for state plan:\n%s", resp.status_code, resp.text)
        raise HTTPException(
            status_code=502,
            detail=f"Kirk planning server error ({resp.status_code}): {resp.text}",
        )

    try:
        plan_payload = resp.json()
    except Exception:
        raise HTTPException(status_code=502, detail="Kirk returned non-JSON response")

    log.info("Plan received from kirk")
    _save_plan(plan_payload, "state_plan")

    # ── Step 2: Initialize causal link monitor, oracle & plan visualization ────
    await _initialize_monitor(plan_payload)
    await _load_oracle_plan(plan_payload)
    await _load_plan_visualization(plan_payload)

    # ── Step 3: Dispatch plan to active downstream service ──────────────────
    # New mission: reset dispatch state (see /execute).
    detail = await _dispatch_plan(plan_payload, model_yaml, reset_dispatch_state=True)
    log.info("State plan dispatched successfully (%s)", "magellan" if ENABLE_MAGELLAN else "pykirk")
    return JSONResponse(
        status_code=202,
        content={
            "status": "dispatched",
            "target": "magellan" if ENABLE_MAGELLAN else "pykirk",
            "detail": detail,
        },
    )


@app.post("/resume")
async def resume(request: Request):
    """
    Continue an existing mission with an updated state plan.

    Unlike `/execute*`, which initializes a fresh causal link monitor (wiping
    any previously-observed world state), `/resume` re-uses the monitor's
    existing `current_state` so the new plan is checked against the post-fault
    world view.  Use this after a fault halt: query `GET /state` for the live
    world state, generate a new state plan from there, and POST it here.

    Request body:
      • `application/json` — raw state plan JSON, or
      • `multipart/form-data` with a `state_plan` JSON file and (when
        `ENABLE_MAGELLAN=1`) a `model` YAML file.

    The plan is forwarded to Kirk's `/plan-from-state-plan` like the other
    execute endpoints, but the monitor is initialised via
    `/resume-state-plan` so observed state survives.  The dispatcher itself
    already supports restart via `RTEStarDispatcherWithReplanning.put_plan`.
    """
    content_type = request.headers.get("content-type", "")
    model_yaml: Optional[str] = None
    if "application/json" in content_type:
        try:
            state_plan = await request.json()
        except Exception as exc:
            raise HTTPException(status_code=400, detail=f"Invalid JSON body: {exc}")
    elif "multipart/form-data" in content_type:
        form = await request.form()
        state_plan_field = form.get("state_plan")
        if state_plan_field is None:
            raise HTTPException(
                status_code=400,
                detail="multipart body must contain a 'state_plan' field (JSON file or text)",
            )
        if isinstance(state_plan_field, UploadFile):
            raw = await state_plan_field.read()
        else:
            raw = str(state_plan_field).encode("utf-8")
        try:
            state_plan = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise HTTPException(status_code=400, detail=f"state_plan is not valid JSON: {exc}")
        model_field = form.get("model")
        if isinstance(model_field, UploadFile):
            model_yaml = await _read_model_yaml(model_field)
    else:
        raise HTTPException(
            status_code=400,
            detail=(
                "Request must be application/json or multipart/form-data "
                "(with 'state_plan' and, when ENABLE_MAGELLAN=1, 'model')"
            ),
        )

    if not isinstance(state_plan, dict):
        raise HTTPException(status_code=400, detail="State plan must be a JSON object")

    if ENABLE_MAGELLAN and model_yaml is None:
        raise HTTPException(
            status_code=400,
            detail=(
                "ENABLE_MAGELLAN=1: send /resume as multipart/form-data with a "
                "'state_plan' JSON file and a 'model' YAML file."
            ),
        )

    _save_plan(state_plan, "resume_input")
    state_plan_json = json.dumps(state_plan)

    # ── Step 1: Send state plan to Kirk for planning ──────────────────────────
    log.info("Sending state plan to kirk for resumption")
    try:
        async with httpx.AsyncClient(timeout=300.0) as client:
            resp = await client.post(
                f"http://127.0.0.1:{KIRK_SERVE_PORT}/plan-from-state-plan",
                content=state_plan_json.encode(),
                headers={"Content-Type": "application/json"},
            )
    except httpx.RequestError as exc:
        raise HTTPException(status_code=503, detail=f"Kirk planning server unreachable: {exc}")

    if resp.status_code == 422:
        log.error("Kirk planning failed (422) for resume state plan:\n%s", resp.text)
        raise HTTPException(status_code=422, detail="No feasible plan found for the given state plan")
    if resp.status_code != 200:
        log.error("Kirk planning error (%s) for resume state plan:\n%s", resp.status_code, resp.text)
        raise HTTPException(
            status_code=502,
            detail=f"Kirk planning server error ({resp.status_code}): {resp.text}",
        )

    try:
        plan_payload = resp.json()
    except Exception:
        raise HTTPException(status_code=502, detail="Kirk returned non-JSON response")

    log.info("Plan received from kirk (resume)")
    _save_plan(plan_payload, "resume")

    # ── Step 2: Re-init monitor (preserving observed state), reload oracle/vis
    await _initialize_monitor(plan_payload, resume=True)
    await _load_oracle_plan(plan_payload)
    await _load_plan_visualization(plan_payload)

    # ── Step 3: Dispatch the new plan; the dispatcher restarts the mission.
    # Pass ``reset_dispatch_state=True`` so the dispatcher discards its prior
    # ``history``/``dispatched`` sets — those refer to events in the previous
    # network's namespace and would otherwise filter every event of the new
    # plan out of ``new_controllables`` (see
    # ``initialize_rte_data_given_replan``).
    detail = await _dispatch_plan(plan_payload, model_yaml, reset_dispatch_state=True)
    log.info("Mission resumed successfully (%s)", "magellan" if ENABLE_MAGELLAN else "pykirk")
    return JSONResponse(
        status_code=202,
        content={
            "status": "resumed",
            "target": "magellan" if ENABLE_MAGELLAN else "pykirk",
            "detail": detail,
        },
    )


@app.get("/state")
async def get_state():
    """Return the current world state as tracked by the causal link monitor.

    Each state-update reported by the oracle or the ROS bridge updates this
    map; the response is a JSON object whose top-level structure matches the
    monitor's internal representation (an `assignments` dict mapping state
    variables to their most recently observed values).  Returns an empty
    object if no plan has been dispatched yet.
    """
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.get(f"http://127.0.0.1:{MONITOR_PORT}/current-state")
    except httpx.RequestError as exc:
        raise HTTPException(status_code=503, detail=f"Monitor unreachable: {exc}")
    if resp.status_code != 200:
        raise HTTPException(status_code=resp.status_code, detail=resp.text)
    state = resp.json()
    # Log the state served to whoever asked, so the Janeway log captures
    # every external view-of-the-world query alongside the monitor's own
    # log line.  Flatten the {variable: {variable, value}} shape into a
    # plain dict for readability.
    assignments = state.get("assignments", {}) if isinstance(state, dict) else {}
    flat = {var: (entry.get("value") if isinstance(entry, dict) else entry)
            for var, entry in assignments.items()}
    log.info("State queried via /state (%d assignment(s)): %s", len(flat), flat)
    return state


@app.post("/state-update")
async def post_state_update(request: Request):
    """Report observed world state to the causal link monitor.

    The body is a flat JSON object mapping state variables to their observed
    values, e.g. ``{"rover1.location": "science1", "rover1.has_sample": true}``
    (variable names are case-insensitive).  This is the same payload the
    local oracle and the ROS bridge send to the monitor's
    ``/observe-state-update``; exposing it here lets an external executive
    report state through the main API port alone.

    A value that contradicts an active causal link is a violation: it is
    broadcast on ``GET /violations`` and triggers a replan (or a halt when
    replanning is disabled), exactly as for oracle-reported state.  The
    response is the monitor's: ``success`` and the list of ``conflicts``.
    """
    try:
        update = await request.json()
    except json.JSONDecodeError:
        raise HTTPException(status_code=400, detail="Body must be JSON")
    if not isinstance(update, dict) or not update:
        raise HTTPException(status_code=400,
                            detail="Body must be a non-empty JSON object of variable: value")
    bad = [k for k, v in update.items() if not isinstance(v, (str, bool, int, float))]
    if bad:
        raise HTTPException(status_code=400,
                            detail=f"Values must be strings, numbers or booleans: {bad}")
    log.info("State update via /state-update: %s", update)
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(
                f"http://127.0.0.1:{MONITOR_PORT}/observe-state-update", json=update,
            )
    except httpx.RequestError as exc:
        raise HTTPException(status_code=503, detail=f"Monitor unreachable: {exc}")
    if resp.status_code != 200:
        raise HTTPException(status_code=resp.status_code, detail=resp.text)
    return resp.json()


@app.get("/health")
async def health():
    """Check liveness of this server and its downstream services."""
    checks = [
        (f"http://127.0.0.1:{KIRK_SERVE_PORT}/health", "kirk"),
        (f"http://127.0.0.1:{DISPATCHER_PORT}/docs", "dispatcher"),
        (f"http://127.0.0.1:{AGENT_PORT}/docs", "agent"),
        (f"http://127.0.0.1:{MONITOR_PORT}/docs", "monitor"),
        (f"http://127.0.0.1:{PLAN_VIS_PORT}/docs", "plan-visualization"),
    ]
    if ENABLE_ORACLE:
        checks.append((f"http://127.0.0.1:{ORACLE_PORT}/docs", "oracle"))
    checks.append((f"http://127.0.0.1:{TELEMETRY_PORT}/docs", "telemetry"))
    if ENABLE_VIS:
        checks.append((f"http://127.0.0.1:{VIS_PORT}/", "visualization"))

    results = {}
    async with httpx.AsyncClient(timeout=3.0) as client:
        for url, name in checks:
            try:
                r = await client.get(url)
                results[name] = "ok" if r.status_code < 500 else "degraded"
            except Exception:
                results[name] = "unreachable"

    overall = "ok" if all(v == "ok" for v in results.values()) else "degraded"
    return {"status": overall, "services": results}


@app.post("/violations")
async def receive_violation(request: Request):
    """Internal endpoint — receives both causal-link violations from the
    monitor and terminal mission-status notifications from the dispatcher.

    Both shapes are forwarded to every active SSE subscriber so external
    listeners (the visualization, CI scripts, etc.) get a single stream of
    plan-execution outcomes:

      * Violation payloads contain a ``violations`` list and trigger a
        dispatcher halt.
      * Mission-status payloads contain a ``status`` field (e.g.
        ``"completed"`` or ``"fail"``) and DO NOT halt — the dispatcher has
        already self-terminated and we just want subscribers to know.
    """
    payload = await request.json()
    status = payload.get("status")

    if status in ("completed", "fail"):
        if status == "completed":
            log.info("Mission completed: %s", payload)
        else:
            log.warning("Mission failed: %s", payload)
        await _broadcast(payload)
        await _notify_mission_status(payload)
        return {"status": "received", "kind": "mission-status"}

    if status == "replan-requested":
        # The dispatcher found the remaining plan temporally inconsistent and
        # paused itself.
        log.warning("Dispatcher requested a replan: %s", payload)
        await _broadcast(payload)
        if ENABLE_REPLANNING:
            _spawn_replan({"source": "dispatcher", "reason": payload.get("reason", "temporal"),
                           "detail": payload.get("detail")})
            return {"status": "received", "kind": "replan-request", "replan": "scheduled"}
        await _halt_dispatcher(f"temporal inconsistency: {payload.get('detail')}")
        return {"status": "received", "kind": "replan-request", "dispatcher": "halt requested"}

    log.warning("Causal link violation: %s", payload)
    await _broadcast(payload)

    if ENABLE_REPLANNING:
        _spawn_replan({"source": payload.get("source", "monitor"),
                       "violations": payload.get("violations", []),
                       "details": payload.get("details", [])})
        return {"status": "received", "replan": "scheduled"}

    # Replanning disabled: halt the dispatcher so no further actions are dispatched.
    await _halt_dispatcher(f"Causal link violation: {payload.get('violations', [])}")
    return {"status": "received", "dispatcher": "halt requested"}


@app.post("/replan")
async def replan_now(request: Request):
    """Trigger an online replan of the running mission by hand.

    Pauses the dispatcher, reads its executed schedule and the monitor's world
    state, asks Kirk to re-solve the mission's TPN with those clamped in, and
    resumes from the new plan.  Returns the replan summary (also published on
    the ``GET /violations`` stream).  409 when no replan is possible (mission
    already ended or replanning disabled), 422 when Kirk finds no plan.
    """
    if not ENABLE_REPLANNING:
        raise HTTPException(status_code=409, detail="ENABLE_REPLANNING=0")
    reason = {"source": "manual"}
    try:
        body = await request.json()
        if isinstance(body, dict):
            reason.update(body)
    except Exception:
        pass
    summary = await _request_replan(reason)
    status = summary.get("status")
    if status == "replanned":
        return JSONResponse(status_code=202, content=summary)
    if status == "replan-queued":
        return JSONResponse(status_code=202, content=summary)
    if status == "replan-failed":
        raise HTTPException(status_code=422, detail=summary)
    raise HTTPException(status_code=409, detail=summary)


@app.get("/replan")
async def replan_status():
    """Replanning status of the current mission."""
    return {
        "enabled": ENABLE_REPLANNING,
        "max_replans": MAX_REPLANS,
        "replans": _mission.replans,
        "in_progress": _mission.in_progress,
        "halted": _mission.halted,
        "last": _mission.last_replan,
    }


@app.get("/violations")
async def stream_violations(request: Request):
    """SSE stream of causal link violations. Connect to receive real-time alerts."""
    queue: asyncio.Queue = asyncio.Queue()
    _violation_subscribers.append(queue)

    async def event_generator():
        try:
            while True:
                if await request.is_disconnected():
                    break
                try:
                    payload = await asyncio.wait_for(queue.get(), timeout=30.0)
                    data = json.dumps(payload)
                    yield f"data: {data}\n\n"
                except asyncio.TimeoutError:
                    # Send keepalive comment to prevent connection timeout
                    yield ": keepalive\n\n"
        finally:
            _violation_subscribers.remove(queue)

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
