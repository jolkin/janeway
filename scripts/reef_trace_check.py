#!/usr/bin/env python3
"""Record and check a Scott's Reef execution trace from a running Janeway.

Two sub-commands:

  record   Poll the PyKirk dispatcher (/history) and the causal-link monitor
           (/current-state, /active-links) while a mission runs, and write
           every sample plus the final history to a trace JSON.  Stops when
           the dispatcher reports a terminal mission status.

  check    Verify a recorded trace against the dispatched plan:
             * at least N `observe` episodes ran to completion, and
             * the glider was never at a boat's site while that boat activity
               was executing.  A boat's site is the location its negated
               over-all requirement excludes (the only place the model says
               where a boat is).
           It also reads the monitor log to confirm the negated links were
           actually watched (held through the consumer's END) and that no
           link violation was raised.

Only the standard library is used so it runs on the host Python.
"""
import argparse
import json
import re
import sys
import time
import urllib.error
import urllib.request

TERMINAL = {"completed", "fail"}


def _get(url, timeout=3.0):
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            return json.load(r)
    except (urllib.error.URLError, TimeoutError, ValueError) as exc:
        return {"_error": str(exc)}


# ── record ────────────────────────────────────────────────────────────────


def record(args):
    samples = []
    t0 = time.time()
    last_status = None
    stable_since = None
    print(f"[record] polling dispatcher={args.dispatcher} monitor={args.monitor} "
          f"every {args.interval}s", flush=True)
    while True:
        hist = _get(f"{args.dispatcher}/history")
        state = _get(f"{args.monitor}/current-state")
        links = _get(f"{args.monitor}/active-links")
        status = hist.get("status")
        assignments = {
            k: v.get("value") if isinstance(v, dict) else v
            for k, v in (state.get("assignments") or {}).items()
        }
        sample = {
            "wall": round(time.time() - t0, 3),
            "now": hist.get("now"),
            "status": status,
            "executed": len(hist.get("executed") or []),
            "dispatched": len(hist.get("dispatched") or []),
            "state": assignments,
            "active_links": links if isinstance(links, list) else links.get("active_links", links),
        }
        samples.append(sample)
        if status != last_status:
            print(f"[record] t={sample['wall']:.1f}s status={status} executed={sample['executed']} "
                  f"state={assignments}", flush=True)
            last_status = status
        if status in TERMINAL:
            # Give trailing state updates a moment to land, then stop.
            if stable_since is None:
                stable_since = time.time()
            elif time.time() - stable_since > args.settle:
                break
        if args.max_seconds and time.time() - t0 > args.max_seconds:
            print("[record] max-seconds reached, stopping", flush=True)
            break
        time.sleep(args.interval)

    final_history = _get(f"{args.dispatcher}/history")
    out = {"history": final_history, "samples": samples,
           "final_state": _get(f"{args.monitor}/current-state")}
    with open(args.out, "w") as f:
        json.dump(out, f, indent=1)
    print(f"[record] wrote {args.out}: {len(samples)} samples, status={final_history.get('status')}, "
          f"executed={len(final_history.get('executed') or [])}", flush=True)
    return 0


# ── check ─────────────────────────────────────────────────────────────────


def _excluded_site(expr):
    """Return (variable, value) for a notApplication{x: equalApplication}."""
    if not isinstance(expr, dict) or expr.get("$type") != "notApplication":
        return None
    x = expr.get("x", {})
    if x.get("$type") != "equalApplication":
        return None
    left = x.get("left")
    var = left.get("stateVar") if isinstance(left, dict) else left
    return (str(var).upper(), x.get("right"))


def _equal_effect(expr):
    if not isinstance(expr, dict) or expr.get("$type") != "equalApplication":
        return None
    left = expr.get("left")
    var = left.get("stateVar") if isinstance(left, dict) else left
    return (str(var).upper(), expr.get("right"))


def load_plan(path):
    with open(path) as f:
        d = json.load(f)
    raw = d.get("goalPlan", d)
    episodes = {}
    for ep in raw.get("goalEpisodes", []):
        key = (ep["startEvent"], ep["endEvent"])
        e = episodes.setdefault(key, {"name": ep.get("activityName", ""),
                                      "start": ep["startEvent"], "end": ep["endEvent"],
                                      "excludes": [], "effects": []})
        for sc in ep.get("overAllConstraints", []):
            ex = _excluded_site(sc.get("expression"))
            if ex:
                e["excludes"].append(ex)
    for ep in raw.get("valueEpisodes", []):
        key = (ep["startEvent"], ep["endEvent"])
        e = episodes.setdefault(key, {"name": ep.get("activityName", ""),
                                      "start": ep["startEvent"], "end": ep["endEvent"],
                                      "excludes": [], "effects": []})
        if not e["name"]:
            e["name"] = ep.get("activityName", "")
        for ec in ep.get("endConstraints", []):
            eff = _equal_effect(ec.get("expression"))
            if eff:
                e["effects"].append(eff)
    return list(episodes.values())


