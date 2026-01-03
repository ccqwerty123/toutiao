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

async def sync_task(context: BrowserContext, db: ArticleDB):
    """
    全量同步任务 - 智能策略优化版
    
    逻辑说明:
    1. 检查 DB 库存:
       - 如果是初次运行(库存为0)或库存很少(<20): 激活 [深度抓取模式]，尽可能多滑。
       - 如果库存充足: 激活 [增量更新模式]，滑到一定数量即停止，节省资源。
    2. 动态调整: 根据模式不同，调整 最大滚动次数(max_scrolls) 和 目标抓取数量(target_count)。
    3. 兜底机制: 无论哪种模式，如果连续多次滚不到新内容，都会自动停止。
    """
    print(">>> [SYNC] 开始执行全量同步任务...")

    # ================= 策略判定区域 =================
    current_article_count = len(db.data.get("articles", {}))
    
    # 判定阈值：如果库存少于 20 篇，视为冷启动/数据不足
    IS_COLD_START = current_article_count < 20
    
    if IS_COLD_START:
        print(f">>> [STRATEGY] 检测到库存较少 ({current_article_count} 篇)，激活【深度抓取模式】")
        # 深度模式：滚得深，抓得多
        MAX_SCROLL_ROUNDS_DYNAMIC = 100   # 此时最大允许滚 100 次
        TARGET_ARTICLE_COUNT = 300        # 目标抓够 300 篇才停（除非到底）
    else:
        print(f">>> [STRATEGY] 检测到库存充足 ({current_article_count} 篇)，激活【增量更新模式】")
        # 增量模式：滚得浅，只要最新的
        MAX_SCROLL_ROUNDS_DYNAMIC = 20    # 平时只滚 20 次
        TARGET_ARTICLE_COUNT = 40         # 只要抓到 40 篇新热文就够了
    # ===============================================

    for attempt in range(1, MAX_RETRIES + 1):
        print(f">>> [SYNC] 第 {attempt}/{MAX_RETRIES} 次尝试连接...")
        page = await context.new_page()
        
        try:
            # ============================================
            # 1. 访问主页
            # ============================================
            print("[SYNC] 🚀 访问目标用户主页...")
            try:
                # 优先等待网络空闲，确保 AJAX 内容加载
                await page.goto(TOUTIAO_URL, wait_until="networkidle", timeout=45000)
            except Exception as timeout_err:
                print(f"[SYNC] ⚠ networkidle 超时，降级等待 DOM: {timeout_err}")
                await page.goto(TOUTIAO_URL, wait_until="domcontentloaded", timeout=30000)
            
            # 刚加载完，随机发呆几秒，模拟人眼浏览
            await human_delay(4, 6)
            
            # 检查是否有验证码
            if await check_captcha(page, f"sync_try_{attempt}"):
                print(f"[SYNC] 第 {attempt} 次遭遇验证码，关闭重试...")
                raise Exception("Captcha detected")

            # ============================================
            # 2. 准备滚动
            # ============================================
            articles_found = False
            links = []
            all_seen_urls = set()
            
            # 等待文章列表容器出现 (多种可能的选择器)
            print("[SYNC] 等待文章列表加载...")
            article_selectors = [
                'a[href*="/article/"]',
                'a[href*="/w/"]', 
                'a[href*="/video/"]',
                '.feed-card-wrapper', # 头条常见的卡片容器
                '.profile-feed-card'
            ]
            
            element_found = False
            for sel in article_selectors:
                try:
                    await page.wait_for_selector(sel, timeout=5000)
                    print(f"[SYNC] ✓ 检测到内容元素: {sel}")
                    element_found = True
                    break
                except:
                    continue
            
            if not element_found:
                print("[SYNC] ⚠ 未检测到明显的文章元素，可能加载慢，尝试直接滚动...")

            # ============================================
            # 3. 循环滚动 (核心逻辑)
            # ============================================
            print(f"[SYNC] 开始滚动加载... (计划最大滚动: {MAX_SCROLL_ROUNDS_DYNAMIC} 次)")
            
            no_new_count = 0 # 连续未发现新内容的次数
            
            for scroll_round in range(MAX_SCROLL_ROUNDS_DYNAMIC):
                # --- A. 执行滚动动作 ---
                # 随机距离，模拟真人滚轮
                scroll_distance = random.randint(500, 900) 
                await page.mouse.wheel(0, scroll_distance)
                
                # 滚动后的停顿，等待 AJAX 加载 (深度抓取时稍微快一点点，但不能太快)
                wait_time = random.uniform(1.2, 2.0) if IS_COLD_START else random.uniform(1.5, 3.0)
                await asyncio.sleep(wait_time)
                
                # 10% 概率往回滚一点，非常像真人找回刚才看过的标题
                if random.random() < 0.1:
                    await page.mouse.wheel(0, -random.randint(100, 300))
                    await asyncio.sleep(0.5)

                # --- B. 提取数据 (每滚 2 次提取一次，减少 JS 注入频率) ---
                # 第一轮必须提取，后续隔轮提取，或者最后一轮强制提取
                if scroll_round == 0 or (scroll_round + 1) % 2 == 0 or scroll_round == MAX_SCROLL_ROUNDS_DYNAMIC - 1:
                    current_links = await page.evaluate(EXTRACT_LINKS_JS)
                    
                    # 统计本轮新增
                    # 注意：current_links 包含页面上所有 current DOM 里的链接
                    #我们需要看其中有多少是之前没见过的
                    current_new_items = [l for l in current_links if l['href'] not in all_seen_urls]
                    
                    # 更新全局记录
                    for l in current_new_items:
                        all_seen_urls.add(l['href'])
                    
                    # 更新当前持有的总链接列表
                    links = current_links # 保存最新的全量快照
                    
                    total_grabbed = len(all_seen_urls)
                    print(f"[SYNC] 滚动 {scroll_round + 1}/{MAX_SCROLL_ROUNDS_DYNAMIC}: "
                          f"累计发现 {total_grabbed} 篇 (本轮新增 {len(current_new_items)})")
                    
                    # --- C. 判断是否应该停止 ---
                    
                    # 1. 连续无新内容判断 (到底了)
                    if len(current_new_items) == 0:
                        no_new_count += 1
                        if no_new_count >= 4: # 连续 4 次没刷出新东西
                            print("[SYNC] 🛑 连续 4 次滚动无新内容，判断已到底部，停止。")
                            break
                    else:
                        no_new_count = 0 # 重置计数器

                    # 2. 数量达标判断 (提前结束)
                    if total_grabbed >= TARGET_ARTICLE_COUNT:
                        print(f"[SYNC] 🛑 已达到目标数量 ({total_grabbed}/{TARGET_ARTICLE_COUNT})，提前结束。")
                        break

            # ============================================
            # 4. 结果处理
            # ============================================
            if len(all_seen_urls) > 0:
                articles_found = True
                final_count = len(all_seen_urls)
                
                # 整理最终结果 (去重逻辑其实在 all_seen_urls 已经做了一部分，这里确保格式)
                # EXTRACT_LINKS_JS 返回的是 list，我们用最后一次抓取的快照，或者合并所有历史
                # 这里的 links 变量存的是最后一次 evaluate 的结果，
                # 但为了保险（防止页面太长 DOM 节点被移除），我们最好重新整理一遍
                # 简单起见，直接使用 links (通常包含页面大部分内容)
                # 如果是无限滚动且 DOM 节点回收的网页，需要用 all_seen_urls 配合历史 item 存储
                # 鉴于头条 PC 端通常保留节点，直接用 links 即可，或者用 evaluate 再跑一次
                
                final_links = await page.evaluate(EXTRACT_LINKS_JS)
                print(f"\n[SYNC] ✅ 同步成功! 第 {attempt} 次尝试，页面共存在 {len(final_links)} 篇文章")
                
                # 打印样本
                print("[SYNC] 文章样本:")
                for i, link in enumerate(final_links[:3], 1):
                    print(f"       {i}. {link['text'][:30]}...")

                # 入库
                db.add_articles(final_links)
                db.mark_synced()
                
                # 清理工作
                try:
                    # 成功后截图留底
                    await page.screenshot(path=DEBUG_DIR / "sync_success_latest.png")
                    # 清理旧的报错图
                    for p_file in DEBUG_DIR.glob("error_sync_*.png"): p_file.unlink(missing_ok=True)
                except: pass
                
                await page.close()
                return # 成功退出函数

            else:
                # 没抓到
                print(f"[WARN] 第 {attempt} 次尝试滚动后未发现文章 (可能是加载失败或空白页)")
                await page.screenshot(path=DEBUG_DIR / f"error_sync_empty_{attempt}.png")
                
                if attempt < MAX_RETRIES:
                    print("[SYNC] 尝试刷新页面重试...")
                    await asyncio.sleep(3)

        except Exception as e:
            print(f"[SYNC] ❌ 第 {attempt} 次尝试发生异常: {e}")
            try:
                if not page.is_closed():
                    await page.screenshot(path=DEBUG_DIR / f"error_sync_exception_{attempt}.png")
            except: pass
            
            # 失败后冷却
            await asyncio.sleep(random.randint(5, 10))
        
        finally:
            try:
                if not page.is_closed(): await page.close()
            except: pass

    print("[SYNC] ❌ 全量同步任务最终失败 (所有重试耗尽)")


