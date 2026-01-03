import asyncio
import json
import random
import time
import math
import sys
from datetime import datetime
from pathlib import Path

from playwright.async_api import async_playwright, Page, BrowserContext

# 尝试导入 playwright-stealth，这是目前最强的反指纹库
try:
    from playwright_stealth import stealth_async
    HAS_STEALTH = True
except ImportError:
    HAS_STEALTH = False
    print("================================================================")
    print("[WARN] 未检测到 playwright-stealth 库！")
    print("       强烈建议安装: pip install playwright-stealth")
    print("       否则无头模式(Headless)极易被今日头条风控识别。")
    print("================================================================")

# ================= 配置区域 =================

# 目标用户主页 Token URL
TOUTIAO_URL = "https://www.toutiao.com/c/user/token/CiyRLPHkUyTCD9FmHodOGQVcmZh5-NRKyfiTSF0XMms-tSja0FdhrUWRp-T-DBpJCjwAAAAAAAAAAAAAT8lExjCbDHcWTgszQQjqU0Ohh9qtuXbuEOe6CQdqJEZ7yIpoM-NJ93_Sty1iMpOe_FUQ9ZmDDhjDxYPqBCIBA9GPpzc="

# 数据存储路径
DATA_DIR = Path("data")
DATA_DIR.mkdir(parents=True, exist_ok=True)
DB_FILE = DATA_DIR / "toutiao_db.json"
DEBUG_DIR = DATA_DIR / "debug"
DEBUG_DIR.mkdir(parents=True, exist_ok=True)

# 任务配置
MAX_READ_COUNT = 10     # 每次脚本运行最多读多少篇
MIN_READ_COUNT = 3      # 每次脚本运行最少读多少篇
AGING_THRESHOLD = 50    # 阅读次数老化阈值 (超过此次数后，被选中概率降低，但不会为0)
MAX_SYNC_SCROLLS = 25   # 每日同步链接时，最大下滑次数

# ================= User-Agent 管理 (恢复全量) =================

# 内置兜底 PC UA 库 (覆盖 Windows, Mac, Chrome, Edge, Safari)
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
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/119.0.0.0 Safari/537.36",
    # Mac Safari
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.2 Safari/605.1.15",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/16.6 Safari/605.1.15",
    # Linux Chrome (少量)
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
]

def get_pc_user_agent():
    """优先使用 real_useragent，失败则随机抽取内置列表"""
    ua = ""
    try:
        from real_useragent import UserAgent
        ua = UserAgent().desktop_useragent()
    except Exception:
        pass
    
    if not ua or len(ua) < 10:
        ua = random.choice(FALLBACK_PC_UAS)
    return ua

