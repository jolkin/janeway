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
from datetime import datetime
from pathlib import Path

import httpx
from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import JSONResponse
from starlette.responses import StreamingResponse

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    stream=sys.stdout,
)
log = logging.getLogger("eaas")

GENERATED_PLANS_DIR = Path(__file__).parent / "generated_plans"
GENERATED_PLANS_DIR.mkdir(exist_ok=True)

KIRK_SERVE_PORT = int(os.environ.get("KIRK_PORT", "7000"))
DISPATCHER_PORT = int(os.environ.get("DISPATCHER_PORT", "9000"))
AGENT_PORT = int(os.environ.get("LOCAL_AGENT_PORT", "9001"))
ORACLE_PORT = int(os.environ.get("LOCAL_ORACLE_PORT", "9002"))

KIRK_BINARY = os.environ.get("KIRK_BINARY", "/app/kirk/kirk")
PYKIRK_DIR = os.environ.get("PYKIRK_DIR", "/app/pykirk")
PDDL_TO_SP_DIR = os.environ.get("PDDL_TO_SP_DIR", "/app/pddl_to_sp")
ROBUST_EXEC_DIR = os.environ.get("ROBUST_EXEC_DIR", "/app/robust-execution")
MONITOR_PORT = int(os.environ.get("MONITOR_PORT", "9003"))
SERVER_PORT = int(os.environ.get("SERVER_PORT", "8000"))
ENABLE_ORACLE = os.environ.get("ENABLE_ORACLE", "1").strip() not in ("0", "", "false", "False")
ENABLE_VIS = os.environ.get("ENABLE_VIS", "0").strip() not in ("0", "", "false", "False")
SIMULATE_FAULTS = os.environ.get("SIMULATE_FAULTS", "0")
TELEMETRY_PORT = int(os.environ.get("TELEMETRY_PORT", "8002"))
VIS_PORT = int(os.environ.get("VIS_PORT", "5173"))
PLAN_VIS_PORT = int(os.environ.get("PLAN_VIS_PORT", "9004"))
PLAN_VIS_DIR = os.environ.get("PLAN_VIS_DIR", str(Path(__file__).parent / "plan_visualization"))
# Public WebSocket URL used by the browser to reach the telemetry server.
# Must be reachable from the client machine, not from inside the container.
VIS_WS_URL = os.environ.get("VIS_WS_URL", f"ws://localhost:{TELEMETRY_PORT}/ws")

# Make pddl_to_sp importable (uses bare imports internally).
if PDDL_TO_SP_DIR not in sys.path:
    sys.path.insert(0, PDDL_TO_SP_DIR)

_processes: list[subprocess.Popen] = []
_violation_subscribers: list[asyncio.Queue] = []


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


def _start_process(cmd: list[str], cwd: str | None, env: dict, name: str) -> subprocess.Popen:
    log.info("Starting %s: %s", name, " ".join(cmd))
    proc = subprocess.Popen(
        cmd,
        cwd=cwd,
        env=env,
        stdout=sys.stdout.fileno(),
        stderr=sys.stderr.fileno(),
    )
    _processes.append(proc)
    return proc


