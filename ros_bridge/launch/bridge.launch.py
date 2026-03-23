"""Launch the EaaS ROS bridge node with configurable parameters."""

from launch import LaunchDescription
from launch.actions import DeclareLaunchArgument
from launch.substitutions import LaunchConfiguration
from launch_ros.actions import Node


def generate_launch_description():
    return LaunchDescription([
        DeclareLaunchArgument(
            "telemetry_ws_url",
            default_value="ws://localhost:8002/ws",
            description="WebSocket URL of the EaaS telemetry service",
        ),
        DeclareLaunchArgument(
            "dispatcher_url",
            default_value="http://localhost:9000",
            description="HTTP base URL of the EaaS dispatcher",
        ),
        DeclareLaunchArgument(
            "event_topic",
            default_value="/eaas/events",
            description="ROS topic for outbound dispatch events",
        ),
        DeclareLaunchArgument(
            "report_topic",
            default_value="/eaas/execution_reports",
            description="ROS topic for inbound execution reports",
        ),
        Node(
            package="ros_bridge",
            executable="bridge_node",
            name="eaas_bridge",
            parameters=[{
                "telemetry_ws_url": LaunchConfiguration("telemetry_ws_url"),
                "dispatcher_url": LaunchConfiguration("dispatcher_url"),
                "event_topic": LaunchConfiguration("event_topic"),
                "report_topic": LaunchConfiguration("report_topic"),
            }],
            output="screen",
        ),
    ])
