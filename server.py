"""
Execution-as-a-Service HTTP server.

Accepts RMPL programs via POST /execute, generates a plan using the kirk
planning server, and dispatches it through the pykirk dispatcher.
"""

import asyncio
import json
import logging
import os
import subprocess
import sys
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    stream=sys.stdout,
)
log = logging.getLogger("eaas")

KIRK_SERVE_PORT = int(os.environ.get("KIRK_PORT", "7000"))
DISPATCHER_PORT = int(os.environ.get("DISPATCHER_PORT", "9000"))
AGENT_PORT = int(os.environ.get("LOCAL_AGENT_PORT", "9001"))
ORACLE_PORT = int(os.environ.get("LOCAL_ORACLE_PORT", "9002"))

KIRK_BINARY = os.environ.get("KIRK_BINARY", "/app/kirk/kirk")
PYKIRK_DIR = os.environ.get("PYKIRK_DIR", "/app/pykirk")

_processes: list[subprocess.Popen] = []


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
        "TELEMETRY_PORT": "8002",
    }

    for uvicorn_app, port, name in [
        ("src.pykirk.dispatch.api.dispatcher.main:app", DISPATCHER_PORT, "dispatcher"),
        ("src.pykirk.dispatch.api.local.agent.main:app", AGENT_PORT, "local-agent"),
        ("src.pykirk.dispatch.api.local.oracle.main:app", ORACLE_PORT, "local-oracle"),
    ]:
        _start_process(
            ["uv", "run", "uvicorn", uvicorn_app,
             "--host", "127.0.0.1", "--port", str(port)],
            cwd=PYKIRK_DIR,
            env={**pykirk_env, "PORT": str(port)},
            name=name,
        )

    # ── Wait for all services to be ready ──────────────────────────────────
    log.info("Waiting for services to become ready...")
    checks = [
        (f"http://127.0.0.1:{KIRK_SERVE_PORT}/health", "kirk-serve"),
        (f"http://127.0.0.1:{DISPATCHER_PORT}/docs", "dispatcher"),
        (f"http://127.0.0.1:{AGENT_PORT}/docs", "local-agent"),
        (f"http://127.0.0.1:{ORACLE_PORT}/docs", "local-oracle"),
    ]
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
        raise HTTPException(status_code=422, detail="No feasible plan found for the given RMPL program")
    if resp.status_code != 200:
        raise HTTPException(
            status_code=502,
            detail=f"Kirk planning server error ({resp.status_code}): {resp.text}",
        )

    try:
        plan_payload = resp.json()
    except Exception:
        raise HTTPException(status_code=502, detail="Kirk returned non-JSON response")

    log.info("Plan received from kirk-serve, dispatching to pykirk")

    # ── Step 2: Dispatch plan via pykirk dispatcher ───────────────────────
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


@app.get("/health")
async def health():
    """Check liveness of this server and its downstream services."""
    results = {}
    async with httpx.AsyncClient(timeout=3.0) as client:
        for url, name in [
            (f"http://127.0.0.1:{KIRK_SERVE_PORT}/health", "kirk"),
            (f"http://127.0.0.1:{DISPATCHER_PORT}/docs", "dispatcher"),
            (f"http://127.0.0.1:{AGENT_PORT}/docs", "agent"),
            (f"http://127.0.0.1:{ORACLE_PORT}/docs", "oracle"),
        ]:
            try:
                r = await client.get(url)
                results[name] = "ok" if r.status_code < 500 else "degraded"
            except Exception:
                results[name] = "unreachable"

    overall = "ok" if all(v == "ok" for v in results.values()) else "degraded"
    return {"status": overall, "services": results}