def check(args):
    episodes = load_plan(args.plan)
    with open(args.trace) as f:
        trace = json.load(f)
    history = trace["history"]
    times = {e["event"]: e["time"] for e in history.get("executed", [])}
    problems = []
    print(f"[check] mission status: {history.get('status')}, executed events: {len(times)}")
    if history.get("status") != "completed":
        problems.append(f"mission status is {history.get('status')!r}, expected 'completed'")

    def interval(ep):
        s, e = times.get(ep["start"]), times.get(ep["end"])
        return (s, e) if s is not None and e is not None else None

    # 1. Observations.
    observes = [ep for ep in episodes if ep["name"].upper().startswith(args.observe_activity.upper())]
    done = [ep for ep in observes if interval(ep)]
    print(f"[check] observe episodes in plan: {len(observes)}, completed in trace: {len(done)}")
    for ep in done:
        s, e = interval(ep)
        print(f"         {ep['start']:<24} [{s:8.1f}, {e:8.1f}]")
    if len(done) < args.required_observations:
        problems.append(f"only {len(done)} observe episodes completed, need {args.required_observations}")
    final_obs = (trace.get("final_state", {}).get("assignments") or {}).get(args.location_var.replace("LOCATION", "OBSERVATIONS"))
    if final_obs is not None:
        print(f"[check] monitor's final observation count: {final_obs.get('value') if isinstance(final_obs, dict) else final_obs}")

    # 2. Glider location vs boat intervals.
    #    (a) model-derived timeline: the location variable changes at the END
    #        of every episode whose end effect writes it;
    #    (b) observed timeline: the monitor's current-state samples.
    writes = []
    for ep in episodes:
        for var, val in ep["effects"]:
            if var == args.location_var and ep["end"] in times:
                writes.append((times[ep["end"]], val, ep["end"]))
    writes.sort()
    initial = args.initial_location

    def model_location_at(t):
        loc = initial
        for wt, val, _ in writes:
            if wt <= t:
                loc = val
        return loc

    def model_locations_within(s, e):
        locs = {model_location_at(s)}
        for wt, val, _ in writes:
            if s <= wt <= e:
                locs.add(val)
        return locs

    samples = [(smp["now"], smp["state"].get(args.location_var))
               for smp in trace.get("samples", [])
               if smp.get("now") is not None and smp["state"].get(args.location_var) is not None]
    print(f"[check] location writes (model): {[(round(t, 1), v) for t, v, _ in writes]}")
    print(f"[check] observed location samples: {len(samples)}")

    boats = [ep for ep in episodes if ep["excludes"]]
    print(f"[check] boat activities with exclusions: {len(boats)}")
    watched = 0
    for ep in sorted(boats, key=lambda x: times.get(x["start"], 1e18)):
        iv = interval(ep)
        excl = [v for var, v in ep["excludes"] if var == args.location_var]
        if iv is None:
            problems.append(f"{ep['name']} ({ep['start']}) did not execute to completion")
            print(f"         {ep['name']:<18} NOT EXECUTED  excludes {excl}")
            continue
        s, e = iv
        model_locs = model_locations_within(s, e)
        obs_locs = {loc for t, loc in samples if s <= t <= e}
        bad_model = model_locs & set(excl)
        bad_obs = obs_locs & set(excl)
        flag = "OK " if not (bad_model or bad_obs) else "BAD"
        print(f"   {flag}   {ep['name']:<18} [{s:8.1f}, {e:8.1f}] excludes {excl}  "
              f"glider(model)={sorted(model_locs)} glider(observed)={sorted(obs_locs) or '-'}")
        watched += 1
        if bad_model:
            problems.append(f"{ep['name']}: glider location {sorted(bad_model)} (from plan effects) "
                            f"inside boat interval [{s:.1f}, {e:.1f}]")
        if bad_obs:
            problems.append(f"{ep['name']}: observed glider location {sorted(bad_obs)} "
                            f"inside boat interval [{s:.1f}, {e:.1f}]")

    # 3. Monitor log: negated links actually watched, no violations.
    if args.monitor_log:
        with open(args.monitor_log, errors="replace") as f:
            log = f.read()
        held_neg = len(re.findall(r"Held until END \(over :all\): [^\n]*!=", log))
        released = len(re.findall(r"Released \(over :all satisfied through END\)", log))
        conflicts = len(re.findall(r"CONFLICT|VIOLATION", log))
        activated_neg = len(re.findall(r"Activated: [^\n]*!=", log))
        print(f"[check] monitor log: negated links activated={activated_neg}, "
              f"held through consumer={held_neg}, released at END={released}, "
              f"conflict/violation lines={conflicts}")
        if activated_neg < len(boats):
            problems.append(f"monitor activated only {activated_neg} negated links for {len(boats)} boat activities")
        if held_neg < len(boats):
            problems.append(f"monitor held only {held_neg} negated links through their consumer")
        if conflicts:
            problems.append(f"monitor log reports {conflicts} conflict/violation line(s)")

    print()
    if problems:
        print("[check] FAILED:")
        for p in problems:
            print(f"   - {p}")
        return 1
    print(f"[check] PASSED: {len(done)} observations, glider never at a boat's site "
          f"during any of {watched} boat activities.")
    return 0


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("record")
    r.add_argument("--out", required=True)
    r.add_argument("--dispatcher", default="http://localhost:9000")
    r.add_argument("--monitor", default="http://localhost:9003")
    r.add_argument("--interval", type=float, default=0.5)
    r.add_argument("--settle", type=float, default=3.0)
    r.add_argument("--max-seconds", type=float, default=0)
    r.set_defaults(fn=record)
    c = sub.add_parser("check")
    c.add_argument("--plan", required=True, help="dispatched plan JSON (generated_plans/<ts>_rmpl.json)")
    c.add_argument("--trace", required=True, help="trace JSON written by `record`")
    c.add_argument("--monitor-log", default=None, help="generated_plans/monitor.log")
    c.add_argument("--required-observations", type=int, default=4)
    c.add_argument("--observe-activity", default="OBSERVE")
    c.add_argument("--location-var", default="GLIDER0.LOCATION")
    c.add_argument("--initial-location", default="start")
    c.set_defaults(fn=check)
    args = ap.parse_args()
    sys.exit(args.fn(args))


if __name__ == "__main__":
    main()
