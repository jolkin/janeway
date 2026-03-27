"""
EaaS ↔ ROS 2 bridge node.

Outbound (container → ROS):
    Connects to the EaaS telemetry WebSocket, receives dispatch/execution
    events, and publishes them on the /eaas/events topic as JSON strings.

Inbound (ROS → container):
    Subscribes to /eaas/execution_reports.  Each message is expected to be a
    JSON string with the shape:
        {"event": "<event_name>", "execution_time": <float>, "is_controllable": <bool>}
    The node POSTs a ReportExecutionPayloadDTO to the dispatcher's
    /handle_execution endpoint so the dispatch cycle can advance.

    Subscribes to /eaas/state_updates.  Each message is expected to be a
    JSON string mapping state variable names to their observed values, e.g.:
        {"rover1.at": "waypoint2", "rover1.has_sample": true}
    The node POSTs the update to the causal link monitor's
    /observe-state-update endpoint.
"""

import asyncio
import json
import threading
from functools import partial

import aiohttp
import rclpy
import websockets
from rclpy.node import Node
from std_msgs.msg import String


class EaaSBridgeNode(Node):
    """ROS 2 node that bridges EaaS dispatch events and execution reports."""

    def __init__(self):
        super().__init__("eaas_bridge")

        # ── Declare parameters ────────────────────────────────────────────
        self.declare_parameter("telemetry_ws_url", "ws://localhost:8002/ws")
        self.declare_parameter("dispatcher_url", "http://localhost:9000")
        self.declare_parameter("monitor_url", "http://localhost:9003")
        self.declare_parameter("event_topic", "/eaas/events")
        self.declare_parameter("report_topic", "/eaas/execution_reports")
        self.declare_parameter("state_update_topic", "/eaas/state_updates")
        self.declare_parameter("reconnect_delay", 3.0)

        self._ws_url = self.get_parameter("telemetry_ws_url").value
        self._dispatcher_url = self.get_parameter("dispatcher_url").value
        self._monitor_url = self.get_parameter("monitor_url").value
        event_topic = self.get_parameter("event_topic").value
        report_topic = self.get_parameter("report_topic").value
        state_update_topic = self.get_parameter("state_update_topic").value
        self._reconnect_delay = self.get_parameter("reconnect_delay").value

        # ── Publishers / subscribers ──────────────────────────────────────
        self._event_pub = self.create_publisher(String, event_topic, 10)
        self._report_sub = self.create_subscription(
            String, report_topic, self._on_execution_report, 10,
        )
        self._state_update_sub = self.create_subscription(
            String, state_update_topic, self._on_state_update, 10,
        )

        # ── Background asyncio loop for WebSocket + HTTP ──────────────────
        self._loop = asyncio.new_event_loop()
        self._ws_thread = threading.Thread(
            target=self._run_event_loop, daemon=True,
        )
        self._ws_thread.start()

        self.get_logger().info(
            f"EaaS bridge started — WS: {self._ws_url}, "
            f"dispatcher: {self._dispatcher_url}, "
            f"monitor: {self._monitor_url}, "
            f"pub: {event_topic}, sub: {report_topic}, "
            f"state_updates: {state_update_topic}"
        )

    # ── Asyncio event loop (runs in background thread) ────────────────────

    def _run_event_loop(self):
        asyncio.set_event_loop(self._loop)
        self._loop.run_until_complete(self._ws_listen_forever())

    async def _ws_listen_forever(self):
        """Connect to the telemetry WebSocket and keep reconnecting."""
        while rclpy.ok():
            try:
                self.get_logger().info(f"Connecting to telemetry WS at {self._ws_url}")
                async with websockets.connect(self._ws_url) as ws:
                    self.get_logger().info("WebSocket connected")
                    async for raw in ws:
                        self._publish_event(raw)
            except (
                websockets.ConnectionClosed,
                ConnectionRefusedError,
                OSError,
            ) as exc:
                self.get_logger().warn(
                    f"WebSocket disconnected ({exc}), "
                    f"retrying in {self._reconnect_delay}s"
                )
            except Exception as exc:
                self.get_logger().error(f"Unexpected WS error: {exc}")
            await asyncio.sleep(self._reconnect_delay)

    # ── Outbound: telemetry WS → ROS topic ────────────────────────────────

    def _publish_event(self, raw_json: str):
        """Publish a telemetry event to the ROS topic."""
        msg = String()
        msg.data = raw_json
        self._event_pub.publish(msg)

        try:
            payload = json.loads(raw_json)
            event_name = payload.get("data", {}).get("event", "?")
            verb = payload.get("data", {}).get("verb", "?")
            self.get_logger().info(f"Published event: {event_name} (verb={verb})")
        except json.JSONDecodeError:
            self.get_logger().debug("Published raw event (non-JSON)")

    # ── Inbound: ROS topic → dispatcher HTTP ──────────────────────────────

    def _on_execution_report(self, msg: String):
        """Forward an execution report from ROS to the dispatcher."""
        try:
            report = json.loads(msg.data)
        except json.JSONDecodeError:
            self.get_logger().error("Ignoring malformed execution report (not JSON)")
            return

        # Validate required fields.
        for field in ("event", "execution_time", "is_controllable"):
            if field not in report:
                self.get_logger().error(
                    f"Execution report missing required field '{field}'"
                )
                return

        self.get_logger().info(
            f"Forwarding execution report: {report['event']} "
            f"(t={report['execution_time']}, "
            f"controllable={report['is_controllable']})"
        )
        asyncio.run_coroutine_threadsafe(
            self._post_execution(report), self._loop,
        )

    async def _post_execution(self, report: dict):
        """POST an execution report to the dispatcher."""
        url = f"{self._dispatcher_url}/executions"
        payload = {
            "executions": [
                {
                    "event": report["event"],
                    "execution_time": report["execution_time"],
                    "is_controllable": report["is_controllable"],
                }
            ]
        }
        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    url,
                    json=payload,
                    headers={"Content-Type": "application/json"},
                    timeout=aiohttp.ClientTimeout(total=10),
                ) as resp:
                    if resp.status == 200:
                        self.get_logger().info(
                            f"Dispatcher accepted report for {report['event']}"
                        )
                    else:
                        body = await resp.text()
                        self.get_logger().warn(
                            f"Dispatcher returned {resp.status}: {body}"
                        )
        except Exception as exc:
            self.get_logger().error(f"Failed to POST execution report: {exc}")

    # ── Inbound: ROS topic → monitor HTTP (state updates) ─────────────

    def _on_state_update(self, msg: String):
        """Forward a state update from ROS to the causal link monitor."""
        try:
            update = json.loads(msg.data)
        except json.JSONDecodeError:
            self.get_logger().error("Ignoring malformed state update (not JSON)")
            return

        if not isinstance(update, dict) or not update:
            self.get_logger().error("State update must be a non-empty JSON object")
            return

        self.get_logger().info(f"Forwarding state update: {update}")
        asyncio.run_coroutine_threadsafe(
            self._post_state_update(update), self._loop,
        )

    async def _post_state_update(self, update: dict):
        """POST a state update to the causal link monitor."""
        url = f"{self._monitor_url}/observe-state-update"
        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    url,
                    json=update,
                    headers={"Content-Type": "application/json"},
                    timeout=aiohttp.ClientTimeout(total=10),
                ) as resp:
                    if resp.status == 200:
                        body = await resp.json()
                        self.get_logger().info(
                            f"Monitor accepted state update (success={body.get('success')})"
                        )
                    else:
                        body = await resp.text()
                        self.get_logger().warn(
                            f"Monitor returned {resp.status}: {body}"
                        )
        except Exception as exc:
            self.get_logger().error(f"Failed to POST state update: {exc}")


def main(args=None):
    rclpy.init(args=args)
    node = EaaSBridgeNode()
    try:
        rclpy.spin(node)
    except KeyboardInterrupt:
        pass
    finally:
        node.destroy_node()
        rclpy.try_shutdown()


if __name__ == "__main__":
    main()
