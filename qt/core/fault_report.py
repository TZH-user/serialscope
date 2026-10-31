# -*- coding: utf-8 -*-
"""!FAULT 故障报告行解析 + 最小 ELF32 符号化（纯函数，无 Qt 依赖，可 headless）。

上报契约（固件线并行开发，逐字节遵守前缀与键名）——新固件发生真故障时向
控制台串口（USART1 115200 8N1）打出一行文本，随后 NVIC_SystemReset()：

    !FAULT T=HardFault PC=0x08001234 LR=0x08004567 XPSR=0x01000000 CFSR=0x00008282 HFSR=0x40000000 BFAR=0x00000000 MMFAR=0x00000000

- 单行，前缀 !FAULT（后跟一个空格），空格分隔 key=value，值为 0x 前缀 8 位十六进制；
  T 取 HardFault/MemManage/BusFault/UsageFault 之一。
- 本侧解析必须宽容：真实故障可能打断半行发送——缺键允许、值被截断也解析出已收到
  的位数、未知键原样保留展示；行式 ingest 天然可重同步（下一行重新对齐）。

符号化：手写最小 ELF32 只读解析（小端 .symtab/.strtab，STT_FUNC 按地址排序），
零第三方依赖（不引 pyelftools/capstone）；lookup(addr) → 最近符号+偏移。
"""
from __future__ import annotations

import bisect
import os
import re
import struct
from dataclasses import dataclass, field

# ---------------------------------------------------------------------------
# 行解析
# ---------------------------------------------------------------------------

# 前缀：!FAULT 后跟一个空格（宽容行内前导空白；固件只打行首，但半行重同步场景
# 上游可能带着上一行的残尾一起喂进来）
_RE_PREFIX = re.compile(r'!\s*FAULT\s+')
# 通用键值 token：key=非空白值（宽容未知键的非数值值原样保留）
_RE_TOKEN_ANY = re.compile(r'([A-Za-z_][A-Za-z0-9_]*)=(\S+)')
# 数值键值：key=0x 前缀十六进制（1~8 位都收：截断的值也要能解析出已到部分）
_RE_HEX = re.compile(r'^0x([0-9A-Fa-f]{1,8})$')
_HEX_KEYS = ('pc', 'lr', 'xpsr', 'cfsr', 'hfsr', 'bfar', 'mmfar')
# 键值要求完整 8 位；少于 8 位说明发送在这里被打断（半行）
_FULL_VALUE_LEN = 8

KNOWN_TYPES = ('HardFault', 'MemManage', 'BusFault', 'UsageFault')


@dataclass(frozen=True)
class FaultInfo:
    """一条 !FAULT 行的结构化结果（宽容解析后的快照）。"""

    fault_type: str                      # T（未给出/未知类型原样保留）
    pc: int | None = None
    lr: int | None = None
    xpsr: int | None = None
    cfsr: int | None = None
    hfsr: int | None = None
    bfar: int | None = None
    mmfar: int | None = None
    extra: dict[str, str] = field(default_factory=dict)   # 未知键原样保留
    truncated: bool = False              # 行尾值疑似被截断（半行发送）
    raw: str = ''


