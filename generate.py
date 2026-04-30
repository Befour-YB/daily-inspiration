#!/usr/bin/env python3
"""每日灵感 v1.4 — 精简版：提取 og:image → wsrv.nl 代理 → 钉钉"""

import json, os, re, subprocess, sys, urllib.request, urllib.parse
from datetime import datetime, timezone, timedelta

CHINA_TZ = timezone(timedelta(hours=8))
TODAY = datetime.now(CHINA_TZ)
VERSION = "v1.4"
FULL_RUN_UNTIL = datetime(2026, 5, 6, tzinfo=CHINA_TZ)

DEEPSEEK_API_KEY = os.environ.get("DEEPSEEK_API_KEY", "")
DINGTALK_TOKEN = os.environ.get("DINGTALK_TOKEN", "")
DINGTALK_WEBHOOK = f"https://oapi.dingtalk.com/robot/send?access_token={DINGTALK_TOKEN}"

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
      "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")


def log(msg):
    print(f"[{TODAY.strftime('%H:%M:%S')}] {msg}")


def run(cmd, timeout=30):
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    return r.stdout.strip()


def search_web(query, max_results=5):
    try:
        from duckduckgo_search import DDGS
        with DDGS() as ddgs:
            return list(ddgs.text(query, max_results=max_results, backend="html"))
    except Exception as e:
        log(f"搜索失败 [{query[:40]}]: {e}")
        return []


def extract_best_image(article_url):
    """用浏览器 UA 读文章 HTML，提取 og:image / twitter:image 并验证可用."""
    try:
        html = run(["curl", "-sL", "-H", f"User-Agent: {UA}",
                    "--connect-timeout", "10", article_url])
        if not html:
            return None
    except Exception:
        return None

    candidates = []
    for pattern in [
        r'<meta\s+property="og:image"\s+content="([^"]+)"',
        r'<meta\s+name="twitter:image"\s+content="([^"]+)"',
    ]:
        for m in re.finditer(pattern, html, re.IGNORECASE):
            url = m.group(1).replace("&amp;", "&")
            if url.startswith("http") and "social" not in url.lower() and "logo" not in url.lower():
                candidates.append(url)

    # 验证：直接 GET 下载（带 UA，不验证 content-type，只要 200 就行）
    for url in candidates:
        code = run(["curl", "-sL", "-o", "/dev/null", "-w", "%{http_code}",
                    "-H", f"User-Agent: {UA}", "--connect-timeout", "8", url])
        if code == "200":
            return url
    return None


def proxy_url(raw_url):
    """wsrv.nl → weserv.nl → 原始 URL 三级降级代理."""
    encoded = urllib.parse.quote(raw_url, safe='')
    proxies = [
        f"https://wsrv.nl/?url={encoded}",
        f"https://images.weserv.nl/?url={encoded}&output=webp",
    ]
    for p in proxies:
        code = run(["curl", "-sL", "-o", "/dev/null", "-w", "%{http_code}",
                    "--connect-timeout", "8", p])
        if code == "200":
            return p
    # 两个代理都失效，返回原始 URL 碰运气
    return raw_url


def search_with_images(section, queries, needed=3):
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
            img = extract_best_image(url)
            if img:
                candidates.append({
                    "title": r.get("title", "").strip(),
                    "url": url,
                    "snippet": r.get("body", "").strip(),
                    "image": proxy_url(img),
                })
                log(f"{section} ✅ {candidates[-1]['title'][:50]}")
    return candidates


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
        log(f"DeepSeek 失败: {e}")
        return None


def parse_newsletter(ai_output):
    m = re.search(r'```(?:json)?\s*(\{.*?\})\s*```', ai_output, re.DOTALL)
    if m:
        try: return json.loads(m.group(1))
        except json.JSONDecodeError: pass
    try: return json.loads(ai_output)
    except json.JSONDecodeError: return None


