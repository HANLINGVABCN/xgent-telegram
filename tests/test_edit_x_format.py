"""edit-x 协议 <<OLD / <<NEW 语法解析回归测试。

2026-09-29：把旧的 -----OLD----- / -----NEW----- 横线标记换成与外层 <<BEGIN_/<<END_
同族的 <<OLD / <<NEW 哨兵（更像协议自身的语法、更抗内容冲突），并删掉 docstring 里
宣称却从未实现的“路径后自定义标记（<<MARKER_OLD / >>MARKER_NEW）”死文档。

本测试锁死：
- 新语法解析正确（explicit_path 与 body 首行给路径两种入口、多行原样保留）；
- 旧横线标记不再被识别（硬切换、不新旧并存，避免模型各写各的）；
- 缺 <<OLD / 缺 <<NEW / 顺序颠倒都给出明确报错且不吐出半截内容；
- new 段内恰好含一行 <<OLD 不破坏解析；
- 可选后缀 <<OLD_<后缀> / <<NEW_<同一后缀>：同后缀配对、后缀隔离旧串内的裸
  <<NEW、后缀不一致或带后缀 OLD 配裸 NEW 均安全失败、<<OLDX 不误判为哨兵。

AgentExecutor 走共享命名空间加载（依赖跨 section 符号，不能单独 import），按仓库
惯例用带环境变量的子进程探针跑；断言在父进程做，失败信息更清楚。
"""

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

PROBE = r'''
import json, sys
sys.path.insert(0, %r)
from xgent_app.bootstrap import load_sections
ns = {"__file__": "xgent_server.py"}
load_sections(ns)
AE = ns["AgentExecutor"]

def parse(body, path=""):
    old, new, p, err = AE._parse_edit_body(body, path)
    return {"old": old, "new": new, "path": p, "err": err}

LS = "a" * 64   # 上限长度后缀
TL = "a" * 65   # 越界长度后缀

result = {
    "explicit": parse("<<OLD\nfoo\n<<NEW\nbar", "/x.py"),
    "body_path": parse("/x.py\n<<OLD\nfoo\n<<NEW\nbar"),
    "multiline": parse("<<OLD\nl1\nl2\n<<NEW\nn1\nn2", "/x.py"),
    "missing_old": parse("foo\n<<NEW\nbar", "/x.py"),
    "missing_new": parse("<<OLD\nfoo\nbar", "/x.py"),
    "swapped": parse("<<NEW\nbar\n<<OLD\nfoo", "/x.py"),
    "legacy_rejected": parse("-----OLD-----\nfoo\n-----NEW-----\nbar", "/x.py"),
    "new_contains_old_marker": parse("<<OLD\nfoo\n<<NEW\nbar\n<<OLD", "/x.py"),
    # 可选后缀：<<OLD_<后缀> 与 <<NEW_<同一后缀> 配对
    "suffix_pair": parse("<<OLD_a3f2\nfoo\n<<NEW_a3f2\nbar", "/x.py"),
    # 后缀隔离：旧串内含裸 <<NEW 行也不再抢走分隔，整段保留进 old
    "suffix_isolates_bare_new": parse("<<OLD_k9\nfoo\n<<NEW\nmid\n<<NEW_k9\nbar", "/x.py"),
    # 后缀不一致：OLD 有后缀而 NEW 后缀不同 → 找不到配对 → 报错、不吐半截
    "suffix_mismatch": parse("<<OLD_a3f2\nfoo\n<<NEW_zzzz\nbar", "/x.py"),
    # 带后缀的 OLD 不与裸 <<NEW 配对（须完全同后缀）
    "suffix_old_bare_new": parse("<<OLD_a3f2\nfoo\n<<NEW\nbar", "/x.py"),
    # <<OLDX / <<OLD_（空后缀）不是合法哨兵
    "not_a_mark_no_underscore": parse("<<OLDX\nfoo\n<<NEW\nbar", "/x.py"),
    # 后缀含下划线合法（与外层同族字符集）
    "suffix_underscore_ok": parse("<<OLD_a_b\nfoo\n<<NEW_a_b\nbar", "/x.py"),
    # 64 位后缀（上限）合法
    "suffix_max_len_ok": parse("<<OLD_" + LS + "\nfoo\n<<NEW_" + LS + "\nbar", "/x.py"),
    # 65 位后缀越界 → 不认作哨兵 → 视作缺 <<OLD
    "suffix_too_long": parse("<<OLD_" + TL + "\nfoo\n<<NEW\nbar", "/x.py"),
    "marks": [AE._EDIT_OLD_MARK, AE._EDIT_NEW_MARK],
}
print(json.dumps(result, ensure_ascii=False))
''' % str(ROOT)


