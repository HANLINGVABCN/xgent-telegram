// 网页端 Markdown 渲染管线的冒烟测试。
//
// 前端没有测试框架，也没有构建步骤，所以这里直接把 index.html 里的
// <script> 抠出来，在 linkedom 造的 DOM 上跑真实的 renderText / highlightCode
// / sanitizeHtml，配合真实的 vendor/marked.umd.js。
//
// 跑法：node tools/render_smoke.mjs
// 依赖：npm install --no-save linkedom（仅本地跑，不进 requirements/CI）

import fs from "node:fs";
import path from "node:path";
import vm from "node:vm";
import { fileURLToPath } from "node:url";
import { parseHTML } from "linkedom";

const root = path.dirname(path.dirname(fileURLToPath(import.meta.url)));
// index.html 按 .gitattributes 存的是 CRLF，这里统一成 LF 再做定位/求值。
const html = fs.readFileSync(path.join(root, "xgent_app/webui/index.html"), "utf8").replace(/\r\n/g, "\n");
const markedSrc = fs.readFileSync(path.join(root, "xgent_app/webui/vendor/marked.umd.js"), "utf8").replace(/\r\n/g, "\n");

// index.html 的内联脚本是一个 IIFE，整段跑起来会去 getElementById 一堆真实节点。
// 这里只取渲染相关的纯函数段：从 escapeHtml 开始，到 renderText 结束。
const startIdx = html.indexOf("  function escapeHtml(s) {");
const endMarker = "    return sanitizeHtml(renderMarkdown(text));\n  }";
const endIdx = html.indexOf(endMarker);
if (startIdx < 0 || endIdx < 0) {
  console.error("找不到渲染函数段，index.html 结构可能变了");
  process.exit(1);
}
const renderSrc = html.slice(startIdx, endIdx + endMarker.length);

const { document, window } = parseHTML("<!doctype html><html><body></body></html>");
const sandbox = { document, window, console };
sandbox.window.marked = undefined;
vm.createContext(sandbox);

// 真实的 vendored marked，走 UMD 的 g["marked"] = f() 分支
vm.runInContext(markedSrc, sandbox);
vm.runInContext("window.marked = marked;", sandbox);
vm.runInContext(renderSrc, sandbox);

let failed = 0;
function check(name, cond, detail) {
  if (cond) {
    console.log(`  ok   ${name}`);
  } else {
    failed++;
    console.log(`  FAIL ${name}`);
    if (detail) console.log(`       ${String(detail).replace(/\n/g, "\n       ")}`);
  }
}
const render = (md) => vm.runInContext("renderText", sandbox)(md, null);
const hl = (code, lang) => vm.runInContext("highlightCode", sandbox)(code, lang);

console.log("\n bug 1/2 引用块与多行引用");
{
  const out = render("> 第一行\n> 第二行\n>\n> 空行后仍在同一引用域");
  check("产出 <blockquote>", out.includes("<blockquote>"), out);
  check("只有一个引用块（不断层）", (out.match(/<blockquote>/g) || []).length === 1, out);
  check("字面 &gt; 没有泄漏到正文", !out.includes("&gt; 第一行"), out);
  const nested = render("> 引用里的 `代码` 和 **加粗**");
  check("引用内行内代码", nested.includes("<code>代码</code>"), nested);
  check("引用内加粗", nested.includes("<strong>加粗</strong>"), nested);
}

console.log("\n bug 5 表格");
{
  const out = render("| 左 | 中 | 右 |\n|:---|:--:|---:|\n| a | b | 123 |");
  check("产出 <table>", out.includes("<table>"), out);
  check("补回 .table-wrap 横向滚动容器", out.includes('class="table-wrap"'), out);
  check("列对齐落到 class 上", out.includes("ta-center") && out.includes("ta-right"), out);
  check("没有裸竖线泄漏", !out.includes("|"), out);
  const esc = render("| a | b |\n|---|---|\n| 转义 \\| 竖线 | y |");
  check("转义竖线不断列", esc.includes("转义 | 竖线"), esc);
}

