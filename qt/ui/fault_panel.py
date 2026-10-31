# -*- coding: utf-8 -*-
"""故障诊断面板：!FAULT 行的树形详情（样式基准：STM32Cube 扩展故障寄存器树）。

- 顶层节点依次为：故障地址 / 故障函数 / 硬故障 / 总线故障 / 内存管理故障 / 用法故障
  （另有「其他字段」仅在宽容解析收到未知键时出现）；
- 置位标志醒目（加粗+status_err 前景），未置位灰显（text_dim）；
- 位名后跟人话说明（口径复用 core/fault_decode.py，DEBUGEVT 按 ARMv7-M 补）；
- 多条历史异常左侧列表切换；ELF 路径为面板设置项（默认空=只显示裸地址）。

排版纪律（本面板是范本）：树自视口滚动（内容超界出滚动条不出容器）、说明列
wordWrap 换行、列宽 Interactive 不被内容顶宽、历史列表窄栏（QSplitter 可拖）、
超长十六进制/函数名行允许换行，禁止任何固定大宽度。
"""
from __future__ import annotations

from PySide6 import QtCore, QtGui, QtWidgets

from core.fault_report import (BFSR_BITS, HFSR_BITS, MMFSR_BITS, UFSR_BITS,
                               FaultInfo, describe_address, fmt_addr,
                               load_elf_symbols, parse_fault_line, symbolize)
from core.settings import read_setting, write_setting
from ui import theme

HISTORY_MAX = 32                  # 历史异常上限（丢最老）
ELF_PATH_KEY = 'fault/elf_path'   # 面板设置项：ELF 路径（默认空=裸地址）

_EMPTY_HINT = ('尚未捕获异常。固件故障时会向控制台串口打出 !FAULT 行，'
               '本页自动记录并解码寄存器位域。')


