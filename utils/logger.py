#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""logger.py -- 像 print 一样用的日志，目录可指定。

    from utils.logger import log, setup

    setup(log_dir="D:/rpa-logs")     # 可选：不调用就默认写到项目根 logs/
    log("普通信息")                   # 等价 info()
    warning("注意")
    error("出错了")

约定：
  * 默认目录：项目根/logs；也可用环境变量 CDP_LOG_DIR 指定
  * 文件名：cdp-<pid>.log（多进程各写各的，不会互相抢文件）
  * 按 5MB 轮转，保留 3 个备份
  * 第一次写日志时自动初始化，不调用 setup() 也能直接用
"""
from __future__ import annotations

import logging
import os
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

DEFAULT_DIR = Path(__file__).resolve().parents[1] / "logs"
DEFAULT_FORMAT = "%(asctime)s [%(levelname)s] %(message)s"
DATE_FORMAT = "%Y-%m-%d %H:%M:%S"

# cdp_flow 内部用的 logger 名（setup 时一并接上同一个 handler）
LIB_LOGGERS = ("element_operator", "element_operator.browser")

_logger = logging.getLogger("cdp_tool")
_logger.setLevel(logging.INFO)
_logger.propagate = False

_configured = False
_current_dir: Path = DEFAULT_DIR


def _format(msg, args) -> str:
    """兼容 print 的写法，也兼容 %s 占位符。"""
    if not args:
        return str(msg)
    try:
        return str(msg) % args
    except (TypeError, ValueError):
        return " ".join(str(x) for x in (msg,) + args)


def setup(log_dir: str | os.PathLike | None = None,
          level: str | int | None = None,
          console: bool = True,
          file_name: str | None = None,
          max_bytes: int = 5 * 1024 * 1024,
          backup_count: int = 3) -> Path:
    """配置日志输出，返回日志目录。

    Args:
        log_dir: 日志目录；不传则用环境变量 CDP_LOG_DIR，再退到项目根 logs/
        level: DEBUG / INFO / WARNING / ERROR（默认 INFO，可用 CDP_LOG_LEVEL）
        console: 是否同时输出到控制台
        file_name: 日志文件名；默认 cdp-<pid>.log
        max_bytes: 单个文件大小上限
        backup_count: 保留几个轮转备份
    """
    global _configured, _current_dir

    target = Path(log_dir or os.environ.get("CDP_LOG_DIR") or DEFAULT_DIR)
    target = target.expanduser().resolve()
    target.mkdir(parents=True, exist_ok=True)
    _current_dir = target

    # 重复调用时先清掉旧 handler，避免同一条日志写两遍
    for handler in list(_logger.handlers):
        _logger.removeHandler(handler)
        try:
            handler.close()
        except Exception:
            pass

    level_value = level or os.environ.get("CDP_LOG_LEVEL") or "INFO"
    if isinstance(level_value, str):
        level_value = getattr(logging, level_value.upper(), logging.INFO)
    _logger.setLevel(level_value)

    formatter = logging.Formatter(DEFAULT_FORMAT, DATE_FORMAT)

    if console and sys.stdout is not None:
        stream = logging.StreamHandler(sys.stdout)
        stream.setFormatter(formatter)
        _logger.addHandler(stream)

    log_file = target / (file_name or f"cdp-{os.getpid()}.log")
    file_handler = RotatingFileHandler(
        log_file, maxBytes=max_bytes, backupCount=backup_count, encoding="utf-8")
    file_handler.setFormatter(formatter)
    _logger.addHandler(file_handler)

    # cdp_flow 内部 logger 也接到同一份输出上
    for name in LIB_LOGGERS:
        lib_logger = logging.getLogger(name)
        lib_logger.setLevel(level_value)
        lib_logger.propagate = False
        for handler in list(lib_logger.handlers):
            lib_logger.removeHandler(handler)
        for handler in _logger.handlers:
            lib_logger.addHandler(handler)

    _configured = True
    return target


def _ensure() -> None:
    if not _configured:
        setup()


def get_log_dir() -> Path:
    """当前日志目录。"""
    _ensure()
    return _current_dir


def log(msg="", *args) -> None:
    """像 print 一样：默认 INFO 级别。"""
    _ensure()
    _logger.info(_format(msg, args))


def debug(msg="", *args) -> None:
    _ensure()
    _logger.debug(_format(msg, args))


def info(msg="", *args) -> None:
    _ensure()
    _logger.info(_format(msg, args))


def warning(msg="", *args) -> None:
    _ensure()
    _logger.warning(_format(msg, args))


warn = warning


def error(msg="", *args) -> None:
    _ensure()
    _logger.error(_format(msg, args))


def exception(msg="", *args) -> None:
    """ERROR 级别，并自动带上当前异常堆栈（要在 except 块里调用）。"""
    _ensure()
    _logger.exception(_format(msg, args))


__all__ = ["setup", "log", "debug", "info", "warning", "warn", "error",
           "exception", "get_log_dir"]