# ================= 常见 PC 分辨率库 =================
# 随机选用，避免所有无头浏览器都是 800x600 或 1280x720
VIEWPORTS = [
    {"width": 1920, "height": 1080},
    {"width": 1366, "height": 768},
    {"width": 1536, "height": 864},
    {"width": 1440, "height": 900},
    {"width": 1280, "height": 720},
]

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
            # 使用 ensure_ascii=False 保证中文可读
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
        """增量添加文章，不覆盖已有的阅读数据"""
        added_count = 0
        current_urls = self.data["articles"]
        
        for item in scraped_items:
            url = item['href']
            # 只有当URL不存在时才添加
            if url not in current_urls:
                current_urls[url] = {
                    "title": item['text'],
                    "url": url,
                    "status": "active",  # active: 正常, invalid: 失效/404
                    "last_read_at": "",  # 上次阅读日期 YYYY-MM-DD
                    "read_count": 0      # 累计阅读次数
                }
                added_count += 1
        
        print(f"[DB] 数据库更新: 新增 {added_count} 篇，总库存 {len(current_urls)} 篇")
        self.save()

    def mark_invalid(self, url):
        """标记文章失效（以后不再读取）"""
        if url in self.data["articles"]:
            self.data["articles"][url]["status"] = "invalid"
            print(f"[DB] 链接标记为无效 (404/删除): {url}")
            self.save()

    def record_read(self, url):
        """记录阅读：更新日期，增加计数"""
        if url in self.data["articles"]:
            today = datetime.now().strftime("%Y-%m-%d")
            entry = self.data["articles"][url]
            entry["last_read_at"] = today
            entry["read_count"] = entry.get("read_count", 0) + 1
            self.save()

    def get_weighted_candidates(self) -> list:
        """
        核心逻辑：根据权重获取今日待读文章。
        1. 排除 status != active
        2. 排除 last_read_at == today (每天每篇只读一次)
        3. 权重计算：次数越少权重越高，次数>50权重最低但保留。
        """
        today = datetime.now().strftime("%Y-%m-%d")
        candidates = []
        weights = []
        
        active_urls = [k for k, v in self.data["articles"].items() if v.get("status") == "active"]
        
        for url in active_urls:
            info = self.data["articles"][url]
            
            # 规则1：今天读过的，绝对不读
            if info.get("last_read_at") == today:
                continue
            
            read_count = info.get("read_count", 0)
            
            # 规则2：计算权重 (Weighting Strategy)
            # 新文章(count<5): 权重 100
            # 普通(5<=count<20): 权重 50
            # 熟悉(20<=count<50): 权重 10
            # 老化(count>=50): 权重 1 (保留被选中的微小概率，防止死库)
            if read_count < 5:
                w = 100
            elif read_count < 20:
                w = 50
            elif read_count < AGING_THRESHOLD:
                w = 10
            else:
                w = 1  # 只要不是0，就有机会被选中
            
            candidates.append(info)
            weights.append(w)
            
        if not candidates:
            return []

        # 随机抽取逻辑
        target_k = random.randint(MIN_READ_COUNT, MAX_READ_COUNT)
        target_k = min(target_k, len(candidates))
        
        print(f"[PLAN] 可选库: {len(candidates)} 篇. 计划抽取: {target_k} 篇")
        
        # 使用 random.choices (有放回) 或者 custom logic (无放回权重抽取)
        # 这里为了去重，实现无放回的加权抽取
        selected = []
        temp_cand = list(candidates)
        temp_weight = list(weights)
        
        for _ in range(target_k):
            if not temp_cand: break
            # 抽取一个
            chosen = random.choices(temp_cand, weights=temp_weight, k=1)[0]
            selected.append(chosen)
            
            # 从列表中移除，防止重复选中
            idx = temp_cand.index(chosen)
            temp_cand.pop(idx)
            temp_weight.pop(idx)
            
        return selected

# ================= 拟人化动作 =================

async def human_delay(min_s=1.0, max_s=3.0):
    await asyncio.sleep(random.uniform(min_s, max_s))

async def human_mouse_move(page: Page, x_target, y_target, steps=25):
    """贝塞尔曲线模拟鼠标移动"""
    try:
        start_x = random.randint(100, 1000)
        start_y = random.randint(100, 600)
        
        # 两个控制点，创造平滑曲线
        ctrl_x1 = start_x + (x_target - start_x) * 0.3 + random.randint(-50, 50)
        ctrl_y1 = start_y + (y_target - start_y) * 0.3 + random.randint(-50, 50)
        ctrl_x2 = start_x + (x_target - start_x) * 0.7 + random.randint(-50, 50)
        ctrl_y2 = start_y + (y_target - start_y) * 0.7 + random.randint(-50, 50)

        for i in range(steps + 1):
            t = i / steps
            # 三阶贝塞尔公式
            x = (1-t)**3 * start_x + 3*(1-t)**2 * t * ctrl_x1 + 3*(1-t)*t**2 * ctrl_x2 + t**3 * x_target
            y = (1-t)**3 * start_y + 3*(1-t)**2 * t * ctrl_y1 + 3*(1-t)*t**2 * ctrl_y2 + t**3 * y_target
            
            # 加一点随机抖动
            noise_x = random.uniform(-2, 2)
            noise_y = random.uniform(-2, 2)
            
            await page.mouse.move(x + noise_x, y + noise_y)
            await asyncio.sleep(random.uniform(0.005, 0.015))
    except Exception:
        pass

async def human_scroll(page: Page, max_scrolls=1):
    for _ in range(max_scrolls):
        delta = random.randint(300, 800)
        await page.mouse.wheel(0, delta)
        await human_delay(1.5, 3.0)
        # 偶尔回滚（回看）
        if random.random() < 0.25:
            await page.mouse.wheel(0, -random.randint(150, 300))
            await human_delay(0.8, 1.5)

