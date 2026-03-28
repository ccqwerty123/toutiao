import asyncio
import json
import random
import time
import math
import sys
from datetime import datetime
from pathlib import Path

from playwright.async_api import async_playwright, Page, BrowserContext

# ================= 依赖库检测 =================
try:
    from playwright_stealth import stealth_async
    HAS_STEALTH = True
except ImportError:
    HAS_STEALTH = False
    print("================================================================")
    print(f"[WARN] 未安装 playwright-stealth 库。")
    print(f"[WARN] 建议运行: pip install playwright-stealth 以降低被检测风险。")
    print("================================================================")

# ================= 配置区域 =================

TOUTIAO_URL = "https://www.toutiao.com/c/user/token/CiyRLPHkUyTCD9FmHodOGQVcmZh5-NRKyfiTSF0XMms-tSja0FdhrUWRp-T-DBpJCjwAAAAAAAAAAAAAT8lExjCbDHcWTgszQQjqU0Ohh9qtuXbuEOe6CQdqJEZ7yIpoM-NJ93_Sty1iMpOe_FUQ9ZmDDhjDxYPqBCIBA9GPpzc="

DATA_DIR = Path("data")
DATA_DIR.mkdir(parents=True, exist_ok=True)

DB_FILE = DATA_DIR / "toutiao_db.json"
DEBUG_DIR = DATA_DIR / "debug"
DEBUG_DIR.mkdir(parents=True, exist_ok=True)

REPORT_FILE = DEBUG_DIR / "read_report.html"

MAX_READ_COUNT = 30
MIN_READ_COUNT = 5
MAX_SYNC_SCROLLS = 20
AGING_THRESHOLD = 50
MAX_RETRIES = 3


# ================= User-Agent 管理 =================

FALLBACK_PC_UAS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/119.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/118.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36 Edg/120.0.0.0",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/119.0.0.0 Safari/537.36 Edg/119.0.0.0",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, Like Gecko) Chrome/119.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.2 Safari/605.1.15",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/16.6 Safari/605.1.15",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
]

def get_pc_user_agent():
    ua = ""
    try:
        from real_useragent import UserAgent
        rua = UserAgent()
        ua = rua.desktop_useragent()
        if not ua or len(ua) < 20:
            raise ValueError("UA too short")
    except Exception:
        ua = random.choice(FALLBACK_PC_UAS)
    return ua

VIEWPORTS = [
    {"width": 1920, "height": 1080},
    {"width": 1536, "height": 864},
    {"width": 1440, "height": 900},
    {"width": 1366, "height": 768},
    {"width": 1280, "height": 720},
]

# ================= JS 注入脚本 =================

