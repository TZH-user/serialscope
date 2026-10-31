# -*- coding: utf-8 -*-
"""Linux 支持页（v1.3.0 更名，原「设备屏」v2 M2 原型）：UDP 收 H.264 裸流 → PyAV 软解 → QOpenGLWidget 渲染 + 统计。

协议（docs/设计_Linux设备互联_v2.md §4 视频面）：UDP 9202，包 =
[seq u32 LE][ts_ms u32 LE][flags u8][payload]，payload 为任意切分的 Annex-B 字节流
（MTU 切包可落在 NAL 中间），本页负责重组：按起始码切 NAL → 访问单元（AU）组包 →
av.CodecContext('h264') 软解 → 帧回调上屏。

渲染（设计锚点「OpenGL 直通，禁 QImage 拷贝」）：
- 主路径 GL 直通已实现：paintGL 内 glTexImage2D/glTexSubImage2D 直传 BGR 帧 + 着色器绘四边形；
- GL 上下文不可用时（如 QT_QPA_PLATFORM=offscreen 自测环境）回退 QPainter.drawImage 的
  ndarray→QImage 零拷贝视图快路径（内存直包不复制，M3 依据真机验证收敛两路）。
渲染慢只显最新帧（scrcpy 法）：解码线程仅覆盖 latest，绘制端永远取最新一帧。

丢包：seq 跳变累计丢包数并发 resync_needed（M2 原型仅提示；M3 经控制通道
发 resync{from_seq} 请求板端强制 IDR）。

控制面（M2 收口）：页上原本只被动收流，video_start 没人发——开始接收时
ControlClient 走 TCP 9201 控制通道 hello→pair→video_start（目标 = 控制连接
对端 IP + listen_port），停止时同连接发 video_stop。板端 server_rk 没在跑时
只报控制失败，UDP 收流继续（允许手工触发发流的自测路径）。
"""
from __future__ import annotations

import json
import queue
import re
import secrets
import socket
import struct
import threading
import time
from collections import deque

import numpy as np
from PySide6 import QtCore, QtGui, QtWidgets
from PySide6.QtGui import QImage, QSurfaceFormat
from PySide6.QtOpenGL import QOpenGLBuffer, QOpenGLShader, QOpenGLShaderProgram
from PySide6.QtOpenGLWidgets import QOpenGLWidget
from PySide6.QtNetwork import QHostAddress, QUdpSocket

from core.settings import read_setting, write_setting
from ui import theme

DEFAULT_UDP_PORT = 9202   # 设计 §4：视频面 UDP 9202
CONTROL_PORT_DEFAULT = 9201   # 设计 §4：控制面 TCP 9201
# M2 明文版配对码，与 server_rk/protocol.DEFAULT_PAIRING_CODE 一致（出厂值，
# 真板可经 pairing_code.txt/SERVER_RK_PAIRING_CODE 覆盖，页上可改）
PAIRING_CODE_DEFAULT = '246810'

_HEADER = struct.Struct('>IIB')   # [seq u32][ts_ms u32][flags u8]，网络字节序（与板端 server_rk.video.packetize 一致）
_START_CODE = b'\x00\x00\x01'
_NAL_SC = b'\x00\x00\x00\x01'     # 对外统一补 4 字节起始码（Annex-B）


def transport_delay_ms(ts32: int, now_epoch_ms: float, offset_ms: float) -> float:
    """单向传输时延估算（实时性量化证明）。

    ts32 = 板端发包时刻的 epoch 毫秒（u32 截断，≈49.7 天回绕一次，符号回卷处理）；
    offset_ms = 板端钟 - PC 钟（video_start 应答的 now_ms 折半 RTT 估出，板钟不准
    也无妨——板钟没 NTP 校时照样能用）。延迟 = PC 收到时刻 - PC 等效发包时刻。
    """
    ref = int(now_epoch_ms + offset_ms) & 0xFFFFFFFF
    d = (int(ts32) - ref) & 0xFFFFFFFF
    if d >= 0x80000000:
        d -= 0x100000000
    return -d

# GL 常量（PySide6 不暴露 GL 头文件常量，手写值）
_GL_TEXTURE_2D = 0x0DE1
_GL_RGB = 0x1907
_GL_BGR = 0x80E0
_GL_UNSIGNED_BYTE = 0x1401
_GL_FLOAT = 0x1406
_GL_LINEAR = 0x2601
_GL_TEXTURE_MIN_FILTER = 0x2801
_GL_TEXTURE_MAG_FILTER = 0x2600
_GL_TEXTURE_WRAP_S = 0x2802
_GL_TEXTURE_WRAP_T = 0x2803
_GL_CLAMP_TO_EDGE = 0x812F
_GL_TRIANGLE_STRIP = 0x0005
_GL_COLOR_BUFFER_BIT = 0x4000


def split_nals(blob: bytes):
    """把 Annex-B 字节流切成 NAL 列表（每项含起始码，统一为 4 字节）。

    兼容 3/4 字节起始码混排；起始码前的游离字节（垃圾/切包残片）按标准归入起始码。
    """
    nals = []
    marks = [m.start() for m in re.finditer(b'\x00\x00\x01', blob)]
    for k, m in enumerate(marks):
        start = m
        while start > 0 and blob[start - 1] == 0:   # 起始码前导 0 属于本起始码
            start -= 1
        end = marks[k + 1] if k + 1 < len(marks) else len(blob)
        while end > m + 3 and blob[end - 1] == 0:   # 尾部 0 属于下一起始码（4 字节 SC）
            end -= 1
        payload = blob[m + 3:end]
        if payload:
            nals.append(_NAL_SC + payload)
    return nals