def parse_fault_line(text: str) -> FaultInfo | None:
    """识别 !FAULT 行 → 结构化 FaultInfo；非 !FAULT 行返回 None。

    宽容口径：
    - 行内任意位置出现 '!FAULT ' 前缀即命中（前导时间戳/残尾不致命）；
    - 键缺省允许（半行）；值为 1~8 位十六进制都解析（截断值取已到部分）；
    - 未知 key=value 保留进 extra（同样解析 0x 值，解析不出按原样字符串）；
    - 最后一个键值不足 8 位 → truncated=True（提示发送被打断）。
    """
    if not text:
        return None
    m = _RE_PREFIX.search(text)
    if not m:
        return None
    body = text[m.end():]

    info = {'fault_type': '', 'pc': None, 'lr': None, 'xpsr': None,
            'cfsr': None, 'hfsr': None, 'bfar': None, 'mmfar': None}
    extra: dict[str, str] = {}
    truncated = False
    for km in _RE_TOKEN_ANY.finditer(body):
        key, val = km.group(1), km.group(2)
        if key == 'T':
            continue    # T 统一在循环后取枚举词（_grab_word）
        hm = _RE_HEX.match(val)
        lkey = key.lower()
        if lkey in _HEX_KEYS:
            if hm:
                info[lkey] = int(hm.group(1), 16)
            continue
        # 未知键：原样保留展示（数值补齐到 8 位，截断/非数值原样）
        extra[key] = (f'0x{int(hm.group(1), 16):08X}'
                      if hm and len(hm.group(1)) == _FULL_VALUE_LEN else val)
    if not info['fault_type']:
        # T 缺失或非枚举值：给原样词（宽容未知类型），彻底没有就标未知
        word = _grab_word(body, 'T')
        info['fault_type'] = word if word else '未知'
    # 疑似截断：行尾（忽略尾随空白）停在 键=0x短值 / 键=0x / 键= 上
    tail = body.rstrip()
    if re.search(r'[A-Za-z_][A-Za-z0-9_]*=(0x[0-9A-Fa-f]{1,7}|0x?)?$', tail):
        truncated = True

    return FaultInfo(fault_type=info['fault_type'],
                     pc=info['pc'], lr=info['lr'], xpsr=info['xpsr'],
                     cfsr=info['cfsr'], hfsr=info['hfsr'],
                     bfar=info['bfar'], mmfar=info['mmfar'],
                     extra=extra, truncated=truncated, raw=text)


def _grab_word(body: str, key: str) -> str:
    """取 T=<词> 的原样词（宽容未知类型；没有返回空串）。"""
    m = re.search(r'\b' + key + r'=([A-Za-z0-9_]+)', body)
    return m.group(1) if m else ''


def fmt_addr(value: int | None) -> str:
    return '0x%08X' % value if value is not None else '—'


# ---------------------------------------------------------------------------
# 位域人话表（文案口径复用 core/fault_decode.py；DEBUGEVT 为 HFSR bit31，
# fault_decode 未覆盖，按 ARMv7-M 手册口径补齐）
# ---------------------------------------------------------------------------

HFSR_BITS = (
    ('DEBUGEVT', 0x80000000, 'HFSR.DEBUGEVT 调试事件(调试器引发)'),
    ('FORCED', 0x40000000, 'HFSR.FORCED 强制升级为HardFault(具体原因查CFSR)'),
    ('VECTTBL', 0x00000002, 'HFSR.VECTTBL 取异常向量表时总线错误'),
)
BFSR_BITS = (   # CFSR 高半的第二字节 (CFSR>>8)&0xFF
    ('BFARVALID', 0x80, 'BFSR.BFARVALID BFAR 地址有效(见「故障地址」)'),
    ('LSPERR', 0x20, 'BFSR.LSPERR 惰性状态保存总线错误'),
    ('STKERR', 0x10, 'BFSR.STKERR 入栈时总线错误'),
    ('UNSTKERR', 0x08, 'BFSR.UNSTKERR 出栈时总线错误'),
    ('IMPRECISERR', 0x04, 'BFSR.IMPRECISERR 非精确数据总线错误(常见于写缓冲)'),
    ('PRECISERR', 0x02, 'BFSR.PRECISERR 精确数据总线错误'),
    ('IBUSERR', 0x01, 'BFSR.IBUSERR 取指总线错误'),
)
MMFSR_BITS = (  # CFSR 低字节
    ('MMARVALID', 0x80, 'MMFSR.MMARVALID MMFAR 地址有效(见「故障地址」)'),
    ('MLSPERR', 0x20, 'MMFSR.MLSPERR 惰性状态保存时错误'),
    ('MUNSTKERR', 0x08, 'MMFSR.MUNSTKERR 出栈时MemManage错误'),
    ('MSTKERR', 0x10, 'MMFSR.MSTKERR 入栈时MemManage错误'),
    ('DACCVIOL', 0x02, 'MMFSR.DACCVIOL 数据访问违例(MPU)'),
    ('IACCVIOL', 0x01, 'MMFSR.IACCVIOL 取指访问违例(MPU/XN区域)'),
)
UFSR_BITS = (   # CFSR 高半 (CFSR>>16)&0xFFFF
    ('DIVBYZERO', 0x0200, 'UFSR.DIVBYZERO 除零(SDIV/UDIV)'),
    ('UNALIGNED', 0x0100, 'UFSR.UNALIGNED 非对齐访问'),
    ('NOCP', 0x0008, 'UFSR.NOCP 调用无协处理器(常见:未使能FPU却用浮点)'),
    ('INVPC', 0x0004, 'UFSR.INVPC 无效PC加载(返回地址错)'),
    ('INVSTATE', 0x0002, 'UFSR.INVSTATE 状态无效(常见:函数指针错误/Thumb位)'),
    ('UNDEFINSTR', 0x0001, 'UFSR.UNDEFINSTR 未定义指令'),
)


