# -*- coding: utf-8 -*-
"""进程级关窗验证：洪泛下 close() 必须「窗口隐藏、收尾完成、进程真退出」。

背景：用户报「X 灰点不动」= 幽灵窗（GUI 线程 5s 泵不到原生消息）；两段式关窗 +
RX 洪泛合并落地后，需要进程级证据而非仅自测组内的窗口级断言。本脚本模拟 ESP32
@1.5M 洪泛（~700KB/s，远超消化速率，逼出积压+丢弃），2 秒后 close()，断言：

  1. close() 瞬时返回（<300ms）且窗口立刻不可见；
  2. 洪泛期间 GUI 线程单拍最大间隔 <1.5s（16KB/拍预算 ≈ 数百 ms）；
  3. app.exec() 自然返回 = _finalize_close 收尾后真的 quit（父进程等 exit code）。

用法：python _verify/close_probe.py   （退出码 0 = 全部断言过；QT_QPA_PLATFORM 自动 offscreen）
2026-11-01 实测：close_ms=72，max_gui_gap_ms=40，PROBE-PASS rc=0（接管工作流后归档）。
"""
import os
import sys
import time

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

QT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "qt")
if QT not in sys.path:
    sys.path.insert(0, QT)

from PySide6 import QtCore, QtWidgets  # noqa: E402

import ui.main_window as mw  # noqa: E402


def main():
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication(sys.argv)
    win = mw.MainWindow()
    win.show()
    win._port_watch_timer.stop()

    stats = {"ticks": 0, "max_gap_ms": 0.0, "bytes": 0}
    last = time.perf_counter()

    flood = (b"ERR-123456 The quick brown fox jumps over the lazy dog 0123456789 "
             b"ABCDEFGHIJKLMNOPQRSTUVWXYZ\n") * 64  # ~7KB/拍

    timer = QtCore.QTimer()
    timer.setInterval(10)  # ~700KB/s

    def flood_tick():
        nonlocal last
        now = time.perf_counter()
        stats["max_gap_ms"] = max(stats["max_gap_ms"], (now - last) * 1000)
        last = now
        win.ingest_bytes(flood)
        stats["ticks"] += 1
        stats["bytes"] += len(flood)

    timer.timeout.connect(flood_tick)
    timer.start()

    def close_now():
        timer.stop()
        t0 = time.perf_counter()
        win.close()
        close_ms = (time.perf_counter() - t0) * 1000
        assert not win.isVisible(), "close 后窗口必须立刻不可见"
        assert close_ms < 300, f"close() 须瞬时返回，实 {close_ms:.0f}ms"
        assert stats["max_gap_ms"] < 1500, \
            f"洪泛期间 GUI 线程单拍间隔过大（幽灵化风险）：{stats['max_gap_ms']:.0f}ms"
        print(f"CLOSE-OK close_ms={close_ms:.0f} flood_ticks={stats['ticks']} "
              f"bytes={stats['bytes']} max_gui_gap_ms={stats['max_gap_ms']:.0f}", flush=True)

    QtCore.QTimer.singleShot(2000, close_now)
    app.aboutToQuit.connect(lambda: print("ABOUT-TO-QUIT", flush=True))

    rc = app.exec()
    print(f"PROBE-PASS rc={rc}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
