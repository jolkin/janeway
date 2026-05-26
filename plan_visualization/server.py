"""
Plan Visualization Server.

Accepts a Kirk plan JSON and an execution schedule, then serves a D3.js-based
web interface showing:
  - A directed graph of events (nodes) and temporal constraints (edges),
    labeled with bounds and causal link assignments.
  - A timeline placing every executed event at its actual execution time.

Subscribes to the telemetry WebSocket to receive execution events in real time.
"""

import asyncio
import json
import logging
import os
import sys
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Optional

import httpx
import websockets
from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from .state_plan_dto import (
    StatePlanDTO,
    TemporalConstraintExpressionDTO,
    StateConstraintExpressionDTO,
    ExecutionDTO,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    stream=sys.stdout,
)
log = logging.getLogger("plan-vis")

TELEMETRY_WS_URL = os.environ.get("TELEMETRY_WS_URL", "ws://127.0.0.1:8002/ws")
DISPATCHER_PORT = int(os.environ.get("DISPATCHER_PORT", "9000"))

# In-memory store for the current visualization data
_current_graph: Optional[dict] = None
# Accumulated raw execution times from telemetry {event_id: wall_clock_time}
_exec_times: dict[str, float] = {}
# The start event ID for the current plan (used as t=0 reference)
_start_event: Optional[str] = None
# Connected frontend WebSocket clients for live updates
_ws_clients: set[WebSocket] = set()
# Background telemetry listener task
_telemetry_task: Optional[asyncio.Task] = None


class VisualizationPayload(BaseModel):
    plan: dict
    executions: list[dict] = []


def _format_bound(val) -> str:
    """Format a temporal bound for display."""
    if isinstance(val, str) and val.lower() == "infinity":
        return "\u221e"
    if isinstance(val, float) and val == float("inf"):
        return "\u221e"
    if isinstance(val, (int, float)):
        return f"{val:g}"
    return str(val)


def _extract_causal_link_label(annotation) -> str:
    """Extract a human-readable causal link label from an annotation."""
    if annotation is None:
        return ""
    cl = annotation.causalLink
    if cl is False or cl is True:
        return ""
    # cl is a StateConstraintExpressionDTO
    left = cl.left
    right = cl.right
    if isinstance(left, dict):
        left = left.get("stateVar", str(left))
    return f"{left} = {right}"


def _normalize_time(raw_time: float) -> float:
    """Normalize a wall-clock execution time relative to the start event (t=0)."""
    start_time = _exec_times.get(_start_event) if _start_event else None
    
    if start_time is not None:
        return raw_time - start_time
    return raw_time


