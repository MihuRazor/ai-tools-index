#!/usr/bin/env python3
"""
AI工具大全自动爬虫
功能：
  1. 检查所有工具 URL 的存活状态
  2. 跟踪重定向并自动修正 URL
  3. 标记失效链接
  4. 获取 GitHub 仓库 Stars（如果是 GitHub 链接）
  5. 从 AI 工具聚合站发现新工具（进入候选池）
  6. 写入 Supabase 数据库

运行方式：
  python crawler.py

环境变量：
  SUPABASE_URL    - Supabase 项目 URL
  SUPABASE_KEY    - Supabase Service Role Key（写权限）
  GITHUB_TOKEN    - GitHub Personal Access Token（可选，提高 API 限额）
"""
import os
import sys
import re
import json
import time
from datetime import datetime
from urllib.parse import urlparse

import requests
from supabase import create_client, Client

# ============ 配置 ============
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
}
REQUEST_TIMEOUT = 20
MAX_RETRIES = 2
RETRY_DELAY = 3

# 新工具发现源（简单 HTML 抓取）
DISCOVERY_SOURCES = [
    {
        "name": "Futurepedia",
        "url": "https://www.futurepedia.io/",
        "list_selector": "a[href^=\'https://www.futurepedia.io/tool/\']",
        # 注：实际选择器需根据网站结构调整，这里用占位说明
    }
]

# 分类关键词映射（用于新工具自动分类猜测）
CATEGORY_KEYWORDS = {
    "chat": ["chatbot", "llm", "gpt", "assistant", "claude", "conversation", "chat"],
    "image": ["image", "photo", "picture", "art", "drawing", "diffusion", "midjourney", "dalle"],
    "video": ["video", "film", "clip", "footage", "runway", "sora", "animation"],
    "audio": ["audio", "music", "voice", "sound", "speech", "podcast", "song"],
    "code": ["code", "programming", "developer", "ide", "copilot", "cursor", "git"],
    "writing": ["writing", "copy", "content", "blog", "email", "grammar", "text"],
    "design": ["design", "ui", "ux", "logo", "prototype", "figma", "canva"],
    "search": ["search", "research", "academic", "paper", "perplexity"],
    "3d": ["3d", "model", "mesh", "spline", "blender", "texture"],
    "productivity": ["automation", "workflow", "meeting", "notes", "task", "productivity"]
}


def get_supabase() -> Client:
    url = os.environ.get("SUPABASE_URL")
    key = os.environ.get("SUPABASE_KEY")
    if not url or not key:
        print("❌ 错误：缺少 SUPABASE_URL 或 SUPABASE_KEY 环境变量")
        sys.exit(1)
    return create_client(url, key)


def check_url(url: str) -> dict:
    """
    检查 URL 状态，返回:
    {
        "status": "ok" | "redirect" | "broken" | "unknown",
        "final_url": str,   # 重定向后的最终 URL
        "http_code": int,
        "error": str | None
    }
    """
    result = {"status": "unknown", "final_url": url, "http_code": 0, "error": None}

    for attempt in range(MAX_RETRIES):
        try:
            resp = requests.head(
                url, headers=HEADERS, timeout=REQUEST_TIMEOUT,
                allow_redirects=True, verify=False
            )
            result["http_code"] = resp.status_code
            result["final_url"] = resp.url

            if resp.status_code == 200:
                result["status"] = "ok"
            elif resp.status_code in (301, 302, 307, 308):
                result["status"] = "redirect"
            elif resp.status_code in (404, 410):
                result["status"] = "broken"
            else:
                result["status"] = "unknown"
            return result

        except requests.exceptions.SSLError:
            # SSL 错误，尝试用 http
            if url.startswith("https://"):
                result["error"] = "SSL error, will retry with http"
                url = url.replace("https://", "http://", 1)
                continue
            result["status"] = "broken"
            result["error"] = "SSL error"
            return result

        except requests.exceptions.RequestException as e:
            result["error"] = str(e)
            if attempt < MAX_RETRIES - 1:
                time.sleep(RETRY_DELAY)
            continue

    # 所有重试失败
    if result["error"] and "timeout" in result["error"].lower():
        result["status"] = "unknown"
    else:
        result["status"] = "broken"
    return result


def extract_github_repo(url: str) -> str | None:
    """从 URL 中提取 GitHub owner/repo"""
    parsed = urlparse(url)
    if parsed.netloc not in ("github.com", "www.github.com"):
        return None
    match = re.match(r"^/([^/]+/[^/]+)/?$", parsed.path)
    if match:
        return match.group(1)
    return None


def fetch_github_stars(repo: str) -> int | None:
    """获取 GitHub 仓库 Stars 数量"""
    token = os.environ.get("GITHUB_TOKEN")
    headers = {"Accept": "application/vnd.github.v3+json", "User-Agent": "ai-tools-crawler"}
    if token:
        headers["Authorization"] = f"token {token}"

    try:
        resp = requests.get(
            f"https://api.github.com/repos/{repo}",
            headers=headers, timeout=15
        )
        if resp.status_code == 200:
            return resp.json().get("stargazers_count")
        elif resp.status_code == 404:
            return None
        else:
            print(f"   ⚠️ GitHub API {resp.status_code} for {repo}")
            return None
    except Exception as e:
        print(f"   ⚠️ GitHub fetch error: {e}")
        return None


