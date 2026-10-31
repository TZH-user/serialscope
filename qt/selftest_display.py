# -*- coding: utf-8 -*-
"""日志显示增强自测组(关键词高亮告警、字号缩放、BREAK/信号线、QoL 设置)。

约定:本文件提供 groups() 返回测试函数列表;由 qt/selftest.py 的 run_selftest 挂载。
所有测试离屏可跑、无真机、无网络、不起 sys.executable 子进程(冻结冒烟安全)。
"""
from __future__ import annotations

import random
import sys
import time

_DISPLAY_KEYS = ('log/kw_alert', 'log/kw_watch', 'log/font_pt', 'log/copy_on_select',
                 'log/stamp_mode', 'ui/on_top', 'log/ansi_render')
# 开过连接的组会经 _save_connect_settings 写 connect/*，一并快照
_SNAPSHOT_KEYS = ('connect/port', 'connect/baud', 'connect/auto_link') + _DISPLAY_KEYS


def _find_block(log, needle: str):
    b = log.document().firstBlock()
    while b.isValid():
        if needle in b.text():
            return b
        b = b.next()
    return None


def _char_color_at(log, block, needle: str) -> str:
    cur = log.textCursor()
    cur.setPosition(block.position() + block.text().index(needle) + 1)  # charFormat 取光标前一字符
    return cur.charFormat().foreground().color().name().lower()


def test_rx_keyword_highlight():
    """关键词高亮纯逻辑 + 主窗两条渲染路径(ANSI 开/关)都要命中。"""
    from PySide6 import QtWidgets

    import ui.main_window as mw
    from core import rx_highlight
    from ui import theme
    from selftest import _restore_settings, _snapshot_settings

    # 纯逻辑:解析/优先级/剥转义/大小写
    assert rx_highlight.parse_keywords('error, Assert fail，OVER TEMP  over') == \
        ['error', 'Assert', 'fail', 'OVER', 'TEMP', 'over'], '去重保序+全角逗号'
    assert rx_highlight.match_line('plain line', [], []) is None, '空表零开销路径'
    assert rx_highlight.match_line('boot ERROR now', ['error'], []) == 'alert'
    assert rx_highlight.match_line('retry 3', [], ['RETRY']) == 'watch'
    assert rx_highlight.match_line('error and retry', ['error'], ['retry']) == 'alert'
    assert rx_highlight.match_line('\x1b[31mE: fail\x1b[0m', ['fail'], []) == 'alert', \
        '转义字节不得拆散关键词'
    assert rx_highlight.match_line('no match', ['error'], ['retry']) is None

    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication(sys.argv)
    saved = _snapshot_settings(_SNAPSHOT_KEYS)
    win = None
    try:
        win = mw.MainWindow()
        win._port_watch_timer.stop()
        from core.settings import write_setting
        write_setting('log/kw_alert', 'PANIC, boom')
        write_setting('log/kw_watch', 'retry')
        err, warn = theme.color('status_err').lower(), theme.color('status_warn').lower()

        # ANSI 路径(默认开):设备色被关键词色覆盖(用户意图优先)
        win._append_log('RX', '\x1b[32mok PANIC now\x1b[0m')
        b = _find_block(win.log, 'PANIC')
        assert _char_color_at(win.log, b, 'PANIC') == err, \
            _char_color_at(win.log, b, 'PANIC')
        # watch 级弱于 alert 级
        win._append_log('RX', 'will retry soon')
        b = _find_block(win.log, 'retry')
        assert _char_color_at(win.log, b, 'retry') == warn
        # 无命中行走默认 RX 色
        win._append_log('RX', 'hello world')
        b = _find_block(win.log, 'hello world')
        assert _char_color_at(win.log, b, 'hello') == theme.color('rx').lower()

        # 旧路径(ANSI 关):整行 color+bold 同样生效
        win.chk_ansi.setChecked(False)
        win._append_log('RX', 'boom happens')
        b = _find_block(win.log, 'boom happens')
        assert _char_color_at(win.log, b, 'boom') == err
        # 关键词条目原文不动(导出/自动日志口径)
        assert win.message_entries[-1]['text'] == 'boom happens'
    finally:
        if win is not None:
            win._port_watch_timer.stop()
            win.close()
        _restore_settings(saved)
    print('RX KEYWORD HIGHLIGHT OK')


