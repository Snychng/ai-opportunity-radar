"""让源码和发行包中的兼容脚本加载同一研究引擎。"""
from pathlib import Path
import sys

SOURCE_ROOT = str(Path(__file__).resolve().parents[1] / "src")
if SOURCE_ROOT not in sys.path:
    sys.path.insert(0, SOURCE_ROOT)
