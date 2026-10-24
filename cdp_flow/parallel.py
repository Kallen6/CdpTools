#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""parallel.py -- 给任务函数挂 @task，按端口一进程执行。

任务脚本（放业务目录里）：

    from cdp_flow.parallel import task

    @task                       # 不写 port 就按注册顺序分配：9222、9223...
    def demo(port: int) -> None:  # 只接收一个 port
        ...

    if __name__ == "__main__":
        demo(demo.port)          # 单独运行也照样能用

入口脚本（与任务脚本同目录时，自动扫描该目录）：

    import sys
    from cdp_flow.parallel import main

    if __name__ == "__main__":
        sys.exit(main())

执行：

    python examples/run_parallel.py              # 全部任务，各自一个进程
    python examples/run_parallel.py --list       # 任务 + 端口
    python examples/run_parallel.py --only demo  # 只跑一个
    python examples/run_parallel.py --dry-run    # 只打印计划
"""
from __future__ import annotations

import importlib
import multiprocessing as mp
import sys
import traceback
from pathlib import Path

HERE = Path(__file__).resolve().parent
BASE_PORT = 9222
SKIP = {"parallel.py", "run_parallel.py", "__init__.py", "_common.py"}

# name -> (module, function, port)
TASKS: dict[str, tuple[str, str, int]] = {}


def task(func=None, *, name: str | None = None, port: int | None = None):
    """注册任务入口；函数必须只接收一个 port。"""
    def register(fn):
        key = name or fn.__name__
        assigned = int(port) if port is not None else _next_port()
        TASKS[key] = (fn.__module__, fn.__name__, assigned)
        fn.port = assigned              # 方便单独运行：demo(demo.port)
        return fn
    return register(func) if func is not None else register


def _next_port() -> int:
    used = [p for _, _, p in TASKS.values()]
    return max(used) + 1 if used else BASE_PORT


def discover(directory: str | Path | None = None) -> list[tuple[str, tuple[str, str, int]]]:
    """import 目录下所有任务脚本，触发 @task 注册。"""
    target = Path(directory or HERE).resolve()
    if str(target) not in sys.path:
        sys.path.insert(0, str(target))
    for path in sorted(target.glob("*.py")):
        if path.name in SKIP or path.name.startswith("_"):
            continue
        try:
            importlib.import_module(path.stem)
        except Exception as exc:
            print(f"[跳过] {path.name}: {type(exc).__name__}: {exc}", flush=True)
    return sorted(TASKS.items())


def _worker(module: str, func: str, port: int, name: str) -> None:
    """子进程入口：按「模块名 + 函数名」重新导入，避免 pickle 函数对象。"""
    try:
        getattr(importlib.import_module(module), func)(port)
    except BaseException:
        print(f"[{name}] 失败：\n{traceback.format_exc()}", flush=True)
        raise


def run(tasks) -> int:
    ports = [port for _, (_, _, port) in tasks]
    duplicated = sorted({p for p in ports if ports.count(p) > 1})
    if duplicated:
        print(f"端口重复：{duplicated}")
        return 2

    ctx = mp.get_context("spawn")
    procs = []
    for name, (module, func, port) in tasks:
        proc = ctx.Process(target=_worker, args=(module, func, port, name), name=name)
        proc.start()
        print(f"[启动] {name}  port={port}  pid={proc.pid}", flush=True)
        procs.append((name, proc))

    failed = []
    for name, proc in procs:
        proc.join()
        print(f"[结束] {name}  exit={proc.exitcode}", flush=True)
        if proc.exitcode != 0:
            failed.append(name)

    if failed:
        print("失败：" + ", ".join(failed))
        return 1
    return 0


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    directory = Path(sys.argv[0]).resolve().parent
    tasks = discover(directory)

    if not tasks:
        print(f"没有任务：在 {directory} 下用 @task 注册")
        return 2

    if "--only" in args:
        wanted = set(args[args.index("--only") + 1:])
        tasks = [t for t in tasks if t[0] in wanted]
        if not tasks:
            print("没有匹配的任务")
            return 2

    if "--list" in args or "--dry-run" in args:
        for name, (module, func, port) in tasks:
            print(f"{name:<10} {module}.{func}  port={port}")
        return 0

    return run(tasks)


if __name__ == "__main__":
    sys.exit(main())
