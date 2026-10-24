"""多进程入口：扫描本目录的 @task，按端口各起一个进程。

    python examples/run_parallel.py
    python examples/run_parallel.py --list
    python examples/run_parallel.py --only demo_local
    python examples/run_parallel.py --dry-run
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from cdp_flow.parallel import main

if __name__ == "__main__":
    sys.exit(main())
