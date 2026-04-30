#!/usr/bin/env python3
"""每日灵感 v1.3 — GitHub Actions 自动化生成器（本地图片存储）"""

import json, os, re, subprocess, sys, urllib.request, urllib.parse, glob
from datetime import datetime, timezone, timedelta

# ── 配置 ──
DEEPSEEK_API_KEY = os.environ.get("DEEPSEEK_API_KEY", "")
DINGTALK_TOKEN = os.environ.get("DINGTALK_TOKEN", "")
DINGTALK_WEBHOOK = f"https://oapi.dingtalk.com/robot/send?access_token={DINGTALK_TOKEN}"
REPO_OWNER = os.environ.get("REPO_OWNER", "Befour-YB")
REPO_NAME = os.environ.get("REPO_NAME", "daily-inspiration")
BRANCH = os.environ.get("BRANCH", "main")
RAW_BASE = f"https://raw.githubusercontent.com/{REPO_OWNER}/{REPO_NAME}/{BRANCH}"
IMAGES_DIR = "images"
CHINA_TZ = timezone(timedelta(hours=8))
TODAY = datetime.now(CHINA_TZ)
VERSION = "v1.3"
FULL_RUN_UNTIL = datetime(2026, 5, 6, tzinfo=CHINA_TZ)


def log(msg):
    print(f"[{TODAY.strftime('%H:%M:%S')}] {msg}")


def run(cmd, timeout=30):
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    return r.stdout.strip()


# ── 搜索 ──

def search_web(query, max_results=5):
    """DuckDuckGo 搜索."""
    try:
        from duckduckgo_search import DDGS
        with DDGS() as ddgs:
            return list(ddgs.text(query, max_results=max_results, backend="html"))
    except Exception as e:
        log(f"搜索失败 [{query[:40]}]: {e}")
        return []


# ── 图片：带 Referer 下载到本地 → GitHub Raw URL ──

UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"

def download_image(img_url, article_url):
    """伪装浏览器下载原图，多重策略绕防盗链，返回 raw.githubusercontent.com URL."""
    os.makedirs(IMAGES_DIR, exist_ok=True)
    date_str = TODAY.strftime("%Y-%m-%d")
    ts = datetime.now(CHINA_TZ).strftime("%H%M%S")
    fname = f"{date_str}-{ts}.tmp"  # 先写临时，验证后改名
    filepath = f"{IMAGES_DIR}/{fname}"

    # 三重策略尝试下载
    strategies = [
        # 策略 1：Referer + 浏览器 UA（最像真实访问）
        ["-H", f"User-Agent: {UA}", "-e", article_url],
        # 策略 2：仅浏览器 UA（有些站反 Referer）
        ["-H", f"User-Agent: {UA}"],
        # 策略 3：裸请求
        [],
    ]

    for strat in strategies:
        cmd = ["curl", "-sL", "-o", filepath, "-w", "%{http_code}",
               "--connect-timeout", "15"] + strat + [img_url]
        code = run(cmd)
        if code != "200":
            continue
        size = os.path.getsize(filepath)
        if size < 4096:  # 小于 4KB 很可能是错误页或占位图
            continue
        mime = run(["file", "-b", "--mime-type", filepath])
        if not mime.startswith("image/"):
            continue

        # 成功——按实际 mime 类型改名
        ext = "jpg"
        if "png" in mime: ext = "png"
        elif "webp" in mime: ext = "webp"
        elif "gif" in mime: ext = "gif"
        elif "svg" in mime: ext = "svg"
        final = f"{IMAGES_DIR}/{date_str}-{ts}.{ext}"
        os.rename(filepath, final)
        log(f"  📷 {os.path.basename(final)} ({size//1024}KB)")
        return f"{RAW_BASE}/{IMAGES_DIR}/{os.path.basename(final)}"

    run(["rm", "-f", filepath])
    return None


def extract_og_urls(article_url):
    """提取文章 og:image / twitter:image URL 列表."""
    urls = []
    try:
        html = run(["curl", "-sL", "-H", f"User-Agent: {UA}", "--connect-timeout", "10", article_url])
        if not html:
            return urls
    except Exception:
        return urls
    for pattern in [r'<meta\s+property="og:image"\s+content="([^"]+)"',
                    r'<meta\s+name="twitter:image"\s+content="([^"]+)"']:
        for m in re.finditer(pattern, html, re.IGNORECASE):
            img = m.group(1).replace("&amp;", "&")
            if img.startswith("http") and "social" not in img.lower() and "logo" not in img.lower():
                urls.append(img)
    return urls


