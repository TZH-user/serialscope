# -*- coding: utf-8 -*-
"""退出码竞态探针：反复 建+关 MainWindow，观察进程退出码是否被污染（127）。

用途：127 出现在 QT_SPIKE_SELFTEST_OK 打印之后的解释器 shutdown 阶段，
无 traceback。本脚本在基线/改动后代码上跑同一负载，对比 127 出现率。
用法：python _verify/shutdown_exit_probe.py   （单进程一次负载，退出码即结果）
"""
import os
import sys

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
os.environ.setdefault('QT_QPA_FONTDIR', r'C:\Windows\Fonts')

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
QT_DIR = os.path.join(ROOT, 'qt')
if QT_DIR not in sys.path:
    sys.path.insert(0, QT_DIR)

from PySide6 import QtWidgets  # noqa: E402

app = QtWidgets.QApplication.instance() or QtWidgets.QApplication(sys.argv)
from ui.main_window import MainWindow  # noqa: E402

for i in range(8):
    win = MainWindow()
    win.show()
    win._port_watch_timer.stop()
    win.combo_rx_format.setCurrentText('ASCII')
    win.ingest_bytes(b'probe line\n')
    win.close()
    app.processEvents()
print('PROBE DONE 8 windows')
