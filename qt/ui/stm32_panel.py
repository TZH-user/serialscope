# -*- coding: utf-8 -*-
"""STM32 烧录/调试页签 —— 基于 STM32CubeProgrammer CLI（core/stm32_cli.py 封装）。

- 状态区：CLI 路径是否找到 / ST-LINK 探测（-l）/ 目标连接与芯片信息，带「刷新」；
- 操作：连接/断开、软/硬复位、hex/bin 烧录（-v 校验）、全片擦除、读保护解除、OB 读取；
  危险操作（擦除/解除读保护）弹确认框；
- 底部 CLI 原始输出日志区（raw_output 增量滚动）。
所有长操作经 QProcess 异步执行（忙碌时按钮统一置灰），UI 不卡；关窗 shutdown() 强杀子进程。
无 CLI / 无 ST-LINK 时给明确中文提示，不崩。
"""
from __future__ import annotations

import os

from PySide6 import QtCore, QtWidgets

from core.settings import read_setting
from core.stm32_cli import (BIN_BASE_ADDR_DEFAULT, TIMEOUT_FLASH_MS, TIMEOUT_LIST_MS,
                            Stm32Cli, build_flash_args, erase_success_marker,
                            flash_success_marker, parse_device_info, parse_option_bytes,
                            parse_probe_list, resolve_cli_path)
from ui.settings_page import STM32_CLI_PATH_KEY

_NO_CLI_HINT = ('未找到 STM32CubeProgrammer CLI。\n'
                '请安装 STM32CubeProgrammer，或在「设置」页手动指定 CLI 路径后点「刷新」。')
_NO_PROBE_HINT = '未检测到 ST-LINK 探测器（请检查 USB 连接与驱动）'