EXTRACT_LINKS_JS = r"""
() => {
  const anchors = Array.from(document.querySelectorAll("a[href]"));
  const origin = window.location.origin;
  const results = [];
  const seen = new Set();

  const isArticle = (path) => {
    if (!path) return false;
    if (path.startsWith("/c/user/")) return false;
    if (path.startsWith("/search/")) return false;
    if (path.includes("toutiao_search")) return false;
    const lastPart = path.split("/").filter(Boolean).pop();
    if (!lastPart) return false;
    const digits = lastPart.replace(/\D/g, "").length;
    return digits > 5;
  };

  const getText = (el) => {
    if (!el) return "";
    let txt = (el.innerText || "").trim();
    if (txt) return txt;
    txt = (el.getAttribute("aria-label") || "").trim();
    if (txt) return txt;
    txt = (el.getAttribute("title") || "").trim();
    if (txt) return txt;
    const img = el.querySelector("img");
    if (img) { txt = (img.getAttribute("alt") || "").trim(); }
    return txt;
  };

  const truncateText = (text, maxLength = 50) => {
    if (!text || text.length <= maxLength) return text;
    return text.substring(0, maxLength) + "...";
  };

  const getContentType = (url) => {
    if (url.includes("/article/")) return "article";
    if (url.includes("/w/")) return "weitoutiao";
    if (url.includes("/video/")) return "video";
    return "unknown";
  };

  const extractTitle = (a, urlObj) => {
    let text = getText(a);
    const contentType = getContentType(urlObj.pathname);

    if (!text || text.length < 4) {
        let container = a.closest('.feed-card-wrapper, .article-card, .feed-card-article-wrapper, .card-wrapper, .weitoutiao-wrap, .wtt-content');
        if (!container) {
            if (a.parentElement && a.parentElement.parentElement && a.parentElement.parentElement.parentElement) {
                container = a.parentElement.parentElement.parentElement;
            }
        }
        if (container) {
            if (contentType === "weitoutiao") {
                const contentEl = container.querySelector('.weitoutiao-content, .wtt-content, .feed-card-article-content, [class*="content"]');
                if (contentEl) {
                    const content = contentEl.innerText.trim();
                    if (content) { text = truncateText(content, 40); }
                }
            } else if (contentType === "video") {
                const titleEl = container.querySelector('.video-title, .title, [class*="title"]');
                if (titleEl) {
                    const t = titleEl.innerText.trim();
                    if (t) text = t;
                }
            }
            if (!text || text.length < 4) {
                const selectors = [
                    '.title', '.feed-card-article-title', '.article-title', '.feed-card-article-l a',
                    '[class*="title"]', 'h1, h2, h3', '.text', 'p'
                ];
                for (const selector of selectors) {
                    const el = container.querySelector(selector);
                    if (el) {
                        const t = el.innerText.trim();
                        if (t && t.length > 4) { text = truncateText(t, 50); break; }
                    }
                }
            }
            if (!text || text.length < 4) {
                const allTexts = container.innerText.trim().split('\n').filter(t => t.trim().length > 4);
                if (allTexts.length > 0) { text = truncateText(allTexts[0], 50); }
            }
        }
    }

    if (!text || text === "Untitled") {
        if (contentType === "weitoutiao") text = "[微头条]";
        else if (contentType === "video") text = "[视频]";
    }

    return { text: text || "Untitled", contentType };
  };

  for (const a of anchors) {
    let href = a.getAttribute("href");
    if (!href) continue;
    if (href.startsWith("/")) href = origin + href;
    try {
        const urlObj = new URL(href);
        if (!urlObj.hostname.includes("toutiao.com")) continue;
        if (!isArticle(urlObj.pathname)) continue;
        const cleanUrl = urlObj.origin + urlObj.pathname;
        if (seen.has(cleanUrl)) continue;
        const titleInfo = extractTitle(a, urlObj);
        let text = titleInfo.text;
        const filterKeywords = [
            '跟帖评论自律管理承诺书', '用户协议', '隐私政策',
            '侵权投诉', '网络谣言曝光台', '违法和不良信息举报', '侵权举报受理公示'
        ];
        if (filterKeywords.some(keyword => text.includes(keyword))) continue;
        if (!text.startsWith('[') && text.match(/^(备案|举报|登录|下载|广告|相关推荐|搜索)$/)) continue;
        seen.add(cleanUrl);
        results.push({ text: text, href: cleanUrl, type: titleInfo.contentType });
    } catch(e) {}
  }
  return results;
}
"""

# ================= HTML 报告 =================

def _ensure_report():
    """确保报告文件存在且有完整 HTML 框架"""
    if not REPORT_FILE.exists():
        REPORT_FILE.write_text("""\
<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<title>头条阅读 Debug 报告</title>
<style>
  body { font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
         font-size: 13px; padding: 24px; background: #f5f5f5; color: #333; }
  h2   { margin: 0 0 16px; font-size: 18px; font-weight: 500; }
  .meta { color: #888; font-size: 12px; margin-bottom: 16px; }
  table { border-collapse: collapse; width: 100%; background: #fff;
          border-radius: 8px; overflow: hidden;
          box-shadow: 0 1px 4px rgba(0,0,0,.08); }
  th   { background: #1a73e8; color: #fff; padding: 10px 14px;
         text-align: left; font-weight: 500; white-space: nowrap; }
  td   { padding: 9px 14px; border-bottom: 1px solid #eee; vertical-align: middle; }
  tr:last-child td { border-bottom: none; }
  tr:hover td { background: #f0f6ff; }
  a    { color: #1a73e8; text-decoration: none; word-break: break-all; }
  a:hover { text-decoration: underline; }
  .tag { display: inline-block; padding: 2px 8px; border-radius: 10px;
         font-size: 11px; font-weight: 500; }
  .tag-success { background: #e6f4ea; color: #1e7e34; }
  .tag-failed  { background: #fce8e6; color: #c5221f; }
  .tag-invalid { background: #f1f3f4; color: #5f6368; }
  .tag-captcha { background: #fef7e0; color: #b06000; }
  img.thumb { max-width: 160px; max-height: 90px; border-radius: 4px;
              border: 1px solid #ddd; display: block; cursor: pointer; }
</style>
<script>
function openImg(src){
  const w = window.open('','_blank');
  w.document.write('<img src="'+src+'" style="max-width:100%">');
}
</script>
</head>
<body>
<h2>📋 头条阅读 Debug 报告</h2>
<p class="meta">文件位置: data/debug/read_report.html &nbsp;|&nbsp; 点击缩略图可放大查看</p>
<table>
<thead><tr>
  <th>时间</th>
  <th>文章链接</th>
  <th>标题</th>
  <th>状态</th>
  <th>备注</th>
  <th>截图</th>
</tr></thead>
<tbody>
<!-- ROWS -->
</tbody>
</table>
</body>
</html>
""", encoding="utf-8")