class H264UdpSource(QtCore.QThread):
    """UDP 收流线程：绑端口 → 拆 [seq][ts][flags] 头 → 累计 payload 切 NAL → got_nals。

    丢包统计：seq 前跳 N>1 记丢 N-1 包并发 resync_needed（M3 经控制通道请求 IDR）；
    重复/迟到包丢弃（喂进解码器会污染码流）。
    """

    got_nals = QtCore.Signal(bytes)        # 完整 NAL（4 字节起始码前缀，可能多 NAL 连发）
    resync_needed = QtCore.Signal(int)     # seq 跳变：参数=最后连续 seq
    bound = QtCore.Signal(int)             # 绑定成功：参数=实际端口
    bind_failed = QtCore.Signal(str)

    ACC_MAX = 4 * 1024 * 1024   # 起始码对齐失败的垃圾洪泛保护上限

    def __init__(self, port: int = DEFAULT_UDP_PORT, parent=None):
        super().__init__(parent)
        self._port = int(port)
        self._stop = False
        self._acc = bytearray()        # 跨包 NAL 重组缓冲（按起始码切）
        self._last_seq: int | None = None
        # 统计（线程内写、UI 线程读：GIL 下 int 读安全）
        self.lost_count = 0
        self.late_count = 0
        self.dup_count = 0
        self.packet_count = 0
        self.bytes_count = 0
        self.last_ts_ms: int | None = None   # 最近一包板端发包时刻（epoch ms 低 32 位）
        self.is_bound = False
        self.sock: QUdpSocket | None = None

    def stop(self):
        """置停止位并等待线程退出（waitForReadyRead 100ms 超时保证响应）。"""
        self._stop = True
        self.wait(3000)

    def run(self):
        sock = QUdpSocket()
        self.sock = sock
        if not sock.bind(QHostAddress.SpecialAddress.AnyIPv4, self._port):
            self.bind_failed.emit(f'UDP {self._port} 绑定失败（端口被占用？）')
            return
        self.is_bound = True
        self.bound.emit(self._port)
        while not self._stop:
            # 无事件循环的独立线程：waitForReadyRead 轮询（100ms 超时保 stop 响应）
            if not sock.waitForReadyRead(100):
                continue
            while sock.hasPendingDatagrams() and not self._stop:
                size = sock.pendingDatagramSize()
                if size <= 0:
                    break
                data, _host, _port = sock.readDatagram(size)
                nals = self._process_datagram(data)
                if nals:
                    self.got_nals.emit(nals)
        sock.close()
        self.sock = None

    # ---------- 拆包 / 组 NAL（独立成方法：自测可不经 socket 直接喂数据） ----------

    def _process_datagram(self, data: bytes) -> bytes:
        """处理一个数据报：拆头 → 丢包判定 → NAL 重组。返回本次切出的 NAL 字节。"""
        self.packet_count += 1
        if len(data) <= _HEADER.size:
            return b''
        seq, ts_ms, _flags = _HEADER.unpack_from(data)
        self.last_ts_ms = ts_ms       # 逐帧传输时延估算用（最近一包的板端发包时刻）
        payload = data[_HEADER.size:]
        self.bytes_count += len(payload)
        if self._last_seq is not None:
            delta = (seq - self._last_seq) & 0xFFFFFFFF
            if delta == 0:
                self.dup_count += 1
                return b''                      # 重复包丢弃
            if delta >= 0x80000000:
                self.late_count += 1
                return b''                      # 迟到包（乱序回退）丢弃
            if delta > 1:
                self.lost_count += delta - 1    # seq 跳变 → 中间包丢失
                self.resync_needed.emit(self._last_seq)
        self._last_seq = seq
        return self._extract_nals(payload)

    def _extract_nals(self, chunk: bytes) -> bytes:
        """累计 payload，按起始码切出完整 NAL（补 4 字节起始码）；不完整部分留缓冲。"""
        self._acc.extend(chunk)
        if len(self._acc) > self.ACC_MAX:       # 长期对不上起始码 = 码流已坏，重对齐
            self._acc.clear()
            return b''
        out = bytearray()
        while True:
            m = self._acc.find(_START_CODE)
            if m < 0:
                self._acc.clear()               # 无起始码：整段垃圾丢弃
                break
            n = self._acc.find(_START_CODE, m + 3)
            if n < 0:
                if m > 0:
                    del self._acc[:m]           # 对齐到首个起始码，等余下字节
                break                           # 末尾 NAL 未收齐
            end = n
            while end > m + 3 and self._acc[end - 1] == 0:
                end -= 1                        # 尾部 0 归下一起始码
            if end > m + 3:
                out += _NAL_SC + bytes(self._acc[m + 3:end])
            del self._acc[:n]
        return bytes(out)


class VideoDecoder(QtCore.QObject):
    """H.264 软解码器（PyAV）：NAL 流 → AU 组包 → av.CodecContext('h264') → BGR ndarray 帧。

    AU 组包：VCL NAL（slice，first_mb_in_slice==0）或 SPS/PPS/SEI/AUD 到达且上个 AU 已含
    VCL 时收口上一帧——FFmpeg 7 的 h264 解码器要求按访问单元喂包（逐 NAL 喂 SPS 会报
    InvalidData）。末帧 AU 靠 drain() 收口（流式无 EOS）。
    线程：实例 moveToThread 到解码线程，feed_nals 经队列信号在解码线程执行。
    """

    frame_ready = QtCore.Signal(object)    # dict{frame: ndarray bgr24, width, height, ms}

    _VCL = {1, 2, 3, 4, 5}
    _AU_BREAKERS = {1, 2, 3, 4, 5, 6, 7, 8, 9}   # VCL/SEI/SPS/PPS/AUD 触发 AU 收口

    def __init__(self, parent=None):
        super().__init__(parent)
        import av                      # 延迟导入：PyAV 缺失时页面可给出可读提示
        self._av = av
        self.ctx = av.CodecContext.create('h264', 'r')   # 软解（无 hwaccel）
        self.ctx.options = {'threads': '1', 'flags': '+low_delay'}  # 低延迟：无帧级并行缓冲
        self._au = bytearray()
        self._au_has_vcl = False
        self._decode_ms: deque[float] = deque(maxlen=30)   # 滑动平均（最近 30 AU）
        self.frame_count = 0
        self.decode_errors = 0
        self.last_size: tuple[int, int] | None = None

    # ---------- 帧流入口（跨线程 queued 调用） ----------

    def feed_nals(self, blob: bytes):
        for nal in split_nals(blob):
            payload = nal[4:]
            t = payload[0] & 0x1F if payload else 0
            if self._au and t in self._AU_BREAKERS and self._au_has_vcl and \
                    (t not in self._VCL or self._first_mb_in_slice(payload) == 0):
                self._decode_au()       # 新 AU 开始 → 收口上一帧
            self._au += nal
            if t in self._VCL:
                self._au_has_vcl = True

    def drain(self):
        """流结束/主动收口：把攒着的最后一个 AU 喂给解码器。"""
        if self._au_has_vcl:
            self._decode_au()

    # ---------- 解码与统计 ----------

    def _decode_au(self):
        if not self._au:
            return
        packet = self._av.Packet(bytes(self._au))
        self._au = bytearray()
        self._au_has_vcl = False
        t0 = time.perf_counter()
        try:
            frames = self.ctx.decode(packet)
        except Exception:               # InvalidData 等码流错误：计数不断流
            self.decode_errors += 1
            return
        ms = (time.perf_counter() - t0) * 1000.0
        self._decode_ms.append(ms)
        for f in frames:
            arr = f.to_ndarray(format='bgr24')   # C 连续，可直接包 QImage / 上传 GL
            self.frame_count += 1
            self.last_size = (int(f.width), int(f.height))
            self.frame_ready.emit({'frame': arr, 'width': int(f.width),
                                   'height': int(f.height), 'ms': ms})

    @property
    def avg_decode_ms(self) -> float | None:
        if not self._decode_ms:
            return None
        return sum(self._decode_ms) / len(self._decode_ms)

    @staticmethod
    def _first_mb_in_slice(payload: bytes) -> int:
        """读 slice 头 ue(v) first_mb_in_slice（==0 为新帧起点；解析失败按 0）。"""
        lzb = 0
        pos = 8                            # 跳过 1 字节 NAL header
        total = len(payload) * 8

        def bit(i):
            return (payload[i >> 3] >> (7 - (i & 7))) & 1

        while pos < total and bit(pos) == 0:
            lzb += 1
            pos += 1
        if pos >= total:
            return 0
        val = 0
        pos += 1
        for _ in range(lzb):
            if pos >= total:
                return 0
            val = (val << 1) | bit(pos)
            pos += 1
        return (1 << lzb) - 1 + val