def _build_graph(plan_data: dict, exec_times: dict[str, float]) -> dict:
    """
    Build a graph structure from the Kirk plan JSON and execution times.

    Returns a dict with:
      - nodes: list of {id, label, executionTime, activity, type}
      - edges: list of {source, target, lb, ub, causalLink, id}
      - episodes: list of {id, startEvent, endEvent, activityName, duration}
      - timelineEnd: float
    """
    global _start_event

    # Parse the plan using the DTO — handle both wrapped and unwrapped formats
    raw = plan_data.get("goalPlan", plan_data)
    plan = StatePlanDTO.model_validate(raw)

    # Record start event for time normalization (from the plan itself)
    _start_event = plan.startEvent

    # Determine t=0 from the start event's wall-clock execution time
    t0 = exec_times.get(_start_event, 0.0)

    # Build episode lookup: event → activity name (from both episode types)
    event_to_activity: dict[str, str] = {}
    for ep in plan.goalEpisodes + plan.valueEpisodes:
        event_to_activity.setdefault(ep.startEvent, ep.activityName)
        event_to_activity.setdefault(ep.endEvent, ep.activityName)

    # Only value episodes are shown on the timeline (goal episodes are
    # redundant — they share the same events but describe preconditions).
    episodes_out = []
    for ep in plan.valueEpisodes:
        episodes_out.append({
            "id": ep.id,
            "startEvent": ep.startEvent,
            "endEvent": ep.endEvent,
            "activityName": ep.activityName,
            "durationLB": ep.duration.lowerBound,
            "durationUB": ep.duration.upperBound,
        })

    # Build nodes from events in the state space
    nodes = []
    for ev in plan.stateSpace.events:
        eid = ev.id
        # Determine node type
        if eid == _start_event:
            ntype = "start"
        elif eid in ("end-event", "end", "END"):
            ntype = "end"
        elif "_start" in eid.lower() or eid.lower().startswith("start"):
            ntype = "action_start"
        elif "_end" in eid.lower() or eid.lower().endswith("end"):
            ntype = "action_end"
        else:
            ntype = "event"

        raw_t = exec_times.get(eid)
        normalized_t = (raw_t - t0) if raw_t is not None else None

        nodes.append({
            "id": eid,
            "label": eid,
            "executionTime": normalized_t,
            "activity": event_to_activity.get(eid, ""),
            "type": ntype,
        })

    # Build edges from temporal constraints
    edges = []
    for c in plan.constraints:
        expr = c.expression
        if not isinstance(expr, TemporalConstraintExpressionDTO):
            continue

        cl_label = _extract_causal_link_label(c.annotations)
        has_causal = bool(
            c.annotations
            and c.annotations.causalLink
            and c.annotations.causalLink is not True
        )

        edges.append({
            "id": c.id,
            "source": expr.from_.ref,
            "target": expr.to_.ref,
            "lb": _format_bound(expr.lowerBound),
            "ub": _format_bound(expr.upperBound),
            "causalLink": cl_label,
            "hasCausalLink": has_causal,
        })

    # Mark episode duration constraints (startEvent → endEvent).  Kirk emits
    # the duration of every durative action as an explicit `simpleTemporal`
    # constraint from start to end (so it shows up in `plan.constraints`),
    # while non-explicit durations only live on the episode's `duration`
    # field.  We want both shapes to render with episode styling, so:
    #   1. If the constraint edge already exists, upgrade it in place
    #      (set isEpisode=True, replace bounds with the episode's duration
    #      so the label matches the episode rather than the constraint).
    #   2. Otherwise, add a fresh episode edge.
    edges_by_pair = {(e["source"], e["target"]): e for e in edges}
    for ep in plan.goalEpisodes:
        pair = (ep.startEvent, ep.endEvent)
        existing = edges_by_pair.get(pair)
        if existing is not None:
            existing["isEpisode"] = True
            existing["id"] = f"episode_{ep.id}"
            existing["lb"] = _format_bound(ep.duration.lowerBound)
            existing["ub"] = _format_bound(ep.duration.upperBound)
        else:
            new_edge = {
                "id": f"episode_{ep.id}",
                "source": ep.startEvent,
                "target": ep.endEvent,
                "lb": _format_bound(ep.duration.lowerBound),
                "ub": _format_bound(ep.duration.upperBound),
                "causalLink": "",
                "hasCausalLink": False,
                "isEpisode": True,
            }
            edges.append(new_edge)
            edges_by_pair[pair] = new_edge

    # Compute timeline end from normalized execution times
    node_times = [n["executionTime"] for n in nodes if n["executionTime"] is not None]
    timeline_end = max(node_times) if node_times else 100.0

    return {
        "nodes": nodes,
        "edges": edges,
        "episodes": episodes_out,
        "timelineEnd": timeline_end,
    }


def _renormalize_all_nodes():
    """Re-normalize all node execution times from raw _exec_times using the current t0."""
    if _current_graph is None:
        return
    t0 = _exec_times.get(_start_event, 0.0) if _start_event else 0.0
    max_t = 0.0
    for node in _current_graph["nodes"]:
        raw_t = _exec_times.get(node["id"])
        if raw_t is not None:
            normalized = raw_t - t0
            node["executionTime"] = normalized
            if normalized > max_t:
                max_t = normalized
    _current_graph["timelineEnd"] = max_t if max_t > 0 else 100.0


def _update_node_execution_time(event: str, raw_time: float):
    """Update a single node's execution time in the current graph (normalized).
    Returns True if this was the start event (requiring a full resync)."""
    if _current_graph is None:
        return False
    if event == _start_event:
        # The start event defines t=0 — renormalize everything
        _renormalize_all_nodes()
        return True
    normalized = _normalize_time(raw_time)
    for node in _current_graph["nodes"]:
        if node["id"] == event:
            node["executionTime"] = normalized
            break
    if normalized > _current_graph["timelineEnd"]:
        _current_graph["timelineEnd"] = normalized
    return False


async def _broadcast(msg_dict: dict):
    """Send a JSON message to all connected frontend WebSocket clients."""
    msg = json.dumps(msg_dict)
    dead = set()
    for ws in _ws_clients:
        try:
            await ws.send_text(msg)
        except Exception:
            dead.add(ws)
    _ws_clients.difference_update(dead)


async def _broadcast_execution(event: str, raw_time: float, controllable: bool):
    """Send an execution update to all connected frontend WebSocket clients."""
    await _broadcast({
        "type": "execution",
        "event": event,
        "time": _normalize_time(raw_time),
        "controllable": controllable,
    })


async def _broadcast_resync():
    """Send all current normalized node times to frontends so they can resync."""
    if _current_graph is None:
        return
    node_times = {
        n["id"]: n["executionTime"]
        for n in _current_graph["nodes"]
        if n["executionTime"] is not None
    }
    await _broadcast({
        "type": "resync",
        "nodeTimes": node_times,
        "timelineEnd": _current_graph["timelineEnd"],
    })