def append_report(article_url: str, title: str, ss_path: str, status: str, note: str = ""):
    """
    向 HTML 报告追加一行：可点击的文章链接 + 状态标签 + 缩略截图。
    ss_path 传空字符串表示无截图。
    """
    _ensure_report()

    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    tag_class = {
        "success": "tag-success",
        "failed":  "tag-failed",
        "invalid": "tag-invalid",
        "captcha": "tag-captcha",
    }.get(status, "tag-failed")

    status_label = {
        "success": "✅ 成功",
        "failed":  "❌ 失败",
        "invalid": "⚠️ 失效",
        "captcha": "🔒 验证码",
    }.get(status, status)

    # 截图使用同目录下的文件名（相对路径），方便浏览器直接加载
    if ss_path:
        img_name = Path(ss_path).name
        img_html = (
            f'<img class="thumb" src="{img_name}" '
            f'onclick="openImg(\'{img_name}\')" '
            f'title="点击放大">'
        )
    else:
        img_html = "<span style='color:#ccc'>—</span>"

    # 标题截断，防止列过宽
    title_safe = (title or "")[:50].replace("<", "&lt;").replace(">", "&gt;")
    note_safe  = (note  or "")[:100].replace("<", "&lt;").replace(">", "&gt;")

    row = (
        f"<tr>"
        f"<td style='white-space:nowrap;color:#888'>{timestamp}</td>"
        f"<td><a href='{article_url}' target='_blank'>{article_url}</a></td>"
        f"<td title='{title_safe}'>{title_safe}</td>"
        f"<td><span class='tag {tag_class}'>{status_label}</span></td>"
        f"<td>{note_safe}</td>"
        f"<td>{img_html}</td>"
        f"</tr>\n"
    )

    content = REPORT_FILE.read_text(encoding="utf-8")
    content = content.replace("<!-- ROWS -->", row + "<!-- ROWS -->")
    REPORT_FILE.write_text(content, encoding="utf-8")


# ================= 数据库管理类 =================

class ArticleDB:
    def __init__(self, db_path: Path):
        self.db_path = db_path
        self.data = self._load()

    def _load(self):
        if not self.db_path.exists():
            return {"last_sync_date": "", "articles": {}}
        try:
            return json.loads(self.db_path.read_text(encoding="utf-8"))
        except Exception as e:
            print(f"[DB] 读取数据库出错: {e}，将初始化新库")
            return {"last_sync_date": "", "articles": {}}

    def save(self):
        try:
            self.db_path.write_text(
                json.dumps(self.data, indent=2, ensure_ascii=False),
                encoding="utf-8"
            )
        except Exception as e:
            print(f"[DB] 保存失败: {e}")

    def needs_sync(self) -> bool:
        today = datetime.now().strftime("%Y-%m-%d")
        return self.data.get("last_sync_date") != today

    def mark_synced(self):
        self.data["last_sync_date"] = datetime.now().strftime("%Y-%m-%d")
        self.save()

    def add_articles(self, scraped_items: list):
        added_count = 0
        current_urls = self.data["articles"]
        for item in scraped_items:
            url = item['href']
            if url not in current_urls:
                current_urls[url] = {
                    "title": item['text'],
                    "url": url,
                    "status": "active",
                    "last_read_at": "",
                    "read_count": 0
                }
                added_count += 1
            elif current_urls[url]["title"] == "Untitled" and item['text'] != "Untitled":
                current_urls[url]["title"] = item['text']
        print(f"[DB] 数据库更新: 新增 {added_count} 篇，当前总库存 {len(current_urls)} 篇")
        self.save()

    def mark_invalid(self, url):
        if url in self.data["articles"]:
            self.data["articles"][url]["status"] = "invalid"
            print(f"[DB] 链接标记为无效: {url}")
            self.save()

    def record_read(self, url):
        if url in self.data["articles"]:
            today = datetime.now().strftime("%Y-%m-%d")
            entry = self.data["articles"][url]
            entry["last_read_at"] = today
            entry["read_count"] = entry.get("read_count", 0) + 1
            self.save()

    def get_weighted_candidates(self) -> list:
        today = datetime.now().strftime("%Y-%m-%d")
        candidates = []
        weights = []
        active_urls = [k for k, v in self.data["articles"].items() if v.get("status") == "active"]
        for url in active_urls:
            info = self.data["articles"][url]
            if info.get("last_read_at") == today:
                continue
            read_count = info.get("read_count", 0)
            if read_count == 0:
                w = 200
            elif read_count < 5:
                w = 100
            elif read_count < 20:
                w = 50
            elif read_count < AGING_THRESHOLD:
                w = 20
            else:
                w = 5
            candidates.append(info)
            weights.append(w)

        if not candidates:
            return []

        target_k = random.randint(MIN_READ_COUNT, MAX_READ_COUNT)
        target_k = min(target_k, len(candidates))
        print(f"[PLAN] 可选文章库: {len(candidates)} 篇. 计划阅读: {target_k} 篇")

        selected = []
        temp_cand = list(candidates)
        temp_weight = list(weights)
        for _ in range(target_k):
            if not temp_cand:
                break
            chosen = random.choices(temp_cand, weights=temp_weight, k=1)[0]
            selected.append(chosen)
            idx = temp_cand.index(chosen)
            temp_cand.pop(idx)
            temp_weight.pop(idx)
        return selected


