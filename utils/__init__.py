#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""utils -- 通用工具。

    from utils import log, setup

    setup(log_dir="D:/rpa-logs")
    log("hello")
"""
from .logger import (
    debug,
    error,
    exception,
    get_log_dir,
    info,
    log,
    setup,
    warn,
    warning,
)

__all__ = ["setup", "log", "debug", "info", "warning", "warn", "error",
           "exception", "get_log_dir"]
