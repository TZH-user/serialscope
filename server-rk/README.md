# server-rk — LiuScope v2 板端服务（M2 骨架）

对应设计稿：[docs/设计_Linux设备互联_v2.md](../docs/设计_Linux设备互联_v2.md)。
目标板：野火 LubanCat 等 RK356x Linux 开发板（Python 3.10+，仅需 stdlib + pyserial）。

## 目录结构（M2 骨架）

    server-rk/
    ├── server_rk/           Python 包（python -m server_rk 启动）
    │   ├── __init__.py      包版本与模块职责索引
    │   ├── __main__.py      入口：--port(视频 UDP, 默认 9202) --control(TCP, 默认 9201) --serial --verbose
    │   ├── protocol.py      JSON 行协议编解码 + hello/challenge/pair 配对状态机（设计稿 §4）
    │   ├── control.py       TCP 9201 控制服务：每连接一线程 + 13 条命令分发表（依赖可注入）
    │   ├── video.py         FFmpegVideoSource（ffmpeg 子进程起停 + build_args）+ packetize 打包
    │   ├── input_inject.py  uinput 键鼠注入：Qt 键码 → Linux input 事件码映射，不可用即降级
    │   └── serial_bridge.py 板端串口透传：serial_for_url（/dev/tty*、rfc2217://、loop:// 通吃）
    ├── test_server_rk.py    本机直跑测试（无需真板/ffmpeg/uinput）
    └── ser2net.yaml.example M1 的 ser2net 配置样例

### 模块一句话

| 模块 | 职责 |
|---|---|
| protocol | 行协议编解码（一行请求一行响应，错误统一 `{"ok":false,"error":…}`）、配对状态机（challenge 绑定 + 配对码明文比对，M3 换 PSK 派生）、未配对门禁 |
| control | TCP 9201 服务、命令分发表（ping/status/hello/pair/video_*/input/**sys_stats**/serial_*/close）、`Deps` 依赖注入容器（video/injector/serial 均可换 fake 单测） |
| video | `FFmpegVideoSource` 只管命令行构造与子进程生命周期（kmsgrab→nv12→h264_rkmpp 无 B 帧→udp，§5.3）；`packetize` 按 `[seq u32][ts_ms u32][flags u8]` 切包，PC 端 H264UdpSource 须对称实现 |
| input_inject | Qt.Key_* → Linux input 事件码映射 + 批注入（key/mouse_move/**mouse_abs**/mouse_button/wheel）；鼠标绝对定位走 X 指针子进程（本板 libX11 非主线程 warp 必段错误——专用子进程主线程持 X 连接，管道喂坐标）；键盘/滚轮/点击走 uinput（零依赖 raw ioctl，python-uinput 缺失时自动兜底，需 root+/dev/uinput）；全不可用记日志不抛异常。板屏量程可用 SERVER_RK_SCREEN=WxH 覆盖（默认 1024x600） |
| sys_stats | 板端 1Hz 系统状态采样（纯读 /proc 与 /sys，零外部依赖）：CPU%（/proc/stat 差值）、内存、eMMC 容量与 I/O 速率（根分区所在 mmcblk，速率按采样差值）、thermal_zone 最高温、负载、运行时长；差值字段首拍缺省，PC 端容缺键渲染 -- |
| serial_bridge | 控制通道 serial_open/write/read → `serial_for_url` 单口占用透传，重开即换口/换波特率 |

### 真板部署（RK356x，含视频链自适应）

    # 依赖：ffmpeg（PATH 内）+ pyserial（root 可见：pip3 install pyserial 或复用用户 site）
    cd /home/cat && sudo -n env PYTHONPATH=/home/cat:/home/cat/.local/lib/python3.11/site-packages         nohup python3 -m server_rk --verbose > /home/cat/server_rk.log 2>&1 < /dev/null &

视频链在 video_start 时自动探测，无需配置：

