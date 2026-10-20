# SSLTool v2.0：终端体验的五个「小」功能，和一个「大」坑

> SSLTool 是一个扁平化界面的 SSL / SSH / SFTP 桌面客户端（Python + PyQt5 + Paramiko + pyte）。
> v1.2 我们做了安全加固（主机密钥 TOFU、DPAPI、证书到期警告），v2.0 的主题是**终端体验**：
> 会话日志、终端内搜索、URL 点击、括号粘贴、备用屏幕。
>
> 这五个功能里，前四个都是「顺手就能做」的活儿；真正的硬骨头是最后一个——
> 备用屏幕（alt-screen）在 pyte 里几乎没有原生支持，而它的解法意外地绕了一个挺有意思的弯。

---

## 一、会话日志：日志系统的最重要原则是「不碍事」

需求很简单：终端里发生的一切（服务器的输出、用户的输入、连接 / 断开 / 报错事件）自动落到
`~/.ssltool/logs/` 下，文件名带时间戳与会话名，标签页上有个开关。

实现上有一个容易被忽略的点：**日志必须永远不能干扰终端本身**。
SSHWorker 在工作线程里 emit 数据，主线程槽函数先 feed 给终端组件、再写日志文件，
所以写文件发生在 GUI 线程——磁盘慢一点，界面最多轻微卡顿，连接与终端渲染不受影响。

```python
# app/core/session_log.py（节选）
class SessionLog:
    def write(self, data: bytes) -> None:
        if not self._f:
            return
        try:
            self._f.write(data.decode("utf-8", errors="replace"))
            self._f.flush()          # 立刻落盘，崩溃也不丢；测试也能实时读到
        except Exception:
            pass                      # 日志绝不干扰终端

    def note(self, text: str) -> None:      # 事件行：[2026-10-20 20:12:01] 已连接
        ...
```

所有异常一律吞掉——磁盘满、权限错、路径被删，都不应该让一个 SSH 会话跟着陪葬。
`flush()` 是特意加的：会话进行中就能 `tail -f` 日志文件，也让端到端测试可以在连接尚未关闭时就断言日志内容。

## 二、括号粘贴：一行包裹，防住「多行粘贴变命令」

把多行文本直接喂给终端，bash 会把每一行当成一条命令执行——复制一段多行命令粘贴过去，
最后一行（或中间的 `sudo` 行）可能就这么被执行了。现代终端用**括号粘贴**（bracketed paste）解决：
数据用 `\x1b[200~` 和 `\x1b[201~` 包起来发送，readline 识别后把整个粘贴当成「输入文本」而不是「逐行命令」。

```python
def paste_text(self, text: str) -> None:
    self.dataSent.emit(b"\x1b[200~" + text.encode("utf-8") + b"\x1b[201~")
```

Ctrl+V、Ctrl+Shift+V、中键粘贴全部走这一个入口，一行代码的事。但要验证它真的生效，
需要看**服务器实际收到了什么**——这是端到端测试干的活（见第五节）。

## 三、终端内搜索：宽字符才是坑

Ctrl+F 弹出搜索条，在**回看缓冲**（已滚动出屏幕的历史行）里找文本，回车下一个、Shift+回车上一个、循环回绕。

难点不在搜索本身（大小写不敏感的子串匹配），而在**命中位置的定位**：
终端一行里一个中文字符占两个单元格，字符串下标和屏幕列号不是一回事。
要把命中范围画成高亮块，就得先把字符索引映射成单元格列：

```python
def _line_char_cols(self, line, cols):
    # 返回 cols 个元素：第 i 个字符占据的单元格列
    # 宽字符占 2 列，窄字符占 1 列——用 unicodedata.east_asian_width 判断
    ...
```

搜索命中高亮是深黄色（#6b5b00），当前命中亮黄色（#f7d700），和「复制选中」的高亮区分开。
数据一到就增量重算命中位置，保证滚动缓冲更新后搜索不失效。

## 四、URL 点击：识别不难，边界处理才是细节

