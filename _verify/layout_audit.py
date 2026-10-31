# -*- coding: utf-8 -*-
"""排版治理：主要页面注入超长文本 → 离屏截图 + 溢出嫌疑清单（无需显示器/串口）。

流程（排版治理两阶段共用本脚本）：
  python _verify/layout_audit.py before   # 修复前：存 _verify/evidence/layout_audit_before_*.png
  python _verify/layout_audit.py after    # 修复后：存 *_after_*.png

注入面（用户反馈「文本没有被容器限制」的典型路径）：
  状态行 set_status / RX|TX 统计 / 故障徽标 / STM32 页 CLI 路径与固件文件行 /
  设置页 CLI 路径与各只读标签 / 分组标题（QGroupBox）超长 / 表格单元格（Modbus）
  / 故障诊断页超长 !FAULT 行（超长未知键）。

程序化「溢出嫌疑」判定：可见 QLabel 未开 wordWrap、横向 sizePolicy 非 Ignored
（ElidedLabel 即 Ignored，天然豁免）、sizeHint 宽 > 实际宽+2px —— 打印清单并
计入退出码；截图另供人工逐张查看（超出像素判定的拐点，如树列、表格列）。
退出码：0=无嫌疑，1=有嫌疑/异常。
"""
from __future__ import annotations

import os
import sys

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
os.environ.setdefault('QT_QPA_FONTDIR', r'C:\Windows\Fonts')

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
QT_DIR = os.path.join(ROOT, 'qt')
if QT_DIR not in sys.path:
    sys.path.insert(0, QT_DIR)

from PySide6 import QtCore, QtWidgets  # noqa: E402

LONG_CN = ('设备打开失败: PermissionError(13, \'连到系统上的设备没有发挥作用。\', None, 31) '
           '原始信息 QuiteLongFunctionNameLikewise_servo_ctrl_linear_actuator+0x1C')
LONG_PATH = r'F:\很长很长的目录\ STM32CubeProgrammer \bin\STM32_Programmer_CLI.exe — 参数 -c port=SWD'
LONG_HEX = '!FAULT T=HardFault PC=0x08001234 LR=0x08004567 XPSR=0x01000000 CFSR=0x00008282 ' \
           'HFSR=0x40000000 BFAR=0x00000000 MMFAR=0x00000000 ' \
           'EXTRA=0xDEADBEEF LONGKEY_0123456789=0xCAFEBABE0123456789ABCDEF'
LONG_TITLE = '这是一个被塞得非常非常长的分组标题——远超常见分组栏宽度，用来验证标题是否会把容器顶宽'


def _set_text(widget, text):
    """统一注入（QLabel/QLineEdit/QGroupBox/QPushButton 都有 setText）。"""
    widget.setText(text)