@asynccontextmanager
async def lifespan(app: FastAPI):
    base_env = os.environ.copy()

    # ── Kirk planning server ───────────────────────────────────────────────
    _start_process(
        [KIRK_BINARY, "serve", "--port", str(KIRK_SERVE_PORT)],
        cwd=None,
        env=base_env,
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
    }

    # When the oracle is disabled, external systems (e.g. a ROS bridge) provide
    # execution reports directly.  Bind the dispatcher to 0.0.0.0 so it is
    # reachable from outside the container.
    dispatcher_host = "127.0.0.1" if ENABLE_ORACLE else "0.0.0.0"

    services = [
        ("src.pykirk.dispatch.api.dispatcher.main:app", DISPATCHER_PORT, dispatcher_host, "dispatcher"),
        ("src.pykirk.dispatch.api.local.agent.main:app", AGENT_PORT, "127.0.0.1", "local-agent"),
    ]
    if ENABLE_ORACLE:
        services.append(
            ("src.pykirk.dispatch.api.local.oracle.main:app", ORACLE_PORT, "127.0.0.1", "local-oracle"),
        )
    else:
        log.info("Oracle disabled — dispatcher bound to 0.0.0.0:%s for external execution reports", DISPATCHER_PORT)

    for uvicorn_app, port, host, name in services:
        _start_process(
            ["uv", "run", "uvicorn", uvicorn_app,
             "--host", host, "--port", str(port)],
            cwd=PYKIRK_DIR,
            env={**pykirk_env, "PORT": str(port)},
            name=name,
        )

    # ── Causal link monitor server ─────────────────────────────────────────
    # When the oracle is disabled, external systems (e.g. a ROS bridge) send
    # state updates directly, so the monitor must be reachable from outside.
    monitor_host = "127.0.0.1" if ENABLE_ORACLE else "0.0.0.0"
    _start_process(
        ["uv", "run", "uvicorn",
         "planexecutive.monitor.server.server:app",
         "--host", monitor_host, "--port", str(MONITOR_PORT)],
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
    # Start the telemetry server when visualization is enabled OR when the
    # oracle is disabled (the ROS bridge needs the telemetry WebSocket to
    # receive dispatch events).
    if ENABLE_VIS or not ENABLE_ORACLE:
        reason = []
        if ENABLE_VIS:
            reason.append("visualization enabled")
        if not ENABLE_ORACLE:
            reason.append("oracle disabled (ROS bridge needs telemetry WS)")
        log.info("Starting telemetry server — %s", ", ".join(reason))
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

    # ── Visualization frontend (optional) ────────────────────────────────
    if ENABLE_VIS:
        log.info("Visualization enabled — starting Vite dev server")
        _start_process(
            ["npm", "run", "dev", "--",
             "--host", "0.0.0.0",
             "--port", str(VIS_PORT)],
            cwd=f"{PYKIRK_DIR}/visualization",
            env={**base_env, "VITE_TELEMETRY_WS_URL": VIS_WS_URL},
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
    if ENABLE_VIS or not ENABLE_ORACLE:
        checks.append((f"http://127.0.0.1:{TELEMETRY_PORT}/docs", "telemetry"))
    if ENABLE_VIS:
        checks.append((f"http://127.0.0.1:{VIS_PORT}/", "visualization"))
    for url, name in checks:
        ready = await wait_for_http(url, timeout=120.0)
        if ready:
            log.info("%s is ready at %s", name, url)
        else:
            log.warning("%s did not become ready at %s within timeout", name, url)

    yield

    log.info("Shutting down services...")
    for proc in _processes:
        proc.terminate()
    for proc in _processes:
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()


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


async def _load_oracle_plan(plan_payload: dict):
    """Send the plan to the oracle so it can extract causal links for state updates."""
    if not ENABLE_ORACLE:
        return
    log.info("Loading plan into oracle for causal link extraction")
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.post(
                f"http://127.0.0.1:{ORACLE_PORT}/plan",
                json=plan_payload,
                headers={"Content-Type": "application/json"},
            )
        if resp.status_code == 200:
            log.info("Oracle plan loaded: %s", resp.json())
        else:
            log.warning("Oracle plan load returned %s: %s", resp.status_code, resp.text)
    except httpx.RequestError as exc:
        log.warning("Could not reach oracle: %s", exc)


async def _initialize_monitor(plan_payload: dict):
    """Send the plan to the causal link monitor for initialization."""
    log.info("Initializing causal link monitor with plan")
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.post(
                f"http://127.0.0.1:{MONITOR_PORT}/initialize-state-plan",
                json=plan_payload,
                headers={"Content-Type": "application/json"},
            )
        if resp.status_code == 200:
            log.info("Causal link monitor initialized successfully")
        else:
            log.warning("Monitor initialization returned %s: %s", resp.status_code, resp.text)
    except httpx.RequestError as exc:
        log.warning("Could not reach causal link monitor: %s", exc)


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
    Accept an RMPL program, generate a plan with Kirk, and dispatch it via PyKirk.

    Request body: raw RMPL program text (Content-Type: text/plain) or JSON with
    an \"rmpl\" field (Content-Type: application/json).

    Optional header:
      X-Package-Name: RMPL package name to plan (default: main)
    """
    content_type = request.headers.get("content-type", "")
    if "application/json" in content_type:
        body = await request.json()
        if isinstance(body, dict) and "rmpl" in body:
            rmpl_text = body["rmpl"]
        else:
            raise HTTPException(status_code=400, detail="JSON body must contain an 'rmpl' key")
    else:
        raw = await request.body()
        if not raw:
            raise HTTPException(status_code=400, detail="Request body must contain an RMPL program")
        rmpl_text = raw.decode("utf-8")

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

    # ── Step 3: Dispatch plan via pykirk dispatcher ───────────────────────
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            dispatch_resp = await client.post(
                f"http://127.0.0.1:{DISPATCHER_PORT}/plans",
                json=plan_payload,
                headers={"Content-Type": "application/json"},
            )
    except httpx.RequestError as exc:
        raise HTTPException(status_code=503, detail=f"PyKirk dispatcher unreachable: {exc}")

    if dispatch_resp.status_code != 200:
        raise HTTPException(
            status_code=502,
            detail=f"PyKirk dispatcher error ({dispatch_resp.status_code}): {dispatch_resp.text}",
        )

    log.info("Plan dispatched successfully")
    return JSONResponse(
        status_code=202,
        content={"status": "dispatched", "detail": dispatch_resp.json()},
    )


@app.post("/execute-pddl")
async def execute_pddl(
    domain: UploadFile = File(..., description="PDDL domain file"),
    problem: UploadFile = File(..., description="PDDL problem file"),
    plan: str = Form(..., description="Temporal PDDL plan (e.g. '0.0: action(args) [dur]' lines)"),
):
    """
    Accept a PDDL domain, problem, and temporal plan; convert to a state plan
    via pddl_to_sp; plan it through Kirk; and dispatch via PyKirk.

    Form fields:
      domain  – PDDL domain file upload
      problem – PDDL problem file upload
      plan    – temporal plan as plain text (form field)
    """
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

    # ── Step 4: Dispatch plan via pykirk dispatcher ───────────────────────────
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            dispatch_resp = await client.post(
                f"http://127.0.0.1:{DISPATCHER_PORT}/plans",
                json=plan_payload,
                headers={"Content-Type": "application/json"},
            )
    except httpx.RequestError as exc:
        raise HTTPException(status_code=503, detail=f"PyKirk dispatcher unreachable: {exc}")

    if dispatch_resp.status_code != 200:
        raise HTTPException(
            status_code=502,
            detail=f"PyKirk dispatcher error ({dispatch_resp.status_code}): {dispatch_resp.text}",
        )

    log.info("PDDL plan dispatched successfully")
    return JSONResponse(
        status_code=202,
        content={"status": "dispatched", "detail": dispatch_resp.json()},
    )


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
    if ENABLE_VIS or not ENABLE_ORACLE:
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
    """Internal endpoint — receives violation reports from the causal link monitor.

    When a violation is received, the dispatcher is halted to prevent further
    actions from being dispatched under an invalid plan state.
    """
    payload = await request.json()
    log.warning("Causal link violation: %s", payload)
    for queue in _violation_subscribers:
        await queue.put(payload)

    # Halt the dispatcher so no further actions are dispatched.
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.post(
                f"http://127.0.0.1:{DISPATCHER_PORT}/halt",
                json={"reason": f"Causal link violation: {payload.get('violations', [])}"},
            )
            log.warning("Dispatcher halt requested (status=%s)", resp.status_code)
    except Exception as exc:
        log.error("Failed to halt dispatcher: %s", exc)

    return {"status": "received", "dispatcher": "halt requested"}


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
