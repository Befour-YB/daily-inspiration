#!/usr/bin/env python3
"""每日灵感 v1.5 — 白名单模式：仅从信任站点取内容"""

import json, os, re, subprocess, sys, time, urllib.request, urllib.parse
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
        from ddgs import DDGS
        with DDGS() as ddgs:
            return list(ddgs.text(query, max_results=max_results))
    except ImportError:
        try:
            from duckduckgo_search import DDGS
            with DDGS() as ddgs:
                return list(ddgs.text(query, max_results=max_results, backend="html"))
        except Exception as e:
            log(f"搜索失败 [{query[:40]}]: {e}")
            return []
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


# 白名单 — 按板块划分，只从这些站点取内容
TRUSTED_DOMAINS = {
    "壹观": [  # 品牌创意
        "behance.net", "dribbble.com",
        "underconsideration.com", "itsnicethat.com", "creativeboom.com",
        "zcool.com.cn",
        "logonews.cn", "identitydesigned.com", "bpando.org",
        "awwwards.com",
    ],
    "贰知": [  # AI 资讯
        "techcrunch.com", "arstechnica.com",
        "36kr.com", "the-decoder.com",
    ],
    "叁赏": [  # 艺术作品
        "dezeen.com", "archdaily.com", "designboom.com",
        "thisiscolossal.com", "gooood.cn",
    ],
}

def search_with_images(section, queries, trusted_domains, needed=3):
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
            # 白名单模式：只保留信任站点
            domain = urllib.parse.urlparse(url).netloc.lower()
            if not any(d in domain for d in trusted_domains):
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
        "max_tokens": 3000,
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
    """尝试解析 AI 输出的 JSON，失败时从半成品 JSON 中尽量提取有效段落."""
    # 尝试完整 JSON
    m = re.search(r'```(?:json)?\s*(\{.*?\})\s*```', ai_output, re.DOTALL)
    if m:
        try: return json.loads(m.group(1))
        except json.JSONDecodeError: pass
    try: return json.loads(ai_output)
    except json.JSONDecodeError: pass

    # 完整解析失败——从截断的 JSON 中逐个提取 section（用正则按 section key 抠）
    log("完整 JSON 解析失败，尝试逐板块提取...")
    sections = {}
    for key in ("壹观", "贰知", "叁赏", "肆律", "伍言"):
        # 匹配 "壹观": { ... } 直到下一个 "key": 或结束
        pat = rf'"{key}"\s*:\s*\{{'
        m = re.search(pat, ai_output)
        if not m:
            continue
        # 找到该 section 的起始位置
        start = m.start()
        # 找该 section 的 content 和 url
        content_m = re.search(rf'"{key}"\s*:\s*\{{.*?"content"\s*:\s*"([^"]*(?:\\.[^"]*)*)"', ai_output[start:], re.DOTALL)
        title_m = re.search(rf'"{key}"\s*:\s*\{{.*?"title"\s*:\s*"([^"]*(?:\\.[^"]*)*)"', ai_output[start:], re.DOTALL)
        url_m = re.search(rf'"{key}"\s*:\s*\{{.*?"url"\s*:\s*"([^"]*(?:\\.[^"]*)*)"', ai_output[start:], re.DOTALL)
        section = {}
        if content_m:
            section["content"] = content_m.group(1).replace('\\n', '\n').replace('\\"', '"')
        if title_m:
            section["title"] = title_m.group(1).replace('\\"', '"')
        if url_m:
            section["url"] = url_m.group(1)
        if section.get("content"):
            sections[key] = section
            log(f"  ✅ 提取 {key}")
    return sections if sections else None


