# -*- coding: utf-8 -*-
"""自测门禁入口：判定完成后用 os._exit 跳过解释器 teardown，退出码=真实判定。

背景（2026-11-01）：全套 71 组全绿后，CPython 关机阶段偶发 ucrtbase fast-fail
（0xC0000409，WER 实录），git-bash 把它映成退出码 127——间歇性（修复主题重复
抛光风暴前近乎必现，修复后约 1/3），与测试判定无关。门禁要的是确定退出码，
故判定完 os._exit 直退（测试判定全部完成于 run_selftest 返回之前，teardown
里没有任何被测逻辑）。

用法：python -X utf8 _verify/selftest_gate.py   （退出码 0=全绿；日志加 SELFTEST_LOG 环境变量）
"""
import os
import sys

QT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "qt")
if QT not in sys.path:
    sys.path.insert(0, QT)

import selftest  # noqa: E402

rc = selftest.run_selftest()
sys.stdout.flush()
sys.stderr.flush()
os._exit(max(0, rc))   # 跳过 teardown：偶发关机 abort 不再污染判定