async def read_article_task(context: BrowserContext, article: dict, db: ArticleDB):
    """
    单篇阅读任务 - 优化版
    改动：
    1. 热身已在 main() 完成，这里直接访问文章
    2. 每篇文章成功后截图
    3. 成功后清理上一次的错误截图
    4. 保留所有拟人化操作
    """
    url = article['url']
    title_preview = article['title'][:30]
    print(f"--- [READ] 正在打开: {title_preview}... ---")
    
    page = await context.new_page()
    
    # ⚠️ 暂时禁用 stealth（测试证明可能有负面影响）
    # if HAS_STEALTH: await stealth_async(page)

    try:
        # ============================================
        # 🔥 直接访问文章页，热身已在 main() 完成
        # ============================================
        await page.goto(url, wait_until="domcontentloaded", timeout=45000)
        
        # 1. 验证码与404检查
        await human_delay(2, 3)
        if await check_captcha(page, "read"):
            return

        page_content = await page.evaluate("document.body.innerText")
        page_title = await page.title()
        
        # 简易的失效判断
        invalid_keywords = ["404", "页面不存在", "文章已删除", "参数错误"]
        if any(k in page_title for k in invalid_keywords):
            print("[READ] 文章已失效，标记 invalid。")
            db.mark_invalid(url)
            return

        # =========================================================
        # 阅读时长计算（保留原逻辑）
        # =========================================================
        
        # 1. 字数统计
        word_count = len(page_content)
        
        # 2. 图片数量统计
        img_count = await page.evaluate("""
            () => {
                const imgs = document.querySelectorAll('article img, .tt-input__content img, .article-content img, .pgc-img img');
                return imgs.length;
            }
        """)

        # 3. 计算基准时长
        text_time = word_count / 25.0  
        img_time = img_count * 5.0
        base_time = text_time + img_time
        
        if base_time < 10:
            base_time = random.randint(20, 40)
        
        # 4. 增加随机扰动
        variation = random.gauss(1.0, 0.2)
        thinking_time = random.uniform(5, 15)
        
        # 计算总时长
        calc_seconds = (base_time * variation) + thinking_time
        
        # 5. 严格截断 (30s ~ 180s)
        read_seconds = max(30.0, calc_seconds)
        read_seconds = min(180.0, read_seconds)
        
        print(f"[READ] 字数:{word_count} | 图片:{img_count} | 算法计算:{calc_seconds:.1f}s")
        print(f"[READ] >> 最终计划停留: {read_seconds:.1f}秒")
        
        # =========================================================
        # 拟人化交互循环（完整保留）
        # =========================================================
        start_read = time.time()
        scroll_count = 0
        
        while (time.time() - start_read) < read_seconds:
            # 随机下滑
            await human_scroll(page, max_scrolls=1)
            scroll_count += 1
            
            # 随机鼠标移动
            if random.random() < 0.3:
                await human_mouse_move(
                    page, 
                    random.randint(200, 1000), 
                    random.randint(300, 800)
                )
            
            # 极低概率模拟选中文本
            if random.random() < 0.1:
                try:
                    await page.click("p", timeout=200)
                except: 
                    pass
            
            # 极低概率短暂停顿（模拟思考）
            if random.random() < 0.05:
                await asyncio.sleep(random.uniform(2, 5))

        # 必须动作：滑动到底部
        await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
        await human_delay(1.5, 3.0)
        
        # ============================================
        # 🔥 成功：截图 + 清理旧错误文件
        # ============================================
        print(f"[READ] ✅ 阅读完成 (滚动{scroll_count}次)")
        
        # 生成截图文件名（使用时间戳区分）
        timestamp_str = datetime.now().strftime("%Y%m%d_%H%M%S")
        # 从URL提取文章ID作为标识
        article_id = url.split('/')[-1][:12] if '/' in url else "unknown"
        screenshot_name = f"read_success_{timestamp_str}_{article_id}.png"
        
        try:
            await page.screenshot(path=DEBUG_DIR / screenshot_name)
            print(f"[READ] 📸 已保存截图: {screenshot_name}")
        except Exception as ss_err:
            print(f"[READ] ⚠ 截图失败: {ss_err}")
        
        # 清理旧的错误截图
        try:
            for file_path in DEBUG_DIR.glob("error_read_*.png"):
                file_path.unlink(missing_ok=True)
            # 只保留最近5张成功截图，删除更早的
            success_screenshots = sorted(
                DEBUG_DIR.glob("read_success_*.png"),
                key=lambda x: x.stat().st_mtime,
                reverse=True
            )
            for old_file in success_screenshots[5:]:
                old_file.unlink(missing_ok=True)
        except Exception as clean_err:
            print(f"[READ] ⚠ 清理旧截图失败: {clean_err}")
        
        # 记录阅读
        db.record_read(url)

    except Exception as e:
        print(f"[READ] ❌ 异常: {e}")
        # 出错时截图
        try:
            timestamp_str = datetime.now().strftime("%Y%m%d_%H%M%S")
            await page.screenshot(path=DEBUG_DIR / f"error_read_{timestamp_str}.png")
            print(f"[READ] 已保存错误截图")
        except:
            pass
    
    finally:
        await page.close()


