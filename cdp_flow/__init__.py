#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""cdp_flow -- 用元素库编排浏览器流程。

    from cdp_flow import Browser

    b = Browser.launch(port=9222, library="elements.json")
    tab = b.new_tab("https://example.com/")
    tab.group("main").element("btn_query").click()
"""
from .browser_manager import Browser, ElementGroup, Tab, TabNotFound
from .element_operator import (
    CdpSession,
    Element,
    ElementLibrary,
    ElementNotFound,
    ElementOperator,
    ElementOperatorError,
    ElementNotInteractable,
    OperationTimeout,
    exec_action,
)
from .parallel import task

__all__ = [
    "Browser",
    "Tab",
    "ElementGroup",
    "TabNotFound",
    "Element",
    "ElementLibrary",
    "ElementOperator",
    "ElementOperatorError",
    "ElementNotFound",
    "ElementNotInteractable",
    "OperationTimeout",
    "CdpSession",
    "exec_action",
    "task",
]
