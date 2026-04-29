#!/usr/bin/env python3
"""每日灵感 v1.0 — GitHub Actions 自动化生成器"""

import json
import os
import re
import subprocess
import sys
import urllib.request
import urllib.parse
from datetime import datetime, timezone, timedelta

# ── 配置 ──
DEEPSEEK_API_KEY = os.environ.get("DEEPSEEK_API_KEY", "")
DINGTALK_TOKEN = os.environ.get("DINGTALK_TOKEN", "")
DINGTALK_WEBHOOK = f"https://oapi.dingtalk.com/robot/send?access_token={DINGTALK_TOKEN}"
CHINA_TZ = timezone(timedelta(hours=8))
TODAY = datetime.now(CHINA_TZ)
VERSION = "v1.1"


def log(msg):
    t = TODAY.strftime("%H:%M:%S")
    print(f"[{t}] {msg}")


def run(cmd, timeout=20):
    """Run a shell command and return stdout."""
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    return r.stdout.strip()


def is_working_day():
    """检查今天是否中国法定工作日."""
    date_str = TODAY.strftime("%Y-%m-%d")
    url = f"https://raw.githubusercontent.com/NateScarlet/holiday-cn/master/{TODAY.year}.json"
    try:
        req = urllib.request.Request(url)
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read().decode())
        for day in data.get("days", []):
            if day.get("date") == date_str:
                is_off = day.get("isOffDay", False)
                log(f"节假日数据: {date_str} isOffDay={is_off} name={day.get('name','')}")
                return not is_off
        log(f"{date_str} 不在节假日列表中，回退 weekday 检查")
        return TODAY.weekday() < 5
    except Exception as e:
        log(f"节假日 API 异常: {e}，回退 weekday 检查")
        return TODAY.weekday() < 5


def search_web(query, max_results=5):
    """DuckDuckGo 搜索（用 HTML 后端，兼容 Actions 环境）."""
    try:
        from duckduckgo_search import DDGS
        with DDGS() as ddgs:
            return list(ddgs.text(query, max_results=max_results, backend="html"))
    except Exception as e:
        log(f"搜索失败 [{query[:40]}]: {e}")
        return []


def proxy_image(url):
    """走 weserv.nl 代理，绕防盗链."""
    if not url:
        return url
    return f"https://images.weserv.nl/?url={urllib.parse.quote(url, safe='')}"


def extract_og_image(article_url):
    """从文章 HTML 中提取 og:image，并验证图片可达."""
    try:
        html = run(["curl", "-sL", "--connect-timeout", "10", article_url])
        if not html:
            return None

        # og:image
        m = re.search(r'<meta\s+property="og:image"\s+content="([^"]+)"', html, re.IGNORECASE)
        if m:
            img = m.group(1).replace("&amp;", "&")
            if "social" not in img:
                code = run(["curl", "-sL", "-o", "/dev/null", "-w", "%{http_code}",
                           "--connect-timeout", "5", img])
                if code == "200":
                    return img

        # twitter:image
        m = re.search(r'<meta\s+name="twitter:image"\s+content="([^"]+)"', html, re.IGNORECASE)
        if m:
            img = m.group(1).replace("&amp;", "&")
            code = run(["curl", "-sL", "-o", "/dev/null", "-w", "%{http_code}",
                       "--connect-timeout", "5", img])
            if code == "200":
                return img

        # 正文首张大图（>600px）
        m = re.search(r'<img[^>]+src="([^"]+)"[^>]*(?:width="([^"]+)")?', html)
        if m:
            img = m.group(1)
            if img.startswith("http") and "logo" not in img.lower():
                code = run(["curl", "-sL", "-o", "/dev/null", "-w", "%{http_code}",
                           "--connect-timeout", "5", img])
                if code == "200":
                    return img

        return None
    except Exception as e:
        log(f"取图失败 [{article_url[:60]}]: {e}")
        return None


def search_with_images(section_name, queries, max_items=3):
    """搜索某板块文章，直到凑齐指定数量的有图结果."""
    candidates = []
    for q in queries:
        if len(candidates) >= max_items:
            break
        results = search_web(q, max_results=5)
        for r in results:
            if len(candidates) >= max_items:
                break
            url = r.get("href", "")
            if not url or not url.startswith("http"):
                continue
            # 跳过已知无图/低质站点
            skip_domains = []
            if any(d in url for d in skip_domains):
                continue
            img = extract_og_image(url)
            if img:
                candidates.append({
                    "title": r.get("title", "").strip(),
                    "url": url,
                    "snippet": r.get("body", "").strip(),
                    "image": proxy_image(img),
                })
                log(f"{section_name} ✅ {candidates[-1]['title'][:40]}...")
    return candidates