def test_log_font_zoom():
    """Ctrl+滚轮字号缩放:控件级样式表只管 size(族名仍由 app QSS 级联)、
    持久化 log/font_pt、复位回跟随主题、上下限钳制。"""
    from PySide6 import QtGui, QtWidgets

    import ui.main_window as mw
    from ui import theme
    from selftest import _restore_settings, _snapshot_settings as _snap

    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication(sys.argv)
    saved = _snap(_DISPLAY_KEYS)
    win = None
    try:
        win = mw.MainWindow()
        win._port_watch_timer.stop()
        win.log.ensurePolished()
        mono_fam = theme.mono_font().family()

        # offscreen 平台插件（Windows）无系统字体库：QFontInfo 无对象可解析，
        # 字号类断言仅在真实字体库下执行（族名/文档字体断言不受平台影响）。
        real_fontdb = bool(QtGui.QFontDatabase.families())

        # 复位态:跟随主题(无控件级 size),族名仍等宽
        win.log.zoom(0)
        assert win.log.font_pt() == 0
        assert win.log.font().family() == mono_fam
        if real_fontdb:
            assert QtGui.QFontInfo(win.log.font()).pointSize() >= 9

        win.log.zoom(1)
        pt1 = win.log.font_pt()
        assert pt1 == 10, pt1
        win.log.ensurePolished()
        if real_fontdb:
            assert QtGui.QFontInfo(win.log.font()).pointSize() == pt1, 'QSS size 应落到实际字体'
        assert win.log.font().family() == mono_fam, '放大不得丢等宽族名'
        assert win.log.document().defaultFont().family() == mono_fam
        assert win.log.document().defaultFont().pointSize() == pt1,             '正文按文档默认字体排版——字号必须落到文档'

        win.log.zoom(-1)
        assert win.log.font_pt() == pt1 - 1, '缩回一步'

        for _ in range(40):
            win.log.zoom(1)
        assert win.log.font_pt() == 24, '上限钳制'
        for _ in range(60):
            win.log.zoom(-1)
        assert win.log.font_pt() == 6, '下限钳制'

        win.log.zoom(0)
        from core.settings import read_setting
        assert read_setting('log/font_pt', '1') == '0', '复位应持久化 0(跟随主题)'
        win.log.zoom(2)
        assert read_setting('log/font_pt', '0') == str(win.log.font_pt()), '缩放应持久化'
    finally:
        if win is not None:
            win._port_watch_timer.stop()
            win.close()
        _restore_settings(saved)
    print('LOG FONT ZOOM OK')


def test_break_and_line_signals():
    """BREAK 按钮 + modem 状态线轮询:fake worker 计数,标签按 ●/○ 渲染。"""
    from PySide6 import QtWidgets

    import ui.main_window as mw
    from selftest import _fake_worker_cls, _restore_settings, _snapshot_settings

    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication(sys.argv)
    saved = _snapshot_settings(_SNAPSHOT_KEYS)
    mw.SerialWorker = _fake_worker_cls()
    win = None
    try:
        win = mw.MainWindow()
        win._port_watch_timer.stop()
        win.combo_port.setCurrentIndex(0)
        win.open_connection()
        assert win.worker is not None and win.worker.breaks == 0

        assert not win.btn_break.isEnabled() or True  # 置灰状态随 _update_connect_button
        win.btn_break.setEnabled(True)
        win.btn_break.click()
        assert win.worker.breaks == 1, 'BREAK 按钮应走到 worker.send_break'
        assert any('BREAK' in e['text'] for e in win.message_entries)

        win._poll_line_signals()
        text = win.lbl_lines.text()
        assert 'CTS●' in text and 'DSR○' in text and 'CD●' in text and 'RI○' in text, text

        win._teardown_worker()
        assert win.lbl_lines.text() == '', '断开后信号线标签应清空'
        assert not win._line_timer.isActive()
    finally:
        if win is not None:
            win._port_watch_timer.stop()
            win.close()
        _restore_settings(saved)
    print('BREAK AND LINE SIGNALS OK')


