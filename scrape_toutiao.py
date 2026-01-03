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
    # 尝试导入 playwright-stealth 增强防爬能力
    from playwright_stealth import stealth_async
    HAS_STEALTH = True
except ImportError:
    HAS_STEALTH = False
    print("================================================================")
    print(f"[WARN] 未安装 playwright-stealth 库。")
    print(f"[WARN] 建议运行: pip install playwright-stealth 以降低被检测风险。")
    print("================================================================")

# ================= 配置区域 =================

# 目标用户主页 Token URL (请确保此链接有效)
TOUTIAO_URL = "https://www.toutiao.com/c/user/token/CiyRLPHkUyTCD9FmHodOGQVcmZh5-NRKyfiTSF0XMms-tSja0FdhrUWRp-T-DBpJCjwAAAAAAAAAAAAAT8lExjCbDHcWTgszQQjqU0Ohh9qtuXbuEOe6CQdqJEZ7yIpoM-NJ93_Sty1iMpOe_FUQ9ZmDDhjDxYPqBCIBA9GPpzc="

# 输出设置
DATA_DIR = Path("data")
DATA_DIR.mkdir(parents=True, exist_ok=True)

DB_FILE = DATA_DIR / "toutiao_db.json"
DEBUG_DIR = DATA_DIR / "debug"
DEBUG_DIR.mkdir(parents=True, exist_ok=True)

# 浏览行为限制
MAX_READ_COUNT = 10     # 每次运行脚本最多阅读多少篇
MIN_READ_COUNT = 3      # 每次运行脚本最少阅读多少篇
MAX_SYNC_SCROLLS = 20   # 同步列表时最大下滑次数
AGING_THRESHOLD = 50    # 文章“老化”阈值
MAX_RETRIES = 3  # 最大重试次数


# ================= User-Agent 管理 =================

# 内置兜底 PC UA 库 (覆盖主流浏览器与操作系统)
FALLBACK_PC_UAS = [
    # Windows 10/11 Chrome
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/119.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/118.0.0.0 Safari/537.36",
    # Windows Edge
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36 Edg/120.0.0.0",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/119.0.0.0 Safari/537.36 Edg/119.0.0.0",
    # Mac Chrome
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, Like Gecko) Chrome/119.0.0.0 Safari/537.36",
    # Mac Safari
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.2 Safari/605.1.15",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/16.6 Safari/605.1.15",
    # Linux Chrome
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
]

def get_pc_user_agent():
    """
    优先使用 real-useragent 库获取随机 PC UA。
    如果获取失败或库未安装，使用内置列表兜底。
    """
    ua = ""
    try:
        from real_useragent import UserAgent
        rua = UserAgent()
        ua = rua.desktop_useragent()
        # 简单校验获取的UA是否合法
        if not ua or len(ua) < 20:
            raise ValueError("UA too short")
    except Exception:
        ua = random.choice(FALLBACK_PC_UAS)
    
    return ua

# 常见 PC 分辨率库 (避免单一指纹)
VIEWPORTS = [
    {"width": 1920, "height": 1080},
    {"width": 1536, "height": 864},
    {"width": 1440, "height": 900},
    {"width": 1366, "height": 768},
    {"width": 1280, "height": 720},
]

