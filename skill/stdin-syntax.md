```!
stdin 协议完整语法参考。说明 `text:` / `key:` 行式语法、旧 inline 宏语法、按键名、组合键、等待、重复、raw/hex/base64/bytes 精确字节、转义规则、限制和常见示例。
```

# stdin 语法大全

本文说明 Agent 的 `stdin:会话ID` 协议。`stdin` 用来给已有 `shell` 会话发送输入：普通文本、回车、方向键、Ctrl/Alt/Shift 组合键、等待、重复和精确字节都走这个协议。

## 1. 基本结构

````text
```stdin:会话ID
text: 普通文本
key: [enter]
```
````

`stdin` 不会自动补回车。需要提交命令、确认输入或换行时，必须显式写 `key: [enter]`。

## 2. 推荐格式：按行写步骤

优先使用行式语法。它比旧 inline 语法更清楚，也更适合 AI 稳定输出。

```text
text: 你好
key: [ctrl]+[a]
key: [ctrl]+[c]
```

每一行是一个步骤：

- `text:` 输入普通文本。
- `key:` 发送按键或组合键。
- `wait:` / `sleep:` / `delay:` 等待。
- `raw:` / `hex:` / `base64:` / `bytes:` / `keys:` 发送精确字节或旧式按键序列。
- `repeat:` 重复一段 inline 宏。

触发规则：

- 只要 stdin 里任一行以 `text:`、`key:`、`wait:`、`raw:` 等行首标记开头，整段就按行式语法解析。
- 只有整段没有任何行首标记时，才按旧 inline 语法解析。
- 在行式语法中，裸行或未知前缀会作为字面文本输入，不解析 `[enter]`，也不保留该行行尾换行。
- 为减少歧义，普通文本始终写 `text:`，按键始终写 `key:`。

## 3. text 行

`text:` 后面的内容会作为普通文本输入，不会把 `[enter]` 当成按键。

```text
text: npm run dev
key: [enter]
```

`text:` 后如果有一个分隔空格，会去掉这个分隔空格：

```text
text: hello
```

输入的是 `hello`，不是前面带空格的 ` hello`。如果确实要以空格开头，可以多写一个空格：

```text
text:  hello
```

`type:` 和 `paste:` 是 `text:` 的别名。

`text:` 只输入这一行冒号后面的文本，不自动附带行尾换行。需要输入多行或提交时，显式加 `key: [enter]`：

```text
text: 第一行
key: [enter]
text: 第二行
key: [enter]
```

## 4. key 行

`key:` 用来发送按键。推荐仍然保留方括号，因为最不容易歧义：

```text
key: [enter]
key: [esc]
key: [up]
```

也支持更短写法：

```text
key: enter
key: ctrl+a
key: alt+left
key: ctrl+a b
```

在 `key:` 行里，`+` 表示同一拍组合按键，空格或换行表示按顺序一个个按：

```text
key: ctrl+a b
```

等价于先按 `Ctrl+A`，再按 `b`，也等价于分两行写：

```text
key: ctrl+a
key: b
```

不要用 `key: ctrl+a+c`；它会被理解成同一拍里有两个主键，这个协议不支持。要先 `Ctrl+A` 再按 `c`，写：

```text
key: ctrl+a c
```

`key:` 行一旦写了，就不会回退成普通文本。非法组合会报错，例如：

```text
key: ctrl+a+c
key: a+a+d
```

如果要输入字面量 `a+a+d`，写：

```text
text: a+a+d
```

如果要按顺序发送 `a`、`a`、`d`，写：

```text
key: a a d
```

常用按键：

- 回车与换行：`[enter]`、`[return]`、`[cr]`、`[lf]`
- 控制键：`[esc]`、`[tab]`、`[backspace]`、`[delete]`、`[insert]`
- 方向与导航：`[up]`、`[down]`、`[left]`、`[right]`、`[home]`、`[end]`、`[pageup]`、`[pagedown]`
- 功能键：`[f1]` 到 `[f12]`
- 空格：`[space]` 或 `[sp]`

Linux/PTY/TUI 场景默认用 `[enter]`。如果明确要发送 LF，用 `[lf]` 或 `raw:\n`；如果明确要发送 CR，用 `[cr]` 或 `raw:\r`。

## 5. 组合键

组合键推荐写成：

```text
key: [ctrl]+[c]
key: [ctrl]+[a]
key: [alt]+[left]
key: [shift]+[tab]
```

也支持紧凑写法：

```text
key: ctrl+c
key: c-c
key: ^c
key: ctrl-a
```

支持的修饰键别名：

- Ctrl：`ctrl`、`control`
- Alt：`alt`、`meta`、`option`
- Shift：`shift`

组合键由若干修饰键和一个主键组成。例如 `key: [ctrl]+[alt]+[delete]` 表示同一拍按下 `Ctrl+Alt+Delete`。

规则是：一组由 `+` 连接的组合键里，最多只能有一个主键；其他部分必须是修饰键。

可以：

```text
key: [ctrl]+[alt]+[delete]
key: ctrl+c
key: shift+tab
```

