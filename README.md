# SSLTool

一个扁平化界面的 SSL / SSH / SFTP 桌面客户端（Python + PyQt5）。

## 功能

- **SSH 终端**：密码 / 私钥登录，基于 pyte 的完整 VT100/VT220 终端（ANSI 彩色、vim / less 等可用），本地回显自动去重
- **SSL 加密通道**：原始 TLS 文本控制台（类似 telnet-over-TLS），连接后显示服务器证书 SHA-256 指纹、TLS 版本与加密套件；可选证书验证与客户端证书
- **SFTP 文件管理**：目录浏览、上传 / 下载（支持文件夹递归与总进度）、**拖拽上传 / 下载**、**传输可取消**、新建文件夹、重命名、删除
- **SSH 端口转发**：本地转发（-L）、远程转发（-R）、动态 SOCKS5 代理（-D），会话配置里一行一条规则；连接后启动结果直接显示在终端里
- **会话管理**：连接配置保存于 `~/.ssltool/sessions.json`；密码与私钥口令存入系统钥匙串（keyring），无可用后端时回退到 `~/.ssltool/secrets.json`（仅 base64 混淆，不推荐）
- **深色主题**：菜单「视图 → 深色主题」（Ctrl+D）一键切换，偏好自动保存

## 安装与运行

1. 安装 Python 3.8+
2. 安装依赖：`pip install -r requirements.txt`
3. 运行：`python main.py`（或双击 `run.bat`）

## 打包为可执行文件

双击 `build.bat`（使用 PyInstaller，输出到 `dist\SSLTool`）。

## 使用说明

1. 左侧「快速连接」输入主机 / 端口 / 用户名并选择协议后点「连接」，或通过「新建连接」保存会话
2. SSH 密码未保存时，连接前会弹框询问；勾选「记住密码」后存入系统钥匙串，下次免输入
3. 私钥支持 OpenSSH / PEM 格式（RSA / Ed25519 / ECDSA / DSA），不支持 `.ppk`（Paramiko 限制）；需要口令的私钥会在连接时弹框询问
4. SSL 加密通道为原始 TLS 文本控制台，默认开启本地回显，适合连接基于 TLS 的文本服务（如 openssl s_server 演示服务）
5. 终端默认开启「本地回显」：键入内容立即在本地显示，并自动去除服务器的重复回显——无论服务器是否回显，每个字符都只显示一次；若显示仍有异常，可取消勾选标签页左上角的「本地回显」
6. 若程序异常退出，可在 `~/.ssltool/error.log` 查看崩溃记录
7. SFTP 支持拖拽：从文件管理器把文件 / 文件夹拖入右侧远程目录树即上传；把远程项目拖到左侧本地目录树即下载到对应文件夹
8. 传输进行中可点进度框「取消」停止本批传输；取消后剩余未开始的传输会被丢弃
9. 菜单「视图 → 深色主题」（Ctrl+D）切换深色模式，重启后保持
10. SSH 端口转发：在「新建 / 编辑连接」对话框的「端口转发」框里每行写一条规则，OpenSSH 风格（端口写 `0` 表示自动分配）：
    - `L:127.0.0.1:8080:10.0.0.5:80` —— 本机 8080 → 服务器可达的 10.0.0.5:80（本地转发）
    - `R:127.0.0.1:0:127.0.0.1:3306` —— 服务器随机端口 → 本机 3306（远程转发）
    - `D:127.0.0.1:1080` —— 本机 SOCKS5 代理（仅 CONNECT、无认证）
    连接后每个转发的实际监听端口会打印在终端里；断开连接时全部自动关闭。

## 快捷键（终端内）

| 按键 | 作用 |
| --- | --- |
| Ctrl+C | 发送中断信号 `\x03`（有选中文本时复制） |
| Ctrl+Shift+C | 复制选中文本 |
| Ctrl+V / Ctrl+Shift+V | 粘贴并发送 |
| Ctrl+A | 全选 |
| 方向键 / Home / End / Ins / Del | 发送对应转义序列 |
| PgUp / PgDn | 回看滚动缓冲 |
| Shift+PgUp / Shift+PgDn | 发送 `\x1b[5~` / `\x1b[6~` 转义序列 |

## 说明与限制

- 终端基于 pyte 的 VT100/VT220 模拟：支持 xterm 16/256 色、粗体 / 下划线 / 反显、滚动缓冲；不支持备用屏幕（alt-screen，`vim` 等全屏程序在主屏幕缓冲中运行，可正常使用，仅外观略有折损）
- SSH 主机密钥采用自动信任策略（AutoAddPolicy）
- 端口转发规则不支持 IPv6 字面量地址；SOCKS5 仅支持 CONNECT 命令与无认证模式

## 目录结构

```
SSLTool/
├── main.py                    # 程序入口
├── requirements.txt
├── run.bat / build.bat
├── app/
│   ├── config.py              # 会话与凭据存取
│   ├── theme.py               # 扁平化 QSS 主题与应用图标
│   ├── core/
│   │   ├── workers.py         # SSH / SSL 连接线程
│   │   ├── forwarders.py      # SSH 端口转发（-L / -R / -D SOCKS5）
│   │   └── sftp_worker.py     # SFTP 连接线程
│   └── ui/
│       ├── terminal_widget.py # pyte 完整 VT 终端组件
│       ├── terminal_tab.py    # 终端 / SSL 控制台标签页
│       ├── sftp_tab.py        # SFTP 文件管理标签页（拖拽 / 可取消）
│       ├── connect_dialog.py  # 新建 / 编辑连接对话框
│       └── main_window.py     # 主窗口
└── tests/
    ├── smoke_test.py          # 离屏冒烟测试（32 项）
    ├── gui_boot.py            # 主窗口启动自检
    └── e2e_test.py            # 内置 SSH 服务器端到端测试（回显 / 去重 / 滚动 / 对话框 / 端口转发）
```