# ================= JS 注入脚本 (核心逻辑优化) =================
# 增强版链接提取脚本：兼容性修复(移除?.) + 增强标题提取 + 过滤词更新
EXTRACT_LINKS_JS = r"""
() => {
  const anchors = Array.from(document.querySelectorAll("a[href]"));
  const origin = window.location.origin;
  const results = [];
  const seen = new Set();
  
  // 1. 路径特征判断
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

  // 2. 基础文本提取
  const getText = (el) => {
    if (!el) return "";
    let txt = (el.innerText || "").trim();
    if (txt) return txt;
    
    txt = (el.getAttribute("aria-label") || "").trim();
    if (txt) return txt;
    
    txt = (el.getAttribute("title") || "").trim();
    if (txt) return txt;
    
    const img = el.querySelector("img");
    if (img) {
        txt = (img.getAttribute("alt") || "").trim();
    }
    return txt;
  };

  // 3. 截取文本
  const truncateText = (text, maxLength = 50) => {
    if (!text || text.length <= maxLength) return text;
    return text.substring(0, maxLength) + "...";
  };

  // 4. 获取内容类型
  const getContentType = (url) => {
    if (url.includes("/article/")) return "article";
    if (url.includes("/w/")) return "weitoutiao";
    if (url.includes("/video/")) return "video";
    return "unknown";
  };

  // 5. 增强的标题提取
  const extractTitle = (a, urlObj) => {
    let text = getText(a);
    const contentType = getContentType(urlObj.pathname);

    // 如果直接获取失败或文本太短，尝试更多方法
    if (!text || text.length < 4) {
        let container = a.closest('.feed-card-wrapper, .article-card, .feed-card-article-wrapper, .card-wrapper, .weitoutiao-wrap, .wtt-content');
        
        // --- 兼容性修改：不使用 ?. 操作符 ---
        if (!container) {
            if (a.parentElement && a.parentElement.parentElement && a.parentElement.parentElement.parentElement) {
                container = a.parentElement.parentElement.parentElement;
            }
        }

        if (container) {
            // 微头条策略
            if (contentType === "weitoutiao") {
                const contentEl = container.querySelector('.weitoutiao-content, .wtt-content, .feed-card-article-content, [class*="content"]');
                if (contentEl) {
                    const content = contentEl.innerText.trim();
                    if (content) {
                        text = truncateText(content, 40);
                    }
                }
            } 
            // 视频策略
            else if (contentType === "video") {
                const titleEl = container.querySelector('.video-title, .title, [class*="title"]');
                if (titleEl) {
                    const t = titleEl.innerText.trim();
                    if (t) text = t;
                }
            }

            // 通用标题查找
            if (!text || text.length < 4) {
                const selectors = [
                    '.title', '.feed-card-article-title', '.article-title', '.feed-card-article-l a',
                    '[class*="title"]', 'h1, h2, h3', '.text', 'p'
                ];

                for (const selector of selectors) {
                    const el = container.querySelector(selector);
                    if (el) {
                        const t = el.innerText.trim();
                        if (t && t.length > 4) {
                            text = truncateText(t, 50);
                            break;
                        }
                    }
                }
            }

            // 最后尝试：获取容器内第一个长文本
            if (!text || text.length < 4) {
                const allTexts = container.innerText.trim().split('\n').filter(t => t.trim().length > 4);
                if (allTexts.length > 0) {
                    text = truncateText(allTexts[0], 50);
                }
            }
        }
    }

    // 兜底重命名
    if (!text || text === "Untitled") {
        if (contentType === "weitoutiao") text = "[微头条]";
        else if (contentType === "video") text = "[视频]";
    }

    return { text: text || "Untitled", contentType };
  };

  // 主循环
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

        // 关键词过滤 (已添加'侵权举报受理公示')
        const filterKeywords = [
            '跟帖评论自律管理承诺书',
            '用户协议',
            '隐私政策',
            '侵权投诉',
            '网络谣言曝光台',
            '违法和不良信息举报',
            '侵权举报受理公示'
        ];
        
        if (filterKeywords.some(keyword => text.includes(keyword))) continue;

        // 额外的短词过滤
        if (!text.startsWith('[') && text.match(/^(备案|举报|登录|下载|广告|相关推荐|搜索)$/)) continue;
        
        seen.add(cleanUrl);
        results.push({ 
            text: text, 
            href: cleanUrl,
            type: titleInfo.contentType
        });

    } catch(e) {}
  }

  return results;
}
"""

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
            self.db_path.write_text(json.dumps(self.data, indent=2, ensure_ascii=False), encoding="utf-8")
        except Exception as e:
            print(f"[DB] 保存失败: {e}")

    def needs_sync(self) -> bool:
        """判断今天是否已经执行过全量抓取"""
        today = datetime.now().strftime("%Y-%m-%d")
        return self.data.get("last_sync_date") != today

    def mark_synced(self):
        self.data["last_sync_date"] = datetime.now().strftime("%Y-%m-%d")
        self.save()

    def add_articles(self, scraped_items: list):
        """增量添加文章"""
        added_count = 0
        current_urls = self.data["articles"]
        
        for item in scraped_items:
            url = item['href']
            # 如果是新链接，或者旧链接是Untitled但这次抓到了真标题，则更新
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
                 current_urls[url]["title"] = item['text'] # 修正标题
        
        print(f"[DB] 数据库更新: 新增 {added_count} 篇，当前总库存 {len(current_urls)} 篇")
        self.save()

    def mark_invalid(self, url):
        """标记失效"""
        if url in self.data["articles"]:
            self.data["articles"][url]["status"] = "invalid"
            print(f"[DB] 链接标记为无效: {url}")
            self.save()

    def record_read(self, url):
        """记录阅读"""
        if url in self.data["articles"]:
            today = datetime.now().strftime("%Y-%m-%d")
            entry = self.data["articles"][url]
            entry["last_read_at"] = today
            entry["read_count"] = entry.get("read_count", 0) + 1
            self.save()

    def get_weighted_candidates(self) -> list:
        """获取今日阅读列表：权重算法"""
        today = datetime.now().strftime("%Y-%m-%d")
        candidates = []
        weights = []
        
        active_urls = [k for k, v in self.data["articles"].items() if v.get("status") == "active"]
        
        for url in active_urls:
            info = self.data["articles"][url]
            
            # 规则1: 今天读过的绝对不读
            if info.get("last_read_at") == today:
                continue
            
            read_count = info.get("read_count", 0)
            
            # 规则2: 权重计算
            # 没读过的(0次): 极高权重 200
            # 读得少的(<5次): 高权重 100
            # 普通(<20次): 中权重 50
            # 老旧(>50次): 低权重 5 (保留微小概率)
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

        # 无放回抽取
        target_k = random.randint(MIN_READ_COUNT, MAX_READ_COUNT)
        target_k = min(target_k, len(candidates))
        
        print(f"[PLAN] 可选文章库: {len(candidates)} 篇. 计划阅读: {target_k} 篇")
        
        selected = []
        temp_cand = list(candidates)
        temp_weight = list(weights)
        
        for _ in range(target_k):
            if not temp_cand: break
            chosen = random.choices(temp_cand, weights=temp_weight, k=1)[0]
            selected.append(chosen)
            
            idx = temp_cand.index(chosen)
            temp_cand.pop(idx)
            temp_weight.pop(idx)
            
        return selected

