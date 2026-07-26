"""最低 Python 版本闸。

⚠️ **必须在任何其它导入之前跑，且本文件自己不能用任何新语法** ——
它存在的意义就是在旧版本上给出一句人话，而不是让用户撞进
某个模块深处的 SyntaxError 去猜哪里不对。

实际下限由用到的特性决定（2026-07-26 全量扫描）：
- `zoneinfo`（3.9+）：美东日期换算，硬依赖
- `str.removesuffix`（3.9+）
- `X | Y` 注解：全部文件都有 `from __future__ import annotations`，
  所以它**不构成** 3.10 的要求。没有用到任何 3.10+ 独有特性。
"""
import sys

MIN = (3, 9)

if sys.version_info < MIN:
    raise SystemExit(
        "Vibe-Flow 需要 Python %d.%d 或更高，当前是 %d.%d。\n"
        "原因：美东时区换算依赖标准库 zoneinfo（3.9 引入）。\n"
        "装个新版 Python 再跑，或用 pyenv/conda 建一个 3.9+ 的环境。"
        % (MIN[0], MIN[1], sys.version_info[0], sys.version_info[1]))