def assemble_markdown(sections, articles):
    now = TODAY.strftime("%Y.%m.%d")
    lines = [f"# 🎨 每日灵感 {now}\n"]
    config = [
        ("壹观", "品牌创意"),
        ("贰知", "AI 资讯"),
        ("叁赏", "艺术作品"),
        ("肆律", ""),
        ("伍言", ""),
    ]
    for key, sub in config:
        item = sections.get(key, {})
        if sub:
            lines.append(f"## {key} · {sub}")
        else:
            lines.append(f"## {key}")
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
        "壹观": [  # 品牌创意 + 包装/logo 案例
            "branding rebrand identity design case study",
            "packaging design award label brand",
            "logo redesign brand identity case study",
            "UI UX design award showcase inspiration",
            "site:behance.net brand identity design",
            "site:underconsideration.com brand",
        ],
        "贰知": [
            "AI artificial intelligence new tool product launch",
            "AI agent workflow automation startup",
            "site:techcrunch.com AI startup product",
        ],
        "叁赏": [
            "architecture design exhibition installation",
            "contemporary art sculpture photography",
            "site:dezeen.com architecture design",
            "site:thisiscolossal.com art design",
        ],
    }

    found = {}
    for sec, queries in search_config.items():
        log(f"搜索 {sec}...")
        found[sec] = search_with_images(sec, queries, TRUSTED_DOMAINS.get(sec, []))
        log(f"  → {len(found[sec])} 篇有图")
        time.sleep(2)  # 避免 DDG 限流

    missing = [k for k in ("壹观", "贰知", "叁赏") if not found.get(k)]
    if missing:
        log(f"⚠️ {', '.join(missing)} 缺图，补搜")
        backup = {"壹观": "品牌 设计 案例", "贰知": "AI 人工智能 资讯", "叁赏": "建筑 设计 艺术"}
        for k in missing:
            found[k] = search_with_images(k, [f"{backup[k]} 2026"], TRUSTED_DOMAINS.get(k, []), needed=1)

    # 构造 prompt
    prompt = f"撰写今日「每日灵感」日报 ({TODAY.strftime('%Y.%m.%d')})。\n\n"

    prompt += "## 严格规则\n"
    prompt += "0. **必须使用简体中文，禁止繁体字**\n"
    prompt += "1. 壹观/贰知/叁赏：各只写 ONE 个案例，素材区每版块第一篇文章即指定案例，必须围绕它撰写\n"
    prompt += "   禁止使用课程推广、付费培训、广告营销类内容，必须是资讯/案例\n"
    prompt += "2. 肆律：一条泛设计原则（如「少即是多」「形式追随功能」），一句话简介\n"
    prompt += "3. 伍言：ONE 条创意/设计圈名人名言，格式为「名言」—— 作者（职业身份）\n"
    prompt += "   - 外国作者 → 必须双语：原文 + 中文翻译\n"
    prompt += "   - 中国作者 → 仅中文\n"
    prompt += "   - 举例：「少即是多」—— 路德维希·密斯·凡德罗（德国现代主义建筑大师）\n"
    prompt += '   - 外国举例："Less is more." —— Ludwig Mies van der Rohe（德国现代主义建筑大师） / 「少即是多。」\n'

    prompt += "## 素材（壹观/贰知/叁赏 各版块第一篇文章即你该写的案例）\n"

    for sec, arts in found.items():
        if sec in ("壹观", "贰知", "叁赏"):
            prompt += f"### {sec}\n"
            if arts:
                a = arts[0]  # 只给第一篇，减少 AI 混乱
                prompt += f"指定案例：{a['title']}\n来源：{a['url']}\n摘要：{a['snippet'][:300]}\n\n"
            else:
                prompt += "（无指定素材，基于你的知识撰写，url 留空）\n\n"
        else:
            prompt += f"### {sec}（基于你的知识撰写）\n\n"

    prompt += "输出 JSON，image 字段填任意占位：\n"
    prompt += '```json\n{"壹观":{"title":"案例标题","content":"120-180字","image":"x","url":"来源URL"},'
    prompt += '"贰知":{...},"叁赏":{...},"肆律":{"content":"设计原则+一句话简介"},"伍言":{"content":"名言——作者（职业）"}}\n```'

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

    # 硬编码分配：按 section 独立，不信任 AI 的图片/链接/标题
    for k in ("壹观", "贰知", "叁赏"):
        sections.setdefault(k, {})
        arts = found.get(k, [])
        if arts:
            sections[k]["image"] = arts[0]["image"]
            sections[k]["url"] = arts[0]["url"]
            sections[k]["title"] = arts[0]["title"]  # 强制标题匹配
            log(f"  {k} → {arts[0]['title'][:40]}")
        else:
            sections[k]["url"] = ""
            sections[k]["image"] = ""
            log(f"  {k} 无真实来源，清空图片和链接")

    markdown = assemble_markdown(sections, found)
    log(f"Markdown: {len(markdown.encode('utf-8'))} bytes")
    send_dingtalk(markdown)
    log("✅ 完成")


if __name__ == "__main__":
    main()
