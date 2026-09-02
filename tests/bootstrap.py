"""加载子模块而不执行NoneBot插件注册入口。"""

from pathlib import Path
import sys
from types import ModuleType

import nonebot

try:
    nonebot.get_driver()
except ValueError:
    nonebot.init(driver="~none")

PACKAGE_NAME = "nonebot_plugin_llmchat"
if PACKAGE_NAME not in sys.modules:
    package = ModuleType(PACKAGE_NAME)
    package.__path__ = [str(Path(__file__).parents[1] / PACKAGE_NAME)]
    sys.modules[PACKAGE_NAME] = package
