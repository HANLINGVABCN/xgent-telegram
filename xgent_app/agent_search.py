"""Web 搜索与网页正文抓取（Tavily）。

本模块只负责发请求和把结果整理成纯文本，不发 Telegram 消息、不写数据库、
不参与 Agent 循环控制。

search-x 用于按关键词检索，fetch-x 用于按 URL 取正文。两者都要求配置
TAVILY_API_KEY，未配置时返回带指引的失败结果而不是抛异常。
"""

from __future__ import annotations

import asyncio
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse

import httpx


TAVILY_SEARCH_URL = "https://api.tavily.com/search"
TAVILY_EXTRACT_URL = "https://api.tavily.com/extract"

DEFAULT_MAX_RESULTS = 5
MAX_ALLOWED_RESULTS = 20
REQUEST_TIMEOUT = 45.0
# 单条正文回灌上限，避免一次抓取就把上下文撑爆。
MAX_CONTENT_CHARS = 6000
MAX_FETCH_URLS = 5

_MISSING_KEY_HINT = (
    "未配置 TAVILY_API_KEY。请在项目根目录 .env 中添加 "
    "TAVILY_API_KEY=tvly-xxxx 后重启，即可使用联网搜索。"
    "免费额度可在 https://tavily.com 注册获取。"
)


def _truncate(text: str, limit: int) -> str:
    text = str(text or "").strip()
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n...(正文已截断，原长 {len(text)} 字符)"


def parse_search_body(body: str) -> Dict[str, Any]:
    """解析 search-x 正文：首行是查询词，其余是 key: value 选项。

    支持的选项：
        max: 8           # 结果条数，默认 5，上限 20
        depth: advanced  # basic(默认) 或 advanced
        site: example.com  # 限定域名，可重复
        -raw             # 附带网页正文，而不是只要摘要
    """
    lines = [line.rstrip() for line in str(body or "").split("\n")]
    query_parts: List[str] = []
    max_results = DEFAULT_MAX_RESULTS
    depth = "basic"
    include_domains: List[str] = []
    include_raw = False

    for line in lines:
        stripped = line.strip()
        if not stripped:
            continue
        lowered = stripped.lower()
        if lowered in {"-raw", "raw"}:
            include_raw = True
            continue
        if ":" in stripped:
            key, _, value = stripped.partition(":")
            key = key.strip().lower()
            value = value.strip()
            if key == "max":
                try:
                    max_results = max(1, min(MAX_ALLOWED_RESULTS, int(value)))
                except ValueError:
                    pass
                continue
            if key == "depth":
                if value.lower() in {"basic", "advanced"}:
                    depth = value.lower()
                continue
            if key == "site":
                if value:
                    include_domains.append(value)
                continue
        # 不是已知选项，当作查询词的一部分。
        query_parts.append(stripped)

    return {
        "query": " ".join(query_parts).strip(),
        "max_results": max_results,
        "depth": depth,
        "include_domains": include_domains,
        "include_raw": include_raw,
    }


def parse_fetch_body(body: str) -> List[str]:
    """解析 fetch-x 正文：每行一个 URL。"""
    urls: List[str] = []
    seen = set()
    for line in str(body or "").split("\n"):
        candidate = line.strip()
        if not candidate or candidate.startswith("#"):
            continue
        parsed = urlparse(candidate)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            continue
        if candidate in seen:
            continue
        seen.add(candidate)
        urls.append(candidate)
        if len(urls) >= MAX_FETCH_URLS:
            break
    return urls


def _failure(kind: str, message: str) -> Dict[str, Any]:
    return {
        "success": False,
        "kind": kind,
        "query": "",
        "results": [],
        "output": message,
        "notice": message,
    }


async def _post_json(
    url: str,
    payload: Dict[str, Any],
    api_key: str,
) -> Dict[str, Any]:
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {api_key}",
    }
    async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT) as client:
        response = await client.post(url, json=payload, headers=headers)
        response.raise_for_status()
        return response.json()


def _describe_http_error(exc: httpx.HTTPStatusError) -> str:
    code = exc.response.status_code
    if code == 401:
        return "搜索失败：TAVILY_API_KEY 无效或已过期，请检查 .env 配置。"
    if code == 429:
        return "搜索失败：已达到 Tavily 免费额度上限，请稍后再试或升级套餐。"
    if code == 432:
        return "搜索失败：Tavily 账户额度已用尽。"
    return f"搜索失败：Tavily 返回 HTTP {code}。"


