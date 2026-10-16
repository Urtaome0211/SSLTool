# 手搓一个 SSL/SSH 终端客户端：SSLTool 开发手记

> 用 Python + PyQt5 从零写一个带 SSH 终端、SFTP、SSL 控制台和端口转发的桌面工具，顺便踩了五个值得记录的大坑。

## 缘起

市面上的终端工具要么笨重、要么收费，而我只想要一个"扁平、简单、能连 SSH 顺便传文件"的小工具。于是有了 **SSLTool**：一个基于 PyQt5 的跨平台桌面客户端，功能包括：

- **SSH 终端**：密码 / 私钥登录，完整 VT100/VT220 终端（vim、top 都能跑）
- **SSL 加密通道**：原始 TLS 文本控制台（telnet-over-TLS 风格），显示证书指纹与加密套件
- **SFTP 文件管理**：浏览、递归传输、拖拽上传/下载、传输可取消
- **SSH 端口转发**：-L 本地 / -R 远程 / -D SOCKS5，规则写进会话配置
- **会话管理**：密码进系统钥匙串（keyring），深色主题一键切换

开发过程中遇到五个"看起来不可能、找到原因后恍然大悟"的问题，全部记录在案。

## 架构速览

核心是"**QThread worker + 线程安全发送队列**"：`SSHWorker` / `SSLWorker` / `SFTPWorker` 各自跑在后台线程，通过 `pyqtSignal` 把数据投递回界面线程，键盘输入经 `queue.Queue` 送进 worker。终端渲染层用 **pyte**（纯 Python VT 模拟器）在 `QWidget` 上自绘，配色、选区、滚动缓冲全部手写。

```
MainWindow ──┬── ConsoleTab ── SSHWorker/SSLWorker ── paramiko / ssl
             ├── SFTPTab    ── SFTPWorker ── paramiko SFTP
             └── ConnectDialog / 会话管理 / 主题
```

## 坑 1：终端里看不到自己敲的字

**现象**：连上服务器后，敲键盘什么都没有；但按一下退格，之前敲的字又"吐"了出来。

**根因有两层**：

1. paramiko 的 `invoke_shell()` 发送的 pty 请求里 **terminal-modes 是空字符串**。RFC 4254 里有个关键选项 `ECHO`（opcode 53）——服务器只有在客户端明确请求时才开启回显。Windows sshd、部分网络设备不看"合理默认值"，结果就是不回显。
2. 我们自己的终端组件为了性能把可打印字符缓存起来、遇到控制字符才刷新落屏，于是"看不到"被进一步放大。

**解法**：手工构造 pty-req 消息，显式发送标准回显模式集：

```python
# RFC 4254 §8：每个条目 = opcode(1 byte) + uint32 值，末尾 0x00
_TERMINAL_MODES = (
    b"\x35\x00\x00\x00\x01"   # 53 ECHO = 1 ← 关键
    b"\x33\x00\x00\x00\x01"   # 51 ICANON = 1
    b"\x01\x00\x00\x00\x03"   # 1  VINTR  = Ctrl+C
    b"\x03\x00\x00\x00\x7f"   # 3  VERASE = Backspace
    b"\x00"                   # TTY_OP_END
)
```

同时给 SSH 也加上"本地回显"开关，配合一个**回显去重模型**：客户端按 ICANON+ECHO+ECHOE 规则预测服务器会回显哪些字节，把服务器数据里与预测匹配的前缀吞掉——于是无论服务器回不回显，每个字符都只显示一次。

## 坑 2：点"新建连接"就闪退

**现象**：应用一开对话框就崩。排查后哭笑不得：

```python
AttributeError: 'QFormLayout' object has no attribute 'setRowVisible'
```

`QFormLayout.setRowVisible()` 是 **Qt 6.4 才有的 API**，PyQt5（5.15）根本没有。这个对话框其实**从头到尾就没成功打开过**——用户此前一直用侧栏的快速连接卡，所以"连上服务器之后才点新建连接"时才发现。

**解法**：退回到显式 `widget.setVisible()` + `form.labelForField(widget).setVisible()`，QFormLayout 对隐藏行会自动折叠，效果一样。