# ================= 拟人化操作函数 =================

async def human_delay(min_s=1.0, max_s=3.0):
    await asyncio.sleep(random.uniform(min_s, max_s))


async def human_mouse_move(page: Page, x_target, y_target, steps=25):
    try:
        start_x = random.randint(100, 1000)
        start_y = random.randint(100, 600)
        ctrl_x1 = start_x + (x_target - start_x) * 0.3 + random.randint(-50, 50)
        ctrl_y1 = start_y + (y_target - start_y) * 0.3 + random.randint(-50, 50)
        ctrl_x2 = start_x + (x_target - start_x) * 0.7 + random.randint(-50, 50)
        ctrl_y2 = start_y + (y_target - start_y) * 0.7 + random.randint(-50, 50)
        for i in range(steps + 1):
            t = i / steps
            x = (1-t)**3 * start_x + 3*(1-t)**2*t*ctrl_x1 + 3*(1-t)*t**2*ctrl_x2 + t**3*x_target
            y = (1-t)**3 * start_y + 3*(1-t)**2*t*ctrl_y1 + 3*(1-t)*t**2*ctrl_y2 + t**3*y_target
            x += random.uniform(-2, 2)
            y += random.uniform(-2, 2)
            await page.mouse.move(x, y)
            await asyncio.sleep(random.uniform(0.005, 0.015))
    except Exception:
        pass


async def human_scroll(page: Page, max_scrolls=1):
    for _ in range(max_scrolls):
        delta_y = random.randint(300, 700)
        await page.mouse.wheel(0, delta_y)
        await human_delay(1.0, 2.5)
        if random.random() < 0.2:
            await page.mouse.wheel(0, -random.randint(100, 250))
            await human_delay(0.5, 1.2)


async def check_captcha(page: Page, tag="unknown") -> bool:
    try:
        title = await page.title()
        is_captcha = False
        if any(kw in title for kw in ["验证", "安全检测", "captcha", "verify"]):
            is_captcha = True
        if not is_captcha:
            if await page.query_selector("#captcha-verify-image") or \
               await page.query_selector(".captcha_verify_container"):
                is_captcha = True
        if is_captcha:
            print(f"[ALERT] {tag} 阶段检测到验证码! Title: {title}")
            screenshot_path = DEBUG_DIR / f"captcha_{tag}_latest.png"
            await page.screenshot(path=screenshot_path)
            print(f"[ALERT] 验证码截图已保存: {screenshot_path}")
            return True
        return False
    except Exception as e:
        print(f"[WARN] 验证码检测出错: {e}")
        return False


# ================= 核心任务逻辑 =================