def search_with_images(section, queries, needed=3):
    """搜索并下载图片，返回已拿到 GitHub Raw URL 的文章列表."""
    candidates = []
    for q in queries:
        if len(candidates) >= needed:
            break
        for r in search_web(q, max_results=5):
            if len(candidates) >= needed:
                break
            url = r.get("href", "")
            if not url.startswith("http"):
                continue
            img_urls = extract_og_urls(url)
            for img_url in img_urls:
                local_url = download_image(img_url, url)
                if local_url:
                    candidates.append({
                        "title": r.get("title", "").strip(),
                        "url": url,
                        "snippet": r.get("body", "").strip(),
                        "image": local_url,
                    })
                    log(f"{section} ✅ {candidates[-1]['title'][:50]}")
                    break  # 这篇文章拿到图了，换下一篇
    return candidates


# ── DeepSeek ──

def call_deepseek(prompt):
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
            return json.loads(resp.read().decode())["choices"][0]["message"]["content"]
    except Exception as e:
        log(f"DeepSeek 调用失败: {e}")
        return None


def parse_newsletter(ai_output):
    m = re.search(r'```(?:json)?\s*(\{.*?\})\s*```', ai_output, re.DOTALL)
    if m:
        try: return json.loads(m.group(1))
        except json.JSONDecodeError: pass
    try: return json.loads(ai_output)
    except json.JSONDecodeError: return None


# ── Markdown 组装 ──

def assemble_markdown(sections, articles):
    now_str = TODAY.strftime("%Y.%m.%d")
    lines = [f"# 🎨 每日灵感 {now_str}\n"]
    config = [
        ("壹观", "品牌 / UI / 创意案例"),
        ("贰知", "AI 资讯 / 工具 / 工作流"),
        ("叁赏", "艺术 / 建筑 / 摄影 / 作品"),
        ("肆律", "设计原则"),
        ("伍言", "名人名言"),
    ]
    for key, subtitle in config:
        item = sections.get(key, {})
        content = item.get("content", "")
        title = item.get("title", "")
        lines.append(f"## {key} · {subtitle}")
        if title:
            lines.append(f"**{title}**")
        if key in ("壹观", "贰知", "叁赏"):
            img = item.get("image", "")
            if not img:
                art = next((a for a in articles.get(key, []) if a.get("image")), None)
                img = art["image"] if art else ""
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


# ── 钉钉发送 ──

def send_dingtalk(text):
    payload_bytes = text.encode("utf-8")
    if len(payload_bytes) > 3500:
        log(f"内容过长 ({len(payload_bytes)} bytes)，截断")
        text = payload_bytes[:3400].decode("utf-8", errors="ignore")
        text = text[:text.rfind("\n")] + "\n\n*（内容截断）*"
    body = json.dumps({
        "msgtype": "markdown",
        "markdown": {"title": f"每日灵感 {TODAY.strftime('%Y.%m.%d')}", "text": text},
    }).encode()
    req = urllib.request.Request(DINGTALK_WEBHOOK, data=body)
    req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            result = json.loads(resp.read().decode())
            if result.get("errcode") == 0:
                log("✅ 钉钉发送成功")
                return True
            log(f"❌ 钉钉返回: {result}")
            return False
    except Exception as e:
        log(f"❌ 钉钉请求异常: {e}")
        return False


# ── Git 操作 ──

def git_push_images():
    """提交并推送下载的图片到 GitHub."""
    new_files = run(["git", "ls-files", "--others", "--exclude-standard", IMAGES_DIR])
    modified = run(["git", "diff", "--name-only", "--", IMAGES_DIR])
    if not new_files and not modified:
        return  # 没有新图片
    run(["git", "add", IMAGES_DIR])
    date_str = TODAY.strftime("%Y-%m-%d")
    run(["git", "commit", "-m", f"daily: {date_str} images", "--allow-empty"])
    r = subprocess.run(["git", "push"], capture_output=True, text=True)
    if r.returncode == 0:
        log("✅ 图片已推送到 GitHub")
    else:
        log(f"⚠️ 图片推送失败: {r.stderr[:200]}")


def cleanup_old_images():
    """清理 30 天前的旧图片."""
    cutoff = TODAY - timedelta(days=30)
    for f in glob.glob(f"{IMAGES_DIR}/*"):
        try:
            mtime = datetime.fromtimestamp(os.path.getmtime(f))
            if mtime < cutoff:
                os.remove(f)
                log(f"🗑 清理旧图: {f}")
        except Exception:
            pass


# ── 主流程 ──