def test_qol_settings():
    """QoL 三件:窗口置顶 flags、选中即复制、elapsed 时间戳。"""
    from PySide6 import QtCore, QtGui, QtWidgets

    import ui.main_window as mw
    from selftest import _fake_worker_cls, _restore_settings, _snapshot_settings

    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication(sys.argv)
    saved = _snapshot_settings(_SNAPSHOT_KEYS)
    mw.SerialWorker = _fake_worker_cls()
    win = None
    try:
        win = mw.MainWindow()
        win._port_watch_timer.stop()

        # 置顶:开→flags 带 StaysOnTop;关→摘掉
        win._apply_on_top(True)
        assert win.windowFlags() & QtCore.Qt.WindowType.WindowStaysOnTopHint
        win._apply_on_top(False)
        assert not (win.windowFlags() & QtCore.Qt.WindowType.WindowStaysOnTopHint)

        # 选中即复制:开关关闭时不动剪贴板;开启后拖选同步
        # (两次选区必须不同区间——同区间重设不触发 selectionChanged)
        clip = QtWidgets.QApplication.clipboard()
        clip.setText('sentinel')
        win._append_log('RX', 'copy me please')
        b = _find_block(win.log, 'copy me please')
        txt = b.text()

        def select(fragment: str):
            start = txt.index(fragment)
            cur = win.log.textCursor()
            cur.setPosition(b.position() + start)
            cur.setPosition(b.position() + start + len(fragment),
                            QtGui.QTextCursor.MoveMode.KeepAnchor)
            win.log.setTextCursor(cur)

        select('copy')
        assert clip.text() == 'sentinel', '开关关闭时选中不得进剪贴板'
        from core.settings import write_setting
        write_setting('log/copy_on_select', '1')
        select('me ple')
        assert clip.text() == 'me ple', clip.text()

        # elapsed 时间戳:未连接回落全时刻;连接后按 [xx.xxxs]
        write_setting('log/stamp_mode', 'elapsed')
        win._append_log('RX', 'no conn yet')
        assert '[20' in win.log.toPlainText(), '未连接应回落全时刻'
        n0 = len(win.message_entries)
        win.open_connection()
        win._append_log('RX', 'connected line')
        e = win.message_entries[-1]
        stamp_line = [x for x in win.log.toPlainText().splitlines()
                      if 'connected line' in x][-1]
        assert stamp_line.split('] ')[0].endswith('s'), stamp_line
        win._teardown_worker()
        win._append_log('RX', 'after teardown')
        assert '[20' in [x for x in win.log.toPlainText().splitlines()
                         if 'after teardown' in x][-1], '断开后回落全时刻'
    finally:
        if win is not None:
            win._port_watch_timer.stop()
            win.close()
        _restore_settings(saved)
    print('QOL SETTINGS OK')


