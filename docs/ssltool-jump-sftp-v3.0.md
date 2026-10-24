# SSLTool v3.0：嵌套的 SSH、会「续」的下载，和看不见的省心

> SSLTool 是一个扁平化界面的 SSL / SSH / SFTP 桌面客户端（Python + PyQt5 + Paramiko + pyte）。
> v1.2 做了安全加固（TOFU / DPAPI / 证书警告），v2.0 做了终端体验（会话日志 / 搜索 / URL / 括号粘贴 / 备用屏幕），
> v3.0 的主题是**连接与传输**：SSH 跳板机（ProxyJump）、SFTP 断点续传，以及一堆
> 「不做也行，做了就再也回不去」的省心功能——批量多选、右键菜单、只读模式、会话分组 / 搜索 / 导入导出。
>
> 这一版最值得写的不是功能本身，而是两个绕不过去的工程细节：**嵌套 SSH 的密钥确认顺序**，
> 和**「续传」两个字的真实成本**。

---

## 一、跳板机：在别人的 SSH 里再开一条 SSH

跳板机（bastion / ProxyJump）的语义很简单：先 SSH 到一台中间机器，再从它连到目标服务器。
实现上 OpenSSH 用 `ProxyJump` 干这件事；Paramiko 没有现成的开关，但给了 `sock=` 参数——
`SSHClient.connect(sock=...)` 可以在任意 socket 上跑 SSH 协议，而跳板机开出的 direct-tcpip 通道
恰好就是一个 socket。

```python
# app/core/jump.py（节选）
class JumpConnector:
    def connect(self):
        client = paramiko.SSHClient()
        client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        client.connect(hostname=self.host, port=self.port, username=self.username, ...)
        self._client = client
        return client

    def open_channel(self, host, port):
        self.connect()
        chan = self._transport.open_channel(
            "direct-tcpip", (host, int(port)), ("127.0.0.1", 0))
        chan.settimeout(12)
        return chan
```

于是「经跳板机连目标」就变成了：`JumpConnector` 先登录跳板机 → 对目标开一条 direct-tcpip 通道 →
把通道塞进目标连接的 `sock=`。核心代码不到 90 行，但**安全细节**不在这 90 行里。

### 1.1 两把主机密钥，两轮 TOFU

v1.2 的主机密钥验证是「探测先行」：先用一条只做密钥交换的探测连接取回指纹，用户确认之后，
才把密码发给服务器。加上跳板机之后，密钥从一把变成了两把，顺序必须想清楚：

1. 先确认**跳板机**的密钥（此时还没有任何凭据离开本机）；
2. 跳板机密钥确认后，才登录跳板机（此时才向跳板机发送密码）；
3. 对**目标**的探测走跳板机通道——`HostKeyGate` 为此加了一个 `sock_factory` 参数，
   探测连接在 direct-tcpip 通道上建立；
4. 目标密钥确认后，认证连接同样走通道，建立后再复核一次实际指纹（防两条连接被分别劫持）。

任一步被拒或超时，都不会发出任何密码或私钥签名。错误信息也做了区分：
跳板机本身连不上是「跳板机：无法连接 …」，目标经跳板机不可达是「经跳板机 …」——
生产环境里「到底是哪一跳坏了」经常是排查的第一个问题。

### 1.2 怎么证明流量真的走了跳板机

单测里不好证明「确实绕路了」——目标机就在 127.0.0.1 上，直连也能通。
e2e 测试在内嵌 SSH 服务器（EchoServer）上加了一个计数器：

```python
direct_requests = 0  # 类属性：direct-tcpip 通道请求次数

def check_channel_direct_tcpip_request(self, chanid, origin, destination):
    EchoServer.direct_requests += 1
    ...
```

跳板机实例收到的 direct-tcpip 请求 ≥ 2 条（探测 1 + 认证 1），且目标连接成功、
欢迎语收到，才能断定整条链路是「目标 ← 跳板机通道 ← 本机」。
还有一个反向断言：跳板机不可达时，错误信息必须指明是跳板机的问题，而不是含糊的「连接失败」。

## 二、断点续传：seek 两行，「续」字千斤

Paramiko 的 `SFTPFile.seek()` 让续传看起来像两行代码的事：

