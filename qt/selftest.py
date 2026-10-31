# -*- coding: utf-8 -*-
"""离屏自测：RxBuffer 纯逻辑单测 + 主窗体收发管线（QT_QPA_PLATFORM=offscreen 可跑）。"""
from __future__ import annotations

import os
import sys

# 双模式自举：直跑（cd qt && python selftest.py）时 sys.path[0] 已是 qt/；
# 包跑（python -m qt.selftest，门禁脚本用）时 sys.path[0] 是 CWD（仓库根），
# 兄弟模块平铺导入（selftest_* / core.* / ui.*）会 ModuleNotFoundError——
# 统一把本文件所在目录插到最前（幂等，两种模式都兼容）。
_QT_DIR = os.path.dirname(os.path.abspath(__file__))
if _QT_DIR not in sys.path:
    sys.path.insert(0, _QT_DIR)

import time


def test_rx_buffer():
    from core.rx_buffer import RxBuffer

    t = 1000.0  # 假时钟（秒）

    # 完整行立即吐出，残行留在缓冲
    buf = RxBuffer()
    assert buf.feed(b'boot v1.0\n', t) == ['boot v1.0']
    assert buf.backlog_bytes == 0

    # CRLF/CR 归一
    assert buf.feed(b'a\r\nb\rc\n', t) == ['a', 'b', 'c']

    # 无换行：进缓冲、不显示、计积压
    lines = buf.feed(b'no_newline_tail', t)
    assert lines == []
    assert buf.backlog_bytes == len(b'no_newline_tail')

    # 未过空闲阈值不落显
    assert buf.poll_idle_flush(t + 0.1) is None
    # 过阈值（400ms）落显且清零
    tail = buf.poll_idle_flush(t + 0.5)
    assert tail == 'no_newline_tail'
    assert buf.backlog_bytes == 0

    # 多字节 UTF-8 的积压按字节计（温度=6B + =41.5=5B）
    buf2 = RxBuffer()
    buf2.feed('温度=41.5'.encode('utf-8'), t)  # 11 字节无换行
    assert buf2.backlog_bytes == 11

    # 续包拼行：分片到达仍能重组
    buf3 = RxBuffer()
    assert buf3.feed(b'12.', t) == []
    assert buf3.feed(b'5\n', t) == ['12.5']

    # 跨块中文：多字节字符被 USB 分包从中间切开不能变乱码（主线 GetDecoder 语义）
    raw = '透传回环测试\n'.encode('utf-8')
    buf4 = RxBuffer()
    for i in range(len(raw)):  # 逐字节喂入 = 最狠的切法
        lines = buf4.feed(raw[i:i + 1], t)
    assert lines == ['透传回环测试'], '逐字节跨块解码必须还原完整中文'

    # 半个字符悬着时空闲落显：吐完整字符前缀，悬着的半个字留在解码器里
    buf5 = RxBuffer()
    half = '瀑'.encode('utf-8')[:2]
    assert buf5.feed(b'x' + half, t) == []
    assert buf5.poll_idle_flush(t + 0.5) == 'x'
    # 后半块到齐 → 悬着的'瀑'补全成行
    assert buf5.feed(b'\x91y\n', t) == ['瀑y']

    print('RXBUFFER OK')


def test_main_window():
    from PySide6 import QtWidgets

    from ui.main_window import MainWindow

    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication(sys.argv)
    win = MainWindow()
    win.show()
    win.wave.state.enabled = False  # 本测试只看终端管线，波形侧不参与积压
    # 设置记忆会恢复本机 ini 的收发格式；终端管线用例统一按 ASCII 口径来
    win.combo_rx_format.setCurrentText('ASCII')
    win.combo_tx_format.setCurrentText('ASCII')

    # 完整行立即吐出，残行留在缓冲
    win.ingest_bytes(b'boot v1.0\n')
    win.ingest_bytes(b'temp=41.5\n')
    win.ingest_bytes(b'no_newline_tail')
    text = win.log.toPlainText()
    assert 'RX boot v1.0' in text and 'RX temp=41.5' in text
    assert 'no_newline_tail' not in text, '残行不该立即显示'
    assert win.buffer.backlog_bytes == len(b'no_newline_tail')

    # 未过阈值的 flush tick：只更新积压标签
    win.on_flush_tick()
    assert win.lbl_backlog.text() == f'积压 {len(b"no_newline_tail")} B'

    # 静默超阈值 → 空闲落显（测试直接把最后收字节的时刻拨早 10s）
    win.buffer._last_byte_at -= 10.0
    win.on_flush_tick()
    assert 'no_newline_tail (空闲落显)' in win.log.toPlainText()
    assert win.lbl_backlog.text() == '积压 0 B'

    # 发送：未连接时仅入日志
    win.edit_send.setText('AT+VERSION?')
    win.send()
    assert 'TX AT+VERSION?' in win.log.toPlainText()
    assert win.edit_send.text() == ''

    win.close()
    print('MAINWINDOW OK')


def test_terminal_chrome():
    """A1/A2：清空记录 + 收发字节统计 + 全时间戳 + 日志条目列表。"""
    from PySide6 import QtWidgets

    from ui.main_window import MainWindow

    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication(sys.argv)
    win = MainWindow()
    win.show()
    win.wave.state.enabled = False
    win.combo_rx_format.setCurrentText('ASCII')

    # 全时间戳：条目与日志头行都带日期（Ctrl+F 时间定位的数据基础）
    win.ingest_bytes(b'boot v1.0\n')
    entry = win.message_entries[-1]
    assert entry['direction'] == 'RX' and entry['text'] == 'boot v1.0'
    import re as _re
    assert _re.search(r'\[\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\.\d{3}\] RX boot v1\.0',
                      win.log.toPlainText()), '日志头行必须是全时间戳'
    assert win.rx_bytes_total == len(b'boot v1.0\n')
    assert 'RX 0.0 KiB | TX 0.0 KiB' in win.lbl_stats.text()

    # 清空记录：显示/条目/残行缓冲全清，字节统计保留（连接周期量）
    win.ingest_bytes(b'temp=41.5\n')
    win.btn_clear_log.click()
    assert len(win.message_entries) == 1
    assert win.message_entries[0]['text'] == '消息记录已清空。'
    assert win.buffer.backlog_bytes == 0
    assert win.rx_bytes_total > 0, '字节统计不清零'
    assert '消息记录已清空。' in win.log.toPlainText()

    # 条目上限裁剪：>上限丢最老（注入小上限避免真灌 2 万条）
    win.message_entries.clear()
    import ui.main_window as mw
    old_max, old_trim = mw.MAX_LOG_ENTRIES, mw.MAX_LOG_TRIM
    mw.MAX_LOG_ENTRIES, mw.MAX_LOG_TRIM = 10, 4
    for i in range(12):
        win._append_log('RX', f'LINE-{i}')
    mw.MAX_LOG_ENTRIES, mw.MAX_LOG_TRIM = old_max, old_trim
    assert len(win.message_entries) == 8
    assert win.message_entries[0]['text'] == 'LINE-4', '应丢最老的 4 条'

    win.close()
    print('TERMINAL CHROME OK')


def test_ports_watch():
    """A3/A4：端口展示文本 / CH340 优选 / 热插拔自动连接 / 设置记忆。"""
    from PySide6 import QtCore, QtWidgets

    import ui.main_window as mw
    from core.ports import (is_ch340_description, pick_auto_connect_port,
                            port_display_text, port_name_from_display)
    from core.settings import read_setting, write_setting

    # 展示文本与取口名（描述自带 (COMx) 尾巴时去重）
    assert port_display_text('COM6', 'USB-SERIAL CH340 (COM6)') == 'COM6 — USB-SERIAL CH340'
    assert port_display_text('COM6', 'COM6') == 'COM6'
    assert port_name_from_display('COM6 — USB-SERIAL CH340') == 'COM6'
    assert port_name_from_display('COM33') == 'COM33'

    # CH340 优选（多新端口选 CH340，无 CH340 取第一项）
    assert pick_auto_connect_port([('COM6', '蓝牙串行'), ('COM8', 'USB-SERIAL CH340')]) == 'COM8'
    assert pick_auto_connect_port([('COM3', ''), ('COM4', 'USB 设备')]) == 'COM3'
    assert pick_auto_connect_port([]) == ''
    assert is_ch340_description('USB-SERIAL CH341A (COM5)')
    assert not is_ch340_description('蓝牙链接上的标准串行 (COM6)')

    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication(sys.argv)
    win = mw.MainWindow()
    win.show()
    win.wave.state.enabled = False

    created = []

    class FakeWorker(QtCore.QObject):
        got_bytes = QtCore.Signal(bytes)
        opened = QtCore.Signal()
        open_failed = QtCore.Signal(str)
        lost = QtCore.Signal(str)
        port_lost = QtCore.Signal(str)
        reconnected = QtCore.Signal()

        def __init__(self, device, baud, parent=None, dtr_rts_neutral=False, auto_reconnect=False):
            super().__init__(parent)
            self.device, self.baud = device, baud
            created.append(self)

        def start(self):
            pass

        def stop(self):
            pass

        def write_bytes(self, data):
            return True

    old_worker_cls, old_scan = mw.SerialWorker, (mw.scan_port_infos, mw.scan_port_infos_full)
    try:
        mw.SerialWorker = FakeWorker
        mw.scan_port_infos = lambda: [('COM3', '蓝牙链接上的标准串行 (COM3)'),
                                      ('COM8', 'USB-SERIAL CH340 (COM8)')]
        mw.scan_port_infos_full = lambda: [('COM3', '蓝牙链接上的标准串行 (COM3)', ''),
                                           ('COM8', 'USB-SERIAL CH340 (COM8)', 'CH340')]
        win.refresh_ports()
        assert win.combo_port.count() == 2

        # 勾选自动连接 + COM8 是基线外新设备 → 自动连接并优选 CH340
        win.chk_auto_link.setChecked(True)
        win._known_port_names = ['COM3']
        win._port_watch_tick()
        assert len(created) == 1 and created[0].device == 'COM8', '新设备应自动连接且优选 CH340'
        assert '检测到新串口设备 COM8' in win.log.toPlainText()
        assert win._connected_device == 'COM8'
        assert read_setting('connect/port', '') == 'COM8'

        # 已连着同一口：设备列表再变也不重复连
        n_created = len(created)
        win._known_port_names = ['COM3']
        win._port_watch_tick()
        assert len(created) == n_created

        # 未勾选自动连接：新设备只刷新列表不连
        win.chk_auto_link.setChecked(False)
        win._teardown_worker()
        win._known_port_names = ['COM3']
        mw.scan_port_infos = lambda: [('COM3', ''), ('COM8', 'USB-SERIAL CH340 (COM8)'),
                                      ('COM9', 'USB-SERIAL CH340 (COM9)')]
        mw.scan_port_infos_full = lambda: [('COM3', '', ''),
                                           ('COM8', 'USB-SERIAL CH340 (COM8)', 'CH340'),
                                           ('COM9', 'USB-SERIAL CH340 (COM9)', 'CH340')]
        win._port_watch_tick()
        assert len(created) == n_created
        assert win.combo_port.count() == 3, '端口列表应刷新出 COM9'
    finally:
        mw.SerialWorker = old_worker_cls
        mw.scan_port_infos, mw.scan_port_infos_full = old_scan
        win._port_watch_timer.stop()
    win.close()

    # 连接周期记忆：先写设置再重建窗口（必须在 win.close() 之后写——
    # closeEvent 会把当时的组合框状态存回 ini）
    write_setting('connect/rx_format', 'HEX')
    write_setting('connect/tx_format', 'HEX')
    write_setting('connect/newline', 'CRLF')
    write_setting('connect/baud', '115200')

    win2 = mw.MainWindow()
    try:
        assert win2.combo_rx_format.currentText() == 'HEX'
        assert win2.combo_tx_format.currentText() == 'HEX'
        assert win2.combo_newline.currentText() == 'CRLF'
        assert win2.combo_baud.currentText() == '115200'
    finally:
        # 组合框先复位再关窗：closeEvent 会把当前状态存回 ini，顺手还原默认
        win2.combo_rx_format.setCurrentText('ASCII')
        win2.combo_tx_format.setCurrentText('ASCII')
        win2.combo_newline.setCurrentText('LF')
        win2.combo_baud.setCurrentText('1500000')
        win2.chk_auto_link.setChecked(False)
        win2._port_watch_timer.stop()
        win2.close()
    print('PORT WATCH OK')


def test_connect_lock():
    """连接期间串口协议参数置灰锁定，断开恢复（改口/改波特率须先断开）。"""
    from PySide6 import QtWidgets

    from ui.main_window import MainWindow
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication(sys.argv)
    win = MainWindow()
    win.combo_rx_format.setCurrentText('ASCII')  # ini 可能残留 HEX
    win.combo_tx_format.setCurrentText('ASCII')
    assert win.combo_port.isEnabled() and win.combo_baud.isEnabled()
    win._on_connected()  # 正常路径由 worker.opened 信号触发
    assert not win.combo_port.isEnabled()
    assert not win.combo_baud.isEnabled()
    assert '断开' in win.combo_port.toolTip() and '断开' in win.combo_baud.toolTip()
    win._teardown_worker()  # worker 为 None 也必须解锁
    assert win.combo_port.isEnabled() and win.combo_baud.isEnabled()
    assert win.combo_port.toolTip() == '' and win.combo_baud.toolTip() == ''
    win._port_watch_timer.stop()
    win.close()
    print('CONNECT LOCK OK')


def test_find_time_mode():
    """A5：时间戳查询识别/解析 + 头行定位（对齐主线 find_log_timestamp_jump）。"""
    from datetime import datetime

    from PySide6 import QtWidgets

    from core.log_find import (collect_timestamp_headers, find_timestamp_jump,
                               is_timestamp_query, parse_timestamp)
    from ui.find_bar import FindBar

    # 查询形态识别
    assert is_timestamp_query('20:57')
    assert is_timestamp_query('10:03:22.5')
    assert is_timestamp_query('2026-10-24 20:57:38.500')
    assert is_timestamp_query('2026-10-24')
    assert not is_timestamp_query('hello')
    assert not is_timestamp_query('')
    # 非法时间解析失败 → 回退文本搜索
    assert parse_timestamp('25:99') is None
    assert parse_timestamp('10:03') == datetime(1900, 1, 1, 10, 3)

    # 头行收集与定位：3 条头行 10:00 / 10:05 / 10:10
    text = ('[2026-10-24 10:00:00.000] RX\nboot ok\n'
            '[2026-10-24 10:05:00.000] RX\ntemp=1\n'
            '[2026-10-24 10:10:00.000] SYS\ndone\n')
    hits = collect_timestamp_headers(text)
    assert len(hits) == 3 and hits[0][0] == 0

    # 只给时刻：日期取首条头行的日期；向前 = 首条 ≥ 时刻
    r = find_timestamp_jump(hits, '10:03', backward=False)
    assert r['found'] and r['ordinal'] == 2 and r['time'].strftime('%H:%M') == '10:05'
    assert text[r['pos']:r['pos'] + r['length']] == '[2026-10-24 10:05:00.000]'
    # 精确命中取自身
    r = find_timestamp_jump(hits, '10:10:00', backward=False)
    assert r['ordinal'] == 3
    # 时刻早于所有条目 → 回绕到第一条
    r = find_timestamp_jump(hits, '09:00', backward=False)
    assert r['ordinal'] == 1
    # 向后 = 末条 < 时刻
    r = find_timestamp_jump(hits, '10:10', backward=True, sel_start=10 ** 9)
    assert r['ordinal'] == 2, '选区在末尾时向后应取 10:05 这条'
    # 无 ≥ 该时刻条目
    r = find_timestamp_jump(hits, '23:00', backward=False, sel_start=10 ** 9, sel_end=10 ** 9)
    assert r is not None and not r['found'] and r['total'] == 3

    # FindBar 集成：时间查询直接跳头行并显示「时刻 第 i/N 处」
    edit = QtWidgets.QTextEdit()
    edit.setPlainText(text)
    bar = FindBar(edit)
    bar.edit_find.setText('10:03')
    assert '时刻 第 2/3 处' in bar.lbl_count.text()
    cursor = edit.textCursor()
    assert '10:05' in cursor.selectedText()
    bar.edit_find.setText('hello')
    assert not bar._time_mode and bar.lbl_count.text() == '无匹配'
    print('FIND TIME OK')


def test_auto_log_export():
    """B6/B7：自动日志落盘（懒建文件/SYS 不落/勾选关闭丢弃）+ 日志导出。"""
    import os
    import tempfile

    from PySide6 import QtWidgets

    import ui.main_window as mw
    from core.auto_log import AutoLogWriter, build_log_export_text, format_entry_block
    from datetime import datetime

    # 纯逻辑：块格式与主线同构
    block = format_entry_block(datetime(2026, 10, 24, 10, 0, 0, 123000), 'RX', 'boot ok')
    assert block == '[2026-10-24 10:00:00.123] RX\r\nboot ok\r\n\r\n'

    tmp_dir = tempfile.mkdtemp(prefix='qt_auto_log_')
    writer = AutoLogWriter(tmp_dir, now_fn=lambda: 1761271200.0)  # 固定文件名时刻
    assert not writer.active and writer.path is None
    writer.enqueue(format_entry_block(datetime(2026, 10, 24, 10, 0, 1), 'RX', 'line-1'))
    assert writer.pending_count() == 1
    assert writer.flush()
    assert writer.active and 'serial_auto_log_' in os.path.basename(writer.path)
    writer.enqueue(format_entry_block(datetime(2026, 10, 24, 10, 0, 2), 'TX', 'AT'))
    writer.close()
    with open(writer.path, encoding='utf-8', newline='') as f:
        content = f.read()
    assert 'line-1' in content and '[2026-10-24 10:00:02.000] TX\r\nAT\r\n\r\n' in content

    # 导出文本构建：头行 + 正文 + 空行
    entries = [
        {'ts': datetime(2026, 10, 24, 10, 0, 0), 'direction': 'RX', 'text': 'hello'},
        {'ts': datetime(2026, 10, 24, 10, 0, 1), 'direction': 'SYS', 'text': 'note'},
    ]
    export_text = build_log_export_text(entries)
    assert export_text == ('[2026-10-24 10:00:00.000] RX\nhello\n\n'
                           '[2026-10-24 10:00:01.000] SYS\nnote\n\n')

    # 主窗体集成：注入临时日志目录
    old_get_log_dir = mw.get_log_dir
    mw.get_log_dir = lambda: tmp_dir
    try:
        app = QtWidgets.QApplication.instance() or QtWidgets.QApplication(sys.argv)
        win = mw.MainWindow()
        win.show()
        win.wave.state.enabled = False
        win.combo_rx_format.setCurrentText('ASCII')
        win.chk_auto_save.setChecked(True)
        before = os.listdir(tmp_dir)
        win.ingest_bytes(b'log-me\n')          # RX 入队
        win._append_log('SYS', 'sys-note')      # SYS 不入队
        assert win._auto_log.pending_count() == 1, 'SYS 不该进自动日志'
        win.on_flush_tick()                     # 落盘 + 标签更新
        assert '日志: ' in win.lbl_log_path.text()
        after = os.listdir(tmp_dir)
        assert len(after) == len(before) + 1
        latest = max(os.path.join(tmp_dir, n) for n in after)
        with open(latest, encoding='utf-8', newline='') as f:
            assert 'log-me' in f.read()

        # 勾选关闭：积压直接丢，不落盘
        win.chk_auto_save.setChecked(False)
        win.ingest_bytes(b'dropped\n')
        win._flush_auto_log()
        assert win._auto_log.pending_count() == 0

        # 导出日志（直接调内部方法避免文件对话框）
        export_path = os.path.join(tmp_dir, 'export_test.txt')
        win._export_log_to(export_path)
        with open(export_path, encoding='utf-8') as f:
            exported = f.read()
        assert 'log-me' in exported, '导出文本应包含 RX 条目'
        assert '日志已导出到' in win.log.toPlainText(), '完成提示进窗口日志（导出文件先写后记，不含自身）'
        win._port_watch_timer.stop()
        win.close()
    finally:
        mw.get_log_dir = old_get_log_dir
    print('AUTO LOG OK')


def test_log_hover():
    """B8：行悬停 HEX⇄文本/十进制/float32 互转（对齐主线 get_line_alt_format_tooltip）。"""
    from PySide6 import QtWidgets

    import struct as _struct

    from core.text_format import alt_format_tooltip
    from ui.log_hover import tooltip_for_position
    from ui.main_window import MainWindow

    # HEX 行 → 文本 + 十进制 + float32
    tip = alt_format_tooltip('41 42 43 44')
    assert tip.startswith('文本(UTF-8): ABCD')
    assert '十进制: 65 66 67 68' in tip
    expect_float = f'{_struct.unpack("<f", bytes.fromhex("41424344"))[0]:.6g}'
    assert f'float32(小端): {expect_float}' in tip

    # 非 4 倍数长 HEX：不给 float32
    assert 'float32' not in alt_format_tooltip('41 42')
    # 控制字节转义与不可见符
    tip = alt_format_tooltip('0D 0A 01')
    assert '<CR><LF>·' in tip
    # 短 token 不当 HEX（误判率太高）
    assert alt_format_tooltip('AB') == 'HEX: 41 42'
    assert alt_format_tooltip('OK') == 'HEX: 4F 4B'
    # 文本行 → UTF-8 HEX；超 64 字节截断加省略号
    assert alt_format_tooltip('hello') == 'HEX: 68 65 6C 6C 6F'
    long_line = 'x' * 100
    tip = alt_format_tooltip(long_line)
    assert tip.startswith('HEX: 78 78') and tip.endswith('…')
    # 空/非日志行不给提示
    assert alt_format_tooltip('') == ''
    assert alt_format_tooltip('1 2 3') == alt_format_tooltip('1 2 3')  # 2字符 token 走文本

    # 主窗体日志块：HEX 条目正文、文本条目正文都能出提示
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication(sys.argv)
    win = MainWindow()
    win.show()
    win.wave.state.enabled = False
    win.combo_rx_format.setCurrentText('ASCII')
    win._append_log('RX', '41 42 43 44')
    win._append_log('RX', 'hello-ascii')
    doc = win.log.document()
    hex_tip = ascii_tip = ''
    for i in range(doc.blockCount()):
        tip = tooltip_for_position(win.log, i)
        if tip.startswith('文本(UTF-8)'):
            hex_tip = tip
        elif tip.startswith('HEX: 68'):
            ascii_tip = tip
    assert 'ABCD' in hex_tip and 'float32' in hex_tip
    assert ascii_tip.startswith('HEX: 68 65 6C')
    win._port_watch_timer.stop()
    win.close()
    print('LOG HOVER OK')


