import asyncio
import json
import random
import math
import time
from datetime import datetime
from pathlib import Path

from playwright.async_api import async_playwright, Page, BrowserContext, ElementHandle

# =================配置区域=================

# 目标用户主页
TOUTIAO_URL = "https://www.toutiao.com/c/user/token/CiyRLPHkUyTCD9FmHodOGQVcmZh5-NRKyfiTSF0XMms-tSja0FdhrUWRp-T-DBpJCjwAAAAAAAAAAAAAT8lExjCbDHcWTgszQQjqU0Ohh9qtuXbuEOe6CQdqJEZ7yIpoM-NJ93_Sty1iMpOe_FUQ9ZmDDhjDxYPqBCIBA9GPpzc="

# 输出设置
OUTPUT_DIR = Path("data")
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
LINKS_FILE = OUTPUT_DIR / "toutiao_links.json"
DEBUG_DIR = OUTPUT_DIR / "debug"
DEBUG_DIR.mkdir(parents=True, exist_ok=True)

# 限制设置
MAX_READ_COUNT = 10  # 每次最多阅读多少篇
MIN_READ_COUNT = 3   # 每次最少阅读多少篇

# =================反爬虫资源库=================

# 丰富的 User-Agent 池 (PC + Mobile)
USER_AGENTS = [
    # Windows Chrome
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/119.0.0.0 Safari/537.36",
    # Windows Edge
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36 Edg/120.0.0.0",
    # Mac Chrome
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    # Mac Safari
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.2 Safari/605.1.15",
    # Linux
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    # Android Mobile
    "Mozilla/5.0 (Linux; Android 10; K) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Mobile Safari/537.36",
    "Mozilla/5.0 (Linux; Android 13; SM-S908B) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/112.0.0.0 Mobile Safari/537.36",
    # iOS Mobile
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_1 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.1 Mobile/15E148 Safari/604.1",
    "Mozilla/5.0 (iPad; CPU OS 17_1 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.1 Mobile/15E148 Safari/604.1"
]

# JS: 提取链接逻辑 (同之前，未变动)
EXTRACT_ARTICLES_JS = r"""
() => {
  const anchors = Array.from(document.querySelectorAll("a[href]"));
  const origin = window.location.origin;
  const isArticleLike = (href) => {
    if (!href) return false;
    if (href === "/" || href === "#" || href.trim() === "") return false;
    if (href.startsWith("sslocal://")) return false;
    try {
      const url = new URL(href, origin);
      if (url.origin !== origin) return false;
      const path = url.pathname || "/";
      if (path.startsWith("/c/user/")) return false;
      if (path.startsWith("/license")) return false;
      if (path.startsWith("/business_license")) return false;
      const segments = path.split("/").filter(Boolean);
      if (segments.length === 0) return false;
      const last = segments[segments.length - 1];
      const pure = last.split("#")[0].split("?")[0];
      if (!pure) return false;
      const digits = pure.replace(/\D/g, "").length;
      if (digits < 6) return false;
      if (digits / pure.length < 0.6) return false;
      return true;
    } catch (e) { return false; }
  };
  const badTextPattern = /侵权举报|举报受理公示|京ICP|ICP备|营业执照|公司名称|跟帖评论自律管理承诺书/;
  const map = new Map();
  for (const a of anchors) {
    const rawHref = a.getAttribute("href") || "";
    if (!isArticleLike(rawHref)) continue;
    const url = new URL(rawHref, origin);
    const canonical = origin + url.pathname;
    let text = (a.textContent || "").trim();
    if (!text) {
      const p = a.closest("p");
      if (p) text = (p.textContent || "").trim();
    }
    if (!text) text = canonical;
    if (badTextPattern.test(text)) continue;
    let entry = map.get(canonical);
    if (!entry) { entry = { href: canonical, texts: [] }; map.set(canonical, entry); }
    entry.texts.push(text);
  }
  const results = [];
  for (const { href, texts } of map.values()) {
    let title = href;
    if (texts && texts.length > 0) {
      title = texts.reduce((best, cur) => (cur.length > best.length ? cur : best), texts[0]);
    }
    results.push({ href, text: title });
  }
  return results;
}
"""

# =================工具函数：拟人化动作=================

async def human_delay(min_s=1.0, max_s=3.0):
    """带高斯分布倾向的随机等待，避免完全均匀分布"""
    mu = (min_s + max_s) / 2
    sigma = (max_s - min_s) / 4
    val = random.gauss(mu, sigma)
    val = max(min_s, min(max_s, val)) # 截断
    await asyncio.sleep(val)

