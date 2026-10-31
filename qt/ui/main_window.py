# -*- coding: utf-8 -*-
"""主窗体：连接/断开 + RX/TX 日志（ASCII/HEX 双格式）+ 行缓冲/空闲落显/积压常显
+ 发送（HEX/换行模式/历史回翻）+ 文件发送（分块定时）+ 查找条 + 波形页签。

M3 复刻主线功能口径：
- HEX 发送解析同主线（0x 前缀可带、空白/逗号/分号/冒号/连字符分隔、非法 token 报错）
- 换行模式 无/LF/CR/CRLF，ASCII 与 HEX 发送都追加
- 文件发送 4 KiB/20ms 分块（≈200 KiB/s 上限），发送中再点 = 停止
"""
from __future__ import annotations

import datetime
import html
import os
import sys
import time

from PySide6 import QtCore, QtGui, QtWidgets

from core import APP_NAME, APP_VERSION, REPO_URL
from core.ai_doc import get_control_channel_doc
from core.auto_log import AutoLogWriter, build_log_export_text, format_entry_block, get_log_dir
from core.control_channel import ControlAPI, ControlChannel, handle_control_request
from core import ansi_render, esp_idf_log, rx_highlight
from core.fault_decode import test_fault_dump_line
from core.hexutil import bytes_to_hex_text, try_parse_hex_text
from core.port_hints import port_open_error_hint
from core.ports import (port_display_text, port_display_text_full,
                        port_name_from_display, pick_auto_connect_port,
                        scan_port_infos, scan_port_infos_full)
from core.rx_buffer import RxBuffer
from core.serial_worker import SerialWorker
from core.xmodem import XmodemSender, YmodemSender
from core.session_record import SessionRecorder, load_replay_chunks
from core.settings import read_setting, write_setting
from core.update_check import RELEASES_PAGE, check_app_update
from ui.backtrace_dialog import BacktraceDialog
from ui.fault_panel import FaultPanel
from ui.find_bar import FindBar
from ui.history_edit import HistoryLineEdit
from ui.log_hover import LogHoverTip
from ui.log_view import LogView
from ui.modbus_panel import ModbusPanel
from ui.settings_page import SettingsPage
from ui.spectrum_panel import SpectrumPanel
from ui.stm32_panel import Stm32Panel
from ui.tx_sequence_dialog import TxSequenceDialog
from ui import theme
from ui.device_screen import DeviceScreenPage
from ui.wave_window import WaveWindow

NEWLINE_TEXT = {'无': '', 'LF': '\n', 'CR': '\r', 'CRLF': '\r\n'}
NEWLINE_BYTES = {'无': b'', 'LF': b'\n', 'CR': b'\r', 'CRLF': b'\r\n'}

# 多会话窗口注册表：每窗口=独立串口会话（各自 worker/日志/页面），广播发送与标题编号用
_LIVE_WINDOWS: list = []
# 收不回的 worker 停尸房：串口线程卡死在内核调用（USB 串口假死）时 wait(1500) 收不回，
# deleteLater 会带线程销毁（"QThread: Destroyed while thread is still running" 可致崩溃）
# ——只扣引用不销毁，进程退出统一回收。
_ZOMBIE_WORKERS: list = []

# AI 通道 settings_set/get 的键别名（英文键 + 中文常用名 → 规范键）
_SETTING_ALIASES = {
    'baud': 'baud', '波特率': 'baud', 'baudrate': 'baud',
    'port': 'port', '串口': 'port', 'serial_port': 'port',
    'protocol': 'protocol', '协议': 'protocol',
    'theme': 'theme', '主题': 'theme',
    'points': 'points', 'window': 'points', '显示点数': 'points',
    'fill': 'fill', 'fill_under': 'fill', 'curve_fill': 'fill', '曲线填充': 'fill',
    'paused': 'paused', 'pause': 'paused', '暂停': 'paused',
}


def _to_bool(value):
    """宽解析布尔（true/false/1/0/on/off/yes/no）；非法返回 None。"""
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)) and value in (0, 1):
        return bool(value)
    text = str(value).strip().lower()
    if text in ('true', '1', 'on', 'yes'):
        return True
    if text in ('false', '0', 'off', 'no'):
        return False
    return None

FILE_CHUNK = 4096   # 4 KiB/20ms ≈ 上限 200 KiB/s（对齐主线）
FILE_INTERVAL_MS = 20


MAX_LOG_ENTRIES = 20000
MAX_LOG_TRIM = 4000


def _stamp() -> str:
    # 主线同款全时间戳（含毫秒）：日志导出 / 自动日志 / Ctrl+F 时间定位都靠它
    now = datetime.datetime.now()
    return f'{now:%Y-%m-%d %H:%M:%S}.{now.microsecond // 1000:03d}'


class _UpdateCheckRunnable(QtCore.QRunnable):
    """网络查询放线程池，UI 不卡（Qt 版比主线 PS 定时器方案干净的地方）。"""

    class Signals(QtCore.QObject):
        done = QtCore.Signal(dict)

    def __init__(self, check_fn, version: str):
        super().__init__()
        self._check_fn = check_fn
        self._version = version
        self.signals = self.Signals()

    def run(self):
        try:
            result = self._check_fn(self._version)
        except Exception as exc:  # 防御：check_app_update 理论上不抛
            result = {'ok': False, 'has_update': False, 'note': str(exc)}
        self.signals.done.emit(result)


class ElidedLabel(QtWidgets.QLabel):
    """单行自动省略标签：宽度不足显示「…」，tooltip 常驻全文，不参与撑宽所在行。

    状态行（打开失败等长报错文本）用它替代裸 QLabel——此前超长报错把整行乃至
    整个窗口顶宽。sizePolicy 横向 Ignored → 布局不再被 sizeHint 顶开，
    实际绘制文本随 resizeEvent 重算（QFontMetrics.elidedText）。
    """

    def __init__(self, text: str = '', parent=None):
        super().__init__(parent)
        self._full = text or ''
        sp = self.sizePolicy()
        sp.setHorizontalPolicy(QtWidgets.QSizePolicy.Policy.Ignored)
        self.setSizePolicy(sp)
        self.setMinimumWidth(1)
        if self._full:
            self._refresh()

    def setText(self, text: str):   # noqa: N802（Qt 命名）
        self._full = text or ''
        self._refresh()

    def text(self) -> str:          # noqa: N802（返回全文，测试/逻辑口径不受省略影响）
        return self._full

    def _refresh(self):
        fm = self.fontMetrics()
        super().setText(fm.elidedText(
            self._full, QtCore.Qt.TextElideMode.ElideRight, max(24, self.width() - 2)))
        self.setToolTip(self._full)

    def resizeEvent(self, ev):      # noqa: N802（宽度变化 → 重算省略）
        super().resizeEvent(ev)
        self._refresh()