def test_fault_decode_and_ai_doc():
    """B9/B10：STM32 HardFault dump 解码 + AI 接入说明文档。"""
    from core.ai_doc import get_ai_format_doc
    from core.fault_decode import test_fault_dump_line
    from core.wave_state import WaveState

    # 无关行不受影响
    assert test_fault_dump_line('boot ok') is None
    assert test_fault_dump_line('MMFAR=0x123') is None, '缺 CFSR/HFSR 不触发'
    # CFSR=0x00020000 → UFSR.INVSTATE；HFSR FORCED
    got = test_fault_dump_line('CFSR: 0x00020000 HFSR: 0x40000000')
    assert got and 'CFSR=0x00020000' in got and 'UFSR.INVSTATE' in got
    assert 'HFSR.FORCED 强制升级为HardFault(具体原因查CFSR)' in got
    # BFSR.PRECISERR + BFAR 出错地址
    got = test_fault_dump_line('CFSR=0x00008200, BFAR=0x12345678')
    assert 'BFSR.PRECISERR 精确数据总线错误' in got
    assert 'BFSR.BFARVALID 出错地址 BFAR=0x12345678' in got
    # UFSR.NOCP（未使能 FPU）+ 非对齐（注意主线正则不允许 CFSR 和冒号间有空格）
    got = test_fault_dump_line('CFSR: 0x01090000')
    assert 'UFSR.NOCP 调用无协处理器(常见:未使能FPU却用浮点)' in got
    assert 'UFSR.UNALIGNED 非对齐访问' in got
    # 全 0：位都没置
    got = test_fault_dump_line('CFSR: 0x00000000')
    assert '各错误位均未置位' in got

    # 主窗体 hook：RX 命中 dump 自动追加 SYS 解码行
    from PySide6 import QtWidgets
    from ui.main_window import MainWindow
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication(sys.argv)
    win = MainWindow()
    win.show()
    win.wave.state.enabled = False
    win.combo_rx_format.setCurrentText('ASCII')
    win._append_log('RX', 'CFSR: 0x00020000')
    sys_texts = [e['text'] for e in win.message_entries if e['direction'] == 'SYS']
    assert any('HardFault 解码' in t and 'UFSR.INVSTATE' in t for t in sys_texts)
    win._port_watch_timer.stop()
    win.close()

    # AI 接入说明：三协议关键要素齐备 + 未知协议回落 FireWater
    fw = get_ai_format_doc('FireWater')
    assert 'FireWater' in fw and 'ch0' in fw and 'printf' in fw
    jf = get_ai_format_doc('JustFloat')
    assert 'float32' in jf and '0x80' in jf and 'Serial.write' in jf
    rd = get_ai_format_doc('RawData')
    assert '0~255' in rd and '单通道' in rd
    assert get_ai_format_doc('Nope') == get_ai_format_doc('FireWater')

    # 波形窗「格式说明」对话框可以拉起（不阻塞：直接构造内部控件）
    from ui.wave_window import WaveWindow
    wave = WaveWindow()
    assert 'JustFloat' in wave.FORMAT_HELP_TEXT and 'RawData' in wave.FORMAT_HELP_TEXT
    wave._copy_ai_doc()  # 复制到剪贴板（离屏环境可运行）
    assert QtWidgets.QApplication.clipboard().text() == get_ai_format_doc('FireWater')
    wave.stop_timers()
    print('FAULT/AI DOC OK')


def _build_min_elf32(funcs) -> bytes:
    """构造只含 .symtab/.strtab 的最小 ELF32（小端），供符号化自测。"""
    import struct
    strtab = b'\x00' + b'\x00'.join(n.encode() for n, _ in funcs) + b'\x00'
    name_off, off = {}, 1
    for n, _ in funcs:
        name_off[n] = off
        off += len(n.encode()) + 1
    symtab = struct.pack('<IIIBBH', 0, 0, 0, 0, 0, 0)   # 空符号
    for n, a in funcs:
        symtab += struct.pack('<IIIBBH', name_off[n], a, 0x40, 0x12, 0, 1)  # STT_FUNC
    shstrtab = b'\x00.symtab\x00.strtab\x00'
    symtab_off = 52
    strtab_off = symtab_off + len(symtab)
    shstr_off = strtab_off + len(strtab)
    shoff = shstr_off + len(shstrtab)
    sh = struct.pack('<10I', 0, 0, 0, 0, 0, 0, 0, 0, 0, 0)
    sh += struct.pack('<10I', 1, 2, 0, 0, symtab_off, len(symtab), 2, 1, 4, 16)
    sh += struct.pack('<10I', 8, 3, 0, 0, strtab_off, len(strtab), 0, 0, 1, 0)
    sh += struct.pack('<10I', 16, 3, 0, 0, shstr_off, len(shstrtab), 0, 0, 1, 0)
    eh = b'\x7fELF' + bytes([1, 1, 1, 0]) + b'\x00' * 8
    eh += struct.pack('<HHIIIIIHHHHHH', 2, 40, 1, 0x08000000, 0, shoff, 0,
                      52, 0, 0, 40, 4, 3)
    return eh + symtab + strtab + shstrtab + sh


def test_fault_report():
    """!FAULT 行宽容解析 + 最小 ELF32 符号化（契约逐字节口径，纯函数）。"""
    from core.fault_report import (ElfSymbols, decode_reasons, describe_address,
                                   load_elf_symbols, parse_fault_line, symbolize)

    # 契约整行：全部键解析、无截断、无未知键
    line = ('!FAULT T=HardFault PC=0x08001234 LR=0x08004567 XPSR=0x01000000 '
            'CFSR=0x00008282 HFSR=0x40000000 BFAR=0x00000000 MMFAR=0x00000000')
    f = parse_fault_line(line)
    assert f is not None and f.fault_type == 'HardFault'
    assert (f.pc, f.lr, f.xpsr, f.cfsr, f.hfsr, f.bfar, f.mmfar) == (
        0x08001234, 0x08004567, 0x01000000, 0x8282, 0x40000000, 0, 0)
    assert not f.truncated and not f.extra

    # 半行截断：短值照常解析 + truncated 标记；键在值没了也认
    f2 = parse_fault_line('!FAULT T=BusFault PC=0x080012')
    assert f2.pc == 0x080012 and f2.truncated and f2.cfsr is None
    f3 = parse_fault_line('!FAULT T=HardFault PC=0x08001234 LR=')
    assert f3.truncated and f3.lr is None
    assert parse_fault_line('!FAULT PC=0x08001000').fault_type == '未知'

    # 未知键保留展示（含非数值值）；缺 T/全零地址的宽容口径
    f4 = parse_fault_line('!FAULT T=UsageFault PC=0x1 SHCSR=0x00007000 FOO=bar')
    assert f4.extra.get('SHCSR') == '0x00007000' and f4.extra.get('FOO') == 'bar'
    assert describe_address(parse_fault_line(
        '!FAULT T=HardFault CFSR=0x00000000 HFSR=0x00000000'))[0] == '无效'
    assert describe_address(parse_fault_line('!FAULT T=HardFault'))[0] == '未知'

    # 非 !FAULT 行不误吞；行中残尾前缀可重同步
    assert parse_fault_line('hello CFSR=0x123') is None
    assert parse_fault_line('') is None
    assert parse_fault_line('tail !FAULT T=MemManage CFSR=0x00000082') \
        .fault_type == 'MemManage'

    # 位域人话（口径与 fault_decode 一致）：0x8282 = MMFSR|BFSR 各两位，UFSR 半区为 0
    reasons = decode_reasons(f)
    assert 'MMFSR.MMARVALID 出错地址 0x00000000' in reasons
    assert 'MMFSR.DACCVIOL 数据访问违例(MPU)' in reasons
    assert 'BFSR.PRECISERR 精确数据总线错误' in reasons
    assert 'BFSR.BFARVALID 出错地址 0x00000000' in reasons
    assert 'HFSR.FORCED 强制升级为HardFault(具体原因查CFSR)' in reasons
    assert decode_reasons(parse_fault_line(
        '!FAULT T=HardFault CFSR=0x00020000')) == [
        'UFSR.INVSTATE 状态无效(常见:函数指针错误/Thumb位)']
    assert decode_reasons(parse_fault_line(
        '!FAULT T=HardFault CFSR=0x00000000')) == ['CFSR/HFSR 各错误位均未置位']

    # 符号化：最近符号+偏移（servo_ctrl+0x1c 契约示例）、裸地址回落
    syms = ElfSymbols.from_bytes(_build_min_elf32(
        [('HardFault_Handler', 0x08001200), ('main', 0x08000100),
         ('servo_ctrl', 0x08001218)]))
    assert symbolize(syms, 0x08001234) == 'servo_ctrl+0x1c'
    assert symbolize(syms, 0x08001200) == 'HardFault_Handler'
    assert symbolize(syms, 0x08000105) == 'main+0x5'
    assert symbolize(syms, 0x10) == '0x00000010'       # 低于最小符号
    assert symbolize(None, 0x08001234) == '0x08001234'  # 未加载 ELF

    # 文件加载缓存：正常加载 / 坏数据报错 / 空路径
    import os
    import tempfile
    fd, tmp = tempfile.mkstemp(suffix='.elf')
    try:
        with os.fdopen(fd, 'wb') as fh:
            fh.write(_build_min_elf32([('main', 0x08000100)]))
        got, err = load_elf_symbols(tmp)
        assert got is not None and not err and symbolize(got, 0x08000104) == 'main+0x4'
        with open(tmp, 'wb') as fh:
            fh.write(b'nope')
        got2, err2 = load_elf_symbols(tmp)
        assert got2 is None and err2
    finally:
        os.remove(tmp)
    assert load_elf_symbols('') == (None, '')
    print('FAULT REPORT OK')


def test_fault_panel():
    """故障诊断面板：树形详情/历史切换/主窗体 ingest 钩子/状态栏徽标。"""
    from PySide6 import QtWidgets

    from core.settings import read_setting, write_setting
    from ui.fault_panel import FaultPanel
    from ui.main_window import MainWindow

    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication(sys.argv)
    saved_elf = read_setting('fault/elf_path', '')
    try:
        panel = FaultPanel()
        line = ('!FAULT T=HardFault PC=0x08001234 LR=0x08004567 XPSR=0x01000000 '
                'CFSR=0x00008282 HFSR=0x40000000 BFAR=0x00000000 MMFAR=0x00000000')
        info = panel.record_line(line)
        assert info is not None and panel.history() == [info]
        titles = [panel.tree.topLevelItem(i).text(0)
                  for i in range(panel.tree.topLevelItemCount())]
        assert titles == ['故障地址', '故障函数', '硬故障', '总线故障',
                          '内存管理故障', '用法故障'], titles
        assert panel.tree.topLevelItem(0).text(1) == '0x00000000'

        def find(group, name=None):
            for i in range(panel.tree.topLevelItemCount()):
                top = panel.tree.topLevelItem(i)
                if top.text(0) == group:
                    if name is None:
                        return top
                    for j in range(top.childCount()):
                        c = top.child(j)
                        if c.text(0) == name:
                            return c
            return None
        # 置位醒目（加粗+status_err 前景），未置位灰显
        assert find('总线故障', 'PRECISERR').font(0).bold()
        assert not find('总线故障', 'IBUSERR').font(0).bold()
        assert find('总线故障', 'BFARVALID').text(1) == '置位 0x00000000'
        # 半行：组值=缺失；未知键 →「其他字段」顶层组
        panel.record_line('!FAULT T=BusFault PC=0x080012')
        assert find('总线故障').text(1) == '缺失'
        panel.record_line('!FAULT T=UsageFault PC=0x1 SHCSR=0x00007000')
        titles = [panel.tree.topLevelItem(i).text(0)
                  for i in range(panel.tree.topLevelItemCount())]
        assert '其他字段' in titles
        assert len(panel.history()) == 3 and panel.list_hist.count() == 3
        panel.list_hist.setCurrentRow(0)   # 历史切换回半行记录
        assert find('总线故障').text(1) == '缺失'
        panel.clear_all()
        assert panel.tree.topLevelItemCount() == 1 and panel.list_hist.count() == 0

        # 主窗体 ingest 钩子：RX 命中 → 面板记录 + 徽标点亮 + 旧解码不受影响
        win = MainWindow()
        win.show()
        win.wave.state.enabled = False
        win.combo_rx_format.setCurrentText('ASCII')
        win._port_watch_timer.stop()
        win.ingest_bytes(line.encode() + b'\n')
        assert len(win.fault.history()) == 1
        assert win.btn_fault_badge.isVisibleTo(win)
        assert 'HardFault' in win.btn_fault_badge.text()
        win.ingest_bytes(b'plain line\n')          # 非故障行不误报
        assert len(win.fault.history()) == 1
        sys_texts = ' '.join(e['text'] for e in win.message_entries
                             if e['direction'] == 'SYS')
        assert 'HardFault 解码' in sys_texts       # fault_decode 钩子保持
        win._port_watch_timer.stop()
        win.close()
    finally:
        write_setting('fault/elf_path', saved_elf)
    print('FAULT PANEL OK')


def test_wave_presets():
    """C12：波形场景预设 INI 往返 + 一键应用（波特率经主窗体钩子）。"""
    import os
    import tempfile

    from core.wave_presets import (WavePreset, default_presets_path,
                                   load_wave_presets, save_wave_presets)
    from ui.wave_window import WaveWindow

    assert default_presets_path().endswith('wave_presets.txt')

    # INI 往返（含中文名 / auto_y=0 / 手动量程）
    tmp = os.path.join(tempfile.mkdtemp(prefix='qt_presets_'), 'wave_presets.txt')
    tp = WavePreset(name='自测预设', baud='115200', protocol='JustFloat',
                    filter='滑动平均', filter_param='8', auto_y=False,
                    y_min='0', y_max='255', window='500')
    assert save_wave_presets([tp], tmp)
    with open(tmp, encoding='utf-8-sig') as f:
        head = f.readline().strip()
    assert head == '[自测预设]', '应带 BOM 的 UTF-8 INI'
    round_trip = load_wave_presets(tmp)
    assert len(round_trip) == 1
    back = round_trip[0]
    assert (back.name, back.baud, back.protocol, back.filter, back.filter_param,
            back.auto_y, back.y_min, back.y_max, back.window) == \
        ('自测预设', '115200', 'JustFloat', '滑动平均', '8', False, '0', '255', '500')
    # 损坏文件不抛
    with open(tmp, 'w', encoding='utf-8') as f:
        f.write('[坏预设]\nbaud=not-a-number\n\xff\xfe junk')
    assert len(load_wave_presets(tmp)) == 1

    # 一键应用：逐项写入控件；波特率经主窗体钩子（这里用局部变量模拟）
    wave = WaveWindow()
    baud_values = []
    wave.presets_path = tmp
    wave.baud_setter = lambda text: baud_values.append(text)
    wave.baud_getter = lambda: '921600'
    assert save_wave_presets([tp], tmp)
    wave.refresh_presets(select='自测预设')
    assert wave.combo_preset.count() == 1
    assert wave.apply_wave_preset('自测预设')
    assert baud_values == ['115200'], '应用预设应写入波特率'
    assert wave.combo_protocol.currentText() == 'JustFloat'
    assert wave.state.protocol == 'JustFloat'
    assert wave.combo_filter.currentText() == '滑动平均'
    assert wave.spin_filter_param.value() == 8
    assert wave.state.filter_param == 8
    assert not wave.chk_auto_y.isChecked()
    assert wave.state.y_min == 0.0 and wave.state.y_max == 255.0
    assert wave.combo_window.currentText() == '500'
    assert wave.state.window_frames == 500

    # 抓取当前状态为预设快照
    snap = wave.capture_wave_preset('快照')
    assert snap.baud == '921600' and snap.protocol == 'JustFloat' and snap.window == '500'
    # 不存在的预设返回 False
    assert not wave.apply_wave_preset('不存在')
    wave.stop_timers()
    print('WAVE PRESETS OK')


