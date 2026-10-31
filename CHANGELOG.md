# 更新日志

本项目遵循 [Semantic Versioning](https://semver.org/lang/zh-CN/) 与
[Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/) 格式。
## [Unreleased]

### 新增
- **板卡镜像实时性证明（跑表 overlay + 单向时延统计）**：板端起流前 1 帧真探测 drawtext（`server_rk/video.py` STOPWATCH_VF，Debian ffmpeg 带 freetype；探测失败自动降级 overlay=off 并在 note/status 行明示），把流内 PTS 跑表（h:mm:ss.ms，起流归零）烙进画面左上角——镜像里跑表以 1x 真实速度跳动 = 帧"现在"采的（证据 stopwatch_t0/t1.png：3.500→4.750，1.2s 墙钟差 1.250s）；video_start 应答新增 now_ms（板端 epoch 毫秒）与 overlay 字段，PC 端 ControlClient 以"应答时刻"为基准估板钟差（**不能用折半 RTT**——video_start 应答在长探测后打戳，折半模型把钟差抬高 handler/2，真机实测虚增 0.5~5s"延迟"），H264UdpSource 记录逐包板端发包时刻，页面统计行新增「传输≈x ms」逐帧刷新（transport_delay_ms，u32 回绕符号回卷，板钟未校时也能用）。配套低延迟三件套：采集帧率对齐输出帧率（30 进 12 出会在滤镜链积 1s）、-fflags nobuffer、-flush_packets 1（缺省 32KB stdout 缓冲在 ~12KB/s 输出下积 ~1s 恒定滞后）——真机实测传输时延 978/2984ms → 8ms。

- **Linux 页鼠标控制（M3 前置：绝对坐标跟手注入）**：页上「鼠标控制」开关 → 开启后控件内点击/拖动/滚轮注入板端——坐标换算走 _fit_viewport_rect 逆映射（控件逻辑坐标 ×DPR → 设备像素 → 板屏像素，letterbox 黑边区拒绝注入），移动 25ms 合并 latest-wins（跟手且不刷爆控制通道），按下/抬起前先落一次绝对定位（光标不在目标位点击仍落所指处）。控制通道 ControlClient 会话期新增 send_command 队列（逐条问答，video_stop 收尾语义不变）。板端配套：①uinput 零依赖 raw ioctl 写入器（python-uinput 在 Debian/PEP 668 装不上——/dev/uinput ioctl+uinput_user_dev 直写，root 天然可跑，每 emit 自带 SYN_REPORT）；②鼠标绝对定位走 **X 指针子进程**：XWarpPointer 在本板 libX11 上任何非主线程调用必段错误（XInitThreads 也救不了，faulthandler 定位），专用子进程主线程持 X 连接、管道喂坐标（8 字节/条），DISPLAY/XAUTHORITY 与采集管线同源（video.last_env）；③板屏量程 SERVER_RK_SCREEN 可配（默认 1024x600）。真机验证：e2e 12/12（applied=3），光标 (80,80)→(940,520) 精确落位（证据 mouse_inject_a/b.png，相机应用对注入出现 ROI 反馈框）。

- **跑表开关与透明度（用户实报常开影响看画面）**：页上「跑表」复选框 + 透明度 10%~100%，改动落盘且发流中自动重起流生效（真机 pid 更替验证）；板端 video_start 受理 overlay/overlay_opacity，STOPWATCH_VF 参数化（fontcolor 与底框同比例淡化）。

- **HardFault 定位（P0：板端真故障自捕获 + PC 树形诊断面板）**：固件 V4.3.0（实验区 serial_port）HardFault/MemManage/BusFault/UsageFault 四路收口——汇编跳板判 EXC_RETURN 选 MSP/PSP 取压栈帧，C 侧收 CFSR/HFSR/BFAR/MMFAR，按契约打一行 `!FAULT T=… PC=… LR=… XPSR=… CFSR=… HFSR=… BFAR=… MMFAR=…` 后自动复位（文本行复用行式 ingest 天然可重同步——B-01 二进制帧 CRC 撞 0x0A 的教训不再犯）；新增 FAULTTEST 命令触发确定性故障供联调；dump 路径零 malloc/无浮点/fault-in-fault 二次直接 lockup。PC 侧新增「故障诊断」页：宽容解析（半行/未知键/截断值）、CFSR/HFSR 位域人话树（样式基准 STM32Cube 故障寄存器树：故障地址/故障函数/硬故障/总线故障/内存管理故障/用法故障，置位醒目未置位灰显）、手写最小 ELF32 符号化（.symtab 查表 PC→函数+偏移，零第三方依赖，ELF 路径为面板设置项且按 mtime 缓存）、状态栏「⚠ 最近一次异常」徽标不弹窗不刷屏。自测 +2 组（fault_report 解析/符号化 + fault_panel 树渲染）。

- **Linux 页系统状态 1Hz（CPU/内存/eMMC/温度/负载/运行时长）**：板端 server_rk 新增 sys_stats 采样（纯读 /proc 与 /sys 零外部依赖；CPU% 与 eMMC I/O 速率按上次采样差值，首拍缺省）+ 控制面 sys_stats 命令；页上「系统状态」开关 + 底部状态行（SysStatsPoller 独立 1Hz 小连接，不看画面也能看板况；断线 3s 退避重连；缺键渲染 '--' 不猜值）；真机 1.1s 双采样验证（cpu 32.9%、mmcblk0 5.93/7.31 GB、58.4℃）。server-rk 单测 17/17。

### 修复
- **自测套件 40 分钟化 + 退出码 127（同根因：主题重复抛光风暴）**：MainWindow.__init__ 每次 apply_saved_theme → setStyleSheet 令 Qt 全量重抛光 app 内所有控件，测试套件窗口越建越多成本 O(窗数²)（实测 40 窗探针 >180s 未完、全量 ~40min；WER 实录 shutdown 阶段 ucrtbase fast-fail 0xC0000409，git-bash 映射成退出码 127——独立自测门禁因此持续误报"失败"并诱发误判"close_two_stage 竞态"，py-spy 定位真凶在 set_theme）。两层收口：①set_theme 内容不变即跳过 setStyleSheet（app.property 记已应用 QSS），40 窗探针 19.6s、全量回分钟级；②残留的间歇性 teardown abort（71 组全绿之后才发生，不影响判定）经 `_verify/selftest_gate.py` 用 os._exit 跳过解释器关机，门禁退出码=真实判定。

- **关闭按钮灰色"未响应"（Windows 幽灵窗根因）**：垃圾洪泛（错波特率/调试口乱码，150KB/s 下约 600 行/s）逐事件整行处理把 GUI 线程钉死在 posted events 里——5s 泵不到原生消息 Windows 即幽灵化（标题栏 X 变灰，点什么都没响应）。RX 入口改洪泛合并：pending 缓冲 + 16KB/拍预算消费（任意相邻消费间必回事件循环泵原生消息），超高水位丢最老并限频打标（统计行「洪泛丢弃 N」），小数据量当场消费语义不变。自测 +3 组（坐标换算/洪泛合并/跑表控件）。

- **关闭按钮"失效"（关窗最长 ~14s 无响应）**：closeEvent 原本把全部收尾内联在 GUI 线程——worker 1.5s + 控制通道 6s + 收流/解码线程 3s×2 + STM32 CLI 1s 串行等完，USB 串口假死时 pyserial close 还会在内核再卡（用户报点 X 无反应）。改两段式：closeEvent 只做轻量收尾并立即隐藏窗口（用户感知瞬时关闭），重活挪 singleShot(0) 的 _finalize_close（try/finally 保证异常也照常退出；最后一个窗口由它负责 app.quit，quitOnLastWindowClosed 暂关防 video_stop 发不出去）；_teardown_worker 对收不回的串口线程（卡死内核调用）改入"停尸房"只扣引用不销毁，避免带运行线程 deleteLater 的崩溃隐患。自测 +2 组（两段式关窗/视频视口 DPR）

- **视频画面不居中不放大（高 DPI 屏缩成 1/dpr）**：ScreenGLWidget 的 glViewport 坐标系是 QOpenGLWidget 帧缓冲（逻辑尺寸 × devicePixelRatio），原实现用逻辑宽高算 letterbox——150% 屏上画面缩成 1/1.5 且错位（用户实报）。抽 _fit_viewport_rect 静态方法按设备像素计算（回归锚=用户实测几何 1689×738@1.5），QPainter 兜底路径本就逻辑坐标不受影响

- **板端管线补丁跨 start 叠加**：起流参数补丁（跑表 drawtext/nobuffer/帧率对齐）写回缓存管线 dict 且无幂等保护——第二次 video_start 起 drawtext×2、第三次×3（真板实测 3 层 drawtext 渲染直接把延迟顶上去），全部改成员判断幂等

- 对比度审计脚本页签清单未随 v1.3「Linux 支持」页更新（7→8）导致无法运行，已修正（现覆盖 436 项，两主题 0 违规）


## [1.3.0] - 2026-10-28

### 新增
- **ESP32 支持**：
  - 连接层——「打开串口时不拉 DTR/RTS」防复位选项（DTR/RTS 经自动下载电路映射 IO0/EN）；「复位 / 下载模式」设备线控按钮（esptool ClassicReset 语义）+ DTR/RTS 实时切换；断线自动重连（每 2s 重试，适配烧录后 COM 重枚举）；端口下拉按 VID/PID 标注芯片（CP210x/CH340/CH341/FT232/ESP32 JTAG/ESP32-S2 CDC）；波特率预设补 74880/921600/2000000
  - 日志层——ESP-IDF 日志解析（等级着色，开关默认开）、复位横幅 `rst:0x…` 装饰行、Guru Meditation panic 高亮、backtrace pc 地址自动收集 + 「解析 backtrace」对话框（addr2line，自动探测 IDF 工具链，ELF/工具路径记忆）
- **Linux 适配**（按需求度决策，见 `docs/research/调研_Linux串口生态与适配决策.md`）：打开失败按平台给处置提示（dialout 权限 / ModemManager·brltty 抢占 / Windows 占用）；「设备屏」页更名 **「Linux 支持」** 并入主线（v2 M1/M2 合并：网络串口 URL 直通 + UDP H.264 投屏原型 + server-rk 板端骨架）
- **外观标准化**：自定义主题——窗口背景/控件底/正文文字/强调色/日志底/日志文字六键可调（基准主题决定曲线色等），即时 WCAG 对比度复核并在设置页警示不达标项，重启保持；页面显示设置（STM32 / Linux 支持页可关，记忆保持）

### 修复
- 主窗顶行加控件导致最小宽度超窗（ESP32 线控按钮移至日志行右段）；测试假端口实现与真实 SerialWorker 新签名同步

### 文档
- README 功能一览补 ESP32 / 自定义主题章节；`docs/research/调研_Linux串口生态与适配决策.md`（竞品特性对照与三档决策）；自测扩至 45 组（ESP32 连接层 5 组 / 日志层 3 组 / 外观与页面 1 组）

## [1.2.1] - 2026-10-27

### 新增
- 应用图标「垂柳 × 示波水线」（`qt/assets/`，SVG 源 + 多尺寸 ico；窗口/任务栏/exe/README 全接入；`make_icon.py` 可再生）
- 浅色/深色主题对比度审计：`_verify/theme_contrast_audit.py` 共 394 项检查（两主题 × 7 页签语义配对 + 渲染像素抽样），selftest 新增 THEME CONTRAST 组固化关键断言
- 浅色截图：`docs/screenshots/main_light.png`、`settings_light.png`
- 用户手册 v1.2 新功能篇：`docs/用户手册_v1.2.md`（快速上手/STM32 烧录页 / AI 通道速用 / 频谱导出口径 / 故障排查 FAQ）
- AI 通道端到端验收：19 题 100 分考卷 + 真机真 socket 实测 **97.5/100**（`_verify/ai_exam.md` / `ai_exam_report.md`）
- 调研文档：`docs/research/调研_测试与交接最佳实践.md`、`docs/research/调研_调试工具与低延迟投屏.md`；下一版设计稿 `docs/设计_Linux设备互联_v2.md`
- CI：tag 构建随 exe 产出 `SHA256SUMS.txt`；`CHANGELOG.md` 建立（SemVer + Keep a Changelog）

### 修复
- 浅色主题下工具行小按钮（#tool）浅灰底白字、禁用按钮灰底白字不可辨识 → 统一蓝底白字（#0E639C，对比 6.4）；禁用态改弱化蓝灰底白字
- Modbus 事务闪烁高亮/回落文本、频谱导出警告色、波形悬停读数首行等处硬编码深色值在浅色下低对比 → 全部改随主题取色
- 暗色主题下单选钮（频谱 CSV 导出口径选择等）indicator 不渲染 → 补 QRadioButton QSS（两主题像素验证）
- AI 通道 `settings_set points` 越界值原样接受 → 按 §1.4 文档口径钳制 50..200000（E2E B-03）
- AI 通道 `stm32_status` 缺文档声称的 `note` 字段 → 按 CLI/连接状态派生明确提示（E2E Q13①）
- 波形页「恢复自动缩放」不还原被历史滑块拖窄的横向宽度 → 还原宽度到「显示点数」档位（E2E B-04）
- 修复 selftest `test_wave_display_and_hover` 探针恰好落在两采样点中点导致的稳定挂（探针偏移避开中点）

### 文档
- AI_INTERFACE.md：§3「程序化 export_csv/export_png」更正为频谱页 GUI 按钮（AI 通道无此命令）；§1.4 `paused` 补充「只冻结波形页，频谱由频谱页冻结按钮单独控制」

## [1.2.0] - 2026-10-26

更名 **LiuScope（柳镜）**（原名 serialscope）。详见 [Release v1.2.0](https://github.com/TZH-user/liuscope/releases/tag/v1.2.0)。

### 新增
- 设置页（主题明暗切换、曲线填充迁移、STM32 CLI 路径）与关于页（作者：折柳）
- STM32 烧录页：STM32CubeProgrammer CLI 封装（探针枚举/连接/软硬复位/带校验烧录/全片擦除/读保护解除/选项字节读取）
- AI 控制通道规范化：19 条命令 + help 自描述 + follow 连续输出 + settings_set 全键 UI 同步跟随（支持中文别名）
- 波形页悬停十字读数（首行 x + 每条可见曲线「名字: y 值」）；波形 PNG/CSV 导出
- 频谱导出 PNG（标注采样率/FFT/窗/RBW/主频）与 CSV（最新一帧 / 最近 N 秒，钳制到实际采集时长）+ 清空显示；频谱历史缓冲上限（6000 帧 / 2000 万单元格）
- 频谱图与瀑布图频率轴双向联动对齐

### 修复
- 终端页连接按钮状态化（已连接显示「断开」）
- 终端页 TX 行聊天式右对齐（行序不变，窗口缩放自动重排）
- 波形页「窗口」改名「显示点数」；XY 模式补齐量程/窗口数/分格/余晖等参数；曲线查看复选框边缘换色
- 波形页右键菜单全中文；日志行悬停 HEX⇄文本互转

### 结构
- master 仅保留 Qt 主线；PowerShell 存档（v1.0.0 封版）移至 ps-legacy 分支；GitHub Actions CI（离屏自测 + tag 打包）