async def check_captcha(page: Page) -> bool:
    try:
        title = await page.title()
        # 关键词匹配
        if any(kw in title for kw in ["验证", "安全检测", "captcha", "verify"]):
            print(f"[ALERT] 触发验证码页面: {title}")
            return True
        # DOM 元素匹配
        if await page.query_selector("#captcha-verify-image") or \
           await page.query_selector(".captcha_verify_container"):
            return True
        return False
    except:
        return False

# ================= 业务逻辑: 同步与阅读 =================

EXTRACT_LINKS_JS = r"""
() => {
  const anchors = Array.from(document.querySelectorAll("a[href]"));
  const origin = window.location.origin;
  const results = [];
  const seen = new Set();
  
  const isArticle = (path) => {
     // 简单的路径特征判断: 数字占比高或包含 /a/ /w/ 且长度足够
     const digits = path.replace(/\D/g, "").length;
     return digits > 5; 
  };

  for (const a of anchors) {
    let href = a.getAttribute("href");
    if (!href) continue;
    if (href.startsWith("/")) href = origin + href;
    try {
        const urlObj = new URL(href);
        if (!urlObj.hostname.includes("toutiao.com")) continue;
        
        // 排除非文章页
        if (urlObj.pathname.startsWith("/c/user/")) continue;
        if (!isArticle(urlObj.pathname)) continue;
        
        const cleanUrl = urlObj.origin + urlObj.pathname;
        if (seen.has(cleanUrl)) continue;

        let text = (a.innerText || "").trim();
        // 过滤无关链接
        if (text.match(/备案|举报|用户|登录|下载/)) continue;
        if (!text) text = "Untitled";

        seen.add(cleanUrl);
        results.push({ href: cleanUrl, text: text });
    } catch(e){}
  }
  return results;
}
"""

async def sync_task(context: BrowserContext, db: ArticleDB):
    """每日一次的全量抓取任务"""
    print(">>> [SYNC] 开始执行每日同步任务...")
    page = await context.new_page()
    if HAS_STEALTH: await stealth_async(page)
    
    try:
        await page.goto(TOUTIAO_URL, wait_until="domcontentloaded", timeout=60000)
        await human_delay(3, 5)
        
        if await check_captcha(page):
            print("[SYNC] 遭遇验证码，跳过同步。")
            return

        print("[SYNC] 正在下滑列表...")
        no_change_count = 0
        last_height = 0
        
        for i in range(MAX_SYNC_SCROLLS):
            await human_scroll(page, max_scrolls=1)
            
            new_height = await page.evaluate("document.body.scrollHeight")
            if new_height == last_height:
                no_change_count += 1
                if no_change_count >= 5: # 连续5次没刷出新内容就停止
                    break
            else:
                no_change_count = 0
            last_height = new_height

        print("[SYNC] 提取链接中...")
        links = await page.evaluate(EXTRACT_LINKS_JS)
        
        if links:
            db.add_articles(links)
            db.mark_synced()
            print(f"[SYNC] 同步成功，当前日期标记为已同步。")
        else:
            print("[SYNC] 未提取到链接，可能页面结构变化或加载失败。")

    except Exception as e:
        print(f"[SYNC] 发生异常: {e}")
    finally:
        await page.close()