def inject(win):
    """向各页面注入超长文本；返回 [(页面名, 注入点说明)]。"""
    notes = []
    # 终端页：状态行（ElidedLabel，验证不回退）+ 统计 + 故障徽标 + 分组标题
    win.set_status(LONG_CN, 'status_err')
    win.lbl_stats.setText('RX 9999999.9 KiB | TX 9999999.9 KiB | 丢弃 8888888 字节（洪泛）')
    win.btn_fault_badge.setText('⚠ HardFault ' + LONG_PATH)
    win.btn_fault_badge.show()
    notes.append(('终端', '状态行/统计/徽标'))
    # 终端页分组标题（发送区/接收区所在容器）
    for gb in win.findChildren(QtWidgets.QGroupBox):
        if gb.isVisible() and gb.title():
            _set_text(gb, LONG_TITLE)
            break
    # STM32 页：CLI 路径行 / 固件文件行 / 忙碌行 / 目标行 / 芯片信息
    st = win.stm32
    st.lbl_cli.setText('CLI: ' + LONG_PATH)
    st.lbl_probe.setText('ST-LINK: ' + LONG_CN)
    st.lbl_target.setText('目标: 已连接 ' + LONG_CN)
    st.lbl_file.setText(r'F:\固件仓库\超长工程目录\ servo_controller_v2_final_release\build'
                        '\\output\\servo_controller_v2_final_release.bin')
    st.lbl_busy.setText('烧录中（-d/-w + -v 校验）：' + LONG_PATH)
    st.info_box.setPlainText('芯片型号 : ' + LONG_CN + '\nFlash : ' + LONG_HEX)
    notes.append(('STM32', 'CLI/探针/目标/固件文件/忙碌/芯片信息'))
    # 设置页：CLI 路径框 + 只读标签
    se = win.settings_page
    if hasattr(se, 'edit_cli'):
        se.edit_cli.setText(LONG_PATH)
    for attr in ('lbl_theme_state', 'lbl_ver'):
        w = getattr(se, attr, None)
        if w is not None:
            w.setText(LONG_CN)
    notes.append(('设置', 'CLI 路径/只读标签'))
    # Modbus 页：状态标签 + 表格单元格（若有表格）
    mb = win.modbus
    for attr in ('lbl_state', 'lbl_status', 'lbl_info'):
        w = getattr(mb, attr, None)
        if w is not None:
            w.setText(LONG_CN)
    for tbl in mb.findChildren(QtWidgets.QTableWidget):
        if tbl.columnCount() and tbl.rowCount():
            tbl.setItem(0, 0, QtWidgets.QTableWidgetItem(LONG_HEX))
    notes.append(('Modbus', '状态标签/表格单元格'))
    # 故障诊断页：超长 !FAULT（超长未知键 → 树/摘要/列表 tooltip）
    win.fault.record_line(LONG_HEX)
    notes.append(('故障诊断', '超长原始行/摘要/未知键树节点'))
    return notes


def audit_widget(root: QtWidgets.QWidget, page: str, issues: list):
    """可见 QLabel 溢出嫌疑清单（ElidedLabel=Ignored 豁免；wordWrap 豁免）。"""
    for lbl in root.findChildren(QtWidgets.QLabel):
        if not lbl.isVisible():
            continue
        if lbl.wordWrap():
            continue
        sp = lbl.sizePolicy().horizontalPolicy()
        if sp == QtWidgets.QSizePolicy.Policy.Ignored:
            continue
        hint, actual = lbl.sizeHint().width(), lbl.width()
        if hint > actual + 2:
            issues.append(f'[{page}] QLabel「{lbl.objectName() or lbl.text()[:18]}」'
                          f'sizeHint={hint}px > 实宽={actual}px（policy={sp}）')


def main(phase: str) -> int:
    from ui.main_window import MainWindow
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication(sys.argv)
    win = MainWindow()
    win.show()
    win.resize(1000, 680)
    win._port_watch_timer.stop()
    try:
        inject(win)
        for _ in range(8):
            app.processEvents()
        evidence = os.path.join(ROOT, '_verify', 'evidence')
        os.makedirs(evidence, exist_ok=True)
        issues: list[str] = []
        pages = [win.tabs.widget(i) for i in range(win.tabs.count())]
        names = [win.tabs.tabText(i) for i in range(win.tabs.count())]
        # 逐页截图（页签切换让目标页真正布局）
        for page, name in zip(pages, names):
            win.tabs.setCurrentWidget(page)
            for _ in range(6):
                app.processEvents()
            pix = win.grab()
            path = os.path.join(evidence, f'layout_audit_{phase}_{name}.png')
            pix.save(path)
            audit_widget(page, name, issues)
        print(f'--- phase={phase} 页面截图 x{len(names)} → {evidence}')
        if issues:
            print(f'--- 溢出嫌疑 {len(issues)} 处:')
            for it in issues:
                print('   ', it)
        else:
            print('--- 溢出嫌疑: 0')
        return 1 if issues else 0
    finally:
        win._port_watch_timer.stop()
        win.close()


if __name__ == '__main__':
    phase = sys.argv[1] if len(sys.argv) > 1 else 'before'
    if phase not in ('before', 'after'):
        print('用法: python _verify/layout_audit.py [before|after]')
        raise SystemExit(2)
    raise SystemExit(main(phase))