async def human_mouse_move(page: Page, x_target, y_target, steps=25):
    """
    贝塞尔曲线模拟鼠标移动：
    不走直线，包含起步加速、中间减速、随机抖动。
    """
    # 获取当前鼠标位置 (Playwright 默认 0,0，这里假设一个或维持上一个)
    # 简单起见，从随机位置开始，或者页面中心附近
    try:
        start_x = random.randint(100, 800)
        start_y = random.randint(100, 800)
        
        # 定义控制点 (用于贝塞尔曲线)
        # 控制点 1：在起点和终点之间，随机偏移
        ctrl_x1 = start_x + (x_target - start_x) * 0.3 + random.randint(-100, 100)
        ctrl_y1 = start_y + (y_target - start_y) * 0.3 + random.randint(-100, 100)
        
        # 控制点 2
        ctrl_x2 = start_x + (x_target - start_x) * 0.7 + random.randint(-100, 100)
        ctrl_y2 = start_y + (y_target - start_y) * 0.7 + random.randint(-100, 100)

        for i in range(steps + 1):
            t = i / steps
            # 三阶贝塞尔曲线公式
            x = (1-t)**3 * start_x + 3*(1-t)**2 * t * ctrl_x1 + 3*(1-t)*t**2 * ctrl_x2 + t**3 * x_target
            y = (1-t)**3 * start_y + 3*(1-t)**2 * t * ctrl_y1 + 3*(1-t)*t**2 * ctrl_y2 + t**3 * y_target
            
            # 加上微小的随机抖动 (Jitter)
            x += random.uniform(-2, 2)
            y += random.uniform(-2, 2)
            
            await page.mouse.move(x, y)
            # 移动间隔随机，模拟变速
            await asyncio.sleep(random.uniform(0.01, 0.05))
            
    except Exception as e:
        # 鼠标移动如果出错（例如坐标超出），不要崩，简单log
        print(f"[WARN] 鼠标模拟移动异常: {e}")

async def human_scroll(page: Page, direction="down", max_scrolls=1):
    """
    拟人化滚动：
    - 变速
    - 偶尔回滚 (上滑)
    - 每次滚动的像素量随机
    """
    try:
        for _ in range(max_scrolls):
            # 随机决定滚动距离
            delta_y = random.randint(300, 700) 
            if direction == "up":
                delta_y = -delta_y

            # 使用 mouse.wheel 而不是 scrollTo，因为 wheel 会触发更多浏览器事件
            await page.mouse.wheel(0, delta_y)
            
            # 随机停顿，模拟看内容
            await human_delay(0.5, 2.0)
            
            # 30% 概率产生“回滚”动作 (看漏了，回去看一眼)
            if random.random() < 0.3:
                back_scroll = random.randint(50, 150)
                await page.mouse.wheel(0, -back_scroll if direction == "down" else back_scroll)
                await human_delay(0.5, 1.5)
                
    except Exception as e:
        print(f"[WARN] 滚动操作异常: {e}")

async def check_captcha(page: Page) -> bool:
    """
    检查页面是否存在已知的验证码特征。
    如果发现验证码，返回 True。
    """
    try:
        # 这里列举常见的验证码 Title 或 元素特征
        title = await page.title()
        if "验证" in title or "安全检测" in title or "captcha" in title.lower():
            print(f"[ALERT] 检测到可能的验证码页面! Title: {title}")
            # 截图保存证据
            await page.screenshot(path=DEBUG_DIR / f"captcha_{int(time.time())}.png")
            return True
        
        # 检查特定的滑块元素 (需要根据头条实际 DOM 调整，这里是示例选择器)
        # cloudflare, geetest, etc.
        if await page.query_selector("#captcha-verify-image") or await page.query_selector(".captcha_verify_container"):
            print("[ALERT] 检测到滑块验证码 DOM 元素!")
            return True

        return False
    except Exception:
        return False

# =================核心逻辑：阅读文章=================