class EditXFormatTests(unittest.TestCase):
    def _run(self):
        env = os.environ.copy()
        env.update({
            "BOT_TOKEN": "123456:TEST_TOKEN_FOR_IMPORT_ONLY",
            "AUTHORIZED_USER_ID": "1",
            "PYTHONPATH": str(ROOT),
            "PYTHONIOENCODING": "utf-8",
            "NO_COLOR": "1",
        })
        with tempfile.TemporaryDirectory() as cwd:
            env["XGENT_TRACE_LOG_FILE"] = str(Path(cwd) / "xgent_full_trace.log")
            r = subprocess.run([sys.executable, "-c", PROBE], cwd=cwd, env=env,
                               text=True, encoding="utf-8", capture_output=True, timeout=120)
        if r.returncode != 0:
            self.fail(f"probe failed\nstdout:\n{r.stdout}\nstderr:\n{r.stderr}")
        return json.loads(r.stdout.strip().splitlines()[-1])

    def test_new_sentinel_syntax_parses_and_legacy_is_rejected(self):
        d = self._run()
        # 常量已切到新哨兵
        self.assertEqual(d["marks"], ["<<OLD", "<<NEW"])
        # 新语法：块头给路径，body 只含两段
        self.assertEqual(d["explicit"], {"old": "foo", "new": "bar", "path": "/x.py", "err": ""})
        # 新语法：无块头时 body 首行为路径
        self.assertEqual(d["body_path"]["path"], "/x.py")
        self.assertEqual(d["body_path"]["old"], "foo")
        self.assertEqual(d["body_path"]["new"], "bar")
        self.assertEqual(d["body_path"]["err"], "")
        # 多行原样保留
        self.assertEqual(d["multiline"]["old"], "l1\nl2")
        self.assertEqual(d["multiline"]["new"], "n1\nn2")
        # 缺 <<OLD：报错点名 <<OLD，且不吐半截内容
        self.assertIn("<<OLD", d["missing_old"]["err"])
        self.assertEqual(d["missing_old"]["old"], "")
        # 缺 <<NEW：报错点名 <<NEW
        self.assertIn("<<NEW", d["missing_new"]["err"])
        # 顺序颠倒报错
        self.assertTrue(d["swapped"]["err"])
        # 旧横线标记必须彻底不认（硬切换）
        self.assertTrue(d["legacy_rejected"]["err"], "旧 -----OLD----- 不应再被识别为标记")
        self.assertIn("<<OLD", d["legacy_rejected"]["err"])
        self.assertEqual(d["legacy_rejected"]["old"], "")
        # new 段内含一行 <<OLD 不破坏解析（idx 取首个哨兵）
        self.assertEqual(d["new_contains_old_marker"]["old"], "foo")
        self.assertEqual(d["new_contains_old_marker"]["new"], "bar\n<<OLD")
        # 可选后缀：同后缀 OLD/NEW 正常配对
        self.assertEqual(d["suffix_pair"], {"old": "foo", "new": "bar", "path": "/x.py", "err": ""})
        # 后缀隔离：旧串里的裸 <<NEW 不再被当分隔，整段（含裸 <<NEW）保留进 old
        self.assertEqual(d["suffix_isolates_bare_new"]["old"], "foo\n<<NEW\nmid")
        self.assertEqual(d["suffix_isolates_bare_new"]["new"], "bar")
        self.assertEqual(d["suffix_isolates_bare_new"]["err"], "")
        # 后缀不一致：找不到配对 NEW → 报错且不吐半截内容（安全失败）
        self.assertTrue(d["suffix_mismatch"]["err"])
        self.assertEqual(d["suffix_mismatch"]["old"], "")
        self.assertEqual(d["suffix_mismatch"]["new"], "")
        # 带后缀 OLD 不与裸 <<NEW 配对
        self.assertTrue(d["suffix_old_bare_new"]["err"])
        self.assertEqual(d["suffix_old_bare_new"]["old"], "")
        # <<OLDX（无下划线）不是哨兵 → 视作缺 <<OLD
        self.assertTrue(d["not_a_mark_no_underscore"]["err"])
        self.assertIn("<<OLD", d["not_a_mark_no_underscore"]["err"])
        self.assertEqual(d["not_a_mark_no_underscore"]["old"], "")
        # 后缀含下划线合法
        self.assertEqual(d["suffix_underscore_ok"], {"old": "foo", "new": "bar", "path": "/x.py", "err": ""})
        # 64 位后缀（上限）合法
        self.assertEqual(d["suffix_max_len_ok"]["old"], "foo")
        self.assertEqual(d["suffix_max_len_ok"]["new"], "bar")
        self.assertEqual(d["suffix_max_len_ok"]["err"], "")
        # 65 位后缀越界 → 不认作哨兵 → 视作缺 <<OLD、且不吐半截
        self.assertTrue(d["suffix_too_long"]["err"])
        self.assertIn("<<OLD", d["suffix_too_long"]["err"])
        self.assertEqual(d["suffix_too_long"]["old"], "")


if __name__ == "__main__":
    unittest.main()
