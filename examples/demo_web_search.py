"""真实网站示例（需要网络）：打开 B 站首页并提交一次搜索。

元素库只依赖 class，不依赖登录态；页面结构变化时用捕获器重新拾取即可。
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
LIBRARY = HERE / "bilibili_lib.json"


@task(port=9223)
def demo_web(port: int = 9223) -> None:
    setup()                                     # 默认写到项目根 logs/
    browser = Browser.launch(port=port, library=str(LIBRARY), headless=False)
    try:
        tab = browser.new_tab("https://www.bilibili.com/")
        group = tab.group("bili")
        group.element("search_input").input_text("CDP")
        # 按钮是 svg 容器，用坐标点击更接近真实用户操作
        group.element("btn_search").click(mouse=True)
        log("[demo_web] 已提交搜索")
    finally:
        browser.close(kill=True)


if __name__ == "__main__":
    demo_web(demo_web.port)