async def sync_task(context: BrowserContext, db: ArticleDB):
    """
    全量同步任务：抓取个人主页文章列表，写入数据库。
    """
    print(">>> [SYNC] 开始执行全量同步任务...")

    current_count = len(db.data.get("articles", {}))
    FULL_SYNC_THRESHOLD = 100
    is_full_sync = current_count < FULL_SYNC_THRESHOLD

    if is_full_sync:
        print(f"[SYNC] 📦 全量模式：当前库存 {current_count} 篇 < {FULL_SYNC_THRESHOLD}，将尽可能抓取所有文章")
        max_scroll_rounds = 50
        early_stop_count = 80
        no_new_threshold = 5
    else:
        print(f"[SYNC] 🔄 增量模式：当前库存 {current_count} 篇，快速更新即可")
        max_scroll_rounds = 15
        early_stop_count = 30
        no_new_threshold = 3

    for attempt in range(1, MAX_RETRIES + 1):
        print(f">>> [SYNC] 第 {attempt}/{MAX_RETRIES} 次尝试连接...")
        page = await context.new_page()

        try:
            print("[SYNC] 🚀 直接访问目标用户主页...")
            try:
                await page.goto(TOUTIAO_URL, wait_until="networkidle", timeout=45000)
                print("[SYNC] ✓ networkidle 完成")
            except Exception as timeout_err:
                print(f"[SYNC] ⚠ networkidle 超时，尝试降级: {timeout_err}")
                try:
                    await page.goto(TOUTIAO_URL, wait_until="domcontentloaded", timeout=30000)
                    print("[SYNC] ✓ domcontentloaded 完成")
                except:
                    raise Exception("页面加载完全失败")

            await human_delay(4, 6)

            if await check_captcha(page, f"sync_try_{attempt}"):
                raise Exception("Captcha detected")

            articles_found = False
            links = []
            all_seen_urls = set()

            article_selectors = [
                'a[href*="/article/"]',
                'a[href*="/w/"]',
                'a[href*="/video/"]',
            ]
            for sel in article_selectors:
                try:
                    await page.wait_for_selector(sel, timeout=8000)
                    print(f"[SYNC] ✓ 检测到文章元素: {sel}")
                    break
                except:
                    continue

            print(f"[SYNC] 开始滚动加载 (最多 {max_scroll_rounds} 次)...")
            no_new_count = 0
            last_total = 0

            for scroll_round in range(max_scroll_rounds):
                scroll_distance = random.randint(400, 700)
                await page.mouse.wheel(0, scroll_distance)
                await asyncio.sleep(random.uniform(1.5, 2.5))
                if random.random() < 0.1:
                    await page.mouse.wheel(0, -random.randint(80, 150))
                    await asyncio.sleep(0.3)

                if (scroll_round + 1) % 3 == 0 or scroll_round == 0:
                    current_links = await page.evaluate(EXTRACT_LINKS_JS)
                    new_urls = [l for l in current_links if l['href'] not in all_seen_urls]
                    for l in current_links:
                        all_seen_urls.add(l['href'])
                    links = current_links
                    new_this_round = len(links) - last_total
                    last_total = len(links)

                    if is_full_sync:
                        print(f"[SYNC] 📊 滚动 {scroll_round+1}/{max_scroll_rounds}: 累计 {len(links)} 篇 (+{new_this_round})")
                    else:
                        print(f"[SYNC] 滚动 {scroll_round+1}/{max_scroll_rounds}: 当前 {len(links)} 篇")

                    if links and len(links) > 0:
                        articles_found = True

                    if not is_full_sync and len(links) >= early_stop_count:
                        print(f"[SYNC] ✓ 增量模式已获取 {len(links)} 篇，提前结束")
                        break

                    if new_this_round == 0:
                        no_new_count += 1
                        if no_new_count >= no_new_threshold:
                            if is_full_sync:
                                print(f"[SYNC] 📍 已滑到底部！连续 {no_new_count} 次无新内容")
                            else:
                                print(f"[SYNC] 连续 {no_new_count} 次无新内容，停止")
                            break
                    else:
                        no_new_count = 0

            await human_delay(2, 3)
            final_links = await page.evaluate(EXTRACT_LINKS_JS)
            if final_links and len(final_links) > len(links):
                links = final_links

            if is_full_sync and no_new_count < no_new_threshold:
                print("[SYNC] 🔄 全量模式：继续尝试加载更多...")
                extra_rounds = 20
                for extra in range(extra_rounds):
                    await page.mouse.wheel(0, random.randint(500, 800))
                    await asyncio.sleep(random.uniform(1.2, 2.0))
                    if (extra + 1) % 5 == 0:
                        extra_links = await page.evaluate(EXTRACT_LINKS_JS)
                        new_extra = len(extra_links) - len(links)
                        if extra_links:
                            links = extra_links
                        print(f"[SYNC] 📊 额外滚动 {extra+1}/{extra_rounds}: 累计 {len(links)} 篇 (+{new_extra})")
                        if new_extra == 0:
                            no_new_count += 1
                            if no_new_count >= 3:
                                print("[SYNC] 📍 确认已到底部")
                                break
                        else:
                            no_new_count = 0

            print(f"\n[SYNC] 最终提取: {len(links)} 篇文章")

            if not links or len(links) == 0:
                if attempt < MAX_RETRIES:
                    print("[SYNC] 未发现文章，尝试刷新页面...")
                    await page.screenshot(path=DEBUG_DIR / f"before_refresh_attempt_{attempt}.png")
                    for refresh_attempt in range(2):
                        print(f"[SYNC] 第 {refresh_attempt+1} 次刷新...")
                        await page.reload(wait_until="networkidle", timeout=30000)
                        await human_delay(5, 7)
                        for i in range(10):
                            await page.mouse.wheel(0, random.randint(400, 600))
                            await asyncio.sleep(random.uniform(0.8, 1.2))
                        await human_delay(3, 5)
                        links = await page.evaluate(EXTRACT_LINKS_JS)
                        if links and len(links) > 0:
                            articles_found = True
                            print(f"[SYNC] ✓ 刷新后发现 {len(links)} 篇文章")
                            break
            else:
                articles_found = True

            if articles_found and links and len(links) > 0:
                mode_str = "全量" if is_full_sync else "增量"
                print(f"\n[SYNC] ✅ {mode_str}同步成功! 第 {attempt} 次尝试，共 {len(links)} 篇文章")
                new_articles = [l for l in links if l['href'] not in db.data.get("articles", {})]
                print(f"[SYNC] 📈 其中新文章: {len(new_articles)} 篇")
                print("[SYNC] 文章样本:")
                for i, link in enumerate(links[:5], 1):
                    print(f"       {i}. [{link.get('type','?')}] {link['text'][:40]}...")

                db.add_articles(links)
                db.mark_synced()

                try:
                    await page.screenshot(path=DEBUG_DIR / "sync_success_latest.png")
                    print("[SYNC] ✓ 已保存成功截图")
                except:
                    pass

                print("[SYNC] 清理旧的调试/错误文件...")
                cleanup_patterns = [
                    "error_sync_*.png", "debug_sync_fail_*.png",
                    "before_refresh_*.png", "sync_source_*.html",
                    "captcha_sync_*.png", "sync_source_final_fail.html",
                ]
                cleaned_count = 0
                try:
                    for pattern in cleanup_patterns:
                        for file_path in DEBUG_DIR.glob(pattern):
                            try:
                                file_path.unlink(missing_ok=True)
                                cleaned_count += 1
                            except:
                                pass
                    if cleaned_count > 0:
                        print(f"[SYNC] ✓ 已清理 {cleaned_count} 个旧文件")
                except Exception as clean_err:
                    print(f"[WARN] 清理文件时出错: {clean_err}")

                await page.close()
                return
            else:
                print(f"[WARN] 第 {attempt} 次尝试未能提取到文章")
                try:
                    await page.screenshot(path=DEBUG_DIR / f"debug_sync_fail_attempt_{attempt}.png")
                except:
                    pass
                try:
                    content = await page.content()
                    (DEBUG_DIR / f"sync_source_attempt_{attempt}.html").write_text(content, encoding="utf-8")
                except:
                    pass
                if attempt < MAX_RETRIES:
                    raise Exception("No links extracted")

        except Exception as e:
            print(f"[SYNC] ❌ 第 {attempt} 次尝试失败: {e}")
            try:
                if not page.is_closed():
                    await page.screenshot(path=DEBUG_DIR / f"error_sync_attempt_{attempt}.png")
            except:
                pass
            if attempt == MAX_RETRIES:
                print("[FATAL] ❌ 全量同步任务最终失败")
                try:
                    if not page.is_closed():
                        content = await page.content()
                        (DEBUG_DIR / "sync_source_final_fail.html").write_text(content, encoding="utf-8")
                except:
                    pass
            else:
                wait_time = random.randint(5, 10)
                print(f"[WAIT] 等待 {wait_time} 秒后重试...")
                await asyncio.sleep(wait_time)
        finally:
            try:
                if not page.is_closed():
                    await page.close()
            except:
                pass

    print("[SYNC] ❌ 全量同步任务完全失败")


