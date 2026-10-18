# SSLTool v1.2 安全加固手记：在发密码之前先问一句"你是谁？"

> 三个改动：SSH 主机密钥 TOFU 验证、Windows DPAPI 凭据加密、SSL 证书详情与到期警告。核心是一句话——**在任何凭据离开本机之前，先完成服务器身份校验**。

## 为什么要做这次加固

SSLTool 第一版上线后，我自己复盘出三个"能用但不安全"的地方：

1. **SSH 主机密钥是自动信任的**。代码里一行 `client.set_missing_host_key_policy(paramiko.AutoAddPolicy())`，任何服务器的密钥第一次见面就无条件写进信任列表。这意味着如果有人在你和服务器之间架一个中间人，客户端会**高高兴兴地把密码发给他**——而且永远不告诉你。
2. **回退凭据存储只做了 base64**。系统钥匙串（keyring）不可用时，密码落盘成 `secrets.json`，内容是 base64 混淆——和明文几乎没有区别。
3. **SSL 控制台连证书有效期都不显示**。连上一个自签名的 TLS 服务，你既看不到它是不是要过期，也看不到它到底是谁。

v1.2 的目标就是把这三件事按"纵深防御"的标准重做一遍。

## 第一件事：TOFU 主机密钥验证

### 一个反直觉的发现：policy 检查发生在密码之后

第一反应当然是用 paramiko 自带的 host key policy——`RejectPolicy` 加上自定义回调，看起来就是为"首次询问用户"设计的。但翻了 `SSHClient.connect()` 的源码之后发现不对劲，它的流程是这样的：

```
t.start_client(timeout=timeout)     ← 密钥交换完成，TCP 已建立
...
self._policy.missing_host_key(...)  ← 现在才问 policy！
...
t.auth_password(username, password) ← 最后才认证
```

注意顺序：**`start_client()` 和 policy 检查之间，没有任何认证发生；但 policy 检查之后就是认证**。看起来没问题？问题在于 paramiko 的 `missing_host_key()` 协议：它只能抛异常或返回。如果我们在里面弹一个"是否信任"的对话框——这在 PyQt 的界面线程里执行会死锁，在 worker 线程里又没法弹窗；而如果返回（信任），AutoAddPolicy 会直接写入。更要命的是：万一将来 paramiko 调整内部流程，把 `start_client` 拆成两段、或者某条代码路径漏掉了 policy 调用，**密码就会在没有任何校验的情况下发出去**。把安全属性挂在"库的内部调用顺序"上，太脆弱了。

### 解法：探测先行，凭据后置

自己掌控顺序，把校验做成**一条独立的前置阶段**：

```python
def probe_host_key(host, port, timeout=8.0):
    """只做密钥交换取回服务器主机密钥，绝不发送任何用户凭据。"""
    sock = socket.create_connection((host, int(port)), timeout=timeout)
    t = paramiko.Transport(sock)
    try:
        t.start_client(timeout=timeout)   # KEX 完成即停
        return t.get_remote_server_key()  # 拿到密钥，关闭
    finally:
        t.close()
```

用**裸 `paramiko.Transport`** 而不是 `SSHClient`：`start_client()` 只完成 TCP + 密钥交换，`userauth` 阶段（密码 / 私钥签名）根本不会发生。之后：

```
探测取回密钥 → 与 ~/.ssltool/known_hosts.json 比对
  ├─ 一致        → 直接建立认证连接
  ├─ 无记录      → 弹「首次连接」指纹确认框（TOFU）
  └─ 与记录不符  → 弹「密钥已变更」警告框（疑似中间人）
认证连接建立后 → 再次比对实际密钥指纹，防两条 TCP 连接被分别劫持
```

### 为什么还要"复核一次"？

探测连接和认证连接是**两条独立的 TCP 连接**。理论上，一个狡猾的中间人可以给探测连接喂真密钥（骗过校验），给认证连接喂假密钥（收密码）。所以认证连接建立后，我们把 `client.get_transport().get_remote_server_key()` 的指纹和用户确认过的指纹再比一次，不一致立即断开：

```python
actual = fingerprint_sha256(client.get_transport().get_remote_server_key())
if actual != fp:
    client.close()
    raise RuntimeError("主机密钥在连接过程中被替换（疑似中间人攻击），已中止")
```

### 工程细节：一个阻塞门（gate）

worker 线程探测完需要**阻塞等用户做决定**，界面线程弹完对话框需要**唤醒 worker**。中间用一个 `threading.Event` 门：

```python
class HostKeyGate:
    def wait(self, timeout=300.0):
        key = probe_host_key(self.host, self.port)
        status, fp, expected = check_known_host(self.host, self.port, key)
        if status == "ok":
            return "ok", key, fp, expected
        # unknown / changed → 发信号给 GUI 弹框，阻塞等待
        if not self._event.wait(timeout):
            return "timeout", ...
        return "accepted" if self._accepted else "rejected", ...
```