```python
# app/core/sftp_worker.py（节选）
if resume and os.path.exists(local):
    off = os.path.getsize(local)
    if off >= size:
        self.opDone.emit(...)      # 已存在且大小一致：跳过
        return
    mode = "ab"
    rfh.seek(off)                  # 从服务器侧的断点继续读
    ...
```

但真正的成本在**交互**上：

- 续传只在「目标文件已存在」时才问——新下载不该打扰用户；
- 目录批量下载**逐个文件**询问：续传 / 覆盖 / 取消，已经完整的目标**自动跳过**不弹窗；
- 弹窗必须防重复：拖拽下载会经过两条代码路径，曾出现同一个文件弹两次的问题，靠
  一个「仅当 `os.path.isfile(local)` 且提示未进行中」的守卫修掉；
- 进度回调要带偏移量：续传场景下进度条显示的是「总进度」，不是从 0 重新开始。

e2e 测试覆盖了三条路：续传偏移正确（本地先放半截文件，断言服务器只传了后半截）、
跳过（本地文件大小一致时不触发传输）、覆盖（选覆盖时完整重传）。

## 三、那些「不做也行」的功能

- **多选批量上传 / 下载**：上传按钮改成 `getOpenFileNames` 循环入队；下载本身已经遍历选中项，
  加上右键菜单「下载到…」就是新入口。
- **SFTP 只读模式**：不只是把 5 个写按钮置灰——拖拽上传的入口（远程树的拖放）也要禁掉
  （`NoDragDrop`），否则「只读」会被一个拖拽动作绕过。
- **右键菜单**：下载 / 复制路径 / 重命名 / 删除。复制路径是所有日常操作里最「顺手」的：
  拿到路径就能去终端 `scp`、`grep`、`tail`。
- **会话分组 / 搜索 / 导入导出**：列表从 `QListWidget` 换成 `QTreeWidget`（分组树 + 顶栏搜索框），
  导出 JSON 时**不含密码**（密码在钥匙串里，导出文件可以放心传给同事）；
  导入时按 ID 合并——同名会话更新、新会话追加，重复导入不会越导越多。

## 四、怎么证明：89 项冒烟 + 11 项端到端

- **冒烟测试 89 项**（离屏）：跳板机门禁的两阶段流转、跳板机探测失败的错误前缀、
  续传三态（偏移 / 跳过 / 覆盖）的逻辑分支、导入合并按 ID 去重、分组树分组正确性、
  只读模式的按钮禁用矩阵。
- **端到端测试 11 项**（内置 SSH 服务器）：新增 `test_jump_connect`（两台内嵌服务器，
  跳板机 direct-tcpip 计数 ≥ 2 断言 + 失败路径错误前缀）与 `test_sftp_resume`
  （真实 SFTP 链路上的续传 / 跳过 / 覆盖）。整个套件回归运行时，前 9 项
  （含 v1.2 的「拒绝密钥变更后服务器零认证尝试」与 v2.0 的备用屏幕 / 括号粘贴）必须全绿。

## 五、小结

| 功能 | 实现量 | 有意思的点 |
| --- | --- | --- |
| 跳板机 | 中 | `sock=` + direct-tcpip 通道；两把密钥两轮 TOFU，顺序即安全 |
| 断点续传 | 小 | `seek` 两行，交互与防重复才是成本 |
| 只读模式 | 小 | 按钮之外还要禁拖放，否则只读形同虚设 |
| 批量多选 / 右键 | 小 | 复用已有遍历逻辑，新入口零风险 |
| 分组 / 搜索 / 导入导出 | 中 | 导出不含密码；导入按 ID 合并，幂等 |

v3.0 和前面两版一样：功能本身都不惊天动地，但每个功能的边界条件（跳板机的密钥顺序、
续传的询问时机、只读的拖放漏洞）都做成了回归测试。**边界条件不是加分项，边界条件就是功能本身。**

---

*SSLTool v3.0 已在 GitHub 发布：<https://github.com/Urtaome0211/SSLTool/releases/tag/v3.0>，Windows x64 可执行文件见发布资产。*

*Copyright (c) 2026 Urtaome0211*
