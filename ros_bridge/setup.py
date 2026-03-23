from setuptools import find_packages, setup

package_name = "ros_bridge"

setup(
    name=package_name,
    version="0.1.0",
    packages=find_packages(exclude=["test"]),
    data_files=[
        ("share/ament_index/resource_index/packages", ["resource/" + package_name]),
        ("share/" + package_name, ["package.xml"]),
        ("share/" + package_name + "/launch", ["launch/bridge.launch.py"]),
    ],
    install_requires=[
        "setuptools",
        "websockets",
        "aiohttp",
    ],
    zip_safe=True,
    entry_points={
        "console_scripts": [
            "bridge_node = ros_bridge.bridge_node:main",
        ],
    },
)