# ================= 拟人化操作函数 =================

async def human_delay(min_s=1.0, max_s=3.0):
    """带随机性的等待"""
    await asyncio.sleep(random.uniform(min_s, max_s))

async def human_mouse_move(page: Page, x_target, y_target, steps=25):
    """贝塞尔曲线模拟鼠标移动"""
    try:
        start_x = random.randint(100, 1000)
        start_y = random.randint(100, 600)
        
        ctrl_x1 = start_x + (x_target - start_x) * 0.3 + random.randint(-50, 50)
        ctrl_y1 = start_y + (y_target - start_y) * 0.3 + random.randint(-50, 50)
        ctrl_x2 = start_x + (x_target - start_x) * 0.7 + random.randint(-50, 50)
        ctrl_y2 = start_y + (y_target - start_y) * 0.7 + random.randint(-50, 50)

        for i in range(steps + 1):
            t = i / steps
            x = (1-t)**3 * start_x + 3*(1-t)**2 * t * ctrl_x1 + 3*(1-t)*t**2 * ctrl_x2 + t**3 * x_target
            y = (1-t)**3 * start_y + 3*(1-t)**2 * t * ctrl_y1 + 3*(1-t)*t**2 * ctrl_y2 + t**3 * y_target
            
            # 抖动
            x += random.uniform(-2, 2)
            y += random.uniform(-2, 2)
            
            await page.mouse.move(x, y)
            await asyncio.sleep(random.uniform(0.005, 0.015))
    except Exception:
        pass

async def human_scroll(page: Page, max_scrolls=1):
    """拟人化滚动"""
    for _ in range(max_scrolls):
        # 随机滚动幅度
        delta_y = random.randint(300, 700)
        await page.mouse.wheel(0, delta_y)
        
        # 滚动后的停顿，模拟阅读
        await human_delay(1.0, 2.5)
        
        # 20% 概率回滚 (回看)
        if random.random() < 0.2:
            await page.mouse.wheel(0, -random.randint(100, 250))
            await human_delay(0.5, 1.2)