class MainWindow(QtWidgets.QMainWindow):
    FLUSH_INTERVAL_MS = 200

    def __init__(self):
        super().__init__()
        # 启动主题恢复（读设置键 ui/theme，缺省深色）：先于各页创建，
        # pyqtgraph 控件即按主题背景/前景初始化（main.py 的深色 QSS 由这里覆盖统一）
        theme.apply_saved_theme(QtWidgets.QApplication.instance())
        self.resize(980, 620)

        self.buffer = RxBuffer()
        self.worker: SerialWorker | None = None
        self._max_log_blocks = 20000
        self._file_send: dict | None = None
        self._modbus_rx_acc = bytearray()  # Modbus 事务在途时的响应累积
        self._modbus_active = False  # 仅事务在途时累积 RX 字节，闲时洪泛不再无界增长
        self.message_entries: list[dict] = []  # {ts, direction, text, seq}：导出/自动日志/控制通道共用
        self._log_seq = 0               # 日志条目单调序号：read_lines follow 推送的游标
        self._modbus_recent: list[dict] = []  # 最近 Modbus 事务摘要（AI 通道 modbus_recent 数据源）
        self.rx_bytes_total = 0
        self.tx_bytes_total = 0
        self._known_port_names: list[str] | None = None  # 热插拔基线（None=还没首扫）
        self._connected_device = ''
        self._auto_log = AutoLogWriter(get_log_dir())
        self.recorder = SessionRecorder(get_log_dir())
        self._replay_state: dict | None = None

        # ---- 顶部连接行 ----
        self.combo_port = QtWidgets.QComboBox()
        self.combo_port.setEditable(True)          # v2 M1：可手输 pyserial URL（rfc2217:// 等）
        self.combo_port.setInsertPolicy(QtWidgets.QComboBox.NoInsert)
        self.combo_port.setToolTip(
            '本地串口从列表选择；网络串口直接手输 pyserial URL 后连接，如\n'
            'rfc2217://192.168.1.50:7000（板端 ser2net，RFC2217 波特率由板端协商）\n'
            'socket://192.168.1.50:7001（TCP 裸透传）')
        self.combo_baud = QtWidgets.QComboBox()
        self.combo_baud.setEditable(True)
        # v1.3.0：补 ESP32 常用波特率——74880(ROM 启动日志)、921600/2000000(烧录口)
        self.combo_baud.addItems(['2000000', '1500000', '921600', '460800', '115200', '74880'])
        self.btn_connect = QtWidgets.QPushButton('连接')
        self.btn_connect.clicked.connect(self.toggle_connect)
        self.btn_new_window = QtWidgets.QPushButton('新窗口')
        self.btn_new_window.setObjectName('tool')
        self.btn_new_window.setToolTip(
            '再开一个会话窗口（Ctrl+N）：每个窗口独立连接一个串口，'
            '各有自己的日志/波形/频谱页；配合「广播」可同时给多块板子发命令')
        self.btn_new_window.clicked.connect(self._open_new_session_window)
        self.btn_refresh = QtWidgets.QPushButton('刷新串口')
        self.btn_refresh.clicked.connect(self.refresh_ports)

        # ---- ESP32 线控（v1.3.0：连接后才可用；loop:// 等虚拟端口失败即忽略） ----
        # ESP32 自动下载电路：DTR→IO0（启动模式）、RTS→EN（复位）
        self.btn_mc_reset = QtWidgets.QPushButton('复位')
        self.btn_mc_reset.setObjectName('tool')
        self.btn_mc_reset.setToolTip('运行模式复位：EN 拉低再放开（等同按板上 RST 键）')
        self.btn_mc_reset.clicked.connect(self._esp32_reset)
        self.btn_download = QtWidgets.QPushButton('下载模式')
        self.btn_download.setObjectName('tool')
        self.btn_download.setToolTip('进 ROM 下载模式（esptool 经典时序：复位期间拉低 IO0）\n'
                                     '进下载模式后方可烧录；USB-Serial/JTAG 口不适用')
        self.btn_download.clicked.connect(self._esp32_download)
        self.btn_dtr = QtWidgets.QPushButton('DTR')
        self.btn_dtr.setObjectName('tool')
        self.btn_dtr.setCheckable(True)
        self.btn_dtr.setToolTip('手动拉高/松开 DTR（ESP32 电路：IO0 启动模式）')
        self.btn_dtr.toggled.connect(lambda on: self._esp32_line('dtr', on))
        self.btn_rts = QtWidgets.QPushButton('RTS')
        self.btn_rts.setObjectName('tool')
        self.btn_rts.setCheckable(True)
        self.btn_rts.setToolTip('手动拉高/松开 RTS（ESP32 电路：EN 复位）')
        self.btn_rts.toggled.connect(lambda on: self._esp32_line('rts', on))
        self.btn_break = QtWidgets.QPushButton('BREAK')
        self.btn_break.setObjectName('tool')
        self.btn_break.setToolTip('发送串口 BREAK 信号（部分板子靠它进 bootloader / 唤醒）')
        self.btn_break.clicked.connect(self._send_break)
        self.lbl_lines = QtWidgets.QLabel('')
        self.lbl_lines.setObjectName('backlog')
        self.lbl_lines.setToolTip('modem 状态线实时状态（●有效 / ○无效）：CTS DSR RI CD')
        for btn in (self.btn_mc_reset, self.btn_download, self.btn_dtr, self.btn_rts,
                    self.btn_break):
            btn.setEnabled(False)   # 未连接置灰，连接后在 _update_connect_button 里启用

        self.chk_auto_link = QtWidgets.QCheckBox('新设备自动连接')
        self.chk_auto_link.setToolTip('勾选后扫描到新插入的串口设备即自动连接（多个新设备优先 CH340/CH341）')
        self.chk_auto_link.toggled.connect(
            lambda on: write_setting('connect/auto_link', '1' if on else '0'))
        self.lbl_status = ElidedLabel('未连接')   # 长报错省略+悬停全文，不撑宽窗口
        self.lbl_status.setObjectName('status_err')
        self.lbl_stats = QtWidgets.QLabel('RX 0.0 KiB | TX 0.0 KiB')
        self.lbl_stats.setObjectName('backlog')
        # 故障徽标：固件打出 !FAULT 行时点亮（不弹窗不刷屏），点击跳故障诊断页
        self.btn_fault_badge = QtWidgets.QPushButton('⚠ 最近一次异常')
        self.btn_fault_badge.setObjectName('fault_badge')
        self.btn_fault_badge.setToolTip('固件打出了 !FAULT 异常行——点击查看寄存器位域详情')
        self.btn_fault_badge.clicked.connect(
            lambda: self.tabs.setCurrentWidget(self.fault))
        self.btn_fault_badge.hide()
        # 小宽度上限：正常文本（类型+时刻）够用；极端长文本被裁但 tooltip 有全文，
        # 不至于把同行的连接状态（ElidedLabel）与串口选择器挤没（layout_audit 实测）
        self.btn_fault_badge.setMaximumWidth(240)

        top = QtWidgets.QHBoxLayout()
        top.addWidget(QtWidgets.QLabel('串口'))
        top.addWidget(self.combo_port, 2)
        top.addWidget(self.btn_refresh)
        top.addWidget(QtWidgets.QLabel('波特率'))
        top.addWidget(self.combo_baud, 1)
        top.addWidget(self.chk_auto_link)
        top.addWidget(self.btn_connect)
        top.addWidget(self.btn_new_window)
        top.addWidget(self.lbl_status, 2)
        top.addWidget(self.lbl_stats, 1)
        top.addWidget(self.btn_fault_badge)

        # ---- 日志区 + 查找条 + 记录操作行 ----
        # QTextEdit 的 QTextDocument 在 append+裁剪循环下碎片内存不归还（实测 ~1.2KB/行
        # 只增不减，长跑高流量会到 GB 级）；QPlainTextEdit 由 C++ 侧 maximumBlockCount
        # 自动裁剪且内存有界（同载荷 15s +2.5MB vs +50MB），appendHtml 保留彩色着色。
        self.log = LogView()
        self.log.setReadOnly(True)
        self.log.setUndoRedoEnabled(False)
        self.log.setMaximumBlockCount(self._max_log_blocks)
        # 自动折行到视口宽：长行/长 HEX 串不再只出横向滚动条，窗口缩放即时重排；
        # WrapAtWordBoundaryOrAnywhere 优先按空格断，无空格长串（HEX）按字符断。
        self.log.setLineWrapMode(QtWidgets.QPlainTextEdit.LineWrapMode.WidgetWidth)
        self.log.setWordWrapMode(QtGui.QTextOption.WrapMode.WrapAtWordBoundaryOrAnywhere)
        # 关键词高亮/告警：两表缓存 + 原始串脏检查（设置页改完下一行 RX 即生效）
        self._kw_raw: tuple[str, str] | None = None
        self._kw_alert: list[str] = []
        self._kw_watch: list[str] = []
        # 选中即复制（PuTTY 风格，默认关）：设置键 log/copy_on_select
        self.log.selectionChanged.connect(self._copy_selection_maybe)
        # modem 状态线轮询（连接期间 500ms；_on_connected 启动、断开停止）
        self._line_timer = QtCore.QTimer(self)
        self._line_timer.setInterval(500)
        self._line_timer.timeout.connect(self._poll_line_signals)
        self._conn_mono: float | None = None   # elapsed 时间戳基准（连接时刻）
        # RX 洪泛合并：垃圾流（错波特率/调试口乱码）每秒可达数百行，逐事件整行
        # 处理（appendHtml+ANSI 解析+逐段 insertText）会把 GUI 线程钉死在 posted
        # events 里——Windows 5s 泵不到原生消息即幽灵化（标题栏 X 变灰"未响应"，
        # 用户实报）。收进 pending 缓冲按预算逐拍消费，任意两次消费之间必然回到
        # 事件循环泵原生消息；超高水位丢最老并打标（数据永远以自动日志/条目为准）。
        self._rx_pending = bytearray()
        self._rx_dropped = 0               # 本次会话累计丢弃字节数（统计行展示）
        self._rx_drop_stamp = 0.0          # 丢弃 SYS 行限频（1s 一条）
        self._rx_drain_timer = QtCore.QTimer(self)
        self._rx_drain_timer.setSingleShot(True)
        self._rx_drain_timer.setInterval(0)
        self._rx_drain_timer.timeout.connect(self._drain_rx_pending)
        # 注意：这里不能再挂控件级静态 tooltip——Qt 会先弹它，抢掉逐行
        # HEX⇄文本转换提示框（LogHoverTip，0.4s 延迟）。
        self._log_hover = LogHoverTip(self.log)
        # TX 行聊天式右对齐靠文档级 RightTab 制表位（见 _sync_log_tab_stop），
        # 这里记下当前制表位，宽度变了才重设，避免每条日志都触发整篇重排。
        self._log_tab_stop_px = None
        self._sync_log_tab_stop()
        self.find_bar = FindBar(self.log)
        self.find_bar.setVisible(False)

        self.btn_clear_log = QtWidgets.QPushButton('清空记录')
        self.btn_clear_log.setObjectName('tool')
        self.btn_clear_log.clicked.connect(self.clear_log)
        self.btn_export_log = QtWidgets.QPushButton('导出日志')
        self.btn_export_log.setObjectName('tool')
        self.btn_export_log.setToolTip('把当前 RX 日志（全部条目）导出为文本文件')
        self.btn_export_log.clicked.connect(self._export_log)
        self.chk_auto_save = QtWidgets.QCheckBox('自动保存日志')
        self.chk_auto_save.setChecked(True)
        self.chk_auto_save.setToolTip('收发的每条日志实时追加到 logs/serial_auto_log_*.txt（SYS 除外）')
        self.lbl_log_path = QtWidgets.QLabel('自动日志将在首次收到数据后生成到 logs 目录')
        self.lbl_log_path.setObjectName('backlog')

        # ---- ESP-IDF 日志解析（v1.3.0：等级着色 + 复位横幅 + panic/backtrace 收集） ----
        self.chk_idf = QtWidgets.QCheckBox('ESP-IDF 日志解析')
        self.chk_idf.setChecked(read_setting('log/esp_idf_parse', '1') != '0')
        self.chk_idf.setToolTip(
            '识别 ESP-IDF 日志（等级着色）、复位横幅（rst:0x…）与 Guru Meditation 崩溃，\n'
            '并收集 backtrace 地址供「解析 backtrace」翻译成源码位置')
        self.chk_idf.toggled.connect(lambda on: write_setting('log/esp_idf_parse', '1' if on else '0'))
        self._esp32_bt: list[str] = []   # backtrace 地址收集器（复位横幅/清空记录时重置）

        # ---- ANSI 终端渲染（SGR 着色 + 框线/艺术字排版保留） ----
        self.chk_ansi = QtWidgets.QCheckBox('ANSI 渲染')
        self.chk_ansi.setChecked(read_setting('log/ansi_render', '1') != '0')
        self.chk_ansi.setToolTip(
            '渲染串口回传里的 ANSI 转义序列：SGR 颜色（16/256 色/真彩）按设备指定着色，\n'
            '粗体/斜体/下划线/反显生效；空格保排版，框线图与 ASCII 艺术字不塌陷。\n'
            '光标移动/清屏等对滚动日志无意义的序列自动剥除；对比度过低的颜色自动混合保底。\n'
            '对不含转义码的行零影响；HEX 显示模式不走本渲染，二进制协议建议用 HEX 查看。\n'
            '原文条目与自动保存日志不受影响，始终记录原始字节解码结果')
        self.chk_ansi.toggled.connect(lambda on: write_setting('log/ansi_render', '1' if on else '0'))
        self.btn_backtrace = QtWidgets.QPushButton('解析 backtrace')
        self.btn_backtrace.setObjectName('tool')
        self.btn_backtrace.setEnabled(False)
        self.btn_backtrace.setToolTip('把收集到的崩溃地址经 addr2line 翻译成 函数 @ 文件:行')
        self.btn_backtrace.clicked.connect(self._show_backtrace_dialog)

        logbar = QtWidgets.QHBoxLayout()
        logbar.addWidget(self.chk_auto_save)
        logbar.addWidget(self.chk_idf)
        logbar.addWidget(self.chk_ansi)
        logbar.addWidget(self.lbl_log_path, 1)
        # ESP32 线控放日志行右段（顶行宽度有限；连接后可用，tooltip 带语义）
        logbar.addWidget(self.btn_mc_reset)
        logbar.addWidget(self.btn_download)
        logbar.addWidget(self.btn_dtr)
        logbar.addWidget(self.btn_rts)
        logbar.addWidget(self.btn_break)
        logbar.addWidget(self.lbl_lines)
        logbar.addWidget(self.btn_clear_log)
        logbar.addWidget(self.btn_export_log)

        # ---- 底部发送行 ----
        self.combo_rx_format = QtWidgets.QComboBox()
        self.combo_rx_format.addItems(['ASCII', 'HEX'])
        self.combo_rx_format.setToolTip('接收显示格式；切到 HEX 时未成行的残行立即落显')
        self.combo_rx_format.currentTextChanged.connect(self._on_rx_format_changed)

        self.combo_tx_format = QtWidgets.QComboBox()
        self.combo_tx_format.addItems(['ASCII', 'HEX'])

        self.combo_newline = QtWidgets.QComboBox()
        self.combo_newline.addItems(['无', 'LF', 'CR', 'CRLF'])
        self.combo_newline.setCurrentText('LF')

        self.edit_send = HistoryLineEdit()
        self.edit_send.setPlaceholderText('输入要发送的内容，Enter 发送，⬆/⬇ 回翻历史')
        self.edit_send.returnPressed.connect(self.send)
        self.btn_send = QtWidgets.QPushButton('发送')
        self.btn_send.clicked.connect(self.send)

        self.btn_broadcast = QtWidgets.QPushButton('广播')
        self.btn_broadcast.setObjectName('tool')
        self.btn_broadcast.setCheckable(True)
        self.btn_broadcast.setChecked(read_setting('tx/broadcast', '0') in ('1', 'true'))
        self.btn_broadcast.setToolTip(
            '发送时同步广播到其他已连接的会话窗口（各自落 TX 日志、各自计数）；\n'
            '只从本窗口发起一跳，不会二次转发；本窗口未连接时不广播')
        self.btn_broadcast.toggled.connect(
            lambda on: write_setting('tx/broadcast', '1' if on else '0'))

        self.btn_find = QtWidgets.QPushButton('查找')
        self.btn_find.setObjectName('tool')
        self.btn_find.clicked.connect(self.find_bar.open_and_focus)

        self.combo_file_proto = QtWidgets.QComboBox()
        self.combo_file_proto.addItems(['原样', 'XMODEM', 'YMODEM'])
        self.combo_file_proto.setToolTip(
            '文件发送协议：原样=裸字节流（按块直发）；XMODEM=128 字节块 CRC16；'
            'YMODEM=批传输（文件名+大小+1024 字节块，STM32 ROM bootloader / U-Boot loady）')
        self.btn_file = QtWidgets.QPushButton('文件发送')
        self.btn_file.setObjectName('tool')
        self.btn_file.clicked.connect(self.toggle_file_send)

        self.btn_seq = QtWidgets.QPushButton('序列')
        self.btn_seq.setObjectName('tool')
        self.btn_seq.setToolTip('序列发送：send/sendhex/delay 脚本，支持重复/循环/连接后自动执行')
        self.btn_seq.clicked.connect(self._open_txseq)
        self._txseq_dialog: TxSequenceDialog | None = None

        self.lbl_backlog = QtWidgets.QLabel('积压 0 B')
        self.lbl_backlog.setObjectName('backlog')

        bottom = QtWidgets.QHBoxLayout()
        bottom.addWidget(QtWidgets.QLabel('收'))
        bottom.addWidget(self.combo_rx_format)
        bottom.addWidget(QtWidgets.QLabel('发'))
        bottom.addWidget(self.combo_tx_format)
        bottom.addWidget(QtWidgets.QLabel('换行'))
        bottom.addWidget(self.combo_newline)
        bottom.addWidget(self.edit_send, 3)
        bottom.addWidget(self.btn_send)
        bottom.addWidget(self.btn_broadcast)
        bottom.addWidget(self.btn_find)
        bottom.addWidget(self.btn_file)
        bottom.addWidget(self.btn_seq)
        bottom.addWidget(self.lbl_backlog, 1)

        terminal = QtWidgets.QWidget()
        terminal_layout = QtWidgets.QVBoxLayout(terminal)
        terminal_layout.setContentsMargins(0, 0, 0, 0)
        terminal_layout.addLayout(top)
        terminal_layout.addLayout(logbar)
        terminal_layout.addWidget(self.find_bar)
        terminal_layout.addWidget(self.log, 1)
        terminal_layout.addLayout(bottom)

        # ---- 页签：终端 / 波形 / 频谱 / Modbus / STM32 / 设备屏 / 设置 / 关于 ----
        self.wave = WaveWindow()
        # 场景预设要读写主窗体波特率（只改选择不重连，对齐主线）
        self.wave.baud_getter = self.combo_baud.currentText
        self.wave.baud_setter = self.combo_baud.setCurrentText
        self.spectrum = SpectrumPanel(self.wave.state)
        self.modbus = ModbusPanel()
        self.modbus.transport = self.modbus_transport
        self.modbus.log_fn = self._append_log
        self.stm32 = Stm32Panel()
        self.stm32.log_fn = self._append_log
        self.fault = FaultPanel()   # 故障诊断页：ingest 在 _append_log RX 分支喂数
        self.device_screen = DeviceScreenPage()   # v2 M2：设备屏（UDP H.264 投屏，只读）
        self.settings_page = SettingsPage(self.wave)
        self.settings_page.chk_on_top.toggled.connect(self._apply_on_top)
        self._apply_on_top(self.settings_page.chk_on_top.isChecked())
        self._register_session_window()
        sc_new = QtGui.QShortcut(QtGui.QKeySequence.StandardKey.New, self)
        sc_new.activated.connect(self._open_new_session_window)
        self.about_page = self._build_about_page()
        self.tabs = QtWidgets.QTabWidget()
        self.tabs.addTab(terminal, '终端')
        self.tabs.addTab(self.wave, '波形')
        self.tabs.addTab(self.spectrum, '频谱')
        self.tabs.addTab(self.modbus, 'Modbus')
        self.tabs.addTab(self.stm32, 'STM32')
        self.tabs.addTab(self.fault, '故障诊断')
        self.tabs.addTab(self.device_screen, 'Linux 支持')
        self.tabs.addTab(self.settings_page, '设置')
        self.tabs.addTab(self.about_page, '关于')
        self.tabs.setObjectName('main_tabs')
        self.setCentralWidget(self.tabs)
        # 页面显隐（设置页记忆，v1.3.0）：STM32 / Linux 支持可关，重启保持
        self.settings_page.chk_show_stm32.toggled.connect(lambda _on: self._apply_page_visibility())
        self.settings_page.chk_show_screen.toggled.connect(lambda _on: self._apply_page_visibility())
        self._apply_page_visibility()

        find_shortcut = QtGui.QShortcut(QtGui.QKeySequence('Ctrl+F'), terminal)
        find_shortcut.activated.connect(self.find_bar.open_and_focus)

        self.flush_timer = QtCore.QTimer(self)
        self.flush_timer.timeout.connect(self.on_flush_tick)
        self.flush_timer.start(self.FLUSH_INTERVAL_MS)

        self.file_timer = QtCore.QTimer(self)
        self._xfer = None   # XMODEM/YMODEM 协议发送状态 {'engine','name'}
        self._xfer_timer = QtCore.QTimer(self)
        self._xfer_timer.setInterval(200)
        self._xfer_timer.timeout.connect(self._xfer_tick)
        self.file_timer.timeout.connect(self._file_send_tick)
        self.file_timer.setInterval(FILE_INTERVAL_MS)

        self.refresh_ports()
        self._wire_settings_memory()
        self._restore_connect_settings()
        self.start_port_watch()

        # ---- 帮助菜单：检查更新 / 回放录制 / 关于 ----
        menu_help = self.menuBar().addMenu('帮助')
        act_check = QtGui.QAction('检查更新', self)
        act_check.triggered.connect(lambda: self._run_update_check(manual=True))
        menu_help.addAction(act_check)
        act_replay = QtGui.QAction('回放会话录制…', self)
        act_replay.setToolTip('选择录制生成的 JSONL 文件，把当时的字节流重放进接收管线')
        act_replay.triggered.connect(self._replay_from_dialog)
        menu_help.addAction(act_replay)
        act_about = QtGui.QAction('关于', self)
        act_about.triggered.connect(self._show_about)
        menu_help.addAction(act_about)
        # AI 控制通道（TCP JSON 行协议，默认开启；端口被占则提示但不影响工具）
        self._control_api = self._build_control_api()
        self._control_channel = ControlChannel(self._control_api, parent=self)
        if self._control_channel.start():
            self._append_log('SYS',
                             f'AI 控制通道已开启: tcp://127.0.0.1:{self._control_channel.port}'
                             '（JSON 行协议: ping/help/status/send/read_lines[+follow]/'
                             'wave_snapshot/spectrum_snapshot/xy_snapshot/modbus_recent/'
                             'stm32_status/settings_*，命令表发 {"cmd":"help"}；'
                             '详见 AI_INTERFACE.md）')
        else:
            self._append_log('SYS', 'AI 控制通道端口被占用，未开启（可能已有实例在运行）')
        self.act_copy_ai = QtGui.QAction('复制 AI 接入信息', self)
        self.act_copy_ai.setToolTip(
            '把控制通道地址、JSON 命令一览与无头模式用法复制到剪贴板，'
            '直接粘贴给其他 AI/脚本即可接入')
        self.act_copy_ai.triggered.connect(self._copy_ai_info)
        menu_help.addAction(self.act_copy_ai)
        self._update_check_fn = check_app_update  # 测试可注入替换

        # 启动更新检查：24h 节流（对齐主线），延迟 3s 等界面稳定
        QtCore.QTimer.singleShot(3000, self._maybe_auto_update_check)

    # ---------- 设置记忆（gui_settings_qt.ini：口/波特率/收发格式/换行/自动连接） ----------

    def _apply_page_visibility(self):
        """按设置页开关显隐可选页签（STM32 / Linux 支持），隐藏页对象保留不销毁。"""
        vis = ((self.stm32, self.settings_page.chk_show_stm32.isChecked()),
               (self.device_screen, self.settings_page.chk_show_screen.isChecked()))
        for page, shown in vis:
            idx = self.tabs.indexOf(page)
            if idx >= 0:
                self.tabs.setTabVisible(idx, shown)

    def _wire_settings_memory(self):
        self.combo_baud.currentTextChanged.connect(
            lambda text: write_setting('connect/baud', text))
        self.combo_rx_format.currentTextChanged.connect(
            lambda text: write_setting('connect/rx_format', text))
        self.combo_tx_format.currentTextChanged.connect(
            lambda text: write_setting('connect/tx_format', text))
        self.combo_newline.currentTextChanged.connect(
            lambda text: write_setting('connect/newline', text))

    def _restore_connect_settings(self):
        for key, combo in (('connect/rx_format', self.combo_rx_format),
                           ('connect/tx_format', self.combo_tx_format),
                           ('connect/newline', self.combo_newline)):
            saved = read_setting(key, '')
            if saved and combo.findText(saved) >= 0:
                combo.setCurrentText(saved)
        baud = read_setting('connect/baud', '')
        if baud.isdigit():
            self.combo_baud.setCurrentText(baud)
        if read_setting('connect/auto_link', '0') in ('1', 'true', 'yes'):
            self.chk_auto_link.setChecked(True)
        # 端口在设备列表刷新后才可能存在，最后恢复
        saved_port = read_setting('connect/port', '')
        if saved_port:
            idx = self.combo_port.findText(saved_port, QtCore.Qt.MatchStartsWith)
            if idx >= 0:
                self.combo_port.setCurrentIndex(idx)

    def _save_connect_settings(self):
        write_setting('connect/baud', self.combo_baud.currentText())
        write_setting('connect/rx_format', self.combo_rx_format.currentText())
        write_setting('connect/tx_format', self.combo_tx_format.currentText())
        write_setting('connect/newline', self.combo_newline.currentText())
        write_setting('connect/auto_link', '1' if self.chk_auto_link.isChecked() else '0')
        port = port_name_from_display(self.combo_port.currentText())
        if port:
            write_setting('connect/port', port)

    # ---------- 检测更新 ----------

    UPDATE_CHECK_INTERVAL_S = 24 * 3600

    def _maybe_auto_update_check(self):
        try:
            last = float(read_setting('update/last_check', '0') or 0)
        except ValueError:
            last = 0.0
        if time.time() - last < self.UPDATE_CHECK_INTERVAL_S:
            return
        write_setting('update/last_check', str(time.time()))
        self._run_update_check(manual=False)

    def _run_update_check(self, manual: bool):
        version = APP_VERSION
        runnable = _UpdateCheckRunnable(self._update_check_fn, version)
        runnable.signals.done.connect(
            lambda result: self._on_update_result(result, manual))
        QtCore.QThreadPool.globalInstance().start(runnable)

    def _on_update_result(self, result: dict, manual: bool):
        if result.get('has_update'):
            self._append_log('SYS', f"发现新版本 {result['latest']}"
                                    f"（当前 v{APP_VERSION}），下载: {result['page_url']}")
            self.set_status(f"有新版本 {result['latest']}（当前 v{APP_VERSION}）", 'status_ok')
            return
        if manual and result.get('ok'):
            self._append_log('SYS', f"已是最新版本（v{APP_VERSION}）")
        elif manual:
            self._append_log('SYS', f"检查更新失败: {result.get('note', '')}")
        # 自动检查失败静默（对齐主线）

    def _replay_from_dialog(self):
        path, _ = QtWidgets.QFileDialog.getOpenFileName(
            self, '选择会话录制文件', get_log_dir(), '会话录制 (*.jsonl);;所有文件 (*.*)')
        if path:
            self._start_replay(path)

    # ---------- AI 控制通道 ----------

    def _build_control_api(self) -> ControlAPI:
        """把主窗体能力包成控制通道可调用集（对齐主线 handle_control_request 的宿主面）。"""
        import re as _re

        from core.ports import port_display_text, scan_port_infos
        from core.wave_state import PROTOCOLS

        def status() -> dict:
            return {
                'connected': bool(self.worker),
                'port': self._connected_device,
                'baud': (self.worker.baud if self.worker else 0),
                'protocol': self.wave.state.protocol,
                'frame_total': self.wave.state.frame_total,
                'frame_count': self.wave.state.frame_count,
                'channels': len(self.wave.state.ch_values),
                'rx_entries': len(self.message_entries),
                'recording': self.recorder.active,
                'sim_on': self.wave.btn_sim.isChecked(),
            }

        def list_ports() -> list:
            return [port_display_text(d, desc) for d, desc in scan_port_infos()]

        def send(text: str, hex: bool, newline: str) -> dict:  # noqa: A002 (协议字段名)
            if not self.worker:
                raise RuntimeError('串口未连接。')
            if hex:
                payload = try_parse_hex_text(text) + NEWLINE_BYTES.get(newline, b'')
                if not payload:
                    raise ValueError('HEX 输入无效')
                display = bytes_to_hex_text(payload)
            else:
                payload = (text + NEWLINE_TEXT.get(newline, '')).encode('utf-8')
                if not payload:
                    raise ValueError('空内容')
                display = text + NEWLINE_TEXT.get(newline, '')
            if not self.worker.write_bytes(payload):
                raise RuntimeError('串口写入失败。')
            self._count_tx_bytes(len(payload))
            self._append_log('TX', display.replace('\r', '<CR>').replace('\n', '<LF>'))
            return {'ok': True}

        def read_lines(count: int = 20, filter: str = '',  # noqa: A002 (协议字段名)
                       since: int = -1) -> dict:
            """读最近记录。since<0 = 快照（最新 count 行）；since>=0 = follow 推送
            （只回 seq>since 的新行，时间升序、count 上限，cursor=最后交付条目序号）。"""
            regex = _re.compile(filter) if filter else None
            if since < 0:
                start = max(0, len(self.message_entries) - count)
                lines = []
                for entry in self.message_entries[start:]:
                    if regex is not None and not regex.search(entry['text']):
                        continue
                    lines.append({
                        't': entry['ts'].strftime('%Y-%m-%d %H:%M:%S.%f')[:-3],
                        'dir': entry['direction'],
                        'text': entry['text'],
                    })
                return {'ok': True, 'lines': lines, 'cursor': self._log_seq}
            lines: list[dict] = []
            cursor = since
            for entry in self.message_entries:
                if entry.get('seq', 0) <= since:
                    continue
                if regex is not None and not regex.search(entry['text']):
                    continue
                if len(lines) >= max(1, count):
                    break  # 超出单次推送上限的剩余行下一轮再推
                lines.append({
                    't': entry['ts'].strftime('%Y-%m-%d %H:%M:%S.%f')[:-3],
                    'dir': entry['direction'],
                    'text': entry['text'],
                })
                cursor = entry.get('seq', cursor)
            return {'ok': True, 'lines': lines, 'cursor': cursor}

        def wave_snapshot(count: int) -> dict:
            st = self.wave.state
            take = min(max(1, count), st.frame_count)
            channels = [{'name': st.channel_display_name(c),
                         'enabled': bool(st.enabled_channels[c])
                         if c < len(st.enabled_channels) else True,
                         'values': list(st.snapshot(c, take))}
                        for c in range(len(st.ch_values))]
            return {'ok': True, 'protocol': st.protocol, 'frame_total': st.frame_total,
                    'frame_count': st.frame_count, 'paused': st.paused,
                    'window_frames': int(st.window_frames),
                    'count': take, 'channels': channels}

        def set_protocol(protocol: str) -> dict:
            if protocol not in PROTOCOLS:
                return {'ok': False, 'error': 'protocol 必须是 FireWater/JustFloat/RawData'}
            self.wave.combo_protocol.setCurrentText(protocol)
            return {'ok': True, 'protocol': self.wave.state.protocol}

        def wave_clear() -> dict:
            self.wave.clear_buffers()
            return {'ok': True}

        def wave_sim(on: bool) -> dict:
            self.wave.btn_sim.setChecked(on)
            return {'ok': True, 'sim_on': self.wave.btn_sim.isChecked()}

        def record(on: bool) -> dict:
            self.toggle_session_record(on)
            return {'ok': True, 'recording': self.recorder.active,
                    'file': self.recorder.path}

        # ---- 数据获取扩展（频谱 / XY / Modbus / STM32）----

        def spectrum_snapshot(count: int = 0) -> dict:
            """频谱快照：FFT 频点+dB（count>0 截高频端最新 N 点）、主频、当前参数、
            瀑布最近帧与累计采集时长（数据全部来自频谱页公开属性）。"""
            import numpy as _np

            from core.dsp import max_bin as _max_bin
            sp = self.spectrum
            out = {'ok': True, 'frozen': sp.frozen,
                   'acquisition_seconds': round(sp.acquisition_seconds, 3),
                   'peak_hold': sp.chk_peak.isChecked(),
                   'average': sp.chk_avg.isChecked(),
                   'waterfall_on': sp.chk_wf.isChecked(),
                   'waterfall_rows': len(getattr(sp, '_waterfall', []))}
            analysis = sp.last_analysis
            if analysis is None:
                out.update({'empty': True, 'params': {}, 'freqs_hz': [], 'db': [],
                            'note': '暂无有效频谱数据（频谱页未收到数据或刚清空）'})
                return out
            db = analysis['db']
            take = max(1, min(int(count), db.size)) if count and count > 0 else db.size
            db_cut = db[-take:]
            freqs_cut = _np.arange(db.size) * analysis['df']
            freqs_cut = freqs_cut[-take:]
            peak_idx = int(_max_bin(db, 1))  # 跳过 DC 的主频 bin
            out.update({
                'empty': False,
                'params': {'sample_rate_hz': float(analysis['fs']),
                           'fft_points': int(analysis['used']),
                           'window': analysis['window'],
                           'df_hz': float(analysis['df']),
                           'rbw_enbw_hz': float(analysis['rbw']),
                           'channel': int(analysis['channel']),
                           'channel_name': self.wave.state.channel_display_name(
                               analysis['channel']),
                           'ref_db': sp._read_scale()[0],
                           'div_db': float(sp.combo_div.currentText())},
                'peak': {'freq_hz': peak_idx * analysis['fs'] / analysis['used'],
                         'db': float(db[peak_idx])},
                'total_bins': int(db.size), 'count': int(take),
                'freq_start_hz': float(freqs_cut[0]) if take else 0.0,
                'freqs_hz': [round(float(x), 6) for x in freqs_cut.tolist()],
                'db': [round(float(x), 3) for x in db_cut.tolist()],
                'note': 'db=FFT 幅度谱 dB（频率升序）；waterfall_latest=瀑布最新帧'
                        '（每频点 [R,G,B] 0..255，与瀑布图渲染同源；同口径截取）'})
            wf = getattr(sp, '_waterfall', [])
            if wf:
                row = wf[0][-take:]  # 面板口径 _waterfall[0]=最新帧（最新在顶）；行=uint8[n,3] 每频点 RGB
                out['waterfall_latest'] = [list(map(int, triple)) for triple in row.tolist()]
            return out

        def xy_snapshot(count: int = 512) -> dict:
            """XY 相位图各窗口最新轨迹（源通道最新 N 点）与窗口参数。"""
            xy = getattr(self.wave, 'xy_section', None)
            if xy is None:
                return {'ok': False, 'error': 'XY 相位图未就绪'}
            st = self.wave.state
            take = max(1, min(int(count), max(1, st.frame_count)))
            windows = []
            for slot, cell in enumerate(getattr(xy, '_cells', [])):
                windows.append({
                    'window': slot + 1,
                    'x_channel': int(cell.x_ch), 'y_channel': int(cell.y_ch),
                    'x_name': st.channel_display_name(cell.x_ch),
                    'y_name': st.channel_display_name(cell.y_ch),
                    'span': cell.span_option, 'divs': int(cell.divs),
                    'draw_mode': cell.draw_mode,
                    'persist': getattr(cell, '_persist_option', ''),
                    'x': st.snapshot(cell.x_ch, take),
                    'y': st.snapshot(cell.y_ch, take)})
            return {'ok': True, 'xy_on': self.wave.btn_xy.isChecked(),
                    'window_count': len(windows), 'windows': windows,
                    'note': 'x/y 为各窗口源通道最新 take 点（时间升序）'}

        def modbus_recent(count: int = 20) -> dict:
            hist = getattr(self, '_modbus_recent', [])
            take = hist[-max(1, int(count)):]
            return {'ok': True, 'transactions': take,
                    'note': '时间升序；ok=false 表示无响应/超时；req_hex/resp_hex 为线上帧'}

        def stm32_status() -> dict:
            panel = getattr(self, 'stm32', None)
            if panel is None:
                return {'ok': True, 'available': False,
                        'note': 'STM32 页未挂接到主窗体（页未就绪）'}
            fn = getattr(panel, 'ai_status', None)  # 页面未来可提供更丰富的状态口
            if callable(fn):
                try:
                    snap = dict(fn() or {})
                except Exception as exc:
                    return {'ok': False, 'error': f'STM32 页状态读取失败: {exc}'}
                if not snap.get('cli_found', False):
                    note = '未找到 STM32CubeProgrammer CLI（设置页配置路径后重试）'
                elif not snap.get('connected', False):
                    note = 'CLI 就绪，尚未连接目标（STM32 页「连接目标」后可用）'
                else:
                    note = '已连接目标（连接状态与 STM32 页一致）'
                return {'ok': True, 'available': True, 'note': note, **snap}
            return {'ok': True, 'available': True,
                    'connected': bool(getattr(panel, 'connected', False)),
                    'busy': bool(getattr(panel, 'busy', False)),
                    'note': 'STM32 页已挂接（未提供 ai_status() 时返回基础连接状态）'}

        # ---- 设置读取 / 修改（改后 UI 控件与内部状态一致，返回改前/改后值）----

        def settings_snapshot() -> dict:
            return {
                'baud': self.combo_baud.currentText(),
                'port': self.combo_port.currentText(),
                'connected': bool(self.worker),
                'protocol': self.wave.state.protocol,
                'theme': theme.current_theme(),
                'points': self.wave.combo_window.currentText(),
                'fill': self.settings_page.chk_fill.isChecked(),
                'paused': self.wave.btn_pause.isChecked(),
                'auto_y': self.wave.chk_auto_y.isChecked(),
                'filter': self.wave.combo_filter.currentText(),
                'filter_param': self.wave.spin_filter_param.value(),
                'rx_format': self.combo_rx_format.currentText(),
                'tx_format': self.combo_tx_format.currentText(),
                'newline': self.combo_newline.currentText(),
                'sim_on': self.wave.btn_sim.isChecked(),
                'recording': self.recorder.active,
            }

        def settings_list() -> dict:
            return {'ok': True, 'settings': settings_snapshot()}

        def settings_get(key: str) -> dict:
            snap = settings_snapshot()
            k = _SETTING_ALIASES.get(key.strip().lower(), key.strip().lower())
            if k in snap:
                return {'ok': True, 'key': k, 'value': snap[k]}
            return {'ok': False,
                    'error': f'未知设置键: {key!r}（用 settings_list 查看全部键名）'}

        def settings_set(key: str, value) -> dict:
            k = _SETTING_ALIASES.get(str(key).strip().lower(), str(key).strip())
            if k == 'baud':
                if self.worker:
                    return {'ok': False,
                            'error': '已连接：请先断开再修改波特率（连接期间 UI 已锁定）'}
                text = str(value).strip()
                if not text.isdigit() or int(text) <= 0:
                    return {'ok': False, 'error': f'波特率必须是正整数: {value!r}'}
                before = self.combo_baud.currentText()
                self.combo_baud.setCurrentText(text)  # 下拉跟随；信号链写设置记忆
                return {'ok': True, 'key': 'baud', 'before': before,
                        'after': self.combo_baud.currentText()}
            if k == 'port':
                if self.worker:
                    return {'ok': False,
                            'error': '已连接：请先断开再修改串口（连接期间 UI 已锁定）'}
                text = str(value).strip()
                idx = self.combo_port.findText(text, QtCore.Qt.MatchStartsWith)
                if idx < 0:
                    if '://' not in text:
                        return {'ok': False,
                                'error': f'没有串口匹配 {text!r}（可先 list_ports 查看）'}
                    # 网络串口 URL（rfc2217://、socket://…）：不进扫描列表，直接接受
                    text_before = self.combo_port.currentText()
                    self.combo_port.setCurrentText(text)
                    return {'ok': True, 'key': 'port', 'before': text_before,
                            'after': self.combo_port.currentText()}
                before = self.combo_port.currentText()
                self.combo_port.setCurrentIndex(idx)
                return {'ok': True, 'key': 'port', 'before': before,
                        'after': self.combo_port.currentText()}
            if k == 'protocol':
                before = self.wave.combo_protocol.currentText()
                result = set_protocol(str(value))
                if not result.get('ok'):
                    return result
                return {'ok': True, 'key': 'protocol', 'before': before,
                        'after': self.wave.state.protocol}
            if k == 'theme':
                want = str(value).strip().lower()
                want = {'深色': 'dark', '浅色': 'light'}.get(want, want)
                if want not in theme.THEME_LABELS:
                    return {'ok': False, 'error': 'theme 必须是 dark/light（深色/浅色）'}
                before = theme.current_theme()
                idx = self.settings_page.combo_theme.findData(want)
                if idx < 0:
                    return {'ok': False, 'error': f'主题下拉缺少选项: {want}'}
                self.settings_page.combo_theme.setCurrentIndex(idx)  # 信号链：写设置+全页换肤
                return {'ok': True, 'key': 'theme', 'before': before,
                        'after': theme.current_theme()}
            if k == 'points':
                try:
                    n = int(str(value).strip())
                except ValueError:
                    return {'ok': False, 'error': f'显示点数必须是整数: {value!r}'}
                before = self.wave.combo_window.currentText()
                # 文档口径：钳制 50..200000（与 wave_state.MIN/MAX_WINDOW 同源）
                n = max(self.wave.state.MIN_WINDOW, min(n, self.wave.state.MAX_WINDOW))
                self.wave.combo_window.setCurrentText(str(n))  # → state.set_view_width
                return {'ok': True, 'key': 'points', 'before': before,
                        'after': self.wave.combo_window.currentText()}
            if k == 'fill':
                want = _to_bool(value)
                if want is None:
                    return {'ok': False, 'error': f'fill 必须是 true/false: {value!r}'}
                before = self.settings_page.chk_fill.isChecked()
                self.settings_page.chk_fill.setChecked(want)  # 信号链：写设置键+波形页生效
                return {'ok': True, 'key': 'fill', 'before': before,
                        'after': self.settings_page.chk_fill.isChecked()}
            if k == 'paused':
                want = _to_bool(value)
                if want is None:
                    return {'ok': False, 'error': f'paused 必须是 true/false: {value!r}'}
                before = self.wave.btn_pause.isChecked()
                self.wave.btn_pause.setChecked(want)  # 信号链：state.paused+按钮「暂停/继续」
                return {'ok': True, 'key': 'paused', 'before': before,
                        'after': self.wave.btn_pause.isChecked()}
            return {'ok': False,
                    'error': f'设置键 {key!r} 不可写（可写: baud/port/protocol/theme/'
                             'points/fill/paused；只读见 settings_list）'}

        return ControlAPI(status=status, list_ports=list_ports, send=send,
                          read_lines=read_lines, wave_snapshot=wave_snapshot,
                          set_protocol=set_protocol, wave_clear=wave_clear,
                          wave_sim=wave_sim, record=record,
                          spectrum_snapshot=spectrum_snapshot, xy_snapshot=xy_snapshot,
                          modbus_recent=modbus_recent, stm32_status=stm32_status,
                          settings_list=settings_list, settings_get=settings_get,
                          settings_set=settings_set)

    def _copy_ai_info(self):
        """一键复制 AI 接入信息（控制通道 + 无头模式），粘贴给其他 AI 即可用。"""
        port = self._control_channel.port if self._control_channel.listening else 9101
        QtWidgets.QApplication.clipboard().setText(get_control_channel_doc(port))
        self._append_log('SYS', '已复制 AI 接入信息到剪贴板，可直接粘贴给其他 AI/脚本')

    def _build_about_page(self) -> QtWidgets.QWidget:
        """关于页签：品牌（LiuScope/柳镜）/版本（读 core.APP_VERSION，不在此维护）/
        作者/许可证/仓库/简介。"""
        page = QtWidgets.QWidget()
        lay = QtWidgets.QVBoxLayout(page)
        lay.setSpacing(8)
        lay.addStretch(1)
        title = QtWidgets.QLabel(
            f'<span style="font-size:16pt; font-weight:bold">{APP_NAME}</span>'
            f'<span style="font-size:12pt"> v{APP_VERSION}</span>')
        title.setAlignment(QtCore.Qt.AlignmentFlag.AlignHCenter)
        alias = QtWidgets.QLabel('别名: 柳镜（原 serialscope）')
        intro = QtWidgets.QLabel(
            '串口调试助手：终端收发、实时波形、频谱分析与 Modbus 调试一体的 Qt 实现。')
        author = QtWidgets.QLabel('作者: 折柳')
        license_lbl = QtWidgets.QLabel('许可证: GPL-3.0')
        repo = QtWidgets.QLabel(f'仓库: <a href="{REPO_URL}">{REPO_URL}</a>')
        repo.setOpenExternalLinks(True)
        repo.setStyleSheet(f'a {{ color: {theme.color("rx")}; }}')  # 深色底下默认链蓝偏暗，随主题提亮
        for lbl in (title, alias, intro, author, license_lbl, repo):
            lbl.setAlignment(QtCore.Qt.AlignmentFlag.AlignHCenter)
            lay.addWidget(lbl)
        lay.addStretch(1)
        return page

    def _show_about(self):
        QtWidgets.QMessageBox.about(
            self, f'关于 {APP_NAME}',
            f'<b>{APP_NAME}</b>（柳镜） v{APP_VERSION}<br>'
            f'v1.2.0 起更名为 LiuScope（中文别名「柳镜」）；'
            f'v1.1.0 起 Qt 实现转正为主线实现'
            f'（PowerShell 实现封版于 v1.0.0，存档移至 ps-legacy 分支）。<br>'
            f'作者: 折柳<br>'
            f'仓库: <a href="{REPO_URL}">{REPO_URL}</a><br>'
            f'发布页: <a href="{RELEASES_PAGE}">{RELEASES_PAGE}</a><br>'
            f'许可证: GPL-3.0')

    # ---------- 串口 / 热插拔 / 设置记忆 ----------

    PORT_WATCH_INTERVAL_MS = 2000  # 主线同款 2s 轮询

    def _open_new_session_window(self):
        """开新会话窗口（Ctrl+N）：独立 MainWindow 实例 = 独立串口会话。"""
        win = MainWindow()
        win.show()
        return win

    def _register_session_window(self):
        """加入多会话注册表并按序号起标题（会话 1/2/...）。"""
        _LIVE_WINDOWS.append(self)
        self.setWindowTitle(f'{APP_NAME} v{APP_VERSION} — 会话 {len(_LIVE_WINDOWS)}')

    def refresh_ports(self):
        infos = scan_port_infos_full()
        keep = port_name_from_display(self.combo_port.currentText())
        self.combo_port.clear()
        for device, desc, label in infos:
            self.combo_port.addItem(port_display_text_full(device, desc, label))
        if keep:
            idx = self.combo_port.findText(keep, QtCore.Qt.MatchStartsWith)
            if idx >= 0:
                self.combo_port.setCurrentIndex(idx)
            elif '://' in keep:
                # 手输的网络串口 URL 不在扫描列表里，刷新后保持（v2 M1）
                self.combo_port.setCurrentText(keep)

    def start_port_watch(self):
        """首扫建基线（已插着的设备不触发自动连接）→ 启动 2s 热插拔轮询。"""
        self._known_port_names = [d for d, _ in scan_port_infos()]
        self._port_watch_timer = QtCore.QTimer(self)
        self._port_watch_timer.setInterval(self.PORT_WATCH_INTERVAL_MS)
        self._port_watch_timer.timeout.connect(self._port_watch_tick)
        self._port_watch_timer.start()

    def _port_watch_tick(self):
        infos = scan_port_infos()
        names = [d for d, _ in infos]
        if names == self._known_port_names:
            return
        new_devices = ([(d, desc) for d, desc in infos if d not in self._known_port_names]
                       if self._known_port_names is not None else [])
        self._known_port_names = names
        self.refresh_ports()
        self._maybe_auto_connect(new_devices)

    def _maybe_auto_connect(self, new_devices: list[tuple[str, str]]):
        if not new_devices or not self.chk_auto_link.isChecked():
            return
        target = pick_auto_connect_port(new_devices)
        if not target:
            return
        desc = next((d for d, desc in new_devices if d == target), '')
        self._append_log('SYS', f'检测到新串口设备 {port_display_text(target, desc)}，尝试自动连接…')
        idx = self.combo_port.findText(target, QtCore.Qt.MatchStartsWith)
        if idx >= 0:
            self.combo_port.setCurrentIndex(idx)
        if self.worker and port_name_from_display(self.combo_port.currentText()) == \
                self._connected_device:
            return  # 已经连着同一个口
        try:
            self.open_connection()
        except Exception as exc:
            self._append_log('SYS', f'自动连接失败: {exc}')

    def toggle_connect(self):
        if self.worker:
            self._teardown_worker()
            self.set_status('未连接', 'status_err')
            return
        if not self.combo_port.currentText():
            self.set_status('没有可用串口', 'status_err')
            return
        try:
            self.open_connection()
        except (ValueError, OSError) as exc:
            self.set_status(f'打开失败: {exc}', 'status_err')
            self._log_open_hint(str(exc))

    def open_connection(self):
        """按当前选口/波特率打开串口（手动连接与热插拔自动连接共用）。"""
        device = port_name_from_display(self.combo_port.currentText())
        if not device:
            self.set_status('没有可用串口', 'status_err')
            return
        try:
            baud = int(self.combo_baud.currentText())
        except ValueError:
            self.set_status('波特率必须是数字', 'status_err')
            return
        if self.worker:  # 换口重连：先拆旧连接
            self._teardown_worker()
        neutral = read_setting('connect/dtr_rts_neutral', '0') in ('1', 'true', 'yes')
        auto_re = read_setting('connect/auto_reconnect', '0') in ('1', 'true', 'yes')
        self.worker = SerialWorker(device, baud, self,
                                   dtr_rts_neutral=neutral, auto_reconnect=auto_re)
        self._update_connect_button()
        self._connected_device = device
        self.worker.got_bytes.connect(self.ingest_bytes)
        self.worker.open_failed.connect(self._on_open_failed)
        self.worker.lost.connect(lambda msg: (self._teardown_worker(),
                                              self.set_status(f'连接丢失: {msg}', 'status_err'),
                                              self._stop_file_send('串口已断开')))
        # 自动重连路径：丢线不拆 worker（线程在重试），重开成功后回到正常收发
        self.worker.port_lost.connect(self._on_port_lost)
        self.worker.reconnected.connect(self._on_reconnected)
        self.worker.opened.connect(lambda: self.set_status(f'已连接 {device} @ {baud}', 'status_ok'))
        self.worker.opened.connect(self._on_connected)
        self.worker.start()
        write_setting('connect/port', device)
        write_setting('connect/baud', str(baud))

    def _on_open_failed(self, msg: str):
        """打开失败（含工作线程异步打开失败）：提示 + 平台处置提示。"""
        self._teardown_worker()
        self.set_status(f'打开失败: {msg}', 'status_err')
        self._log_open_hint(msg)

    def _log_open_hint(self, exc_text: str):
        """按平台/错误文本给一句可执行的处置提示（Linux 权限/占用、Windows 占用）。"""
        hint = port_open_error_hint(exc_text, sys.platform)
        if hint:
            self._append_log('SYS', f'处置提示: {hint}')

    def _on_port_lost(self, msg: str):
        """自动重连开启时的丢线通知：状态栏提示，保留 worker 让线程继续重试。"""
        self._stop_file_send('串口已断开')
        self.set_status(f'连接丢失: {msg}（等待自动重连…）', 'status_err')
        self._append_log('SYS', f'连接丢失: {msg}（自动重连中，每 2s 重试）')

    def _on_reconnected(self):
        device, baud = self._connected_device, (self.worker.baud if self.worker else 0)
        self.set_status(f'已重连 {device} @ {baud}', 'status_ok')
        self._append_log('SYS', f'串口已自动重连: {device} @ {baud}')

    # ---------- ESP32 线控（复位 / 下载模式 / DTR / RTS） ----------

    def _esp32_reset(self):
        if not (self.worker and self.worker.ctrl_reset()):
            self.set_status('复位失败（虚拟端口或 USB-Serial/JTAG 不支持线控）', 'status_err')
        else:
            self._append_log('SYS', '已发送复位时序（EN 脉冲）')

    def _esp32_download(self):
        if not (self.worker and self.worker.ctrl_download()):
            self.set_status('进下载模式失败（虚拟端口或 USB-Serial/JTAG 不支持线控）', 'status_err')
        else:
            self._append_log('SYS', '已发送下载模式时序（芯片将进 ROM bootloader）')

    def _esp32_line(self, which: str, on: bool):
        if not self.worker:
            return
        fn = self.worker.set_dtr if which == 'dtr' else self.worker.set_rts
        if not fn(on):
            self.set_status(f'{which.upper()} 切换失败（端口不支持线控）', 'status_err')

    def _send_break(self):
        """发送 BREAK：驱动不支持/未连接时按现有线控口径给状态栏提示。"""
        if self.worker and self.worker.send_break():
            self._append_log('SYS', '已发送 BREAK 信号')
        else:
            self.set_status('BREAK 发送失败（未连接或端口不支持）', 'status_err')

    def _poll_line_signals(self):
        sigs = self.worker.line_signals() if self.worker else None
        if not sigs:
            self.lbl_lines.setText('')
            return
        self.lbl_lines.setText(
            'CTS' + ('●' if sigs['cts'] else '○')
            + ' DSR' + ('●' if sigs['dsr'] else '○')
            + ' RI' + ('●' if sigs['ri'] else '○')
            + ' CD' + ('●' if sigs['cd'] else '○'))

    def _copy_selection_maybe(self):
        """选中即复制（PuTTY 风格）：拖选过程中持续同步剪贴板，空选不动。"""
        if read_setting('log/copy_on_select', '0') not in ('1', 'true'):
            return
        cur = self.log.textCursor()
        if cur.hasSelection():
            QtWidgets.QApplication.clipboard().setText(cur.selectedText())

    def _apply_on_top(self, on: bool):
        """窗口置顶开关（设置页）：setWindowFlags 需要重新 show 一次才生效。"""
        hint = QtCore.Qt.WindowType.WindowStaysOnTopHint
        flags = self.windowFlags()
        new_flags = (flags | hint) if on else (flags & ~hint)
        if new_flags != flags:
            self.setWindowFlags(new_flags)
            if self.isVisible():
                self.show()

    def _log_stamp(self) -> str:
        """时间戳：clock=全时刻（默认，导出/定位依赖它）；elapsed=连接起的相对秒。"""
        if read_setting('log/stamp_mode', 'clock') == 'elapsed' \
                and self._conn_mono is not None:
            return f'{time.monotonic() - self._conn_mono:8.3f}s'
        return _stamp()

    def _on_connected(self):
        # 连接周期归零收发统计（对齐主线 reset_receive_stats 时机）
        self.rx_bytes_total = 0
        self.tx_bytes_total = 0
        self._update_stats_label()
        self._set_connect_params_locked(True)
        self._conn_mono = time.monotonic()   # elapsed 时间戳基准
        self._line_timer.start()
        if read_setting('txseq/auto_run', '0') in ('1', 'true', 'yes'):
            self._open_txseq()
            self._txseq_dialog.auto_start()   # 已在跑（重连场景）则不打断

    def _set_connect_params_locked(self, locked: bool):
        """连接期间锁定串口协议参数（改口/改波特率须先断开），置灰提示。"""
        self.combo_port.setEnabled(not locked)
        self.combo_baud.setEnabled(not locked)
        tip = '已连接：断开后才能修改' if locked else ''
        self.combo_port.setToolTip(tip)
        self.combo_baud.setToolTip(tip)

    def _update_connect_button(self):
        """连接按钮文案跟连接状态同步：连着显示「断开」，空闲显示「连接」。"""
        connected = bool(self.worker)
        self.btn_connect.setText('断开' if connected else '连接')
        # ESP32 线控按钮只随连接可用；断开时弹起 DTR/RTS（guard 会拦住对已关口写线）
        for btn in (self.btn_mc_reset, self.btn_download, self.btn_dtr, self.btn_rts,
                    self.btn_break):
            btn.setEnabled(connected)
        if not connected:
            for btn in (self.btn_dtr, self.btn_rts):
                if btn.isChecked():
                    btn.setChecked(False)   # toggled → _esp32_line，此时 worker 已 None 直接跳过

    def _teardown_worker(self):
        self._set_connect_params_locked(False)
        self._line_timer.stop()
        self.lbl_lines.setText('')
        self._conn_mono = None
        if not self.worker:
            return
        self.worker.stop()
        if getattr(self.worker, 'isRunning', lambda: False)():
            # 线程卡死在内核调用（USB 串口假死）：销毁会连带运行中的线程，可致崩溃
            # ——只扣进停尸房保引用，进程退出统一回收（见 _ZOMBIE_WORKERS 注）
            _ZOMBIE_WORKERS.append(self.worker)
        else:
            self.worker.deleteLater()
        self.worker = None
        self._update_connect_button()
        self._auto_log.close()  # 断开即落盘收尾（文件名保留，重连后追加同一文件）

    def set_status(self, text: str, obj_name: str):
        self.lbl_status.setText(text)
        self.lbl_status.setObjectName(obj_name)
        self.lbl_status.style().unpolish(self.lbl_status)
        self.lbl_status.style().polish(self.lbl_status)

    def _note_fault(self, info):
        """!FAULT 行到达：点亮状态栏徽标（带类型+时刻），不弹窗不刷屏。"""
        stamp = datetime.datetime.now().strftime('%H:%M:%S')
        text = f'⚠ {info.fault_type} {stamp}'
        self.btn_fault_badge.setText(text)
        self.btn_fault_badge.setToolTip(text + '\n点击切换到「故障诊断」页查看寄存器位域详情')
        self.btn_fault_badge.show()

    def closeEvent(self, event):  # noqa: N802 (Qt 命名)
        """关窗两段式：窗口立即消失，线程/进程收尾异步进行。

        旧的"全部内联收尾"在串口洪泛 + 视频流 + CLI 子进程等状态下要把多条
        bounded-wait 串着等完（worker 1.5s + 控制 6s + 收流 3s + 解码 3s + CLI 1s，
        最坏 ~14s，USB 串口假死时 close() 还会在内核再卡），表现为点 X 没反应
        （"关闭按钮失效"）。现在 closeEvent 只做轻量收尾并隐藏窗口，重活挪进
        _finalize_close（singleShot 下一拍），最后一个窗口由它负责 app.quit()。
        """
        event.accept()
        if self in _LIVE_WINDOWS:
            _LIVE_WINDOWS.remove(self)
        if getattr(self, '_finalized', False):
            super().closeEvent(event)
            return
        if self._txseq_dialog is not None:
            self._txseq_dialog.close()
        self._save_connect_settings()
        if getattr(self, '_port_watch_timer', None):
            self._port_watch_timer.stop()
        self._auto_log.close()
        # 阻止"最后窗口关闭即退出"在收尾完成前把事件循环退掉（video_stop 必须能发出）
        app = QtWidgets.QApplication.instance()
        self._quit_on_last_saved = app.quitOnLastWindowClosed()
        if not _LIVE_WINDOWS:
            app.setQuitOnLastWindowClosed(False)
        QtCore.QTimer.singleShot(0, self._finalize_close)
        super().closeEvent(event)

    def _finalize_close(self):
        """重收尾（closeEvent 下一拍执行）：窗口已隐藏，用户不再感知等待。"""
        if getattr(self, '_finalized', False):
            return
        self._finalized = True
        try:
            self.recorder.stop()
            self._control_channel.stop()
            self.wave.stop_timers()
            self.spectrum.stop_timers()
            self.stm32.shutdown()
            self.device_screen.shutdown()   # v2：设备屏收流/解码线程收尾
            self._stop_file_send('窗口关闭')
            self._teardown_worker()
        finally:
            # 收尾自身出异常也要照常退出（窗口已经关了，留着无窗进程没意义）
            app = QtWidgets.QApplication.instance()
            if not getattr(self, '_quit_on_last_saved', True):
                return                  # 原本就不随最后窗口退出：保持原行为
            app.setQuitOnLastWindowClosed(True)
            left = [w for w in app.topLevelWidgets()
                    if w is not self and w.isVisible() and not w.testAttribute(
                        QtCore.Qt.WidgetAttribute.WA_DontShowOnScreen)]
            if not _LIVE_WINDOWS and not left:
                app.quit()

    # ---------- RX 管线 ----------

    # RX 洪泛合并参数：单次消费预算（离屏实测 ~1.2ms/行整行处理，16KB≈60 行
    # ≈百 ms 量级/拍——任意相邻消费间必回事件循环泵原生消息，杜绝幽灵窗且
    # 交互不掉帧）与高水位（超限丢最老——洪泛下旧垃圾没有保留价值）
    RX_DRAIN_BUDGET = 16 * 1024
    RX_PENDING_MAX = 256 * 1024

    def ingest_bytes(self, data: bytes):
        """RX 入口：洪泛合并的入队侧。小数据量（人机交互节奏）当场消费完，
        语义与直处理一致；大流量只做有界消费，余量留给下一拍/下一条事件。"""
        self._rx_pending.extend(data)
        if len(self._rx_pending) > self.RX_PENDING_MAX:
            drop = len(self._rx_pending) - self.RX_PENDING_MAX
            del self._rx_pending[:drop]
            self._note_rx_drop(drop)
        self._drain_rx_pending()
        if self._rx_pending:
            # 尾量（到达快于消费）交给下一拍：singleShot(0) 让事件循环先泵原生消息
            self._rx_drain_timer.start()

    def _drain_rx_pending(self):
        self._rx_drain_timer.stop()          # 同拍重入（ingest 内联调用）先摘定时器
        pending = self._rx_pending
        if not pending:
            return
        budget = bytes(pending[:self.RX_DRAIN_BUDGET])
        del pending[:len(budget)]
        self._ingest_now(budget)
        if len(pending) > self.RX_PENDING_MAX:
            drop = len(pending) - self.RX_PENDING_MAX
            del pending[:drop]
            self._note_rx_drop(drop)
        if pending:
            self._rx_drain_timer.start()     # 余量下一拍继续（中间必有原生消息泵）

    def _note_rx_drop(self, drop: int):
        self._rx_dropped += drop
        now = time.monotonic()
        if now - self._rx_drop_stamp >= 1.0:     # 限频：洪泛期间 1s 一条不打爆日志
            self._rx_drop_stamp = now
            self._append_log('SYS', f'[洪泛保护] 接收缓冲超限，已丢弃最老 {drop} 字节'
                                    f'（累计 {self._rx_dropped}）')

    def _ingest_now(self, data: bytes):
        """真正的 RX 处理（原 ingest_bytes 主体；按洪泛预算分批调用）。"""
        now = time.monotonic()
        self.rx_bytes_total += len(data)
        self._update_stats_label()
        if self._xfer is not None:
            for b in data:
                self._xfer['engine'].on_rx(b)   # 协议引擎先吃应答字节
        if self.recorder.active and self._replay_state is None:
            # 回放期间不再录自己（避免回放套娃）
            self.recorder.record_chunk(data)
        if self.combo_rx_format.currentText() == 'HEX':
            # 对齐主线：HEX 显示模式按到达块直接落显；先清掉未成行的文本残行
            tail = self.buffer.drain()
            if tail:
                self._append_log('RX', f'{tail} (格式切换落显)')
            self._append_log('RX', bytes_to_hex_text(data))
        else:
            for line in self.buffer.feed(data, now):
                self._append_log('RX', line)
        if self._modbus_active:
            self._modbus_rx_acc.extend(data)
        self.wave.consume_bytes(data)
        self._update_backlog()

    def _update_stats_label(self):
        self.lbl_stats.setText(
            f'RX {self.rx_bytes_total / 1024.0:.1f} KiB | '
            f'TX {self.tx_bytes_total / 1024.0:.1f} KiB')

    def _count_tx_bytes(self, n: int):
        self.tx_bytes_total += n
        self._update_stats_label()

    def modbus_transport(self, frame: bytes, timeout_ms: int = 600) -> bytes:
        """Modbus 线上事务：清残留 → 写请求 → 有界读响应（对齐主线 send_modbus_wire_transaction）。

        响应长度可预期（功能码/异常位推出）或静默 150ms 即收；整体不超过 timeout_ms。
        """
        if not self.worker:
            return b''
        self._modbus_rx_acc = bytearray()
        self._modbus_active = True
        try:
            if not self.worker.write_bytes(frame):
                return b''
            self._count_tx_bytes(len(frame))
            deadline = time.monotonic() + timeout_ms / 1000.0
            idle_at = time.monotonic() + 0.15
            expected = -1

            loop = QtCore.QEventLoop()
            poll = QtCore.QTimer(loop)
            poll.setInterval(10)

            def check():
                nonlocal expected, idle_at
                acc = self._modbus_rx_acc
                now = time.monotonic()
                if acc:
                    idle_at = now + 0.15
                    if expected < 0 and len(acc) >= 3:
                        expected = 5 if (acc[1] & 0x80) else 3 + acc[2] + 2
                    if 0 <= expected <= len(acc):
                        loop.quit()
                        return
                    if now > idle_at:
                        loop.quit()
                        return
                if now > deadline:
                    loop.quit()

            poll.timeout.connect(check)
            poll.start()
            loop.exec()
            poll.stop()  # 切断 poll→check→loop 引用环：否则僵尸定时器在 GC 后
                         # 于事件分发里抛 RuntimeError，AI 控制通道 socket 从此失聪
            resp = bytes(self._modbus_rx_acc)
            self._record_modbus_transaction(frame, resp)
            return resp
        finally:
            self._modbus_active = False
            self._modbus_rx_acc = bytearray()

    def _record_modbus_transaction(self, frame: bytes, resp: bytes):
        """记录最近一次 Modbus 事务摘要（上限 50 条；AI 通道 modbus_recent 数据源）。"""
        self._modbus_recent.append({
            't': _stamp(),
            'req_hex': bytes_to_hex_text(frame),
            'resp_hex': bytes_to_hex_text(resp) if resp else '',
            'resp_bytes': len(resp),
            'ok': bool(resp)})
        del self._modbus_recent[:-50]

    def on_flush_tick(self):
        now = time.monotonic()
        self._sync_log_tab_stop()  # 空闲时窗口缩放后，TX 行制表位跟着视口宽度走
        tail = self.buffer.poll_idle_flush(now)
        if tail is not None:
            self._append_log('RX', f'{tail} (空闲落显)')
        self._flush_auto_log()
        self._update_backlog()

    def _flush_auto_log(self):
        """自动日志节拍：勾选开 → 懒建文件落盘；勾选关 → 积压直接丢（对齐主线）。"""
        if not self.chk_auto_save.isChecked():
            self._auto_log.discard()
            return
        if self._auto_log.flush() and self._auto_log.path:
            self.lbl_log_path.setText(f'日志: {self._auto_log.path}')

    def _export_log(self):
        default_name = f'serial_export_{datetime.datetime.now():%Y%m%d_%H%M%S}.txt'
        log_dir = get_log_dir()
        os.makedirs(log_dir, exist_ok=True)
        path, _ = QtWidgets.QFileDialog.getSaveFileName(
            self, '导出日志', os.path.join(log_dir, default_name),
            '文本日志 (*.txt);;所有文件 (*.*)')
        if not path:
            return
        try:
            self._export_log_to(path)
        except OSError as exc:
            self._append_log('SYS', f'导出日志失败: {exc}')
            QtWidgets.QMessageBox.warning(self, '导出失败', str(exc))

    def _export_log_to(self, path: str):
        with open(path, 'w', encoding='utf-8', newline='') as f:
            f.write(build_log_export_text(self.message_entries))
        self._append_log('SYS', f'日志已导出到:\n{path}')

    def _on_rx_format_changed(self, _text: str):
        # 切格式时把残行落到日志，避免"看起来丢了"
        tail = self.buffer.drain()
        if tail:
            self._append_log('RX', f'{tail} (格式切换落显)')
        self._update_backlog()

    def _update_backlog(self):
        total = self.buffer.backlog_bytes + self.wave.state.backlog_bytes
        if self._rx_pending:
            total += len(self._rx_pending)
        if self._rx_dropped:
            self.lbl_backlog.setText(f'积压 {total} B（洪泛丢弃 {self._rx_dropped}）')
        else:
            self.lbl_backlog.setText(f'积压 {total} B')

    # ---------- 会话录制 / 回放 ----------

    REPLAY_INTERVAL_MS = 10

    def toggle_session_record(self, on: bool) -> str | None:
        """波形窗「录制」按钮的实际执行方。返回录制文件路径（停止时 None）。"""
        if on:
            path = self.recorder.start()
            self._append_log('SYS', f'会话录制开始: {path}')
            return path
        if self.recorder.active:
            self.recorder.stop()
            self._append_log('SYS', '会话录制结束。')
        return None

    def _start_replay(self, path: str):
        """回放录制文件：按录制顺序逐块定时喂回接收管线（不重新录制）。"""
        if self._replay_state is not None:
            self.set_status('已在回放中', 'status_err')
            return
        chunks = load_replay_chunks(path)
        if not chunks:
            self._append_log('SYS', f'回放文件没有可重放的数据块: {path}')
            return
        self._replay_state = {'chunks': chunks, 'index': 0, 'path': path}
        self._replay_timer = QtCore.QTimer(self)
        self._replay_timer.setInterval(self.REPLAY_INTERVAL_MS)
        self._replay_timer.timeout.connect(self._replay_step)
        self._replay_timer.start()
        self._append_log('SYS', f'会话回放开始: {path}（{len(chunks)} 块）')

    def _replay_step(self) -> bool:
        """喂下一块。返回是否还有剩余（测试可手工循环调用）。"""
        state = self._replay_state
        if state is None:
            return False
        if state['index'] >= len(state['chunks']):
            self._stop_replay()
            return False
        _ms, data = state['chunks'][state['index']]
        state['index'] += 1
        self.ingest_bytes(data)
        if state['index'] >= len(state['chunks']):
            self._stop_replay()
            return False
        return True

    def _stop_replay(self):
        state = self._replay_state
        self._replay_state = None
        if getattr(self, '_replay_timer', None):
            self._replay_timer.stop()
        if state:
            self._append_log('SYS', f"会话回放完成: {state['path']}（{state['index']} 块）")

    # ---------- 日志 / 发送 ----------

    def _append_log(self, direction: str, text: str):
        now = datetime.datetime.now()
        self._log_seq += 1
        self.message_entries.append({'ts': now, 'direction': direction, 'text': text,
                                     'seq': self._log_seq})
        if len(self.message_entries) > MAX_LOG_ENTRIES:
            # 长跑防涨：与主线同款裁剪量（丢最老 4000 条）
            del self.message_entries[:MAX_LOG_TRIM]
        if direction != 'SYS' and self.chk_auto_save.isChecked():
            self._auto_log.enqueue(format_entry_block(now, direction, text))
        if direction == 'RX':
            fault_info = self.fault.record_line(text)   # !FAULT 行 → 故障诊断页记录
            if fault_info is not None:
                self._note_fault(fault_info)            # 状态栏徽标点亮（不弹窗不刷屏）
            decoded = test_fault_dump_line(text)
            if decoded:
                self._append_log('SYS', f'HardFault 解码: {decoded}')
        self._sync_log_tab_stop()  # 视口宽度可能在追加间隙变了，先对齐制表位
        # 日志行配色动态读主题模块（切主题后新日志行随新主题换色）
        color = {'RX': theme.RX_COLOR, 'TX': theme.TX_COLOR}.get(direction, theme.SYS_COLOR)
        bold = False
        if direction == 'RX' and self.chk_idf.isChecked():
            color, marker, bold = self._esp32_log_style(text)
            if marker is not None:
                self._append_log('SYS', marker)   # 复位横幅装饰行（递归，落在 RX 行前）
        # 关键词高亮/告警（用户意图 > IDF 等级色 > 设备 SGR 色）：alert 红 / watch 琥珀
        kw_color = None
        if direction == 'RX':
            self._refresh_keywords()
            kw = rx_highlight.match_line(text, self._kw_alert, self._kw_watch)
            if kw == 'alert':
                kw_color = theme.color('status_err')
            elif kw == 'watch':
                kw_color = theme.color('status_warn')
            if kw_color:
                color = kw_color
                bold = True
        if direction == 'RX' and self.chk_ansi.isChecked():
            # ANSI 渲染：显示段以字符格式直插文档——文本零损耗（查找/悬停互转
            # 按原文工作），空格天然保排版（框线图/艺术字不塌陷），非 SGR 转义
            # （清屏/光标移动）同样剥除。无 SGR 行回落 IDF/默认整行样式。
            segs, had_sgr = ansi_render.parse_segments(
                text, color, theme.color('log_bg'), theme.color('log_text'))
            if had_sgr:
                bold = False   # 设备 SGR 自带粗体表达，IDF 层整行加粗不再叠加
            self.log.appendHtml(
                f'<span style="color:{theme.TIME_COLOR}">[{self._log_stamp()}]</span> '
                f'<b style="color:{color}">{direction}</b>'
            )
            cursor = self.log.textCursor()
            # 光标必须收拢到文档末尾再插入：用户在日志里留有选中区时，控件光标
            # 带着选区——直接 insertText 会把新数据替换进选区（毁日志、吞选中）
            cursor.movePosition(QtGui.QTextCursor.MoveOperation.End)
            cursor.clearSelection()
            if any(seg.text for seg in segs):
                cursor.insertText(' ')   # 方向与正文间的分隔空格走光标插入
            for seg in segs:
                if not seg.text:
                    continue
                fmt = QtGui.QTextCharFormat()
                if had_sgr:
                    fmt.setForeground(QtGui.QColor(seg.color))
                    if seg.bg:
                        fmt.setBackground(QtGui.QColor(seg.bg))
                    if seg.bold:
                        fmt.setFontWeight(QtGui.QFont.Weight.Bold)
                    if seg.italic:
                        fmt.setFontItalic(True)
                    if seg.underline:
                        fmt.setFontUnderline(True)
                    if seg.strike:
                        fmt.setFontStrikeOut(True)
                else:
                    fmt.setForeground(QtGui.QColor(color))
                    if bold:
                        fmt.setFontWeight(QtGui.QFont.Weight.Bold)
                if kw_color:
                    fmt.setForeground(QtGui.QColor(kw_color))
                    fmt.setFontWeight(QtGui.QFont.Weight.Bold)
                cursor.insertText(seg.text, fmt)
        else:
            safe = html.escape(text)
            weight = 'font-weight:bold; ' if bold else ''
            self.log.appendHtml(
                f'<span style="color:{theme.TIME_COLOR}">[{self._log_stamp()}]</span> '
                f'<b style="color:{color}">{direction}</b> '
                f'<span style="color:{color}; {weight}">{safe}</span>'
            )
        if direction == 'TX':
            self._indent_tx_line_right()

    def _refresh_keywords(self):
        """关键词表缓存：原始串脏检查（QSettings 内存读便宜，解析只在变更时）。"""
        raw = (read_setting('log/kw_alert', ''), read_setting('log/kw_watch', ''))
        if raw != self._kw_raw:
            self._kw_raw = raw
            self._kw_alert = rx_highlight.parse_keywords(raw[0])
            self._kw_watch = rx_highlight.parse_keywords(raw[1])

    def _esp32_log_style(self, text: str):
        """ESP-IDF 行 → (显示色, 复位装饰行|None, 加粗)。

        只影响显示：条目原文（message_entries/导出/自动日志）不动。
        副作用：收集/重置 backtrace 地址收集器，并联动「解析 backtrace」按钮。
        """
        clean = esp_idf_log.normalize_for_parse(text)
        rst = esp_idf_log.is_reset_banner(clean)
        if rst is not None:
            self._esp32_bt.clear()   # 新一轮启动：上一次崩溃的 backtrace 作废
            self._update_backtrace_button()
            return theme.color('accent'), f'── 设备复位: 0x{rst[0]:02X} {rst[1]} ──', True
        panic = esp_idf_log.is_panic(clean)
        if panic is not None:
            self._esp32_bt.clear()   # panic 开始新一轮崩溃现场
            self._update_backtrace_button()
            return theme.color('status_err'), None, True
        if esp_idf_log.is_backtrace_line(clean):
            addrs = esp_idf_log.extract_backtrace_addrs(clean)
            for a in addrs:
                if a not in self._esp32_bt:
                    self._esp32_bt.append(a)
            del self._esp32_bt[:-256]   # 防洪泛上限
            self._update_backtrace_button()
            return theme.color('text_dim'), None, False
        info = esp_idf_log.parse_idf_line(clean)
        if info:
            level_color = {'E': 'status_err', 'W': 'status_warn',
                           'D': 'text_dim', 'V': 'text_dim'}.get(info['level'])
            if level_color:
                return theme.color(level_color), None, False
        return theme.color('rx'), None, False

    def _update_backtrace_button(self):
        self.btn_backtrace.setEnabled(bool(self._esp32_bt))

    def _show_backtrace_dialog(self):
        dlg = BacktraceDialog(list(self._esp32_bt), self)
        dlg.exec()

    def _sync_log_tab_stop(self):
        """TX 行聊天式右对齐的 RightTab 制表位，跟随日志视口宽度（变了才重设）。

        QPlainTextEdit 的 plain 文档布局忽略块级对齐——QTextBlockFormat.setAlignment
        与 div align="right" 都不渲染（QPlainTextDocumentLayout::layoutBlock 只认
        defaultTextOption）。所以整行靠右用「文档级 RightTab 制表位 + TX 行行首
        制表符」实现：制表位随每次块重排从 defaultTextOption 重新读取，追加、
        maximumBlockCount 裁剪、滚动都不丢；只有含制表符的 TX 行受影响，
        RX/SYS 行照常左对齐。窗口拉宽/缩窄后下一次同步（≤200ms）全体 TX 行重对齐。
        """
        stop = self.log.viewport().width() - 2 * self.log.document().documentMargin()
        if stop <= 0 or stop == self._log_tab_stop_px:
            return
        self._log_tab_stop_px = stop
        opt = QtGui.QTextOption(self.log.document().defaultTextOption())
        tab = QtGui.QTextOption.Tab()
        tab.position = float(stop)
        tab.type = QtGui.QTextOption.TabType.RightTab
        opt.setTabs([tab])
        self.log.document().setDefaultTextOption(opt)

    def _indent_tx_line_right(self):
        """给刚落显的 TX 行行首插入制表符（appendHtml 会折叠 HTML 里的空白，
        制表符必须走光标插入）。appendHtml 后光标停在本条 block 末尾。
        先收拢到文档末尾——同 _append_log，防止把制表符插进用户选中区。"""
        cursor = self.log.textCursor()
        cursor.movePosition(QtGui.QTextCursor.MoveOperation.End)
        cursor.clearSelection()
        cursor.movePosition(QtGui.QTextCursor.MoveOperation.StartOfBlock)
        cursor.insertText('\t')

    def clear_log(self):
        """清空记录：显示、条目、残行缓冲一并清（字节统计不清——那是连接周期的量）。"""
        self.buffer.clear()
        self.message_entries.clear()
        self.log.clear()
        self._esp32_bt.clear()   # 崩溃现场随日志清空
        self._update_backtrace_button()
        self._append_log('SYS', '消息记录已清空。')
        self._update_backlog()

    def send(self):
        raw = self.edit_send.text()
        if not raw:
            return
        newline_mode = self.combo_newline.currentText()
        payload_bytes, display_text = self._build_tx_payload(raw, newline_mode)
        if not payload_bytes:
            return
        sent = bool(self.worker and self.worker.write_bytes(payload_bytes))
        if sent:
            self._count_tx_bytes(len(payload_bytes))
        suffix = '' if sent else '  (未连接，仅入日志)'
        self._append_log('TX', display_text + suffix)
        if sent and self.btn_broadcast.isChecked():
            peers = [w for w in _LIVE_WINDOWS if w is not self and w.worker is not None]
            for w in peers:
                w._send_tx_bytes(payload_bytes, display_text + '（广播）')
            if peers:
                self._append_log('SYS', f'已广播到 {len(peers)} 个其他会话窗口')
        self.edit_send.push_history(raw)
        self.edit_send.clear()

    def _send_tx_bytes(self, payload: bytes, display_text: str) -> bool:
        """发送原始字节并落 TX 日志/统计（序列发送/协议发送共用入口）。

        未连接时仍落日志（带未连接后缀）但返回 False——调用方据此中止序列。
        """
        sent = bool(self.worker and self.worker.write_bytes(payload))
        if sent:
            self._count_tx_bytes(len(payload))
        suffix = '' if sent else '  (未连接，仅入日志)'
        self._append_log('TX', display_text + suffix)
        return sent

    def _open_txseq(self):
        """打开/前置序列发送对话框（非模态，脚本与选项记忆在设置键里）。"""
        if self._txseq_dialog is None:
            self._txseq_dialog = TxSequenceDialog(self)
        self._txseq_dialog.show()
        self._txseq_dialog.raise_()
        self._txseq_dialog.activateWindow()

    def _build_tx_payload(self, raw: str, newline_mode: str):
        """按发送格式组包。返回 (bytes, 展示文本)；无效 HEX 返回 (b'', 错误文本)。"""
        if self.combo_tx_format.currentText() == 'HEX':
            try:
                payload = try_parse_hex_text(raw)
            except ValueError as exc:
                self.set_status(str(exc), 'status_err')
                return b'', ''
            payload += NEWLINE_BYTES.get(newline_mode, b'')
            if not payload:
                return b'', ''
            return payload, bytes_to_hex_text(payload)
        payload_text = raw + NEWLINE_TEXT.get(newline_mode, '')
        if not payload_text:
            return b'', ''
        # 主线 TX 展示：换行可视化（<CR>/<LF>）
        return payload_text.encode('utf-8'), \
            payload_text.replace('\r', '<CR>').replace('\n', '<LF>')

    # ---------- 文件发送（分块定时器，UI 不卡死；发送中再点 = 停止） ----------

    def toggle_file_send(self):
        if self._file_send or self._xfer:
            self._stop_file_send('手动停止')
            return
        path, _ = QtWidgets.QFileDialog.getOpenFileName(self, '选择要发送的文件')
        if not path:
            return
        try:
            with open(path, 'rb') as f:
                data = f.read()
        except OSError as exc:
            self.set_status(f'文件读取失败: {exc}', 'status_err')
            return
        if not data:
            self.set_status('文件为空，没有可发送的内容。', 'status_err')
            return
        if not self.worker:
            self.set_status('串口未连接。', 'status_err')
            return
        name = path.replace('\\', '/').rsplit('/', 1)[-1]
        proto = self.combo_file_proto.currentText()
        if proto != '原样':
            self._start_proto_send(name, data, proto)
            return
        self._file_send = {'name': name, 'bytes': data, 'pos': 0}
        self.btn_file.setText('停止')
        self._append_log('TX', f'[文件发送] {name}（{len(data)} 字节，原样字节流）')
        self.file_timer.start()

    def _file_send_tick(self):
        state = self._file_send
        if not state:
            self.file_timer.stop()
            return
        if not self.worker:
            self._stop_file_send('串口已断开')
            return
        data = state['bytes']
        chunk = data[state['pos']:state['pos'] + FILE_CHUNK]
        try:
            ok = self.worker.write_bytes(chunk)
        except Exception as exc:  # pragma: no cover - 真实串口路径
            self._stop_file_send(f'写入失败: {exc}')
            self.set_status(f'文件发送失败: {exc}', 'status_err')
            return
        if not ok:
            self._stop_file_send('写入失败: 串口未打开')
            return
        self._count_tx_bytes(len(chunk))
        state['pos'] += len(chunk)
        if state['pos'] >= len(data):
            self._stop_file_send('完成')
            self.set_status(f"文件发送完成: {state['name']}（{len(data)} 字节）", 'status_ok')
        else:
            self.set_status(
                f"文件发送中 {state['name']}: {state['pos'] / 1024:.1f} / {len(data) / 1024:.1f} KiB",
                'status_ok')

    def _stop_file_send(self, reason: str):
        if self._xfer is not None:
            self._stop_xfer(reason)   # 断线/窗口关闭等路径一并终止协议发送
        state = self._file_send
        if not state:
            return
        self._file_send = None
        self.file_timer.stop()
        self.btn_file.setText('文件发送')
        if reason != '完成':
            self._append_log(
                'SYS',
                f"[文件发送中断] {state['name']}: {reason}"
                f"（已发 {state['pos']} / {len(state['bytes'])} 字节）")

    # ---------- XMODEM/YMODEM 协议发送 ----------

    def _xfer_write(self, payload: bytes):
        """引擎待发字节 → 串口（线程同 UI；协议帧很小，无需分片）。"""
        if self.worker and self.worker.write_bytes(payload):
            self._count_tx_bytes(len(payload))

    def _start_proto_send(self, name: str, data: bytes, proto: str):
        if not self.worker:
            self.set_status('串口未连接。', 'status_err')
            return
        engine = (XmodemSender(data, self._xfer_write)
                  if proto == 'XMODEM'
                  else YmodemSender(data, name, self._xfer_write))
        self._xfer = {'engine': engine, 'name': name, 'proto': proto}
        self.btn_file.setText('停止')
        self._append_log('TX',
                         f'[{proto}发送] {name}（{len(data)} 字节）——'
                         '等待对端请求（C/NAK）…')
        self._xfer_timer.start()

    def _xfer_tick(self):
        state = self._xfer
        if not state:
            self._xfer_timer.stop()
            return
        engine = state['engine']
        engine.tick()
        if engine.done:
            self._stop_xfer('完成')
            self.set_status(f"协议发送完成: {state['name']}", 'status_ok')
            self._append_log('SYS',
                             f"[{state['proto']}发送] {state['name']} 完成"
                             f"（对端 ACK，协议层校验通过）")
        elif engine.error:
            self._stop_xfer(engine.error)
            self.set_status(f"协议发送失败: {engine.error}", 'status_err')

    def _stop_xfer(self, reason: str):
        state = self._xfer
        if not state:
            return
        self._xfer = None
        self._xfer_timer.stop()
        engine = state['engine']
        if not engine.done and not engine.error:
            engine.error = reason   # 阻止引擎继续写
            self._xfer_write(bytes.fromhex('1818'))   # CAN CAN 取消对端
        self.btn_file.setText('文件发送')
        if reason != '完成':
            self._append_log('SYS', f"[{state['proto']}发送中断] {state['name']}: {reason}")