def main():
    log(f"=== 每日灵感 {TODAY.strftime('%Y.%m.%d')} ===")

    # 1. 工作日检查
    if TODAY <= FULL_RUN_UNTIL:
        log(f"每日全发模式 (至 {FULL_RUN_UNTIL.strftime('%m/%d')})")
    elif not is_working_day():
        log("今天非工作日，跳过")
        return

    # 2. 搜索 + 下载图片
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

    # 3. 补搜
    missing = [k for k in ("壹观", "贰知", "叁赏") if not found_articles.get(k)]
    if missing:
        log(f"⚠️ {', '.join(missing)} 缺图，补搜")
        backup = {"壹观": "brand design", "贰知": "AI technology", "叁赏": "art design"}
        for k in missing:
            found_articles[k] = search_with_images(k, [f"{backup[k]} 2026"], needed=1)
            if not found_articles[k]:
                log(f"❌ {k} 实在找不到有图文章")

    # 4. 推送图片到 GitHub（提前推，确保 raw URL 可用）
    git_push_images()

    # 5. 构造 AI prompt
    prompt = f"""请撰写今日的「每日灵感」日报（{TODAY.strftime('%Y.%m.%d')}）。

## 格式要求
- **壹观**：品牌/UI/交互案例，120-180 字
- **贰知**：AI 资讯/工具/工作流，120-180 字
- **叁赏**：艺术/建筑/摄影，120-180 字
- **肆律**：一条设计原则 + 简介，50-80 字
- **伍言**：设计名人名言（外国人需双语），50-80 字
每条末尾标注原始来源链接。

## 今日素材
以下是为各板块找到的文章（配图已自动处理，你不需要关心 image 字段）：\n\n"""

    has_articles = False
    for section, articles in found_articles.items():
        prompt += f"### {section}\n"
        if articles:
            has_articles = True
            for a in articles:
                prompt += f"- 标题：{a['title']}\n  来源：{a['url']}\n  摘要：{a['snippet'][:200]}\n"
        else:
            prompt += "（请基于你的知识撰写）\n"
        prompt += "\n"

    if has_articles:
        prompt += "**image 字段填任意占位 URL 即可，系统会自动替换为正确配图。**\n"

    prompt += """请直接输出 JSON（含所有板块的内容、配图 URL、来源链接），格式：

```json
{
  "壹观": {"title": "案例标题", "content": "正文...", "image": "https://raw.githubusercontent.com/...", "url": "https://..."},
  "贰知": {"title": "案例标题", "content": "正文...", "image": "https://raw.githubusercontent.com/...", "url": "https://..."},
  "叁赏": {"title": "案例标题", "content": "正文...", "image": "https://raw.githubusercontent.com/...", "url": "https://..."},
  "肆律": {"content": "设计原则+简介", "url": "https://..."},
  "伍言": {"content": "名人名言（双语）", "url": "https://..."}
}
```"""

    # 6. 调用 DeepSeek
    log("调用 DeepSeek 生成内容...")
    ai_output = call_deepseek(prompt)
    if not ai_output:
        log("❌ AI 生成失败")
        sys.exit(1)
    log(f"AI 回复长度: {len(ai_output)} 字符")

    sections = parse_newsletter(ai_output)
    if not sections:
        log("❌ 解析 AI 输出失败")
        print(ai_output[:500])
        sys.exit(1)

    # 7. 硬编码图片分配——不信任 AI 的 image 输出，我们搜到什么就用什么
    image_map = {}  # section → image URL
    for k in ("壹观", "贰知", "叁赏"):
        articles = found_articles.get(k, [])
        if articles:
            image_map[k] = articles[0]["image"]
            log(f"  {k} 配图 → {articles[0]['title'][:40]}")
    for k in ("壹观", "贰知", "叁赏"):
        sections.setdefault(k, {})
        if k in image_map:
            sections[k]["image"] = image_map[k]
        # URL 也一样强制匹配
        articles = found_articles.get(k, [])
        if articles:
            sections[k]["url"] = articles[0]["url"]

    # 8. 组装并发送
    markdown = assemble_markdown(sections, found_articles)
    log(f"Markdown: {len(markdown.encode('utf-8'))} bytes")

    send_dingtalk(markdown)
    cleanup_old_images()
    log("✅ 每日灵感完毕")


# ── 复用 ──

def is_working_day():
    date_str = TODAY.strftime("%Y-%m-%d")
    url = f"https://raw.githubusercontent.com/NateScarlet/holiday-cn/master/{TODAY.year}.json"
    try:
        req = urllib.request.Request(url)
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read().decode())
        for day in data.get("days", []):
            if day.get("date") == date_str:
                is_off = day.get("isOffDay", False)
                log(f"节假日: {date_str} isOffDay={is_off} name={day.get('name','')}")
                return not is_off
        log(f"{date_str} 不在节假日列表，回退 weekday 检查")
        return TODAY.weekday() < 5
    except Exception as e:
        log(f"节假日 API 异常: {e}，回退 weekday")
        return TODAY.weekday() < 5


if __name__ == "__main__":
    main()