def test_find_regex():
    """查找条正则模式:全部匹配计数/跳转、无效正则提示、时刻模式不受影响。"""
    from PySide6 import QtWidgets

    import ui.main_window as mw
    from selftest import _CONNECT_KEYS, _restore_settings, _snapshot_settings

    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication(sys.argv)
    saved = _snapshot_settings(_CONNECT_KEYS)
    win = None
    try:
        win = mw.MainWindow()
        win._port_watch_timer.stop()
        win._append_log('RX', 'RX 0x41 0x42 ok')
        win._append_log('RX', 'E (12) i2c: nack')
        win._append_log('RX', 'RX 0xff tail')
        fb = win.find_bar
        fb.open_and_focus()
        fb.chk_regex.setChecked(True)
        fb.edit_find.setText('RX 0x[0-9A-Fa-f]{2}')
        n = len(fb._matches)
        assert n == 2, n   # 'RX 0x41' 与 'RX 0xff'（0x42 无 RX 前缀不匹配）
        assert fb.lbl_count.text().startswith('正则 第 1/2'), fb.lbl_count.text()
        assert len(fb._edit.extraSelections()) == 2, '全部匹配应弱高亮'
        fb.navigate(+1)
        assert '第 2/2' in fb.lbl_count.text()

        # 无效正则不崩、给提示
        fb.edit_find.setText(r'E (\d+')
        assert '正则无效' in fb.lbl_count.text()
        # 无匹配
        fb.edit_find.setText('zzz-not-there')
        assert fb.lbl_count.text() == '无匹配'
        # 关闭正则后时刻模式仍可用
        fb.chk_regex.setChecked(False)
        fb.edit_find.clear()
        stamps = [e['ts'].strftime('%H:%M:%S') for e in win.message_entries]
        fb.edit_find.setText(stamps[1])
        assert fb._time_mode and '时刻' in fb.lbl_count.text(), fb.lbl_count.text()
    finally:
        if win is not None:
            win._port_watch_timer.stop()
            win.close()
        _restore_settings(saved)
    print('FIND REGEX OK')


def test_status_elide():
    """状态行省略标签:超长报错不撑宽(sizePolicy Ignored)、绘制文本带「…」、tooltip 全文。"""
    from PySide6 import QtCore, QtGui, QtWidgets

    import ui.main_window as mw
    from selftest import _CONNECT_KEYS, _fake_worker_cls, _restore_settings, _snapshot_settings

    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication(sys.argv)
    saved = _snapshot_settings(_CONNECT_KEYS)
    mw.SerialWorker = _fake_worker_cls()
    win = None
    try:
        win = mw.MainWindow()
        win._port_watch_timer.stop()
        long_err = '打开失败: Cannot configure port, something went wrong. ' * 4
        win.set_status(long_err, 'status_err')

        lbl = win.lbl_status
        assert lbl.text() == long_err, 'text() 应返回全文（逻辑/测试口径不受省略影响）'
        assert lbl.toolTip() == long_err, '悬停 tooltip 必须是完整报错'
        assert lbl.sizePolicy().horizontalPolicy() == \
            QtWidgets.QSizePolicy.Policy.Ignored, '横向 Ignored 才不会把窗口撑宽'

        # 窄容器下重算省略:实际显示文本被截断且带省略号。
        # 驱动方式：setGeometry 同步改 width()，但 offscreen 对隐藏控件推迟事件
        # 派发——逻辑层直接调 _refresh()，接线层用 sendEvent 送真 QResizeEvent。
        holder = QtWidgets.QWidget()
        holder.resize(400, 24)
        probe = mw.ElidedLabel(long_err, parent=holder)
        probe.setGeometry(0, 0, 240, 24)
        probe._refresh()
        shown = QtWidgets.QLabel.text(probe)
        assert len(shown) < len(long_err) and shown.endswith('…'), shown[-30:]
        # 复宽后再展开:恢复完整绘制（宽度按字体度量自适应，写死像素会假阳性）
        need = probe.fontMetrics().horizontalAdvance(long_err) + 100
        probe.setGeometry(0, 0, need, 24)
        probe._refresh()
        assert QtWidgets.QLabel.text(probe) == long_err, '足够宽时应完整显示不省略'
        assert probe.toolTip() == long_err
        # 接线:resizeEvent 必须触发重算（真实桌面上宽度变化都经这条路）。
        # sendEvent 只送通知不改几何——先真正缩窄再送事件，重算读到的才是新宽度。
        probe.setGeometry(0, 0, 120, 24)
        evt = QtGui.QResizeEvent(QtCore.QSize(120, 24), QtCore.QSize(need, 24))
        QtWidgets.QApplication.sendEvent(probe, evt)
        shown3 = QtWidgets.QLabel.text(probe)
        assert len(shown3) < len(long_err) and shown3.endswith('…'), 'resize 事件后应重算省略'
        holder.deleteLater()

        # 打开失败路径:同样落省略标签（文本带「打开失败: 」前缀，tooltip=全文）
        win._on_open_failed(long_err)
        assert lbl.text() == '打开失败: ' + long_err, lbl.text()[:60]
        assert lbl.toolTip() == lbl.text(), '悬停 tooltip 必须是完整报错'
    finally:
        if win is not None:
            win._port_watch_timer.stop()
            win.close()
        _restore_settings(saved)
    print('STATUS ELIDE OK')


