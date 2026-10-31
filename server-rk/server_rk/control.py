# -*- coding: utf-8 -*-
"""control.py — TCP 9201 控制服务：每连接一线程 + 命令分发表（设计稿 §2/§4）。

结构（依赖注入便于测试）：
- Deps            板端能力集合（video/injector/serial），全部可换成 fake；
                  测试不经 socket 直接调 handle_line 即可覆盖全部分发表。
- PairingSession  每条连接一个（配对状态不跨连接共享），协议见 protocol.py。
- ControlServer   threading 监听线程 + 每连接一线程；M2 骨架不做心跳/加密。

命令分发表（未配对时除 hello/pair 外一律 {"ok":false,"error":"未配对"}）：
    ping / status / hello / pair
    video_start / video_stop / video_params
    input
    sys_stats（板端 1Hz 系统状态：CPU/内存/eMMC/温度/负载，PC 轮询；差值字段首次缺省）
    serial_open / serial_write / serial_read
    close（回 ok 后服务端主动断开）
"""
from __future__ import annotations

import logging
import socket
import threading
import time
from dataclasses import dataclass

from . import SERVER_VERSION
from . import sys_stats
from .protocol import (PairingSession, decode_line, encode_msg, error_resp,
                       load_pairing_code, ok_resp)

log = logging.getLogger(__name__)


@dataclass
class Deps:
    """板端能力集合（DI 容器）：单测里 video/injector/serial 均可注入 fake。"""
    video: object
    injector: object
    serial: object
    video_udp_port: int = 9202            # video_start 缺省 udp target 端口
    server_version: str = SERVER_VERSION


@dataclass
class ControlContext:
    """单次命令处理的上下文（handler 通过它访问依赖与连接信息）。"""
    deps: Deps
    pairing: PairingSession
    peer_ip: str = ""
    close_requested: bool = False         # close 命令置位 → 回完响应断开


# ---------- 命令 handler（签名统一 handler(ctx, req) -> dict）----------

def _h_ping(ctx: ControlContext, req: dict) -> dict:
    return ok_resp(pong=True, ts=round(time.time(), 3))


def _h_status(ctx: ControlContext, req: dict) -> dict:
    d = ctx.deps
    return ok_resp(server_version=d.server_version, paired=ctx.pairing.paired,
                   video_udp_port=d.video_udp_port,
                   video=d.video.describe(),
                   input={"available": d.injector.is_available},
                   serial=d.serial.describe())


def _h_hello(ctx: ControlContext, req: dict) -> dict:
    return ctx.pairing.on_hello(req)


def _h_pair(ctx: ControlContext, req: dict) -> dict:
    return ctx.pairing.on_pair(req)


def _h_video_start(ctx: ControlContext, req: dict) -> dict:
    d = ctx.deps
    if d.video is None:
        return error_resp("视频源未配置")
    # udp target 缺省 = 控制连接对端 IP + 视频 UDP 端口（PC 与控制通道同机即免配）；
    # listen_port 允许 PC 端收流口非默认（IP 恒取控制连接对端，端口以请求为准）
    try:
        port = int(req["listen_port"]) if req.get("listen_port") else d.video_udp_port
    except (TypeError, ValueError):
        return error_resp("listen_port 必须是整数")
    target = req.get("target") or (
        f"{ctx.peer_ip}:{port}" if ctx.peer_ip else None)
    if not target:
        return error_resp("缺少 target（无法从控制连接推断 PC 地址）")
    # 跑表 overlay 开关与透明度（用户实报常开影响看画面，PC 端可关/调淡）
    overlay = bool(req.get("overlay", True))
    try:
        op = float(req["overlay_opacity"]) if req.get("overlay_opacity") is not None \
            else 0.85
    except (TypeError, ValueError):
        return error_resp("overlay_opacity 必须是数字")
    ok, msg = d.video.start(resolution=(req["width"], req["height"])
                            if req.get("width") and req.get("height") else None,
                            fps=req.get("fps"), bitrate=req.get("bitrate"),
                            udp_target=target, overlay=overlay, overlay_opacity=op)
    if not ok:
        return error_resp(msg)
    # now_ms = 板端 epoch 毫秒：PC 端用它对齐板钟（折半 RTT 估钟差），逐帧算单向
    # 传输时延——实时性的量化证明。overlay = 跑表是否烙进画面（定性证明）。
    return ok_resp(target=target, note=msg,
                   now_ms=int(time.time() * 1000),
                   overlay=bool(getattr(d.video, "overlay", False)))


def _h_video_stop(ctx: ControlContext, req: dict) -> dict:
    ctx.deps.video.stop()
    return ok_resp(stopped=True)


def _h_video_params(ctx: ControlContext, req: dict) -> dict:
    return ok_resp(params=ctx.deps.video.describe())


def _h_input(ctx: ControlContext, req: dict) -> dict:
    # 鼠标绝对定位需要 X 会话环境（DISPLAY/XAUTHORITY）——与采集管线同源喂入
    env = getattr(ctx.deps.video, "last_env", None)
    if env:
        ctx.deps.injector.pointer_env = env
    applied, skipped = ctx.deps.injector.apply_events(req.get("events") or [])
    return ok_resp(applied=applied, skipped=skipped)