def call_deepseek(prompt):
    """调用 DeepSeek API 生成日报内容."""
    payload = json.dumps({
        "model": "deepseek-chat",
        "messages": [
            {"role": "system", "content": "你是「每日灵感」的编辑，擅长撰写设计创意日报。用中文。"},
            {"role": "user", "content": prompt},
        ],
        "temperature": 0.7,
        "max_tokens": 2000,
    }).encode()

    req = urllib.request.Request("https://api.deepseek.com/v1/chat/completions", data=payload)
    req.add_header("Content-Type", "application/json")
    req.add_header("Authorization", f"Bearer {DEEPSEEK_API_KEY}")

    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            data = json.loads(resp.read().decode())
            return data["choices"][0]["message"]["content"]
    except Exception as e:
        log(f"DeepSeek 调用失败: {e}")
        return None


def parse_newsletter(ai_output):
    """从 AI 输出中解析出结构化内容."""
    # 尝试从 ```json ... ``` 中提取
    m = re.search(r'```(?:json)?\s*(\{.*?\})\s*```', ai_output, re.DOTALL)
    if m:
        try:
            return json.loads(m.group(1))
        except json.JSONDecodeError:
            pass
    # 直接尝试解析整个输出
    try:
        return json.loads(ai_output)
    except json.JSONDecodeError:
        log("AI 输出不是标准 JSON，尝试构造结构化数据")
        return None


def assemble_markdown(sections, articles):
    """组装钉钉 Markdown 消息."""
    now_str = TODAY.strftime("%Y.%m.%d")
    lines = [f"# 🎨 每日灵感 {now_str}\n"]

    section_config = [
        ("壹观", "品牌 / UI / 创意案例"),
        ("贰知", "AI 资讯 / 工具 / 工作流"),
        ("叁赏", "艺术 / 建筑 / 摄影 / 作品"),
        ("肆律", "设计原则"),
        ("伍言", "名人名言"),
    ]

    for key, subtitle in section_config:
        item = sections.get(key, {})
        content = item.get("content", "")
        title = item.get("title", "")

        lines.append(f"## {key} · {subtitle}")
        if title:
            lines.append(f"**{title}**")

        # 前 3 条带配图
        if key in ("壹观", "贰知", "叁赏"):
            img = item.get("image", "")
            art = next((a for a in articles.get(key, []) if a.get("image")), None)
            img = img or (art["image"] if art else "")
            if img:
                lines.append(f"![]({img})")

        lines.append(content)

        url = item.get("url", "")
        if not url:
            art = next((a for a in articles.get(key, []) if a.get("url")), None)
            url = art["url"] if art else ""
        if url:
            lines.append(f"[📎 原始案例]({url})")
        lines.append("")

    lines.append(f"---\n*每日灵感 {VERSION} · 工作日 9:30 自动发送*")
    return "\n".join(lines)


def send_dingtalk(text):
    """发送 Markdown 到钉钉群."""
    payload_bytes = text.encode("utf-8")
    if len(payload_bytes) > 3500:
        log(f"内容过长 ({len(payload_bytes)} bytes)，截断")
        text = payload_bytes[:3400].decode("utf-8", errors="ignore")
        text = text[:text.rfind("\n")] + "\n\n*（内容截断）*"

    body = json.dumps({
        "msgtype": "markdown",
        "markdown": {
            "title": f"每日灵感 {TODAY.strftime('%Y.%m.%d')}",
            "text": text,
        },
    }).encode()

    req = urllib.request.Request(DINGTALK_WEBHOOK, data=body)
    req.add_header("Content-Type", "application/json")

    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            result = json.loads(resp.read().decode())
            if result.get("errcode") == 0:
                log("✅ 钉钉发送成功")
                return True
            else:
                log(f"❌ 钉钉返回错误: {result}")
                return False
    except Exception as e:
        log(f"❌ 钉钉请求异常: {e}")
        return False


# ── 主流程 ──