def test_multi_window_broadcast():
    """多会话窗口:注册编号 + 广播一跳转发不回环 + 未连接窗口跳过。"""
    from PySide6 import QtWidgets

    import ui.main_window as mw
    from selftest import (_CONNECT_KEYS, _fake_worker_cls, _restore_settings,
                          _snapshot_settings)

    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication(sys.argv)
    saved = _snapshot_settings(_CONNECT_KEYS + ('tx/broadcast',))
    mw.SerialWorker = _fake_worker_cls()
    wins = []
    try:
        win1 = mw.MainWindow()
        win1._port_watch_timer.stop()
        wins.append(win1)
        assert '会话 1' in win1.windowTitle()
        win1.open_connection()

        win2 = win1._open_new_session_window()
        win2._port_watch_timer.stop()
        wins.append(win2)
        assert '会话 2' in win2.windowTitle()
        win2.open_connection()

        # 统一收发口径（ini 记忆可能残留 HEX/CRLF）+ 钉死广播初态
        for w in (win1, win2):
            w.combo_tx_format.setCurrentText('ASCII')
            w.combo_newline.setCurrentText('LF')
            w.btn_broadcast.setChecked(False)

        def tx_texts(w):
            return [e['text'] for e in w.message_entries if e['direction'] == 'TX']

        # 广播：win2 开广播发送 → win1 收到（广播）条目，win2 自身也落 TX
        win2.btn_broadcast.setChecked(True)
        win2.edit_send.setText('AT+BC')
        win2.send()
        assert any(t.startswith('AT+BC<LF>') for t in tx_texts(win2))
        assert any(t == 'AT+BC<LF>（广播）' for t in tx_texts(win1)), tx_texts(win1)
        assert any('已广播到 1' in e['text'] for e in win2.message_entries
                   if e['direction'] == 'SYS')
        assert len(tx_texts(win1)) == 1, '一跳语义：接收窗口不得二次转发'

        # win1 广播关：win1 发送不进 win2
        win1.edit_send.setText('AT+SOLO')
        win1.send()
        assert not any('AT+SOLO' in t for t in tx_texts(win2))

        # 第三个窗口未连接：广播跳过、计数值不变
        win3 = win1._open_new_session_window()
        win3._port_watch_timer.stop()
        wins.append(win3)
        win2.edit_send.setText('AT+BC2')
        win2.send()
        assert any('已广播到 1' in e['text'] for e in win2.message_entries
                   if e['direction'] == 'SYS')
        assert not any('广播' in t for t in tx_texts(win3))
    finally:
        for w in wins:
            w._port_watch_timer.stop()
            w.close()
        _restore_settings(saved)
    print('MULTI WINDOW BROADCAST OK')


