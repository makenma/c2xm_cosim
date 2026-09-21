#!/usr/bin/env python3
"""Exercise the real XLS Proc network using the repository's HNF checker.

This is supplemental protocol coverage, not a replacement for real gem5.
Both the test generator and the XLS peer must exit successfully; the
generator also checks read bytes and committed write bytes against its DDR.
"""
import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time


def run_case(root, output, xls_runtime, ir, name, scenario, extra):
    case_dir = output / name
    case_dir.mkdir()
    socket_dir = Path(tempfile.mkdtemp(prefix="c2xm_proto_"))
    socket_path = socket_dir / "peer.sock"
    start = time.monotonic()
    processes = []
    result = {"name": name, "scenario": scenario, "passed": False}
    try:
        with (case_dir / "generator.log").open("w") as glog, \
                (case_dir / "xls.log").open("w") as xlog:
            generator = subprocess.Popen([
                sys.executable, str(root / "fake_gem5.py"),
                "--socket", str(socket_path), "--scenario", scenario,
                "--max-time", "40", *extra,
            ], stdout=glog, stderr=subprocess.STDOUT)
            processes.append(generator)
            deadline = time.monotonic() + 15
            while not socket_path.exists():
                if generator.poll() is not None or time.monotonic() > deadline:
                    raise RuntimeError("test generator did not create its socket")
                time.sleep(0.05)
            peer = subprocess.Popen([
                sys.executable, str(root / "xls_cosim_peer.py"),
                "--socket", str(socket_path), "--max-time", "50",
                "--xls-runtime", str(xls_runtime), "--ir", str(ir),
            ], stdout=xlog, stderr=subprocess.STDOUT)
            processes.append(peer)
            generator_rc = generator.wait(timeout=65)
            peer_rc = peer.wait(timeout=15)
        result.update(generator_rc=generator_rc, xls_rc=peer_rc)
        result["passed"] = (
            generator_rc == 0 and peer_rc == 0
            and f"PASS scenario={scenario}" in (case_dir / "generator.log").read_text()
        )
    except Exception as exc:
        result["error"] = str(exc)
    finally:
        for process in processes:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()
        socket_path.unlink(missing_ok=True)
        socket_dir.rmdir()
    result["wall_seconds"] = round(time.monotonic() - start, 3)
    print(json.dumps(result), flush=True)
    if not result["passed"]:
        for log in ("generator.log", "xls.log"):
            path = case_dir / log
            if path.exists():
                print(f"{path}:\n" + "\n".join(path.read_text().splitlines()[-12:]), flush=True)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output")
    parser.add_argument(
        "--xls-runtime",
        default=os.environ.get("C2XM_XLS_RUNTIME", "bin/xls_proc_rpc_main"),
    )
    parser.add_argument("--ir", default=os.environ.get("C2XM_XLS_IR"))
    args = parser.parse_args()
    root = Path(__file__).resolve().parent
    xls_runtime = Path(args.xls_runtime)
    if not xls_runtime.is_absolute():
        xls_runtime = root / xls_runtime
    if not xls_runtime.is_file() or not os.access(xls_runtime, os.X_OK):
        parser.error(f"XLS runtime is not executable: {xls_runtime}")
    if not args.ir:
        parser.error("pass --ir or set C2XM_XLS_IR")
    ir = Path(args.ir).expanduser().resolve()
    if not ir.is_file():
        parser.error(f"optimized XLS IR not found: {ir}")
    output = Path(args.output) if args.output else root / "results" / time.strftime("protocol-%Y%m%d-%H%M%S")
    output.mkdir(parents=True, exist_ok=False)
    cases = [
        ("read", "read", []),
        ("write", "write", []),
        ("mixed", "mixed", []),
        ("bypass", "bypass", []),
        ("all", "all", []),
        ("pressure", "all", ["--reads", "80", "--writes", "40"]),
    ]
    results = [run_case(root, output, xls_runtime, ir, *case) for case in cases]
    (output / "summary.json").write_text(json.dumps(results, indent=2) + "\n")
    print(f"Results: {output}", flush=True)
    return 0 if all(result["passed"] for result in results) else 1


if __name__ == "__main__":
    sys.exit(main())