不可以：

```text
key: ctrl+a+c
key: a+a+d
```

如果要表达顺序按键，用空格分开：

```text
key: ctrl+a b
```

这表示先按 `Ctrl+A`，再按 `b`，也等价于分两行写 `key: ctrl+a` 和 `key: b`。如果要先 `Ctrl+A` 再 `Ctrl+B`，写：

```text
key: ctrl+a ctrl+b
```

`key: ctrl+a+c` 不表示顺序按键；它会被理解为试图同一拍按 `Ctrl+A+C`，这种写法不支持。顺序按键请用空格或多行 `key:`。

如果要全选后复制，写成两个顺序步骤：

```text
key: [ctrl]+[a]
key: [ctrl]+[c]
```

也可以写在一行，用空格隔开两个组合键：

```text
key: ctrl+a ctrl+c
```

## 6. 等待

等待用 `wait:`，单位默认是毫秒，也支持秒：

```text
wait: 500
wait: 1s
wait: 250ms
```

别名：`sleep:`、`delay:`。

限制：

- 单次等待最多 60 秒。
- 一个 stdin 宏总等待最多 120 秒。

## 7. 精确字节

需要发送非普通按键或终端转义序列时，用精确字节语法。

Raw 转义：

```text
raw: \r
raw: \n
raw: \x1b\x5bA
```

支持的 raw 转义：

- `\\` 反斜杠
- `\r` CR
- `\n` LF
- `\t` Tab
- `\b` Backspace
- `\f` Form feed
- `\v` Vertical tab
- `\0` NUL
- `\e` / `\E` ESC
- `\xHH` 两位十六进制字节

Hex：

```text
hex: 1b5b41
hex: 1b 5b 41
hex: 0x1b,0x5b,0x41
```

Base64：

```text
base64: SGVsbG8K
```

Bytes：

```text
bytes: 13 10
bytes: [13,10]
bytes: 0x1b 0x5b 0x41
```

Keys 旧式序列：

```text
keys: ctrl-c enter
keys: ["ctrl-c", "enter"]
```

## 8. 旧 inline 语法

如果不使用行首标记，整段按 inline 宏解析。

```text
npm run dev [enter]
```

inline 里不在 `[]` 中的内容是普通文本，`[]` 中的是按键或特殊动作：

```text
[ctrl]+[c]
[up]*2 [enter]
[repeat:3 [up] [enter]]
[raw:\x1b\x5bA]
```

行式语法中的 `key:` 可以嵌入 inline 按键写法。只要 `key:` 的内容含 `[` 或 `]`，这一行会按 inline 片段解析：

```text
key: [ctrl]+[c]
key: [up]*2
key: [ctrl]+[a] [b]
```

因此 `key: [up]*2` 是重复两次上箭头；`key: up up` 是两个顺序按键。简单按键推荐写成多行，复杂重复或组合可用这种 inline 片段。

## 9. 重复

inline 后缀重复：

```text
key: [up]*3
```

重复一段宏：

```text
repeat: 3 [up] [enter]
```

限制：重复次数必须在 0 到 200 之间。

## 10. 转义普通文本

在 inline 普通文本中，如果要输入字面量 `[`、`]`、`\`，写：

```text
\[
\]
\\
```

在 `text:` 行中，`[` 和 `]` 不会被当成按键；一般不需要转义，除非你希望统一保守写法。

行式语法中的裸行也会作为字面文本输入，但不建议依赖裸行：它不会解析 `[enter]`，也不会保留行尾换行。需要文本就写 `text:`，需要按键就写 `key:`。

## 11. 常见场景

确认提示：

```text
text: y
key: [enter]
```

发送 Ctrl-C：

```text
key: [ctrl]+[c]
```

在 Vim 保存退出：

```text
key: [esc]
text: :wq
key: [enter]
```

选择历史命令并执行：

```text
key: [up]
key: [up]
key: [enter]
```

输入命令并提交：

```text
text: npm run dev
key: [enter]
```

全选并复制：

```text
key: [ctrl]+[a]
key: [ctrl]+[c]
```

输入中文后全选复制：

```text
text: 你好
key: [ctrl]+[a]
key: [ctrl]+[c]
```

发送上方向键的原始终端序列：

```text
raw: \x1b\x5bA
```

## 12. 限制与安全边界

当前实现会限制 stdin 宏规模：

- 单个 `wait:` 最多 60 秒。
- 总等待最多 120 秒。
- 总步骤最多 1000。
- 总输入最多 1048576 字节。
- `repeat` 次数最多 200。

这些限制用于避免一次 stdin 操作让会话长时间卡住或写入过大数据。

## 13. 选择建议

- 普通输入和按键混合时，优先用 `text:` / `key:`。
- TUI、编辑器、确认提示、REPL 场景，优先显式写 `key: [enter]`、`key: [esc]`、`key: [ctrl]+[c]`。
- 需要精确终端序列时，用 `raw:`、`hex:`、`bytes:`。
- 需要文件内容时，不要靠 `sendfile` 或 `file:` 的结果推断文件本体；用 `read` 读取。
