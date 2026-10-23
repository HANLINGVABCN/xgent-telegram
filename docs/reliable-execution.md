# 可靠执行提示词：合并说明与可验证示例

更新的是分发版 `prompts/agent_addon.txt` 的「命令与进程」，不增加运行时拦截、不更换模型、不自动覆盖自定义 prompts。选择“保留 prompts”的部署，请手动合入这几条替换规则，而不是在旧规则末尾重复粘贴。不要修改用户自己的人设和授权限制。

规则重点：简单命令直接运行；多层解释器/引号、长段嵌入代码、动态配置组装在允许写文件时必须先落脚本，等写入成功再检查和执行。首次引号/转义错误就换简单实现，不继续叠转义；检查实际副作用再重试，不能因超时就推断没有执行。只读任务不擅自创建文件。

以下是脚本文件内容示例，不是可直接执行的 `*-x` 协议块。测试使用临时目录和合成数据，不使用真实密钥。

## 1. 结构化配置：环境读取、非空校验、序列化与原子写入

需要已授权的输出路径。缺变量立即停止，不创建空配置；已有配置先备份的规则仍然适用，本示例用于新建配置。

```python
import json
import os
from pathlib import Path
import tempfile

label = os.environ.get("SERVICE_LABEL")
destination = os.environ.get("CONFIG_PATH")
if label is None or label == "" or destination is None or destination == "":
    raise SystemExit("Required configuration variables are missing; request input again")
target = Path(destination).resolve()
if target.exists():
    raise SystemExit("Target already exists; inspect and back it up before replacing")
temporary = None
try:
    with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=target.parent,
                                     prefix=".config-", delete=False) as handle:
        temporary = Path(handle.name)
        json.dump({"service": {"label": label}}, handle, ensure_ascii=False, indent=2)
        handle.flush()
        os.fsync(handle.fileno())
    # The script is for a single authorized writer; external writers require their own lock/CAS.
    os.replace(temporary, target)
    temporary = None
finally:
    if temporary is not None:
        temporary.unlink(missing_ok=True)
```

不打印变量值或把密钥放进错误信息。示例的 `SERVICE_LABEL` 可含空格、单双引号、反斜杠、中文、`$`、分号和换行，序列化不需要手工叠转义。

## 2. 特殊字符参数：参数数组，不进入 shell

```python
import os
from pathlib import Path
import subprocess
import sys

source = os.environ.get("INPUT_PATH")
destination = os.environ.get("OUTPUT_PATH")
if source is None or source == "" or destination is None or destination == "":
    raise SystemExit("Required file paths are missing; request input again")
source = Path(source).resolve()
target = Path(destination).resolve()
if not source.is_file() or target.exists() or source == target:
    raise SystemExit("Inspect source and destination before running")
subprocess.run([sys.executable, "-m", "json.tool", str(source), str(target)],
               check=True, shell=False)
```

这里调用 Python 标准库校验并格式化 JSON；路径中的空格、引号、`$` 和分号仍是一整个参数，不会执行别的命令。失败时先核对输出文件，不假设它完全没有产生副作用。

## 3. 检查、执行与短命令

在**收到脚本写入成功回传之后**，bash 可使用 `python -m py_compile /实际路径/task.py && python /实际路径/task.py`。语法错误则执行阶段不能发生；实际执行后仍需读取目标产物或验证服务响应。检查使用实际已存在的解释器，不为此安装新依赖。

`pwd`、`git status --short` 这类明确的短命令直接执行，不必先写一个只包裹它们的脚本。远程脚本在目标环境执行检查；Windows 使用该环境的退出码判断，不照搬 bash。

这些规则针对常见转义和重试错误，不保证模型永不犯错。真实准确率/耗时改善需要固定模型与任务集做前后对比；静态提示词测试和脚本通过不能冒充模型效果评测。参考 [OpenAI Prompt engineering](https://developers.openai.com/api/docs/guides/prompt-engineering) 中关于明确指令与持续评测的建议。