class ScreenGLWidget(QOpenGLWidget):
    """视频渲染区：GL 纹理直通主路径 + QPainter 零拷贝兜底。

    GL 直通：initializeGL 编译着色器/建 VBO/纹理；paintGL 把最新 BGR 帧直传
    GL_TEXTURE_2D（换尺寸重建，同尺寸 TexSubImage 增量传）+ 视口留黑边绘四边形。
    兜底（GL 不可用，如 offscreen 自测）：QPainter.drawImage 包 ndarray 内存视图，
    零拷贝快路径（禁 QImage 复制慢路径；M3 依据真机验证收敛两路）。
    """

    VERTEX_SRC = """
    attribute highp vec2 aPos;
    varying highp vec2 vUV;
    void main() {
        gl_Position = vec4(aPos, 0.0, 1.0);
        vUV = vec2(aPos.x * 0.5 + 0.5, 0.5 - aPos.y * 0.5);
    }
    """
    FRAGMENT_SRC = """
    uniform sampler2D uTex;
    varying highp vec2 vUV;
    void main() { gl_FragColor = texture2D(uTex, vUV); }
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        # 请求 GL2 兼容上下文：GLSL 100 着色器在桌面 GL 2.0/3.x 兼容档均可编译
        fmt = QSurfaceFormat()
        fmt.setVersion(2, 0)
        fmt.setProfile(QSurfaceFormat.OpenGLContextProfile.CompatibilityProfile)
        self.setFormat(fmt)
        self.setMinimumHeight(280)
        self.setSizePolicy(QtWidgets.QSizePolicy.Policy.Expanding,
                           QtWidgets.QSizePolicy.Policy.Expanding)
        self._latest: dict | None = None    # 最新帧（scrcpy 法：只显最新）
        self._gl_ready = False
        self._prog: QOpenGLShaderProgram | None = None
        self._vbo: QOpenGLBuffer | None = None
        self._tex = 0
        self._tex_size: tuple[int, int] | None = None
        # 鼠标注入（M3 前置）：开启后把控件内鼠标事件换算成板屏绝对坐标回调。
        # 移动 25ms 合并取最新（跟手且不刷爆控制通道）；按下/滚轮即时发送。
        self.mouse_ctl = False
        self.inject_cb = None               # callable(events: list[dict])，页面接控制通道
        self.setMouseTracking(True)
        self._mouse_pending: tuple[int, int] | None = None
        self._mouse_timer = QtCore.QTimer(self)
        self._mouse_timer.setInterval(25)
        self._mouse_timer.timeout.connect(self._flush_mouse_move)

    # ---------- 帧入口（解码线程信号 → UI 线程） ----------

    def set_frame(self, info: dict):
        self._latest = info
        self.update()

    def clear_frame(self):
        self._latest = None
        self.update()

    # ---------- 鼠标注入（坐标换算与事件回调） ----------

    def map_to_board(self, wx: float, wy: float) -> tuple[int, int] | None:
        """控件坐标 → 板屏像素（_fit_viewport_rect 的逆映射；letterbox 区返回 None）。

        换算链：控件逻辑坐标 ×DPR → 帧缓冲设备像素 →（减视口偏移、除缩放比）→
        板屏像素，再钳到 [0, W-1]×[0, H-1]。视口垂直居中对称，GL 的底原点
        不影响 y 向映射。
        """
        info = self._latest
        if info is None:
            return None
        dpr = self.devicePixelRatioF()
        vx, vy, dw, dh = self._fit_viewport_rect(
            self.width(), self.height(), info['width'], info['height'], dpr)
        px, py = wx * dpr, wy * dpr
        if not (vx <= px < vx + dw and vy <= py < vy + dh):
            return None                      # 黑边区域：不注入
        bw = info['width']
        bh = info['height']
        bx = int(max(0.0, min(1.0, (px - vx) / max(1, dw))) * (bw - 1))
        by = int(max(0.0, min(1.0, (py - vy) / max(1, dh))) * (bh - 1))
        return bx, by

    def _inject(self, events: list[dict]):
        if self.mouse_ctl and self.inject_cb is not None:
            self.inject_cb(events)

    def _flush_mouse_move(self):
        pos, self._mouse_pending = self._mouse_pending, None
        if pos is not None:
            self._inject([{'t': 'mouse_abs', 'x': pos[0], 'y': pos[1]}])

    def mousePressEvent(self, ev):  # noqa: N802 (Qt 命名)
        self._mouse_button(ev, True)
        super().mousePressEvent(ev)

    def mouseReleaseEvent(self, ev):  # noqa: N802 (Qt 命名)
        self._mouse_button(ev, False)
        super().mouseReleaseEvent(ev)

    def _mouse_button(self, ev, down: bool):
        if not self.mouse_ctl or self.inject_cb is None:
            return
        btn = {QtCore.Qt.MouseButton.LeftButton: 'left',
               QtCore.Qt.MouseButton.RightButton: 'right',
               QtCore.Qt.MouseButton.MiddleButton: 'middle'}.get(ev.button())
        if btn is None:
            return
        pos = self.map_to_board(ev.position().x(), ev.position().y())
        if pos is None:
            return
        # 按下/抬起前先落一次绝对定位：光标不在目标位时点击仍落在所指处
        self._inject([{'t': 'mouse_abs', 'x': pos[0], 'y': pos[1]},
                      {'t': 'mouse_button', 'btn': btn, 'down': down}])

    def mouseMoveEvent(self, ev):  # noqa: N802 (Qt 命名)
        if self.mouse_ctl:
            pos = self.map_to_board(ev.position().x(), ev.position().y())
            if pos is not None:
                self._mouse_pending = pos       # 25ms 合并，latest-wins（跟手）
        super().mouseMoveEvent(ev)

    def wheelEvent(self, ev):  # noqa: N802 (Qt 命名)
        if self.mouse_ctl and self.inject_cb is not None:
            dy = ev.angleDelta().y()
            dx = ev.angleDelta().x()
            if dy or dx:
                self._inject([{'t': 'wheel',
                               'dy': 1 if dy > 0 else (-1 if dy < 0 else 0),
                               'dx': 1 if dx > 0 else (-1 if dx < 0 else 0)}])
        super().wheelEvent(ev)

    def set_mouse_ctl(self, on: bool):
        self.mouse_ctl = bool(on)
        self.setCursor(QtCore.Qt.CursorShape.BlankCursor if on
                       else QtCore.Qt.CursorShape.ArrowCursor)
        if on:
            self._mouse_timer.start()
        else:
            self._mouse_timer.stop()
            self._mouse_pending = None

    # ---------- GL 生命周期 ----------

    def initializeGL(self):
        if not self.context().isValid():
            return
        try:
            prog = QOpenGLShaderProgram(self)
            if not (prog.addShaderFromSourceCode(QOpenGLShader.ShaderTypeBit.Vertex, self.VERTEX_SRC)
                    and prog.addShaderFromSourceCode(QOpenGLShader.ShaderTypeBit.Fragment,
                                                     self.FRAGMENT_SRC)
                    and prog.link()):
                raise RuntimeError('shader link failed')
            self._prog = prog
            vbo = QOpenGLBuffer(QOpenGLBuffer.Type.VertexBuffer)
            vbo.create()
            vbo.bind()
            quad = np.array([-1.0, -1.0, 1.0, -1.0, -1.0, 1.0, 1.0, 1.0], np.float32)
            vbo.allocate(quad.tobytes(), quad.nbytes)
            self._vbo = vbo
            f = self.context().functions()
            tex_ids = [0]                         # PySide6 形态：glGenTextures(n, 填充序列)
            f.glGenTextures(1, tex_ids)
            self._tex = int(tex_ids[0])
            f.glBindTexture(_GL_TEXTURE_2D, self._tex)
            f.glTexParameteri(_GL_TEXTURE_2D, _GL_TEXTURE_MIN_FILTER, _GL_LINEAR)
            f.glTexParameteri(_GL_TEXTURE_2D, _GL_TEXTURE_MAG_FILTER, _GL_LINEAR)
            f.glTexParameteri(_GL_TEXTURE_2D, _GL_TEXTURE_WRAP_S, _GL_CLAMP_TO_EDGE)
            f.glTexParameteri(_GL_TEXTURE_2D, _GL_TEXTURE_WRAP_T, _GL_CLAMP_TO_EDGE)
            f.glBindTexture(_GL_TEXTURE_2D, 0)
            self._gl_ready = True
        except Exception:
            self._gl_ready = False                # 回退 QPainter 快路径

    def paintGL(self):
        if not self.context().isValid():
            return                                # 无 GL（offscreen）：平台兜底纯色
        f = self.context().functions()
        if self._gl_ready:
            try:
                if self._paint_gl_direct(f):
                    self._restore_full_viewport(f)
                    return
            except Exception:
                self._gl_ready = False            # GL 路径异常 → 本次起走 QPainter 兜底
        self._paint_qt(f)

    def _restore_full_viewport(self, f):
        """恢复整帧视口（QOpenGLWidget 帧缓冲 = 控件尺寸 × devicePixelRatio）。"""
        dpr = self.devicePixelRatioF()
        f.glViewport(0, 0, max(1, int(self.width() * dpr)), max(1, int(self.height() * dpr)))

    @staticmethod
    def _fit_viewport_rect(widget_w: int, widget_h: int,
                           img_w: int, img_h: int, dpr: float) -> tuple[int, int, int, int]:
        """等比缩放居中的视口矩形（设备像素）。

        glViewport 的坐标系是 QOpenGLWidget 帧缓冲（控件逻辑尺寸 × devicePixelRatio），
        不是逻辑像素——早前用逻辑宽高算视口，高 DPR 屏（125%/150%）画面被缩成 1/dpr
        且错位（用户实报"画面小一半还不居中"）。返回 (x, y, w, h) 设备像素。
        """
        vw, vh = widget_w * dpr, widget_h * dpr
        if img_w <= 0 or img_h <= 0 or vw <= 0 or vh <= 0:
            return 0, 0, 1, 1
        scale = min(vw / img_w, vh / img_h)
        dw = max(1, int(img_w * scale))
        dh = max(1, int(img_h * scale))
        return int((vw - dw) / 2), int((vh - dh) / 2), dw, dh

    def _paint_gl_direct(self, f) -> bool:
        """GL 直通：纹理直传 + 着色器四边形。返回 False 表示无帧可画。"""
        info = self._latest
        f.glBindTexture(_GL_TEXTURE_2D, self._tex)
        if info is not None:
            arr = info['frame']
            w, h = info['width'], info['height']
            if self._tex_size != (w, h):
                f.glTexImage2D(_GL_TEXTURE_2D, 0, _GL_RGB, w, h, 0, _GL_BGR,
                               _GL_UNSIGNED_BYTE, arr.data)
                self._tex_size = (w, h)
            else:
                f.glTexSubImage2D(_GL_TEXTURE_2D, 0, 0, 0, w, h, _GL_BGR,
                                  _GL_UNSIGNED_BYTE, arr.data)
        f.glClearColor(0.0, 0.0, 0.0, 1.0)
        f.glClear(_GL_COLOR_BUFFER_BIT)
        if info is None:
            f.glBindTexture(_GL_TEXTURE_2D, 0)
            return False
        # 等比缩放居中（设备像素视口；见 _fit_viewport_rect 的 DPR 说明）
        vx, vy, dw, dh = self._fit_viewport_rect(
            self.width(), self.height(), info['width'], info['height'],
            self.devicePixelRatioF())
        f.glViewport(vx, vy, dw, dh)
        prog = self._prog
        prog.bind()
        prog.setUniformValue('uTex', 0)
        self._vbo.bind()
        loc = prog.attributeLocation('aPos')
        if loc >= 0:
            prog.setAttributeBuffer(loc, _GL_FLOAT, 0, 2, 0)
            prog.enableAttributeArray(loc)
            f.glDrawArrays(_GL_TRIANGLE_STRIP, 0, 4)
            prog.disableAttributeArray(loc)
        self._vbo.release()
        prog.release()
        f.glBindTexture(_GL_TEXTURE_2D, 0)
        return True

    def _paint_qt(self, f):
        """QPainter 零拷贝快路径：ndarray 内存视图直包 QImage（无复制；M3 收敛 GL 直通）。"""
        f.glClear(_GL_COLOR_BUFFER_BIT)
        painter = QtGui.QPainter(self)
        painter.fillRect(self.rect(), QtGui.QColor(0, 0, 0))
        info = self._latest
        if info is None:
            painter.setPen(QtGui.QColor(theme.color('text_dim')))
            painter.drawText(self.rect(), QtCore.Qt.AlignmentFlag.AlignCenter,
                             f'等待视频流…（UDP {DEFAULT_UDP_PORT}）\n'
                             '[seq u32][ts_ms u32][flags u8][payload NAL]')
        else:
            arr = info['frame']
            img = QImage(arr.data, info['width'], info['height'], arr.strides[0],
                         QImage.Format.Format_BGR888)   # 零拷贝视图（arr 由 _latest 持活）
            vw, vh = self.width(), self.height()
            scale = min(vw / info['width'], vh / info['height'])
            dw, dh = max(1, int(info['width'] * scale)), max(1, int(info['height'] * scale))
            target = QtCore.QRect((vw - dw) // 2, (vh - dh) // 2, dw, dh)
            painter.drawImage(target, img)   # QRect 重载 = 等比缩放落目标矩形
        painter.end()


class ControlError(RuntimeError):
    """控制协议层的失败（对端回 ok=false / 断连 / 超时），message 即给用户看的一句。"""


class ControlClient(QtCore.QThread):
    """控制面客户端：一条连接上 hello→pair→video_start 一气呵成，会话保活。

    会话期间 send_command() 可下发任意命令（鼠标注入 input 等）：线程经队列
    取命令逐条问答；request_stop() 置位后同连接发 video_stop 再收尾（板端先停流、
    后撤 UDP，避免 ffmpeg 已停而页面还在等的空转窗口）。connect_fn 可注入，自测换
    loopback 假服务端；report(文本, 级别) 逐步汇报，级别对应状态行配色。
    """

    report = QtCore.Signal(str, str)   # (状态文本, 'status_ok'|'status_warn'|'status_err')

    QUEUE_MAX = 128            # 命令队列上限（鼠标移动是 latest-wins，满了丢不致命）

    def __init__(self, host: str, control_port: int, code: str, udp_port: int,
                 connect_fn=None, parent=None, overlay: bool = True,
                 overlay_opacity: float = 0.85):
        super().__init__(parent)
        self.host = host
        self.control_port = int(control_port)
        self.code = code
        self.udp_port = int(udp_port)
        self.overlay = bool(overlay)                    # 跑表 overlay 请求态
        self.overlay_opacity = max(0.05, min(1.0, float(overlay_opacity)))
        self._connect_fn = connect_fn or (
            lambda hp, timeout: socket.create_connection(hp, timeout=timeout))
        self._stop_evt = threading.Event()
        self._q: queue.Queue = queue.Queue(maxsize=self.QUEUE_MAX)
        self.board_offset_ms: float | None = None   # 板钟-PC钟（video_start 应答基准）
        self.board_overlay = False                  # 板端跑表 overlay 是否已烙进画面

    def request_stop(self):
        self._stop_evt.set()

    def send_command(self, req: dict):
        """会话期下发一条命令（线程安全；队列满丢弃并报一次）。"""
        try:
            self._q.put_nowait(req)
        except queue.Full:
            self.report.emit('控制通道命令队列已满（板端响应慢？），本次已丢弃', 'status_warn')

    @staticmethod
    def _ask(sock: socket.socket, req: dict, timeout: float = 5.0) -> dict:
        """发一问收一答（JSON 行协议）；ok=false 抛 ControlError。"""
        sock.settimeout(timeout)
        sock.sendall((json.dumps(req, ensure_ascii=False) + '\n').encode('utf-8'))
        buf = bytearray()
        while b'\n' not in buf:
            chunk = sock.recv(4096)
            if not chunk:
                raise ConnectionError('控制连接被对端关闭')
            buf.extend(chunk)
        line, _, _ = bytes(buf).partition(b'\n')
        resp = json.loads(line.decode('utf-8'))
        if not resp.get('ok'):
            raise ControlError(str(resp.get('error', resp)))
        return resp

    def run(self):
        try:
            with self._connect_fn((self.host, self.control_port), 3.0) as sock:
                self.report.emit(
                    f'控制通道 {self.host}:{self.control_port} 已连接，配对中…', 'status_warn')
                hello = self._ask(sock, {'cmd': 'hello', 'v': 1,
                                         'nonce': secrets.token_hex(8)})
                self._ask(sock, {'cmd': 'pair', 'code': self.code,
                                 'challenge': hello.get('challenge', '')})
                # video_start 冷启动含板端采集/编码探测（~10s 量级），超时给足
                t0 = time.time() * 1000
                resp = self._ask(sock, {'cmd': 'video_start', 'listen_port': self.udp_port,
                                        'overlay': self.overlay,
                                        'overlay_opacity': self.overlay_opacity},
                                 timeout=20.0)
                t1 = time.time() * 1000
                now_ms = int(resp.get('now_ms') or 0)
                if now_ms:
                    # 板钟差 = 板端应答时刻 - PC 收到应答时刻。不能用折半 RTT：
                    # video_start 的应答在长探测后才打时间戳（处理器工作横跨 t0..t1），
                    # 折半模型会把钟差抬高 handler/2（真机实测虚增 0.5~5s 的"延迟"）；
                    # 以收到时刻为基准误差只剩发送单程 ≈ RTT/2（局域网 1~2ms）。
                    self.board_offset_ms = now_ms - t1
                self.board_overlay = bool(resp.get('overlay'))
                target = resp.get('target') or f'{self.host}:{self.udp_port}'
                note = f'（{resp["note"]}）' if resp.get('note') else ''
                tip = ('；画面左上角为板端毫秒跑表（实时性定性证明）'
                       if self.board_overlay else '')
                self.report.emit(f'板端已发流 → {target}{note}{tip}', 'status_ok')
                # 会话保活循环：取队列命令逐条问答（鼠标注入等）；停止位或哨兵退出
                while True:
                    try:
                        req = self._q.get(timeout=0.2)
                    except queue.Empty:
                        if self._stop_evt.is_set():
                            break
                        continue
                    if req is None:
                        break
                    self._ask(sock, req, timeout=5.0)
                try:
                    self._ask(sock, {'cmd': 'video_stop'}, timeout=3.0)
                    self.report.emit('已请求板端停流', 'status_warn')
                except Exception:              # 对端可能已退出：停流目的已达
                    pass
        except ControlError as exc:
            self.report.emit(f'控制通道: {exc}', 'status_err')
        except OSError as exc:
            self.report.emit(f'控制通道不可达（板端 server_rk 没在跑？）: {exc}', 'status_err')


class SysStatsPoller(QtCore.QThread):
    """板端系统状态 1Hz 轮询线程：独立于发流的 ControlClient（不看画面也能看状态）。

    连接 → 配对 → 循环 sys_stats（服务端 handler 见 server_rk/sys_stats.py，差值
    字段首次缺省）；断线退避 3s 重连。sample(dict) 每次采样发给 UI，report(文本,
    级别) 汇报连接状态。connect_fn 可注入（自测走 socketpair 假服务端，不碰真网）。
    """

    sample = QtCore.Signal(dict)
    report = QtCore.Signal(str, str)

    RETRY_DELAY_S = 3.0

    def __init__(self, host: str, control_port: int, code: str,
                 connect_fn=None, parent=None):
        super().__init__(parent)
        self.host = host
        self.control_port = int(control_port)
        self.code = code
        self._connect_fn = connect_fn or (
            lambda hp, timeout: socket.create_connection(hp, timeout=timeout))
        self._stop_evt = threading.Event()

    def request_stop(self):
        self._stop_evt.set()

    def run(self):
        while not self._stop_evt.is_set():
            try:
                with self._connect_fn((self.host, self.control_port), 3.0) as sock:
                    hello = ControlClient._ask(sock, {'cmd': 'hello', 'v': 1,
                                                      'nonce': secrets.token_hex(8)})
                    ControlClient._ask(sock, {'cmd': 'pair', 'code': self.code,
                                              'challenge': hello.get('challenge', '')})
                    self.report.emit('系统状态：已连接（1Hz）', 'status_ok')
                    while not self._stop_evt.is_set():
                        resp = ControlClient._ask(sock, {'cmd': 'sys_stats'}, timeout=3.0)
                        self.sample.emit(resp.get('stats') or {})
                        if self._stop_evt.wait(1.0):    # 分片节拍，停止即刻生效
                            break
            except (ControlError, OSError, ValueError) as exc:
                if self._stop_evt.is_set():
                    break
                self.report.emit(f'系统状态: {exc}', 'status_warn')
                if self._stop_evt.wait(self.RETRY_DELAY_S):
                    break


class DeviceScreenPage(QtWidgets.QWidget):
    """「Linux 支持」页：工具行（开始/停止 + UDP 端口 + 状态）+ 渲染区 + 统计行。

    管线：H264UdpSource(QThread) --got_nals(queued)--> VideoDecoder(解码线程)
          --frame_ready(queued)--> ScreenGLWidget（只显最新帧）。
    """

    STATS_INTERVAL_MS = 500

    def __init__(self, parent=None):
        super().__init__(parent)
        self.source: H264UdpSource | None = None
        self.decoder: VideoDecoder | None = None
        self._decode_thread: QtCore.QThread | None = None
        self.control: ControlClient | None = None
        self._last_frame_count = 0
        self._fps = 0.0
        self._last_delay_ms: float | None = None

        # ---- 工具行 ----
        self.btn_start = QtWidgets.QPushButton('开始接收')
        self.btn_start.setObjectName('tool')
        self.btn_start.clicked.connect(self.toggle_stream)
        self.edit_host = QtWidgets.QLineEdit(read_setting('linux/board_host', '192.168.2.100'))
        self.edit_host.setMaximumWidth(130)
        self.edit_host.setPlaceholderText('板 IP')
        self.edit_host.setToolTip('板端 server_rk 所在地址；开始接收时经控制通道 TCP 触发发流')
        self.edit_host.editingFinished.connect(
            lambda: write_setting('linux/board_host', self.edit_host.text().strip()))
        self.spin_control = QtWidgets.QSpinBox()
        self.spin_control.setRange(1024, 65535)
        self.spin_control.setValue(CONTROL_PORT_DEFAULT)
        self.spin_control.setToolTip('控制面 TCP 端口（设计 §4，server_rk 监听）')
        self.edit_code = QtWidgets.QLineEdit(read_setting('linux/pairing_code',
                                                          PAIRING_CODE_DEFAULT))
        self.edit_code.setMaximumWidth(96)   # 80px 会把 246810 裁显成 46810（值不裁）
        self.edit_code.setToolTip('控制通道配对码（M2 明文版；与板端 server_rk 配置一致）')
        self.edit_code.editingFinished.connect(
            lambda: write_setting('linux/pairing_code', self.edit_code.text().strip()))
        self.spin_port = QtWidgets.QSpinBox()
        self.spin_port.setRange(1024, 65535)
        self.spin_port.setValue(DEFAULT_UDP_PORT)
        self.spin_port.setToolTip('视频面 UDP 端口（设计 §4，板端发流到本端口）\n'
                                  '包 = [seq u32][ts_ms u32][flags u8][payload NAL]\n'
                                  '开始接收时经控制通道把本端口作为 listen_port 告知板端')
        # 跑表 overlay 开关与透明度：改动即存设置；正在发流时自动重起流生效
        self.chk_watch = QtWidgets.QCheckBox('跑表')
        self.chk_watch.setChecked(read_setting('linux/stopwatch', '1') in ('1', 'true', 'yes'))
        self.chk_watch.setToolTip('板端把毫秒跑表烙进画面左上角（实时性定性证明）。\n'
                                  '影响看画面时可关；关闭/开启会重起流（1~2s）')
        self.spin_op = QtWidgets.QSpinBox()
        self.spin_op.setRange(10, 100)
        self.spin_op.setSuffix(' %')
        self.spin_op.setValue(int(read_setting('linux/stopwatch_op', '85')))
        self.spin_op.setToolTip('跑表不透明度（10%~100%）；正在发流时改动自动重起流生效')
        self.chk_watch.toggled.connect(self._on_overlay_changed)
        self.spin_op.editingFinished.connect(self._on_overlay_changed)
        # 鼠标控制：开启后控件内点击/移动/滚轮注入板端（绝对坐标，跟手）
        self.chk_mouse = QtWidgets.QCheckBox('鼠标控制')
        self.chk_mouse.setChecked(False)
        self.chk_mouse.setToolTip('开启后鼠标在本画面上的点击/移动/滚轮注入板端'
                                  '（绝对坐标跟手；画面里的板端光标会跟着走）。\n'
                                  '需要板端 server_rk 以 root 运行（/dev/uinput）')
        self.chk_mouse.toggled.connect(self._on_mouse_ctl)
        # 系统状态（1Hz 轮询板端 sys_stats）：独立小连接，不看画面也能看板况
        self.chk_sys = QtWidgets.QCheckBox('系统状态')
        self.chk_sys.setChecked(read_setting('linux/sys_stats', '1') in ('1', 'true', 'yes'))
        self.chk_sys.setToolTip('1Hz 轮询板端 CPU/内存/eMMC/温度/负载/运行时长。\n'
                                '独立于发流的小连接；板端需 server_rk 在跑')
        self.chk_sys.toggled.connect(self._on_sys_stats_toggled)
        self.lbl_status = QtWidgets.QLabel('空闲')
        self.lbl_status.setObjectName('status_err')
        self.lbl_status.setToolTip('M2 原型：只读播放；M3 加键鼠交互与延迟 overlay')

        tool = QtWidgets.QHBoxLayout()
        tool.addWidget(self.btn_start)
        tool.addWidget(QtWidgets.QLabel('板 IP'))
        tool.addWidget(self.edit_host)
        tool.addWidget(QtWidgets.QLabel('控制'))
        tool.addWidget(self.spin_control)
        tool.addWidget(QtWidgets.QLabel('配对码'))
        tool.addWidget(self.edit_code)
        tool.addWidget(QtWidgets.QLabel('UDP 端口'))
        tool.addWidget(self.spin_port)
        tool.addWidget(self.chk_watch)
        tool.addWidget(QtWidgets.QLabel('透明度'))
        tool.addWidget(self.spin_op)
        tool.addWidget(self.chk_mouse)
        tool.addWidget(self.lbl_status, 1)

        # ---- 渲染区 + 统计行 ----
        self.gl_widget = ScreenGLWidget()
        self.lbl_fps = QtWidgets.QLabel('帧率 -- fps')
        self.lbl_dec = QtWidgets.QLabel('解码 -- ms')
        self.lbl_lost = QtWidgets.QLabel('丢包 0')
        self.lbl_delay = QtWidgets.QLabel('传输 --')
        self.lbl_delay.setToolTip(
            '单向传输时延估算 = PC 收到帧时刻 - 板端发包时刻\n'
            '（板钟差用 video_start 应答折半 RTT 校正，板钟未校时也能用）\n'
            '含解码/上屏排队，数值偏保守；局域网正常应在几十 ms 内')
        for lbl in (self.lbl_fps, self.lbl_dec, self.lbl_lost, self.lbl_delay):
            lbl.setObjectName('backlog')
        stats = QtWidgets.QHBoxLayout()
        stats.addWidget(self.lbl_fps)
        stats.addWidget(self.lbl_dec)
        stats.addWidget(self.lbl_lost)
        stats.addWidget(self.lbl_delay)
        stats.addStretch(1)

        # ---- 系统状态行（勾选「系统状态」后 1Hz 刷新）----
        self.lbl_cpu = QtWidgets.QLabel('CPU --%')
        self.lbl_mem = QtWidgets.QLabel('内存 --/-- MB')
        self.lbl_emmc = QtWidgets.QLabel('eMMC --')
        self.lbl_temp = QtWidgets.QLabel('温度 --℃')
        self.lbl_load = QtWidgets.QLabel('负载 --')
        self.lbl_up = QtWidgets.QLabel('运行 --')
        for lbl in (self.lbl_cpu, self.lbl_mem, self.lbl_emmc,
                    self.lbl_temp, self.lbl_load, self.lbl_up):
            lbl.setObjectName('backlog')
        sysrow = QtWidgets.QHBoxLayout()
        sysrow.addWidget(self.chk_sys)
        sysrow.addWidget(self.lbl_cpu)
        sysrow.addWidget(self.lbl_mem)
        sysrow.addWidget(self.lbl_emmc)
        sysrow.addWidget(self.lbl_temp)
        sysrow.addWidget(self.lbl_load)
        sysrow.addWidget(self.lbl_up)
        sysrow.addStretch(1)

        lay = QtWidgets.QVBoxLayout(self)
        lay.setContentsMargins(6, 6, 6, 6)
        lay.setSpacing(4)
        lay.addLayout(tool)
        lay.addWidget(self.gl_widget, 1)
        lay.addLayout(stats)
        lay.addLayout(sysrow)

        self._stats_timer = QtCore.QTimer(self)
        self._stats_timer.timeout.connect(self._update_stats)
        self.gl_widget.inject_cb = self._send_inject

        # 系统状态轮询：按设置自启（离屏自测环境不自动起真网线程；自测经
        # _start_sys_poller 显式注入假服务端）；板 IP/控制口/配对码改动即重连
        self._sys_poller: SysStatsPoller | None = None
        self.edit_host.editingFinished.connect(self._restart_sys_poller)
        self.spin_control.editingFinished.connect(self._restart_sys_poller)
        self.edit_code.editingFinished.connect(self._restart_sys_poller)
        if self.chk_sys.isChecked():
            self._on_sys_stats_toggled(True)

        # 主题联动：贴现有页面写法（启动同步一次 + set_theme 通知刷新）
        self.apply_theme()
        theme.register_listener(self.apply_theme)

    # ---------- 开始 / 停止 ----------

    def toggle_stream(self):
        if self.source is None:
            self._start_pipeline()
        else:
            self._stop_pipeline()

    def _start_pipeline(self):
        if self.source is not None:
            return
        try:
            decoder = VideoDecoder()
        except ImportError as exc:      # PyAV 缺失：给可读提示，不影响工具其余功能
            self._set_status(f'PyAV 未安装（pip install av）: {exc}', 'status_err')
            return
        self._decode_thread = QtCore.QThread(self)
        self._decode_thread.setObjectName('device_screen_decode')
        decoder.moveToThread(self._decode_thread)
        self.decoder = decoder
        self.source = H264UdpSource(self.spin_port.value())
        self.source.got_nals.connect(decoder.feed_nals)        # 跨线程 queued
        self.source.bound.connect(self._on_bound)
        self.source.bind_failed.connect(self._on_bind_failed)
        self.source.resync_needed.connect(self._on_resync)
        decoder.frame_ready.connect(self._on_frame_ready)
        self._decode_thread.start()
        self.source.start()
        self.btn_start.setText('停止')
        self.spin_port.setEnabled(False)
        self._set_status(f'绑定中 udp://0.0.0.0:{self.spin_port.value()} …', 'status_warn')
        self._stats_timer.start(self.STATS_INTERVAL_MS)
        self._start_control()

    def _start_control(self):
        """起控制通道线程：板 IP 为空则跳过（纯收流模式），否则自动触发发流。"""
        host = self.edit_host.text().strip()
        if not host:
            self._set_status('未填板 IP：仅收流等待（可在左侧填板端地址自动触发发流）',
                             'status_warn')
            return
        self.control = ControlClient(host, self.spin_control.value(),
                                     self.edit_code.text().strip(),
                                     self.spin_port.value(), parent=self,
                                     overlay=self.chk_watch.isChecked(),
                                     overlay_opacity=self.spin_op.value() / 100.0)
        self.control.report.connect(self._set_status)
        self.control.start()

    def _on_overlay_changed(self):
        """跑表开关/透明度改动：存设置；正在发流则重起流让板端按新参数烙表。"""
        write_setting('linux/stopwatch', '1' if self.chk_watch.isChecked() else '0')
        write_setting('linux/stopwatch_op', str(self.spin_op.value()))
        if self.source is not None:
            self._set_status('跑表参数变更，重起流中…', 'status_warn')
            self._stop_pipeline()
            self._start_pipeline()

    def _on_mouse_ctl(self, on: bool):
        self.gl_widget.set_mouse_ctl(on)
        if on and self.control is None:
            self._set_status('鼠标控制需要控制通道：请填板 IP 并开始接收（自动触发发流）',
                             'status_warn')
        elif on:
            self._set_status('鼠标控制已开启：画面内点击/拖动/滚轮直接注入板端', 'status_ok')

    def _send_inject(self, events: list[dict]):
        ctl = self.control
        if ctl is not None:
            ctl.send_command({'cmd': 'input', 'events': events})

    # ---------- 系统状态轮询 ----------

    def _on_sys_stats_toggled(self, on: bool):
        write_setting('linux/sys_stats', '1' if on else '0')
        if on:
            # 离屏自测环境不起真网线程（自测经 _start_sys_poller 显式注入假服务端）
            if QtWidgets.QApplication.instance().platformName() != 'offscreen':
                self._start_sys_poller()
        else:
            self._stop_sys_poller()

    def _start_sys_poller(self, connect_fn=None):
        self._stop_sys_poller()
        host = self.edit_host.text().strip()
        if not host:
            return
        poller = SysStatsPoller(host, self.spin_control.value(),
                                self.edit_code.text().strip(),
                                connect_fn=connect_fn, parent=self)
        poller.sample.connect(self._on_sys_sample)
        poller.report.connect(self._set_status)
        self._sys_poller = poller
        poller.start()

    def _stop_sys_poller(self):
        poller, self._sys_poller = self._sys_poller, None
        if poller is not None:
            poller.request_stop()
            poller.wait(4000)       # 覆盖 sys_stats 3s 问答超时的最坏收尾

    def _restart_sys_poller(self):
        if self.chk_sys.isChecked():
            self._start_sys_poller()

    def _on_sys_sample(self, stats: dict):
        """板端 1Hz 采样上屏；缺键（首拍差值/半行）渲染成 --，不猜值。"""
        cpu = stats.get('cpu_percent')
        self.lbl_cpu.setText('CPU --%' if cpu is None else f'CPU {cpu:.0f}%')
        mem_t, mem_u = stats.get('mem_total_mb'), stats.get('mem_used_mb')
        self.lbl_mem.setText('--/-- MB' if mem_t is None or mem_u is None
                             else f'内存 {mem_u:.0f}/{mem_t:.0f} MB')
        dt, df = stats.get('disk_total_gb'), stats.get('disk_free_gb')
        used = dt - df if (dt is not None and df is not None) else None
        dev = stats.get('disk_dev', '')
        self.lbl_emmc.setText('eMMC --' if used is None
                              else f'eMMC {used:.1f}/{dt:.1f} GB' + (f'·{dev}' if dev else ''))
        temp = stats.get('temp_c')
        self.lbl_temp.setText('温度 --℃' if temp is None else f'温度 {temp:.0f}℃')
        load = stats.get('load1')
        self.lbl_load.setText('负载 --' if load is None else f'负载 {load:.2f}')
        up = stats.get('uptime_s')
        self.lbl_up.setText('运行 --' if up is None
                            else f'运行 {up // 86400}天{up % 86400 // 3600:02d}:{up % 3600 // 60:02d}')

    def _stop_pipeline(self):
        self._stats_timer.stop()
        if self.control is not None:
            # 先停流（video_stop 走控制连接）再撤 UDP，避免 ffmpeg 停了页面还在等
            self.control.request_stop()
            self.control.wait(6000)     # 覆盖连接 3s / 问答 5s 超时的最坏收尾
            self.control = None
        if self.source is not None:
            self.source.stop()
            self.source = None
        if self._decode_thread is not None:
            self._decode_thread.quit()
            self._decode_thread.wait(3000)
            self._decode_thread = None
        self.decoder = None
        self.gl_widget.clear_frame()
        self._last_frame_count = 0
        self._last_delay_ms = None
        self.lbl_delay.setText('传输 --')
        self.btn_start.setText('开始接收')
        self.spin_port.setEnabled(True)
        self._set_status('空闲', 'status_err')

    def shutdown(self):
        """主窗关闭时的收尾（幂等，未启动时为空操作）。"""
        if self.source is not None or self._decode_thread is not None:
            self._stop_pipeline()
        self._stop_sys_poller()

    # ---------- 信号槽 ----------

    def _on_bound(self, port: int):
        self._set_status(f'接收中 udp://0.0.0.0:{port}（等待 H.264 流…）', 'status_ok')

    def _on_bind_failed(self, msg: str):
        self._stop_pipeline()
        self._set_status(msg, 'status_err')

    def _on_resync(self, last_seq: int):
        # M2 原型：只提示。M3 经控制通道发 resync{from_seq} 请求板端强制 IDR
        self._set_status(f'检测到丢包（seq>{last_seq}），需请求关键帧（M3 接控制通道）',
                         'status_err')

    def _on_frame_ready(self, info: dict):
        self.gl_widget.set_frame(info)
        # 传输时延逐帧更新（板端发包时刻 + 钟差 → 单向时延估算）
        src, ctl = self.source, self.control
        if (src is not None and src.last_ts_ms is not None and ctl is not None
                and ctl.board_offset_ms is not None):
            self._last_delay_ms = transport_delay_ms(
                src.last_ts_ms, time.time() * 1000, ctl.board_offset_ms)

    # ---------- 统计行 ----------

    def _update_stats(self):
        dec = self.decoder
        if dec is None:
            return
        self._fps = (dec.frame_count - self._last_frame_count) * 1000.0 / self.STATS_INTERVAL_MS
        self._last_frame_count = dec.frame_count
        self.lbl_fps.setText(f'帧率 {self._fps:.1f} fps（共 {dec.frame_count} 帧）')
        avg = dec.avg_decode_ms
        self.lbl_dec.setText(f'解码 {avg:.1f} ms' if avg is not None else '解码 -- ms')
        lost = self.source.lost_count if self.source else 0
        self.lbl_lost.setText(f'丢包 {lost}')
        d = getattr(self, '_last_delay_ms', None)
        self.lbl_delay.setText(f'传输≈{d:.0f} ms' if d is not None else '传输 --')

    def _set_status(self, text: str, obj_name: str):
        """状态标签（对齐 main_window.set_status：QSS objectName 切色需重 polish）。"""
        self.lbl_status.setText(text)
        self.lbl_status.setObjectName(obj_name)
        self.lbl_status.style().unpolish(self.lbl_status)
        self.lbl_status.style().polish(self.lbl_status)

    # ---------- 主题联动 ----------

    def apply_theme(self, name: str | None = None):
        """主题切换回调（theme.set_theme 通知）：无信号提示文字随主题换色。"""
        self.gl_widget.update()