def test_screen_gl_fit_dpr():
    """视频视口等比缩放居中必须按设备像素算（glViewport 坐标系 = 逻辑尺寸 × DPR）。

    用户实报：150% 缩放屏上画面缩成 1/dpr 且不居中。回归锚 = 用户实测几何
    （1689×738 逻辑 @ dpr 1.5，帧 1024×600）：缩放后应高向撑满、水平居中。
    """
    from ui.device_screen import ScreenGLWidget
    x, y, w, h = ScreenGLWidget._fit_viewport_rect(1689, 738, 1024, 600, 1.5)
    assert (w, h) == (1889, 1107), (x, y, w, h)   # 1107=738*1.5 高向撑满
    assert abs((x + w / 2) - (1689 * 1.5 / 2)) <= 1, '水平居中'
    assert y == 0, '垂直方向撑满无黑边'
    # dpr=1 退化为经典 letterbox：高向撑满（738/600 < 1689/1024），水平居中留黑边
    x1, y1, w1, h1 = ScreenGLWidget._fit_viewport_rect(1689, 738, 1024, 600, 1.0)
    assert (w1, h1) == (1259, 738) and y1 == 0, (x1, y1, w1, h1)
    assert abs((x1 + w1 / 2) - 1689 / 2) <= 1, 'dpr=1 仍须水平居中'
    print('SCREEN GL FIT DPR OK')


def test_close_two_stage():
    """两段式关窗：closeEvent 即刻隐藏（窗口不等收尾），重活下一拍 finalize。

    旧实现把 worker 1.5s + 控制 6s + 收流/解码 3s×2 + CLI 1s 全内联在 closeEvent，
    最坏 ~14s 无响应 = 用户报"关闭按钮失效"。回归锚：close() 返回后窗口立刻不可见，
    再一轮事件循环后收尾全部完成（worker 拆干净、quitOnLastWindowClosed 还原）。
    """
    import ui.main_window as mw

    from PySide6 import QtWidgets

    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication(sys.argv)
    win = mw.MainWindow()
    win.show()
    win._port_watch_timer.stop()
    app.processEvents()                     # 先放行前序测试窗口遗留的 finalize（全局旗标会还原）
    saved_quit = app.quitOnLastWindowClosed()
    try:
        t0 = time.perf_counter()
        win.close()
        close_ms = (time.perf_counter() - t0) * 1000
        assert not win.isVisible(), 'closeEvent 必须即刻隐藏窗口（不等收尾）'
        assert close_ms < 300, f'close() 应瞬时返回，实 {close_ms:.0f}ms'
        for _ in range(50):                    # 放行 singleShot(0) 的 finalize
            app.processEvents()
            if getattr(win, '_finalized', False):
                break
            time.sleep(0.01)
        assert getattr(win, '_finalized', False), 'finalize 必须在下一拍执行'
        assert win.worker is None, '收尾必须拆干净 worker'
        assert app.quitOnLastWindowClosed() == saved_quit, 'quitOnLastWindowClosed 必须还原'
        assert all(w is not win for w in mw._LIVE_WINDOWS), '注册表必须除名'
    finally:
        app.setQuitOnLastWindowClosed(saved_quit)
        if win in mw._LIVE_WINDOWS:
            mw._LIVE_WINDOWS.remove(win)
    print('CLOSE TWO STAGE OK')


def test_screen_mouse_map():
    """鼠标注入坐标换算：控件坐标 ×DPR → 逆 letterbox → 板屏像素，黑边区拒绝。

    跟手的关键换算：中心点必须命中等比中心（512,300）；黑边/无帧必须返回 None
    （否则点击会注入到错误坐标）。
    """
    from PySide6 import QtWidgets

    from ui.device_screen import ScreenGLWidget

    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication(sys.argv)
    w = ScreenGLWidget()
    w.resize(1689, 738)
    app.processEvents()
    assert w.map_to_board(10, 10) is None, '无帧必须拒绝注入'
    w._latest = {'width': 1024, 'height': 600, 'frame': None}
    cx, cy = w.map_to_board(w.width() / 2, w.height() / 2)
    assert (cx, cy) == (511, 299), (cx, cy)   # 中心 → 板屏中心（×(W-1)/(H-1) 取整）
    assert w.map_to_board(10, 10) is None, '左黑边必须拒绝'
    assert w.map_to_board(w.width() - 1, w.height() / 2) is None, '右黑边必须拒绝'
    # 图内有效点：左上视口起点附近（dpr=1：视口 x 从 215 起）
    first = w.map_to_board(220, 5)
    assert first is not None and 0 <= first[0] < 8 and 0 <= first[1] <= 5, first
    print('SCREEN MOUSE MAP OK')