def main():
    log(f"=== 每日灵感 {TODAY.strftime('%Y.%m.%d')} ===")

    # 1. 检查工作日
    if not is_working_day():
        log("今天非工作日，跳过")
        return

    # 2. 搜索各板块素材
    search_config = {
        "壹观": [
            "branding identity design 2026 -pinterest",
            "creative brand identity rebrand case study 2026",
            "UI UX design award showcase 2026",
        ],
        "贰知": [
            "site:theverge.com AI artificial intelligence tool 2026",
            "site:techcrunch.com AI agent startup 2026",
            "site:arstechnica.com AI product launch 2026",
        ],
        "叁赏": [
            "architecture installation design exhibition 2026 -pinterest",
            "art sculpture photography contemporary 2026",
            "creative installation public art 2026",
        ],
    }

    found_articles = {}
    for section, queries in search_config.items():
        log(f"搜索 {section}...")
        found_articles[section] = search_with_images(section, queries)
        log(f"  → 找到 {len(found_articles[section])} 篇有图文章")

    # 3. 检查配图完整性（前 3 条必须全有图）
    missing = [k for k in ("壹观", "贰知", "叁赏") if not found_articles.get(k)]
    if missing:
        log(f"⚠️ {', '.join(missing)} 缺少有图文章，尝试补充搜索")
        for k in missing:
            fallback_map = {"壹观": "brand design inspiration", "贰知": "AI technology news", "叁赏": "art design creative"}
            backup_q = [f"{fallback_map.get(k, 'design')} 2026"]
            found_articles[k] = search_with_images(k, backup_q)
            if not found_articles[k]:
                log(f"❌ {k} 实在找不到有图文章，发送简化版")

    # 4. 构造 AI prompt
    has_articles = any(found_articles.get(k) for k in ("壹观", "贰知", "叁赏"))

    if has_articles:
        prompt = f"""请撰写今日的「每日灵感」日报（{TODAY.strftime('%Y.%m.%d')}）。

## 格式要求
- **壹观**：品牌/UI/交互案例，120-180 字
- **贰知**：AI 资讯/工具/工作流，120-180 字
- **叁赏**：艺术/建筑/摄影，120-180 字
- **肆律**：一条设计原则 + 简介，50-80 字
- **伍言**：设计名人名言（外国人需双语），50-80 字

每条末尾标注原始来源链接。

## 今日素材
以下是为各板块找到的文章（含配图），请基于它们来撰写：\n\n"""

        for section, articles in found_articles.items():
            prompt += f"### {section}\n"
            for a in articles:
                prompt += f"- 标题：{a['title']}\n  来源：{a['url']}\n  配图：{a['image']}\n  摘要：{a['snippet'][:200]}\n"
            prompt += "\n"
        prompt += "**重要：每条内容的 image 字段必须使用上面提供的对应配图 URL，不要编造。**\n"
    else:
        prompt = f"""请撰写今日的「每日灵感」日报（{TODAY.strftime('%Y.%m.%d')}）。

## 格式要求
- **壹观**：品牌/UI/交互案例，120-180 字
- **贰知**：AI 资讯/工具/工作流，120-180 字
- **叁赏**：艺术/建筑/摄影，120-180 字
- **肆律**：一条设计原则 + 简介，50-80 字
- **伍言**：设计名人名言（外国人需双语），50-80 字

每条末尾标注原始来源链接。

## 说明
本次搜索未找到合适的外部素材，请基于你自己的知识来撰写内容。配图 URL 留空即可。"""

    prompt += """请直接输出 JSON（含所有板块的内容、配图 URL、来源链接），格式：

```json
{
  "壹观": {"title": "案例标题", "content": "正文...", "image": "https://...", "url": "https://..."},
  "贰知": {"title": "案例标题", "content": "正文...", "image": "https://...", "url": "https://..."},
  "叁赏": {"title": "案例标题", "content": "正文...", "image": "https://...", "url": "https://..."},
  "肆律": {"content": "设计原则+简介", "url": "https://..."},
  "伍言": {"content": "名人名言（双语）", "url": "https://..."}
}
```"""

    # 5. 调用 DeepSeek
    log("调用 DeepSeek 生成内容...")
    ai_output = call_deepseek(prompt)
    if not ai_output:
        log("❌ AI 生成失败，终止")
        sys.exit(1)
    log(f"AI 回复长度: {len(ai_output)} 字符")

    sections = parse_newsletter(ai_output)
    if not sections:
        log("❌ 解析 AI 输出失败")
        log("原始输出:")
        print(ai_output[:500])
        sys.exit(1)

    # 6. 智能匹配图片（AI 写了哪篇文章就用哪张图）
    for k in ("壹观", "贰知", "叁赏"):
        articles = found_articles.get(k, [])
        if not articles:
            continue
        ai_url = sections.get(k, {}).get("url", "")
        # 按 URL 精确匹配
        matched = next((a for a in articles if a["url"] == ai_url), None)
        if not matched:
            matched = articles[0]  # fallback
        # AI 的图如果不是我们代理过的，用匹配到的替代
        if "weserv.nl" not in (sections.get(k, {}).get("image") or ""):
            sections[k]["image"] = matched["image"]
        if not sections.get(k, {}).get("url"):
            sections[k]["url"] = matched["url"]

    # 7. 组装并发送
    markdown = assemble_markdown(sections, found_articles)
    log(f"Markdown 长度: {len(markdown.encode('utf-8'))} bytes")

    success = send_dingtalk(markdown)
    if success:
        log("✅ 每日灵感发送完毕")
    else:
        log("⚠️ 钉钉返回异常（消息可能已发送），流程完成")


if __name__ == "__main__":
    main()