class Stm32Panel(QtWidgets.QWidget):
    """STM32 页签：状态区 + 操作按钮 + CLI 原始日志。"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.runner = Stm32Cli(self)
        self.runner.raw_output.connect(self._append_cli_log)
        self.runner.finished.connect(self._on_finished)
        self.runner.busy_changed.connect(self._on_busy_changed)
        self.probes: list[dict] = []
        self.connected = False
        self.flash_path = ''
        self.log_fn = None            # callable(direction, text)：重大事件镜像到主日志（可选）
        self._busy_text = ''

        # ---- 状态区 ----
        # 状态标签统一 wordWrap：CLI 路径/报错可能很长，窄窗换行显示而非被裁
        # （layout_audit 实测：无换行时长路径随窗口缩窄直接截断丢失尾部）
        self.lbl_cli = QtWidgets.QLabel('CLI: 探测中…')
        self.lbl_cli.setObjectName('backlog')
        self.lbl_cli.setWordWrap(True)
        self.lbl_probe = QtWidgets.QLabel('ST-LINK: 未探测（点「刷新」探测）')
        self.lbl_probe.setObjectName('backlog')
        self.lbl_probe.setWordWrap(True)
        self.lbl_target = QtWidgets.QLabel('目标: 未连接')
        self.lbl_target.setObjectName('backlog')
        self.lbl_target.setWordWrap(True)
        self.info_box = QtWidgets.QPlainTextEdit()
        self.info_box.setReadOnly(True)
        self.info_box.setMaximumBlockCount(200)
        self.info_box.setPlaceholderText('连接后在此显示芯片信息（型号/Flash/内核/电压…）')

        self.combo_probe = QtWidgets.QComboBox()
        self.combo_probe.setToolTip('多探针时选择目标探针（-l 的 Probe 序号）')
        self.combo_mode = QtWidgets.QComboBox()
        self.combo_mode.addItems(['UR', 'HOTPLUG', 'NORMAL'])
        self.combo_mode.setToolTip('连接模式：UR=复位下连接（默认，适合跑飞固件）；HOTPLUG=热插拔；NORMAL=正常')
        self.btn_refresh = QtWidgets.QPushButton('刷新')
        self.btn_refresh.setObjectName('tool')
        self.btn_refresh.clicked.connect(self.refresh_all)

        status_row = QtWidgets.QHBoxLayout()
        status_row.addWidget(QtWidgets.QLabel('探针'))
        status_row.addWidget(self.combo_probe)
        status_row.addWidget(QtWidgets.QLabel('连接模式'))
        status_row.addWidget(self.combo_mode)
        status_row.addWidget(self.btn_refresh)
        status_row.addStretch(1)

        status_box = QtWidgets.QVBoxLayout()
        status_box.addWidget(self.lbl_cli)
        status_box.addWidget(self.lbl_probe)
        status_box.addWidget(self.lbl_target)
        status_box.addLayout(status_row)
        status_box.addWidget(self.info_box, 1)
        status_group = QtWidgets.QGroupBox('状态')
        status_group.setLayout(status_box)

        # ---- 操作区 ----
        self.btn_connect = QtWidgets.QPushButton('连接目标')
        self.btn_connect.clicked.connect(self.toggle_connect)
        self.btn_rst_soft = QtWidgets.QPushButton('软复位')
        self.btn_rst_soft.setObjectName('tool')
        self.btn_rst_soft.setToolTip('-rst：系统复位（重启用户程序）')
        self.btn_rst_soft.clicked.connect(lambda: self.run_reset(hard=False))
        self.btn_rst_hard = QtWidgets.QPushButton('硬复位')
        self.btn_rst_hard.setObjectName('tool')
        self.btn_rst_hard.setToolTip('-hardRst：NRST 硬件复位')
        self.btn_rst_hard.clicked.connect(lambda: self.run_reset(hard=True))
        self.btn_ob = QtWidgets.QPushButton('读取 OB')
        self.btn_ob.setObjectName('tool')
        self.btn_ob.setToolTip('-ob displ：读取 Option Bytes（只读）')
        self.btn_ob.clicked.connect(self.run_option_bytes)

        self.btn_pick = QtWidgets.QPushButton('选择 hex/bin…')
        self.btn_pick.setObjectName('tool')
        self.btn_pick.clicked.connect(self.pick_file)
        self.lbl_file = QtWidgets.QLabel('未选择固件文件')
        self.lbl_file.setObjectName('backlog')
        self.lbl_file.setWordWrap(True)   # 固件路径长，窄窗换行而非被裁
        self.edit_addr = QtWidgets.QLineEdit(BIN_BASE_ADDR_DEFAULT)
        self.edit_addr.setToolTip('bin 烧录基址（hex 文件自带地址，忽略此框）；支持 0x 前缀或十进制')
        self.edit_addr.setMaximumWidth(120)
        self.btn_flash = QtWidgets.QPushButton('烧录（带校验）')
        self.btn_flash.clicked.connect(self.run_flash)

        self.btn_erase = QtWidgets.QPushButton('全片擦除')
        self.btn_erase.setObjectName('tool')
        self.btn_erase.setToolTip('-e all：擦除全部 Flash（危险，会弹确认框）')
        self.btn_erase.clicked.connect(self.run_erase)
        self.btn_rdu = QtWidgets.QPushButton('读保护解除')
        self.btn_rdu.setObjectName('tool')
        self.btn_rdu.setToolTip('-rdu：RDP 回归 Level 0（危险，会弹确认框，芯片会被整片擦除）')
        self.btn_rdu.clicked.connect(self.run_rdu)

        op_row = QtWidgets.QHBoxLayout()
        op_row.addWidget(self.btn_connect)
        op_row.addWidget(self.btn_rst_soft)
        op_row.addWidget(self.btn_rst_hard)
        op_row.addWidget(self.btn_ob)
        op_row.addStretch(1)
        flash_row = QtWidgets.QHBoxLayout()
        flash_row.addWidget(self.btn_pick)
        flash_row.addWidget(self.lbl_file, 1)
        flash_row.addWidget(QtWidgets.QLabel('bin 基址'))
        flash_row.addWidget(self.edit_addr)
        flash_row.addWidget(self.btn_flash)
        danger_row = QtWidgets.QHBoxLayout()
        danger_row.addWidget(self.btn_erase)
        danger_row.addWidget(self.btn_rdu)
        danger_row.addStretch(1)

        self.lbl_busy = QtWidgets.QLabel('空闲')
        self.lbl_busy.setObjectName('backlog')
        self.lbl_busy.setWordWrap(True)   # 忙碌文案带文件名，窄窗换行而非被裁

        op_box = QtWidgets.QVBoxLayout()
        op_box.addLayout(op_row)
        op_box.addLayout(flash_row)
        op_box.addLayout(danger_row)
        op_box.addWidget(self.lbl_busy)
        op_group = QtWidgets.QGroupBox('操作')
        op_group.setLayout(op_box)

        # ---- CLI 原始日志 ----
        self.cli_log = QtWidgets.QPlainTextEdit()
        self.cli_log.setReadOnly(True)
        self.cli_log.setUndoRedoEnabled(False)
        self.cli_log.setMaximumBlockCount(4000)
        self.cli_log.setPlaceholderText('CLI 原始输出…')

        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(6, 6, 6, 6)
        layout.setSpacing(4)
        layout.addWidget(status_group, 3)
        layout.addWidget(op_group)
        layout.addWidget(self.cli_log, 2)

        self.op_buttons = (self.btn_connect, self.btn_rst_soft, self.btn_rst_hard,
                           self.btn_ob, self.btn_pick, self.btn_flash,
                           self.btn_erase, self.btn_rdu)
        self._set_target_ops_enabled(False)
        # 启动只解析 CLI 路径（本地、瞬时）；-l 探测等长操作等用户点「刷新」再跑
        self._apply_cli_path()

    # ---------- 状态区 ----------

    def _set_label(self, lbl: QtWidgets.QLabel, text: str, kind: str = 'backlog'):
        lbl.setText(text)
        lbl.setObjectName(kind)
        lbl.style().unpolish(lbl)
        lbl.style().polish(lbl)

    def _apply_cli_path(self):
        r"""解析 CLI 路径（设置键 → C:\ST → PATH → 常见位置），只更新状态不发子进程。"""
        explicit = read_setting(STM32_CLI_PATH_KEY, '').strip()
        path = resolve_cli_path(explicit)
        self.runner.cli_path = path
        if path:
            self._set_label(self.lbl_cli, f'CLI: {path}', 'status_ok')
            self._set_target_ops_enabled(True)
        else:
            self._set_target_ops_enabled(False)
            if explicit:
                self._set_label(self.lbl_cli, f'CLI: 设置的路径不存在：{explicit}', 'status_err')
            else:
                self._set_label(self.lbl_cli, 'CLI: 未找到', 'status_err')

    def refresh_all(self):
        """刷新：重解析 CLI 路径 + 后台 -l 探测 ST-LINK（无 CLI 只给中文提示，不崩）。"""
        self._apply_cli_path()
        if not self.runner.cli_path:
            self._set_label(self.lbl_probe, 'ST-LINK: 无法探测（' + _NO_CLI_HINT.split('。')[0] + '）',
                            'status_err')
            self._log_event('SYS', _NO_CLI_HINT.replace('\n', ' '))
            return
        if not self.runner.run('list', ['-l'], TIMEOUT_LIST_MS):
            self._log_event('SYS', '已有操作在执行，请稍候。')
            return
        self._set_label(self.lbl_probe, 'ST-LINK: 探测中…', 'backlog')
        self._set_busy_text('探测 ST-LINK（-l）…')

    # ---------- 操作入口 ----------

    def _require_cli(self) -> bool:
        if self.runner.cli_path:
            return True
        self._log_event('SYS', _NO_CLI_HINT.replace('\n', ' '))
        QtWidgets.QMessageBox.warning(self, 'STM32', _NO_CLI_HINT)
        return False

    def toggle_connect(self):
        if self.connected:
            # 断开只复位本页状态（CLI 每条命令都是独立连接，无需真正关闭）
            self.connected = False
            self._set_label(self.lbl_target, '目标: 未连接', 'backlog')
            self.info_box.clear()
            self.btn_connect.setText('连接目标')
            self._log_event('SYS', 'STM32 目标已断开（仅复位本页状态）。')
            return
        if not self._require_cli():
            return
        index = self.combo_probe.currentIndex() if self.probes else None
        if not self.runner.connect_target(self.combo_mode.currentText(), index):
            self._log_event('SYS', '已有操作在执行，请稍候。')
            return
        self._set_busy_text(f'连接目标（-c port=SWD mode={self.combo_mode.currentText()}）…')

    def run_reset(self, hard: bool):
        if not self._require_cli():
            return
        index = self.combo_probe.currentIndex() if self.probes else None
        if not self.runner.reset_target(hard, self.combo_mode.currentText(), index):
            self._log_event('SYS', '已有操作在执行，请稍候。')
            return
        self._set_busy_text('硬复位（-hardRst）…' if hard else '软复位（-rst）…')

    def run_option_bytes(self):
        if not self._require_cli():
            return
        index = self.combo_probe.currentIndex() if self.probes else None
        if not self.runner.read_option_bytes(self.combo_mode.currentText(), index):
            self._log_event('SYS', '已有操作在执行，请稍候。')
            return
        self._set_busy_text('读取 Option Bytes…')

    def pick_file(self):
        path, _ = QtWidgets.QFileDialog.getOpenFileName(
            self, '选择固件文件', '',
            '固件 (*.hex *.bin);;所有文件 (*.*)')
        if not path:
            return
        self.flash_path = path
        is_bin = path.lower().endswith('.bin')
        self.lbl_file.setText(path)
        self.edit_addr.setEnabled(is_bin)
        self.lbl_file.setToolTip('bin 文件按下方基址烧录' if is_bin else 'hex 文件按文件内地址烧录（基址无效）')

    def run_flash(self):
        if not self._require_cli():
            return
        if not self.flash_path or not os.path.isfile(self.flash_path):
            QtWidgets.QMessageBox.warning(self, '烧录', '请先选择有效的固件文件（hex 或 bin）。')
            return
        base_addr = None
        if self.flash_path.lower().endswith('.bin'):
            base_addr = self.edit_addr.text().strip()
            try:
                int(base_addr, 0)   # 0x 前缀/十进制都接受，只校验不转换
            except ValueError:
                QtWidgets.QMessageBox.warning(self, '烧录', f'bin 基址无效：{base_addr}')
                return
        index = self.combo_probe.currentIndex() if self.probes else None
        args = build_flash_args(self.flash_path, base_addr,
                                self.combo_mode.currentText(), index)
        if not self.runner.run('flash', args, TIMEOUT_FLASH_MS):
            self._log_event('SYS', '已有操作在执行，请稍候。')
            return
        self._set_busy_text(f'烧录中（-d/-w + -v 校验）：{os.path.basename(self.flash_path)}')

    def run_erase(self):
        if not self._require_cli() or not self._confirm_danger(
                '全片擦除',
                '将擦除目标芯片全部 Flash 内容，且不可恢复！\n确定继续？'):
            return
        index = self.combo_probe.currentIndex() if self.probes else None
        if not self.runner.erase_all(self.combo_mode.currentText(), index):
            self._log_event('SYS', '已有操作在执行，请稍候。')
            return
        self._set_busy_text('全片擦除（-e all）…')

    def run_rdu(self):
        if not self._require_cli() or not self._confirm_danger(
                '读保护解除',
                '解除读保护会把 RDP 从 Level 1 回归 Level 0，芯片将被整片擦除！\n确定继续？'):
            return
        index = self.combo_probe.currentIndex() if self.probes else None
        if not self.runner.readout_unprotect(self.combo_mode.currentText(), index):
            self._log_event('SYS', '已有操作在执行，请稍候。')
            return
        self._set_busy_text('读保护解除（-rdu -y）…')

    def _confirm_danger(self, title: str, text: str) -> bool:
        """危险操作二次确认；返回是否继续。"""
        answer = QtWidgets.QMessageBox.question(
            self, title, text,
            QtWidgets.QMessageBox.StandardButton.Yes | QtWidgets.QMessageBox.StandardButton.No,
            QtWidgets.QMessageBox.StandardButton.No)
        return answer == QtWidgets.QMessageBox.StandardButton.Yes

    # ---------- 执行结果 ----------

    def _on_busy_changed(self, busy: bool):
        for btn in self.op_buttons:
            btn.setEnabled(not busy and bool(self.runner.cli_path))
        if not busy:
            self._set_busy_text('')

    def _set_busy_text(self, text: str):
        self._busy_text = text
        self.lbl_busy.setText(text or '空闲')

    def _set_target_ops_enabled(self, on: bool):
        for btn in self.op_buttons:
            btn.setEnabled(on and bool(self.runner.cli_path) and not self.runner.is_busy())

    def _on_finished(self, payload: dict):
        action, ok, error = payload['action'], payload['ok'], payload['error']
        output = payload['output']
        if payload['timed_out']:
            self._log_event('SYS', f'[{action}] 操作超时，已强制终止 CLI。')
            return
        if action == 'list':
            self._on_list_done(ok, error, output)
        elif action == 'connect':
            self._on_connect_done(ok, error, output)
        elif action == 'ob':
            self._on_ob_done(ok, error, output)
        else:
            self._on_generic_done(action, ok, error, output)

    def _on_list_done(self, ok: bool, error: str, output: str):
        self.probes = parse_probe_list(output) if ok else []
        self.combo_probe.clear()
        if not self.runner.cli_path:
            self._set_label(self.lbl_probe, f'ST-LINK: 无法探测（{_NO_CLI_HINT.split("。")[0]}）',
                            'status_err')
            return
        if not self.probes:
            self._set_label(self.lbl_probe, f'ST-LINK: {_NO_PROBE_HINT}', 'status_err')
            self._log_event('SYS', _NO_PROBE_HINT + (f'（{error}）' if error else ''))
            return
        for probe in self.probes:
            sn = probe['sn'] or 'SN?'
            self.combo_probe.addItem(f"Probe {probe['index']}  {sn}")
        first = self.probes[0]
        suffix = f'（共 {len(self.probes)} 个，FW {first["fw"]}）' if first['fw'] else ''
        self._set_label(self.lbl_probe, f'ST-LINK: 已检测到 {len(self.probes)} 个{suffix}', 'status_ok')
        self._log_event('SYS', f"ST-LINK 探测完成：{len(self.probes)} 个"
                               f"（首个 SN {first['sn'] or '?'}）")

    def _on_connect_done(self, ok: bool, error: str, output: str):
        if not ok:
            self.connected = False
            self._set_label(self.lbl_target, '目标: 连接失败', 'status_err')
            self._log_event('SYS', f'连接失败: {error or "无响应"}')
            return
        info = parse_device_info(output)
        name = info.get('name') or '未知型号'
        nvm = info.get('nvm_size') or '—'
        cpu = info.get('cpu') or '—'
        ram = info.get('ram_size') or '—'
        voltage = info.get('voltage') or '—'
        self.connected = True
        self.btn_connect.setText('断开')
        self._set_label(self.lbl_target, f'目标: 已连接 {name}', 'status_ok')
        self.info_box.setPlainText(
            f'芯片型号 : {name}\n'
            f'Flash    : {nvm}\n'
            f'RAM      : {ram}\n'
            f'内核     : {cpu}\n'
            f'电压     : {voltage}\n'
            f"DeviceID : {info.get('device_id') or '—'}"
            f"   RevID : {info.get('revision_id') or '—'}\n"
            f"ST-LINK  : {info.get('sn') or '—'}（FW {info.get('fw') or '—'}）")
        self._log_event('SYS', f'目标已连接: {name}（Flash {nvm} / 内核 {cpu} / {voltage}）')

    def _on_ob_done(self, ok: bool, error: str, output: str):
        if not ok:
            self._log_event('SYS', f'OB 读取失败: {error or "无响应"}')
            return
        ob = parse_option_bytes(output)
        desc = ob['rdp_desc'] or '未解析到 RDP'
        self._log_event('SYS', f'OB 读取完成：RDP {ob["rdp_value"] or "?"}（{desc}），全文见 CLI 日志')

    def _on_generic_done(self, action: str, ok: bool, error: str, output: str):
        titles = {'reset_soft': '软复位', 'reset_hard': '硬复位',
                  'flash': '烧录', 'erase': '全片擦除', 'rdu': '读保护解除'}
        title = titles.get(action, action)
        if not ok:
            self._log_event('SYS', f'{title}失败: {error or "无响应"}')
            return
        if action == 'flash' and not flash_success_marker(output):
            self._log_event('SYS', f'{title}命令完成，但未见校验成功标志（Download verified successfully）。')
            return
        if action == 'erase' and not erase_success_marker(output):
            self._log_event('SYS', f'{title}命令完成（未捕获 erase successfully 字样）。')
            return
        self._log_event('SYS', f'{title}完成。')

    # ---------- 日志 ----------

    def _append_cli_log(self, chunk: str):
        """CLI 原始增量输出（进度条 \r 块字符按行折叠，避免日志区刷屏）。"""
        clean = chunk.replace('\r', '\n')
        for line in clean.splitlines():
            if line.strip():
                self.cli_log.appendPlainText(line)

    def _log_event(self, direction: str, text: str):
        self.cli_log.appendPlainText(f'[SYS] {text}')
        if self.log_fn:
            self.log_fn(direction, f'[STM32] {text}')

    # ---------- 收尾 ----------

    def ai_status(self) -> dict:
        """给主窗体 AI 控制通道 stm32_status() 的状态口（只读快照，不触发任何 CLI 操作）。"""
        return {
            'cli_found': bool(self.runner.cli_path),
            'cli_path': self.runner.cli_path,
            'connected': bool(self.connected),
            'busy': self.runner.is_busy(),
            'busy_text': self._busy_text,
            'probe_count': len(self.probes),
            'last_probe_sn': (self.probes[0]['sn'] if self.probes else ''),
            'flash_path': self.flash_path,
        }

    def shutdown(self):
        """关窗收尾：终止在跑的 CLI 子进程。"""
        self.runner.shutdown()