教训：写 PyQt5 代码时，别信"最近文档"里的 API，先确认它属于哪个 Qt 版本。

## 坑 3：端口转发静默失效（本次最难啃的骨头）

**现象**：-L/-R/-D 转发器代码写好后，e2e 测试里 **25 秒等不到任何启动信号**，连失败信号都没有——转发线程像是根本没出生。

**排查过程**（值得记录方法论）：
1. 裸 paramiko 连接：0.01 秒成功 → 网络没问题；
2. 单独在**主线程**里构造转发器：信号正常 → 转发器代码没问题；
3. 在**普通 Python 线程**里构造并 `start()` 一个转发器 QThread：`start()` 返回了、不抛异常，但 `threading.enumerate()` 里**没有新线程**，run() 从未执行。

**根因**：**PyQt5 的 `QThread.start()` 在非主线程里调用是静默 no-op**——不报错、不启动、不执行 run()。而我们的转发器恰好是在 SSHWorker 的 worker 线程内部创建的，完美踩中。

**解法**：转发器全部改用纯 `threading.Thread` + 回调，回调接到 worker 的 pyqtSignal 上（信号从任意线程发射都安全，PyQt 会自动排队投递回界面线程）：

```python
fw = LocalForward(transport, spec["bind"], spec["target"],
                  on_started=self.forwardStarted.emit,
                  on_failed=self.forwardError.emit)
fw.start()   # threading.Thread.start()，任何线程都能用
```

顺带又踩了一个小坑：类里写了 `self._stop = False`，把 `threading.Thread._stop()` 私有方法给遮蔽了，`join()` 直接 `TypeError`——属性命名撞车 Python 私有 API，改成 `_stop_flag` 完事。

## 坑 4：双击 run.bat 提示"系统找不到指定的路径"

**现象**：双击批处理启动脚本报错，命令行里手动跑却正常。

**根因**：脚本文件被编辑器存成了 **LF 换行 + UTF-8 中文**，而双击时 cmd.exe 在中文 GBK 代码页（chcp 936）下解析这种文件会错乱，命令被拆得支离破碎。

**解法**：批处理文件必须 **纯 ASCII + CRLF**（错误提示都改成英文）。这不是玄学，是"批处理文件的编码规范"。

## 坑 5：QTermWidget 还是自己造轮子？

终端模拟器最初的候选是 QTermWidget（KDE 家的，功能全），但它依赖 libqtermwidget，Windows 上安装极其痛苦。最终选择 **pyte**：纯 Python、pip 即装、API 干净。代价是要自己写渲染层——paintEvent 逐格绘制、选区反色、宽字符占两格、粗体提亮、块状光标闪烁、滚动缓冲……大约 500 行换来零原生依赖，打包 PyInstaller 也省心。

pyte 的两个小坑：`Screen.display` 是渲染后的字符串列表而不是行对象（行数据在 `buffer` 里）；宽字符会写"数据 + 下一格空 Char"两格。看源码十分钟，少踩一天坑。

## 测试策略：把 SSH 服务器塞进测试里

纯 mock 测不出真问题（比如坑 1 和坑 3 都是真实协议交互才暴露的）。我们用 **paramiko 自己实现了一个迷你 SSH 服务器**塞进 e2e 测试：

- 服务器**只在 pty-req 携带 ECHO=1 时才回显**，精确复现 sshd 行为；
- 服务器端 -R 要自行 bind 端口并泵数据，客户端 -L 的 direct-tcpip 通道要按 chanid 从 accept 队列里捞出来（队列里混着 session 通道！）；
- 三个转发各自配一个本地 echo TCP 服务器，真实数据往返断言。

最终：**冒烟 32/32、e2e 7/7、GUI 启动自检、全量 py_compile 全绿**。

## 写在最后

这个项目的代码量不大，但"终端看不到输入"、"点按钮闪退"、"转发静默失效"、"bat 双击报错"四个问题，每个都是教科书级别的隐蔽坑。复盘下来最大的收获是：**离屏 UI 测试 + 内置协议服务器做端到端验证，比任何肉眼检查都靠谱**——四个坑里有三个是测试逼出来的。

源码与完整 README 见项目仓库（`app/` 目录结构清晰，`tests/` 可直接复现所有坑的验证）。