def decode_reasons(info: FaultInfo) -> list[str]:
    """置位位的整个人话文案列表（口径与 fault_decode.test_fault_dump_line 一致，
    供日志侧一行式复用；面板树另用 *_BITS 表逐位展示）。"""
    reasons: list[str] = []
    if info.cfsr is not None:
        mmfsr, bfsr = info.cfsr & 0xFF, (info.cfsr >> 8) & 0xFF
        ufsr = (info.cfsr >> 16) & 0xFFFF
        for name, bit, text in MMFSR_BITS:
            if mmfsr & bit:
                reasons.append(text if name not in ('MMARVALID',)
                               else f'MMFSR.MMARVALID 出错地址 {fmt_addr(info.mmfar)}')
        for name, bit, text in BFSR_BITS:
            if bfsr & bit:
                reasons.append(text if name not in ('BFARVALID',)
                               else f'BFSR.BFARVALID 出错地址 {fmt_addr(info.bfar)}')
        for name, bit, text in UFSR_BITS:
            if ufsr & bit:
                reasons.append(text)
    if info.hfsr is not None:
        for name, bit, text in HFSR_BITS:
            if info.hfsr & bit:
                reasons.append(text)
    if not reasons:
        reasons.append('CFSR/HFSR 各错误位均未置位')
    return reasons


def describe_address(info: FaultInfo) -> tuple[str, str]:
    """「故障地址」顶层节点：返回 (展示文本, 来源说明)。

    BFARVALID/MMARVALID 有效时给 0x 地址；无效标注「无效」；CFSR 缺失（半行）
    标注未知。两个都置位时优先 BusFault 地址（来源里带上另一个）。
    """
    if info.cfsr is None:
        return '未知', 'CFSR 缺失（行被截断），无法判定地址有效性'
    bfsr, mmfsr = (info.cfsr >> 8) & 0xFF, info.cfsr & 0xFF
    picks: list[str] = []
    if bfsr & 0x80:
        picks.append(fmt_addr(info.bfar) + '（BFAR，BusFault）')
    if mmfsr & 0x80:
        picks.append(fmt_addr(info.mmfar) + '（MMFAR，MemManage）')
    if picks:
        return picks[0].split('（')[0], '；'.join(picks)
    return '无效', 'BFARVALID/MMARVALID 均未置位，地址寄存器不可信'


# ---------------------------------------------------------------------------
# 最小 ELF32 只读符号表
# ---------------------------------------------------------------------------

SHT_SYMTAB = 2
STT_FUNC = 2


class ElfError(ValueError):
    """ELF 解析失败（非 ELF32/截断/无符号表）。"""


@dataclass(frozen=True)
class ElfSymbols:
    """按地址升序的函数符号表；lookup 返回最近符号与偏移。"""

    addrs: tuple[int, ...]
    names: tuple[str, ...]

    @classmethod
    def from_bytes(cls, data: bytes) -> 'ElfSymbols':
        syms = parse_elf32_functions(data)
        syms.sort()
        return cls(tuple(a for a, _ in syms), tuple(n for _, n in syms))

    @classmethod
    def from_file(cls, path: str) -> 'ElfSymbols':
        with open(path, 'rb') as f:
            return cls.from_bytes(f.read())

    def lookup(self, addr: int) -> tuple[str, int] | None:
        """地址 → (最近符号名, 偏移)；低于最小符号地址返回 None。"""
        idx = bisect.bisect_right(self.addrs, addr) - 1
        if idx < 0:
            return None
        return self.names[idx], addr - self.addrs[idx]