def guess_category(name: str, desc: str) -> str | None:
    """根据名称和描述猜测分类"""
    text = (name + " " + (desc or "")).lower()
    scores = {}
    for cat_id, keywords in CATEGORY_KEYWORDS.items():
        scores[cat_id] = sum(1 for kw in keywords if kw in text)
    best = max(scores, key=scores.get)
    return best if scores[best] > 0 else None


def discover_new_tools(supabase: Client) -> int:
    """
    从聚合站发现新工具。
    当前实现：基于简单启发式，生产环境建议接入特定 API 或更稳定的爬虫。
    返回发现的候选数量。
    """
    print("🔍 开始发现新工具...")
    # 获取现有 URL 列表用于去重
    existing = supabase.table("tools").select("url").execute()
    existing_urls = {r["url"] for r in existing.data}

    candidates = []

    # 示例：从一些已知的新工具源（这里用简化的模拟逻辑）
    # 生产环境中，你可以替换为实际的 requests + BeautifulSoup 抓取逻辑
    # 例如抓取 https://www.futurepedia.io/ 或 https://www.theresanaiforthat.com/

    # 由于聚合站结构变化频繁，这里提供一个可扩展的框架
    # 实际抓取代码需根据目标网站 HTML 结构编写

    print(f"   当前已收录 {len(existing_urls)} 个工具")
    print("   提示： discover_new_tools() 需要接入具体聚合站 HTML 结构")
    print("   请根据目标网站的 DOM 选择器补充抓取逻辑")

    # 如果你补充了抓取逻辑，将结果插入 candidates 表：
    # for cand in candidates:
    #     supabase.table("tool_candidates").insert(cand).execute()

    return 0


def main():
    print("=" * 60)
    print(f"🤖 AI工具大全自动爬虫启动")
    print(f"📅 {datetime.now().isoformat()}")
    print("=" * 60)

    supabase = get_supabase()

    # 读取所有工具
    print("
📦 从数据库读取工具列表...")
    resp = supabase.table("tools").select("*").execute()
    tools = resp.data
    print(f"   共 {len(tools)} 个工具")

    stats = {
        "checked": 0, "fixes": 0, "broken": 0,
        "github_fetched": 0, "candidates": 0
    }

    # 1. 检查所有 URL
    print("
🔗 开始检查 URL 存活状态...")
    for tool in tools:
        tid = tool["id"]
        name = tool["name"]
        url = tool["url"]
        stats["checked"] += 1

        print(f"   [{stats['checked']}/{len(tools)}] {name} ...", end=" ")

        # 检查 URL
        result = check_url(url)
        updates = {
            "url_status": result["status"],
            "url_status_checked_at": datetime.now().isoformat()
        }

        # 如果发生重定向，修正 URL
        if result["status"] == "redirect" and result["final_url"] != url:
            updates["url"] = result["final_url"]
            stats["fixes"] += 1
            print(f"🔄 重定向 → {result['final_url']}")
        elif result["status"] == "broken":
            stats["broken"] += 1
            print(f"❌ 失效 (HTTP {result['http_code']})")
        elif result["status"] == "ok":
            print(f"✅ OK")
        else:
            print(f"⚠️ 未知 (HTTP {result['http_code']}, {result['error']})")

        # 写入更新
        supabase.table("tools").update(updates).eq("id", tid).execute()

        # 2. 如果是 GitHub 链接，获取 Stars
        gh_repo = extract_github_repo(updates.get("url", url))
        if gh_repo:
            stars = fetch_github_stars(gh_repo)
            if stars is not None:
                supabase.table("tools").update({
                    "github_stars": stars,
                    "github_repo": gh_repo
                }).eq("id", tid).execute()
                stats["github_fetched"] += 1
                print(f"      ⭐ GitHub Stars: {stars}")

        # 礼貌延迟，避免被限流
        time.sleep(0.8)

    # 3. 发现新工具
    stats["candidates"] = discover_new_tools(supabase)

    # 4. 写入更新日志
    log = {
        "tools_checked": stats["checked"],
        "url_fixes": stats["fixes"],
        "url_broken": stats["broken"],
        "github_stars_fetched": stats["github_fetched"],
        "new_candidates": stats["candidates"],
        "details": stats
    }
    supabase.table("update_logs").insert(log).execute()

    print("
" + "=" * 60)
    print("📊 本次更新统计")
    print(f"   检查工具: {stats['checked']}")
    print(f"   URL 修正: {stats['fixes']}")
    print(f"   失效链接: {stats['broken']}")
    print(f"   GitHub Stars: {stats['github_fetched']}")
    print(f"   新候选工具: {stats['candidates']}")
    print("=" * 60)
    print("✅ 爬虫完成")


if __name__ == "__main__":
    # 忽略 SSL 警告（部分网站证书问题）
    import urllib3
    urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
    main()