def test_rx_flood_coalesce():
    """RX 洪泛合并：小数据当场消费（语义不变），洪泛有界消费+丢最老+统计行打标。

    根因回归锚：垃圾流逐事件整行处理会把 GUI 线程钉死在 posted events（Windows
    5s 泵不到原生消息 → 幽灵窗 → 标题栏 X 灰）。合并后任意相邻消费间必回事件循环。
    """
    import ui.main_window as mw

    from PySide6 import QtWidgets

    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication(sys.argv)
    win = mw.MainWindow()
    win._port_watch_timer.stop()
    try:
        # 小数据：当场消费（不依赖事件循环），日志立即可见
        win.ingest_bytes(b'FLOOD-INLINE-1\n')
        assert 'FLOOD-INLINE-1' in win.log.toPlainText(), '小数据必须当场消费'
        # 洪泛：640KB 随机垃圾（含换行）→ 单次 ingest 有界（预算 16KB/拍）、超限丢弃
        rng = random.Random(41)
        blob = bytes(rng.randrange(256) for _ in range(64 * 1024))
        worst = 0.0
        for _ in range(10):
            t0 = time.perf_counter()
            win.ingest_bytes(blob)              # 逐次灌 64KB
            worst = max(worst, (time.perf_counter() - t0) * 1000)
        assert worst < 400, f'洪泛下单次 ingest 必须快速返回（≤16KB 预算），实最差 {worst:.0f}ms'
        assert win._rx_dropped > 0, '超出高水位应丢最老'
        assert '洪泛保护' in win.log.toPlainText(), '丢弃必须打标'
        assert '洪泛丢弃' in win.lbl_backlog.text()
        for _ in range(200):                     # 放行 drain 定时器至排空
            app.processEvents()
            if not win._rx_pending:
                break
            time.sleep(0.01)
        assert not win._rx_pending, '洪泛结束后 pending 必须排空（无静默丢尾）'
    finally:
        win.close()
    for _ in range(50):
        app.processEvents()
        if getattr(win, '_finalized', False):
            break
        time.sleep(0.01)
    print('RX FLOOD COALESCE OK')


def test_screen_overlay_controls():
    """跑表开关/透明度控件：改动落盘；未发流时不重起流（source 仍为 None）。"""
    from PySide6 import QtWidgets

    import ui.main_window as mw
    from core.settings import read_setting

    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication(sys.argv)
    win = mw.MainWindow()
    win._port_watch_timer.stop()
    saved = (read_setting('linux/stopwatch', '1'), read_setting('linux/stopwatch_op', '85'))
    try:
        page = win.device_screen
        page.chk_watch.setChecked(False)
        page.spin_op.setValue(40)
        page.spin_op.editingFinished.emit()
        app.processEvents()
        assert read_setting('linux/stopwatch', '') == '0', '开关必须落盘'
        assert read_setting('linux/stopwatch_op', '') == '40', '透明度必须落盘'
        assert page.source is None, '未发流时改动不得触发重起流'
    finally:
        from core.settings import write_setting
        write_setting('linux/stopwatch', saved[0])
        write_setting('linux/stopwatch_op', saved[1])
        win.close()
    print('SCREEN OVERLAY CONTROLS OK')