正则 `https?://[^\s'"<>]+` 抓出来，再把末尾的标点（`.` `,` `;` `:` `!` `?`）剥掉——
否则终端里「详见 https://example.com。」这种句子点开就 404。
Ctrl+左键用 `QDesktopServices.openUrl` 交给系统浏览器，右键菜单给「打开链接 / 复制链接」。
URL 的列区间计算同样复用上面的字符→列映射，所以跨宽字符的链接也能准确命中。

## 五、备用屏幕：pyte 不支持的 alt-screen，和被「焊死」的监听器

**这是 v2.0 的核心战场。** `vim`、`less`、`top` 这类全屏程序进入时发 `ESC [?1049h`（保存光标、切到备用屏幕缓冲），
退出时发 `ESC [?1049l`（切回主屏、恢复光标）。没有备用屏幕支持时，vim 的界面直接画在主屏缓冲上，
退出后满屏残留——v1.x 的已知限制就是这么写的。

### 5.1 想「换屏幕」，但 pyte 的 Stream 不让你换

最直觉的方案：进出备用屏时，把 `pyte.Stream` 的 `listener` 从主屏 `Screen` 换成备用屏 `Screen`。
然后我翻开 pyte 0.8.2 的源码，发现这条路**根本走不通**：

```python
# pyte/streams.py：Stream.feed()
def feed(self, data):
    ...
    draw = self.listener.draw          # ① 每块数据进来时缓存当前 listener 的 draw
    ...
# Stream 初始化解析器时：
self._parser_fsm = ...
listener = self.listener               # ② 解析器状态机在初始化时就把 listener 捕获了
dispatcher = create_dispatcher(mapping)
for event, attr in dispatcher.items():
    dispatch[event] = getattr(listener, attr)   # ③ 所有事件处理器都是「老 listener」的绑定方法
```

两个坑：解析器状态机初始化时把 `listener` 捕获进闭包，把所有 `getattr(listener, attr)`
生成的**绑定方法**做成了分发表——中途换 `stream.listener`，状态机照样往老屏幕上画；
而且 `feed()` 开头就缓存了 `draw`，同一块数据里后半截字节也会画错地方。

### 5.2 反过来：屏幕不动，状态换班

既然 listener 的身份必须从一而终，那就让**同一个屏幕对象**自己在两套状态之间换班——
一个「外观（facade）」：`_SSLToolScreen(pyte.HistoryScreen)` 永远是 listener，
进出备用屏时，把主屏的完整状态收进抽屉，再把备用 `pyte.Screen` 的状态搬上自己的台面：

```python
_SCREEN_STATE_FIELDS = ("columns","lines","buffer","dirty","mode","margins","title",
                        "icon_name","charset","g0_charset","g1_charset",
                        "tabstops","cursor","savepoints","saved_columns","history")

def _enter_alt(self):
    if self._alt_active: return                    # 幂等：vim 里再开 less 不会再进一次
    self._alt = pyte.Screen(self.columns, self.lines)
    # 把备用屏初始化成与主屏一致的状态（含历史缓冲换成 None）
    snapshot = self._snapshot_of(self._alt)
    for f in _SCREEN_STATE_FIELDS:
        self.__dict__[f], snapshot[f] = snapshot[f], self.__dict__[f]   # 状态对调
    self._alt_active = True
```

两行状态对调，主屏状态进了抽屉，备用屏状态上了台面，**listener 还是同一个对象**，
解析器状态机、分发表、`draw` 缓存全部继续有效。

### 5.3 xterm 语义抠细节：1049 与 47 的差别

- **1049**（`vim`/`less` 用）：进入时保存光标、退出时恢复光标；
- **47**（老程序用）：两个缓冲**共享**同一个光标位置，进出都不动光标。

```python
def set_mode(self, *modes, **kwargs):
    private = kwargs.get("private")
    for m in modes:
        if private and m == 1049:      # 1049h = 保存光标 + 进入备用屏
            self.save_cursor(); self._enter_alt()
        elif private and m == 47:
            self._enter_alt(preserve_cursor=True)
    ...
```

