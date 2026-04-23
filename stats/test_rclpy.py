#!/usr/bin/env python3
"""
rclpy diagnostic — run with both the stats venv AND as the service sees it.

  # What the CLI sees (setup.bash sourced):
  python3 stats/test_rclpy.py

  # What the service sees (no setup.bash):
  env -i HOME=/home/pi USER=pi PATH=/usr/bin:/bin \
    PYTHONPATH=$(systemctl show rov-stats | grep ^Environment | grep -o 'PYTHONPATH=[^ ]*' | cut -d= -f2) \
    /home/pi/rovcamera/stats/venv/bin/python3 stats/test_rclpy.py
"""
import os, sys

ROS_DOMAIN_ID   = "42"
FASTDDS_PROFILE = "/home/pi/fastdds_config.xml"

print("=" * 60)
print("ENVIRONMENT")
print("=" * 60)
print(f"  PYTHONPATH       : {os.environ.get('PYTHONPATH', '(not set)')}")
print(f"  ROS_DOMAIN_ID    : {os.environ.get('ROS_DOMAIN_ID', '(not set)')}")
print(f"  FASTDDS_PROFILE  : {FASTDDS_PROFILE} {'(exists)' if os.path.exists(FASTDDS_PROFILE) else '(MISSING)'}")
print(f"  python exe       : {sys.executable}")

print("\n" + "=" * 60)
print("sys.path (in order)")
print("=" * 60)
for p in sys.path:
    print(f"  {p}")

print("\n" + "=" * 60)
print("rclpy candidates on sys.path")
print("=" * 60)
import glob
for p in sys.path:
    candidate = os.path.join(p, "rclpy")
    if os.path.isdir(candidate):
        init = os.path.join(candidate, "__init__.py")
        print(f"  {candidate}")
        print(f"    __init__.py: {'exists' if os.path.exists(init) else 'MISSING'}")
        if os.path.exists(init):
            attrs = []
            ns = {}
            try:
                with open(init) as f:
                    exec(compile(f.read(), init, "exec"), ns)
                attrs = [k for k in ns if not k.startswith("_")]
            except Exception as e:
                attrs = [f"(parse error: {e})"]
            print(f"    top-level names: {', '.join(attrs[:10]) or '(none)'}")

print("\n" + "=" * 60)
print("Attempting import")
print("=" * 60)

os.environ["ROS_DOMAIN_ID"] = ROS_DOMAIN_ID
if os.path.exists(FASTDDS_PROFILE):
    os.environ["FASTRTPS_DEFAULT_PROFILES_FILE"] = FASTDDS_PROFILE

# Find the real rclpy and prioritise it
ros_site_pkgs = sorted(glob.glob("/opt/ros/*/lib/python*/site-packages"), reverse=True)
for rsp in ros_site_pkgs:
    if os.path.isdir(os.path.join(rsp, "rclpy")) and rsp not in sys.path:
        print(f"  Inserting into sys.path: {rsp}")
        sys.path.insert(0, rsp)
        break

try:
    import rclpy
    print(f"  rclpy imported from : {rclpy.__file__}")
    print(f"  dir(rclpy)          : {[a for a in dir(rclpy) if not a.startswith('_')]}")
except ImportError as e:
    print(f"  IMPORT FAILED: {e}")
    sys.exit(1)

print("\n" + "=" * 60)
print("rclpy init + node discovery")
print("=" * 60)
import time
try:
    rclpy.init()
    print("  init() OK")
    node = rclpy.create_node("rclpy_diag")
    print("  create_node() OK")
    deadline = time.time() + 2.0
    while time.time() < deadline:
        rclpy.spin_once(node, timeout_sec=0.1)
    pairs = node.get_node_names_and_namespaces()
    nodes = [f"{ns.rstrip('/')}/{name}" for name, ns in pairs if name != "rclpy_diag"]
    print(f"  nodes found: {nodes or '(none)'}")
    node.destroy_node()
    rclpy.shutdown()
except Exception as e:
    print(f"  FAILED: {e}")
