```!
stdin 协议完整语法参考。普通文本直接写；明确控制前缀包括 `key:`、`line:`、`paste:`、`wait:`、`raw:`、`hex:`、`base64:`、`bytes:`、`keys:`、`repeat:`。高层语法覆盖常见人类终端操作，精确字节语法可兜底表达理论上任意 stdin 输入。
```

# stdin 语法大全

本文说明 Agent 的 `stdin:会话ID` 协议。它用于给已有 `shell` 会话发送输入：普通文字、回车、方向键、Ctrl/Alt/Shift 组合键、EOF、Ctrl-C、等待、重复、粘贴文本和任意字节。

设计目标：

- 普通文本像普通文本一样写。
- 常见按键用易读的 `key:` 表达。
- 大段文本可以显式 `paste:`。
- 需要提交一行时可以用 `line:`。
- 任何高层语法没覆盖的输入，都能用 `raw:` / `hex:` / `base64:` / `bytes:` 兜底。

## 1. 总规则

顶层始终按行解析。

- 普通文本行：直接输入该行内容，不自动加换行。
- 控制行：只有顶格出现的明确控制前缀才有特殊含义。
- 旧顶层 inline 语法已删除，`npm run dev [enter]` 只是普通文本。
- `text:`、`type:` 没有特殊语义，会作为普通文本输入。
- `paste:` 是控制前缀；如果要输入字面量 `paste:`，写 `\paste:`。

支持的控制前缀：

- `key:` 发送按键或组合键。
- `line:` 输入文本并发送回车。
- `paste:` 显式粘贴一段普通文本。
- `wait:` / `sleep:` / `delay:` 等待。
- `raw:` / `escape:` / `escaped:` 发送转义字节。
- `hex:` 发送十六进制字节。
- `base64:` / `b64:` 发送 Base64 字节。
- `bytes:` 发送字节数组。
- `keys:` 发送旧式按键序列。
- `repeat:` 重复一段括号宏。

## 2. 最常用写法

输入命令并执行：

```text
npm run dev
key: [enter]
```

也可以写成一行：

```text
line: npm run dev
```

确认提示：

```text
y
key: [enter]
```

输入字面量 `text: hello`：

```text
text: hello
key: [enter]
```

输入字面量 `paste:`：

```text
\paste: hello
key: [enter]
```

## 3. 普通文本

不是控制前缀的行，都会作为普通文本输入。

```text
hello
world
```

这会输入 `helloworld`，中间不会自动插入换行。需要换行或提交时写 `key: [enter]`、`line:` 或 `raw: \n`。

普通文本可以包含冒号、方括号、加号、中文、Emoji、空格等：

```text
foo: bar
a+a+d
[enter]
你好 🙂
```

如果普通文本刚好以控制前缀开头，加反斜杠转义：

```text
\key: [enter]
\wait: 1s
\repeat: 3 [up]
\paste: hello
```

这会输入字面量 `key: [enter]wait: 1srepeat: 3 [up]paste: hello`。

## 4. line 和 paste

`line:` 输入冒号后的文本，并自动发送回车。适合命令、菜单项、yes/no 回答：

```text
line: git status
line: y
```

冒号后面紧跟的一个空格或 Tab 会当作分隔符去掉。需要让内容本身以空格开头时，多写一个空格：

```text
line:  indented
```

这会输入一个开头空格，然后输入 `indented` 并回车。

`paste:` 显式输入普通文本，不解析其中的按键名。适合 AI 表达“这里是文本，不是按键”：

```text
paste: npm run dev [enter]
key: [enter]
```

这会先输入字面量 `npm run dev [enter]`，再发送真实回车。

`paste:` 和 `line:` 支持轻量转义：

- `\[` 输入 `[`
- `\]` 输入 `]`
- `\\` 输入反斜杠

多行文本用 heredoc 形式，最适合代码、配置、带缩进文本和包含控制前缀字样的内容：

```text
paste: <<EOF
line 1
key: [enter] 只是文字
  缩进会保留
EOF
key: [enter]
```

规则：

- `paste: <<EOF` 后面的行都会作为字面文本输入，直到遇到单独一行 `EOF`。
- 结束标记可以换成任意字母/数字/下划线/短横线组合，例如 `END`、`PY`、`CONFIG_1`、`123`。
- heredoc 内容里的 `key:`、`wait:`、`paste:`、`[enter]` 都是普通文字，不会被解析。
- 非空 heredoc 会保留内部换行，并在末尾保留一个换行；空 heredoc 不写入字节。
- 结束标记必须顶格、独占一行。

## 5. key

`key:` 发送按键。推荐使用方括号，最不容易歧义：

```text
key: [enter]
key: [esc]
key: [up]
key: [ctrl]+[c]
key: [ctrl]+[d]
```

短写也支持：

```text
key: enter
key: ctrl+c
key: ctrl+d
key: alt+left
key: shift+tab
```

规则：

- `+` 表示同一拍组合键。
- 空格表示按顺序发送多个按键。
- 一组组合键最多只能有一个主键，其他必须是修饰键。