async def _telemetry_listener():
    """Connect to the telemetry WebSocket and collect execution events."""
    while True:
        try:
            log.info("Connecting to telemetry WebSocket at %s", TELEMETRY_WS_URL)
            async with websockets.connect(TELEMETRY_WS_URL) as ws:
                log.info("Connected to telemetry WebSocket")
                async for raw_msg in ws:
                    try:
                        msg = json.loads(raw_msg)
                    except json.JSONDecodeError:
                        continue

                    if msg.get("type") == "execution":
                        data = msg.get("data", {})
                        event = data.get("event")
                        time = data.get("time")
                        controllable = data.get("controllable", True)
                        if event and time is not None:
                            _exec_times[event] = time
                            is_start = _update_node_execution_time(event, time)
                            if is_start:
                                # Start event arrived — send full resync
                                await _broadcast_resync()
                            else:
                                await _broadcast_execution(event, time, controllable)
                            log.info("Execution: %s at t=%.3f (normalized=%.3f)",
                                     event, time, _normalize_time(time))

        except asyncio.CancelledError:
            log.info("Telemetry listener cancelled")
            return
        except Exception as exc:
            log.warning("Telemetry WebSocket error: %s — reconnecting in 3s", exc)
            await asyncio.sleep(3)


# Store the raw plan data so we can rebuild the graph with new execution times
_raw_plan: Optional[dict] = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _telemetry_task
    _telemetry_task = asyncio.create_task(_telemetry_listener())
    yield
    _telemetry_task.cancel()
    try:
        await _telemetry_task
    except asyncio.CancelledError:
        pass


app = FastAPI(title="Plan Visualization", lifespan=lifespan)

STATIC_DIR = Path(__file__).parent / "static"
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


@app.post("/load")
async def load_visualization(payload: VisualizationPayload):
    """Load a plan and execution schedule for visualization."""
    global _current_graph, _raw_plan
    try:
        _raw_plan = payload.plan

        # Merge any explicitly provided executions with telemetry-collected ones
        for ex in payload.executions:
            e = ExecutionDTO.model_validate(ex)
            _exec_times[e.event] = e.execution_time

        _current_graph = _build_graph(payload.plan, _exec_times)
        log.info(
            "Loaded plan with %d nodes, %d edges",
            len(_current_graph["nodes"]),
            len(_current_graph["edges"]),
        )

        # Notify connected frontends that a new plan is available
        await _broadcast({"type": "plan_loaded"})

        return {
            "status": "ok",
            "nodes": len(_current_graph["nodes"]),
            "edges": len(_current_graph["edges"]),
        }
    except Exception as exc:
        log.exception("Failed to load plan")
        raise HTTPException(status_code=422, detail=str(exc))


@app.get("/graph")
async def get_graph():
    """Return the current graph data as JSON for D3."""
    if _current_graph is None:
        raise HTTPException(
            status_code=404, detail="No plan loaded. POST to /load first."
        )
    return _current_graph


@app.websocket("/ws")
async def websocket_endpoint(ws: WebSocket):
    """
    Frontend WebSocket for live execution updates.

    Clients receive messages like:
      {"type": "execution", "event": "drive_1_end", "time": 5.2, "controllable": true}
    """
    await ws.accept()
    _ws_clients.add(ws)
    log.info("Frontend WebSocket client connected (%d total)", len(_ws_clients))
    try:
        while True:
            # Keep connection alive; client doesn't send anything meaningful
            await ws.receive_text()
    except WebSocketDisconnect:
        pass
    finally:
        _ws_clients.discard(ws)
        log.info("Frontend WebSocket client disconnected (%d remaining)", len(_ws_clients))


@app.post("/monitor-event")
async def monitor_event(request: Request):
    """Receive a state update (with optional violations) from the causal link monitor."""
    payload = await request.json()
    # Stamp with the latest normalized execution time so the frontend can
    # position state-update markers at the correct point on the timeline.
    if _exec_times:
        latest_raw = max(_exec_times.values())
        payload["time"] = _normalize_time(latest_raw)
    log.info("Monitor event: %s", payload)
    await _broadcast(payload)
    return {"status": "ok"}


@app.get("/dispatchable-form")
async def get_dispatchable_form():
    """Proxy the dispatchable form from pykirk's dispatcher for frontend visualization."""
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.get(f"http://127.0.0.1:{DISPATCHER_PORT}/dispatchable-form")
        if resp.status_code != 200:
            raise HTTPException(status_code=resp.status_code, detail=resp.text)
        return resp.json()
    except httpx.RequestError as exc:
        raise HTTPException(status_code=503, detail=f"Dispatcher unreachable: {exc}")


@app.get("/", response_class=HTMLResponse)
async def index():
    """Serve the visualization page."""
    index_path = STATIC_DIR / "index.html"
    if not index_path.exists():
        raise HTTPException(status_code=500, detail="index.html not found")
    return FileResponse(index_path)
