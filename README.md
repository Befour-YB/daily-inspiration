# 每日灵感

创意审美灵感日报，每个工作日 9:30 自动发送到钉钉群 YBL。

## 架构

```
GitHub Actions (9:30 UTC+8, Mon-Fri)
  → 查节假日 API → 非工作日则跳过
  → DuckDuckGo 搜索设计/AI/艺术案例
  → curl 提取文章 og:image 并验证
  → DeepSeek API 生成日报内容
  → curl 发送 Markdown 到钉钉 Webhook
```

## 配置步骤

### 第 1 步：在 GitHub 上创建仓库

1. 打开 https://github.com/new
2. 仓库名填 `daily-inspiration` 或你喜欢的名字
3. 选 **Private**（私有仓库）
4. 点 **Create repository**

### 第 2 步：推送代码

```bash
# 在终端执行
cd /Users/elfsys.yb/claude/Claude\ test/daily-inspiration
git init
git add .
git commit -m "初始化每日灵感"
git branch -M main
git remote add origin https://github.com/你的用户名/daily-inspiration.git
git push -u origin main
```

### 第 3 步：配置 Secrets

在 GitHub 仓库页面：
1. 点 **Settings** → **Secrets and variables** → **Actions**
2. 点 **New repository secret**，添加以下两个：

| Name | Value |
|------|-------|
| `DEEPSEEK_API_KEY` | 你的 DeepSeek API key |
| `DINGTALK_TOKEN` | `c569247c55429203006b4523a939dedf2d3b3c239d3a311bf7c1cb2d1a8210fa` |

### 第 4 步：手动触发测试

1. 点 **Actions** 标签
2. 在 **每日灵感** workflow 右侧点 **Run workflow**
3. 等待 1-2 分钟
4. 检查 YBL 群是否收到日报

### 第 5 步：确认自动运行

第一次推送后，明天 9:30 会自动触发。也可以随时点 Run workflow 手动跑。

## 费用

| 项目 | 费用 | 说明 |
|------|------|------|
| GitHub Actions | 免费 | 每月 2000 分钟，日报只用 ~35 分钟 |
| DeepSeek API | ~¥0.0005/次 | 几乎免费 |
| DuckDuckGo 搜索 | 免费 | 无需 API key |

## 维护

- 脚本在 `generate.py`，可随时修改
- 提交推送后自动生效下次运行
- 钉钉 Webhook token 在 GitHub Secrets 里，安全隔离