def _h_sys_stats(ctx: ControlContext, req: dict) -> dict:
    # Linux 页 1Hz 轮询系统状态；CPU%/eMMC IO 速率按上次采样差值（首次缺省，PC 端容缺键）
    return ok_resp(stats=sys_stats.sample())


def _h_serial_open(ctx: ControlContext, req: dict) -> dict:
    url = req.get("url") or req.get("port")
    if not url:
        return error_resp("缺少 url（如 /dev/ttyS3、/dev/ttyUSB0）")
    info = ctx.deps.serial.open(url, baud=req.get("baud", 115200),
                                timeout=req.get("timeout", 0.1))
    return ok_resp(**info)


def _h_serial_write(ctx: ControlContext, req: dict) -> dict:
    import base64
    if req.get("data_b64"):
        data = base64.b64decode(req["data_b64"])
    elif req.get("hex"):
        data = bytes.fromhex(req.get("data", ""))
    else:
        data = str(req.get("data", "")).encode("utf-8")
    n = ctx.deps.serial.write(data)
    return ok_resp(n=n)


def _h_serial_read(ctx: ControlContext, req: dict) -> dict:
    import base64
    data = ctx.deps.serial.read(size=int(req.get("count", 512)),
                                timeout=req.get("timeout"))
    return ok_resp(n=len(data), data_b64=base64.b64encode(data).decode("ascii"),
                   text=data.decode("utf-8", errors="replace"))


def _h_close(ctx: ControlContext, req: dict) -> dict:
    ctx.close_requested = True
    return ok_resp(bye=True)


# 分发表：命令名 → handler（hello/pair 在未配对门禁白名单内，见 protocol.PAIRING_COMMANDS）
COMMANDS = {
    "ping": _h_ping,
    "status": _h_status,
    "hello": _h_hello,
    "pair": _h_pair,
    "video_start": _h_video_start,
    "video_stop": _h_video_stop,
    "video_params": _h_video_params,
    "input": _h_input,
    "sys_stats": _h_sys_stats,
    "serial_open": _h_serial_open,
    "serial_write": _h_serial_write,
    "serial_read": _h_serial_read,
    "close": _h_close,
}


def handle_line(deps: Deps, session: PairingSession, peer_ip: str, raw_line):
    """处理一行请求 → (响应 dict | None, 是否断开)。

    TCP 线程与单测共用此入口；顺序：解码 → 未配对门禁 → 分发 → 异常兜底。
    """
    if not raw_line or not raw_line.strip():
        return None, False
    try:
        req = decode_line(raw_line)
    except ValueError as exc:  # ProtocolError
        return error_resp(str(exc)), False
    ctx = ControlContext(deps=deps, pairing=session, peer_ip=peer_ip)
    gate = session.gate(req)  # 门禁先于分发：未配对时除 hello/pair 一律拒绝
    if gate is not None:
        return gate, False
    cmd = req["cmd"]
    handler = COMMANDS.get(cmd)
    if handler is None:
        return error_resp(f"未知命令: {cmd}（可用: {', '.join(sorted(COMMANDS))}）"), False
    try:
        resp = handler(ctx, req)
    except Exception as exc:
        log.exception("命令 %s 执行异常", cmd)
        return error_resp(f"{cmd} 执行异常: {exc}"), False
    return resp, ctx.close_requested


class ControlServer:
    """TCP 控制服务：监听线程 accept，每连接一线程（M2 骨架规模足够）。

    port=0 可让系统随机分配（测试用），start() 后读 self.port 拿实际端口。
    """

    def __init__(self, deps: Deps, host: str = "0.0.0.0", port: int = 9201,
                 pairing_code: str | None = None):
        self.deps = deps
        self.host = host
        self.port = port
        self.pairing_code = pairing_code if pairing_code is not None else load_pairing_code()
        self._sock: socket.socket | None = None
        self._stop = threading.Event()

    def start(self):
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind((self.host, self.port))
        sock.listen(4)
        self.port = sock.getsockname()[1]  # port=0 时回填实际端口
        self._sock = sock
        threading.Thread(target=self._accept_loop, name="server-rk-accept",
                         daemon=True).start()
        log.info("控制服务监听 tcp://%s:%d", self.host, self.port)

    def _accept_loop(self):
        while not self._stop.is_set():
            try:
                conn, addr = self._sock.accept()
            except OSError:
                break  # stop() 关闭监听 socket
            threading.Thread(target=self._client, args=(conn, addr), daemon=True,
                             name=f"server-rk-conn-{addr[0]}").start()

    def _client(self, conn: socket.socket, addr):
        """单连接循环：每行请求 → handle_line → 回一行响应；close/EOF 断开。"""
        peer_ip = addr[0]
        log.info("控制连接接入 %s", peer_ip)
        session = PairingSession(code=self.pairing_code)  # 配对状态按连接隔离
        reader = conn.makefile("rb")
        try:
            for raw in reader:
                resp, close = handle_line(self.deps, session, peer_ip, raw)
                if resp is not None:
                    conn.sendall(encode_msg(resp))
                if close:
                    break
        except (OSError, ConnectionError) as exc:
            log.info("控制连接断开 %s: %s", peer_ip, exc)
        finally:
            reader.close()
            conn.close()
            log.info("控制连接关闭 %s", peer_ip)

    def stop(self):
        self._stop.set()
        if self._sock is not None:
            try:
                self._sock.close()
            except OSError:
                pass
            self._sock = None