async def read_article_task(
    context: BrowserContext,
    article: dict,
    db: ArticleDB,
    home_page: Page          # ← 复用的主页 Page 对象
):
    """
    单篇阅读任务。
    
    修复：不再新建空白页直接 goto 文章 URL（会缺少 Referer 导致跳登录页），
    而是复用已停留在个人主页的 home_page，通过 JS 跳转到文章，
    让浏览器自动携带正确的 Referer: <个人主页 URL>。
    阅读完成后再导航回主页，为下一篇做准备。
    """
    url = article['url']
    title_preview = article['title'][:40]

    try:
        article_id = url.split('/')[-1].split('?')[0][-12:]
    except:
        article_id = "unknown"

    timestamp_str = datetime.now().strftime("%H%M%S")
    start_ss_path = ""
    end_ss_path   = ""

    print(f"--- [READ] 正在打开: {title_preview}... ---")

    try:
        # ============================================================
        # 1. 从主页跳转到文章（保持真实 Referer = 个人主页）
        # ============================================================
        await home_page.evaluate(f"window.location.href = '{url}'")
        try:
            await home_page.wait_for_load_state("domcontentloaded", timeout=45000)
        except Exception as e:
            print(f"[READ] ⚠ wait_for_load_state 超时，继续: {e}")

        # 强制等待渲染
        await asyncio.sleep(3)

        # ============================================================
        # 2. 首屏截图
        # ============================================================
        start_ss_name = f"read_{timestamp_str}_{article_id}_START.png"
        start_ss_path = str(DEBUG_DIR / start_ss_name)
        try:
            await home_page.screenshot(path=start_ss_path, full_page=False)
            print(f"[READ] 📸 首屏已保存: {start_ss_name}")
        except Exception as e:
            print(f"[WARN] 首屏截图失败: {e}")
            start_ss_path = ""

        # ============================================================
        # 3. 异常检测
        # ============================================================
        if await check_captcha(home_page, "read_start"):
            append_report(url, title_preview, start_ss_path, "captcha", "触发验证码")
            return

        page_title   = await home_page.title()
        page_content = await home_page.evaluate("document.body.innerText")

        # 登录墙 / 404 / 失效检测
        invalid_keywords = ["404", "页面不存在", "文章已删除", "参数错误", "访问受限", "登录"]
        if any(k in page_title for k in invalid_keywords):
            print(f"[READ] ❌ 文章已失效，标记 invalid。Title={page_title}")
            append_report(url, title_preview, start_ss_path, "invalid", f"页面标题: {page_title}")
            db.mark_invalid(url)
            return

        # ============================================================
        # 4. 计算停留时长
        # ============================================================
        word_count = len(page_content)
        img_count = await home_page.evaluate("""
            () => {
                const imgs = document.querySelectorAll(
                    'article img, .tt-input__content img, .article-content img, .pgc-img img'
                );
                return imgs.length;
            }
        """)

        text_time  = word_count / 25.0
        img_time   = img_count * 5.0
        base_time  = text_time + img_time
        if base_time < 10:
            base_time = random.randint(20, 40)

        variation     = random.gauss(1.0, 0.2)
        thinking_time = random.uniform(5, 15)
        calc_seconds  = (base_time * variation) + thinking_time
        read_seconds  = max(30.0, min(180.0, calc_seconds))

        print(f"[READ] 统计: {word_count}字 | {img_count}图 | 算法:{calc_seconds:.1f}s")
        print(f"[READ] >> ⏱️ 计划停留: {read_seconds:.1f}秒")

        # ============================================================
        # 5. 拟人化阅读循环
        # ============================================================
        start_read  = time.time()
        scroll_count = 0

        while (time.time() - start_read) < read_seconds:
            await human_scroll(home_page, max_scrolls=1)
            scroll_count += 1

            if random.random() < 0.3:
                rand_x = random.randint(200, 1000)
                rand_y = random.randint(300, 800)
                await human_mouse_move(home_page, rand_x, rand_y)

            if random.random() < 0.1:
                try:
                    await home_page.click("p", timeout=200, force=True)
                except:
                    pass

            if random.random() < 0.05:
                await asyncio.sleep(random.uniform(2.0, 5.0))

            await asyncio.sleep(random.uniform(0.8, 2.0))

        # ============================================================
        # 6. 滚到底部，触发埋点
        # ============================================================
        await home_page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
        await human_delay(1.5, 3.0)
        print(f"[READ] ✅ 阅读完成 (滚动{scroll_count}次)")

        # ============================================================
        # 7. 完读截图
        # ============================================================
        end_ss_name = f"read_{timestamp_str}_{article_id}_END.png"
        end_ss_path = str(DEBUG_DIR / end_ss_name)
        try:
            await home_page.screenshot(path=end_ss_path)
            print(f"[READ] 📸 完读截图已保存: {end_ss_name}")
        except Exception as e:
            print(f"[READ] ⚠ 完读截图失败: {e}")
            end_ss_path = ""

        # 写数据库
        db.record_read(url)

        # 写报告（用完读截图）
        append_report(
            url, title_preview,
            end_ss_path or start_ss_path,
            "success",
            f"停留{read_seconds:.0f}s，滚动{scroll_count}次"
        )

        # ============================================================
        # 8. 读完后导航回主页，为下一篇做准备
        # ============================================================
        print("[READ] 🔙 返回个人主页...")
        try:
            await home_page.evaluate(f"window.location.href = '{TOUTIAO_URL}'")
            await home_page.wait_for_load_state("domcontentloaded", timeout=20000)
            await human_delay(2, 4)
        except Exception as e:
            print(f"[READ] ⚠ 返回主页失败（下一篇可能出错）: {e}")

        # ============================================================
        # 9. 清理旧截图（只保留最新 5 张）
        # ============================================================
        try:
            for pattern, keep in [("*_START.png", 5), ("*_END.png", 5), ("error_read_*.png", 3)]:
                files = sorted(DEBUG_DIR.glob(pattern), key=lambda x: x.stat().st_mtime, reverse=True)
                for old in files[keep:]:
                    old.unlink(missing_ok=True)
        except Exception as e:
            print(f"[READ] ⚠ 清理截图失败: {e}")

    except Exception as e:
        print(f"[READ] ❌ 异常中断: {e}")
        err_name = f"error_read_{timestamp_str}.png"
        err_path = str(DEBUG_DIR / err_name)
        try:
            await home_page.screenshot(path=err_path)
            print(f"[READ] 已保存错误现场: {err_name}")
        except:
            err_path = ""
        append_report(url, title_preview, err_path, "failed", str(e)[:100])

        # 出错后也尝试回主页，避免下一篇受影响
        try:
            await home_page.evaluate(f"window.location.href = '{TOUTIAO_URL}'")
            await home_page.wait_for_load_state("domcontentloaded", timeout=20000)
            await human_delay(2, 3)
        except:
            pass


