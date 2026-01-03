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

# 增强版链接提取脚本：解决 Untitled 问题
EXTRACT_LINKS_JS = r"""
() => {
  const anchors = Array.from(document.querySelectorAll("a[href]"));
  const origin = window.location.origin;
  const results = [];
  const seen = new Set();
  
  // 严格的文章路径特征判断
  const isArticle = (path) => {
     if (!path) return false;
     // 排除个人主页、搜索页等
     if (path.startsWith("/c/user/")) return false;
     if (path.startsWith("/search/")) return false;
     
     // 特征1: 路径以 /a/ 开头接数字 (老版)
     // 特征2: 路径以 /w/ 开头 (新版)
     // 特征3: 路径最后一部分全是数字且很长
     const lastPart = path.split("/").filter(Boolean).pop();
     if (!lastPart) return false;
     
     const digits = lastPart.replace(/\D/g, "").length;
     // 文章ID通常很长，至少10位以上，这里放宽到6位避免漏抓
     return digits > 5; 
  };

  // 辅助函数：从元素及其子元素中提取有效文本
  const getText = (el) => {
      if (!el) return "";
      let txt = (el.innerText || "").trim();
      if (txt) return txt;
      // 尝试找 aria-label
      txt = (el.getAttribute("aria-label") || "").trim();
      if (txt) return txt;
      // 尝试找 title
      txt = (el.getAttribute("title") || "").trim();
      if (txt) return txt;
      // 尝试找图片 alt
      const img = el.querySelector("img");
      if (img) {
          txt = (img.getAttribute("alt") || "").trim();
      }
      return txt;
  };

  for (const a of anchors) {
    let href = a.getAttribute("href");
    if (!href) continue;
    if (href.startsWith("/")) href = origin + href;
    
    try {
        const urlObj = new URL(href);
        // 必须是头条域名
        if (!urlObj.hostname.includes("toutiao.com")) continue;
        
        // 路径判断
        if (!isArticle(urlObj.pathname)) continue;
        
        const cleanUrl = urlObj.origin + urlObj.pathname;
        if (seen.has(cleanUrl)) continue;

        // --- 标题提取逻辑 ---
        let text = getText(a);
        
        // 如果当前a标签没找到字，尝试往上找一层的卡片容器
        // 今日头条的卡片结构经常是 div.title-box 包含标题，a标签只是覆盖层
        if (!text || text.length < 4) {
             const card = a.closest('.feed-card-wrapper, .article-card, .feed-card-article-wrapper');
             if (card) {
                 // 尝试找常见的标题类名
                 const titleEl = card.querySelector('.title, .feed-card-article-title, .article-title');
                 if (titleEl) {
                     const t = titleEl.innerText.trim();
                     if (t) text = t;
                 }
             }
        }
        // ------------------

        // 过滤垃圾链接文本
        if (text.match(/备案|举报|用户|登录|下载|广告|相关推荐/)) continue;
        
        # 实在找不到标题，标为 Untitled，但链接有效依然保留
        if (!text) text = "Untitled";

        seen.add(cleanUrl);
        results.push({ href: cleanUrl, text: text });
    } catch(e){}
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
    全量同步任务：支持重试机制
    """
    print(">>> [SYNC] 开始执行全量同步任务...")
    
    # 循环尝试 MAX_RETRIES 次
    for attempt in range(1, MAX_RETRIES + 1):
        print(f">>> [SYNC] 第 {attempt}/{MAX_RETRIES} 次尝试连接...")
        page = await context.new_page()
        if HAS_STEALTH: await stealth_async(page)
        
        try:
            # 1. 尝试访问，设置较长的超时防止卡顿
            # timeout=60000 (60秒)
            await page.goto(TOUTIAO_URL, wait_until="domcontentloaded", timeout=60000)
            await human_delay(3, 5)
            
            # 2. 验证码检查
            # 如果遇到验证码，视为当前尝试失败，记录截图并进行下一次重试（也许下次就没了）
            if await check_captcha(page, f"sync_try_{attempt}"):
                print(f"[SYNC] 第 {attempt} 次遭遇验证码，稍后重试...")
                raise Exception("Captcha detected")

            print("[SYNC] 正在模拟下滑加载...")
            no_change_count = 0
            last_height = 0
            
            # 3. 循环下滑
            for i in range(MAX_SYNC_SCROLLS):
                await human_scroll(page, max_scrolls=1)
                new_height = await page.evaluate("document.body.scrollHeight")
                if new_height == last_height:
                    no_change_count += 1
                    if no_change_count >= 5: 
                        print("[SYNC] 页面高度不再变化，停止下滑。")
                        break
                else:
                    no_change_count = 0
                last_height = new_height

            # 4. 保存源码用于调试 (每次尝试都覆盖最新)
            try:
                debug_html_path = DEBUG_DIR / "sync_source_latest.html"
                content = await page.content()
                debug_html_path.write_text(content, encoding="utf-8")
            except: pass

            print("[SYNC] 执行 JS 提取链接...")
            links = await page.evaluate(EXTRACT_LINKS_JS)
            
            # 5. 结果判断
            if links:
                # --- 成功路径 ---
                db.add_articles(links)
                db.mark_synced()
                
                # 随机保存成功截图
                if random.random() < 0.3:
                    await page.screenshot(path=DEBUG_DIR / "debug_sync_success_latest.png")
                    
                print(f"[SYNC] 同步成功 (在第 {attempt} 次尝试)。")
                await page.close()
                return  # 直接结束整个函数
            else:
                # --- 软失败（页面加载了但没抓到东西）---
                print(f"[WARN] 第 {attempt} 次尝试未提取到链接。")
                await page.screenshot(path=DEBUG_DIR / "debug_sync_fail_latest.png")
                # 抛出异常以触发重试逻辑
                raise Exception("No links extracted")

        except Exception as e:
            print(f"[SYNC] 第 {attempt} 次尝试失败: {e}")
            # 保存错误截图
            try:
                await page.screenshot(path=DEBUG_DIR / "error_sync_latest.png")
            except: pass
            
            # 如果是最后一次尝试，打印最终失败日志
            if attempt == MAX_RETRIES:
                print("[FATAL] 全量同步任务最终失败，已达到最大重试次数。")
            else:
                # 失败冷却时间：失败一次休息 5~10 秒再试
                wait_time = random.randint(5, 10)
                print(f"[WAIT] 等待 {wait_time} 秒后重试...")
                await asyncio.sleep(wait_time)
        
        finally:
            # 关键：每次尝试结束后，无论成功失败，都关闭当前 Page
            # 这样下一次循环会创建一个全新的 Page，避免旧页面的缓存或卡顿影响
            try:
                if not page.is_closed():
                    await page.close()
            except: pass