- 用户点了"接受"才把指纹写入 `known_hosts.json`（拒绝不落盘）；
- 标签页关闭 / 应用退出时 `cancel()` 唤醒 worker，不会卡死线程（否则要等 300 秒超时）；
- 首次连接和密钥变更用**两个不同的对话框**：前者引导核对指纹，后者红色大字警告"疑似中间人攻击"，两个指纹（旧记录 / 新收到）并排显示，可以复制出来找管理员核实。

存取的格式是一个按 `"[host]:port"` 为键的 JSON（方括号保证 IPv6 也安全），指纹用 OpenSSH 同款的 `SHA256:` + 无填充 base64。

## 第二件事：Windows DPAPI 加密回退存储

keyring 不可用时（比如某些精简环境没有 Windows Credential Manager 后端），密码要落盘。原版 base64 方案等于裸奔。这次用 Windows 自带的 **DPAPI（Data Protection API）**，不需要用户管理密钥，密文只有当前 Windows 用户能解开：

```python
class _DATA_BLOB(ctypes.Structure):
    _fields_ = [("cbData", ctypes.wintypes.DWORD),
                ("pbData", ctypes.POINTER(ctypes.c_char))]

# CryptProtectData / CryptUnprotectData（crypt32.dll）
protect(payload.encode("utf-8"))   # → bytes（密文）
unprotect(blob)                    # → 明文（仅本用户可解）
```

实现上三个注意点：

1. **启动时自检**：`_setup_dpapi()` 里先做一次加密→解密的 roundtrip，成功了才启用 DPAPI 路径（防止在某些系统上静默失败）；
2. **兼容旧格式**：读文件时先看 `{"format": "dpapi", "blob": "..."}` 的新格式，认不出就按旧 base64 格式解，读到后立即用 DPAPI 重写升级；
3. 密文 `LocalFree` 释放、结构体按 ctypes 规范定义，避免内存泄漏。

非 Windows 平台保持兼容降级（仍建议配好 keyring）。

## 第三件事：SSL 证书详情与到期警告

SSL 控制台连接成功后，除了原有的 TLS 版本 / 加密套件 / SHA-256 指纹，v1.2 增加了证书的**主题、颁发者、序列号、SAN、有效期**，并且：

- 用 `cryptography` 库解析 DER（`tls.getpeercert(binary_form=True)` 拿原始字节，比 ssl 模块的字典式返回值信息全）；
- 有效期不足 **30 天**打印「⚠ 证书将在 N 天后到期」；已过期打印「⚠ 证书已过期！」；
- 解析失败不炸连接——指纹行照常显示，详情行静默降级。

一个兼容性小细节：`cert.not_valid_after_utc` 是 cryptography 42 才有的属性，代码里做了 fallback（旧属性 + 手动补时区）。

## 测试怎么证明"密码真的没发出去"

这类安全逻辑的测试最难写——怎么断言"没有发生"？我们的 e2e 测试里嵌了一个 paramiko 迷你 SSH 服务器，在服务端 `check_auth_password()` 里装计数器：

```python
class EchoServer(paramiko.ServerInterface):
    auth_attempts = 0
    def check_auth_password(self, username, password):
        EchoServer.auth_attempts += 1
        return paramiko.AUTH_SUCCESSFUL
```

然后跑三个场景：

1. **未知密钥 + 接受** → 连接成功，指纹写入 known_hosts；
2. **密钥变更 + 拒绝** → 客户端报"已取消"，断言 `auth_attempts == 0`——服务器**一次认证都没收到**，密码从未离开本机；
3. **密钥变更 + 接受** → 记录更新并连接成功。

冒烟测试还覆盖了 DPAPI 密文非明文断言（读 `secrets.json` 原文不含密码字符串）、证书解析（用 cryptography 现场生成自签证书，覆盖"30 天内到期"和"已过期"两种警告）。最终：**冒烟 44/44、e2e 8/8 全绿**。

## 写在最后

这次加固最大的收获是那个反直觉的发现：**不要把安全属性寄托在第三方库的内部调用顺序上**。paramiko 的 policy 机制本身没问题，但"校验在认证之前"这个顺序是它的内部实现细节，而我们自己搭一个"探测先行"的显式流程，顺序由自己的代码保证，并且在两条连接上都校验——即使将来库改了，防线也不会失守。

安全默认值应该前置校验：先问清楚"你是谁"，再交出"我是谁"的证明。

---

项目地址：https://github.com/Urtaome0211/SSLTool · 版本：v1.2 · 完整测试可复现：`python tests/smoke_test.py`、`python tests/e2e_test.py`
