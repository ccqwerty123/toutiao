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
MAX_READ_COUNT = 50     # 每次运行脚本最多阅读多少篇
MIN_READ_COUNT = 7      # 每次运行脚本最少阅读多少篇
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
    全量同步任务 - 智能模式版
    
    策略：
    1. 首次运行/库存少 → 全量模式：滑到底，尽可能抓取所有文章
    2. 库存充足 → 增量模式：快速扫描，覆盖更新即可
    """
    print(">>> [SYNC] 开始执行全量同步任务...")
    
    # ============================================
    # 🔥 判断同步模式
    # ============================================
    current_count = len(db.data.get("articles", {}))
    
    # 首次运行或库存少于100篇 → 全量模式
    FULL_SYNC_THRESHOLD = 100
    is_full_sync = current_count < FULL_SYNC_THRESHOLD
    
    if is_full_sync:
        print(f"[SYNC] 📦 全量模式：当前库存 {current_count} 篇 < {FULL_SYNC_THRESHOLD}，将尽可能抓取所有文章")
        max_scroll_rounds = 50      # 最多滚动50次
        early_stop_count = 80       # 不提前停止（设很大）
        no_new_threshold = 5        # 连续5次无新内容才停
    else:
        print(f"[SYNC] 🔄 增量模式：当前库存 {current_count} 篇，快速更新即可")
        max_scroll_rounds = 15      # 最多滚动15次
        early_stop_count = 30       # 获取30篇即可停止
        no_new_threshold = 3        # 连续3次无新内容就停
    
    for attempt in range(1, MAX_RETRIES + 1):
        print(f">>> [SYNC] 第 {attempt}/{MAX_RETRIES} 次尝试连接...")
        page = await context.new_page()
        
        # ⚠️ 暂时禁用 stealth
        # if HAS_STEALTH: await stealth_async(page)
        
        try:
            # ============================================
            # 直接访问用户主页
            # ============================================
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
            
            # 验证码检查
            if await check_captcha(page, f"sync_try_{attempt}"):
                raise Exception("Captcha detected")

            articles_found = False
            links = []
            all_seen_urls = set()
            
            # ============================================
            # 等待文章元素出现
            # ============================================
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
            
            # ============================================
            # 🔥 滚动加载（根据模式调整策略）
            # ============================================
            print(f"[SYNC] 开始滚动加载 (最多 {max_scroll_rounds} 次)...")
            
            no_new_count = 0
            last_total = 0
            
            for scroll_round in range(max_scroll_rounds):
                # 滚动
                scroll_distance = random.randint(400, 700)
                await page.mouse.wheel(0, scroll_distance)
                await asyncio.sleep(random.uniform(1.5, 2.5))
                
                # 偶尔回滚
                if random.random() < 0.1:
                    await page.mouse.wheel(0, -random.randint(80, 150))
                    await asyncio.sleep(0.3)
                
                # 每3次滚动提取一次链接
                if (scroll_round + 1) % 3 == 0 or scroll_round == 0:
                    current_links = await page.evaluate(EXTRACT_LINKS_JS)
                    
                    # 统计新增
                    new_urls = [l for l in current_links if l['href'] not in all_seen_urls]
                    for l in current_links:
                        all_seen_urls.add(l['href'])
                    
                    links = current_links
                    new_this_round = len(links) - last_total
                    last_total = len(links)
                    
                    # 打印进度（全量模式更详细）
                    if is_full_sync:
                        print(f"[SYNC] 📊 滚动 {scroll_round + 1}/{max_scroll_rounds}: "
                              f"累计 {len(links)} 篇 (+{new_this_round})")
                    else:
                        print(f"[SYNC] 滚动 {scroll_round + 1}/{max_scroll_rounds}: "
                              f"当前 {len(links)} 篇")
                    
                    if links and len(links) > 0:
                        articles_found = True
                    
                    # ============================================
                    # 停止条件判断
                    # ============================================
                    
                    # 条件1：增量模式下获取足够文章
                    if not is_full_sync and len(links) >= early_stop_count:
                        print(f"[SYNC] ✓ 增量模式已获取 {len(links)} 篇，提前结束")
                        break
                    
                    # 条件2：连续无新内容
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
            
            # 最终等待
            await human_delay(2, 3)
            
            # 最终提取
            final_links = await page.evaluate(EXTRACT_LINKS_JS)
            if final_links and len(final_links) > len(links):
                links = final_links
            
            # ============================================
            # 全量模式额外努力：如果还没到底，继续滚动
            # ============================================
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
                        
                        print(f"[SYNC] 📊 额外滚动 {extra + 1}/{extra_rounds}: "
                              f"累计 {len(links)} 篇 (+{new_extra})")
                        
                        if new_extra == 0:
                            no_new_count += 1
                            if no_new_count >= 3:
                                print("[SYNC] 📍 确认已到底部")
                                break
                        else:
                            no_new_count = 0
            
            print(f"\n[SYNC] 最终提取: {len(links)} 篇文章")
            
            # ============================================
            # 页面刷新重试（如果没找到文章）
            # ============================================
            if not links or len(links) == 0:
                if attempt < MAX_RETRIES:
                    print("[SYNC] 未发现文章，尝试刷新页面...")
                    
                    await page.screenshot(path=DEBUG_DIR / f"before_refresh_attempt_{attempt}.png")
                    
                    for refresh_attempt in range(2):
                        print(f"[SYNC] 第 {refresh_attempt + 1} 次刷新...")
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
            
            # ============================================
            # 结果判断
            # ============================================
            if articles_found and links and len(links) > 0:
                # ========== 成功 ==========
                mode_str = "全量" if is_full_sync else "增量"
                print(f"\n[SYNC] ✅ {mode_str}同步成功! 第 {attempt} 次尝试，共 {len(links)} 篇文章")
                
                # 打印统计
                new_articles = [l for l in links if l['href'] not in db.data.get("articles", {})]
                print(f"[SYNC] 📈 其中新文章: {len(new_articles)} 篇")
                
                # 打印样本
                print("[SYNC] 文章样本:")
                for i, link in enumerate(links[:5], 1):
                    print(f"       {i}. [{link.get('type', '?')}] {link['text'][:40]}...")
                
                # 保存到数据库
                db.add_articles(links)
                db.mark_synced()
                
                # 保存成功截图
                try:
                    await page.screenshot(path=DEBUG_DIR / "sync_success_latest.png")
                    print("[SYNC] ✓ 已保存成功截图")
                except: pass
                
                # 清理旧错误文件
                print("[SYNC] 清理旧的调试/错误文件...")
                cleanup_patterns = [
                    "error_sync_*.png",
                    "debug_sync_fail_*.png",
                    "before_refresh_*.png",
                    "sync_source_*.html",
                    "captcha_sync_*.png",
                    "sync_source_final_fail.html",
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
                # ========== 失败 ==========
                print(f"[WARN] 第 {attempt} 次尝试未能提取到文章")
                
                try:
                    await page.screenshot(path=DEBUG_DIR / f"debug_sync_fail_attempt_{attempt}.png")
                except: pass
                
                try:
                    content = await page.content()
                    (DEBUG_DIR / f"sync_source_attempt_{attempt}.html").write_text(
                        content, encoding="utf-8"
                    )
                except: pass
                
                if attempt < MAX_RETRIES:
                    raise Exception("No links extracted")

        except Exception as e:
            print(f"[SYNC] ❌ 第 {attempt} 次尝试失败: {e}")
            
            try:
                if not page.is_closed():
                    await page.screenshot(path=DEBUG_DIR / f"error_sync_attempt_{attempt}.png")
            except: pass
            
            if attempt == MAX_RETRIES:
                print("[FATAL] ❌ 全量同步任务最终失败")
                try:
                    if not page.is_closed():
                        content = await page.content()
                        (DEBUG_DIR / "sync_source_final_fail.html").write_text(
                            content, encoding="utf-8"
                        )
                except: pass
            else:
                wait_time = random.randint(5, 10)
                print(f"[WAIT] 等待 {wait_time} 秒后重试...")
                await asyncio.sleep(wait_time)
        
        finally:
            try:
                if not page.is_closed():
                    await page.close()
            except: pass
    
    print("[SYNC] ❌ 全量同步任务完全失败")


async def read_article_task(context: BrowserContext, article: dict, db: ArticleDB):
    """
    单篇阅读任务 - 完整增强版
    包含：双重截图验证、深度拟人化操作、智能文件清理
    """
    url = article['url']
    
    # 从 URL 中提取简单的 ID 用于文件名（防止文件名过长）
    try:
        # 尝试提取最后一段数字或字符作为ID
        article_id = url.split('/')[-1].split('?')[0][-12:]
    except:
        article_id = "unknown"

    title_preview = article['title'][:30]
    print(f"--- [READ] 正在打开: {title_preview}... ---")
    
    page = await context.new_page()
    
    # 生成本次任务的时间戳字符串 (时分秒)
    timestamp_str = datetime.now().strftime("%H%M%S")

    # ⚠️ 暂时禁用 stealth，因为部分环境下会导致检测加重，可视情况开启
    # if HAS_STEALTH: await stealth_async(page)

    try:
        # ============================================
        # 1. 页面访问与首屏验证
        # ============================================
        # domcontentloaded 比 networkidle 更快，适合有广告流的页面
        await page.goto(url, wait_until="domcontentloaded", timeout=45000)
        
        # 强制等待 3 秒，让图片和 JS 渲染出来，确保截图不是白的
        await asyncio.sleep(3)

        # --------------------------------------------
        # 🔥【关键】首屏截图 (START) - 证明文章加载出来了
        # --------------------------------------------
        try:
            start_ss_name = f"read_{timestamp_str}_{article_id}_START.png"
            # full_page=False 只截当前视口，避免太长导致报错
            await page.screenshot(path=DEBUG_DIR / start_ss_name, full_page=False)
            print(f"[READ] 📸 首屏内容已保存: {start_ss_name}")
        except Exception as e:
            print(f"[WARN] 首屏截图失败: {e}")

        # --------------------------------------------
        # 异常检测
        # --------------------------------------------
        # A. 验证码检查 (调用外部定义的 check_captcha)
        if await check_captcha(page, "read_start"):
            return

        # B. 获取页面关键信息
        page_content = await page.evaluate("document.body.innerText")
        page_title = await page.title()
        
        # C. 404/失效判断
        invalid_keywords = ["404", "页面不存在", "文章已删除", "参数错误", "访问受限"]
        if any(k in page_title for k in invalid_keywords):
            print("[READ] ❌ 文章已失效，标记 invalid。")
            db.mark_invalid(url)
            return

        # =========================================================
        # 2. 智能阅读时长计算 (拟人化核心)
        # =========================================================
        
        # A. 统计字数
        word_count = len(page_content)
        
        # B. 统计图片数量 (JS 注入)
        img_count = await page.evaluate("""
            () => {
                // 查找头条常见的文章正文区域内的图片
                const imgs = document.querySelectorAll('article img, .tt-input__content img, .article-content img, .pgc-img img');
                return imgs.length;
            }
        """)

        # C. 计算基准时长
        # 假设：人眼每秒扫视 25 个字，每张图看 5 秒
        text_time = word_count / 25.0  
        img_time = img_count * 5.0
        base_time = text_time + img_time
        
        # 兜底：如果没提取到内容，给一个随机基础值
        if base_time < 10:
            base_time = random.randint(20, 40)
        
        # D. 增加随机扰动 (正态分布)
        variation = random.gauss(1.0, 0.2) # 均值1.0，标准差0.2
        thinking_time = random.uniform(5, 15) # 额外的思考/发呆时间
        
        # 计算总时长
        calc_seconds = (base_time * variation) + thinking_time
        
        # E. 严格截断范围 (最少读 30s，最多读 180s)
        read_seconds = max(30.0, calc_seconds)
        read_seconds = min(180.0, read_seconds)
        
        print(f"[READ] 统计: {word_count}字 | {img_count}图 | 算法计算:{calc_seconds:.1f}s")
        print(f"[READ] >> ⏱️ 最终计划停留: {read_seconds:.1f}秒")
        
        # =========================================================
        # 3. 拟人化交互循环 (重中之重)
        # =========================================================
        start_read = time.time()
        scroll_count = 0
        
        # 在规定时间内循环操作
        while (time.time() - start_read) < read_seconds:
            
            # --- 动作 1: 随机下滑 (调用外部 human_scroll) ---
            # 每次只滑一点点，模拟边看边滑
            await human_scroll(page, max_scrolls=1)
            scroll_count += 1
            
            # --- 动作 2: 贝塞尔曲线鼠标移动 (调用外部 human_mouse_move) ---
            # 30% 的概率移动鼠标，模拟人在看某些段落时鼠标无意识晃动
            if random.random() < 0.3:
                # 随机生成目标点
                rand_x = random.randint(200, 1000)
                rand_y = random.randint(300, 800)
                await human_mouse_move(page, rand_x, rand_y)
            
            # --- 动作 3: 模拟文本选中 (极低概率) ---
            # 10% 的概率点击一下段落文字，很多人阅读时有这个习惯
            if random.random() < 0.1:
                try:
                    # 尝试点击一个 p 标签
                    await page.click("p", timeout=200, force=True) 
                except: 
                    pass
            
            # --- 动作 4: 随机停顿 (模拟思考/被打断) ---
            # 5% 的概率停顿较长时间 (2-5秒)
            if random.random() < 0.05:
                # print("[ACT] 模拟思考暂停...")
                await asyncio.sleep(random.uniform(2.0, 5.0))
            
            # --- 循环间隔 ---
            # 每次操作完，等待一小会儿，避免操作太密集
            await asyncio.sleep(random.uniform(0.8, 2.0))

        # ============================================
        # 4. 结束动作与验证
        # ============================================
        
        # 必须动作：滑动到页面最底部 (触发"已阅读"埋点)
        await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
        await human_delay(1.5, 3.0)
        
        print(f"[READ] ✅ 阅读时间达标 (滚动{scroll_count}次)")
        
        # --------------------------------------------
        # 🔥【关键】完读截图 (END) - 证明读到底了
        # --------------------------------------------
        end_ss_name = f"read_{timestamp_str}_{article_id}_END.png"
        try:
            await page.screenshot(path=DEBUG_DIR / end_ss_name)
            print(f"[READ] 📸 完读底部已保存: {end_ss_name}")
        except Exception as ss_err:
            print(f"[READ] ⚠ 截图失败: {ss_err}")
        
        # 记录阅读状态到数据库
        db.record_read(url)

        # ============================================
        # 5. 文件清理逻辑 (防止磁盘占满)
        # ============================================
        try:
            # 清理 START 截图：按时间倒序排，只保留最新的 5 张
            start_files = sorted(DEBUG_DIR.glob("*_START.png"), key=lambda x: x.stat().st_mtime, reverse=True)
            for old_file in start_files[5:]:
                old_file.unlink(missing_ok=True)

            # 清理 END 截图：按时间倒序排，只保留最新的 5 张
            end_files = sorted(DEBUG_DIR.glob("*_END.png"), key=lambda x: x.stat().st_mtime, reverse=True)
            for old_file in end_files[5:]:
                old_file.unlink(missing_ok=True)
            
            # 清理错误截图：只保留最新的 3 张
            error_files = sorted(DEBUG_DIR.glob("error_read_*.png"), key=lambda x: x.stat().st_mtime, reverse=True)
            for old_file in error_files[3:]:
                old_file.unlink(missing_ok=True)

        except Exception as clean_err:
            print(f"[READ] ⚠ 清理旧截图失败: {clean_err}")

    except Exception as e:
        print(f"[READ] ❌ 异常中断: {e}")
        # 发生异常时的紧急截图
        try:
            err_name = f"error_read_{timestamp_str}.png"
            await page.screenshot(path=DEBUG_DIR / err_name)
            print(f"[READ] 已保存错误现场: {err_name}")
        except:
            pass
    
    finally:
        # 确保页面关闭，释放内存
        try:
            if not page.is_closed():
                await page.close()
        except:
            pass
            


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