async def read_article_task(context: BrowserContext, article: dict, db: ArticleDB):
    """阅读单篇文章任务"""
    url = article['url']
    print(f"--- [READ] 正在打开: {article['title'][:20]}... ---")
    
    page = await context.new_page()
    if HAS_STEALTH: await stealth_async(page)

    try:
        start_load = time.time()
        # 使用 domcontentloaded 即可，networkidle 往往太慢
        await page.goto(url, wait_until="domcontentloaded", timeout=45000)
        
        # 1. 有效性与验证码检查
        await human_delay(2, 3)
        if await check_captcha(page):
            print("[READ] 验证码拦截，跳过。")
            return

        # 检查404或删除
        page_content = await page.evaluate("document.body.innerText")
        page_title = await page.title()
        
        invalid_keywords = ["404", "页面不存在", "文章已删除", "审核中", "参数错误"]
        if any(k in page_title for k in invalid_keywords) or \
           "文章已删除" in page_content[:500]: # 检查前500字即可
            print("[READ] 文章失效，标记 invalid。")
            db.mark_invalid(url)
            return

        # 2. 智能阅读时长计算
        word_count = len(page_content)
        
        # 算法：基准时长 = 字数 / 8.3 (约500字/分钟)
        # 如果是极短内容(少于100字)，可能是图片或视频，给予基础观察时间
        if word_count < 100:
            base_time = random.randint(8, 15)
        else:
            base_time = word_count / 8.3
        
        # 高斯扰动: 0.8 ~ 1.2 倍波动
        read_seconds = base_time * random.gauss(1.0, 0.2)
        
        # 边界截断
        read_seconds = max(8.0, read_seconds) # 至少读8秒
        read_seconds = min(90.0, read_seconds) # 最多读90秒 (模拟没有耐心读完长文)
        
        print(f"[READ] 字数: {word_count}, 计划停留: {read_seconds:.1f}s (Current Count: {article.get('read_count',0)})")

        # 3. 交互循环
        start_read = time.time()
        while (time.time() - start_read) < read_seconds:
            # 随机滚动
            await human_scroll(page, max_scrolls=1)
            
            # 随机鼠标轨迹
            if random.random() < 0.3:
                await human_mouse_move(page, random.randint(100, 1000), random.randint(200, 800))
            
            # 随机选中 (User Engagement)
            if random.random() < 0.15:
                try:
                    await page.click("p", timeout=500)
                except: pass

        # 4. 到底部 (模拟看完评论)
        await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
        await human_delay(1, 2)
        
        print("[READ] 完成。")
        db.record_read(url)

    except Exception as e:
        print(f"[READ] 异常: {e}")
    finally:
        await page.close()

# ================= 主程序 =================

async def main():
    # 1. 初始化
    db = ArticleDB(DB_FILE)
    
    # 随机选择一个分辨率
    vp = random.choice(VIEWPORTS)
    ua = get_pc_user_agent()
    
    print(f"[INIT] 启动爬虫 | UA: {ua[:40]}... | VP: {vp['width']}x{vp['height']}")

    async with async_playwright() as p:
        # 启动浏览器 (加强防检测参数)
        browser = await p.chromium.launch(
            headless=True, # 生产环境保持 True
            args=[
                "--disable-blink-features=AutomationControlled", # 核心去自动化特征
                "--no-sandbox",
                "--disable-infobars",
                "--disable-dev-shm-usage",
                "--disable-extensions",
                f"--window-size={vp['width']},{vp['height']}"
            ]
        )
        
        # 创建干净的上下文 (不保存Cookie，每次都是新访客，防止被追踪行为轨迹)
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

        # 兜底注入 webdriver 移除
        await context.add_init_script("Object.defineProperty(navigator, 'webdriver', {get: () => undefined})")

        # --- 阶段一：检查是否需要全量同步 ---
        if db.needs_sync():
            await sync_task(context, db)
        else:
            print("[INIT] 今日已同步过链接，跳过抓取步骤。")

        # --- 阶段二：获取阅读列表 ---
        # 这里会应用“权重算法”和“防重复阅读”
        targets = db.get_weighted_candidates()
        
        if not targets:
            print("[DONE] 当前没有符合条件的文章 (今日已读完或无新文章)。")
            await browser.close()
            return

        # --- 阶段三：执行阅读 ---
        # 访问首页热身 (建立 referer 和 基础 cookie)
        try:
            print("[WARMUP] 访问首页热身...")
            page_home = await context.new_page()
            if HAS_STEALTH: await stealth_async(page_home)
            await page_home.goto("https://www.toutiao.com/", timeout=30000)
            await human_delay(2, 4)
            await page_home.close()
        except:
            pass

        for i, article in enumerate(targets, 1):
            print(f"\n>>> 进度 [{i}/{len(targets)}]")
            await read_article_task(context, article, db)
            
            # 篇间休息 (Cooldown)
            if i < len(targets):
                wait_time = random.randint(6, 12)
                print(f"[COOL] 休息 {wait_time} 秒...")
                await asyncio.sleep(wait_time)

        await browser.close()
        print("\n[DONE] 任务全部完成。")

if __name__ == "__main__":
    asyncio.run(main())