async def simulate_reading_article(context: BrowserContext, url: str, index: int):
    """
    在新标签页中打开文章，模拟深度阅读。
    包含：字数检查、随机滚动、随机选中、随机复制、随机时长。
    """
    page = None
    try:
        print(f"--- [READ] 开始浏览第 {index} 篇: {url} ---")
        
        # 1. 新标签页打开 (模拟 Ctrl+Click 或者 context.new_page)
        # 为了更可控，我们使用 new_page + goto，模拟从列表页点击跳转
        page = await context.new_page()
        
        # 随机视窗大小 (模拟不同标签页状态)
        viewport_w = 1280 + random.randint(-50, 50)
        viewport_h = 720 + random.randint(-50, 50)
        await page.set_viewport_size({"width": viewport_w, "height": viewport_h})
        
        await page.goto(url, wait_until="domcontentloaded", timeout=45000)
        
        # 2. 检查验证码/加载错误
        if await check_captcha(page):
            print("[WARN] 遇到验证码，跳过本篇阅读，发呆一会。")
            await human_delay(10, 20)
            await page.close()
            return

        # 3. 初始渲染等待
        await human_delay(2, 4)
        
        # 4. 获取文章内容估算阅读时长
        # 尝试寻找文章主体的 selector，头条通常是 article 或 div.article-content
        # 加上 try-except 防止找不到元素报错
        word_count = 0
        try:
            # 尝试获取正文文本
            content_el = await page.query_selector("article")
            if not content_el:
                content_el = await page.query_selector("div.tt-input__content") # 备选
            
            if content_el:
                text = await content_el.inner_text()
                word_count = len(text.strip())
            else:
                # 找不到正文，可能是视频或图集，给个默认值
                word_count = random.randint(200, 800)
        except Exception as e:
            print(f"[WARN] 字数提取失败，使用随机默认值: {e}")
            word_count = random.randint(300, 1000)

        # 5. 计算阅读时长
        # 假设人均阅读速度 10-20 字/秒，加上随机波动
        base_read_time = word_count / random.randint(15, 30)
        # 限制最大最小值，防止读太久或太短
        read_time = max(5.0, min(60.0, base_read_time)) 
        print(f"[READ] 估算字数: {word_count}, 计划停留: {read_time:.1f} 秒")

        start_time = time.time()
        
        # 6. 阅读过程中的滚动与交互循环
        while time.time() - start_time < read_time:
            # 随机滚动
            await human_scroll(page, "down", max_scrolls=1)
            
            # 随机交互：选中文字 (Selection)
            # 只有 30% 的概率会在阅读中途选中文本
            if random.random() < 0.3:
                try:
                    # 随机找个 P 标签
                    paragraphs = await page.query_selector_all("p")
                    if paragraphs:
                        target_p = random.choice(paragraphs)
                        # 模拟鼠标按下选中
                        box = await target_p.bounding_box()
                        if box:
                            await page.mouse.move(box["x"], box["y"])
                            await page.mouse.down()
                            await page.mouse.move(box["x"] + random.randint(50, 200), box["y"] + random.randint(5, 20))
                            await page.mouse.up()
                            
                            # 随机交互：复制 (Copy)
                            # 选中后，有 40% 概率触发复制
                            if random.random() < 0.4:
                                print("[ACT] 模拟 Ctrl+C 复制操作")
                                await page.keyboard.press("Control+C")
                                await human_delay(0.5, 1.0)
                                
                            # 点击别处取消选中
                            await page.mouse.click(box["x"] - 10, box["y"])
                except Exception:
                    pass # 交互失败不影响整体流程

            # 剩余时间检查
            if (time.time() - start_time) >= read_time:
                break
                
        # 7. 必须滑到底部 (看评论区)
        await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
        await human_delay(1.5, 3.0)
        
        # 8. 评论区悬停
        if random.random() < 0.5:
            try:
                # 随便找个类似头像或用户名的元素 hover 一下
                comment_avatar = await page.query_selector("img")
                if comment_avatar:
                    await comment_avatar.hover()
                    await human_delay(1, 2)
            except:
                pass

        print(f"[READ] 阅读结束，关闭页面。")

    except Exception as e:
        print(f"[ERR] 阅读文章时发生未知错误: {e}")
        # 截图保存错误现场
        if page:
            await page.screenshot(path=DEBUG_DIR / f"error_read_{int(time.time())}.png")
            
    finally:
        if page:
            try:
                await page.close()
            except:
                pass

# =================主程序逻辑=================