顺序按键：

```text
key: ctrl+a c
```

表示先 `Ctrl+A`，再按 `c`。

组合键：

```text
key: [ctrl]+[alt]+[delete]
```

非法例子：

```text
key: ctrl+a+c
key: a+a+d
```

如果要输入字面量 `a+a+d`，直接写普通文本：

```text
a+a+d
```

## 6. 常用按键名

控制键：

- `enter` / `return` / `cr`
- `lf` / `newline` / `linefeed`
- `esc` / `escape`
- `tab`
- `backspace` / `bs` / `rubout`
- `delete` / `del`
- `insert` / `ins`
- `space` / `sp`
- `eof` / `eot` / `ctrl+d`
- `interrupt` / `sigint` / `cancel` / `ctrl+c`

方向与导航：

- `up`
- `down`
- `left`
- `right`
- `home`
- `end`
- `pageup` / `page-up` / `pgup`
- `pagedown` / `page-down` / `pgdn`

功能键：

- `f1` 到 `f24`

小键盘常用别名：

- `kp-enter` / `numpad-enter`
- `kp-plus`
- `kp-minus`
- `kp-multiply`
- `kp-divide`
- `kp-decimal`
- `kp0` 到 `kp9`
- `numpad0` 到 `numpad9`

修饰键：

- Ctrl: `ctrl` / `control`
- Alt: `alt` / `meta` / `option`
- Shift: `shift`

## 7. 重复

`key:` 的方括号片段支持后缀重复：

```text
key: [up]*3
key: [ctrl]+[a]*2
```

`repeat:` 重复一段内部括号宏：

```text
repeat: 3 [up] [enter]
repeat: 2 [tab]
```

`repeat:` 重复的是括号片段，不是整行语法。

## 8. 等待

等待默认单位是毫秒，也支持秒：

```text
wait: 500
wait: 250ms
wait: 1s
sleep: 1s
delay: 500
```

注意：

- 等待时长没有硬性上限，但必须是有限的非负数字。
- 长等待会占用当前 Agent 回合；需要等待很久时应先说明原因，并确保用户可以停止。

## 9. 精确字节：理论完备兜底

只要人类能通过终端 stdin 发送某个字节序列，就可以用精确字节语法表达。

`raw:` 使用转义：

```text
raw: \r
raw: \n
raw: \t
raw: \e[31m
raw: \x1b\x5bA
```

支持：

- `\\`
- `\r`
- `\n`
- `\t`
- `\b`
- `\f`
- `\v`
- `\0`
- `\e` / `\E`
- `\xHH`

`hex:`：

```text
hex: 1b5b41
hex: 1b 5b 41
hex: 0x1b,0x5b,0x41
```

`base64:`：

```text
base64: SGVsbG8K
```

`bytes:`：

```text
bytes: 13 10
bytes: [13,10]
bytes: 0x1b 0x5b 0x41
```

任意 0-255 字节都能用 `bytes:` 表达。

注意：语法层面可以表达任意字节，但 PTY/终端的当前模式仍会影响前台程序实际收到什么。典型 cooked mode 下，`Ctrl-C` 可能先被终端驱动转换成 SIGINT，`Ctrl-D` 可能作为 EOF，回车也可能经历 CR/LF 转换；这是终端行为，不是语法缺口。需要验证原始控制字节时，让目标程序进入 raw mode。

`keys:` 是兼容式按键序列入口，适合旧配置或 JSON 列表：

```text
keys: ctrl-c enter
keys: ["ctrl-c", "enter", 27]
```

## 10. 复杂场景

发送 Ctrl-C：

```text
key: [interrupt]
```

发送 EOF / Ctrl-D：

```text
key: [eof]
```

Vim 保存退出：

```text
key: [esc]
line: :wq
```

选择历史命令并执行：

```text
key: [up]
key: [up]
key: [enter]
```

全选、复制：

```text
key: [ctrl]+[a]
key: [ctrl]+[c]
```

输入带方括号的文字并提交：

```text
paste: npm run dev [enter]
key: [enter]
```

粘贴多行 Python 代码并用 EOF 结束：

```text
line: python -
paste: <<PY
print("hello")
print("key: [enter] is text")
PY
key: [eof]
```

发送 ANSI 上箭头原始序列：

```text
raw: \x1b\x5bA
```

发送 0 到 255 的所有字节：

```text
bytes: 0 1 2 3 4 5 6 7 8 9 ... 255
```

## 11. 限制

当前实现限制：

- `wait:` 没有单次或总等待硬性上限，但必须是有限的非负数字。
- 总步骤最多 1000。
- 总输入最多 1048576 字节。
- `repeat` 次数最多 200。

这些限制用于避免一次 stdin 操作让会话卡住或写入过大数据。

## 12. 选择建议

- 普通输入直接写文本行。
- 输入一行并提交，用 `line:`。
- 明确粘贴一段文字，用 `paste:`。
- 操作 TUI、编辑器、REPL，用 `key:`。
- 高层语法表达不了时，用 `raw:` / `hex:` / `base64:` / `bytes:`。