pyte 对 47/1049 没有特殊处理，只是把它们放进 mode 位集合——语义全在覆写里补。
另外两个容易被忽略的点：退出备用屏时若窗口尺寸变了（备用屏期间拖了窗口），要用
`pyte.HistoryScreen.resize` 把主屏校正回来；备用屏期间清屏序列（`CSI 2J`）不能把主屏的
滚动历史一起清掉，所以 `erase_in_display` 和 `before_event` 里都要对「备用屏没有历史」加守卫。

### 5.4 调试路上踩的五个坑（都进了回归测试）

1. **同名 staticmethod 与实例方法不共存**——第二个定义直接覆盖第一个，`TypeError: takes 1 positional argument`；静态助手改名 `_snapshot_of(screen)`。
2. 普通 `pyte.Screen` **没有 `history` 字段**——快照助手要 `getattr(f, None)`。
3. `HistoryScreen.after_event` 的 line 事件访问 `self.history.position`——备用屏 `history=None`，AttributeError；覆写 `after_event`，无历史时只标 dirty。
4. 同样的守卫在 `before_event`（pyte screens.py 滚动逻辑）再来一遍。
5. 47 模式光标语义：最初当成 1049 处理，对照 xterm 行为改成共享光标，测试期望跟着修正。

## 六、怎么证明它真的能用：72 项冒烟 + 9 项端到端

- **冒烟测试 72 项**（离屏）：备用屏幕的进出 / 光标保存恢复 / 47 共享光标 / 备用屏内清屏不影响主屏；
  括号粘贴的包裹格式；搜索的单命中跳转 / 60 命中计数 / 循环回绕；URL 的识别 / 第二链接 / 尾部标点剔除；
  SessionLog 的文件名安全化 / 中文与损坏字节（`\ufffd` 替换）/ 事件行 / 结束标记。
- **端到端测试 9 项**（内置 SSH 服务器）：新增的 `test_terminal_experience` 走**真实 SSH 链路**——
  本地发 `ESC [?1049h` 字符，本地解析器即刻进备用屏；服务器回显再喂回来（回显去重模型对 ESC 序列有切分处理，且进出已幂等），
  断言退出后主屏没有被 vim 界面污染；粘贴经剪贴板走 `_paste_clipboard`，断言**服务器侧**收到的字节确实被
  `\x1b[200~` / `\x1b[201~` 包裹（EchoServer 加了个 `record_data` 开关专门记录收到的原始字节）；
  再断言会话日志文件在**连接尚未关闭时**就包含欢迎横幅。

## 七、小结

五个功能里，四个是「体验」、一个是「工程」：

| 功能 | 实现量 | 有意思的点 |
| --- | --- | --- |
| 会话日志 | 小 | 异常全吞 + 实时 flush，日志绝不碍事 |
| 括号粘贴 | 小 | 一行包裹，靠服务器侧断言验证 |
| 终端搜索 | 中 | 宽字符的字符索引 → 单元格列映射 |
| URL 点击 | 中 | 尾部标点剔除、跨宽字符定位 |
| 备用屏幕 | 大 | pyte 的 listener 被状态机「焊死」→ 外观对象状态换班 |

备用屏幕这题最有意思的地方在于：第一反应是「换屏幕」，但框架的约束（解析器在初始化时捕获
listener）决定了「换状态」才是正确解法——**顺藤摸瓜读源码，顺着框架的纹理找设计**，
比和框架较劲省力得多。这也再次说明把关键行为做成回归测试有多重要：
五个坑里任何一个漏了，`vim` 一退出就是满屏乱码，用户报告一个「终端坏了」，你都不知道该从哪查起。

---

*SSLTool v2.0 已在 GitHub 发布：<https://github.com/Urtaome0211/SSLTool/releases/tag/v2.0>，Windows x64 可执行文件见发布资产。*