# ================= 主程序入口 =================

async def main():
    db = ArticleDB(DB_FILE)

    vp = random.choice(VIEWPORTS)
    ua = get_pc_user_agent()

    print(f"[INIT] 启动爬虫任务")
    print(f"[INIT] UA: {ua[:60]}...")
    print(f"[INIT] Viewport: {vp['width']}x{vp['height']}")

    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=True,
            args=[
                "--disable-blink-features=AutomationControlled",
                "--no-sandbox",
                "--disable-infobars",
                "--disable-dev-shm-usage",
                "--disable-gpu",
                f"--window-size={vp['width']},{vp['height']}"
            ]
        )

        context = await browser.new_context(
            user_agent=ua,
            viewport=vp,
            locale="zh-CN",
            timezone_id="Asia/Shanghai",
            device_scale_factor=1,
            has_touch=False,
            is_mobile=False,
            java_script_enabled=True
        )

        await context.add_init_script(
            "Object.defineProperty(navigator, 'webdriver', {get: () => undefined})"
        )

        # ============================================================
        # 热身：访问头条首页
        # ============================================================
        print("[WARMUP] 执行热身...")
        warmup_page = None
        try:
            warmup_page = await context.new_page()
            await warmup_page.goto(
                "https://www.toutiao.com/",
                wait_until="domcontentloaded",
                timeout=30000
            )
            await human_delay(2, 4)
            await human_mouse_move(warmup_page, 500, 400)
            await warmup_page.mouse.wheel(0, random.randint(200, 400))
            await human_delay(1, 2)
            print("[WARMUP] ✓ 热身完成")
        except Exception as e:
            print(f"[WARMUP] ⚠ 热身失败(可忽略): {e}")
        finally:
            if warmup_page:
                try:
                    await warmup_page.close()
                except:
                    pass

        await asyncio.sleep(random.uniform(1, 2))

        # ============================================================
        # 步骤 1：同步文章列表
        # ============================================================
        if db.needs_sync() or not db.data.get("articles"):
            print("\n[TASK] 开始同步任务...")
            await sync_task(context, db)
        else:
            print("[INIT] 今日已执行过同步，跳过列表抓取。")

        # ============================================================
        # 步骤 2：获取今日阅读目标
        # ============================================================
        targets = db.get_weighted_candidates()
        if not targets:
            print("[DONE] 暂无待读文章 (可能已全部读完或无新内容)。")
            await browser.close()
            return

        print(f"\n[TASK] 今日阅读计划: {len(targets)} 篇文章")

        # ============================================================
        # 步骤 3：打开并保持一个主页 Page，用于所有文章跳转
        # ============================================================
        print("[INIT] 打开复用主页 Page...")
        home_page = await context.new_page()
        try:
            await home_page.goto(TOUTIAO_URL, wait_until="domcontentloaded", timeout=30000)
            await human_delay(2, 4)
            print("[INIT] ✓ 主页已就绪，开始逐篇阅读")
        except Exception as e:
            print(f"[ERROR] 主页打开失败，无法继续: {e}")
            await browser.close()
            return

        # ============================================================
        # 步骤 4：循环阅读（全部复用同一个 home_page）
        # ============================================================
        for i, article in enumerate(targets, 1):
            print(f"\n{'='*50}")
            print(f">>> 进度 [{i}/{len(targets)}]")
            print(f"{'='*50}")

            await read_article_task(context, article, db, home_page)

            if i < len(targets):
                wait_time = random.randint(8, 15)
                print(f"[COOL] 休息 {wait_time} 秒...")
                await asyncio.sleep(wait_time)

        # ============================================================
        # 收尾
        # ============================================================
        try:
            await home_page.close()
        except:
            pass

        await browser.close()

        print("\n" + "="*50)
        print("[DONE] ✅ 所有任务完成！")
        print(f"[DONE] 📋 Debug 报告: {REPORT_FILE}")
        print("="*50)


if __name__ == "__main__":
    asyncio.run(main())