class FaultPanel(QtWidgets.QWidget):
    """故障诊断页：左历史列表 + 右寄存器树。ingest 由主窗体喂 record_line()。"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._history: list[FaultInfo] = []
        self._syms = None
        self._elf_err = ''
        self._current: FaultInfo | None = None

        # ---- 顶部：摘要行 + ELF 设置行 ----
        self.lbl_summary = QtWidgets.QLabel(_EMPTY_HINT)
        self.lbl_summary.setObjectName('backlog')
        self.lbl_summary.setWordWrap(True)          # 摘要长不撑宽

        self.edit_elf = QtWidgets.QLineEdit(read_setting(ELF_PATH_KEY, ''))
        self.edit_elf.setPlaceholderText('固件 ELF 路径（可选，把 PC/LR 符号化成 函数+偏移）')
        self.edit_elf.setClearButtonEnabled(True)
        self.edit_elf.setToolTip('带符号表的 .elf（Keil 为 .axf）。留空只显示裸地址。')
        self.btn_elf_browse = QtWidgets.QPushButton('浏览…')
        self.btn_elf_browse.setObjectName('tool')
        self.lbl_elf_state = QtWidgets.QLabel('')
        self.lbl_elf_state.setObjectName('backlog')

        elf_row = QtWidgets.QHBoxLayout()
        elf_row.addWidget(QtWidgets.QLabel('ELF'))
        elf_row.addWidget(self.edit_elf, 1)
        elf_row.addWidget(self.btn_elf_browse)

        # ---- 左：历史列表（窄栏，splitter 可拖）----
        self.list_hist = QtWidgets.QListWidget()
        self.list_hist.setAlternatingRowColors(True)
        self.list_hist.setToolTip('历史异常（最新在上），点击切换右侧详情')
        self.list_hist.setMinimumWidth(150)
        self.btn_clear = QtWidgets.QPushButton('清除记录')
        self.btn_clear.setObjectName('tool')
        left_box = QtWidgets.QVBoxLayout()
        left_box.addWidget(self.list_hist, 1)
        left_box.addWidget(self.btn_clear)
        left_page = QtWidgets.QWidget()
        left_page.setLayout(left_box)

        # ---- 右：寄存器树（自带视口滚动 + wordWrap）----
        self.tree = QtWidgets.QTreeWidget()
        self.tree.setColumnCount(3)
        self.tree.setHeaderLabels(['项目', '值', '说明'])
        self.tree.setWordWrap(True)             # 说明列/长值换行，不撑横向
        self.tree.setAlternatingRowColors(True)
        header = self.tree.header()
        header.setSectionResizeMode(QtWidgets.QHeaderView.ResizeMode.Interactive)
        header.setStretchLastSection(True)      # 说明列吃满余宽
        self.tree.setColumnWidth(0, 150)
        self.tree.setColumnWidth(1, 210)
        self.tree.setUniformRowHeights(False)   # wordWrap 行高不齐，不能开

        split = QtWidgets.QSplitter(QtCore.Qt.Orientation.Horizontal)
        split.addWidget(left_page)
        split.addWidget(self.tree)
        split.setStretchFactor(0, 0)
        split.setStretchFactor(1, 1)
        split.setSizes([300, 640])
        split.setChildrenCollapsible(False)

        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(6, 6, 6, 6)
        layout.setSpacing(4)
        layout.addWidget(self.lbl_summary)
        layout.addLayout(elf_row)
        layout.addWidget(self.lbl_elf_state)
        layout.addWidget(split, 1)

        self.list_hist.currentRowChanged.connect(self._on_hist_selected)
        self.btn_clear.clicked.connect(self.clear_all)
        self.btn_elf_browse.clicked.connect(self._browse_elf)
        self.edit_elf.editingFinished.connect(lambda: self.set_elf_path(self.edit_elf.text()))
        self._apply_elf()

    # ---------- ingest 入口 ----------

    def record_line(self, text: str) -> FaultInfo | None:
        """主窗体每条 RX 行喂进来；!FAULT 行记录并展示，返回 FaultInfo。"""
        info = parse_fault_line(text)
        if info is None:
            return None
        self._history.insert(0, info)
        del self._history[HISTORY_MAX:]
        stamp = QtCore.QTime.currentTime().toString('HH:mm:ss')
        item = QtWidgets.QListWidgetItem(f'{stamp}  {info.fault_type}')
        item.setToolTip(info.raw.strip()[:600])
        # 程序化插入不走选中信号（连续故障时 row 不变、信号不触发），展示直接调
        self.list_hist.blockSignals(True)
        self.list_hist.insertItem(0, item)
        while self.list_hist.count() > HISTORY_MAX:
            self.list_hist.takeItem(self.list_hist.count() - 1)
        self.list_hist.setCurrentRow(0)
        self.list_hist.blockSignals(False)
        self.show_fault(info)
        return info

    # ---------- 展示 ----------

    def show_fault(self, info: FaultInfo | None):
        """重建右侧树（重符号化/历史切换共用）。"""
        self._current = info
        self.tree.setUpdatesEnabled(False)
        self.tree.clear()
        if info is None:
            self._item(['尚未捕获异常', '—', '固件打出 !FAULT 行后这里显示寄存器位域'])
            self.lbl_summary.setText(_EMPTY_HINT)
            self.tree.setUpdatesEnabled(True)
            return
        trunc = '；⚠ 行疑似被截断（半行）' if info.truncated else ''
        extra_note = f'；未知键 {len(info.extra)} 个（见「其他字段」）' if info.extra else ''
        self.lbl_summary.setText(
            f'最近异常：{info.fault_type}{trunc}{extra_note}（原始行见历史列表悬停）')
        self._fill_top_nodes(info)
        self.tree.expandAll()
        self.tree.setUpdatesEnabled(True)

    def _fill_top_nodes(self, info: FaultInfo):
        # 1) 故障地址（BFARVALID/MMARVALID 有效给 0x 地址，无效标注「无效」）
        addr_text, addr_note = describe_address(info)
        top = self._item(['故障地址', addr_text, addr_note])
        if info.bfar is not None:
            self._item(['— BFAR', fmt_addr(info.bfar), 'BusFault 地址寄存器原值'], top)
        if info.mmfar is not None:
            self._item(['— MMFAR', fmt_addr(info.mmfar), 'MemManage 地址寄存器原值'], top)

        # 2) 故障函数（PC 符号化；LR 标注为返回地址）
        top = self._item(['故障函数', symbolize(self._syms, info.pc),
                          'PC 符号化结果（未加载 ELF 时为裸地址）'])
        self._item(['PC', fmt_addr(info.pc), '出错位置'], top)
        self._item(['LR', fmt_addr(info.lr), '返回地址'], top)
        self._item(['XPSR', fmt_addr(info.xpsr), '程序状态寄存器'], top)

        # 3) 硬故障 HFSR
        self._bit_group('硬故障', info.hfsr, HFSR_BITS, 'HFSR 硬故障状态寄存器', None)
        # 4) 总线故障 BFSR（CFSR bits[15:8]；BFARVALID 置位才带地址）
        bfsr = None if info.cfsr is None else (info.cfsr >> 8) & 0xFF
        bfsr_ok = bool(bfsr is not None and bfsr & 0x80)
        self._bit_group('总线故障', bfsr, BFSR_BITS, 'CFSR[15:8]',
                        info.bfar if bfsr_ok else None)
        # 5) 内存管理故障 MMFSR（CFSR bits[7:0]；MMARVALID 置位才带地址）
        mmfsr = None if info.cfsr is None else info.cfsr & 0xFF
        mmfsr_ok = bool(mmfsr is not None and mmfsr & 0x80)
        self._bit_group('内存管理故障', mmfsr, MMFSR_BITS, 'CFSR[7:0]',
                        info.mmfar if mmfsr_ok else None)
        # 6) 用法故障 UFSR（CFSR bits[31:16]）
        ufsr = None if info.cfsr is None else (info.cfsr >> 16) & 0xFFFF
        self._bit_group('用法故障', ufsr, UFSR_BITS, 'CFSR[31:16]', None)

        # 7) 其他字段（宽容解析的未知键，仅在出现时展示）
        if info.extra:
            top = self._item(['其他字段', '', '契约之外的键，原样保留展示'])
            for k, v in info.extra.items():
                self._item([k, v, '未知键'], top)

    def _bit_group(self, title: str, value: int | None, bits, note: str,
                   valid_addr: int | None):
        """一组状态位：值列=寄存器值，子行=逐位（置位醒目/未置位灰显）。

        valid_addr：BFARVALID/MMARVALID 置位时在值列带出地址；None 则只标「置位」。
        """
        if value is None:
            top = self._item([title, '缺失', f'{note}（该行被截断，本组无法判定）'])
            self._dim(top)
            return
        reg_text = f'0x{value:08X}' if value > 0xFFFF else f'0x{value:02X}'
        top = self._item([title, reg_text, note])
        for name, bit, text in bits:
            on = bool(value & bit)
            if on and name in ('BFARVALID', 'MMARVALID'):
                val = '置位 ' + fmt_addr(valid_addr)
            else:
                val = '置位' if on else '—'
            row = self._item([name, val, text], top)
            (self._hot if on else self._dim)(row)

    # ---------- 条目样式 ----------

    def _item(self, texts: list[str], parent=None) -> QtWidgets.QTreeWidgetItem:
        it = QtWidgets.QTreeWidgetItem(texts)
        if texts[1].startswith('0x') or texts[1].startswith('— '):
            # 十六进制值等宽对齐；mono_font() 不带字号，显式对齐树字号防行高被顶大
            f = theme.mono_font()
            f.setPointSize(self.tree.font().pointSize())
            it.setFont(1, f)
        (parent.addChild if parent is not None else self.tree.addTopLevelItem)(it)
        return it

    def _hot(self, row: QtWidgets.QTreeWidgetItem):
        """置位标志：位名加粗 + status_err 前景（随主题换色）。"""
        font = row.font(0)
        font.setBold(True)
        row.setFont(0, font)
        for col in range(3):
            row.setForeground(col, QtGui.QBrush(theme.color('status_err')))

    def _dim(self, row: QtWidgets.QTreeWidgetItem):
        for col in range(3):
            row.setForeground(col, QtGui.QBrush(theme.color('text_dim')))

    # ---------- 历史列表 ----------

    def _on_hist_selected(self, row: int):
        if 0 <= row < len(self._history):
            self.show_fault(self._history[row])

    def clear_all(self):
        self._history.clear()
        self.list_hist.clear()
        self.show_fault(None)

    def history(self) -> list[FaultInfo]:
        """自测/快照用（最新在前）。"""
        return list(self._history)

    # ---------- ELF 符号化设置 ----------

    def set_elf_path(self, path: str):
        """面板设置项：ELF 路径落盘并重符号化当前展示。"""
        clean = (path or '').strip().strip('"')
        if clean != self.edit_elf.text():
            self.edit_elf.setText(clean)
        write_setting(ELF_PATH_KEY, clean)
        self._apply_elf()

    def _browse_elf(self):
        path, _ = QtWidgets.QFileDialog.getOpenFileName(
            self, '选择带符号表的固件 ELF', self.edit_elf.text(),
            '固件 ELF (*.elf *.axf *.out);;所有文件 (*.*)')
        if path:
            self.set_elf_path(path)

    def _apply_elf(self):
        self._syms, self._elf_err = load_elf_symbols(self.edit_elf.text())
        if not self.edit_elf.text().strip():
            self.lbl_elf_state.setText('ELF: 未设置（只显示裸地址）')
            self.lbl_elf_state.setObjectName('backlog')
        elif self._syms is not None:
            self.lbl_elf_state.setText(f'ELF: 已加载（函数符号 {len(self._syms.addrs)} 个）')
            self.lbl_elf_state.setObjectName('status_ok')
        else:
            self.lbl_elf_state.setText(f'ELF: 加载失败——{self._elf_err}')
            self.lbl_elf_state.setObjectName('status_err')
        self.lbl_elf_state.style().unpolish(self.lbl_elf_state)
        self.lbl_elf_state.style().polish(self.lbl_elf_state)
        if self._current is not None:
            self.show_fault(self._current)

    def elf_state(self) -> dict:
        """AI 通道/自测的只读快照。"""
        return {'elf_path': self.edit_elf.text(),
                'loaded': self._syms is not None,
                'symbol_count': len(self._syms.addrs) if self._syms else 0,
                'error': self._elf_err,
                'history': len(self._history)}