async def read_article_task(context: BrowserContext, article: dict, db: ArticleDB):
    """
    单篇阅读任务：包含优化的时长算法
    """
    url = article['url']
    title_preview = article['title'][:20]
    print(f"--- [READ] 正在打开: {title_preview}... ---")
    
    page = await context.new_page()
    if HAS_STEALTH: await stealth_async(page)

    try:
        # domcontentloaded 比 networkidle 更快且够用
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
        # 核心修改：优化阅读时长计算算法
        # 目标：30s ~ 180s 自然分布，避免一刀切
        # =========================================================
        
        # 1. 字数统计
        word_count = len(page_content)
        
        # 2. 图片数量统计 (权重很大，因为图集文章字数少)
        # 排除小图标，只找文章区域的大图
        img_count = await page.evaluate("""
            () => {
                const imgs = document.querySelectorAll('article img, .tt-input__content img, .article-content img, .pgc-img img');
                return imgs.length;
            }
        """)

        # 3. 计算基准时长
        # 假设：PC端快速浏览速度为 25字/秒 (1500字/分钟)
        # 假设：每张大图浏览耗时 5秒
        text_time = word_count / 25.0  
        img_time = img_count * 5.0
        
        base_time = text_time + img_time
        
        # 兜底：如果没字也没图 (可能是视频或加载部分失败)，给一个基础值
        if base_time < 10:
            base_time = random.randint(20, 40)
        
        # 4. 增加随机扰动
        # 高斯分布：均值1.0，标准差0.2 -> 产生 0.8 ~ 1.2 的倍数
        variation = random.gauss(1.0, 0.2)
        
        # 额外增加“思考/发呆”时间 (5~15秒)
        thinking_time = random.uniform(5, 15)
        
        # 计算总时长
        calc_seconds = (base_time * variation) + thinking_time
        
        # 5. 严格截断 (30s ~ 180s)
        read_seconds = max(30.0, calc_seconds)
        read_seconds = min(180.0, read_seconds)
        
        print(f"[READ] 字数:{word_count} | 图片:{img_count} | 算法计算:{calc_seconds:.1f}s")
        print(f"[READ] >> 最终计划停留: {read_seconds:.1f}秒")
        
        # =========================================================

        # 交互循环
        start_read = time.time()
        while (time.time() - start_read) < read_seconds:
            # 随机下滑
            await human_scroll(page, max_scrolls=1)
            
            # 随机鼠标移动
            if random.random() < 0.3:
                await human_mouse_move(page, random.randint(200, 1000), random.randint(300, 800))
            
            # 极低概率模拟选中文本 (模拟用户阅读时的习惯性动作)
            if random.random() < 0.1:
                try:
                    await page.click("p", timeout=200)
                except: pass

        # 必须动作：滑动到底部 (模拟看完/看评论)
        await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
        await human_delay(1.5, 3.0)
        
        # 成功完成
        print(f"[READ] 阅读完成。")
        db.record_read(url)

    except Exception as e:
        print(f"[READ] 异常: {e}")
        # 出错时截图 (覆盖最新一份)
        await page.screenshot(path=DEBUG_DIR / "error_read_latest.png")
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
        # 生产环境保持 headless=True
        browser = await p.chromium.launch(
            headless=True,
            args=[
                "--disable-blink-features=AutomationControlled", # 去除自动化特征
                "--no-sandbox",
                "--disable-infobars",
                "--window-size={},{}".format(vp['width'], vp['height'])
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

        # 注入 webdriver 移除脚本 (双重保险)
        await context.add_init_script("Object.defineProperty(navigator, 'webdriver', {get: () => undefined})")

        # --- 步骤 1: 检查是否需要全量同步 ---
        # 如果今天是第一次运行，或者数据库为空，则执行同步
        if db.needs_sync() or not db.data.get("articles"):
            await sync_task(context, db)
        else:
            print("[INIT] 今日已执行过同步，跳过列表抓取。")

        # --- 步骤 2: 获取今日阅读目标 ---
        targets = db.get_weighted_candidates()
        
        if not targets:
            print("[DONE] 暂无待读文章 (可能已全部读完或无新内容)。")
            await browser.close()
            return

        # --- 步骤 3: 首页热身 (建立信任) ---
        try:
            print("[WARMUP] 正在访问首页热身...")
            page_home = await context.new_page()
            if HAS_STEALTH: await stealth_async(page_home)
            
            await page_home.goto("https://www.toutiao.com/", timeout=45000)
            await human_delay(2, 5)
            # 简单动一下鼠标
            await human_mouse_move(page_home, 500, 500)
            await page_home.close()
        except Exception as e:
            print(f"[WARMUP] 热身跳过: {e}")

        # --- 步骤 4: 循环阅读 ---
        for i, article in enumerate(targets, 1):
            print(f"\n>>> 进度 [{i}/{len(targets)}]")
            await read_article_task(context, article, db)
            
            # 篇间冷却时间 (避免操作过快)
            if i < len(targets):
                wait_time = random.randint(8, 15)
                print(f"[COOL] 休息 {wait_time} 秒...")
                await asyncio.sleep(wait_time)

        await browser.close()
        print("\n[DONE] 所有任务完成。")

if __name__ == "__main__":
    asyncio.run(main())
