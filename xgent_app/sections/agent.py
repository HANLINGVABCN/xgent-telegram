# This file is executed by xgent_server.py in the shared application namespace.
# Keep cross-section names available through the loader until the next decoupling phase.

from xgent_app.agent_context import (
    build_read_attachment_context_message,
    build_read_ranged_context_message,
    build_read_text_context_message,
)
from xgent_app.protocols import ProtocolParser
class AgentExecutor:
    """安全地执行 AI 请求的 shell 命令"""
    
    TIMEOUT = DEFAULT_AGENT_COMMAND_TIMEOUT  # 秒
    MAX_FILE_SIZE = 50 * 1024 * 1024
    WORK_DIR = os.path.dirname(os.path.abspath(__file__))
    MEDIA_INLINE_MAX_BYTES = 8 * 1024 * 1024
    TEXT_INLINE_MAX_BYTES = 512 * 1024
    # [edit] 原地替换的备份后缀（后随时间戳）
    EDIT_BACKUP_SUFFIX = '.editbak.'
    # [edit] 固定分隔标记（标记行必须顶格独占一行）
    _EDIT_OLD_MARK = '-----OLD-----'
    _EDIT_NEW_MARK = '-----NEW-----'
    # [grep] 跳过的噪声目录
    GREP_SKIP_DIRS = {'.git', 'node_modules', '__pycache__', '.venv', 'venv',
                      'dist', 'build', '.idea', '.vscode'}
    # [grep] 单次扫描文件数上限（防止误扫超大目录树）
    GREP_MAX_FILES = 20000
    TEXT_FILE_EXTENSIONS = {
        '.txt', '.md', '.markdown', '.rst', '.log', '.csv', '.tsv',
        '.json', '.jsonl', '.yaml', '.yml', '.toml', '.ini', '.cfg', '.conf',
        '.py', '.js', '.ts', '.tsx', '.jsx', '.html', '.htm', '.css', '.scss',
        '.sh', '.bash', '.ps1', '.bat', '.cmd', '.sql', '.xml', '.svg',
        '.java', '.c', '.h', '.cpp', '.hpp', '.cs', '.go', '.rs', '.php',
    }
    
    @classmethod
    def get_timeout(cls) -> int:
        return normalize_command_timeout(
            UserDataManager.get('agent_command_timeout', cls.TIMEOUT),
            cls.TIMEOUT
        )

    RAW_KEY_ALIASES: Dict[str, bytes] = {
        'enter': b'\r',
        'return': b'\r',
        'cr': b'\r',
        'ctrl-m': b'\r',
        'c-m': b'\r',
        '^m': b'\r',
        'lf': b'\n',
        'newline': b'\n',
        'linefeed': b'\n',
        'esc': b'\x1b',
        'escape': b'\x1b',
        'ctrl-[': b'\x1b',
        'c-[': b'\x1b',
        '^[': b'\x1b',
        'tab': b'\t',
        'ctrl-i': b'\t',
        'c-i': b'\t',
        '^i': b'\t',
        'ctrl-d': b'\x04',
        'c-d': b'\x04',
        '^d': b'\x04',
        'eot': b'\x04',
        'eof': b'\x04',
        'ctrl-c': b'\x03',
        'c-c': b'\x03',
        '^c': b'\x03',
        'interrupt': b'\x03',
        'sigint': b'\x03',
        'cancel': b'\x03',
        'space': b' ',
        'sp': b' ',
        'backspace': b'\x7f',
        'bs': b'\x7f',
        'rubout': b'\x7f',
        'insert': b'\x1b[2~',
        'ins': b'\x1b[2~',
        'delete': b'\x1b[3~',
        'del': b'\x1b[3~',
        'up': b'\x1b[A',
        'down': b'\x1b[B',
        'right': b'\x1b[C',
        'left': b'\x1b[D',
        'home': b'\x1b[H',
        'end': b'\x1b[F',
        'pageup': b'\x1b[5~',
        'page-up': b'\x1b[5~',
        'pgup': b'\x1b[5~',
        'pagedown': b'\x1b[6~',
        'page-down': b'\x1b[6~',
        'pgdn': b'\x1b[6~',
        'f1': b'\x1bOP',
        'f2': b'\x1bOQ',
        'f3': b'\x1bOR',
        'f4': b'\x1bOS',
        'f5': b'\x1b[15~',
        'f6': b'\x1b[17~',
        'f7': b'\x1b[18~',
        'f8': b'\x1b[19~',
        'f9': b'\x1b[20~',
        'f10': b'\x1b[21~',
        'f11': b'\x1b[23~',
        'f12': b'\x1b[24~',
        'f13': b'\x1b[25~',
        'f14': b'\x1b[26~',
        'f15': b'\x1b[28~',
        'f16': b'\x1b[29~',
        'f17': b'\x1b[31~',
        'f18': b'\x1b[32~',
        'f19': b'\x1b[33~',
        'f20': b'\x1b[34~',
        'f21': b'\x1b[1;2P',
        'f22': b'\x1b[1;2Q',
        'f23': b'\x1b[1;2R',
        'f24': b'\x1b[1;2S',
        'kp-enter': b'\r',
        'numpad-enter': b'\r',
        'kp-plus': b'+',
        'kp-minus': b'-',
        'kp-multiply': b'*',
        'kp-divide': b'/',
        'kp-decimal': b'.',
        'kp0': b'0',
        'kp1': b'1',
        'kp2': b'2',
        'kp3': b'3',
        'kp4': b'4',
        'kp5': b'5',
        'kp6': b'6',
        'kp7': b'7',
        'kp8': b'8',
        'kp9': b'9',
        'numpad0': b'0',
        'numpad1': b'1',
        'numpad2': b'2',
        'numpad3': b'3',
        'numpad4': b'4',
        'numpad5': b'5',
        'numpad6': b'6',
        'numpad7': b'7',
        'numpad8': b'8',
        'numpad9': b'9',
    }
    MACRO_MODIFIER_ALIASES = {
        'ctrl': 'ctrl',
        'control': 'ctrl',
        'alt': 'alt',
        'meta': 'alt',
        'option': 'alt',
        'shift': 'shift',
    }
    MACRO_CSI_FINAL_KEYS = {
        'up': 'A',
        'down': 'B',
        'right': 'C',
        'left': 'D',
        'home': 'H',
        'end': 'F',
    }
    MACRO_CSI_TILDE_KEYS = {
        'insert': 2,
        'ins': 2,
        'delete': 3,
        'del': 3,
        'pageup': 5,
        'page-up': 5,
        'pgup': 5,
        'pagedown': 6,
        'page-down': 6,
        'pgdn': 6,
        'f5': 15,
        'f6': 17,
        'f7': 18,
        'f8': 19,
        'f9': 20,
        'f10': 21,
        'f11': 23,
        'f12': 24,
        'f13': 25,
        'f14': 26,
        'f15': 28,
        'f16': 29,
        'f17': 31,
        'f18': 32,
        'f19': 33,
        'f20': 34,
    }
    MACRO_FKEY_FINALS = {
        'f1': 'P',
        'f2': 'Q',
        'f3': 'R',
        'f4': 'S',
    }
    MAX_STDIN_MACRO_REPEAT = 200
    MAX_STDIN_MACRO_STEPS = 1000
    MAX_STDIN_MACRO_BYTES = 1024 * 1024

    @staticmethod
    def _bytes_from_ints(values: Any) -> bytes:
        if not isinstance(values, list):
            raise ValueError("bytes payload must be a JSON list of integers")
        out = bytearray()
        for value in values:
            if isinstance(value, bool) or not isinstance(value, int):
                raise ValueError("bytes payload items must be integers")
            if value < 0 or value > 255:
                raise ValueError("bytes payload items must be between 0 and 255")
            out.append(value)
        return bytes(out)

    @classmethod
    def _key_name_to_bytes(cls, key: str) -> bytes:
        token = key.strip()
        if not token:
            return b''

        normalized = token.lower()
        alias = cls.RAW_KEY_ALIASES.get(normalized)
        if alias is not None:
            return alias

        ctrl_prefix = None
        for prefix in ('ctrl-', 'c-'):
            if normalized.startswith(prefix):
                ctrl_prefix = prefix
                break
        if ctrl_prefix:
            suffix = token[len(ctrl_prefix):]
            if len(suffix) == 1:
                char = suffix.upper()
                if '@' <= char <= '_':
                    return bytes([ord(char) & 0x1f])
                if char == '?':
                    return b'\x7f'

        if token.startswith('^') and len(token) == 2:
            char = token[1].upper()
            if '@' <= char <= '_':
                return bytes([ord(char) & 0x1f])
            if char == '?':
                return b'\x7f'

        if len(token) == 1:
            return token.encode('utf-8')

        raise ValueError(f"unknown key token: {key}")

    @classmethod
    def parse_key_sequence(cls, keys_payload: Any) -> bytes:
        if isinstance(keys_payload, list):
            tokens = keys_payload
        else:
            text = str(keys_payload or '').strip()
            if not text:
                return b''
            if text.startswith('['):
                parsed = json.loads(text)
                if not isinstance(parsed, list):
                    raise ValueError("keys JSON payload must be a list")
                tokens = parsed
            else:
                tokens = [token for token in re.split(r'[\s,]+', text) if token]

        out = bytearray()
        for token in tokens:
            if isinstance(token, bool):
                raise ValueError("key tokens must be strings or byte integers")
            if isinstance(token, int):
                out.extend(cls._bytes_from_ints([token]))
                continue
            out.extend(cls._key_name_to_bytes(str(token)))
        return bytes(out)

    @staticmethod
    def parse_escape_bytes(text: str) -> bytes:
        out = bytearray()
        i = 0
        while i < len(text):
            char = text[i]
            if char != '\\':
                out.extend(char.encode('utf-8'))
                i += 1
                continue

            if i + 1 >= len(text):
                raise ValueError("dangling backslash in raw stdin payload")

            esc = text[i + 1]
            simple = {
                '\\': b'\\',
                'r': b'\r',
                'n': b'\n',
                't': b'\t',
                'b': b'\b',
                'f': b'\f',
                'v': b'\v',
                '0': b'\x00',
                'e': b'\x1b',
                'E': b'\x1b',
            }
            if esc in simple:
                out.extend(simple[esc])
                i += 2
                continue

            if esc == 'x':
                hex_digits = text[i + 2:i + 4]
                if len(hex_digits) != 2 or not re.fullmatch(r'[0-9a-fA-F]{2}', hex_digits):
                    raise ValueError("raw stdin \\x escapes must use exactly two hex digits")
                out.append(int(hex_digits, 16))
                i += 4
                continue

            raise ValueError(f"unsupported raw stdin escape sequence: \\{esc}")

        return bytes(out)

    @staticmethod
    def _append_macro_bytes(steps: List[Dict[str, Any]], payload: bytes):
        if not payload:
            return
        if steps and steps[-1].get('type') == 'bytes' and 'pipe_payload' not in steps[-1]:
            steps[-1]['payload'] += payload
        else:
            steps.append({'type': 'bytes', 'payload': payload})

    @classmethod
    def _append_macro_step(cls, steps: List[Dict[str, Any]], step: Dict[str, Any]):
        if step.get('type') == 'bytes' and 'pipe_payload' not in step:
            cls._append_macro_bytes(steps, step.get('payload') or b'')
        else:
            steps.append(dict(step))

    @staticmethod
    def _normalize_stdin_command_value(value: str) -> str:
        return value.lstrip(' \t')

    @staticmethod
    def _normalize_stdin_text_value(value: str) -> str:
        if value[:1] in {' ', '\t'}:
            return value[1:]
        return value

    @classmethod
    def _parse_stdin_key_line(cls, value: str) -> List[Dict[str, Any]]:
        normalized_value = cls._normalize_stdin_command_value(value)
        if not normalized_value.strip():
            raise ValueError("key: 需要指定按键内容")
        if re.search(r'\[[^\]]*\]', normalized_value):
            return cls._parse_inline_stdin_macro(normalized_value)
        return [
            cls._macro_key_parts_to_step([token])
            for token in re.split(r'\s+', normalized_value.strip())
            if token
        ]

    @classmethod
    def _parse_stdin_line_mode(cls, macro: str) -> List[Dict[str, Any]]:
        text = macro or ''
        steps: List[Dict[str, Any]] = []
        line_pattern = re.compile(r'([A-Za-z][A-Za-z0-9_-]*)\s*:(.*)\Z')
        command_names = {
            'key',
            'line', 'paste',
            'wait', 'sleep', 'delay',
            'raw', 'escape', 'escaped',
            'base64', 'b64', 'hex', 'bytes', 'keys',
            'repeat',
        }

        lines = text.splitlines()
        i = 0
        while i < len(lines):
            raw_line = lines[i]
            i += 1
            if not raw_line.strip():
                continue

            escaped_line = raw_line[1:] if raw_line.startswith('\\') else ''
            escaped_match = line_pattern.match(escaped_line) if escaped_line else None
            if escaped_match and escaped_match.group(1).lower() in command_names:
                cls._append_macro_bytes(steps, escaped_line.encode('utf-8'))
                continue

            match = line_pattern.match(raw_line)
            if not match:
                cls._append_macro_bytes(steps, raw_line.encode('utf-8'))
                continue

            name = match.group(1).lower()
            value = match.group(2)
            if name == 'key':
                key_steps = cls._parse_stdin_key_line(value)
                for step in key_steps:
                    cls._append_macro_step(steps, step)
                continue
            if name in {'line', 'paste'}:
                normalized_value = cls._normalize_stdin_text_value(value)
                heredoc_match = re.fullmatch(r'<<\s*([A-Za-z0-9_-]+)', normalized_value.strip())
                if name == 'paste' and heredoc_match:
                    marker = heredoc_match.group(1)
                    block_lines: List[str] = []
                    found_marker = False
                    while i < len(lines):
                        block_line = lines[i]
                        i += 1
                        if block_line == marker:
                            found_marker = True
                            break
                        block_lines.append(block_line)
                    if not found_marker:
                        raise ValueError(f"paste heredoc 未找到结束标记: {marker}")
                    payload_text = "\n".join(block_lines)
                    if block_lines:
                        payload_text += "\n"
                    payload = payload_text.encode('utf-8')
                    cls._append_macro_bytes(steps, payload)
                    continue
                command_steps = cls._macro_command_to_steps(f'{name}:{normalized_value}')
                for step in command_steps:
                    cls._append_macro_step(steps, step)
                continue
            if name in command_names:
                normalized_value = cls._normalize_stdin_command_value(value)
                command_steps = cls._macro_command_to_steps(f'{name}:{normalized_value}')
                for step in command_steps:
                    cls._append_macro_step(steps, step)
                continue

            cls._append_macro_bytes(steps, raw_line.encode('utf-8'))

        cls._validate_stdin_steps(steps)
        return steps

    @classmethod
    def _summarize_stdin_steps(cls, steps: List[Dict[str, Any]]) -> Tuple[int, int, float]:
        byte_count = 0
        wait_seconds = 0.0
        for step in steps:
            step_type = step.get('type')
            if step_type == 'bytes':
                payload = step.get('payload') or b''
                if not isinstance(payload, (bytes, bytearray)):
                    raise ValueError("stdin bytes step payload must be bytes")
                pipe_payload = step.get('pipe_payload')
                if pipe_payload is not None and not isinstance(pipe_payload, (bytes, bytearray)):
                    raise ValueError("stdin pipe payload must be bytes")
                byte_count += max(len(payload), len(pipe_payload or b''))
            elif step_type == 'wait':
                seconds = float(step.get('seconds') or 0)
                if seconds < 0:
                    raise ValueError("stdin wait step cannot be negative")
                wait_seconds += seconds
            else:
                raise ValueError(f"unknown stdin macro step: {step_type}")
        return len(steps), byte_count, wait_seconds

    @classmethod
    def _validate_stdin_steps(cls, steps: List[Dict[str, Any]], repeat_count: int = 1):
        step_count, byte_count, wait_seconds = cls._summarize_stdin_steps(steps)
        total_steps = step_count * repeat_count
        total_bytes = byte_count * repeat_count
        total_wait_seconds = wait_seconds * repeat_count
        if total_steps > cls.MAX_STDIN_MACRO_STEPS:
            raise ValueError(f"stdin 宏步骤数不能超过 {cls.MAX_STDIN_MACRO_STEPS}")
        if total_bytes > cls.MAX_STDIN_MACRO_BYTES:
            raise ValueError(f"stdin 宏输入不能超过 {cls.MAX_STDIN_MACRO_BYTES} 字节")
        if not math.isfinite(total_wait_seconds):
            raise ValueError("stdin 宏等待时长必须是有限数字")

    @staticmethod
    def _unescape_macro_text(text: str) -> str:
        out: List[str] = []
        i = 0
        while i < len(text):
            char = text[i]
            if char == '\\' and i + 1 < len(text) and text[i + 1] in {'[', ']', '\\'}:
                out.append(text[i + 1])
                i += 2
                continue
            out.append(char)
            i += 1
        return ''.join(out)

    @staticmethod
    def _unescape_macro_brackets(text: str) -> str:
        out: List[str] = []
        i = 0
        while i < len(text):
            char = text[i]
            if char == '\\' and i + 1 < len(text) and text[i + 1] in {'[', ']'}:
                out.append(text[i + 1])
                i += 2
                continue
            out.append(char)
            i += 1
        return ''.join(out)

    @staticmethod
    def _read_macro_bracket(text: str, start: int) -> Tuple[str, int]:
        command_match = re.match(r'\s*([A-Za-z][A-Za-z0-9_-]*)\s*:', text[start + 1:])
        flat_commands = {
            'raw', 'escape', 'escaped',
            'base64', 'b64', 'hex',
            'wait', 'sleep', 'delay',
        }
        if command_match and command_match.group(1).lower() in flat_commands:
            out: List[str] = []
            i = start + 1
            while i < len(text):
                char = text[i]
                if char == '\\' and i + 1 < len(text):
                    out.append(char)
                    out.append(text[i + 1])
                    i += 2
                    continue
                if char == ']':
                    return ''.join(out), i
                out.append(char)
                i += 1
            raise ValueError("stdin 宏语法中的 [] 未闭合；若要输入字面量 [ 或 ]，请写成 \\[ 或 \\]")

        depth = 1
        out: List[str] = []
        i = start + 1
        while i < len(text):
            char = text[i]
            if char == '\\' and i + 1 < len(text):
                out.append(char)
                out.append(text[i + 1])
                i += 2
                continue
            if char == '[':
                depth += 1
                out.append(char)
                i += 1
                continue
            if char == ']':
                depth -= 1
                if depth == 0:
                    return ''.join(out), i
                out.append(char)
                i += 1
                continue
            out.append(char)
            i += 1
        raise ValueError("stdin 宏语法中的 [] 未闭合；若要输入字面量 [ 或 ]，请写成 \\[ 或 \\]")

    @staticmethod
    def _skip_macro_space(text: str, index: int) -> int:
        while index < len(text) and text[index] in ' \t\r\n':
            index += 1
        return index

    @classmethod
    def _normalize_macro_key_part(cls, part: str) -> str:
        token = cls._unescape_macro_text(part)
        stripped = token.strip()
        if stripped:
            return stripped
        if token and all(char in ' \t' for char in token):
            return 'space'
        return ''

    @staticmethod
    def _parse_macro_duration_seconds(value: str) -> float:
        text = value.strip().lower()
        if not text:
            raise ValueError("[wait:] 需要指定等待时长")
        multiplier = 0.001
        if text.endswith('ms'):
            number = text[:-2].strip()
            multiplier = 0.001
        elif text.endswith('s'):
            number = text[:-1].strip()
            multiplier = 1.0
        else:
            number = text
        try:
            seconds = float(number) * multiplier
        except ValueError as exc:
            raise ValueError(f"无法解析等待时长: {value}") from exc
        if not math.isfinite(seconds):
            raise ValueError("[wait:] 等待时长必须是有限数字")
        if seconds < 0:
            raise ValueError("[wait:] 不能使用负数")
        return seconds

    @staticmethod
    def _modifier_code(modifiers: set) -> int:
        code = 1
        if 'shift' in modifiers:
            code += 1
        if 'alt' in modifiers:
            code += 2
        if 'ctrl' in modifiers:
            code += 4
        return code

    @staticmethod
    def _ctrl_char_to_bytes(key: str) -> bytes:
        token = key.strip()
        lowered = token.lower()
        aliases = {
            'space': ' ',
            'sp': ' ',
            'leftbracket': '[',
            'rightbracket': ']',
            'backslash': '\\',
            'slash': '/',
            'question': '?',
        }
        token = aliases.get(lowered, token)
        if len(token) != 1:
            raise ValueError(f"Ctrl 组合键需要单字符或可识别按键: {key}")
        char = token.upper()
        if char == '?':
            return b'\x7f'
        if char == ' ':
            return b'\x00'
        if '@' <= char <= '_':
            return bytes([ord(char) & 0x1f])
        raise ValueError(f"无法转换 Ctrl 组合键: {key}")

    @classmethod
    def _modified_key_to_bytes(cls, modifiers: set, key: str) -> bytes:
        normalized = key.strip().lower()
        if not normalized:
            return b''

        if not modifiers:
            return cls._key_name_to_bytes(key)

        if normalized in cls.MACRO_CSI_FINAL_KEYS:
            code = cls._modifier_code(modifiers)
            return f"\x1b[1;{code}{cls.MACRO_CSI_FINAL_KEYS[normalized]}".encode('ascii')

        if normalized in cls.MACRO_CSI_TILDE_KEYS:
            code = cls._modifier_code(modifiers)
            number = cls.MACRO_CSI_TILDE_KEYS[normalized]
            return f"\x1b[{number};{code}~".encode('ascii')

        if normalized in cls.MACRO_FKEY_FINALS:
            code = cls._modifier_code(modifiers)
            return f"\x1b[1;{code}{cls.MACRO_FKEY_FINALS[normalized]}".encode('ascii')

        if normalized == 'tab' and modifiers == {'shift'}:
            return b'\x1b[Z'

        if 'ctrl' in modifiers:
            payload = cls._ctrl_char_to_bytes(key)
        elif 'shift' in modifiers and len(key) == 1:
            payload = key.upper().encode('utf-8')
        else:
            payload = cls._key_name_to_bytes(key)

        if 'alt' in modifiers:
            payload = b'\x1b' + payload
        return payload

    @classmethod
    def _macro_key_parts_to_bytes(cls, parts: List[str]) -> bytes:
        if not parts:
            return b''
        if len(parts) == 1:
            token = cls._normalize_macro_key_part(parts[0])
            if '+' in token:
                return cls._macro_key_parts_to_bytes([part for part in re.split(r'\s*\+\s*', token) if part])
            return cls._key_name_to_bytes(token)

        modifiers = set()
        key_parts: List[str] = []
        tokens = [cls._normalize_macro_key_part(part) for part in parts]
        tokens = [token for token in tokens if token]
        for token in tokens:
            lowered = token.lower()
            modifier = cls.MACRO_MODIFIER_ALIASES.get(lowered)
            if modifier:
                modifiers.add(modifier)
            else:
                key_parts.append(token)
        if not key_parts:
            return b''
        if len(key_parts) > 1:
            raise ValueError(
                f"组合键 {'+'.join(tokens)} 表示同一拍按下多个普通键 ({'+'.join(key_parts)})；"
                "终端 stdin 只能编码“修饰键 + 一个主键”，不能表达多个普通主键同时按下。"
                "如果要按顺序发送多个键，请用空格分隔，例如 key: ctrl+a c"
            )
        return cls._modified_key_to_bytes(modifiers, key_parts[0])

    @classmethod
    def _macro_key_parts_to_step(cls, parts: List[str]) -> Dict[str, Any]:
        payload = cls._macro_key_parts_to_bytes(parts)
        step: Dict[str, Any] = {'type': 'bytes', 'payload': payload}
        if len(parts) == 1:
            token = cls._normalize_macro_key_part(parts[0]).lower()
            if token in {'enter', 'return'}:
                step['pipe_payload'] = b'\n'
        return step

    @classmethod
    def _macro_command_to_steps(cls, content: str) -> List[Dict[str, Any]]:
        leading_trimmed = content.lstrip()
        command_match = re.match(r'([A-Za-z][A-Za-z0-9_-]*)\s*:(.*)\Z', leading_trimmed, re.DOTALL)
        if not command_match:
            return [cls._macro_key_parts_to_step([content])]

        name = command_match.group(1).lower()
        value = command_match.group(2)

        if name == 'paste':
            return [{'type': 'bytes', 'payload': cls._unescape_macro_text(value).encode('utf-8')}]
        if name == 'line':
            payload = cls._unescape_macro_text(value).encode('utf-8')
            return [{'type': 'bytes', 'payload': payload + b'\r', 'pipe_payload': payload + b'\n'}]
        if name in {'raw', 'escape', 'escaped'}:
            return [{'type': 'bytes', 'payload': cls.parse_escape_bytes(cls._unescape_macro_brackets(value))}]
        if name in {'base64', 'b64'}:
            data = ''.join(cls._unescape_macro_brackets(value).split())
            return [{'type': 'bytes', 'payload': base64.b64decode(data, validate=True)}]
        if name == 'hex':
            data = cls._unescape_macro_brackets(value).replace(',', ' ').replace('0x', '').replace('0X', '')
            return [{'type': 'bytes', 'payload': bytes.fromhex(data)}]
        if name == 'bytes':
            data = cls._unescape_macro_brackets(value).strip()
            if data.startswith('['):
                payload = cls._bytes_from_ints(json.loads(data))
            else:
                values = [int(item, 0) for item in re.split(r'[\s,]+', data) if item]
                payload = cls._bytes_from_ints(values)
            return [{'type': 'bytes', 'payload': payload}]
        if name == 'keys':
            return [{'type': 'bytes', 'payload': cls.parse_key_sequence(cls._unescape_macro_brackets(value))}]
        if name in {'wait', 'sleep', 'delay'}:
            return [{'type': 'wait', 'seconds': cls._parse_macro_duration_seconds(value)}]
        if name == 'repeat':
            repeat_match = re.match(r'\s*(\d+)\s*(?::|\s)\s*(.*?)\s*\Z', value, re.DOTALL)
            if not repeat_match:
                raise ValueError("[repeat:] 格式应为 [repeat:次数 内容]，例如 [repeat:3 [up]]")
            count = int(repeat_match.group(1))
            if count < 0 or count > cls.MAX_STDIN_MACRO_REPEAT:
                raise ValueError(f"[repeat:] 次数必须在 0 到 {cls.MAX_STDIN_MACRO_REPEAT} 之间")
            nested_steps = cls._parse_inline_stdin_macro(repeat_match.group(2))
            cls._validate_stdin_steps(nested_steps, count)
            out: List[Dict[str, Any]] = []
            for _ in range(count):
                for step in nested_steps:
                    cls._append_macro_step(out, step)
            return out

        return [cls._macro_key_parts_to_step([content])]

    @staticmethod
    def _parse_macro_postfix_repeat(text: str, index: int) -> Tuple[int, int]:
        start = AgentExecutor._skip_macro_space(text, index)
        if start >= len(text) or text[start] != '*':
            return 1, index
        i = AgentExecutor._skip_macro_space(text, start + 1)
        begin = i
        while i < len(text) and text[i].isdigit():
            i += 1
        if begin == i:
            return 1, index
        count = int(text[begin:i])
        if count < 0 or count > AgentExecutor.MAX_STDIN_MACRO_REPEAT:
            raise ValueError(f"重复次数必须在 0 到 {AgentExecutor.MAX_STDIN_MACRO_REPEAT} 之间")
        return count, i

    @classmethod
    def _parse_inline_stdin_macro(cls, macro: str) -> List[Dict[str, Any]]:
        text = macro or ''
        steps: List[Dict[str, Any]] = []
        text_buf: List[str] = []
        i = 0

        def flush_text(strip_right: bool = False):
            if not text_buf:
                return
            payload_text = ''.join(text_buf)
            if strip_right:
                payload_text = payload_text.rstrip(' \t\r\n')
            text_buf.clear()
            if payload_text:
                cls._append_macro_bytes(steps, payload_text.encode('utf-8'))

        while i < len(text):
            char = text[i]
            if char == '\\' and i + 1 < len(text) and text[i + 1] in {'[', ']', '\\'}:
                text_buf.append(text[i + 1])
                i += 2
                continue
            if char != '[':
                text_buf.append(char)
                i += 1
                continue

            content, end_index = cls._read_macro_bracket(text, i)
            parts = [content]
            cursor = end_index + 1
            while True:
                plus_index = cls._skip_macro_space(text, cursor)
                if plus_index >= len(text) or text[plus_index] != '+':
                    break
                next_index = cls._skip_macro_space(text, plus_index + 1)
                if next_index >= len(text) or text[next_index] != '[':
                    break
                next_content, next_end = cls._read_macro_bracket(text, next_index)
                parts.append(next_content)
                cursor = next_end + 1

            repeat_count, after_repeat = cls._parse_macro_postfix_repeat(text, cursor)
            flush_text(strip_right=True)
            if len(parts) > 1:
                part_steps = [cls._macro_key_parts_to_step(parts)]
            else:
                part_steps = cls._macro_command_to_steps(parts[0])
            for _ in range(repeat_count):
                for step in part_steps:
                    cls._append_macro_step(steps, step)
            i = cls._skip_macro_space(text, after_repeat)

        flush_text(strip_right=False)
        cls._validate_stdin_steps(steps)
        return steps

    @classmethod
    def parse_stdin_macro(cls, macro: str) -> List[Dict[str, Any]]:
        return cls._parse_stdin_line_mode(macro or '')

    @classmethod
    def extract_protocol_blocks(cls, ai_response: str) -> List[Dict[str, Any]]:
        """兼容入口：协议解析委托给纯解析模块。"""
        return ProtocolParser.extract_protocol_blocks(ai_response)

    @classmethod
    def strip_protocol_blocks(cls, ai_response: str) -> str:
        """兼容入口：只移除协议块，不参与执行或消息发送。"""
        return ProtocolParser.strip_protocol_blocks(ai_response)
    @classmethod
    def resolve_file_path(cls, requested_path: str) -> str:
        """将文件路径解析为服务器上的实际路径。仅接受绝对路径。"""
        cleaned = requested_path.strip()
        if not cleaned:
            raise ValueError("文件路径为空")
        if not os.path.isabs(cleaned):
            raise ValueError(f"路径必须是绝对路径（以 / 开头），收到: {cleaned}。请使用项目根目录等绝对路径。")
        return os.path.abspath(cleaned)

    @classmethod
    def resolve_write_path(cls, requested_path: str) -> str:
        """写文件用的路径解析。file 协议必须明确提供绝对路径。"""
        cleaned = requested_path.strip()
        default_workspace = to_display_path(os.path.join(cls.WORK_DIR, 'workspace'))
        if not cleaned:
            raise ValueError(
                "file 协议必须指定目标文件的绝对路径；"
                f"未指定保存位置时，请使用 {default_workspace}/文件名"
            )
        if not os.path.isabs(cleaned):
            raise ValueError(
                f"file 协议路径必须是绝对路径，收到: {cleaned}；"
                f"未指定保存位置时，请使用 {default_workspace}/文件名"
            )
        return os.path.abspath(cleaned)

    @classmethod
    async def write_file(cls, requested_path: str, content: str) -> Dict[str, Any]:
        """将文件真实写入服务器。"""
        target_path = cls.resolve_write_path(requested_path)
        existed = os.path.exists(target_path)
        byte_size = len(content.encode('utf-8'))

        def _write():
            parent = os.path.dirname(target_path)
            if parent:
                os.makedirs(parent, exist_ok=True)
            with open(target_path, 'w', encoding='utf-8') as f:
                f.write(content)

        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, _write)
        return {
            'path': target_path,
            'existed': existed,
            'bytes': byte_size,
        }

    @classmethod
    async def edit_file(cls, edit_body: str) -> Dict[str, Any]:
        """字符串级原地替换：把文件中唯一存在的 old_str 换成 new_str。

        body 格式（标记行必须顶格独占一行，内容区可含任意字符）：
            /path/to/file
            -----OLD-----
            旧字符串（原样，可多行）
            -----NEW-----
            新字符串（原样，可多行）

        若 old_str 或 new_str 恰好含有分隔标记行，可在路径后追加自定义标记：
            /path/to/file
            <<MARKER_OLD
            ...
            >>MARKER_NEW
            ...
        返回 dict 含 success/path/backed_up/matches/line_range 等。
        """
        old_str, new_str, requested_path, parse_err = cls._parse_edit_body(edit_body)
        if parse_err:
            return {'success': False, 'error': parse_err, 'output': parse_err}

        target_path = cls.resolve_file_path(requested_path)
        if not os.path.exists(target_path):
            msg = f"文件不存在: {to_display_path(target_path)}"
            return {'success': False, 'error': msg, 'output': msg}

        mime_type, _ = mimetypes.guess_type(target_path)
        if not cls.is_text_file(target_path, mime_type or 'application/octet-stream'):
            msg = f"非文本文件，拒绝原地编辑: {to_display_path(target_path)}"
            return {'success': False, 'error': msg, 'output': msg}

        def _read() -> str:
            with open(target_path, 'r', encoding='utf-8') as f:
                return f.read()

        loop = asyncio.get_running_loop()
        try:
            content = await loop.run_in_executor(None, _read)
        except UnicodeDecodeError:
            msg = f"文件非 UTF-8，无法安全编辑: {to_display_path(target_path)}"
            return {'success': False, 'error': msg, 'output': msg}

        if old_str == new_str:
            msg = "old 与 new 完全相同，无需替换。"
            return {'success': False, 'error': msg, 'output': msg}

        if old_str == '':
            msg = "old 为空，禁止空串替换（易误伤）。若需插入请带上周边上下文。"
            return {'success': False, 'error': msg, 'output': msg}

        match_count = content.count(old_str)
        if match_count == 0:
            # 给出文件里与 old 最相近的若干行，帮 AI 自校对
            hint = cls._nearest_lines_hint(content, old_str)
            msg = (
                f"未找到该字符串。可能是缩进/空白/换行不一致，或文件已被改动。\n"
                f"建议：重新 grep 拿行号 → read 带行号确认 → 再 edit。\n{hint}"
            )
            return {'success': False, 'error': msg, 'output': msg}
        if match_count > 1:
            # 列出每个命中所在行号，引导 AI 扩大上下文
            line_nos = cls._line_numbers_of(content, old_str)
            msg = (
                f"匹配到 {match_count} 处，不唯一（命中起始行: {line_nos}）。"
                f"请在 old 串里多带几行上下文，使其在文件中唯一。"
            )
            return {'success': False, 'error': msg, 'output': msg}

        # 命中唯一，执行替换 + 备份
        new_content = content.replace(old_str, new_str, 1)

        # 计算命中行号区间（供回执展示，也便于 AI 后续 read 复核）
        before_lines = content.split('\n')
        old_line_count = old_str.count('\n') + 1
        old_first_line = cls._line_numbers_of(content, old_str)[0]
        old_last_line = old_first_line + old_line_count - 1

        def _backup_and_write() -> str:
            backup_path = target_path + cls.EDIT_BACKUP_SUFFIX + time.strftime(
                '%Y%m%d_%H%M%S')
            try:
                shutil.copy2(target_path, backup_path)
            except Exception:
                backup_path = ''
            with open(target_path, 'w', encoding='utf-8') as f:
                f.write(new_content)
            return backup_path

        try:
            backup_path = await loop.run_in_executor(None, _backup_and_write)
        except Exception as e:
            msg = f"写入失败: {str(e)[:200]}"
            return {'success': False, 'error': msg, 'output': msg}

        display_path = to_display_path(target_path)
        delta = len(new_content) - len(content)
        notice = (
            f"✅ 已替换 1 处（第 {old_first_line}-{old_last_line} 行）。"
            f"文件: {display_path}，字节变化 {'+' if delta >= 0 else ''}{delta}。"
        )
        if backup_path:
            notice += f" 备份: {to_display_path(backup_path)}。"
        else:
            notice += " 备份失败（内容已写入）。"
        return {
            'success': True,
            'path': target_path,
            'display_path': display_path,
            'backup_path': backup_path,
            'matches': 1,
            'line_range': (old_first_line, old_last_line),
            'byte_delta': delta,
            'new_size': len(new_content.encode('utf-8')),
            'output': notice,
            'notice': notice,
        }

    @classmethod
    def _parse_edit_body(cls, body: str) -> Tuple[str, str, str, str]:
        """解析 edit 块 body，返回 (old_str, new_str, path, err)。

        格式（固定分隔标记，标记行必须顶格独占一行）：
            /path
            -----OLD-----
            旧串（原样，可多行）
            -----NEW-----
            新串（原样，可多行）
        若旧/新串本身含分隔标记行（极罕见），AI 会收到"未找到"反馈，
        自然会换一段上下文重试，无需自定义标记。
        """
        body = body.replace('\r\n', '\n')
        lines = body.split('\n')
        if not lines or not lines[0].strip():
            return '', '', '', "edit 块第一行必须是文件路径。"
        path_line = lines[0].strip()
        rest_lines = lines[1:]

        if cls._EDIT_OLD_MARK not in rest_lines:
            return '', '', '', (
                f"edit 块缺少分隔标记行 '{cls._EDIT_OLD_MARK}'。"
                f"格式：第一行路径，随后 '{cls._EDIT_OLD_MARK}'，旧串，"
                f"再 '{cls._EDIT_NEW_MARK}'，新串。"
            )
        if cls._EDIT_NEW_MARK not in rest_lines:
            return '', '', '', (
                f"edit 块缺少分隔标记行 '{cls._EDIT_NEW_MARK}'（在 OLD 段之后）。"
            )
        idx_old = rest_lines.index(cls._EDIT_OLD_MARK)
        idx_new = rest_lines.index(cls._EDIT_NEW_MARK)
        if idx_new <= idx_old:
            return '', '', '', (
                f"标记顺序错误：'{cls._EDIT_NEW_MARK}' 必须出现在 '{cls._EDIT_OLD_MARK}' 之后。"
            )
        old_str = '\n'.join(rest_lines[idx_old + 1: idx_new])
        new_str = '\n'.join(rest_lines[idx_new + 1:])
        return old_str, new_str, path_line, ''

    @staticmethod
    def _line_numbers_of(content: str, needle: str) -> List[int]:
        """返回 needle 在 content 中每次命中的起始行号（1-based）。"""
        if not needle:
            return []
        results: List[int] = []
        search_from = 0
        while True:
            pos = content.find(needle, search_from)
            if pos == -1:
                break
            line_no = content.count('\n', 0, pos) + 1
            results.append(line_no)
            search_from = pos + 1
        return results

    @staticmethod
    def _nearest_lines_hint(content: str, old_str: str, top_k: int = 5) -> str:
        """未命中时，找 old_str 首行在文件中最接近的若干行，帮 AI 定位差异。"""
        first_line = old_str.split('\n', 1)[0].strip()
        if not first_line or len(first_line) < 3:
            return ''
        token = first_line[:40]
        candidates = []
        for ln, line in enumerate(content.split('\n'), start=1):
            if token in line:
                candidates.append((ln, line.rstrip()[:80]))
        if not candidates:
            return ''
        sample = candidates[:top_k]
        lines_str = '\n'.join(f"  L{n}: {t}" for n, t in sample)
        return f"文件中含 '{token}' 的行（可能就是你要改的位置，请核对空白/缩进）:\n{lines_str}"

    @classmethod
    async def grep_search(cls, grep_body: str) -> Dict[str, Any]:
        """结构化检索：返回 文件+行号+命中行+上下文，一次拿全。

        body 格式（首行是 pattern，其余是 key: value 选项）：
            关键字或正则
            path: .              # 目录或文件，默认工作区根
            -i                   # 忽略大小写
            -r                   # 正则模式（默认按字面量）
            -n: 3                # 上下文行数（前后各 N 行），默认 0
            -m: 50               # 最多命中行数，默认 50
            glob: *.py           # 文件名过滤
        """
        opts = cls._parse_grep_body(grep_body)
        if opts.get('error'):
            return {'success': False, 'error': opts['error'], 'output': opts['error']}

        pattern = opts['pattern']
        regex = opts['regex']
        ignore_case = opts['ignore_case']
        context_n = opts['context_n']
        max_hits = opts['max_hits']
        glob_pat = opts['glob']
        search_path = cls.resolve_file_path(opts['path'])

        if not os.path.exists(search_path):
            msg = f"检索路径不存在: {to_display_path(search_path)}"
            return {'success': False, 'error': msg, 'output': msg}

        flags = 0
        if ignore_case:
            flags |= re.IGNORECASE
        try:
            if regex:
                compiled = re.compile(pattern, flags)
                matcher = lambda s: compiled.search(s) is not None
            else:
                # 字面量：转义
                compiled_lit = re.compile(re.escape(pattern), flags)
                matcher = lambda s: compiled_lit.search(s) is not None
        except re.error as e:
            msg = f"正则编译失败: {str(e)[:150]}"
            return {'success': False, 'error': msg, 'output': msg}

        # 收集目标文件列表
        if os.path.isfile(search_path):
            files = [search_path]
        else:
            files = []
            for root, dirs, fnames in os.walk(search_path):
                # 跳过常见噪声目录
                dirs[:] = [d for d in dirs if d not in cls.GREP_SKIP_DIRS]
                for fn in fnames:
                    if glob_pat and not fnmatch.fnmatch(fn, glob_pat):
                        continue
                    files.append(os.path.join(root, fn))
                if len(files) > cls.GREP_MAX_FILES:
                    msg = (
                        f"命中文件过多（>{cls.GREP_MAX_FILES}），请缩小 path 或加 glob 过滤。"
                    )
                    return {'success': False, 'error': msg, 'output': msg}

        results: List[Dict[str, Any]] = []
        total_hits = 0
        truncated = False
        loop = asyncio.get_running_loop()

        def _scan():
            nonlocal total_hits, truncated
            for fpath in files:
                if truncated:
                    break
                try:
                    with open(fpath, 'r', encoding='utf-8', errors='replace') as f:
                        all_lines = f.readlines()
                except (OSError, UnicodeDecodeError):
                    continue
                # 统一去行尾换行用于匹配，但展示保留原样
                stripped = [l.rstrip('\n').rstrip('\r') for l in all_lines]
                for ln, line in enumerate(stripped, start=1):
                    if matcher(line):
                        if total_hits >= max_hits:
                            truncated = True
                            break
                        ctx_before = stripped[max(0, ln - 1 - context_n): ln - 1]
                        ctx_after = stripped[ln: ln + context_n]
                        results.append({
                            'path': fpath,
                            'line': ln,
                            'match': line[:500],
                            'before': list(enumerate(ctx_before, start=max(1, ln - context_n))),
                            'after': list(enumerate(ctx_after, start=ln + 1)),
                        })
                        total_hits += 1

        await loop.run_in_executor(None, _scan)

        display_path = to_display_path(search_path)
        mode = 'regex' if regex else 'literal'
        header = (
            f"[grep结果] pattern='{pattern}' mode={mode} path={display_path} "
            f"context=±{context_n} 命中={total_hits}"
            f"{' (已截断，调大 -m)' if truncated else ''}"
        )

        if not results:
            body_text = header + "\n无命中。可尝试：换关键字 / 去掉 glob / -i 忽略大小写 / -r 正则。"
            return {
                'success': True,
                'hits': 0,
                'output': body_text,
                'notice': body_text,
                'message': {'role': 'user', 'content': body_text},
            }

        # 渲染：路径 + 行号 + 命中行 + 上下文（带行号，便于后续 read/edit 对齐）
        parts = [header]
        # 按文件分组，路径只打印一次
        last_path = None
        for r in results:
            rp = to_display_path(r['path'])
            if rp != last_path:
                parts.append(f"\n📄 {rp}")
                last_path = rp
            for n, t in r['before']:
                parts.append(f"  {n:>6}\t{t[:200]}")
            parts.append(f"▶ {r['line']:>6}\t{r['match']}")
            for n, t in r['after']:
                parts.append(f"  {n:>6}\t{t[:200]}")
        body_text = '\n'.join(parts)
        return {
            'success': True,
            'hits': total_hits,
            'truncated': truncated,
            'output': body_text,
            'notice': body_text,
            'message': {'role': 'user', 'content': body_text},
        }

    @staticmethod
    def _parse_grep_body(body: str) -> Dict[str, Any]:
        body = body.replace('\r\n', '\n')
        lines = body.split('\n')
        if not lines or not lines[0].strip():
            return {'error': 'grep 块第一行必须是搜索 pattern。'}
        pattern = lines[0]
        opts: Dict[str, Any] = {
            'pattern': pattern,
            'path': '.',
            'regex': False,
            'ignore_case': False,
            'context_n': 0,
            'max_hits': 50,
            'glob': '',
        }
        for raw in lines[1:]:
            line = raw.strip()
            if not line:
                continue
            if line == '-i':
                opts['ignore_case'] = True
            elif line == '-r':
                opts['regex'] = True
            elif line.startswith('-n:'):
                try:
                    opts['context_n'] = max(0, int(line[3:].strip()))
                except ValueError:
                    return {'error': f"-n 需要整数: {line}"}
            elif line.startswith('-m:'):
                try:
                    opts['max_hits'] = max(1, int(line[3:].strip()))
                except ValueError:
                    return {'error': f"-m 需要整数: {line}"}
            elif line.startswith('path:'):
                opts['path'] = line[5:].strip()
            elif line.startswith('glob:'):
                opts['glob'] = line[5:].strip()
            # 其余未知行忽略，容错
        return opts

    @classmethod
    async def read_file_ranged(cls, requested: str) -> Dict[str, Any]:
        """带行号的文本读取，支持 path[:START-END] 或 path[:START:+COUNT]。

        - 无区间：读全文（受 TEXT_INLINE_MAX_BYTES 限制），输出 cat -n 格式。
        - START-END：读 [START, END] 闭区间。
        - START:+COUNT：从 START 行起读 COUNT 行。
        - START-：从 START 行读到文件尾。
        """
        path_part, range_part = cls._split_read_range(requested)
        target_path = cls.resolve_file_path(path_part)
        if not os.path.exists(target_path):
            raise FileNotFoundError(f"文件不存在: {target_path}")

        file_size = os.path.getsize(target_path)
        mime_type, _ = mimetypes.guess_type(target_path)
        mime_type = mime_type or 'application/octet-stream'
        basename = os.path.basename(target_path) or 'unnamed_file'
        display_path = to_display_path(target_path)

        if not cls.is_text_file(target_path, mime_type):
            # 非文本走原 read_path_for_model 逻辑（图片/二进制等）
            return await cls.read_path_for_model(path_part, api_format='openai')

        def _read_full() -> str:
            with open(target_path, 'r', encoding='utf-8', errors='replace') as f:
                return f.read()

        loop = asyncio.get_running_loop()
        full_text = await loop.run_in_executor(None, _read_full)
        all_lines = full_text.split('\n')
        total_lines = len(all_lines)

        start, end = cls._resolve_range(range_part, total_lines)
        # 区间读取不校验总大小（只取片段）；全量读取仍校验
        sliced = all_lines[start - 1:end]
        # 行号格式：6位右对齐 + tab + 内容（cat -n 风格，和 grep 输出对齐）
        numbered = []
        for offset, text in enumerate(sliced):
            ln = start + offset
            numbered.append(f"{ln:>6}\t{text}")

        body_text = '\n'.join(numbered)
        if start > 1 or end < total_lines:
            scope = f"第 {start}-{end} 行（共 {total_lines} 行）"
        else:
            scope = f"全文（共 {total_lines} 行）"
        notice = f"[read结果] 已读取 {display_path}，{scope}"
        return {
            'notice': notice,
            'message': build_read_ranged_context_message(
                notice, mime_type, basename, body_text
            ),
            'start': start,
            'end': end,
            'total_lines': total_lines,
        }

    @staticmethod
    def _split_read_range(requested: str) -> Tuple[str, str]:
        """从 read 块的 path 字段切出 真实路径 和 区间串。

        支持形如 C:\\path\\file.py:10-20 的 Windows 路径：
        只把最后一个符合区间语法的冒号后缀当作区间。
        """
        requested = requested.strip()
        # 区间语法特征：冒号后是 数字 / 数字- / 数字-N / 数字:+N
        m = re.search(r':(?P<rng>(\d+(-\d*)?|(\d+:\+\d+)))\s*$', requested)
        if m:
            return requested[:m.start()].strip(), m.group('rng')
        return requested, ''

    @staticmethod
    def _resolve_range(range_part: str, total_lines: int) -> Tuple[int, int]:
        if not range_part:
            return 1, total_lines
        # START:+COUNT
        m = re.match(r'^(\d+):\+(\d+)$', range_part)
        if m:
            start = max(1, int(m.group(1)))
            count = max(1, int(m.group(2)))
            return start, min(total_lines, start + count - 1)
        # START-END 或 START-
        m = re.match(r'^(\d+)-(\d*)$', range_part)
        if m:
            start = max(1, int(m.group(1)))
            end_s = m.group(2)
            end = int(end_s) if end_s else total_lines
            return start, max(start, min(total_lines, end))
        # 单个行号 START
        m = re.match(r'^(\d+)$', range_part)
        if m:
            start = max(1, int(m.group(1)))
            return start, start
        return 1, total_lines

    @classmethod
    def is_text_file(cls, path: str, mime_type: str) -> bool:
        ext = os.path.splitext(path)[1].lower()
        if ext in cls.TEXT_FILE_EXTENSIONS:
            return True
        return mime_type.startswith('text/') or mime_type in {
            'application/json',
            'application/xml',
            'application/javascript',
            'application/x-sh',
            'application/x-yaml',
        }

    @staticmethod
    def decode_text_file(content: bytes) -> Optional[str]:
        if b'\x00' in content[:4096]:
            return None

        for encoding in ('utf-8-sig', 'utf-8', 'gbk'):
            try:
                text = content.decode(encoding)
            except UnicodeDecodeError:
                continue

            sample = text[:4096]
            if sample:
                controls = sum(
                    1 for ch in sample
                    if ord(ch) < 32 and ch not in '\r\n\t\f\b'
                )
                if controls / len(sample) > 0.05:
                    return None
            return text
        return None

    @classmethod
    async def read_path_for_model(cls, requested_path: str, api_format: str = 'openai') -> Dict[str, Any]:
        """按路径读取文件，并把原文件本体直接构造成回灌消息。"""
        # 安全分离可能的行号区间后缀，防止把区间误当文件名
        path_part, _ = cls._split_read_range(requested_path)
        target_path = cls.resolve_file_path(path_part)
        if not os.path.exists(target_path):
            raise FileNotFoundError(f"文件不存在: {target_path}")

        file_size = os.path.getsize(target_path)
        mime_type, _ = mimetypes.guess_type(target_path)
        mime_type = mime_type or 'application/octet-stream'
        basename = os.path.basename(target_path) or 'unnamed_file'
        display_path = to_display_path(target_path)
        notice = f"[read结果] 已按路径读取 {display_path}"
        if file_size > cls.MEDIA_INLINE_MAX_BYTES:
            raise ValueError(
                f"文件过大，当前不能把文件本体直接交给 AI: {display_path} ({file_size} bytes)"
            )

        loop = asyncio.get_running_loop()
        raw_content = await loop.run_in_executor(None, lambda: open(target_path, 'rb').read())

        looks_like_text = cls.is_text_file(target_path, mime_type)
        text_content: Optional[str] = None
        if file_size <= cls.TEXT_INLINE_MAX_BYTES:
            text_content = cls.decode_text_file(raw_content)

        if looks_like_text or text_content is not None:
            if file_size > cls.TEXT_INLINE_MAX_BYTES:
                raise ValueError(
                    f"文本文件过大，不能一次性完整塞进上下文: {display_path} ({file_size} bytes)。"
                    "请改用 shell 配合 sed/head/tail 分段查看。"
                )
            if text_content is None:
                raise ValueError(f"文件看起来是文本类型，但无法稳定解码: {display_path}")

            text_notice = notice + f"（完整文本本体，{file_size} bytes）"
            return {
                'notice': text_notice,
                'message': build_read_text_context_message(
                    text_notice, mime_type, basename, text_content
                ),
            }

        data_b64 = base64.b64encode(raw_content).decode('ascii')

        if mime_type.startswith('image/'):
            return {
                'notice': notice + f"（图片本体，{file_size} bytes）",
                'message': build_read_attachment_context_message(
                    notice,
                    mime_type,
                    basename,
                    data_b64,
                    attachment_type='image',
                ),
            }

        if api_format in {'gemini', 'vertex'}:
            return {
                'notice': notice + f"（文件本体，{mime_type}，{file_size} bytes）",
                'message': build_read_attachment_context_message(
                    notice,
                    mime_type,
                    basename,
                    data_b64,
                    attachment_type='binary',
                ),
            }

        raise ValueError(
            f"当前接入层不支持把 {mime_type} 文件本体直接交给当前模型；"
            "当前仅图片可稳定直传，其他文件本体请改用支持原生文件输入的模型通道。"
        )
    
    @classmethod
    def extract_file_block(cls, ai_response: str) -> Optional[Tuple[str, str]]:
        """从 AI 回复中提取 file-x 文件块（创建新文件）"""
        for block in cls.extract_protocol_blocks(ai_response):
            if block['type'] == 'file':
                return block['path'], block['body']
        return None

    @classmethod
    def extract_sendfile(cls, ai_response: str) -> Optional[str]:
        """从 AI 回复中提取 sendfile-x 块（发送已有服务器文件）"""
        for block in cls.extract_protocol_blocks(ai_response):
            if block['type'] == 'sendfile':
                return block['body']
        return None

    @classmethod
    def extract_media_prompt(cls, ai_response: str) -> Optional[str]:
        """从 AI 回复中提取 media-x 媒体生成提示词块"""
        for block in cls.extract_protocol_blocks(ai_response):
            if block['type'] == 'media':
                prompt = block['body'].strip()
                return prompt or None
        return None

    @classmethod
    async def run_command(cls, command: str,
                          stop_event: Optional[asyncio.Event] = None) -> Dict[str, Any]:
        """执行一次性命令，等待结束并保存完整输出。"""
        command = command.strip()
        if not command:
            return {'success': False, 'output': 'run 命令为空', 'return_code': -1}

        blocked, pattern = AgentCommandBlacklist.check(command)
        if blocked:
            # 命中的规则只写日志，不回灌给模型：告诉它具体匹配了哪一条，
            # 等于直接指导它改写命令来绕过。
            logger.warning(f"run 命令被黑名单拦截，命中规则: {pattern} | 命令: {command[:200]}")
            return {
                'success': False,
                'output': BLACKLIST_BLOCKED_NOTICE,
                'return_code': -1,
            }

        timeout = cls.get_timeout()
        process = None
        started_at = time.monotonic()
        try:
            kwargs: Dict[str, Any] = {}
            if os.name != 'nt':
                kwargs['start_new_session'] = True
            else:
                kwargs['creationflags'] = getattr(subprocess, 'CREATE_NEW_PROCESS_GROUP', 0)

            process = await asyncio.create_subprocess_shell(
                command,
                cwd=cls.WORK_DIR,
                env={**os.environ, 'LANG': 'en_US.UTF-8'},
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                **kwargs
            )

            communicate_task = asyncio.create_task(process.communicate())
            stop_task = asyncio.create_task(stop_event.wait()) if stop_event else None
            timeout_task = asyncio.create_task(asyncio.sleep(timeout))
            wait_tasks = {communicate_task, timeout_task}
            if stop_task:
                wait_tasks.add(stop_task)

            done, pending = await asyncio.wait(wait_tasks, return_when=asyncio.FIRST_COMPLETED)

            stopped = bool(stop_task and stop_task in done and stop_event and stop_event.is_set())
            timed_out = timeout_task in done
            if stopped or timed_out:
                await terminate_async_process(process)

            for task in (timeout_task, stop_task):
                if task and not task.done():
                    task.cancel()
                    with contextlib.suppress(asyncio.CancelledError):
                        await task

            stdout = b''
            stderr = b''
            if communicate_task.done():
                with contextlib.suppress(Exception):
                    stdout, stderr = communicate_task.result()
            elif stopped or timed_out:
                with contextlib.suppress(Exception):
                    stdout, stderr = await asyncio.wait_for(communicate_task, timeout=2)
                if not communicate_task.done():
                    communicate_task.cancel()
                    with contextlib.suppress(asyncio.CancelledError):
                        await communicate_task
            else:
                communicate_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await communicate_task

            stdout_text = stdout.decode('utf-8', errors='replace') if isinstance(stdout, (bytes, bytearray)) else (stdout or '')
            stderr_text = stderr.decode('utf-8', errors='replace') if isinstance(stderr, (bytes, bytearray)) else (stderr or '')
            output = stdout_text
            if stderr_text:
                output += ("\n--- stderr ---\n" + stderr_text) if output else stderr_text
            if stopped:
                output = (output + "\n" if output else "") + "⏹️ 命令已被用户手动停止"
            elif timed_out:
                output = (output + "\n" if output else "") + f"⏰ 命令超过等待窗口 ({timeout}秒)，已停止。需要长驻/日志/交互任务时请使用 shell。"
            output = output or '(无输出)'

            elapsed_seconds = round(max(0.0, time.monotonic() - started_at), 2)
            saved = save_command_output(command, output)
            rc = process.returncode if process else -1
            return {
                'success': bool(not stopped and not timed_out and rc == 0),
                'command': command,
                'output': output,
                'display_output': format_shell_context_output(output, running=False),
                'return_code': rc if rc is not None else -1,
                'timed_out': timed_out,
                'stopped': stopped,
                'output_path': saved['path'],
                'output_bytes': saved['bytes'],
                'elapsed_seconds': elapsed_seconds,
            }
        except Exception as e:
            if process is not None:
                await terminate_async_process(process)
            logger.error(f"run 命令执行异常: {e}")
            output = f"执行异常: {str(e)[:200]}"
            elapsed_seconds = round(max(0.0, time.monotonic() - started_at), 2)
            saved = save_command_output(command, output)
            return {
                'success': False,
                'command': command,
                'output': output,
                'display_output': output,
                'return_code': -1,
                'timed_out': False,
                'stopped': False,
                'output_path': saved['path'],
                'output_bytes': saved['bytes'],
                'elapsed_seconds': elapsed_seconds,
            }
    
    @classmethod
    def get_clean_response_all(cls, ai_response: str) -> str:
        """获取 AI 回复中除命令块、文件块、发送块以外的文本。
        复用 strip_protocol_blocks 的行扫描逻辑，确保与 extract_protocol_blocks
        对 heredoc / file:base64: 的识别完全一致。"""
        return AgentExecutor.strip_protocol_blocks(ai_response).strip()


# --- ☆ Agent 交互 Shell 会话管理器 ☆ ---