- **采集管线**：kmsgrab（DRM 直抓，设计目标）→ x11grab（X11 会话，授权文件自动发现）。探测 = 1 帧真采集+滤镜链全流程；RK3562（Debian 12 / rockchip 4.0 驱动）实测 kmsgrab 的 hwdownload 滤镜链首帧即崩，自动落 x11grab
- **编码器**：h264_rkmpp → h264_v4l2m2m → libx264，`-encoders` 清单之外逐个 1 帧真编码验证（清单列得出 ≠ 能跑）；libx264 软编钳 12fps（720p30 实测 0.52x 实时，不钳会积压延迟）
- **发送路径**：ffmpeg 输出走 stdout，板端泵线程切 NAL → packetize（§4 包头）→ UDP——ffmpeg 直发 udp:// 是裸 Annex-B 无包头，PC 端没法拆
- 配对码：环境变量 SERVER_RK_PAIRING_CODE > 包目录 pairing_code.txt > 出厂默认 246810
- **实时性措施**（真机 RK3562 实测传输时延 ~1s → 8ms）：采集帧率对齐输出帧率（30 进 12 出会在滤镜链积 1s）+ `-fflags nobuffer` + `-flush_packets 1`（缺省 32KB stdout 缓冲在低码率下积 ~1s 恒定滞后）+ 泵线程 read1（有多少读多少，不凑满 4096）；起流参数补丁全部幂等（跨 start 复用缓存管线，重复追加 drawtext/nobuffer 会层层叠加）
- **跑表 overlay**（实时性定性证明）：起流前 1 帧真探测 drawtext（依赖 freetype，Debian ffmpeg 带；失败自动降级并在 note 注明 overlay=off），把流内 PTS 跑表烙进画面左上角；video_start 应答带 now_ms（板端 epoch 毫秒）与 overlay 字段，PC 端据此显示单向传输时延

### 本机测试（仓库根目录运行，Windows 即可）

    python server-rk/test_server_rk.py
    # 末行输出 SERVER-RK TESTS OK

覆盖：配对全流程（含错误配对码拒绝、未配对拒绝业务命令）、命令分发表（fake 依赖
直驱）+ TCP 127.0.0.1 全链路（随机端口）、build_args 快照断言、packetize 对称性
（模拟 PC 端拆包还原原流）、loop:// 串口读写闭环、Qt 键码映射、无 uinput 优雅降级。

### 视频拆包约定（PC 端 H264UdpSource 实现须一致）

UDP 包 = `[seq u32 大端][ts_ms u32 大端][flags u8][payload ≤1200]`；
flags：`0x01` 关键帧、`0x02` 分片首包、`0x04` 分片末包（单包 NAL 首末同置）；
seq 每 UDP 包 +1（u32 回绕）；PC 端按「首包起 → 末包止」拼接 payload 还原 NAL，
seq 跳变 → 控制通道 `resync{from_seq}` → 板端强制 IDR（M3 实现）。

### 板端运行（真板验证项见下方清单）

    cd server-rk && sudo python3 -m server_rk --verbose
    # 配对码：放 server_rk/pairing_code.txt 或设环境变量 SERVER_RK_PAIRING_CODE（缺省 246810）

### 真板上才验证的清单（骨架期遗留）

- kmsgrab 需要 `cap_sys_admin`（setcap 或 udev 规则），以及镜像图形栈（X11/Weston/无桌面）对捕获路线的影响（设计稿 §7.1）；
- `h264_rkmpp` 编码器名与 nyanmisaka ffmpeg-rkmpp 的可得性（野火镜像是否预编译，§7.2）；`-s` 缩放对 rkmpp 的支持度；
- `/dev/uinput` 权限组（udev 规则给 input 组）与 python-uinput 安装（§2 权限落地）；
- GOP `-g fps*2` 与码率默认值在 1080p30 实测的观感调优；`resync`→强制 IDR 的 M3 落地。

## M1（已完成）：串口网络化 —— 板端 ser2net + PC 端直连

M1 不需要本目录代码：板端装 **ser2net**（RFC2217），LiuScope 串口页直接手输
`rfc2217://<板子IP>:7000` 连接（v2 分支已支持，pyserial 原生协议，波特率由板端协商）。

板端安装与配置：

```bash
sudo apt install ser2net
sudo cp ser2net.yaml.example /etc/ser2net.yaml   # 按实际串口设备号/波特率修改
sudo systemctl restart ser2net
```

验收（对齐设计稿 M1）：LiuScope 串口页以 `rfc2217://` 连接后，收发行为与本地 COM 口一致；
`loop://` 回环全链已在 selftest `NET SERIAL URL` 组覆盖。M2 起串口透传由 server 内置
（`serial_bridge`，与配对鉴权统一），ser2net 方案保留并存。