async def check_captcha(page: Page, tag="unknown") -> bool:
    """检查验证码，并截图（覆盖最新一份）"""
    try:
        title = await page.title()
        is_captcha = False
        
        # 1. 标题判断
        if any(kw in title for kw in ["验证", "安全检测", "captcha", "verify"]):
            is_captcha = True
            
        # 2. DOM 判断
        if not is_captcha:
            if await page.query_selector("#captcha-verify-image") or \
               await page.query_selector(".captcha_verify_container"):
                is_captcha = True
        
        if is_captcha:
            print(f"[ALERT] {tag} 阶段检测到验证码! Title: {title}")
            # 保存验证码截图，覆盖旧的同类型文件
            screenshot_path = DEBUG_DIR / f"captcha_{tag}_latest.png"
            await page.screenshot(path=screenshot_path)
            print(f"[ALERT] 验证码截图已保存: {screenshot_path}")
            return True
            
        return False
    except Exception as e:
        print(f"[WARN] 验证码检测出错: {e}")
        return False

# ================= 核心任务逻辑 =================

async def sync_task(page: Page, db: ArticleDB):
    """
    全量同步任务：复用主 Page，成功后清理旧截图
    """
    print(">>> [SYNC] 开始执行全量同步任务 (复用当前标签页)...")
    
    # 尝试次数
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            print(f">>> [SYNC] 第 {attempt}/{MAX_RETRIES} 次尝试加载列表...")
            
            # 直接在当前 Tab 跳转
            await page.goto(TOUTIAO_URL, wait_until="domcontentloaded", timeout=45000)
            
            # 强制等待渲染 (SPA 框架缓冲)
            await human_delay(6, 10)
            
            # 验证码检查
            if await check_captcha(page, f"sync_try_{attempt}"):
                print(f"[SYNC] 第 {attempt} 次遭遇验证码，稍后重试...")
                raise Exception("Captcha detected")

            # 模拟下滑加载 (增加次数确保加载)
            print("[SYNC] 正在模拟下滑加载...")
            last_height = 0
            no_change_count = 0
            
            for i in range(MAX_SYNC_SCROLLS):
                await human_scroll(page, max_scrolls=1)
                await human_delay(1.5, 2.5) # 稍快一点的节奏
                
                new_height = await page.evaluate("document.body.scrollHeight")
                if new_height == last_height:
                    no_change_count += 1
                    if no_change_count >= 5: # 连续5次没变才停
                        break
                else:
                    no_change_count = 0
                last_height = new_height
            
            await asyncio.sleep(3) # 等待最后的渲染
            
            # 提取链接
            print("[SYNC] 执行 JS 提取链接...")
            links = await page.evaluate(EXTRACT_LINKS_JS)
            
            if links and len(links) > 0:
                # === 成功逻辑 ===
                db.add_articles(links)
                db.mark_synced()
                print(f"[SYNC] 同步成功 (第 {attempt} 次，共 {len(links)} 篇)")
                
                # === 关键：成功后清理旧的错误/调试截图，防止混淆 ===
                try:
                    for p in DEBUG_DIR.glob("sync_*.png"):
                        p.unlink() # 删除旧文件
                    for p in DEBUG_DIR.glob("error_sync_*.png"):
                        p.unlink()
                    print("[SYNC] 已清理旧的调试截图。")
                except Exception as e:
                    print(f"[WARN] 清理截图失败: {e}")

                # 成功后直接返回，不关闭页面
                return
            else:
                raise Exception("No links extracted (Result empty)")

        except Exception as e:
            print(f"[SYNC] 第 {attempt} 次尝试失败: {e}")
            # 失败截图 (保留以供分析)
            await page.screenshot(path=DEBUG_DIR / f"error_sync_fail.png")
            
            if attempt < MAX_RETRIES:
                wait_time = random.randint(5, 10)
                print(f"[WAIT] 等待 {wait_time} 秒后刷新重试...")
                await asyncio.sleep(wait_time)
            else:
                print("[FATAL] 全量同步任务最终失败")