console.log("\n bug A/B 代码高亮自噬");
{
  const out = hl('x = "字符串里的 class"', "python");
  check("class 不自我污染", !out.includes('<span <span'), out);
  check("class 属性完整", (out.match(/class="hl-/g) || []).length >= 1, out);
  const q = hl("echo 'single'", "bash");
  check("单引号不被拆成 &#39; 字面", !q.includes("&amp;#39"), q);
  check("单引号串识别为字符串", q.includes('hl-str'), q);
  const cmt = hl("# 注释里有 class 关键字\nx = 1", "python");
  check("注释块无嵌套 span 破损", !cmt.includes("<span <span"), cmt);
}

console.log("\n bug C 代码块里的 $ 替换模式");
{
  const out = render("```bash\necho $'带 $& 和 $` 与 $$'\n```");
  check("$& 保留", out.includes("$&amp;") || out.includes("$&"), out);
  check("$` 保留", out.includes("$`"), out);
  check("$$ 保留", out.includes("$$"), out);
  check("产出 pre/code", out.includes("<pre") && out.includes("<code"), out);
}

console.log("\n 代码块语言标记（enhanceBubbleContent 靠它读语言）");
{
  const out = render("```python\nprint(1)\n```");
  check("带 language- class", /class="language-python"/.test(out), out);
}

console.log("\n 列表 / 任务列表 / 分隔线");
{
  const out = render("- [ ] 未完成\n- [x] 已完成");
  check("checkbox 换成 .task-checkbox", out.includes("task-checkbox"), out);
  check("没有裸 <input>", !out.includes("<input"), out);
  check("任务项对齐到 .task-item", out.includes("task-item"), out);
  check("已勾选项保留 checked 状态", out.includes("task-checkbox checked"), out);
  check("已勾选项渲染出 ✓", out.includes("✓"), out);
  check("未勾选项不带 checked", /class="task-checkbox"><\/span>/.test(out), out);
  const ol = render("1. 一\n2. 二\n   - 嵌套");
  check("有序列表", ol.includes("<ol>"), ol);
  check("嵌套无序列表", ol.includes("<ul>"), ol);
  check("分隔线", render("a\n\n---\n\nb").includes("<hr"), "");
}

console.log("\n 换行语义（breaks:true，聊天输出依赖单换行可见）");
{
  const out = render("第一行\n第二行");
  check("单换行产出 <br>", out.includes("<br"), out);
}

console.log("\n XSS / 消毒");
{
  const out = render("<script>alert(1)</script>\n\n<img src=x onerror=alert(1)>");
  check("script 标签不残留", !/<script/i.test(out), out);
  check("onerror 属性被剥掉", !/onerror/i.test(out), out);
  const link = render("[x](javascript:alert(1))");
  check("javascript: 链接被剥 href", !/href="javascript/i.test(link), link);
  const ok = render("[x](https://example.com)");
  check("正常链接保留并加 rel", ok.includes('rel="noopener noreferrer"'), ok);
}

console.log("\n 流式：未闭合围栏不炸版");
{
  const out = render("说明文字\n\n```python\nprint(1)");
  check("未闭合围栏当作代码块", out.includes("<pre"), out);
  check("不残留字面反引号", !out.includes("```"), out);
}

console.log("\n marked 缺失时的兜底");
{
  vm.runInContext("window.marked = undefined;", sandbox);
  const out = render("**不该崩** <script>x</script>");
  check("退化为纯文本且不抛异常", out.includes("**不该崩**"), out);
  check("兜底路径仍然消毒", !/<script/i.test(out), out);
  vm.runInContext("window.marked = marked;", sandbox);
}

console.log(failed === 0 ? "\n全部通过\n" : `\n${failed} 项失败\n`);
process.exit(failed === 0 ? 0 : 1);