def assemble_markdown(sections, articles):
    now = TODAY.strftime("%Y.%m.%d")
    lines = [f"# 🎨 每日灵感 {now}\n"]
    config = [
        ("壹观", "品牌 / UI / 创意案例"),
        ("贰知", "AI 资讯 / 工具 / 工作流"),
        ("叁赏", "艺术 / 建筑 / 摄影 / 作品"),
        ("肆律", "设计原则"),
        ("伍言", "名人名言"),
    ]
    for key, sub in config:
        item = sections.get(key, {})
        lines.append(f"## {key} · {sub}")
        if item.get("title"):
            lines.append(f"**{item['title']}**")
        # 前 3 条配图——发送前最终验证，不通则宁缺毋滥
        if key in ("壹观", "贰知", "叁赏"):
            img = item.get("image", "")
            if not img:
                art = next((a for a in articles.get(key, []) if a.get("image")), None)
                img = art["image"] if art else ""
            if img:
                ok = run(["curl", "-sL", "-o", "/dev/null", "-w", "%{http_code}",
                          "--connect-timeout", "6", img])
                if ok == "200":
                    lines.append(f"![]({img})")
                else:
                    log(f"  ⚠️ {key} 配图 HTTP {ok}，跳过")
        lines.append(item.get("content", ""))
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
    b = text.encode("utf-8")
    if len(b) > 3500:
        log(f"过长 ({len(b)} bytes)，截断")
        text = b[:3400].decode("utf-8", errors="ignore")
        text = text[:text.rfind("\n")] + "\n\n*（内容截断）*"
    body = json.dumps({
        "msgtype": "markdown",
        "markdown": {"title": f"每日灵感 {TODAY.strftime('%Y.%m.%d')}", "text": text},
    }).encode()
    req = urllib.request.Request(DINGTALK_WEBHOOK, data=body)
    req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            r = json.loads(resp.read().decode())
            if r.get("errcode") == 0:
                log("✅ 发送成功")
                return True
            log(f"❌ 钉钉: {r}")
            return False
    except Exception as e:
        log(f"❌ 异常: {e}")
        return False


def is_working_day():
    date_str = TODAY.strftime("%Y-%m-%d")
    url = f"https://raw.githubusercontent.com/NateScarlet/holiday-cn/master/{TODAY.year}.json"
    try:
        req = urllib.request.Request(url)
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read().decode())
        for d in data.get("days", []):
            if d.get("date") == date_str:
                off = d.get("isOffDay", False)
                log(f"节假日: {date_str} isOffDay={off}")
                return not off
        return TODAY.weekday() < 5
    except Exception as e:
        log(f"节假日 API 异常: {e}")
        return TODAY.weekday() < 5


def main():
    log(f"=== 每日灵感 {TODAY.strftime('%Y.%m.%d')} ===")

    if TODAY <= FULL_RUN_UNTIL:
        log(f"全发模式 (至 {FULL_RUN_UNTIL.strftime('%m/%d')})")
    elif not is_working_day():
        log("非工作日，跳过")
        return

    search_config = {
        "壹观": [
            "branding identity rebrand case study 2026",
            "creative brand design identity 2026 -pinterest",
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

    found = {}
    for sec, queries in search_config.items():
        log(f"搜索 {sec}...")
        found[sec] = search_with_images(sec, queries)
        log(f"  → {len(found[sec])} 篇有图")

    missing = [k for k in ("壹观", "贰知", "叁赏") if not found.get(k)]
    if missing:
        log(f"⚠️ {', '.join(missing)} 缺图，补搜")
        backup = {"壹观": "brand design", "贰知": "AI technology", "叁赏": "art design"}
        for k in missing:
            found[k] = search_with_images(k, [f"{backup[k]} 2026"], needed=1)

    # 构造 prompt
    prompt = f"撰写今日「每日灵感」日报 ({TODAY.strftime('%Y.%m.%d')})。\n\n"
    prompt += "格式：壹观(品牌/UI 120-180字) 贰知(AI资讯 120-180字) 叁赏(艺术/建筑/摄影 120-180字) 肆律(设计原则 50-80字) 伍言(名人名言双语 50-80字)\n"
    prompt += "每条末尾标注原始来源链接。\n\n## 素材\n"

    for sec, arts in found.items():
        prompt += f"### {sec}\n"
        if arts:
            for a in arts:
                prompt += f"- {a['title']}\n  来源:{a['url']}\n  摘要:{a['snippet'][:200]}\n"
        else:
            prompt += "（基于知识撰写）\n"
        prompt += "\n"

    prompt += "输出 JSON（image 字段填任意占位，系统会自动替换）：\n"
    prompt += '```json\n{"壹观":{"title":"","content":"","image":"https://placeholder","url":"https://..."},'
    prompt += '"贰知":{...},"叁赏":{...},"肆律":{"content":"","url":""},"伍言":{"content":"","url":""}}\n```'

    log("调用 DeepSeek...")
    ai_output = call_deepseek(prompt)
    if not ai_output:
        log("❌ AI 失败")
        sys.exit(1)

    sections = parse_newsletter(ai_output)
    if not sections:
        log("❌ 解析失败")
        print(ai_output[:500])
        sys.exit(1)

    # 硬编码分配图片：按 section 独立，不信任 AI
    for k in ("壹观", "贰知", "叁赏"):
        sections.setdefault(k, {})
        arts = found.get(k, [])
        if arts:
            sections[k]["image"] = arts[0]["image"]
            sections[k]["url"] = arts[0]["url"]
            log(f"  {k} → {arts[0]['title'][:40]}")

    markdown = assemble_markdown(sections, found)
    log(f"Markdown: {len(markdown.encode('utf-8'))} bytes")
    send_dingtalk(markdown)
    log("✅ 完成")


if __name__ == "__main__":
    main()