async def read_article_task(page: Page, article: dict, db: ArticleDB):
    """
    单篇阅读任务：复用主 Page，实时覆盖截图
    """
    url = article['url']
    title_preview = article['title'][:20]
    print(f"--- [READ] 正在跳转: {title_preview}... ---")
    
    try:
        # 复用当前 Tab 跳转
        await page.goto(url, wait_until="domcontentloaded", timeout=45000)
        
        # 1. 验证码与404检查
        await human_delay(2, 4)
        if await check_captcha(page, "read"):
            return # 有验证码则跳过当前篇

        # === 关键：保存当前正在阅读的截图 (覆盖模式) ===
        # 这样你只需要打开这张图，就知道爬虫现在的视角
        await page.screenshot(path=DEBUG_DIR / "latest_reading.png")
        # ============================================

        page_title = await page.title()
        
        # 失效判断
        invalid_keywords = ["404", "页面不存在", "文章已删除", "参数错误"]
        if any(k in page_title for k in invalid_keywords):
            print("[READ] 文章已失效，标记 invalid。")
            db.mark_invalid(url)
            return

        # --- 优化的时长算法 (保持不变) ---
        page_content = await page.evaluate("document.body.innerText")
        word_count = len(page_content)
        img_count = await page.evaluate("document.querySelectorAll('img').length")
        
        base_time = (word_count / 25.0) + (img_count * 5.0)
        if base_time < 15: base_time = random.randint(20, 40)
        
        calc_seconds = (base_time * random.gauss(1.0, 0.2)) + random.uniform(5, 10)
        read_seconds = max(30.0, min(180.0, calc_seconds))
        
        print(f"[READ] 计划阅读时长: {read_seconds:.1f}秒")

        # --- 交互循环 ---
        start_read = time.time()
        while (time.time() - start_read) < read_seconds:
            await human_scroll(page, max_scrolls=1)
            
            if random.random() < 0.3:
                await human_mouse_move(page, random.randint(200, 1000), random.randint(300, 800))
            
            # 偶尔更新一下截图，方便观察进度
            if random.random() < 0.1:
                await page.screenshot(path=DEBUG_DIR / "latest_reading.png")

        await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
        await human_delay(1.5, 3.0)
        
        print(f"[READ] 阅读完成。")
        db.record_read(url)

    except Exception as e:
        print(f"[READ] 异常: {e}")
        await page.screenshot(path=DEBUG_DIR / "error_read_latest.png")
    
    # 注意：这里不再调用 page.close()，因为要留给下一篇用


# ================= 主程序入口 =================

async def main():
    # 1. 准备工作
    db = ArticleDB(DB_FILE)
    vp = random.choice(VIEWPORTS)
    ua = get_pc_user_agent()
    
    print(f"[INIT] 启动爬虫 (单Page模式)")
    print(f"[INIT] UA: {ua[:50]}...")

    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=True,
            args=[
                "--disable-blink-features=AutomationControlled",
                "--no-sandbox",
                "--disable-infobars",
                "--window-size={},{}".format(vp['width'], vp['height'])
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
        await context.add_init_script("Object.defineProperty(navigator, 'webdriver', {get: () => undefined})")

        # =========================================================
        # 核心修改：创建唯一的主 Page，并在此处完成热身
        # =========================================================
        main_page = await context.new_page()
        if HAS_STEALTH: await stealth_async(main_page)

        try:
            print(">>> [WARMUP] 正在访问首页建立信任...")
            # 这一步建立 Cookies
            await main_page.goto("https://www.toutiao.com/", wait_until="networkidle", timeout=60000)
            await human_delay(3, 5)
            await human_mouse_move(main_page, 500, 500)
            print("[WARMUP] 首页热身完成，Session 已建立。")
        except Exception as e:
            print(f"[WARN] 热身加载可能不完整: {e}")

        # =========================================================

        # --- 步骤 1: 检查是否需要全量同步 ---
        if db.needs_sync() or not db.data.get("articles"):
            # 传入 main_page，复用刚才热身好的页面
            await sync_task(main_page, db)
        else:
            print("[INIT] 今日已执行过同步，跳过列表抓取。")

        # --- 步骤 2: 获取今日阅读目标 ---
        targets = db.get_weighted_candidates()
        
        if not targets:
            print("[DONE] 暂无待读文章。")
            await browser.close()
            return

        # --- 步骤 3: 循环阅读 ---
        for i, article in enumerate(targets, 1):
            print(f"\n>>> 进度 [{i}/{len(targets)}]")
            
            # 传入 main_page，复用同一个页面
            await read_article_task(main_page, article, db)
            
            if i < len(targets):
                wait_time = random.randint(8, 15)
                print(f"[COOL] 休息 {wait_time} 秒...")
                await asyncio.sleep(wait_time)

        await browser.close()
        print("\n[DONE] 所有任务完成。")
        

if __name__ == "__main__":
    asyncio.run(main())