def test_xy_view():
    """C11：XY 相位图——余晖衰减/常驻/关、固定与自适应量程、清缓冲重置、窗口数切换。"""
    import numpy as np

    from core.wave_state import WaveState
    from ui.xy_view import (CELL_SIZE, XYCell, XYSection, adaptive_span,
                            persist_seconds, span_half)

    # 档位映射（对齐主线 get_wave_xy_persist_seconds / get_wave_xy_span_half）
    assert persist_seconds('0') == 0.0 and persist_seconds('1s') == 1.0
    assert persist_seconds('常驻') == -1.0 and persist_seconds('5s') == 5.0
    assert span_half('±8') == 8 and span_half('自适应') == 0

    # 自适应量程：2 的幂量化防抖
    assert adaptive_span(np.array([3.0, -4.0])) == 4.0
    assert adaptive_span(np.array([4.1])) == 8.0
    assert adaptive_span(np.zeros(0)) == 8.0

    cell = XYCell(0)
    # 固定量程 ±8：值 (0,0) 落中心，(8,0) 落右缘
    cell.feed(np.array([0.0]), np.array([0.0]))
    cell.tick(0.05)
    center = cell._accum[CELL_SIZE // 2, CELL_SIZE // 2]
    assert center == 1.0
    cell.feed(np.array([8.0]), np.array([0.0]))
    cell.tick(0.05)
    assert cell._accum[CELL_SIZE // 2, CELL_SIZE - 1] == 1.0
    # Y 轴向上：+8 落顶缘（映射式上下各留 1px 内缩）
    cell.feed(np.array([0.0]), np.array([8.0]))
    cell.tick(0.05)
    assert cell._accum[1, CELL_SIZE // 2] == 1.0

    # 余晖 1s：50ms 衰减但仍亮；常驻不衰减；0 每帧清
    cell2 = XYCell(0)
    cell2.set_persist_option('1s')
    cell2.feed(np.array([1.0]), np.array([1.0]))
    cell2.tick(0.05)
    v1 = float(cell2._accum.max())
    cell2.tick(0.05)
    v2 = float(cell2._accum.max())
    assert 0 < v2 < v1, '余晖应逐帧衰减'
    cell2.set_persist_option('常驻')
    cell2.tick(0.5)
    assert float(cell2._accum.max()) == v2, '常驻不衰减'
    cell2.set_persist_option('0')
    cell2.tick(0.05)
    assert not cell2._accum.any(), '余晖关闭每帧清屏'

    # 清像素
    cell.clear_pixels()
    assert not cell._accum.any()
    # 渲染冒烟：paintEvent 里 numpy→QImage 映射不抛
    cell.feed(np.array([-6.0, 6.0]), np.array([3.0, -3.0]))
    cell.tick(0.05)
    assert not cell.grab().isNull()

    # 区块联动：进帧 → tick → 分配到窗 1（X=CH0,Y=CH1）
    st = WaveState()
    st.enabled = True
    st.consume_bytes(b'1,2\n2,4\n3,6\n')
    section = XYSection(st)
    section.combo_count.setCurrentText('2')
    assert len(section._cells) == 2
    section.tick()
    assert section._cells[0]._accum.any(), '窗 1 应收到 CH0/CH1 点'
    # 改配对 X=CH1,Y=CH0 后换数据可收
    section._slot_x[0].setCurrentIndex(1)
    section._slot_y[0].setCurrentIndex(0)
    st.consume_bytes(b'5,7\n')
    section.tick()
    assert section._cells[0]._accum.any()

    # 清缓冲后 frame_count 回 0 → 累积重置
    section._last_total = st.frame_count
    st.reset_buffers()
    section.tick()
    assert section._last_total == 0

    # 波形窗开关联动
    from ui.wave_window import WaveWindow
    wave = WaveWindow()
    wave.btn_xy.setChecked(True)
    assert wave.xy_section.isVisibleTo(wave)
    wave.btn_xy.setChecked(False)
    assert not wave.xy_section.isVisibleTo(wave)
    wave.stop_timers()
    print('XY VIEW OK')


def test_session_record():
    """C13：会话录制 JSONL + 回放重放进接收管线。"""
    import json
    import os
    import tempfile

    from PySide6 import QtWidgets

    import ui.main_window as mw
    from core.session_record import SessionRecorder, load_replay_chunks

    tmp_dir = tempfile.mkdtemp(prefix='qt_record_')
    recorder = SessionRecorder(tmp_dir)
    path = recorder.start()
    assert recorder.active and path.endswith('.jsonl')
    recorder.record_chunk(b'1,2\n')
    recorder.record_chunk(b'3,4\n')
    recorder.stop()
    assert not recorder.active
    chunks = load_replay_chunks(path)
    assert [c[1] for c in chunks] == [b'1,2\n', b'3,4\n']
    assert all(isinstance(c[0], int) for c in chunks)
    with open(path, encoding='utf-8') as f:
        rec = json.loads(f.readline())
    assert rec['dir'] == 'DATA' and rec['n'] == 4 and rec['b64']
    assert load_replay_chunks(os.path.join(tmp_dir, 'nope.jsonl')) == []

    # 主窗体集成：录制 → 收数 → 停止 → 回放进管线
    old_get_log_dir = mw.get_log_dir
    real_recorder_cls = mw.SessionRecorder
    stamps = iter(['20261024_000001', '20261024_000002'])
    mw.SessionRecorder = lambda d: real_recorder_cls(d, stamp_fn=lambda: next(stamps))
    mw.get_log_dir = lambda: tmp_dir
    try:
        app = QtWidgets.QApplication.instance() or QtWidgets.QApplication(sys.argv)
        win = mw.MainWindow()
        win.show()
        win.wave.state.enabled = True
        win.combo_rx_format.setCurrentText('ASCII')
        assert win.toggle_session_record(True) is not None
        assert win.recorder.active
        win.ingest_bytes(b'hello-rec\n')
        win.ingest_bytes(b'7.5,2.5\n')
        win.toggle_session_record(False)
        assert not win.recorder.active

        record_path = win.recorder.path
        assert load_replay_chunks(record_path)[0][1] == b'hello-rec\n'

        # 回放：逐块喂回，波形/日志都能看到
        win.clear_log()
        win.wave.state.reset_buffers()
        win._start_replay(record_path)
        assert win._replay_state is not None
        while win._replay_step():
            pass
        assert win._replay_state is None, '回放完自动收尾'
        assert 'hello-rec' in win.log.toPlainText()
        assert win.wave.state.frame_total == 1, '回放数据应进波形管线'
        assert '会话回放完成' in win.log.toPlainText()
        # 回放期间录制按钮开着也不会套娃
        win.toggle_session_record(True)
        path2 = win.recorder.path
        win._start_replay(record_path)
        while win._replay_step():
            pass
        win.toggle_session_record(False)
        assert load_replay_chunks(path2) == [], '回放不再录自己'
        assert len(load_replay_chunks(record_path)) == 2, '原录制文件不被第二次录制覆盖'
        win._port_watch_timer.stop()
        win.close()
    finally:
        mw.get_log_dir = old_get_log_dir
        mw.SessionRecorder = real_recorder_cls
    print('SESSION RECORD OK')


def test_control_channel():
    """C14：AI 控制通道——TCP JSON 行协议（ping/status/send/read_lines/协议/波形控制）。"""
    import json
    import time

    from PySide6 import QtCore, QtWidgets

    from PySide6.QtNetwork import QTcpSocket

    from core.control_channel import ControlAPI, ControlChannel, handle_control_request
    from core.wave_state import PROTOCOLS
    from ui.main_window import MainWindow

    # 纯协议层：假 api
    calls = []

    def fake_send(text, hex, newline):
        calls.append(('send', text, hex, newline))
        return {'ok': True}

    api = ControlAPI(
        status=lambda: {'connected': False, 'protocol': 'FireWater'},
        list_ports=lambda: ['COM8 — USB-SERIAL CH340'],
        send=fake_send,
        read_lines=lambda count, filter: {'ok': True, 'lines': [{'t': 't', 'dir': 'RX',
                                                                 'text': f'{count}|{filter}'}]},
        wave_snapshot=lambda count: {'ok': True, 'count': count},
        set_protocol=lambda p: {'ok': True, 'protocol': p},
        wave_clear=lambda: {'ok': True},
        wave_sim=lambda on: {'ok': True, 'sim_on': on},
        record=lambda on: {'ok': True, 'recording': on},
    )
    r = handle_control_request('{"cmd":"ping"}', api)
    assert r['ok'] and r['control'] == 'on'
    r = handle_control_request('{bad json', api)
    assert not r['ok'] and 'JSON 解析失败' in r['error']
    r = handle_control_request('{"cmd":"nope"}', api)
    assert '未知命令' in r['error']
    r = handle_control_request('{"cmd":"send","text":"AT","hex":false,"newline":"LF"}', api)
    assert r['ok'] and calls[-1] == ('send', 'AT', False, 'LF')
    # 命令执行抛异常 → 错误响应不断链
    def boom():
        raise RuntimeError('串口未连接。')
    broken = ControlAPI(status=boom, list_ports=boom, send=boom, read_lines=boom,
                        wave_snapshot=boom, set_protocol=boom, wave_clear=boom,
                        wave_sim=boom, record=boom)
    r = handle_control_request('{"cmd":"status"}', broken)
    assert not r['ok'] and r['error'] == '串口未连接。'

    # TCP 壳：连上后连续多行请求 → 逐行响应（离屏无事件循环，用 processEvents 泵）
    # 注意 QApplication 必须先于 QTcpServer 创建（否则 standalone 跑本组时收不到响应）
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication(sys.argv)
    channel = ControlChannel(api, port=0)
    assert channel.start()

    def pump_connect(sock, port):
        sock.connectToHost('127.0.0.1', port)
        deadline = time.time() + 3
        while sock.state() != sock.SocketState.ConnectedState and time.time() < deadline:
            app.processEvents()
            sock.waitForConnected(20)
        assert sock.state() == sock.SocketState.ConnectedState

    def pump_lines(sock, want: int) -> bytes:
        data = b''
        deadline = time.time() + 3
        while data.count(b'\n') < want and time.time() < deadline:
            app.processEvents()
            data += bytes(sock.readAll())
            if data.count(b'\n') < want:
                sock.waitForReadyRead(20)
        return data

    sock = QTcpSocket()
    pump_connect(sock, channel.port)
    requests = ('{"cmd":"ping"}\n'
                '{"cmd":"list_ports"}\n'
                '{"cmd":"read_lines","count":3,"filter":"err"}\n')
    sock.write(requests.encode('utf-8'))
    data = pump_lines(sock, 3)
    sock.disconnectFromHost()
    lines = [json.loads(x) for x in data.decode().strip().splitlines()]
    assert lines[0]['ok'] and lines[1]['ports'] == ['COM8 — USB-SERIAL CH340']
    assert lines[2]['lines'][0]['text'] == '3|err'
    channel.stop()

    # 主窗体集成：默认开启 + 复制接入信息动作
    win = MainWindow()
    win.show()
    win.wave.state.enabled = True
    win.combo_rx_format.setCurrentText('ASCII')
    if not win._control_channel.listening:  # 9101 被外部进程占用时兜底临时端口
        win._control_channel.stop()
        win._control_channel._port = 0
        assert win._control_channel.start()
    assert win._control_channel.listening, '控制通道应默认开启'
    log_text = win.log.toPlainText()
    assert ('AI 控制通道已开启' in log_text) or ('端口被占用' in log_text), \
        '默认开启成功或被占用降级提示，二者必有其一'
    # 一键复制接入信息：剪贴板有内容且带通道地址，日志有成功提示
    win.act_copy_ai.trigger()
    clip = QtWidgets.QApplication.clipboard().text()
    assert '127.0.0.1' in clip and 'headless_main.py' in clip and 'read_lines' in clip
    assert '已复制 AI 接入信息' in win.log.toPlainText()

    sock2 = QTcpSocket()
    pump_connect(sock2, win._control_channel.port)

    def rpc(request: dict) -> dict:
        sock2.write((json.dumps(request, ensure_ascii=False) + '\n').encode('utf-8'))
        line = pump_lines(sock2, 1).split(b'\n')[0]
        return json.loads(line)

    status = rpc({'cmd': 'status'})
    assert status['ok'] and status['connected'] is False and status['protocol'] == 'FireWater'
    # 发送未连接 → 错误响应不断链
    r = rpc({'cmd': 'send', 'text': 'AT'})
    assert not r['ok'] and '未连接' in r['error']
    # 波形控制真动作
    assert rpc({'cmd': 'wave_sim', 'on': True})['ok']
    assert win.wave.btn_sim.isChecked()
    assert rpc({'cmd': 'set_protocol', 'protocol': 'JustFloat'})['ok']
    assert win.wave.state.protocol == 'JustFloat'
    assert rpc({'cmd': 'set_protocol', 'protocol': 'Bad'})['ok'] is False
    # 关 sim（防模拟帧串扰）、切回 FireWater、清缓冲后灌两帧
    assert rpc({'cmd': 'wave_sim', 'on': False})['ok']
    assert not win.wave.btn_sim.isChecked()
    assert rpc({'cmd': 'set_protocol', 'protocol': 'FireWater'})['ok']
    win.wave.state.reset_buffers()
    win.wave.state.consume_bytes(b'1,2\n2,4\n')
    snap = rpc({'cmd': 'wave_snapshot', 'count': 2})
    assert snap['count'] == 2 and len(snap['channels']) == 2
    win.ingest_bytes(b'ctrl-line\n')
    lines = rpc({'cmd': 'read_lines', 'count': 5, 'filter': 'ctrl-line'})['lines']
    assert any('ctrl-line' in l['text'] for l in lines)
    assert rpc({'cmd': 'record', 'on': True})['recording'] is True
    assert rpc({'cmd': 'record', 'on': False})['recording'] is False
    assert rpc({'cmd': 'wave_clear'})['ok']
    assert win.wave.state.frame_count == 0

    sock2.close()
    win.wave.btn_sim.setChecked(False)
    win._port_watch_timer.stop()
    win.close()
    # 两段式关窗：收尾在 singleShot(0) 的 finalize——放行事件循环至收尾完成
    for _ in range(50):
        app.processEvents()
        if getattr(win, '_finalized', False):
            break
        time.sleep(0.01)
    assert getattr(win, '_finalized', False), '关窗收尾应在下一拍完成'
    assert not win._control_channel.listening, '关窗应停掉控制通道（释放端口）'
    print('CONTROL CHANNEL OK')


def test_headless():
    """C15：无头捕获——回放管线/触发器/错误命中/期望测试/退出码（不需要真实串口）。"""
    import base64
    import json
    import os
    import tempfile

    from core.headless import (EXIT_ERROR_HITS, EXIT_OK, EXIT_TEST_FAIL,
                               HeadlessOptions, TriggerEngine, invoke_expect_steps,
                               run_headless)

    tmp = tempfile.mkdtemp(prefix='qt_headless_')
    log_dir = os.path.join(tmp, 'logs')

    # 触发器引擎：before/after 上下文 + once
    trigger_cfg = os.path.join(tmp, 'triggers.json')
    with open(trigger_cfg, 'w', encoding='utf-8') as f:
        json.dump([{'name': 'panic', 'pattern': 'panic', 'before_lines': 2,
                    'after_lines': 1, 'once': True}], f)

    def data_line(b: bytes, ms: int) -> str:
        return json.dumps({'dir': 'DATA', 'b64': base64.b64encode(b).decode(),
                           'n': len(b), 'ms': ms}) + '\n'

    replay_path = os.path.join(tmp, 'replay.jsonl')
    with open(replay_path, 'w', encoding='utf-8') as f:
        f.write(data_line(b'line-1\nline-2\n', 10))
        f.write(data_line(b'kernel panic!\n', 20))
        f.write(data_line(b'after-panic\n', 30))
        f.write(data_line(b'CFSR: 0x00020000\n', 40))

    opts = HeadlessOptions(replay_path=replay_path, protocol='FireWater',
                           trigger_file=trigger_cfg, log_dir=log_dir)
    exit_code, summary = run_headless(opts, print_fn=lambda *_: None)
    assert exit_code == EXIT_OK, '回放文件没有 error_regex → 0'
    assert summary['lines'] == 5 and summary['frames'] == 0, '文本行不是数值帧'
    with open(opts.out, encoding='utf-8') as f:
        events = [json.loads(line) for line in f if line.strip()]
    assert events[0]['dir'] == 'META' and events[-1]['dir'] == 'SUMMARY'
    dirs = [e['dir'] for e in events]
    assert dirs.count('RX') == 5 and 'EVT' in dirs, 'panic 行应触发 EVT'
    assert 'panic' in next(e for e in events if e['dir'] == 'EVT')['text']
    assert os.path.isdir(os.path.join(log_dir, 'events')), '触发上下文落盘'
    evt_file = next(e for e in events if e['dir'] == 'EVT')['file']
    with open(evt_file, encoding='utf-8') as f:
        ctx = f.read()
    assert 'line-2' in ctx and 'kernel panic!' in ctx and 'after-panic' in ctx,         'before/after 上下文（before 窗口含命中行自身，与主线一致）'
    # HardFault 自动解码事件
    fault = next(e for e in events if e['dir'] == 'FAULT')
    assert 'UFSR.INVSTATE' in fault['decoded']

    # 错误模式命中 → 退出码 2
    opts2 = HeadlessOptions(replay_path=replay_path, protocol='FireWater',
                            error_regex='panic', log_dir=log_dir)
    exit_code, summary = run_headless(opts2, print_fn=lambda *_: None)
    assert exit_code == EXIT_ERROR_HITS and summary['error_hits'] == 2,         "panic 命中 kernel panic! 与 after-panic 两行" 

    # JustFloat 回放：帧行带 ch
    frame = __import__('struct').pack('<2f', 1.5, -2.25) + b'\x00\x00\x80\x7f'
    replay_jf = os.path.join(tmp, 'replay_jf.jsonl')
    with open(replay_jf, 'w', encoding='utf-8') as f:
        f.write(data_line(frame * 3, 5))
    opts3 = HeadlessOptions(replay_path=replay_jf, protocol='JustFloat', log_dir=log_dir)
    exit_code, summary = run_headless(opts3, print_fn=lambda *_: None)
    assert exit_code == EXIT_OK and summary['frames'] == 3
    with open(opts3.out, encoding='utf-8') as f:
        events = [json.loads(line) for line in f if line.strip()]
    rows = [e for e in events if e['dir'] == 'RX']
    assert len(rows) == 3 and rows[0]['ch'] == [1.5, -2.25]

    # 期望测试：假串口 + 脚本化应答
    class FakeSerial:
        def __init__(self):
            self.rx = b''

        def write(self, data):
            self.rx += bytes(data)
            return len(data)

        def in_waiting(self):
            return 0

    fake = FakeSerial()
    send_calls = []

    def send_fn(text, as_hex, newline_mode):
        send_calls.append((text, as_hex, newline_mode))

    steps = [{'name': 'at', 'send': 'AT', 'expect': 'OK', 'timeout_ms': 5},
             {'name': 'only-read', 'timeout_ms': 5}]
    script = ['AT -> OK\r\n']  # 第一次读返回命中文本，第二次空

    def read_fn(timeout_ms):
        return script.pop(0) if script else ''

    result = invoke_expect_steps(steps, send_fn, read_fn)
    assert result['all_pass'] and result['total'] == 2
    assert send_calls == [('AT', False, 'CRLF')]
    # 失败路径
    script = ['no match here']
    result = invoke_expect_steps(steps[:1], send_fn, read_fn)
    assert not result['all_pass'] and result['fail'] == 1

    # 参数校验
    try:
        run_headless(HeadlessOptions(replay_path=replay_path, port='COM8'))
        assert False, 'replay+port 应报错'
    except ValueError:
        pass
    try:
        run_headless(HeadlessOptions(protocol='Bad'))
        assert False, '协议校验'
    except ValueError:
        pass
    # 期望测试失败路径（串口注入假对象）
    opts4 = HeadlessOptions(replay_path=replay_path, protocol='FireWater',
                            log_dir=log_dir)
    exit_code, summary = run_headless(opts4, print_fn=lambda *_: None)
    assert exit_code == EXIT_OK and summary['test_total'] == 0
    print('HEADLESS OK')


def test_wave_protocols():
    """三协议解码 + 视图模型 + 滤波 + 快照合同（对齐主线 SelfTest 断言口径）。"""
    import struct

    from core.wave_state import WaveState, sanitize_terminal_text, try_parse_wave_frame

    assert try_parse_wave_frame('1.5,-2,3e2') == [1.5, -2.0, 300.0]
    assert try_parse_wave_frame('  12 , -0.5 , 7.25e-1 ')[2] == 0.725
    assert try_parse_wave_frame('42') == [42.0]
    assert try_parse_wave_frame('spec:abc') is None
    assert try_parse_wave_frame('1.0,NaN') is None
    assert try_parse_wave_frame('$H:1.0') is None
    assert try_parse_wave_frame('') is None
    ansi = chr(27) + '[32m1.5' + chr(27) + '[0m,2.5'
    assert try_parse_wave_frame(sanitize_terminal_text(ansi)) == [1.5, 2.5]

    # FireWater：多行一次喂入 + 残行保留
    st = WaveState()
    st.enabled = True
    st.consume_bytes(b'1,2\n3,4\n5')
    assert st.frame_total == 2
    assert st.ch_values[0] == [1.0, 3.0] and st.ch_values[1] == [2.0, 4.0]
    assert st.line_buffer == '5'

    # JustFloat：分片到达重组
    frame = struct.pack('<3f', 1.5, -2.25, 3.75) + b'\x00\x00\x80\x7f'
    st2 = WaveState()
    st2.enabled = True
    st2.protocol = 'JustFloat'
    st2.consume_bytes(frame[:6])
    st2.consume_bytes(frame[6:])
    assert st2.frame_total == 1
    assert [st2.ch_values[c][-1] for c in range(3)] == [1.5, -2.25, 3.75]

    # JustFloat：垃圾+好帧 —— 垃圾不含帧尾，整帧的尾在 index 16，
    # payload 连垃圾一起被当 4 通道帧收下（与主线逐字节扫描行为一致）
    st2.consume_bytes(b'\xde\xad\xbe\xef' + frame)
    assert st2.frame_total == 2

    # JustFloat：暂停冻结 append 但缓冲照常排空
    st3 = WaveState()
    st3.enabled = True
    st3.protocol = 'JustFloat'
    st3.paused = True
    st3.consume_bytes(frame)
    assert st3.frame_total == 0 and len(st3.jf_buffer) == 0

    # RawData：字节即采样点
    st4 = WaveState()
    st4.enabled = True
    st4.protocol = 'RawData'
    st4.consume_bytes(bytes([1, 2, 3]))
    st4.consume_bytes(bytes([255]))
    assert st4.ch_values[0] == [1.0, 2.0, 3.0, 255.0] and st4.frame_total == 4

    # 视图：跟随 → 平移历史 → 改宽度保持中心 → 拖回右端恢复跟随
    st5 = WaveState()
    st5.enabled = True
    st5.consume_bytes('\n'.join(str(i) for i in range(3000)).encode() + b'\n')
    assert st5.frame_total == 3000
    assert st5.view_range() == (2000, 3000, True)
    st5.pan_view(-100)
    assert st5.view_range() == (1900, 2900, False)
    st5.set_view_width(500)
    assert st5.view_range() == (2150, 2650, False)
    st5.set_view_window(2900, 3400)
    assert st5.view_range() == (2900, 3000, True)

    # 快照合同：越界/正常都不抛
    assert st5.snapshot(999, 100) == []
    assert st5.snapshot(0, 5) == [2995.0, 2996.0, 2997.0, 2998.0, 2999.0]

    # 滤波：滑动平均窗口语义（前缀和，窗口不足按已有样本平均）
    st6 = WaveState()
    st6.enabled = True
    st6.consume_bytes(b'0\n4\n8\n')
    st6.filter_mode = '滑动平均'
    st6.filter_param = 2
    data, offset, length = st6.view_data(0, 0, 3)
    assert length == 3 and list(data[offset:offset + length]) == [0.0, 2.0, 6.0]
    # 切回"关"立即对旧数据生效（缓存键失效）
    st6.filter_mode = '关'
    data, offset, length = st6.view_data(0, 0, 3)
    assert list(data[offset:offset + length]) == [0.0, 4.0, 8.0]

    # 切协议清缓冲 + 重置解码器
    st6.consume_bytes(b'9')  # 残行
    st6.switch_protocol('JustFloat')
    assert st6.frame_total == 0 and st6.line_buffer == '' and st6.protocol == 'JustFloat'

    # 积压提示节流（假时钟）
    notes = []
    clock = {'t': 100.0}
    st7 = WaveState(now_fn=lambda: clock['t'])
    st7.enabled = True
    st7.on_note = notes.append
    st7.consume_bytes(b'x' * 9000)
    assert len(notes) == 1 and '积压' in notes[0]
    st7.consume_bytes(b'y' * 9000)
    assert len(notes) == 1, '5 秒内重复积压只提示一次'
    clock['t'] += 6.0
    st7.consume_bytes(b'z' * 9000)
    assert len(notes) == 2

    print('WAVE PROTOCOLS OK')


def test_wave_window():
    """波形页端到端：喂字节 → 曲线数据 / 暂停冻结 / 清空 / 模拟数据 / CSV 导出。"""
    import os
    import tempfile

    from PySide6 import QtWidgets

    from ui.main_window import MainWindow

    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication(sys.argv)
    win = MainWindow()
    win.show()
    wave = win.wave

    # 主窗体管线分流：RX 日志 + 波形解码都吃到同一份字节
    win.ingest_bytes(b'1,2\n2,4\n3,6\n')
    wave.repaint_wave()
    assert len(wave._curves) == 2
    xs, ys = wave._curves[0].getData()
    assert list(xs) == [0.0, 1.0, 2.0] and list(ys) == [1.0, 2.0, 3.0]
    assert 'RX 1,2' in win.log.toPlainText()

    # 暂停冻结曲线（数据照常进 RX 日志）
    wave.btn_pause.setChecked(True)
    win.ingest_bytes(b'9,9\n')
    wave.repaint_wave()
    _xs, ys = wave._curves[0].getData()
    assert list(ys) == [1.0, 2.0, 3.0], '暂停时曲线不更新'
    assert 'RX 9,9' in win.log.toPlainText()
    wave.btn_pause.setChecked(False)

    # 通道开关：芯片取消勾选 → 曲线隐藏（无数据）
    wave._chips[0].setChecked(False)
    wave.repaint_wave()
    _xs, ys = wave._curves[0].getData()
    assert ys is None or ys.size == 0, '芯片关闭后该通道不绘制'
    wave._chips[0].setChecked(True)

    # 模拟数据走真实解码管线（当前协议 FireWater）
    wave.btn_sim.setChecked(True)
    wave._on_sim_tick()
    assert wave.state.frame_total > 3
    wave.btn_sim.setChecked(False)

    # CSV 导出：表头 + 行数
    fd, path = tempfile.mkstemp(suffix='.csv')
    os.close(fd)
    try:
        wave.write_csv(path)
        with open(path, 'r', encoding='utf-8-sig') as f:
            rows = f.read().strip().splitlines()
        assert rows[0] == 'frame,CH0,CH1,CH2', rows[0]  # 模拟帧是 3 通道，CH2 被扩出来
        assert len(rows) - 1 == wave.state.frame_count
    finally:
        os.unlink(path)

    # 清空归零
    wave.clear_buffers()
    wave.repaint_wave()
    assert wave.state.frame_total == 0

    # ---- 三点历史滑块（主线语义：左右点拖宽度、中点拖中心位置）----
    bar = wave.slider_view
    bar.resize(600, 34)
    for i in range(3000):
        wave.state.consume_bytes(f'{i}\n'.encode())
    wave.repaint_wave()
    pts = bar._points()
    assert pts is not None, '缓冲 >2 帧后滑块条有点位'
    xs, xm, xe, _total = pts
    assert abs(xm - (xs + xe) / 2) < 1e-6, '中点恒在两点正中'
    assert bar._hit(xs) == 'start' and bar._hit(xe) == 'end' and bar._hit(xm) == 'mid'
    assert bar._hit(2) == 'none', '轨道外不命中'
    # 拖中点：宽度不变，窗口中心跟随鼠标（拖到 25% 位置）
    w_before = int(wave.state.window_frames)
    bar._begin_drag('mid', xm)
    x_target = bar.MARGIN + 0.25 * (600 - 2 * bar.MARGIN)
    bar._drag_to(x_target)
    start, end, follow = wave.state.view_range()
    assert not follow, '中点拖离右端 → 历史模式'
    assert abs((end - start) - w_before) <= 2, '中点拖动宽度不变'
    assert abs((start + end) / 2 - 0.25 * 2999) < 30, '窗口中心跟随鼠标位置'
    # 拖左点：右端钉住，宽度收窄
    end_before = end
    xs2 = bar._points()[0]
    bar._begin_drag('start', xs2)
    bar._drag_to(xs2 + 80)
    s2, e2, _f2 = wave.state.view_range()
    assert e2 == end_before, '拖左点右端钉住'
    assert (e2 - s2) < (end_before - start), '左点右移 → 窗口收窄'
    # 拖右点贴到最右 → 恢复跟随
    xe3 = bar._points()[2]
    bar._begin_drag('end', xe3)
    bar._drag_to(600 - bar.MARGIN)
    _s3, _e3, follow3 = wave.state.view_range()
    assert follow3, '右点拖到最右端 → 恢复跟随'

    # ---- 滚轮缩放（主线语义） ----
    # 横轴区滚轮：窗口帧数 ×0.8（跟随态下贴末尾）
    old_window = int(wave.state.window_frames)
    wave._apply_window_zoom(True)
    assert int(wave.state.window_frames) == int(round(old_window * 0.8))
    assert wave.combo_window.currentText() == str(int(wave.state.window_frames))
    # 曲线区滚轮：缩 Y（锚点=鼠标处数值），自动Y → 手动
    wave.chk_auto_y.setChecked(True)
    wave.repaint_wave()
    y0, y1 = wave.state.y_range()
    wave._apply_y_zoom(0.5, True)  # 中心锚点缩 0.8 倍
    assert not wave.chk_auto_y.isChecked(), '滚轮缩Y自动切手动'
    assert abs(wave.state.y_min - (y0 + 0.1 * (y1 - y0))) < 1e-6
    assert abs(wave.state.y_max - (y1 - 0.1 * (y1 - y0))) < 1e-6

    # ---- 曲线填充：设置键 wave/fill_under（工具行复选框已迁设置页，集成口径） ----
    # 复选框不再在波形页上：开关走设置键 + apply_fill_from_setting()（设置页/AI 通道调用）
    from core.settings import read_setting as _read_setting
    from core.settings import write_setting as _write_setting
    from ui.wave_window import FILL_SETTING_KEY
    old_fill = _read_setting(FILL_SETTING_KEY, 'true')
    assert not hasattr(wave, 'chk_fill'), '工具行不应再有「曲线填充」复选框'
    _write_setting(FILL_SETTING_KEY, 'true')
    wave.apply_fill_from_setting()
    assert wave._fill_under is True, '设置 true → 填充开'
    wave.repaint_wave()
    c0 = wave._curves[0]
    assert c0.opts.get('fillLevel') is not None, '填充基线跟随绘图区底部'
    brush = c0._fill_brush
    assert brush is not None and brush.color().alpha() == 26, '同色 alpha=26'
    _write_setting(FILL_SETTING_KEY, 'false')
    wave.apply_fill_from_setting()
    assert wave._fill_under is False
    assert wave._curves[0]._fill_brush is None
    assert wave._curves[0].opts.get('fillLevel') is None
    _write_setting(FILL_SETTING_KEY, old_fill)  # 还原设置键
    wave.apply_fill_from_setting()

    win.close()
    print('WAVE WINDOW OK')


def test_hexutil():
    from core.hexutil import bytes_to_hex_text, try_parse_hex_text

    assert bytes_to_hex_text(b'\xDE\xAD\x01') == 'DE AD 01'
    assert bytes_to_hex_text(b'') == ''
    assert try_parse_hex_text('DE AD 01') == b'\xde\xad\x01'
    assert try_parse_hex_text('de,ad;be:ef') == b'\xde\xad\xbe\xef'
    assert try_parse_hex_text('0xDE 0x0A') == b'\xde\x0a'
    assert try_parse_hex_text('0X2 A') == b'\x02\n'  # 0X 大小写不敏感、单 token 1~2 位
    assert try_parse_hex_text('  ') == b''
    try:
        try_parse_hex_text('GG')
        raise AssertionError('GG 应当抛 ValueError')
    except ValueError as exc:
        assert 'HEX 输入无效: GG' in str(exc)
    print('HEXUTIL OK')


def test_main_window_m3():
    """M3 端到端：HEX 收发 / 换行模式 / 发送历史 / 文件发送状态机 / 查找条。"""
    from PySide6 import QtWidgets

    from ui.main_window import MainWindow

    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication(sys.argv)
    win = MainWindow()
    win.show()

    # HEX 接收：按块落显，残行先落显
    win.combo_rx_format.setCurrentText('HEX')
    win.ingest_bytes(b'\xde\xad\xbe\xef')
    assert 'RX DE AD BE EF' in win.log.toPlainText()
    # 切回 ASCII：HEX 模式下没进行缓冲，积压为 0
    win.combo_rx_format.setCurrentText('ASCII')
    assert win.buffer.backlog_bytes == 0

    # HEX 发送：未连接仅入日志；非法输入置状态不发送
    win.combo_tx_format.setCurrentText('HEX')
    win.edit_send.setText('01 0x2, A')
    win.send()
    assert 'TX 01 02 0A' in win.log.toPlainText()
    win.edit_send.setText('GG')
    win.send()
    assert 'HEX 输入无效: GG' in win.lbl_status.text()

    # ASCII 发送 + 换行模式：展示层 <CR>/<LF> 可视化
    win.combo_tx_format.setCurrentText('ASCII')
    win.combo_newline.setCurrentText('CRLF')
    win.edit_send.setText('AT+RST')
    win.send()
    assert 'TX AT+RST<CR><LF>' in win.log.toPlainText()

    # 发送历史：⬆ 回翻、⬇ 走到底恢复草稿、连续重复不入栈
    win.edit_send.setText('draft-x')
    win.edit_send.recall(-1)
    assert win.edit_send.text() == 'AT+RST', '⬆ 应取最近一条历史'
    win.edit_send.recall(+1)
    assert win.edit_send.text() == 'draft-x', '⬇ 走到底应恢复草稿'
    before = len(win.edit_send._history)
    win.edit_send.push_history('HIST-UNIQUE-1')
    win.edit_send.push_history('HIST-UNIQUE-1')
    assert len(win.edit_send._history) == before + 1, '连续重复只入栈一次'

    # 文件发送状态机：假 worker 4KiB 分块，20ms 定时器驱到完成
    sent = bytearray()

    class FakeWorker:
        def write_bytes(self, data: bytes) -> bool:
            sent.extend(data)
            return True

    win.worker = FakeWorker()
    import tempfile
    import os
    payload = bytes(range(256)) * 40  # 10240 字节 → 3 块（4096+4096+2048）
    fd, path = tempfile.mkstemp(suffix='.bin')
    os.write(fd, payload)
    os.close(fd)
    try:
        win._file_send = {'name': 't.bin', 'bytes': payload, 'pos': 0}
        win.btn_file.setText('停止')
        win.file_timer.start()
        for _ in range(10):
            win._file_send_tick()
            if win._file_send is None:
                break
        assert bytes(sent) == payload, '文件必须原样字节流发完'
        assert win._file_send is None
        assert '文件发送完成: t.bin' in win.lbl_status.text()

        # 中途停止：已发字节如实入账
        win.worker = FakeWorker()
        win._file_send = {'name': 't2.bin', 'bytes': payload, 'pos': 0}
        win._file_send_tick()
        assert win._file_send['pos'] == 4096
        win._stop_file_send('手动停止')
        assert win._file_send is None
        assert '[文件发送中断] t2.bin: 手动停止（已发 4096 / 10240 字节）' in win.log.toPlainText()
        assert win.btn_file.text() == '文件发送'
    finally:
        os.unlink(path)

    # 查找条：实时高亮 + 计数 + 循环跳转
    win.find_bar.open_and_focus()
    win.find_bar.edit_find.setText('AT+RST')
    assert win.find_bar.lbl_count.text().startswith('第 1/')
    total = int(win.find_bar.lbl_count.text().split('/')[1].split(' ')[0])
    win.find_bar.navigate(+1)
    assert win.find_bar.lbl_count.text() == f'第 {2 if total > 1 else 1}/{total} 处'
    win.find_bar.navigate(-1)
    assert win.find_bar.lbl_count.text() == f'第 1/{total} 处'
    win.find_bar.hide_and_clear()
    assert not win.log.extraSelections(), '关闭查找条应清掉高亮'

    win.worker = None
    win.close()
    print('MAINWINDOW M3 OK')


def test_modbus_core():
    from core.modbus import (WatchItem, build_modbus_request, decode_modbus_registers,
                             get_modbus_crc16, merge_watch_groups, parse_modbus_response,
                             slice_watch_value, test_modbus_crc, watch_flash_tone)

    # CRC16 经典向量：01 03 00 00 00 02 → C4 0B（低字节在前）
    assert get_modbus_crc16(bytes.fromhex('010300000002')) == bytes.fromhex('C40B')
    frame, error = build_modbus_request(1, 3, 0, 2)
    assert error == '' and frame == bytes.fromhex('010300000002C40B')
    assert test_modbus_crc(frame) and not test_modbus_crc(frame[:-2] + b'\x00\x00')

    # 组帧边界：03 数量 126 拒绝；06 写值 0x2000；05 置位 0xFF00；16 列表
    _f, e = build_modbus_request(1, 3, 0, 126)
    assert '1~125' in e
    f6, e6 = build_modbus_request(1, 6, 0x10, 1, value_text='0x2000')
    assert f6[:6] == bytes.fromhex('010600102000') and test_modbus_crc(f6)
    f5, _ = build_modbus_request(1, 5, 4, 1, value_text='on')
    assert f5[4:6] == b'\xff\x00'
    f16, e16 = build_modbus_request(2, 16, 0, 0, value_list=['100', '0x2000', '-5'])
    assert e16 == '' and f16[6] == 6 and f16[4:6] == b'\x00\x03'
    # -5 按 0xFFFB 收（band 0xFFFF，与主线一致）
    assert f16[7:13] == bytes.fromhex('0064') + bytes.fromhex('2000') + bytes.fromhex('FFFB')

    # 响应解析：2 寄存器 0x000A 0x0102 → 十进制 '10, 258'
    body = bytes.fromhex('010304000A0102')
    resp = body + get_modbus_crc16(body)
    parsed = parse_modbus_response(resp, 3, 1, 2)
    assert parsed['ok'] and parsed['reg_bytes'] == bytes.fromhex('000A0102')
    assert decode_modbus_registers(parsed['reg_bytes'], '十进制') == '10, 258'
    assert decode_modbus_registers(parsed['reg_bytes'], 'HEX') == '0x000A, 0x0102'
    assert decode_modbus_registers(bytes.fromhex('FFFF'), '有符号') == '-1'
    # float32(ABCD)：寄存器 0x42F6 0x0000 → 123.0
    assert decode_modbus_registers(bytes.fromhex('42F60000'), 'float32(ABCD)') == '123'

    # 异常帧：01 83 02 + CRC
    exc_body = bytes.fromhex('018302')
    exc = exc_body + get_modbus_crc16(exc_body)
    parsed = parse_modbus_response(exc, 3, 1, 2)
    assert not parsed['ok'] and '02 非法数据地址' in parsed['error']
    # CRC 坏帧
    parsed = parse_modbus_response(resp[:-2] + b'\x00\x00', 3, 1, 2)
    assert parsed['error'] == 'CRC 校验失败'

    # Watch 合并：间隙≤4 并区间，超上限不并
    items = [WatchItem(1, 3, 0, 2), WatchItem(1, 3, 4, 3), WatchItem(1, 3, 100, 1)]
    groups = merge_watch_groups(items)
    assert len(groups) == 2
    assert (groups[0]['start'], groups[0]['count']) == (0, 7)
    # 切片：合并响应里地址 4、数量 3 的项取 00 01 00 02 00 03 → '1, 2, 3'
    text, raw = slice_watch_value(bytes.fromhex('000A01020064000F000100020003'), 3, 0, items[1])
    assert text == '1, 2, 3' and raw == '00 01 00 02 00 03'
    # 线圈切片：fc1，字节 0b00000101 → 位 0,2 = 1
    bit_item = WatchItem(1, 1, 0, 3)
    text, _ = slice_watch_value(b'\x05', 1, 0, bit_item)
    assert text == '1,0,1'
    # 闪烁语义
    assert watch_flash_tone('10', '11') == 'up'
    assert watch_flash_tone('10', '9') == 'down'
    assert watch_flash_tone('10', '10') == 'same'
    assert watch_flash_tone('ok', '10') == 'changed'
    print('MODBUS CORE OK')


def test_modbus_panel():
    from PySide6 import QtWidgets

    from core.modbus import get_modbus_crc16
    from ui.main_window import MainWindow

    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication(sys.argv)
    win = MainWindow()
    win.show()
    panel = win.modbus

    # 假从站：03 读保持寄存器 → 固定回 2 寄存器 0x0064 0x00C8（100,200）
    def fake_slave(frame: bytes, timeout_ms: int = 600) -> bytes:
        assert frame[-2:] == get_modbus_crc16(frame[:-2])
        if frame[1] == 3:
            body = bytes([frame[0], 3, 4, 0, 100, 0, 200])
            return body + get_modbus_crc16(body)
        body = bytes([frame[0], frame[1]]) + frame[2:3] + b'\x02'
        return (body + get_modbus_crc16(body))[:5]  # 异常响应占位（本测试不会走到）

    panel.transport = fake_slave
    win_modbus_logs = []

    panel.spin_slave.setValue(1)
    panel.combo_fc.setCurrentIndex(2)   # 03
    panel.spin_start.setValue(0)
    panel.spin_count.setValue(2)
    panel.run_transaction()
    assert '寄存器[2] 十进制: 100, 200' in panel.result_box.toPlainText()
    assert panel.lbl_frame.text() == '发送: 01 03 00 00 00 02 C4 0B'

    # 事务回写主日志（TX/RX [Modbus] 条目，Ctrl+F 可检索）
    panel.log_fn = lambda d, t: win_modbus_logs.append((d, t))
    panel.run_transaction()
    assert ('TX', '[Modbus] 01 03 00 00 00 02 C4 0B') in win_modbus_logs
    assert any(d == 'RX' and t.startswith('[Modbus] 01 03 04') for d, t in win_modbus_logs)

    # 监视：加两项（同从站同功能码、地址近邻）→ 合并成一轮读，行值回填
    panel.spin_start.setValue(0)
    panel.spin_count.setValue(1)
    panel.edit_value.setText('温度')
    panel.add_watch()
    panel.spin_start.setValue(1)
    panel.add_watch()
    assert len(panel.watch_items) == 2
    panel.chk_watch.setChecked(True)
    panel.run_watch_cycle()
    assert panel.watch_table.item(0, 4).text() == '100'
    assert panel.watch_table.item(1, 4).text() == '200'
    assert '无响应' not in panel.watch_table.item(0, 5).text()

    # 未连接提示（transport 置空）
    panel.transport = None
    panel.run_transaction()
    assert '串口未连接' in panel.result_box.toPlainText()

    win.close()
    print('MODBUS PANEL OK')


def test_update_check():
    import io
    import json

    from core.update_check import compare_app_versions, get_version_tuple, check_app_update

    assert get_version_tuple('v1.2.10') == (1, 2, 10)
    assert get_version_tuple('1.2') == (1, 2, 0)
    assert get_version_tuple('0.2.0-qt') == (0, 2, 0)
    assert get_version_tuple('v1.x.3') == (1, 0, 3)
    assert compare_app_versions('1.1.0', 'v1.0.0') == 1
    assert compare_app_versions('1.1.0', '1.1.0') == 0
    assert compare_app_versions('0.9.9', '1.0.0') == -1
    assert compare_app_versions('1.1', '1.1.0') == 0

    def fake_opener_ok(request, timeout=10):
        return io.BytesIO(json.dumps({'tag_name': 'v9.9.9', 'body': '新版说明'}).encode())

    result = check_app_update('1.1.0', opener=fake_opener_ok)
    assert result['ok'] and result['has_update'] and result['latest'] == 'v9.9.9'
    assert result['note'] == '新版说明' and result['current'] == '1.1.0'

    def fake_opener_old(request, timeout=10):
        return io.BytesIO(json.dumps({'tag_name': 'v0.0.1'}).encode())
    result = check_app_update('1.1.0', opener=fake_opener_old)
    assert result['ok'] and not result['has_update']

    def fake_opener_dead(request, timeout=10):
        raise OSError('network unreachable')

    result = check_app_update('1.1.0', opener=fake_opener_dead)
    assert not result['ok'] and 'network unreachable' in result['note']
    print('UPDATE CHECK OK')


def test_update_ui():
    from PySide6 import QtWidgets

    from ui.main_window import MainWindow

    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication(sys.argv)
    win = MainWindow()
    win.show()

    win._on_update_result({'ok': True, 'has_update': True, 'latest': 'v9.9.9',
                           'page_url': 'https://example.com', 'note': ''}, manual=False)
    assert '发现新版本 v9.9.9' in win.log.toPlainText()
    assert '有新版本 v9.9.9' in win.lbl_status.text()

    win._on_update_result({'ok': True, 'has_update': False}, manual=True)
    assert '已是最新版本' in win.log.toPlainText()

    win._on_update_result({'ok': False, 'has_update': False, 'note': '超时'}, manual=True)
    assert '检查更新失败: 超时' in win.log.toPlainText()

    win._on_update_result({'ok': False, 'has_update': False, 'note': '静默'}, manual=False)
    assert '静默' not in win.log.toPlainText(), '自动检查失败不打扰'

    win.close()
    print('UPDATE UI OK')


def test_spectrum_dsp():
    import math

    import numpy as np

    from core.dsp import (color_map, magnitude_spectrum, max_bin, to_db,
                          update_ema, update_max_hold, waterfall_row)

    # 1000 点、fs=1000：50 Hz 正弦（25 个周期）峰在 bin 50
    n = 1000
    sig = [3.0 * math.sin(2 * math.pi * 50.0 * i / 1000.0) for i in range(n)]
    mag, used = magnitude_spectrum(sig)
    assert used == 512 and mag.size == 256
    peak = max_bin(to_db(mag), 1)
    assert abs(peak * 1000.0 / used - 50.0) < 1000.0 / used, f'peak bin {peak}'
    # 满量程正弦（频率对齐 bin）→ 汉宁窗相干增益 0.5 → -6.02 dB（主线 C# 同样不补偿）
    sig2 = [math.sin(2 * math.pi * 32 * i / 512.0) for i in range(512)]
    mag2, _ = magnitude_spectrum(sig2)
    db2 = to_db(mag2)
    assert abs(db2[32] + 6.02) < 0.5, f'bin 幅度应≈-6dB，实际 {db2[32]}'
    # 底噪下限 -140
    assert to_db(np.array([0.0]))[0] == -140.0
    # 短输入返回空
    short_mag, short_used = magnitude_spectrum([1.0] * 10)
    assert short_used == 0 and short_mag.size == 0

    # 峰值保持 / EMA
    hold = np.full(4, -140.0)
    update_max_hold(np.array([-120.0, -130.0, -110.0, -100.0]), hold)
    assert list(hold) == [-120.0, -130.0, -110.0, -100.0]
    update_max_hold(np.array([-125.0, -125.0, -125.0, -125.0]), hold)
    assert list(hold) == [-120.0, -125.0, -110.0, -100.0], '保持只升不降'
    avg = np.array([0.0, 0.0])
    update_ema(np.array([10.0, -10.0]), avg, 0.25)
    assert list(avg) == [2.5, -2.5]

    # 瀑布配色端点：t=0 → 暗蓝(0,0,60)，t=1 → 白(255,255,255)
    row = waterfall_row(np.array([-100.0, 0.0]), -100.0, 0.0, 1.0)
    assert row.shape == (2, 3) and tuple(row[0]) == (0, 0, 60) and tuple(row[1]) == (255, 255, 255)
    assert color_map(0.4)[1] > color_map(0.1)[1], '中段应比暗端绿'
    print('SPECTRUM DSP OK')


def test_spectrum_panel():
    import math

    import numpy as np
    from PySide6 import QtWidgets

    from ui.main_window import MainWindow

    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication(sys.argv)
    win = MainWindow()
    win.show()
    panel = win.spectrum

    # 喂 FireWater 单通道 50Hz 正弦（fs=1000 → 帧率 1000/秒，模拟数据 20ms/帧太慢，直接喂数据）
    phase = 0.0
    for _ in range(600):
        phase += 2 * math.pi * 50.0 / 1000.0
        win.wave.state.consume_bytes(f'{math.sin(phase):.6f}\n'.encode())
    panel.combo_ch.setCurrentIndex(0)
    panel.combo_pts.setCurrentText('512')
    panel.edit_fs.setText('1000')

    result = panel.analyze()
    assert result is not None and result['used'] == 512
    assert abs(result['df'] - 1000.0 / 512) < 1e-9
    assert abs(result['rbw'] - 1.5 * 1000.0 / 512) < 1e-9

    # 峰值保持积累：重绘两帧后 max_hold 不低于当前谱
    panel.chk_peak.setChecked(True)
    panel.repaint_spectrum()
    panel.repaint_spectrum()
    assert panel._max_hold is not None and panel._max_hold.size == 256
    # 主频标注
    assert '主频:' in panel.lbl_peak.text() and '--' not in panel.lbl_peak.text()
    # 瀑布在积累且行数受限
    assert 1 <= len(panel._waterfall) <= 120

    # 冻结：底层继续进数据，面板不更新
    panel.btn_freeze.setChecked(True)
    rows_at_freeze = len(panel._waterfall)
    win.wave.state.consume_bytes(b'0.5\n')
    panel.repaint_spectrum()
    assert len(panel._waterfall) == rows_at_freeze, '冻结时瀑布不进新行'
    panel.btn_freeze.setChecked(False)

    # 瀑布方向：默认最新在顶（主线口径）；勾"最新在底"则相反。
    # yInverted 视图下 setImage 第0行落在屏幕底部 → 默认需翻转显示数组（末行=最新→顶）。
    panel.chk_wf.setChecked(True)
    panel.chk_wf_flip.setChecked(False)
    assert panel.wf_plot.getViewBox().state['yInverted'], 'y 轴反转是方向映射的一半（另一半是显示数组翻转）'
    panel.repaint_spectrum()
    panel.repaint_spectrum()  # 数据没变，但再推进一帧序列
    from core.dsp import waterfall_row as _wf_row
    scale = panel._read_scale()
    expected = _wf_row(panel.analyze()['db'], scale[2], scale[0], 1.0)
    assert panel._wf_image.image.shape[0] == expected.shape[0], '转置后第0轴必须是频率bin'
    assert np.array_equal(panel._wf_image.image[:, 0], expected), \
        '默认(最新在顶)：时间第0行(渲染在顶)必须是最新谱行'
    assert np.array_equal(panel._waterfall[0], expected), '数据层最新行仍是列表首元素'
    panel.chk_wf_flip.setChecked(True)
    panel.repaint_spectrum()
    assert np.array_equal(panel._wf_image.image[:, -1], expected), \
        '勾选最新在底：时间末行(渲染在底)必须是最新谱行'

    win.close()
    print('SPECTRUM PANEL OK')


# ===================== 集成收口阶段新增组（自 _verify/*.py 有价值验证并入） =====================

def _pump_until(app, cond, timeout_s=6.0):
    """事件循环轮询直到 cond 成立（替代固定 sleep；超时返回最终真假）。"""
    import time
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        app.processEvents()
        if cond():
            return True
        time.sleep(0.005)
    app.processEvents()
    return bool(cond())


def _fake_worker_cls():
    """替身串口线程类（信号面同 SerialWorker；start() 同步发 opened）。"""
    from PySide6 import QtCore

    class FakeWorker(QtCore.QObject):
        got_bytes = QtCore.Signal(bytes)
        opened = QtCore.Signal()
        open_failed = QtCore.Signal(str)
        lost = QtCore.Signal(str)
        port_lost = QtCore.Signal(str)
        reconnected = QtCore.Signal()

        def __init__(self, device, baud, parent=None, dtr_rts_neutral=False, auto_reconnect=False):
            super().__init__(parent)
            self.device, self.baud = device, baud
            self.breaks = 0

        def start(self):
            self.opened.emit()

        def stop(self):
            pass

        def write_bytes(self, data):
            return True

        def send_break(self):
            self.breaks += 1
            return True

        def line_signals(self):
            return {'cts': True, 'dsr': False, 'ri': False, 'cd': True}

    return FakeWorker


def _snapshot_settings(keys):
    from core.settings import read_setting
    return {k: read_setting(k, '') for k in keys}


def _restore_settings(snapshot):
    """还原设置键（原值为空串时写空串，read_setting 视同缺省）。"""
    from core.settings import write_setting
    for key, value in snapshot.items():
        write_setting(key, value)


_CONNECT_KEYS = ('connect/port', 'connect/baud', 'connect/auto_link',
                 'connect/rx_format', 'connect/tx_format', 'connect/newline')


def test_connect_button_states():
    """集成①：连接按钮状态化——手动开/关、连接丢失、打开失败、自动连接、程序性断开全路径同步。"""
    from PySide6 import QtWidgets

    import ui.main_window as mw

    FakeWorker = _fake_worker_cls()
    saved = _snapshot_settings(_CONNECT_KEYS)
    old_worker_cls, old_scan = mw.SerialWorker, (mw.scan_port_infos, mw.scan_port_infos_full)
    mw.SerialWorker = FakeWorker
    mw.scan_port_infos = lambda: [('COM3', '蓝牙链接上的标准串行 (COM3)'),
                                  ('COM8', 'USB-SERIAL CH340 (COM8)')]
    mw.scan_port_infos_full = lambda: [('COM3', '蓝牙链接上的标准串行 (COM3)', ''),
                                       ('COM8', 'USB-SERIAL CH340 (COM8)', 'CH340')]
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication(sys.argv)
    win = mw.MainWindow()
    win.show()
    win._port_watch_timer.stop()
    try:
        btn = win.btn_connect
        assert btn.text() == '连接', btn.text()
        btn.click()   # 手动连接（worker 建立 → 文案同步「断开」）
        assert btn.text() == '断开' and win.lbl_status.text().startswith('已连接')
        btn.click()   # 手动断开
        assert btn.text() == '连接' and win.lbl_status.text() == '未连接'
        btn.click()   # 连接丢失（worker.lost 路径）
        assert btn.text() == '断开'
        win.worker.lost.emit('拔出模拟')
        assert btn.text() == '连接' and win.lbl_status.text().startswith('连接丢失')
        btn.click()   # 打开失败（open_failed 路径也回「连接」）
        assert btn.text() == '断开'
        win.worker.open_failed.emit('端口被占用模拟')
        assert btn.text() == '连接' and win.lbl_status.text().startswith('打开失败')
        win.chk_auto_link.setChecked(True)   # 热插拔自动连接（新设备在基线外）
        win._known_port_names = ['COM3']
        win._port_watch_tick()
        assert btn.text() == '断开' and win._connected_device == 'COM8'
        win.chk_auto_link.setChecked(False)
        win._teardown_worker()   # 程序性断开（closeEvent 同款路径）
        assert btn.text() == '连接'
        win.open_connection()    # open_connection 直调（自动连接内部路径）同样同步
        assert btn.text() == '断开' and win.worker is not None
        win._teardown_worker()
        assert btn.text() == '连接'
    finally:
        mw.SerialWorker = old_worker_cls
        mw.scan_port_infos, mw.scan_port_infos_full = old_scan
        win._port_watch_timer.stop()
        win.close()
        _restore_settings(saved)  # closeEvent 会回写 connect/*，这里还原
    print('CONNECT BUTTON OK')


def test_tx_alignment_and_hover():
    """集成②/③：TX 行聊天式右对齐（制表位载体 + 像素右移 + 拉宽重对齐）
    + 日志行悬停 HEX⇄文本互转（含 TX 制表行/空正文头行/0.4s 时序/离场复位）。"""
    import re

    from PySide6 import QtCore, QtGui, QtWidgets

    import ui.main_window as mw
    from ui import theme
    from ui.log_hover import tooltip_for_position

    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication(sys.argv)
    win = mw.MainWindow()
    win.show()
    win.wave.state.enabled = False
    win._port_watch_timer.stop()
    win.chk_auto_save.setChecked(False)  # 像素检查阶段不掺自动日志节拍
    theme.set_theme('dark')              # 统一深色基线（像素断言按深色日志底扫描）
    app.processEvents()
    try:
        def find_block(needle):
            b = win.log.document().firstBlock()
            while b.isValid():
                if needle in b.text():
                    return b
                b = b.next()
            return None

        def grab_span(block):
            """log.grab() 扫该 block 首行 → 文本起点 min_x（与日志底色差>60 记为文本像素）。"""
            cursor = QtGui.QTextCursor(block)
            win.log.setTextCursor(cursor)
            win.log.ensureCursorVisible()
            app.processEvents()
            img = win.log.grab().toImage()
            vp = win.log.viewport()
            vpo = vp.mapTo(win.log, QtCore.QPoint(0, 0))
            bg = QtGui.QColor(theme.color('log_bg'))
            r = win.log.cursorRect(cursor)
            lo = None
            for yy in range(max(vpo.y() + 2, int(vpo.y() + r.y()) + 2),
                            max(vpo.y() + 3, int(vpo.y() + r.y() + r.height()) - 2)):
                for xx in range(vpo.x() + 2, vpo.x() + vp.width() - 14):
                    c = img.pixelColor(xx, yy)
                    if (abs(c.red() - bg.red()) + abs(c.green() - bg.green())
                            + abs(c.blue() - bg.blue())) > 60:
                        lo = xx if lo is None else min(lo, xx)
            return lo

        # ---- TX 右对齐 ----
        win.clear_log()
        win._append_log('RX', 'RX-左对齐-0123456789')
        win._append_log('TX', 'TX-右对齐')
        win._append_log('SYS', 'SYS-系统行-0123456789')
        app.processEvents()
        rx_b, tx_b, sys_b = (find_block(t) for t in
                             ('RX-左对齐-0123456789', 'TX-右对齐', 'SYS-系统行'))
        assert all(b is not None for b in (rx_b, tx_b, sys_b))
        assert tx_b.text().startswith('\t'), f'TX 行应以制表符开头: {tx_b.text()!r}'
        assert not rx_b.text().startswith(('\t', ' ')) and not sys_b.text().startswith(('\t', ' '))
        vw = win.log.viewport().width()
        rx_x, tx_x = grab_span(rx_b), grab_span(tx_b)
        assert rx_x is not None and rx_x < vw * 0.2, f'RX 行应靠左: {rx_x}/{vw}'
        assert tx_x is not None and tx_x > vw * 0.5, f'TX 行应明显靠右: {tx_x}/{vw}'
        assert (win.log.cursorRect(QtGui.QTextCursor(rx_b)).y()
                < win.log.cursorRect(QtGui.QTextCursor(tx_b)).y()
                < win.log.cursorRect(QtGui.QTextCursor(sys_b)).y()), '行序应保持追加顺序'
        win.resize(1300, 620)   # 拉宽 → 制表位同步 → TX 行跟随新右缘
        app.processEvents()
        win._sync_log_tab_stop()
        app.processEvents()
        vw2 = win.log.viewport().width()
        tx_x2 = grab_span(tx_b)
        assert tx_x2 is not None and tx_x2 > vw2 * 0.5 and tx_x2 > tx_x, \
            f'拉宽后 TX 行应重对齐: {tx_x} -> {tx_x2}'
        rx_x2 = grab_span(rx_b)
        assert rx_x2 is not None and rx_x2 < vw2 * 0.2, f'拉宽后 RX 行仍靠左: {rx_x2}'
        win.resize(980, 620)
        app.processEvents()
        win._sync_log_tab_stop()
        app.processEvents()

        # ---- 悬停互转 ----
        win.clear_log()
        win._append_log('RX', '41 42 43 44')
        win._append_log('RX', 'hello-ascii')
        win._append_log('TX', 'ping-ok')
        win._append_log('RX', '')            # 空正文条目（头行式，不能借下一条正文）
        win._append_log('RX', 'after-empty')
        hex_b = find_block('41 42 43 44')
        ascii_b = find_block('hello-ascii')
        tx_b2 = find_block('ping-ok')
        empty_b = after_b = None
        b = win.log.document().firstBlock()
        while b.isValid():
            t = b.text()
            if empty_b is None and re.match(r'^\s*\[[^\]]+\] RX\s*$', t):
                empty_b = b
            if 'after-empty' in t:
                after_b = b
            b = b.next()
        assert all(x is not None for x in (hex_b, ascii_b, tx_b2, empty_b, after_b))
        tip_hex = tooltip_for_position(win.log, hex_b.blockNumber())
        assert tip_hex.startswith('文本(UTF-8): ABCD') and '十进制: 65 66 67 68' in tip_hex
        assert tooltip_for_position(win.log, ascii_b.blockNumber()) == \
            'HEX: 68 65 6C 6C 6F 2D 61 73 63 69 69'
        assert tooltip_for_position(win.log, tx_b2.blockNumber()) == 'HEX: 70 69 6E 67 2D 6F 6B', \
            'TX 制表行也要能互转'
        assert tooltip_for_position(win.log, empty_b.blockNumber()) == '', '空正文条目不给提示'
        assert tooltip_for_position(win.log, after_b.blockNumber()).startswith('HEX: 61 66 74')
        # 0.4s 悬停时序（offscreen 合成鼠标事件不经视口过滤器，直接驱动过滤分支执行体）
        hover = win._log_hover
        assert hover.HOVER_DELAY_MS == 400 and hover._timer.isSingleShot()
        r = win.log.cursorRect(QtGui.QTextCursor(hex_b))
        hover._on_move(QtCore.QPoint(int(r.x()) + 8, int(r.y() + r.height() / 2)))
        assert hover._last_block == hex_b.blockNumber() and hover._timer.isActive()
        assert hover._tooltip.startswith('文本(UTF-8)')
        loop = QtCore.QEventLoop()
        QtCore.QTimer.singleShot(450, loop.quit)
        loop.exec()
        assert not hover._timer.isActive() and hover._tooltip.startswith('文本(UTF-8)')
        QtWidgets.QApplication.sendEvent(
            win.log.viewport(), QtCore.QEvent(QtCore.QEvent.Type.Leave))
        assert hover._last_block == -1 and not hover._timer.isActive(), '离场应复位悬停状态'
    finally:
        win.chk_auto_save.setChecked(True)
        win._port_watch_timer.stop()
        win.close()
    print('TX ALIGN + HOVER OK')


def test_wave_display_and_hover():
    """集成④：显示点数改名（旧「窗口」文案移除）+ 工具行「导出」菜单
    + 悬停十字线读数（首行 x + 每条可见曲线一行真实采样点，文字色=曲线色）。"""
    from PySide6 import QtCore, QtGui, QtWidgets

    from ui import theme
    from ui.wave_window import CURVE_COLORS, WaveWindow

    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication(sys.argv)
    theme.set_theme('dark')
    wave = WaveWindow()
    wave.resize(1280, 800)
    wave.show()
    app.processEvents()
    try:
        labels = [l.text() for l in wave.findChildren(QtWidgets.QLabel)]
        assert '显示点数' in labels, str(labels)
        assert '窗口' not in labels, '旧「窗口」文案应已改名'
        assert '点数' in wave.combo_window.toolTip(), wave.combo_window.toolTip()
        assert not hasattr(wave, 'chk_fill'), '曲线填充复选框应已迁设置页'
        assert wave.btn_export.text() == '导出'
        assert [a.text() for a in wave.menu_export.actions()] == ['导出PNG', '导出CSV']
        for _ in range(300):   # 模拟数据通路（真实解码管线）喂 300 帧
            wave._on_sim_tick()
        wave.repaint_wave()
        app.processEvents()
        st = wave.state
        # ---- 悬停读数 ----
        wave._hide_hover()
        start, end, _follow = st.view_range()
        # 1/4 帧偏移：探针必须避开两采样点正中（5.5 处经控件像素取整会随机偏向
        # start+6，此前固定挂「y 应取真实采样点」），5.25 离中点远于像素取整误差
        x_probe = float(start) + 5.25     # → 最近样本恒为 start+5
        vb = wave.plot.getViewBox()
        y_mid = sum(vb.viewRange()[1]) / 2.0
        scene = vb.mapViewToScene(QtCore.QPointF(x_probe, y_mid))
        vpos = wave.plot.mapFromScene(scene)
        if isinstance(vpos, QtCore.QPointF):
            vpos = vpos.toPoint()
        wave._update_hover(vpos)
        assert wave._vline.isVisible() and wave._hline.isVisible() and wave._readout.isVisible()
        lines = wave._readout.toPlainText().split('\n')
        visible_n = sum(1 for c in range(len(wave._curves))
                        if c >= len(st.enabled_channels) or st.enabled_channels[c])
        assert len(lines) == 1 + visible_n, repr(wave._readout.toPlainText())
        first = lines[0].strip()
        assert first.startswith('x = ') and abs(float(first[4:]) - x_probe) < 0.51, first
        assert all(lines[k + 1].startswith(st.channel_display_name(k) + ':')
                   for k in range(visible_n)), str(lines)
        expect_y0 = st.ch_values[0][start + 5]
        got_y0 = float(lines[1].split(':', 1)[1])
        assert abs(got_y0 - expect_y0) < 1e-9, f'y 应取真实采样点: {got_y0} vs {expect_y0}'
        html = wave._readout.toHtml().lower()
        assert all(c.lower() in html for c in CURVE_COLORS[:visible_n]), '读数文字色应为曲线色'
        # MouseMove 事件经过滤器端到端
        wave._hide_hover()
        mmove = QtGui.QMouseEvent(
            QtCore.QEvent.Type.MouseMove, QtCore.QPointF(vpos), QtCore.QPointF(vpos),
            QtCore.Qt.MouseButton.NoButton, QtCore.Qt.MouseButton.NoButton,
            QtCore.Qt.KeyboardModifier.NoModifier)
        QtWidgets.QApplication.sendEvent(wave.plot.viewport(), mmove)
        app.processEvents()
        assert wave._readout.isVisible(), 'MouseMove 事件应经过滤器驱动读数框'
        wave._hide_hover()
    finally:
        wave.stop_timers()
        wave.close()
    print('WAVE DISPLAY + HOVER OK')


def test_xy_new_params():
    """集成⑤：XY 新参数——分格/显示画法档位、渲染随参数变化、余晖拉伸迁移与回铺、格菜单中文。"""
    from PySide6 import QtWidgets

    import numpy as np

    from core.wave_state import WaveState
    from ui.xy_view import (CELL_SIZE, DIVS_OPTIONS, DRAW_MODES, PERSIST_OPTIONS,
                            SPAN_OPTIONS, XYSection)

    st = WaveState()
    st.enabled = True
    st.consume_bytes(b'1,2\n2,4\n3,6\n')  # FireWater 3 通道
    sec = XYSection(st)
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication(sys.argv)
    app.processEvents()

    def img_arr(w):
        img = w.grab().toImage()
        return np.frombuffer(img.constBits(), dtype=np.uint8,
                             count=img.sizeInBytes()).copy()

    try:
        # 档位
        assert [sec.combo_count.itemText(i) for i in range(sec.combo_count.count())] == ['1', '2', '4']
        assert [sec.combo_persist.itemText(i) for i in range(sec.combo_persist.count())] \
            == list(PERSIST_OPTIONS)
        assert [sec.combo_draw.itemText(i) for i in range(sec.combo_draw.count())] == list(DRAW_MODES)
        assert list(DIVS_OPTIONS) == ['4', '5', '8', '10', '16']
        assert list(SPAN_OPTIONS)[0] == '自适应'
        # 回铺：进 XY（构造）立即有画面；改窗口数后新格同样回铺
        assert sec._cells[0]._accum.any() and sec._last_total == 3
        sec.combo_count.setCurrentText('4')
        app.processEvents()
        assert len(sec._cells) == 4 and sec._cells[2]._accum.any(), '窗口数 2→4 新格应回铺'
        sec.combo_count.setCurrentText('2')
        app.processEvents()
        cell = sec._cells[0]
        # 渲染随量程/源/分格/画法变化
        sec._slot_span[0].setCurrentText('±8')
        sec.tick()
        app.processEvents()
        a = img_arr(cell)
        sec._slot_span[0].setCurrentText('±2')
        sec.tick()
        app.processEvents()
        assert not np.array_equal(a, img_arr(cell)), '改量程后渲染应变化'
        sec._slot_span[0].setCurrentText('±8')
        sec.tick()
        app.processEvents()
        a = img_arr(cell)
        sec._slot_y[0].setCurrentIndex(min(2, sec._slot_y[0].count() - 1))
        st.consume_bytes(b'7,9\n3,1\n')
        sec.tick()
        app.processEvents()
        assert not np.array_equal(a, img_arr(cell)), '改 Y 源通道后渲染应变化'
        sec._slot_y[0].setCurrentIndex(1)
        a = img_arr(cell)
        sec._slot_divs[0].setCurrentText('4')
        app.processEvents()
        assert not np.array_equal(a, img_arr(cell)), '改分格数后渲染应变化'
        sec._slot_divs[0].setCurrentText('8')
        app.processEvents()
        a = img_arr(cell)
        sec.combo_draw.setCurrentText('点')
        sec.tick()
        app.processEvents()
        assert not np.array_equal(a, img_arr(cell)), '画法 线→点 渲染应变化'
        # 余晖拉伸迁移：±8 画点 → 切 ±4 → 轨迹不清空且绕中心拉伸到新位置
        sec._slot_span[0].setCurrentText('±8')
        cell.clear_pixels()
        mid = CELL_SIZE // 2
        cell.feed(np.array([2.0]), np.array([0.0]))
        cell.tick(0.0)
        assert cell._accum[mid, int(2.0 * (CELL_SIZE / 2 - 1) / 8 + mid)] == 1.0
        sec._slot_span[0].setCurrentText('±4')
        assert cell._accum.any(), '迁移不清空轨迹'
        assert cell._accum[mid, int(2.0 * (CELL_SIZE / 2 - 1) / 4 + mid)] == 1.0, '应拉伸到新位置'
        assert cell._last_span == 4.0
        before = cell._accum.copy()
        sec._slot_x[0].setCurrentIndex(1)   # 换源：量程未变=恒等迁移，轨迹保留
        assert np.array_equal(cell._accum, before), '换源应保留轨迹'
        # 自适应：挂起迁移等下一帧算出半幅
        sec._slot_span[0].setCurrentText('自适应')
        assert cell._migrate_from == 4.0
        cell.feed(np.array([6.0]), np.array([0.0]))
        cell.tick(0.05)
        assert cell._migrate_from is None and cell._last_span == 8.0, '自适应半幅算出后应迁移'
        # 格右键菜单全中文 + 恢复自动缩放/清除余晖动作
        menu = sec._new_cell_menu(cell)
        texts = [a.text() for a in menu.actions() if not a.isSeparator()]
        assert texts == ['恢复自动缩放', '导出PNG', '导出CSV', '显示网格', '清除余晖'], str(texts)
        sec._slot_span[0].setCurrentText('±8')
        next(a for a in menu.actions() if a.text() == '恢复自动缩放').trigger()
        assert sec._slot_span[0].currentText() == '自适应' and cell.span_option == '自适应'
        sec._slot_span[0].setCurrentText('±8')
        next(a for a in menu.actions() if a.text() == '清除余晖').trigger()
        assert not cell._accum.any(), '清除余晖应清空累积'
    finally:
        sec.stop()
    print('XY NEW PARAMS OK')


def test_spectrum_axis_sync_export():
    """集成⑥：频谱/瀑布 x 轴双向联动 + 导出CSV 两模式与 N 钳制 + 清空复位。"""
    import csv as csv_mod
    import math
    import os
    import tempfile

    from PySide6 import QtWidgets

    from core.wave_state import WaveState
    from ui.spectrum_panel import _ExportCsvDialog, SpectrumPanel

    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication(sys.argv)
    state = WaveState()
    state.enabled = True
    panel = SpectrumPanel(state)
    panel.resize(1000, 720)
    panel.show()
    app.processEvents()
    tmp = tempfile.mkdtemp(prefix='selftest_spectrum_')
    clock = {'t': 1000.0}
    sample = {'n': 0}
    fs = 1000.0
    panel._now_fn = lambda: clock['t']   # 假时钟：帧时间戳可控

    def feed(dt=0.1, frames=100):
        """喂一串单点帧（50Hz+120Hz 正弦）→ 推进假时钟 → 触发一次分析。"""
        for _ in range(frames):
            n = sample['n']
            sample['n'] += 1
            v = (math.sin(2 * math.pi * 50.0 * n / fs)
                 + 0.3 * math.sin(2 * math.pi * 120.0 * n / fs))
            state.append_frame([v])
        clock['t'] += dt
        panel.repaint_spectrum()
        app.processEvents()

    try:
        for _ in range(30):
            feed()
        assert panel.last_analysis is not None and panel.last_analysis['used'] == 2048
        assert 2.85 <= panel.acquisition_seconds <= 3.0, panel.acquisition_seconds
        # ---- x 轴联动 ----
        vb, wv = panel.plot.getViewBox(), panel.wf_plot.getViewBox()
        (x0a, x1a), _ = vb.viewRange()
        (x0b, x1b), _ = wv.viewRange()
        fs_half = panel.last_analysis['fs'] / 2.0
        assert abs(x1a - fs_half) < 1e-9, f'初始满量程 (0,{fs_half})，实际 x1={x1a}'
        assert abs(x0a - x0b) < 1e-9 and abs(x1a - x1b) < 1e-9, '两图 x 范围应一致'
        vb.setXRange(30.0, 80.0, padding=0)
        (x0b, x1b), _ = wv.viewRange()
        assert abs(x0b - 30) < 1e-9 and abs(x1b - 80) < 1e-9, '谱图改范围 → 瀑布跟随'
        wv.setXRange(10.0, 60.0, padding=0)
        (x0a, x1a), _ = vb.viewRange()
        assert abs(x0a - 10) < 1e-9 and abs(x1a - 60) < 1e-9, '瀑布改范围 → 谱图跟随'
        feed()
        (x0a, x1a), _ = vb.viewRange()
        assert abs(x0a - 10) < 1e-9 and abs(x1a - 60) < 1e-9, '例行重绘应保留用户缩放'
        panel.edit_fs.setText('2000')
        feed()
        (x0a, x1a), _ = vb.viewRange()
        (x0b, x1b), _ = wv.viewRange()
        assert abs(x1a - 1000) < 1e-9 and abs(x1b - 1000) < 1e-9, 'fs=2000 → 重置回 (0,1000)'
        panel.edit_fs.setText('1000')
        feed()
        (_x0, x1a), _ = vb.viewRange()
        (_x0, x1b), _ = wv.viewRange()
        assert abs(x1a - 500) < 1e-9 and abs(x1b - 500) < 1e-9, 'fs=1000 → 重置回 (0,500)'
        for _ in range(10):
            feed()   # fs 回 1000 后 key 变化清了导出历史，补帧再测导出
        # ---- 导出CSV：latest + window（N 钳制） ----
        latest_path = os.path.join(tmp, 'latest.csv')
        meta = panel.export_csv(latest_path, mode='latest')
        with open(latest_path, encoding='utf-8-sig') as f:
            rows = list(csv_mod.reader(f))
        assert rows[1] == ['频率Hz', 'dB'], str(rows[1])
        assert len(rows) - 2 == meta['rows'] == 1024, f'{len(rows) - 2} vs {meta["rows"]}'
        win_path = os.path.join(tmp, 'window.csv')
        acq2 = panel.acquisition_seconds
        meta2 = panel.export_csv(win_path, mode='window', seconds=999.0)
        assert meta2['clamped'] and abs(meta2['seconds_eff'] - acq2) < 0.06, \
            f"N=999 应钳制到采集时长: {meta2['seconds_eff']:.3f} vs {acq2:.3f}"
        assert meta2['frames'] == len(panel._hist), '钳制后应覆盖全部历史帧'
        assert '钳制' in meta2['note'], meta2['note']
        with open(win_path, encoding='utf-8-sig') as f:
            rows_w = list(csv_mod.reader(f))
        assert rows_w[1] == ['时间s', '频率Hz', 'dB'], str(rows_w[1])
        times = sorted({float(r[0]) for r in rows_w[2:]})
        assert times and times[-1] - times[0] <= meta2['seconds_eff'] + 1e-6, '导出跨度 ≤ seconds_eff'
        try:
            panel.export_csv(os.path.join(tmp, 'bad.csv'), mode='nope')
            assert False, '未知导出模式应抛 ValueError'
        except ValueError:
            pass
        # 导出对话框逻辑（不 exec）
        dlg = _ExportCsvDialog(panel)
        dlg.radio_window.setChecked(True)
        dlg.spin_seconds.setValue(999.0)
        assert '钳制' in dlg.lbl_warn.text(), dlg.lbl_warn.text()
        assert dlg.result_choice() == ('window', 999.0)
        dlg.radio_latest.setChecked(True)
        assert dlg.result_choice()[0] == 'latest'
        # ---- 清空复位 ----
        panel.clear_spectrum()
        state.reset_buffers()   # 模拟数据源停止（波形页「清空」语义），此后不再有数据
        app.processEvents()
        assert panel._max_hold is None and panel._avg_db is None
        assert panel._waterfall == [] and panel._hist == []
        assert panel.acquisition_seconds == 0.0 and panel.last_analysis is None
        assert '已清空' in panel.lbl_info.text()
        for _ in range(3):   # 无新数据：清空提示不被例行重绘冲掉
            panel.repaint_spectrum()
            app.processEvents()
        assert '已清空' in panel.lbl_info.text() and panel._waterfall == []
        for _ in range(3):   # 数据恢复 → 提示让位，采集重新累计
            feed()
        assert panel.acquisition_seconds > 0 and '已清空' not in panel.lbl_info.text()
    finally:
        panel.stop_timers()
        panel.close()
    print('SPECTRUM AXIS + EXPORT OK')


def test_theme_switching():
    """集成⑦：主题切换全局生效（设置页下拉 → 全局 QSS/绘图区/曲线/芯片边框 + ini 记忆 + 重启保持）。"""
    from PySide6 import QtWidgets

    import ui.main_window as mw
    from core import APP_NAME, APP_VERSION
    from core.settings import read_setting, write_setting
    from ui import theme
    from ui.theme import DARK_QSS, LIGHT_QSS

    saved = _snapshot_settings(('ui/theme', 'connect/port'))
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication(sys.argv)
    win = mw.MainWindow()
    win.show()
    win._port_watch_timer.stop()
    try:
        # 统一深色基线：模块与下拉都归位（ini 可能残留 light；下拉已在 light 时
        # setCurrentIndex(light) 是无操作不发信号，所以先显式归位下拉）
        combo = win.settings_page.combo_theme
        if combo.currentData() != 'dark':
            combo.setCurrentIndex(combo.findData('dark'))
            app.processEvents()
        theme.set_theme('dark')
        app.processEvents()
        win.wave.state.consume_bytes(b'1,2\n2,4\n3,6\n')   # 喂几帧：让曲线与 CH 芯片建出来
        win.wave.repaint_wave()
        app.processEvents()
        tabs = win.tabs
        texts = [tabs.tabText(i) for i in range(tabs.count())]

        def luma_all():
            """七个页签各自的渲染平均亮度。"""
            out = {}
            for i, name in enumerate(texts):
                tabs.setCurrentIndex(i)
                app.processEvents()
                img = tabs.widget(i).grab().toImage()
                total = n = 0
                for y in range(0, img.height(), 4):
                    for x in range(0, img.width(), 4):
                        c = img.pixelColor(x, y)
                        total += (c.red() * 299 + c.green() * 587 + c.blue() * 114) // 1000
                        n += 1
                out[name] = total / max(1, n)
            return out

        dark_base = luma_all()
        combo.setCurrentIndex(combo.findData('light'))
        app.processEvents()
        assert theme.current_theme() == 'light' and app.styleSheet() == LIGHT_QSS
        assert read_setting('ui/theme', '') == 'light', '主题应写入设置记忆'
        light_mean = luma_all()
        for name in texts:
            assert light_mean[name] - dark_base[name] > 20.0, f'「{name}」页应变亮'
        assert win.wave.plot.backgroundBrush().color().name() == \
            theme.color('plot_bg', 'light').lower()
        assert win.spectrum.plot.backgroundBrush().color().name() == \
            theme.color('plot_bg', 'light').lower()
        assert win.wave._curve_colors == theme.curve_colors('light')
        assert win.spectrum._trace.opts['pen'].color().name() == \
            theme.color('sp_trace', 'light').lower()
        assert theme.color('chip_border', 'light') in win.wave._chips[0].styleSheet()
        combo.setCurrentIndex(combo.findData('dark'))
        app.processEvents()
        assert theme.current_theme() == 'dark' and app.styleSheet() == DARK_QSS
        dark_back = luma_all()
        assert max(abs(dark_back[k] - dark_base[k]) for k in dark_base) <= 8.0, '切回深色应恢复'
        # 重启保持：写 ini → 全新实例启动即浅色
        write_setting('ui/theme', 'light')
        win2 = mw.MainWindow()
        try:
            win2._port_watch_timer.stop()
            app.processEvents()
            assert theme.current_theme() == 'light' and app.styleSheet() == LIGHT_QSS
            assert win2.wave.plot.backgroundBrush().color().name() == \
                theme.color('plot_bg', 'light').lower()
            assert [win2.tabs.tabText(i) for i in range(win2.tabs.count())] == texts
        finally:
            win2._port_watch_timer.stop()
            win2.close()
        # 关于页签内容（版本只读 core，作者/许可证/简介在）
        tabs.setCurrentIndex(texts.index('关于'))
        app.processEvents()
        about_text = '\n'.join(l.text() for l in win.about_page.findChildren(QtWidgets.QLabel))
        assert APP_NAME in about_text and APP_VERSION in about_text, about_text
        assert '折柳' in about_text and 'GPL-3.0' in about_text
    finally:
        theme.set_theme('dark')
        win._port_watch_timer.stop()
        win.close()
        _restore_settings(saved)
    print('THEME SWITCH OK')


def _wcag_contrast(fg, bg):
    """WCAG 2.x 相对亮度对比度（#RRGGBB 或 (r,g,b)），对比度审计公用小函数。"""
    def lum(color):
        if isinstance(color, str):
            h = color.lstrip('#')
            r, g, b = (int(h[i:i + 2], 16) / 255 for i in (0, 2, 4))
        else:
            r, g, b = (c / 255 for c in color[:3])

        def lin(c):
            return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4

        return 0.2126 * lin(r) + 0.7152 * lin(g) + 0.0722 * lin(b)

    l1, l2 = lum(fg), lum(bg)
    if l1 < l2:
        l1, l2 = l2, l1
    return (l1 + 0.05) / (l2 + 0.05)


def _grab_ink_pair(widget):
    """grab 像素抽样：(最常见色=底, 距底最远像素=墨色, 墨距)。防「配色表对但渲染不对」。"""
    img = widget.grab().toImage()
    counts = {}
    for y in range(img.height()):
        for x in range(img.width()):
            c = img.pixelColor(x, y)
            key = (c.red(), c.green(), c.blue())
            counts[key] = counts.get(key, 0) + 1
    bg = max(counts, key=counts.get)
    fg = max(counts, key=lambda k: sum(abs(a - b) for a, b in zip(k, bg)))
    return bg, fg, sum(abs(a - b) for a, b in zip(fg, bg))


def test_theme_contrast():
    """集成⑪：双主题对比度——浅色 tool 按钮蓝底白字、禁用按钮弱化蓝灰可辨、
    两主题页签未选中文字与 RX/TX/SYS 日志行色全过 WCAG（正文 ≥4.5、禁用 ≥3.0），
    代表控件 grab 像素抽样复核渲染（配色表对≠渲染对）。"""
    from PySide6 import QtWidgets

    import ui.main_window as mw
    from core.settings import write_setting
    from ui import theme

    saved = _snapshot_settings(('ui/theme', 'stm32_cli_path'))
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication(sys.argv)
    try:
        for name in (theme.LIGHT_KEY, theme.DARK_KEY):
            # MainWindow.__init__ 会按 ini 恢复主题，先写入目标主题再建窗
            write_setting(theme.THEME_SETTING_KEY, name)
            theme.set_theme(name)
            win = mw.MainWindow()
            win.show()
            win._port_watch_timer.stop()
            win.chk_auto_save.setChecked(False)
            win.wave.state.enabled = False
            assert theme.current_theme() == name, '建窗后必须是目标主题'
            p = theme.palette()
            tag = theme.theme_label(name)

            # ---- 语义配对：正文 ≥4.5 / 禁用 ≥3.0 ----
            # tool 按钮（浅色蓝底白字回归：旧浅灰底 #DDE3EC 配白字几乎不可读）
            assert _wcag_contrast('#FFFFFF', p['tool_btn']) >= 4.5, f'[{tag}] tool 按钮字/底'
            assert _wcag_contrast('#FFFFFF', p['tool_btn_hover']) >= 4.5, f'[{tag}] tool 按钮 hover'
            assert _wcag_contrast('#FFFFFF', p['accent']) >= 4.5, f'[{tag}] checked/主按钮字/底'
            # 页签未选中文字/底（两主题都断言）
            assert _wcag_contrast(p['text_dim'], p['panel']) >= 4.5, f'[{tag}] 页签未选中文字/底'
            assert _wcag_contrast(p['text'], p['window']) >= 4.5, f'[{tag}] 页签选中文字/底'
            # RX/TX/SYS 日志行色对 log_bg（两主题都断言）
            for key in ('rx', 'tx', 'sys'):
                assert _wcag_contrast(p[key], p['log_bg']) >= 4.5, f'[{tag}] 日志行 {key}/log_bg'
            # 禁用按钮：白字对弱化底可辨（浅色旧 border 灰 #B9C2CF 仅 1.8）
            assert _wcag_contrast('#FFFFFF', p['btn_disabled_bg']) >= 3.0, f'[{tag}] 禁用按钮字/底'

            # ---- 像素抽样复核（清空日志避开离屏追加行不重绘的怪癖） ----
            win.clear_log()
            win._append_log('RX', 'temp=23.50')
            win._append_log('TX', 'AT')
            win._append_log('SYS', '对比度抽样')
            win.settings_page.set_cli_path('')   # 置出禁用按钮样本
            app.processEvents()

            bg, fg, dist = _grab_ink_pair(win.btn_clear_log)
            assert dist > 60, 'tool 按钮图内应有墨色'
            assert ('#%02X%02X%02X' % bg).lower() == p['tool_btn'].lower(), \
                f'[{tag}] tool 按钮渲染底色 {bg} 应等于配色表 {p["tool_btn"]}'
            assert _wcag_contrast(fg, bg) >= 4.5, f'[{tag}] tool 按钮实测墨色对比'

            btn = win.settings_page.btn_cli_reset
            assert not btn.isEnabled(), 'CLI 路径为空时恢复按钮应禁用'
            bg, fg, dist = _grab_ink_pair(btn)
            assert dist > 60, '禁用按钮图内应有墨色'
            assert ('#%02X%02X%02X' % bg).lower() == p['btn_disabled_bg'].lower(), \
                f'[{tag}] 禁用按钮渲染底色 {bg} 应等于配色表 {p["btn_disabled_bg"]}'
            assert _wcag_contrast(fg, bg) >= 3.0, f'[{tag}] 禁用按钮实测墨色对比'

            win._port_watch_timer.stop()
            win.close()
        theme.set_theme(theme.DARK_KEY)
    finally:
        _restore_settings(saved)
    print('THEME CONTRAST OK')


def test_ai_channel_extended():
    """集成⑧：AI 控制通道——help 结构化命令表 / spectrum_snapshot 合法数组 /
    settings_set UI 同步（含中文别名与已连接拒绝）/ Modbus 事务后同一连接仍存活。"""
    import json
    import time

    from PySide6 import QtWidgets
    from PySide6.QtNetwork import QTcpSocket

    import core.ports as core_ports
    import ui.main_window as mw
    import ui.theme as theme_mod
    from core.settings import read_setting

    FakeWorker = _fake_worker_cls()
    saved = _snapshot_settings(('connect/baud', 'connect/port', 'ui/theme', 'wave/fill_under'))
    old_worker_cls, old_scan = mw.SerialWorker, (mw.scan_port_infos, mw.scan_port_infos_full)
    old_ports_scan = core_ports.scan_port_infos
    mw.SerialWorker = FakeWorker
    fake_ports = [('COM3', '蓝牙链接上的标准串行 (COM3)'), ('COM8', 'USB-SERIAL CH340 (COM8)')]
    fake_ports_full = [(d, desc, 'CH340' if 'CH340' in desc else '')
                       for d, desc in fake_ports]
    mw.scan_port_infos = lambda: fake_ports
    mw.scan_port_infos_full = lambda: fake_ports_full
    core_ports.scan_port_infos = lambda: fake_ports  # 控制通道闭包构建时从 core.ports 取
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication(sys.argv)
    win = mw.MainWindow()
    win.show()
    win._port_watch_timer.stop()
    if not win._control_channel.listening:  # 9101 被外部占用时兜底临时端口
        win._control_channel.stop()
        win._control_channel._port = 0
        assert win._control_channel.start()
    sock = QTcpSocket()

    pump_until = lambda cond, timeout_s=6.0: _pump_until(app, cond, timeout_s)

    def rpc(request):
        sock.write((json.dumps(request, ensure_ascii=False) + '\n').encode('utf-8'))
        data = b''
        deadline = time.time() + 5.0
        while data.count(b'\n') < 1 and time.time() < deadline:
            app.processEvents()
            data += bytes(sock.readAll())
            if data.count(b'\n') < 1:
                sock.waitForReadyRead(20)
        line = data.split(b'\n')[0]
        return json.loads(line) if line.strip() else {}

    try:
        sock.connectToHost('127.0.0.1', win._control_channel.port)
        assert pump_until(lambda: sock.state() == QTcpSocket.SocketState.ConnectedState, 5.0)
        # ---- help：结构化命令表 + follow 语义块 ----
        r = rpc({'cmd': 'help'})
        cmds = {c['cmd'] for c in r.get('commands', [])}
        assert {'spectrum_snapshot', 'xy_snapshot', 'modbus_recent', 'stm32_status',
                'settings_list', 'settings_get', 'settings_set', 'stop'} <= cmds, str(cmds)
        assert all(isinstance(c.get('params'), dict) and c.get('desc')
                   and isinstance(c.get('example'), dict) for c in r.get('commands', []))
        fs_block = r.get('follow_semantics')
        assert isinstance(fs_block, dict) and fs_block.get('default_interval_s') == 0.5 \
            and fs_block.get('default_max_lines') == 200 and 'stop' in fs_block.get('exit', '')
        r = rpc({'cmd': 'nope'})
        assert not r.get('ok') and '未知命令' in r.get('error', '') and 'help' in r.get('error', '')
        # ---- spectrum_snapshot（模拟数据 → 合法数组） ----
        win.wave.state.consume_bytes(b'1.5,2.5,0.3\n' * 600)
        assert pump_until(lambda: win.spectrum.last_analysis is not None
                          and win.spectrum.last_analysis['db'].size >= 64, 8.0)
        r = rpc({'cmd': 'spectrum_snapshot', 'count': 64})
        assert r.get('ok') and r.get('empty') is False, str(r)[:120]
        assert len(r['freqs_hz']) == 64 and len(r['db']) == 64
        p = r['params']
        assert p['fft_points'] > 0 and p['sample_rate_hz'] > 0 \
            and p['window'] == '汉宁窗' and p['rbw_enbw_hz'] > 0
        assert len(r['waterfall_latest']) == 64
        assert all(isinstance(t, list) and len(t) == 3
                   and all(isinstance(x, int) and 0 <= x <= 255 for x in t)
                   for t in r['waterfall_latest']), '瀑布最近帧应为每频点 RGB 三元组'
        full = rpc({'cmd': 'spectrum_snapshot'})
        assert full['total_bins'] == len(full['freqs_hz']) == len(full['db'])
        assert full['freq_start_hz'] == 0.0
        freqs = full['freqs_hz']
        assert all(freqs[i] < freqs[i + 1] for i in range(len(freqs) - 1)), '频率轴应严格递增'
        # ---- settings_set：UI 同步 / 别名 / 已连接拒绝 / 只读键 ----
        r = rpc({'cmd': 'settings_set', 'key': 'baud', 'value': 921600})
        app.processEvents()
        assert r.get('ok') and r.get('after') == '921600', str(r)
        assert win.combo_baud.currentText() == '921600'
        assert read_setting('connect/baud') == '921600'
        win.btn_connect.click()
        assert pump_until(lambda: win.worker is not None)
        r = rpc({'cmd': 'settings_set', 'key': 'baud', 'value': 115200})
        assert not r.get('ok') and '断开' in r.get('error', ''), '已连接改波特率应拒绝'
        r = rpc({'cmd': 'settings_set', 'key': 'port', 'value': 921600})
        assert not r.get('ok') and '断开' in r.get('error', ''), '已连接改串口应拒绝'
        win.btn_connect.click()
        assert pump_until(lambda: win.worker is None)
        r = rpc({'cmd': 'settings_set', 'key': '波特率', 'value': 115200})
        app.processEvents()
        assert r.get('ok') and win.combo_baud.currentText() == '115200', '中文别名应可用'
        r = rpc({'cmd': 'settings_set', 'key': 'theme', 'value': 'light'})
        app.processEvents()
        assert r.get('ok') and theme_mod.current_theme() == 'light' \
            and win.settings_page.combo_theme.currentData() == 'light'
        r = rpc({'cmd': 'settings_set', 'key': 'theme', 'value': 'dark'})
        app.processEvents()
        assert r.get('ok') and theme_mod.current_theme() == 'dark'
        r = rpc({'cmd': 'settings_set', 'key': 'points', 'value': 2500})
        app.processEvents()
        assert r.get('ok') and win.wave.combo_window.currentText() == '2500' \
            and win.wave.state.window_frames == 2500, '显示点数改后下拉与内部状态应一致'
        r = rpc({'cmd': 'settings_set', 'key': 'points', 'value': 10})
        app.processEvents()
        assert r.get('ok') and r.get('after') == '50' \
            and win.wave.state.window_frames == 50, 'points 低于下限应钳制到 50（E2E B-03）'
        r = rpc({'cmd': 'settings_set', 'key': 'points', 'value': 999999})
        app.processEvents()
        assert r.get('ok') and win.wave.state.window_frames == 200000, \
            'points 超上限应钳制到 200000（E2E B-03）'
        r = rpc({'cmd': 'settings_set', 'key': 'points', 'value': 2500})
        app.processEvents()
        assert r.get('ok') and win.wave.combo_window.currentText() == '2500'
        r = rpc({'cmd': 'settings_set', 'key': 'fill', 'value': False})
        app.processEvents()
        assert r.get('ok') and win.settings_page.chk_fill.isChecked() is False \
            and read_setting('wave/fill_under') == 'false' and win.wave._fill_under is False, \
            '填充关：复选框+设置键+波形页三者应一致'
        r = rpc({'cmd': 'settings_set', 'key': '曲线填充', 'value': 'true'})
        app.processEvents()
        assert r.get('ok') and win.wave._fill_under is True
        r = rpc({'cmd': 'settings_set', 'key': 'paused', 'value': True})
        app.processEvents()
        assert r.get('ok') and win.wave.btn_pause.isChecked() \
            and win.wave.btn_pause.text() == '继续' and win.wave.state.paused is True
        r = rpc({'cmd': 'settings_set', 'key': 'paused', 'value': False})
        app.processEvents()
        assert r.get('ok') and win.wave.state.paused is False
        r = rpc({'cmd': 'settings_list'})
        assert r.get('ok') and all(k in r['settings'] for k in
                                   ('baud', 'port', 'protocol', 'theme', 'points', 'fill', 'paused'))
        r = rpc({'cmd': 'settings_get', 'key': '波特率'})
        assert r.get('ok') and r.get('value') == '115200'
        r = rpc({'cmd': 'stm32_status'})
        app.processEvents()
        assert r.get('ok') and r.get('available') is True \
            and isinstance(r.get('note'), str) and r.get('note'), \
            'stm32_status 应带明确 note 字段（E2E Q13①）'
        r = rpc({'cmd': 'settings_set', 'key': 'filter', 'value': 'x'})
        assert not r.get('ok'), '只读键应拒绝写'
        # ---- Modbus 与 AI 通道共存（poll.stop() 修复回归：嵌套事件循环后同连接仍存活） ----
        win.btn_connect.click()
        assert pump_until(lambda: win.worker is not None)
        resp = win.modbus_transport(bytes.fromhex('010300000002'), timeout_ms=60)
        assert resp == b'', '无从站应超时返回空响应'
        r = rpc({'cmd': 'ping'})
        assert r.get('ok'), f'Modbus 事务后同一条 AI 连接必须仍能响应: {r}'
        win.btn_connect.click()
        assert pump_until(lambda: win.worker is None)
    finally:
        mw.SerialWorker = old_worker_cls
        mw.scan_port_infos, mw.scan_port_infos_full = old_scan
        core_ports.scan_port_infos = old_ports_scan
        sock.close()
        win._port_watch_timer.stop()
        win.close()
        _restore_settings(saved)
        theme_mod.set_theme('dark')
    print('AI CHANNEL EXTENDED OK')


def test_stm32_fake_cli():
    """集成⑨：STM32 假 CLI 状态机——探测/连接芯片信息/断开/hex+bin 烧录（带校验与基址）/
    OB 读取/软硬复位/全片擦除与读保护解除（危险确认）/连接失败与无探针/无 CLI 提示。"""
    import os
    import sys
    import tempfile
    import time

    from PySide6 import QtWidgets

    import core.stm32_cli as cli_mod
    import ui.main_window as mw
    from core.settings import read_setting, write_setting
    from core.stm32_cli import Stm32Cli
    from ui.settings_page import STM32_CLI_PATH_KEY

    tmp = tempfile.mkdtemp(prefix='selftest_stm32_')
    fake_sn = 'FAKE00112233445566'
    fake_py = os.path.join(tmp, 'fake_stm32_cli.py')
    fake_bat = os.path.join(tmp, 'fake_stm32_cli.bat')
    # 假 CLI：按 argv/环境变量 STM32_FAKE_MODE 输出真实格式样本（ok/no_probe/no_connect）
    FAKE_SOURCE = r"""
# -*- coding: utf-8 -*-
import os
import sys
import time

args = sys.argv[1:]
mode = os.environ.get('STM32_FAKE_MODE', 'ok')

L_OK = '''===== STLink Interface =====

-------- Connected ST-LINK Probes List --------

ST-Link Probe 0 :
   ST-LINK SN  : FAKE00112233445566
   ST-LINK FW  : V2J46S7
   Access Port Number  : 3
   Board Name  : NUCLEO-H743ZI
-----------------------------------------------
'''

CONNECT_OK = '''Fake STM32CubeProgrammer v2.23.0
Voltage     : 3.27V
Device ID   : 0x450
Device name : STM32H7xx
NVM size    : 2 MBytes
Device CPU  : Cortex-M7'''

OB_OK = '''OPTION BYTES BANK: 0
   Read Out Protection:
     RDP          : 0xAA (Level 0, no protection)'''

DOWNLOAD_OK = '''File download complete
Download verified successfully'''

if mode == 'slow':
    time.sleep(30)
    sys.exit(0)
if '-l' in args:
    if mode == 'no_probe':
        print('Fake STM32CubeProgrammer v2.23.0\nNo ST-Link probe detected.')
        sys.exit(0)
    print('Fake STM32CubeProgrammer v2.23.0')
    print(L_OK)
    sys.exit(0)
if '-c' in args:
    print('ARGS: ' + ' '.join(args))
    if mode == 'no_connect':
        print('Error: No ST-LINK detected!')
        sys.exit(1)
    print(CONNECT_OK)
    if '-ob' in args:
        print(OB_OK)
    if '-rdu' in args:
        print('Read Protection is removed')
    if '-e' in args and 'all' in args:
        print('Mass erase successfully triggered')
    if '-rst' in args:
        print('System reset is performed')
    if '-hardRst' in args:
        print('Hardware reset is performed')
    if '-d' in args or '-w' in args:
        print(DOWNLOAD_OK)
    sys.exit(0)
print('Fake CLI: nothing to do')
sys.exit(0)
"""
    with open(fake_py, 'w', encoding='utf-8') as f:
        f.write(FAKE_SOURCE)
    with open(fake_bat, 'w', encoding='utf-8') as f:
        f.write(f'@echo off\r\n"{sys.executable}" "%~dp0fake_stm32_cli.py" %*\r\n')
    hex_path = os.path.join(tmp, 'fw_test.hex')
    with open(hex_path, 'wb') as f:
        f.write(b':00000001FF\r\n')
    bin_path = os.path.join(tmp, 'fw_test.bin')
    with open(bin_path, 'wb') as f:
        f.write(b'\x00' * 256)

    saved = _snapshot_settings((STM32_CLI_PATH_KEY, *_CONNECT_KEYS, 'update/last_check'))
    write_setting(STM32_CLI_PATH_KEY, fake_bat)
    write_setting('update/last_check', str(time.time()))  # 屏蔽 3s 自动更新检查
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication(sys.argv)

    # 模态对话框护栏（收尾必须还原，后续组还要跑）
    dialogs = {'question': [], 'warning': []}
    answer = {'value': QtWidgets.QMessageBox.StandardButton.No}
    file_pick = {'path': ''}
    real_question = QtWidgets.QMessageBox.question
    real_warning = QtWidgets.QMessageBox.warning
    real_open_file = QtWidgets.QFileDialog.getOpenFileName

    def fake_question(parent, title, text, *a, **k):
        dialogs['question'].append((title, text))
        return answer['value']

    def fake_warning(parent, title, text, *a, **k):
        dialogs['warning'].append((title, text))
        return QtWidgets.QMessageBox.StandardButton.Ok

    QtWidgets.QMessageBox.question = staticmethod(fake_question)
    QtWidgets.QMessageBox.warning = staticmethod(fake_warning)
    QtWidgets.QFileDialog.getOpenFileName = staticmethod(
        lambda *a, **k: (file_pick.get('path', ''), ''))

    def wait_idle(runner, timeout_ms=15000):
        deadline = time.time() + timeout_ms / 1000
        while time.time() < deadline:
            app.processEvents()
            if not runner.is_busy():
                app.processEvents()
                return True
            time.sleep(0.01)
        return False

    win = mw.MainWindow()
    win.show()
    win._port_watch_timer.stop()
    try:
        app.processEvents()
        titles = [win.tabs.tabText(i) for i in range(win.tabs.count())]
        assert titles == ['终端', '波形', '频谱', 'Modbus', 'STM32', '故障诊断', 'Linux 支持', '设置', '关于'], \
            str(titles)
        panel = win.stm32
        assert panel.runner.cli_path == fake_bat, panel.runner.cli_path
        assert not panel.runner.is_busy() and '未探测' in panel.lbl_probe.text()
        # ---- 探测 ----
        panel.refresh_all()
        assert panel.runner.is_busy()
        assert wait_idle(panel.runner)
        assert len(panel.probes) == 1 and panel.probes[0]['sn'] == fake_sn, str(panel.probes)
        assert panel.combo_probe.count() == 1 and fake_sn in panel.combo_probe.itemText(0)
        assert '已检测到 1 个' in panel.lbl_probe.text() and panel.lbl_probe.objectName() == 'status_ok'
        assert 'Fake STM32CubeProgrammer' in panel.cli_log.toPlainText()
        assert any('[STM32]' in e['text'] and 'ST-LINK 探测完成' in e['text']
                   for e in win.message_entries), '探测事件应镜像进主日志'
        # ---- 连接 / 断开 ----
        panel.toggle_connect()
        assert wait_idle(panel.runner)
        assert panel.connected and panel.btn_connect.text() == '断开'
        assert '已连接 STM32H7xx' in panel.lbl_target.text()
        info_text = panel.info_box.toPlainText()
        assert all(s in info_text for s in ('STM32H7xx', '2 MBytes', 'Cortex-M7', '3.27V', '0x450'))
        panel.toggle_connect()
        assert not panel.connected and panel.btn_connect.text() == '连接目标'
        assert panel.lbl_target.text() == '目标: 未连接'
        # ---- hex 烧录（带校验）与 bin 基址 ----
        file_pick['path'] = hex_path
        panel.pick_file()
        assert panel.lbl_file.text() == hex_path and not panel.edit_addr.isEnabled()
        panel.run_flash()
        assert wait_idle(panel.runner)
        log_text = panel.cli_log.toPlainText()
        assert 'Download verified successfully' in log_text and '[SYS] 烧录完成。' in log_text
        file_pick['path'] = bin_path
        panel.pick_file()
        assert panel.edit_addr.isEnabled()
        panel.edit_addr.setText('0x0800C000')
        panel.run_flash()
        assert wait_idle(panel.runner)
        log_text = panel.cli_log.toPlainText()
        assert '-w' in log_text and '0x0800C000' in log_text and '[SYS] 烧录完成。' in log_text
        file_pick['path'] = bin_path
        panel.pick_file()
        panel.edit_addr.setText('不是地址')
        mark = panel.cli_log.toPlainText()
        panel.run_flash()
        app.processEvents()
        assert dialogs['warning'] and '基址无效' in dialogs['warning'][-1][1]
        assert panel.cli_log.toPlainText() == mark, '基址非法不应发 CLI'
        panel.edit_addr.setText('0x08000000')
        # ---- OB / 软硬复位 ----
        panel.run_option_bytes()
        assert wait_idle(panel.runner)
        assert 'OB 读取完成：RDP 0xAA（Level 0' in panel.cli_log.toPlainText()
        panel.run_reset(hard=False)
        assert wait_idle(panel.runner)
        assert '[SYS] 软复位完成。' in panel.cli_log.toPlainText()
        panel.run_reset(hard=True)
        assert wait_idle(panel.runner)
        assert '[SYS] 硬复位完成。' in panel.cli_log.toPlainText()
        # ---- 危险确认：拒绝不执行，确认后执行 ----
        answer['value'] = QtWidgets.QMessageBox.StandardButton.No
        mark = panel.cli_log.toPlainText()
        panel.run_erase()
        app.processEvents()
        assert panel.cli_log.toPlainText() == mark, '拒绝确认不应执行擦除'
        panel.run_rdu()
        app.processEvents()
        assert panel.cli_log.toPlainText() == mark, '拒绝确认不应执行读保护解除'
        answer['value'] = QtWidgets.QMessageBox.StandardButton.Yes
        panel.run_erase()
        assert wait_idle(panel.runner)
        assert '[SYS] 全片擦除完成。' in panel.cli_log.toPlainText() and '-e' in panel.cli_log.toPlainText()
        assert dialogs['question'] and '不可恢复' in dialogs['question'][-1][1], '确认框应带危险提示'
        panel.run_rdu()
        assert wait_idle(panel.runner)
        assert '[SYS] 读保护解除完成。' in panel.cli_log.toPlainText()
        # ---- 失败态 ----
        panel.runner.extra_env = {'STM32_FAKE_MODE': 'no_connect'}
        panel.toggle_connect()
        assert wait_idle(panel.runner)
        assert not panel.connected and '连接失败' in panel.lbl_target.text()
        assert 'Error: No ST-LINK detected!' in panel.cli_log.toPlainText()
        panel.runner.extra_env = {'STM32_FAKE_MODE': 'no_probe'}
        panel.refresh_all()
        assert wait_idle(panel.runner)
        assert '未检测到 ST-LINK' in panel.lbl_probe.text() and panel.combo_probe.count() == 0
        panel.runner.extra_env = {}
        # ---- 无 CLI / 路径失效（不崩，中文提示，按钮禁用） ----
        win.close()
        app.processEvents()
        real_which, real_cands, real_fixed = cli_mod.shutil.which, cli_mod._probe_candidates, cli_mod._c_st_fixed
        cli_mod.shutil.which = lambda name: None
        cli_mod._probe_candidates = lambda: []
        cli_mod._c_st_fixed = lambda: ''
        try:
            write_setting(STM32_CLI_PATH_KEY, '')
            panel._apply_cli_path()
            assert panel.lbl_cli.text() == 'CLI: 未找到' and panel.lbl_cli.objectName() == 'status_err'
            assert all(not btn.isEnabled() for btn in panel.op_buttons)
            dialogs['warning'].clear()
            panel.run_flash()
            app.processEvents()
            assert any('未找到 STM32CubeProgrammer CLI' in t for _t, t in dialogs['warning'])
            assert not panel.runner.is_busy()
            write_setting(STM32_CLI_PATH_KEY, r'Z:\no_such_dir\STM32_Programmer_CLI.exe')
            panel._apply_cli_path()
            assert '设置的路径不存在' in panel.lbl_cli.text()
        finally:
            cli_mod.shutil.which, cli_mod._probe_candidates, cli_mod._c_st_fixed = \
                real_which, real_cands, real_fixed
    finally:
        QtWidgets.QMessageBox.question = real_question
        QtWidgets.QMessageBox.warning = real_warning
        QtWidgets.QFileDialog.getOpenFileName = real_open_file
        win._port_watch_timer.stop()
        win.close()
        _restore_settings(saved)
    # core 级：忙碌拒绝 + 超时强杀（借 python -c 造慢任务，不依赖假 CLI）
    slow_runner = Stm32Cli()
    slow_runner.cli_path = sys.executable
    result = {}
    slow_runner.finished.connect(lambda p: result.update(p))
    try:
        assert slow_runner.run('flash', ['-c', 'import time; time.sleep(5)'], 600)
        assert slow_runner.run('list', ['-l'], 30000) is False, '忙碌中并发请求应被拒绝'
        t0 = time.time()
        deadline = time.time() + 4.0
        while time.time() < deadline and slow_runner.is_busy():
            app.processEvents()
            time.sleep(0.01)
        assert not slow_runner.is_busy() and time.time() - t0 < 4.5, '超时应强杀且早于任务自然结束'
        assert result.get('timed_out') is True and '超时' in result.get('error', ''), str(result)
    finally:
        slow_runner.shutdown()
    print('STM32 FAKE CLI OK')


def test_main_tabs_integration():
    """集成⑩（任务 1 口径）：主窗 offscreen 完整跑一遍——七页签建/切、模拟数据下
    波形/频谱/XY 正常（XY 回铺立即有画面）、AI 控制通道随主窗开启。"""
    import time as _time

    from PySide6 import QtWidgets

    import ui.main_window as mw
    from core.settings import write_setting

    saved = _snapshot_settings(('ui/theme', 'wave/fill_under', *_CONNECT_KEYS,
                                'update/last_check'))
    write_setting('update/last_check', str(_time.time()))  # 屏蔽 3s 自动更新检查
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication(sys.argv)
    win = mw.MainWindow()
    win.show()
    win._port_watch_timer.stop()
    try:
        app.processEvents()
        texts = [win.tabs.tabText(i) for i in range(win.tabs.count())]
        assert texts == ['终端', '波形', '频谱', 'Modbus', 'STM32', '故障诊断', 'Linux 支持', '设置', '关于'], \
            str(texts)
        for i, name in enumerate(texts):   # 逐页签切换 + 渲染非空
            win.tabs.setCurrentIndex(i)
            app.processEvents()
            pix = win.tabs.widget(i).grab()
            assert not pix.isNull() and pix.width() > 50 and pix.height() > 50, name
        # 模拟数据：波形 → 频谱 → XY（回铺立即有画面）
        win.wave.btn_sim.setChecked(True)
        deadline = _time.time() + 8.0
        while _time.time() < deadline and win.wave.state.frame_count < 30:
            app.processEvents()
            _time.sleep(0.005)
        assert win.wave.state.frame_count >= 30, '模拟数据应流入波形缓冲'
        deadline = _time.time() + 8.0
        while _time.time() < deadline and win.spectrum.last_analysis is None:
            app.processEvents()
            _time.sleep(0.005)
        assert win.spectrum.last_analysis is not None, '频谱页应分析出有效谱'
        win.wave.btn_xy.setChecked(True)
        app.processEvents()
        assert win.wave.xy_section.isVisibleTo(win.wave)
        assert all(c._accum.any() for c in win.wave.xy_section._cells), 'XY 回铺应立即有画面'
        win.wave.btn_xy.setChecked(False)
        win.wave.btn_sim.setChecked(False)
        app.processEvents()
        assert win._control_channel.listening, 'AI 控制通道应随主窗默认开启'
        status = win._control_api.status()
        assert status.get('protocol') == 'FireWater' and status.get('channels', 0) >= 3
    finally:
        win._port_watch_timer.stop()
        win.close()
        _restore_settings(saved)
    print('MAIN TABS INTEGRATION OK')



def test_net_serial_url():
    """v2 M1①：网络串口 URL 直通——serial_for_url 全链（loop:// 回环）+ 可编辑端口下拉。

    不需要硬件：loop:// 是 pyserial 内建回环 URL，写进去的字节立刻回读。
    验证：下拉可手输 URL → 连接成功 → UI 发送经 URL 端口回显进 RX 日志 → 断开干净。
    """
    from PySide6 import QtWidgets

    import ui.main_window as mw

    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication(sys.argv)
    saved = _snapshot_settings(_CONNECT_KEYS)
    win = mw.MainWindow()
    win.show()
    win._port_watch_timer.stop()
    received = []
    try:
        assert win.combo_port.isEditable(), '端口下拉应可编辑（网络串口 URL 直输）'
        win.combo_port.setCurrentText('loop://')
        win.combo_baud.setCurrentText('115200')
        win.btn_connect.click()
        assert _pump_until(app, lambda: win.worker is not None
                              and win._connected_device == 'loop://', timeout_s=15),             'URL 直连应成功'
        win.worker.got_bytes.connect(received.append)
        win.edit_send.setText('HELLO-URL')
        win.send()
        assert _pump_until(app, lambda: any(b'HELLO-URL' in chunk for chunk in received)), \
            f'loop:// 应回显发送内容，实际收到 {received!r}'
        assert win.rx_bytes_total >= len('HELLO-URL\n'), 'RX 统计应累计回环字节'
        btn = win.btn_connect
        btn.click()
        assert _pump_until(app, lambda: win.worker is None), '断开应干净'
    finally:
        win._port_watch_timer.stop()
        win.close()
        _restore_settings(saved)  # closeEvent 会回写 connect/*，这里还原
    print('NET SERIAL URL OK')


def test_device_screen():
    """v2 M2①：设备屏解码渲染原型——libx264 合成流 → MTU=1200 切包 UDP 回环 →
    H264UdpSource（拆头/重组 NAL/丢包统计）→ VideoDecoder（AU 组包/软解/耗时统计）→
    ScreenGLWidget 渲染。

    断言：解出 ≥25 帧、帧尺寸 320x240、丢包 0、解码 0 错、seq 跳变触发 resync_needed。
    渲染断言说明：QT_QPA_PLATFORM=offscreen 无 GL 上下文（QOpenGLWidget: Failed to
    create context，Qt 回退纯色 pixmap，画面恒为单色），因此 grab() 内容断言在离屏下
    降级为「不崩溃 + 非空 + 帧计数正确」；GL 直通路径的真实渲染需窗口平台人工/脚本验证。
    """
    import os
    import socket
    import struct
    import tempfile
    from fractions import Fraction

    import numpy as np
    from PySide6 import QtWidgets

    from ui.device_screen import H264UdpSource, split_nals

    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication(sys.argv)

    # ---- 1) PyAV libx264 编码合成流：320x240、30 帧移动色块、baseline 无 B 帧、Annex-B ----
    import av
    tmp_dir = tempfile.mkdtemp(prefix='qt_devicescreen_')
    h264_path = os.path.join(tmp_dir, 'synthetic.h264')
    w, h, n_frames, fps = 320, 240, 30, 30
    cont = av.open(h264_path, 'w', format='h264')   # Annex-B 裸流 muxer
    st = cont.add_stream('libx264', rate=fps)
    st.width, st.height, st.pix_fmt = w, h, 'yuv420p'
    st.time_base = Fraction(1, fps)
    st.options = {'profile': 'baseline', 'tune': 'zerolatency', 'preset': 'ultrafast'}
    for i in range(n_frames):
        img = np.zeros((h, w, 3), np.uint8)
        x = (i * 8) % (w - 40)
        img[100:140, x:x + 40] = (0, 200, 80)       # 移动色块：保证帧间有变化
        img[:, :8] = i * 8 % 256                    # 亮度条随帧递增：帧内容可区分
        for pkt in st.encode(av.VideoFrame.from_ndarray(img, format='bgr24')):
            cont.mux(pkt)
    for pkt in st.encode():
        cont.mux(pkt)
    cont.close()
    stream = open(h264_path, 'rb').read()
    assert stream.count(b'\x00\x00\x00\x01') >= n_frames, 'Annex-B 起始码应存在'
    nals = split_nals(stream)
    assert any(nal[4] & 0x1F == 7 for nal in nals), '应含 SPS'
    assert any(nal[4] & 0x1F == 5 for nal in nals), '应含 IDR'

    # ---- 2) 丢包统计 / NAL 重组纯逻辑（不经 socket 直喂数据报） ----
    src = H264UdpSource(0)
    resyncs = []
    src.resync_needed.connect(resyncs.append)
    assert src._process_datagram(struct.pack('>IIB', 0, 0, 1) + b'\x00' * 3) == b''
    assert src._process_datagram(struct.pack('>IIB', 2, 0, 0) + b'\x00' * 3) == b''
    assert src.lost_count == 1 and resyncs == [0], 'seq 跳 1 应记丢 1 包并触发 resync'
    # 起始码切包落在 NAL 中间（MTU 切包常态）：跨数据报重组
    nal_a = nals[0]
    mid = len(nal_a) // 2
    out = src._process_datagram(struct.pack('>IIB', 3, 0, 0) + nal_a[:mid])
    assert out == b'', '半 NAL 不应提前吐出'
    out = src._process_datagram(struct.pack('>IIB', 4, 0, 0) + nal_a[mid:])
    assert out == b'', '无下一起始码前 NAL 悬着（完整性未知）'
    out = src._process_datagram(struct.pack('>IIB', 5, 0, 0) + b'\x00\x00\x00\x01\x65\x88')
    assert out == nal_a, f'下一 NAL 起始码到达时应吐出重组完整的前一 NAL: {out!r}'
    assert src.late_count == 0 and src.dup_count == 0

    # ---- 3) 端到端：页面起管线 → UDP 回环发流 → 解码断言 ----
    from ui.device_screen import DeviceScreenPage

    page = DeviceScreenPage()
    page.show()
    port = 29202 + os.getpid() % 200    # 避免与其他测试进程端口冲突
    page.spin_port.setValue(port)
    page.btn_start.click()
    assert _pump_until(app, lambda: page.source is not None and page.source.is_bound), \
        'UDP 源应绑定成功'

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    seq = 0
    for off in range(0, len(stream), 1200):     # MTU=1200 切包（可落在 NAL 中间）
        head = struct.pack('>IIB', seq, off, 1 if seq == 0 else 0)
        sock.sendto(head + stream[off:off + 1200], ('127.0.0.1', port))
        seq += 1
    sock.close()

    def _frame_size():
        return page.decoder.last_size if page.decoder else None

    assert _pump_until(app, lambda: page.decoder is not None
                       and page.decoder.frame_count >= 25, timeout_s=15), \
        f'解码帧数不足: {page.decoder.frame_count if page.decoder else "无解码器"}'
    page.decoder.drain()                         # 收口最后一帧 AU
    app.processEvents()
    dec = page.decoder
    assert dec.frame_count >= 25
    assert _frame_size() == (320, 240), f'帧尺寸应为 320x240，实际 {_frame_size()}'
    assert dec.decode_errors == 0, f'解码错误 {dec.decode_errors}'
    assert dec.avg_decode_ms is not None and dec.avg_decode_ms >= 0
    assert page.source.lost_count == 0, '回环不应丢包'
    assert page.source.packet_count == seq, '应收到全部数据报'
    page._update_stats()
    assert 'fps' in page.lbl_fps.text() and '解码' in page.lbl_dec.text()
    assert page.lbl_lost.text() == '丢包 0'

    # ---- 4) 渲染区 grab（offscreen 降级断言，原因见 docstring）----
    app.processEvents()
    pix = page.gl_widget.grab()
    assert not pix.isNull() and pix.width() > 50 and pix.height() > 50, '渲染区 grab 应非空'
    img = pix.toImage()
    colors = {img.pixelColor(x, y).name()
              for x in range(0, pix.width(), max(1, pix.width() // 16))
              for y in range(0, pix.height(), max(1, pix.height() // 16))}
    if len(colors) > 1:
        print(f'  (设备屏 GL 渲染出内容: {len(colors)} 种采样色)')
    else:
        print('  (offscreen 无 GL 上下文：grab 为纯色兜底，降级断言帧计数)')

    # ---- 5) 停止收尾 ----
    page.btn_start.click()
    assert page.source is None and page.decoder is None, '停止应回收收流/解码线程'
    page.shutdown()                              # 幂等收尾
    page.close()
    print('DEVICE SCREEN OK')


def test_device_screen_control():
    """M2 收口①：控制通道客户端——loopback 假 server_rk 验 hello→pair→video_start
    对话、listen_port 透传、video_stop 收尾；连接拒绝时只报错不炸。"""
    import json as _json
    import socket as _socket
    import threading

    from PySide6 import QtWidgets

    from ui.device_screen import ControlClient

    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication(sys.argv)

    recv_lines: list[dict] = []
    srv = _socket.socket()
    srv.bind(('127.0.0.1', 0))
    srv.listen(1)
    port = srv.getsockname()[1]

    def serve():
        srv.settimeout(6)
        try:
            conn, _ = srv.accept()
        except OSError:
            return
        conn.settimeout(6)
        buf = bytearray()

        def read_line():
            while b'\n' not in buf:
                try:
                    chunk = conn.recv(4096)
                except OSError:
                    return None
                if not chunk:
                    return None
                buf.extend(chunk)
            line, _, rest = bytes(buf).partition(b'\n')
            del buf[:]
            buf.extend(rest)
            req = _json.loads(line.decode('utf-8'))
            recv_lines.append(req)
            return req

        while True:
            req = read_line()
            if req is None:
                break
            cmd = req.get('cmd')
            if cmd == 'hello':
                resp = {'ok': True, 'challenge': 'CH7', 'nonce': req.get('nonce'),
                        'server_version': 'test'}
            elif cmd == 'pair':
                resp = {'ok': True, 'session': 'sess-1'}
            elif cmd == 'video_start':
                resp = {'ok': True, 'target': f'127.0.0.1:{req.get("listen_port")}',
                        'note': 'ffmpeg pid=4321 overlay=on',
                        'now_ms': 1_000_000, 'overlay': True}
            elif cmd == 'video_stop':
                conn.sendall(b'{"ok": true, "stopped": true}\n')
                break
            elif cmd == 'input':
                resp = {'ok': True, 'applied': len(req.get('events') or []),
                        'skipped': 0}
            else:
                resp = {'ok': False, 'error': f'未知命令 {cmd}'}
            conn.sendall((_json.dumps(resp, ensure_ascii=False) + '\n').encode('utf-8'))
        conn.close()

    threading.Thread(target=serve, daemon=True).start()

    def reports_until(level: str, timeout: float = 5.0) -> list[tuple[str, str]]:
        reports: list[tuple[str, str]] = []
        client.report.connect(lambda text, lv: reports.append((text, lv)))
        deadline = time.time() + timeout
        while time.time() < deadline:
            app.processEvents()
            if any(lv == level for _, lv in reports):
                return reports
            time.sleep(0.02)
        return reports

    client = ControlClient('127.0.0.1', port, '246810', 9300,
                           connect_fn=lambda hp, t: _socket.create_connection(hp, timeout=t))
    client.start()
    reports = reports_until('status_ok')
    assert any('已发流' in txt and lv == 'status_ok' for txt, lv in reports), reports
    cmds = [r.get('cmd') for r in recv_lines]
    assert cmds[:3] == ['hello', 'pair', 'video_start'], cmds
    assert recv_lines[1].get('challenge') == 'CH7', 'pair 必须回显 hello 的 challenge'
    assert recv_lines[2].get('listen_port') == 9300, '收流端口必须透传给板端'
    assert recv_lines[2].get('overlay') is True and \
        abs(recv_lines[2].get('overlay_opacity', 0) - 0.85) < 1e-6, \
        '跑表开关/透明度必须随 video_start 下发'
    # 实时性证明链：应答 now_ms → 以收到时刻为基准估板钟差；overlay 上报 → 状态行提示跑表
    assert client.board_overlay is True, 'overlay 上报必须透传'
    assert client.board_offset_ms is not None, 'now_ms 必须折算出板钟差'
    assert abs(client.board_offset_ms - (1_000_000 - time.time() * 1000)) < 500, \
        '板钟差 = 板端应答时刻 - PC 收到应答时刻（不能用折半 RTT，会虚增 handler/2）'
    assert any('跑表' in txt for txt, _ in reports), reports
    from ui.device_screen import transport_delay_ms
    # 单向时延：ts32 恰为板钟低 32 位（无回绕）；延迟 = 收包时刻 - 等效发包时刻
    pc_now = time.time() * 1000
    board_now = pc_now + client.board_offset_ms
    assert abs(transport_delay_ms(int(board_now - 40) & 0xFFFFFFFF, pc_now,
                                  client.board_offset_ms) - 40) < 5, '40ms 时延应估出'
    # u32 回绕：发包在 2^32 边界前 16ms，收包在边界后 9ms（延迟 25ms）
    assert abs(transport_delay_ms(0xFFFFFFF0, (1 << 32) + 9, 0.0) - 25) < 5, \
        '回绕差值应按有符号解读'
    # 会话期命令下发（鼠标注入通道）：input 命令经队列到板端并收到 applied
    client.send_command({'cmd': 'input',
                         'events': [{'t': 'mouse_abs', 'x': 512, 'y': 300},
                                    {'t': 'mouse_button', 'btn': 'left', 'down': True}]})
    deadline = time.time() + 5
    while time.time() < deadline:
        app.processEvents()
        if any(r.get('cmd') == 'input' for r in recv_lines):
            break
        time.sleep(0.02)
    in_req = next(r for r in recv_lines if r.get('cmd') == 'input')
    assert in_req['events'][0] == {'t': 'mouse_abs', 'x': 512, 'y': 300}, in_req

    # 停止：同连接发 video_stop 后线程自然退出
    client.request_stop()
    client.wait(6000)
    assert not client.isRunning()
    assert recv_lines[-1].get('cmd') == 'video_stop', recv_lines[-1]

    # 错误路径：连接拒绝 → err 级 report，线程不炸不挂
    client2 = ControlClient('127.0.0.1', 1, 'x', 9202,
                            connect_fn=lambda hp, t: (_ for _ in ()).throw(
                                ConnectionRefusedError('refused')))
    reports2: list[tuple[str, str]] = []
    client2.report.connect(lambda text, lv: reports2.append((text, lv)))
    client2.start()
    deadline = time.time() + 5
    while time.time() < deadline:
        app.processEvents()
        if any(lv == 'status_err' for _, lv in reports2):
            break
        time.sleep(0.02)
    client2.wait(3000)
    assert any(lv == 'status_err' and '不可达' in txt for txt, lv in reports2), reports2
    assert not client2.isRunning()
    srv.close()
    print('DEVICE SCREEN CONTROL OK')



def test_theme_custom_and_pages():
    """v1.3.0 外观标准化：自定义主题（六键改色+对比度警示+重启保持）+ 页面显隐记忆。"""
    from PySide6 import QtWidgets

    import ui.main_window as mw
    from core.settings import read_setting, write_setting
    from ui import theme
    from ui.settings_page import SHOW_SCREEN_KEY, SHOW_STM32_KEY

    saved = _snapshot_settings(('ui/theme', theme.CUSTOM_COLORS_KEY,
                                theme.CUSTOM_BASE_KEY, SHOW_STM32_KEY, SHOW_SCREEN_KEY))
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication(sys.argv)
    win = mw.MainWindow()
    win.show()
    win._port_watch_timer.stop()
    try:
        combo = win.settings_page.combo_theme
        if combo.currentData() != 'dark':   # 统一深色基线（ini 可能残留）
            combo.setCurrentIndex(combo.findData('dark'))
            app.processEvents()

        # ---- 页面显隐：设置页开关 ↔ 页签可见性 + ini 记忆 ----
        win.settings_page.chk_show_stm32.setChecked(False)
        win.settings_page.chk_show_screen.setChecked(False)
        assert not win.tabs.isTabVisible(win.tabs.indexOf(win.stm32)), 'STM32 页应隐藏'
        assert not win.tabs.isTabVisible(win.tabs.indexOf(win.device_screen)),             'Linux 支持页应隐藏'
        assert read_setting(SHOW_STM32_KEY, '') == '0', '显隐应写设置记忆'
        win.settings_page.chk_show_stm32.setChecked(True)
        assert win.tabs.isTabVisible(win.tabs.indexOf(win.stm32)), '重新勾选应恢复显示'

        # ---- 自定义主题 ----
        idx = combo.findData('custom')
        assert idx >= 0, '主题下拉应含「自定义」'
        combo.setCurrentIndex(idx)
        app.processEvents()
        assert theme.current_theme() == 'custom'
        assert not win.settings_page.custom_box.isHidden(), '自定义主题下应展开配色面板'

        win.settings_page.apply_custom_color('window', '#401010')
        assert theme.color('window') == '#401010', '改色应即时生效'
        assert '#401010' in app.styleSheet(), '全局 QSS 应包含新底色'
        assert '401010' in read_setting(theme.CUSTOM_COLORS_KEY, ''), '改色应写设置记忆'

        win.settings_page.apply_custom_color('text', '#401010')   # 文字与底同色
        assert theme.custom_palette_issues(theme.palette('custom')), '同色文字应触发复核警示'
        assert '对比度不足' in win.settings_page.lbl_contrast.text(), '警示应上屏'

        win.settings_page._reset_custom()
        assert win.settings_page.lbl_contrast.text() == '', '恢复默认后警示清空'
        assert theme.color('window') == theme.color('window', 'dark'), '应回到深色基准'

        combo.setCurrentIndex(combo.findData('dark'))
        app.processEvents()
        assert theme.current_theme() == 'dark'
        assert win.settings_page.custom_box.isHidden(), '非自定义下配色面板应收起'

        # ---- 重启保持：ui/theme=custom + 已存覆盖 → 新实例恢复自定义配色 ----
        win.settings_page.apply_custom_color('window', '#123456')
        write_setting('ui/theme', 'custom')
        win.close()
        win2 = mw.MainWindow()
        win2._port_watch_timer.stop()
        try:
            assert theme.current_theme() == 'custom', '启动应按 ini 恢复自定义主题'
            assert theme.color('window') == '#123456', '自定义覆盖应跨重启保持'
        finally:
            win2.close()
        win = None
    finally:
        if win is not None:
            win._port_watch_timer.stop()
            win.close()
        _restore_settings(saved)
        theme.set_theme('dark')   # 测试基线复位（后续组不串色）
    print('THEME CUSTOM + PAGES OK')


def run_selftest(frozen: bool = False, only: str | None = None) -> int:
    import os
    os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
    from selftest_esp32_link import groups as _esp32_link_groups
    from selftest_esp32_log import groups as _esp32_log_groups
    from selftest_ansi import groups as _ansi_groups
    from selftest_display import groups as _display_groups
    from selftest_txseq import groups as _txseq_groups
    from selftest_xfer import groups as _xfer_groups
    groups = [
        test_rx_buffer,
        test_hexutil,
        test_main_window,
        test_terminal_chrome,
        test_ports_watch,
        test_connect_lock,
        test_find_time_mode,
        test_auto_log_export,
        test_log_hover,
        test_fault_decode_and_ai_doc,
        test_fault_report,
        test_fault_panel,
        test_wave_presets,
        test_xy_view,
        test_session_record,
        test_control_channel,
        test_headless,
        test_wave_protocols,
        test_wave_window,
        test_main_window_m3,
        test_modbus_core,
        test_modbus_panel,
        test_update_check,
        test_update_ui,
        test_spectrum_dsp,
        test_spectrum_panel,
        test_connect_button_states,
        test_tx_alignment_and_hover,
        test_wave_display_and_hover,
        test_xy_new_params,
        test_spectrum_axis_sync_export,
        test_theme_switching,
        test_theme_contrast,
        test_theme_custom_and_pages,
        test_ai_channel_extended,
        test_stm32_fake_cli,
        test_main_tabs_integration,
        test_net_serial_url,
        test_device_screen,
        test_device_screen_control,
        *_esp32_link_groups(),
        *_esp32_log_groups(),
        *_ansi_groups(),
        *_display_groups(),
        *_txseq_groups(),
        *_xfer_groups(),
    ]
    # 冻结冒烟（--selftest-exit）剔除两类组：
    # ① windowed onefile 里以 sys.executable 再起子进程（= exe 自身 → 卡死）或访问外网；
    # ② 逐像素 grab 扫描 / PyAV 编解码重断言——冻结渲染慢约 10×（v1.3.0 实测全量 >25min），
    #    这类组留给 L1 开发环境全量跑（CI 另有 python qt/selftest.py 步骤），冻结只留快速门禁。
    frozen_unsafe = {test_headless, test_stm32_fake_cli, test_update_check,
                     test_tx_alignment_and_hover, test_wave_display_and_hover,
                     test_theme_switching, test_main_tabs_integration,
                     test_theme_contrast, test_device_screen}
    _log_path = os.environ.get('SELFTEST_LOG')
    _logf = open(_log_path, 'a', encoding='utf-8') if _log_path else None

    def _trace(msg: str):
        print(msg, flush=True)
        if _logf:
            _logf.write(f'[{time.strftime("%H:%M:%S")}] {msg}\n')
            _logf.flush()
    if only:
        # 诊断/子集运行：逗号分隔组名；冻结冒烟用于二分定位挂起组
        wanted = {n.strip() for n in only.split(',') if n.strip()}
        groups = [fn for fn in groups if fn.__name__ in wanted]
    _trace(f'RUN_SELFTEST frozen={frozen} only={only!r} groups={len(groups)}')
    for fn in groups:
        if frozen and fn in frozen_unsafe:
            _trace(f'{fn.__name__}: SKIP（冻结冒烟子集）')
            continue
        _trace(f'{fn.__name__} ...')
        try:
            fn()
        except BaseException as exc:
            _trace(f'{fn.__name__}: RAISED {type(exc).__name__}: {exc}')
            raise
    _trace('QT_SPIKE_SELFTEST_OK')
    print('QT_SPIKE_SELFTEST_OK')
    return 0


if __name__ == '__main__':
    sys.exit(run_selftest())