def format_search_results(query: str, results: List[Dict[str, Any]], answer: str) -> str:
    """把搜索结果渲染为模型和用户都能直接读的纯文本。"""
    header = f"[search结果] query='{query}' 命中 {len(results)} 条"
    if not results:
        return (
            header + "\n无结果。可尝试：换关键词 / 去掉 site 限定 / 用 depth: advanced。"
        )

    parts = [header]
    if answer:
        parts.append(f"\n【摘要】{answer}")
    for index, item in enumerate(results, start=1):
        title = str(item.get("title") or "(无标题)").strip()
        url = str(item.get("url") or "").strip()
        content = str(item.get("content") or "").strip()
        parts.append(f"\n{index}. {title}\n   {url}")
        if content:
            parts.append(f"   {_truncate(content, 800)}")
        raw = str(item.get("raw_content") or "").strip()
        if raw:
            parts.append(f"   [正文]\n{_truncate(raw, MAX_CONTENT_CHARS)}")
    return "\n".join(parts)


def format_fetch_results(pages: List[Dict[str, Any]], failed: List[Dict[str, Any]]) -> str:
    """渲染 fetch-x 的抓取结果。"""
    header = f"[fetch结果] 成功 {len(pages)} 个，失败 {len(failed)} 个"
    parts = [header]
    for index, page in enumerate(pages, start=1):
        url = str(page.get("url") or "").strip()
        content = str(page.get("raw_content") or "").strip()
        parts.append(f"\n{index}. {url}")
        parts.append(_truncate(content, MAX_CONTENT_CHARS) or "(正文为空)")
    for item in failed:
        url = str(item.get("url") or "").strip()
        reason = str(item.get("error") or "抓取失败").strip()
        parts.append(f"\n✗ {url}\n   {reason}")
    return "\n".join(parts)


async def run_search(body: str, api_key: Optional[str]) -> Dict[str, Any]:
    """按关键词检索，返回带 output/notice 的结果字典。"""
    if not api_key:
        return _failure("search", f"[search结果] {_MISSING_KEY_HINT}")

    options = parse_search_body(body)
    query = options["query"]
    if not query:
        return _failure(
            "search",
            "[search结果] 查询词为空。search-x 正文首行必须是要搜索的关键词。",
        )

    payload: Dict[str, Any] = {
        "query": query,
        "max_results": options["max_results"],
        "search_depth": options["depth"],
        "include_answer": True,
        "include_raw_content": options["include_raw"],
    }
    if options["include_domains"]:
        payload["include_domains"] = options["include_domains"]

    try:
        data = await _post_json(TAVILY_SEARCH_URL, payload, api_key)
    except httpx.HTTPStatusError as exc:
        return _failure("search", f"[search结果] {_describe_http_error(exc)}")
    except httpx.TimeoutException:
        return _failure(
            "search", f"[search结果] 搜索超时（{int(REQUEST_TIMEOUT)} 秒），请重试。"
        )
    except Exception as exc:  # 网络不可达、DNS 失败、JSON 异常等
        return _failure("search", f"[search结果] 搜索异常: {str(exc)[:200]}")

    raw_results = data.get("results")
    results = [item for item in raw_results if isinstance(item, dict)] if isinstance(raw_results, list) else []
    answer = str(data.get("answer") or "").strip()
    output = format_search_results(query, results, answer)

    return {
        "success": True,
        "kind": "search",
        "query": query,
        "results": results,
        "answer": answer,
        "output": output,
        "notice": output,
    }


async def run_fetch(body: str, api_key: Optional[str]) -> Dict[str, Any]:
    """按 URL 抓取网页正文。"""
    if not api_key:
        return _failure("fetch", f"[fetch结果] {_MISSING_KEY_HINT}")

    urls = parse_fetch_body(body)
    if not urls:
        return _failure(
            "fetch",
            "[fetch结果] 没有有效 URL。fetch-x 正文每行写一个 http/https 链接，"
            f"一次最多 {MAX_FETCH_URLS} 个。",
        )

    try:
        data = await _post_json(TAVILY_EXTRACT_URL, {"urls": urls}, api_key)
    except httpx.HTTPStatusError as exc:
        return _failure("fetch", f"[fetch结果] {_describe_http_error(exc)}")
    except httpx.TimeoutException:
        return _failure(
            "fetch", f"[fetch结果] 抓取超时（{int(REQUEST_TIMEOUT)} 秒），请重试。"
        )
    except Exception as exc:
        return _failure("fetch", f"[fetch结果] 抓取异常: {str(exc)[:200]}")

    raw_pages = data.get("results")
    pages = [item for item in raw_pages if isinstance(item, dict)] if isinstance(raw_pages, list) else []
    raw_failed = data.get("failed_results")
    failed = [item for item in raw_failed if isinstance(item, dict)] if isinstance(raw_failed, list) else []
    output = format_fetch_results(pages, failed)

    return {
        "success": bool(pages),
        "kind": "fetch",
        "query": "",
        "results": pages,
        "failed": failed,
        "output": output,
        "notice": output,
    }


__all__ = [
    "run_search",
    "run_fetch",
    "parse_search_body",
    "parse_fetch_body",
    "format_search_results",
    "format_fetch_results",
    "MAX_FETCH_URLS",
]