async def run_crawler():
    # 随机选择一个 UA
    chosen_ua = random.choice(USER_AGENTS)
    # 随机视口大小
    vp_width = 1920 + random.randint(-100, 50)
    vp_height = 1080 + random.randint(-200, 0)

    print(f"[INIT] 启动爬虫...")
    print(f"[INIT] User-Agent: {chosen_ua[:50]}...")
    print(f"[INIT] Viewport: {vp_width}x{vp_height}")

    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=True, # 调试时可改为 False 观看效果
            args=[
                "--disable-blink-features=AutomationControlled", # 移除 webdriver 特征
                "--no-sandbox",
                "--disable-infobars",
            ]
        )
        
        context = await browser.new_context(
            user_agent=chosen_ua,
            viewport={"width": vp_width, "height": vp_height},
            locale="zh-CN",
            timezone_id="Asia/Shanghai",
            device_scale_factor=random.choice([1, 1.25, 1.5, 2]) # 模拟不同屏幕像素比
        )

        # 注入脚本，进一步隐藏 WebDriver 属性
        await context.add_init_script("""
            Object.defineProperty(navigator, 'webdriver', {
                get: () => undefined
            });
        """)

        # ---------------- 阶段一：首页热身 ----------------
        page = await context.new_page()
        try:
            print("[HOME] 访问头条首页进行热身...")
            await page.goto("https://www.toutiao.com/", wait_until="networkidle", timeout=60000)
            
            # 随机动动鼠标
            await human_mouse_move(page, 500, 500)
            
            # 首页随机看 5-10 秒
            warmup_time = random.randint(5, 10)
            print(f"[HOME] 首页停留 {warmup_time} 秒，模拟闲逛...")
            
            # 简单滑两下
            await human_scroll(page, "down", max_scrolls=2)
            await human_delay(1, 2)
            await human_scroll(page, "up", max_scrolls=1)
            
            await asyncio.sleep(warmup_time)

        except Exception as e:
            print(f"[WARN] 首页热身出现问题 (不影响主流程): {e}")

        # ---------------- 阶段二：访问目标主页 & 提取 ----------------
        print(f"[TARGET] 跳转目标主页: {TOUTIAO_URL}")
        # 直接 goto，模拟从地址栏输入或书签
        response = await page.goto(TOUTIAO_URL, wait_until="domcontentloaded", timeout=60000)
        
        if not response or response.status >= 400:
            print(f"[ERR] 目标主页访问失败，状态码: {response.status if response else 'None'}")
            await browser.close()
            return

        # 检查是否立刻弹验证码
        if await check_captcha(page):
            print("[FATAL] 主页遭遇验证码，任务终止。")
            await browser.close()
            return

        print("[TARGET] 开始列表页下滑加载...")
        # 下滑加载列表 (沿用之前的逻辑，但加上随机性)
        # 这里为了演示，简化下滑次数，实际使用建议 20+
        for i in range(10): 
            await human_scroll(page, "down", max_scrolls=1)
            # 每次下滑后，检查一下页面是否在加载中，随机发呆
            await human_delay(1.5, 3.5)
            # 每下滑 3 次，可能回滚一次
            if i % 3 == 0 and i > 0:
                await human_scroll(page, "up", max_scrolls=1)

        print("[TARGET] 提取文章链接...")
        links = await page.evaluate(EXTRACT_ARTICLES_JS)
        print(f"[TARGET] 原始抓取链接数: {len(links)}")

        if not links:
            print("[WARN] 未找到任何文章，保存截图退出。")
            await page.screenshot(path=DEBUG_DIR / "no_links.png")
            await browser.close()
            return

        # ---------------- 阶段三：蓄水池抽样 & 深度浏览 ----------------
        
        # 1. 洗牌
        random.shuffle(links)
        
        # 2. 随机决定要读几篇
        read_count = random.randint(MIN_READ_COUNT, MAX_READ_COUNT)
        targets = links[:read_count]
        
        print(f"[PLAN] 计划阅读 {len(targets)} 篇文章 (随机抽样)")

        results_data = []

        # 3. 循环阅读
        for idx, item in enumerate(targets, start=1):
            url = item['href']
            title = item['text']
            
            # 模拟：从列表页鼠标移动到链接上 (虽然后面是用 new_page 打开，但动作要做足)
            # 这里简单模拟移动到屏幕中间区域，假装在点链接
            await human_mouse_move(page, random.randint(300, 900), random.randint(300, 800))
            await human_delay(0.5, 1.0)
            
            # 调用阅读逻辑
            await simulate_reading_article(context, url, idx)
            
            results_data.append(item)
            
            # 读完一篇，回到列表页后的“冷却时间”
            # 模拟人看完一篇，伸个懒腰，喝口水
            cooldown = random.randint(3, 8)
            print(f"[COOL] 冷却 {cooldown} 秒...")
            await asyncio.sleep(cooldown)

        # ---------------- 阶段四：保存与收尾 ----------------
        
        print(f"[SAVE] 保存数据到 {LINKS_FILE}")
        final_data = {
            "scraped_at": datetime.utcnow().isoformat() + "Z",
            "source": TOUTIAO_URL,
            "total_extracted": len(links),
            "read_count": len(results_data),
            "articles": results_data # 这里只存了实际读过的，或者你可以存 links (全部)
        }
        
        LINKS_FILE.write_text(json.dumps(final_data, indent=2, ensure_ascii=False), encoding="utf-8")
        
        await browser.close()
        print("[DONE] 任务完成。")

if __name__ == "__main__":
    asyncio.run(run_crawler())