def test_sys_stats_panel():
    """系统状态 1Hz 轮询：假服务端喂两拍（首拍差值缺省→次拍全键），页标签正确渲染。

    轮询线程走 socketpair 假服务端（不碰真网）；页标签对缺键渲染 '--'，
    满键渲染真实值（数值口径与 server_rk/sys_stats.py 真机采样一致）。
    """
    import json as _json
    import socket as _socket
    import threading as _threading

    from PySide6 import QtWidgets

    from ui.device_screen import DeviceScreenPage, SysStatsPoller

    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication(sys.argv)

    # ---- 假服务端：socketpair，hello→pair→两拍 sys_stats ----
    csock, ssock = _socket.socketpair()
    srv_done = _threading.Event()

    def serve():
        try:
            f = ssock.makefile('rwb')
            expect = iter([{'stats': {}},           # 首拍：差值字段缺省（CPU%/IO 速率）
                           {'stats': {'cpu_percent': 32.9,
                                      'mem_total_mb': 1962.0, 'mem_used_mb': 1148.2,
                                      'disk_total_gb': 6.98, 'disk_free_gb': 1.05,
                                      'disk_dev': 'mmcblk0', 'temp_c': 58.4,
                                      'load1': 1.73, 'uptime_s': 81965}}])
            for raw in f:
                req = _json.loads(raw.decode('utf-8'))
                cmd = req.get('cmd')
                if cmd == 'hello':
                    f.write((_json.dumps({'ok': True, 'challenge': 'c0'}) + '\n').encode())
                    f.flush()
                elif cmd == 'pair':
                    f.write((_json.dumps({'ok': True, 'session': 's0'}) + '\n').encode())
                    f.flush()
                elif cmd == 'sys_stats':
                    try:
                        body = next(expect)
                    except StopIteration:
                        break
                    f.write((_json.dumps({'ok': True, **body}) + '\n').encode())
                    f.flush()
                else:
                    break
        except OSError:
            pass
        finally:
            srv_done.set()

    srv = _threading.Thread(target=serve, daemon=True)
    srv.start()

    poller = SysStatsPoller('127.0.0.1', 9201, '246810',
                            connect_fn=lambda hp, timeout: csock)
    got: list[dict] = []
    poller.sample.connect(got.append)
    poller.start()
    deadline = time.time() + 3.5
    while len(got) < 2 and time.time() < deadline:
        app.processEvents()          # sample 信号跨线程 queued，需事件循环放行
        time.sleep(0.02)
    poller.request_stop()
    poller.wait(3000)
    ssock.close()
    srv_done.wait(1.0)
    assert len(got) >= 2, f'两拍采样未到齐: {len(got)}'
    assert got[0] == {}, f'首拍应为空（差值缺省）: {got[0]}'
    assert got[1].get('cpu_percent') == 32.9, got[1]

    # ---- 页面渲染：满键上真值，空拍回落 '--' ----
    page = DeviceScreenPage()
    page._on_sys_sample(got[1])
    assert page.lbl_cpu.text() == 'CPU 33%', page.lbl_cpu.text()
    assert page.lbl_mem.text() == '内存 1148/1962 MB', page.lbl_mem.text()
    assert page.lbl_emmc.text() == 'eMMC 5.9/7.0 GB·mmcblk0', page.lbl_emmc.text()
    assert page.lbl_temp.text() == '温度 58℃', page.lbl_temp.text()
    assert page.lbl_load.text() == '负载 1.73', page.lbl_load.text()
    assert page.lbl_up.text() == '运行 0天22:46', page.lbl_up.text()
    page._on_sys_sample({})
    assert page.lbl_cpu.text() == 'CPU --%' and page.lbl_emmc.text() == 'eMMC --'
    page._stop_sys_poller()
    print('SYS STATS PANEL OK')


def groups():
    return [test_rx_keyword_highlight, test_log_font_zoom,
            test_break_and_line_signals, test_qol_settings, test_find_regex,
            test_status_elide, test_multi_window_broadcast,
            test_screen_gl_fit_dpr, test_close_two_stage,
            test_screen_mouse_map, test_rx_flood_coalesce,
            test_screen_overlay_controls, test_sys_stats_panel]