# ================= 主程序入口 =================

async def main():
    # 1. 准备工作
    db = ArticleDB(DB_FILE)
    
    # 随机选择视窗
    vp = random.choice(VIEWPORTS)
    # 获取随机 UA
    ua = get_pc_user_agent()
    
    print(f"[INIT] 启动爬虫任务")
    print(f"[INIT] UA: {ua[:50]}...")
    print(f"[INIT] Viewport: {vp['width']}x{vp['height']}")

    async with async_playwright() as p:
        # 启动浏览器
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
        
        # 创建上下文
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

        # 注入 webdriver 移除脚本
        await context.add_init_script(
            "Object.defineProperty(navigator, 'webdriver', {get: () => undefined})"
        )

        # ============================================
        # 🔥 统一热身：在所有任务之前，用独立 Page
        # ============================================
        print("[WARMUP] 执行一次性热身...")
        warmup_page = None
        try:
            warmup_page = await context.new_page()
            await warmup_page.goto(
                "https://www.toutiao.com/", 
                wait_until="domcontentloaded",  # 不用 networkidle
                timeout=30000
            )
            await human_delay(2, 4)
            
            # 简单交互
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
        
        # 热身后短暂等待
        await asyncio.sleep(random.uniform(1, 2))

        # ============================================
        # 步骤 1: 检查是否需要全量同步
        # ============================================
        if db.needs_sync() or not db.data.get("articles"):
            print("\n[TASK] 开始同步任务...")
            await sync_task(context, db)
        else:
            print("[INIT] 今日已执行过同步，跳过列表抓取。")

        # ============================================
        # 步骤 2: 获取今日阅读目标
        # ============================================
        targets = db.get_weighted_candidates()
        
        if not targets:
            print("[DONE] 暂无待读文章 (可能已全部读完或无新内容)。")
            await browser.close()
            return

        print(f"\n[TASK] 今日阅读计划: {len(targets)} 篇文章")

        # ============================================
        # 步骤 3: 循环阅读
        # 注意：热身已完成，每篇文章直接访问
        # ============================================
        for i, article in enumerate(targets, 1):
            print(f"\n{'='*50}")
            print(f">>> 进度 [{i}/{len(targets)}]")
            print(f"{'='*50}")
            
            await read_article_task(context, article, db)
            
            # 篇间冷却时间
            if i < len(targets):
                wait_time = random.randint(8, 15)
                print(f"[COOL] 休息 {wait_time} 秒...")
                await asyncio.sleep(wait_time)

        # ============================================
        # 完成
        # ============================================
        await browser.close()
        print("\n" + "="*50)
        print("[DONE] ✅ 所有任务完成！")
        print("="*50)


if __name__ == "__main__":
    asyncio.run(main())