def parse_elf32_functions(data: bytes) -> list[tuple[int, str]]:
    """读小端 ELF32 的 .symtab/.strtab，收集 STT_FUNC 符号 [(地址, 名)]。

    只读实现：ELF 头 → 节头表 → 找 SHT_SYMTAB（其 sh_link 指向 .strtab）→
    16 字节符号项取 st_value/st_info/st_name。架构无关（ARM/RISC-V 皆用此布局）。
    """
    if len(data) < 52 or data[:4] != b'\x7fELF':
        raise ElfError('不是 ELF 文件（缺 \\x7fELF 魔数）')
    ei_class, ei_data = data[4], data[5]
    if ei_class != 1:
        raise ElfError('不是 32 位 ELF（EI_CLASS=%d）' % ei_class)
    if ei_data != 1:
        raise ElfError('不是小端 ELF（EI_DATA=%d）' % ei_data)
    e_shoff, = struct.unpack_from('<I', data, 0x20)
    e_shentsize, e_shnum = struct.unpack_from('<HH', data, 0x2E)
    if e_shoff == 0 or e_shentsize < 40 or e_shnum == 0:
        raise ElfError('无节头表（stripped 或截断）')
    if e_shoff + e_shentsize * e_shnum > len(data):
        raise ElfError('节头表超出文件（截断）')

    sections = []
    for i in range(e_shnum):
        off = e_shoff + i * e_shentsize
        sh_name, sh_type, _sh_flags, _sh_addr, sh_offset, sh_size, sh_link, \
            _sh_info, _sh_align, sh_entsize = struct.unpack_from('<10I', data, off)
        sections.append((sh_type, sh_offset, sh_size, sh_link, sh_entsize))

    out: list[tuple[int, str]] = []
    for sh_type, sh_offset, sh_size, sh_link, sh_entsize in sections:
        if sh_type != SHT_SYMTAB:
            continue
        if sh_link >= len(sections):
            continue
        _t, st_off, st_size, _l, _e = sections[sh_link]
        strtab = data[st_off:st_off + st_size]
        entsize = sh_entsize or 16
        if entsize < 16:
            continue
        count = sh_size // entsize
        for i in range(count):
            base = sh_offset + i * entsize
            if base + 16 > len(data):
                break
            st_name, st_value, _st_size, st_info, _other, _shndx = \
                struct.unpack_from('<IIIBBH', data, base)
            if (st_info & 0xF) != STT_FUNC or st_value == 0:
                continue
            if st_name >= len(strtab):
                continue
            end = strtab.find(b'\x00', st_name)
            if end < 0:
                end = len(strtab)
            name = strtab[st_name:end].decode('utf-8', errors='replace')
            if name:
                out.append((st_value, name))
    if not out:
        raise ElfError('符号表里没有函数符号（strip 过的发行固件？）')
    return out


# ---------------------------------------------------------------------------
# 文件级缓存（面板切历史条目/重符号化时避免反复读盘）
# ---------------------------------------------------------------------------

_elf_cache: dict[str, tuple[tuple[int, int], ElfSymbols | None, str]] = {}


def load_elf_symbols(path: str) -> tuple[ElfSymbols | None, str]:
    """按路径加载并缓存符号表；返回 (符号表|None, 失败原因|'')。

    缓存键含 mtime+size：固件重编译后同一路径自动失效。路径为空 → (None, '')。
    """
    clean = (path or '').strip().strip('"')
    if not clean:
        return None, ''
    try:
        st = os.stat(clean)
    except OSError as exc:
        _elf_cache.pop(clean, None)
        return None, f'文件不可访问: {exc}'
    stamp = (st.st_mtime_ns, st.st_size)
    hit = _elf_cache.get(clean)
    if hit and hit[0] == stamp:
        return hit[1], hit[2]
    try:
        syms = ElfSymbols.from_file(clean)
        err = ''
    except (ElfError, OSError, struct.error) as exc:
        syms, err = None, f'{type(exc).__name__}: {exc}'
    _elf_cache[clean] = (stamp, syms, err)
    return syms, err


def symbolize(syms: ElfSymbols | None, addr: int | None) -> str:
    """地址 → 'name+0x偏移'（如 servo_ctrl+0x1c）；无符号表/查不到给裸地址。"""
    if addr is None:
        return '—'
    if syms is not None:
        hit = syms.lookup(addr)
        if hit:
            name, off = hit
            return name if off == 0 else f'{name}+0x{off:x}'
    return f'0x{addr:08X}'
