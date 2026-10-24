"""本地页面示例：完全离线。

流程：打开 demo_page.html → 输入关键词 → 点击搜索 → 读取结果文本。
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from cdp_flow import Browser
from cdp_flow.parallel import task
from utils import log, setup

HERE = Path(__file__).resolve().parent
LIBRARY = HERE / "demo_lib.json"
PAGE = (HERE / "demo_page.html").as_uri()


@task(port=9222)
def demo_local(port: int = 9222) -> None:
    setup()                                     # 默认写到项目根 logs/
    browser = Browser.launch(port=port, library=str(LIBRARY), headless=True)
    try:
        tab = browser.new_tab(PAGE)
        group = tab.group("demo")
        group.element("search_input").input_text("hello cdp")
        group.element("btn_search").click()
        log(f"[demo_local] {group.element('result_text').text()}")
    finally:
        browser.close(kill=True)


if __name__ == "__main__":
    demo_local(demo_local.port)
