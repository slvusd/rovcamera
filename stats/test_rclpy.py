#!/usr/bin/env python3
"""
Quick rclpy diagnostic — run on the Pi to verify node discovery works.
Usage:
    python3 test_rclpy.py
"""
import os, sys, time

# Must match how nodes are started
os.environ["ROS_DOMAIN_ID"] = "42"
FASTDDS_PROFILE = "/home/pi/fastdds_config.xml"
if os.path.exists(FASTDDS_PROFILE):
    os.environ["FASTRTPS_DEFAULT_PROFILES_FILE"] = FASTDDS_PROFILE
    print(f"[env] FASTRTPS_DEFAULT_PROFILES_FILE={FASTDDS_PROFILE}")
else:
    print(f"[env] {FASTDDS_PROFILE} not found — using default DDS config")

print(f"[env] ROS_DOMAIN_ID={os.environ['ROS_DOMAIN_ID']}")

print("\n[1] Importing rclpy...")
try:
    import rclpy
    print(f"      OK — rclpy found at {rclpy.__file__}")
except ImportError as e:
    print(f"      FAILED: {e}")
    print("\nrclpy is not importable. Options:")
    print("  - Rebuild venv: rm -rf stats/venv && ./install.sh stats")
    print("  - Or run with system python: python3 test_rclpy.py")
    sys.exit(1)

print("\n[2] Initialising rclpy...")
try:
    rclpy.init()
    print("      OK")
except Exception as e:
    print(f"      FAILED: {e}")
    sys.exit(1)

print("\n[3] Creating node 'rclpy_test'...")
try:
    node = rclpy.create_node("rclpy_test")
    print("      OK")
except Exception as e:
    print(f"      FAILED: {e}")
    sys.exit(1)

print("\n[4] Spinning for 2s to allow DDS discovery...")
deadline = time.time() + 2.0
while time.time() < deadline:
    rclpy.spin_once(node, timeout_sec=0.1)

print("\n[5] Node names and namespaces:")
try:
    pairs = node.get_node_names_and_namespaces()
    own = "rclpy_test"
    others = [(n, ns) for n, ns in pairs if n != own]
    if others:
        for name, ns in others:
            print(f"      {ns.rstrip('/')}/{name}")
    else:
        print("      (none found)")
        print("\n      Possible causes:")
        print("      - No nodes running (check: ros2 node list)")
        print("      - Wrong DOMAIN_ID (check both sides match ROS_DOMAIN_ID=42)")
        print("      - Wrong/missing FASTRTPS_DEFAULT_PROFILES_FILE")
        print("      - DDS multicast blocked on this network interface")
except Exception as e:
    print(f"      FAILED: {e}")

node.destroy_node()
rclpy.shutdown()
print("\nDone.")
