import asyncio
import json
import random
import time
import math
from datetime import datetime
from pathlib import Path

from playwright.async_api import async_playwright, Page, BrowserContext

# =================配置区域=================

# 目标用户主页 (请替换为你需要抓取的主页 token URL)
TOUTIAO_URL = "https://www.toutiao.com/c/user/token/CiyRLPHkUyTCD9FmHodOGQVcmZh5-NRKyfiTSF0XMms-tSja0FdhrUWRp-T-DBpJCjwAAAAAAAAAAAAAT8lExjCbDHcWTgszQQjqU0Ohh9qtuXbuEOe6CQdqJEZ7yIpoM-NJ93_Sty1iMpOe_FUQ9ZmDDhjDxYPqBCIBA9GPpzc="

# 输出设置
OUTPUT_DIR = Path("data")
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
LINKS_FILE = OUTPUT_DIR / "toutiao_pc_data.json"
DEBUG_DIR = OUTPUT_DIR / "debug"
DEBUG_DIR.mkdir(parents=True, exist_ok=True)

# 浏览行为限制
MAX_READ_COUNT = 10  # 每次最多阅读多少篇
MIN_READ_COUNT = 3   # 每次最少阅读多少篇

# =================User-Agent 管理=================

# 内置兜底 PC UA 库 (仅当 real-useragent 获取失败时使用)
FALLBACK_PC_UAS = [
    # Windows 10/11 Chrome
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/119.0.0.0 Safari/537.36",
    # Windows Edge
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36 Edg/120.0.0.0",
    # Mac Chrome
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    # Mac Safari
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.2 Safari/605.1.15",
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
        # 实例化并获取 desktop 类型的 UA
        rua = UserAgent()
        ua = rua.desktop_useragent()
        print(f"[UA] 成功从 real-useragent 获取: {ua[:60]}...")
    except ImportError:
        print("[UA] 未安装 real-useragent，使用内置列表兜底。")
        ua = random.choice(FALLBACK_PC_UAS)
    except Exception as e:
        print(f"[UA] real-useragent 获取异常 ({e})，使用内置列表兜底。")
        ua = random.choice(FALLBACK_PC_UAS)
    
    # 双重保险：确保 UA 看起来不像空的
    if not ua or len(ua) < 10:
        ua = random.choice(FALLBACK_PC_UAS)
        
    return ua

# =================JS 注入脚本=================

# 提取文章链接逻辑 (保持原逻辑，过滤非文章链接)
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

# =================拟人化操作函数=================

async def human_delay(min_s=1.0, max_s=3.0):
    """带高斯分布倾向的随机等待"""
    mu = (min_s + max_s) / 2
    sigma = (max_s - min_s) / 4
    val = random.gauss(mu, sigma)
    val = max(min_s, min(max_s, val))
    await asyncio.sleep(val)

async def human_mouse_move(page: Page, x_target, y_target, steps=25):
    """
    贝塞尔曲线模拟鼠标移动。
    从当前位置（或随机位置）移动到目标位置，带有抖动。
    """
    try:
        # 获取当前可能的鼠标位置，如果没有则随机初始化
        start_x = random.randint(100, 1000)
        start_y = random.randint(100, 600)
        
        # 控制点逻辑
        ctrl_x1 = start_x + (x_target - start_x) * 0.3 + random.randint(-50, 50)
        ctrl_y1 = start_y + (y_target - start_y) * 0.3 + random.randint(-50, 50)
        ctrl_x2 = start_x + (x_target - start_x) * 0.7 + random.randint(-50, 50)
        ctrl_y2 = start_y + (y_target - start_y) * 0.7 + random.randint(-50, 50)

        for i in range(steps + 1):
            t = i / steps
            x = (1-t)**3 * start_x + 3*(1-t)**2 * t * ctrl_x1 + 3*(1-t)*t**2 * ctrl_x2 + t**3 * x_target
            y = (1-t)**3 * start_y + 3*(1-t)**2 * t * ctrl_y1 + 3*(1-t)*t**2 * ctrl_y2 + t**3 * y_target
            
            # 随机抖动
            x += random.uniform(-1.5, 1.5)
            y += random.uniform(-1.5, 1.5)
            
            await page.mouse.move(x, y)
            await asyncio.sleep(random.uniform(0.005, 0.02))
    except Exception as e:
        pass

async def human_scroll(page: Page, direction="down", max_scrolls=1):
    """
    拟人化滚动：变速、偶尔回滚。
    """
    try:
        for _ in range(max_scrolls):
            # 随机滚动幅度
            delta_y = random.randint(400, 800) 
            if direction == "up":
                delta_y = -delta_y

            await page.mouse.wheel(0, delta_y)
            
            # 随机停顿阅读
            await human_delay(0.8, 2.5)
            
            # 25% 概率产生“回看”动作
            if random.random() < 0.25:
                back_scroll = random.randint(100, 200)
                await page.mouse.wheel(0, -back_scroll if direction == "down" else back_scroll)
                await human_delay(0.5, 1.5)
                
    except Exception:
        pass

async def check_captcha(page: Page) -> bool:
    """
    检查页面是否存在已知的验证码/滑块特征。
    """
    try:
        title = await page.title()
        # 关键词匹配
        if any(kw in title for kw in ["验证", "安全检测", "captcha", "verify"]):
            print(f"[ALERT] 检测到可能的验证码页面! Title: {title}")
            await page.screenshot(path=DEBUG_DIR / f"captcha_{int(time.time())}.png")
            return True
        
        # 常见滑块容器ID/Class (根据实际情况维护)
        if await page.query_selector("#captcha-verify-image") or \
           await page.query_selector(".captcha_verify_container") or \
           await page.query_selector("#verify-bar-box"):
            print("[ALERT] 检测到滑块验证码 DOM 元素!")
            return True

        return False
    except Exception:
        return False

# =================核心逻辑：阅读文章=================

async def simulate_reading_article(context: BrowserContext, url: str, index: int, list_title: str):
    """
    打开文章页，模拟阅读，并返回抓取到的元数据（包括页面真实标题）。
    """
    page = None
    result = {
        "index": index,
        "url": url,
        "list_title": list_title, # 列表页抓取的标题
        "page_title": "",         # 详情页抓取的真实标题
        "read_time_seconds": 0,
        "status": "fail"
    }

    try:
        print(f"--- [READ] 正在打开第 {index} 篇 ---")
        page = await context.new_page()
        
        # 随机PC视窗大小，模拟非最大化窗口
        vp_w = 1920 + random.randint(-600, 0) # 1320 ~ 1920
        vp_h = 1080 + random.randint(-400, 0) # 680 ~ 1080
        await page.set_viewport_size({"width": vp_w, "height": vp_h})
        
        await page.goto(url, wait_until="domcontentloaded", timeout=45000)
        
        # 1. 验证码检查
        if await check_captcha(page):
            print("[WARN] 遭遇验证码，跳过。")
            result["status"] = "captcha"
            await page.close()
            return result

        await human_delay(2, 4)

        # 2. 获取真实页面标题
        try:
            page_title = await page.title()
            result["page_title"] = page_title.strip()
            print(f"[INFO] 页面标题: {result['page_title']}")
        except:
            result["page_title"] = "unknown"

        # 3. 字数估算与时长计算
        word_count = 0
        try:
            # 尝试寻找文章内容区域
            content_el = await page.query_selector("article") 
            if not content_el:
                content_el = await page.query_selector(".tt-input__content")
            if not content_el:
                 content_el = await page.query_selector("div.article-content")

            if content_el:
                text = await content_el.inner_text()
                word_count = len(text.strip())
            else:
                # 找不到正文可能是图集或视频，给个随机值
                word_count = random.randint(300, 800)
        except Exception:
            word_count = random.randint(300, 800)

        # 计算阅读时长：字数 / 速度 + 随机波动
        # 假设 PC 端浏览速度稍快
        base_read_time = word_count / random.randint(20, 40)
        read_time = max(5.0, min(90.0, base_read_time))
        result["read_time_seconds"] = round(read_time, 2)
        
        print(f"[READ] 估算字数: {word_count}, 计划停留: {read_time:.1f}s")

        # 4. 模拟阅读交互循环
        start_time = time.time()
        while time.time() - start_time < read_time:
            # 下滑
            await human_scroll(page, "down", max_scrolls=1)
            
            # 随机交互：选中与复制
            if random.random() < 0.25: # 25% 概率
                try:
                    paragraphs = await page.query_selector_all("p")
                    if paragraphs:
                        target = random.choice(paragraphs)
                        box = await target.bounding_box()
                        if box:
                            # 模拟选中
                            await page.mouse.move(box["x"], box["y"])
                            await page.mouse.down()
                            await page.mouse.move(box["x"] + random.randint(50, 150), box["y"] + 10)
                            await page.mouse.up()
                            
                            # 偶尔复制
                            if random.random() < 0.3:
                                await page.keyboard.press("Control+C")
                                print("[ACT] 模拟了 Ctrl+C")
                                
                            # 点击空白取消选中
                            await page.mouse.click(10, box["y"])
                except:
                    pass
            
            # 时间到则退出循环
            if (time.time() - start_time) >= read_time:
                break
        
        # 5. 必须到底部 (看评论行为)
        await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
        await human_delay(2, 4)
        
        # 悬停头像模拟
        if random.random() < 0.4:
            try:
                imgs = await page.query_selector_all("img")
                if len(imgs) > 5:
                    target_img = random.choice(imgs[:5]) # 只要前几个大概率是头像或相关推荐
                    await target_img.hover()
                    await human_delay(1, 1.5)
            except:
                pass

        result["status"] = "success"
        print("[READ] 阅读完成。")

    except Exception as e:
        print(f"[ERR] 阅读异常: {e}")
        result["status"] = "error"
        if page:
            await page.screenshot(path=DEBUG_DIR / f"error_{index}_{int(time.time())}.png")
    finally:
        if page:
            await page.close()
            
    return result

# =================主程序=================

async def run_crawler():
    # 1. 获取 PC User-Agent
    pc_ua = get_pc_user_agent()
    
    # 2. 设置随机窗口基准 (PC标准分辨率)
    # 宽度在 1366 到 1920 之间，高度在 768 到 1080 之间
    screen_w = random.randint(1366, 1920)
    screen_h = random.randint(768, 1080)

    print(f"[INIT] 启动爬虫...")
    print(f"[INIT] UA: {pc_ua}")
    print(f"[INIT] Window: {screen_w}x{screen_h}")

    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=True, # 设为 False 可观看过程
            args=[
                "--disable-blink-features=AutomationControlled",
                "--no-sandbox",
                "--start-maximized" # 配合 PC 模式
            ]
        )
        
        context = await browser.new_context(
            user_agent=pc_ua,
            viewport={"width": screen_w, "height": screen_h},
            locale="zh-CN",
            timezone_id="Asia/Shanghai",
            device_scale_factor=1, # PC 通常是 1，偶尔 1.25/1.5
            has_touch=False,       # PC 没有触摸屏
            is_mobile=False        # 显式声明非移动端
        )

        # 注入防检测脚本
        await context.add_init_script("""
            Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
        """)

        # --- 阶段一：首页热身 ---
        page = await context.new_page()
        try:
            print("[HOME] 访问首页热身...")
            await page.goto("https://www.toutiao.com/", wait_until="networkidle", timeout=60000)
            
            # 随机浏览首页 5-15 秒
            warmup_time = random.randint(5, 15)
            # 模拟在首页寻找入口
            await human_mouse_move(page, random.randint(200, 800), random.randint(200, 600))
            await human_scroll(page, "down", max_scrolls=2)
            await asyncio.sleep(warmup_time)
            
        except Exception as e:
            print(f"[WARN] 首页热身小插曲: {e}")

        # --- 阶段二：访问目标主页 & 提取 ---
        print(f"[TARGET] 正在跳转目标主页...")
        # 模拟手动输入地址并回车
        await page.goto(TOUTIAO_URL, wait_until="domcontentloaded", timeout=60000)
        
        if await check_captcha(page):
            print("[FATAL] 主页遭遇验证码，任务停止。")
            await browser.close()
            return

        print("[TARGET] 列表页加载中 (下滑操作)...")
        # 随机下滑次数 (例如 10 到 20 次)
        scroll_times = random.randint(10, 20)
        for i in range(scroll_times):
            await human_scroll(page, "down", max_scrolls=1)
            # 偶尔停下来发呆
            if random.random() < 0.2:
                await human_delay(2, 4)
            else:
                await human_delay(1, 2)
                
            # 每 5 次尝试回滚
            if i > 0 and i % 5 == 0:
                await human_scroll(page, "up", max_scrolls=1)

        print("[TARGET] 提取链接...")
        raw_links = await page.evaluate(EXTRACT_ARTICLES_JS)
        print(f"[TARGET] 共提取到 {len(raw_links)} 条链接。")

        if not raw_links:
            print("[WARN] 无链接，截图退出。")
            await page.screenshot(path=DEBUG_DIR / "no_links_found.png")
            await browser.close()
            return

        # --- 阶段三：蓄水池采样 & 深度浏览 ---
        # 1. 洗牌
        random.shuffle(raw_links)
        
        # 2. 随机抽取
        target_count = random.randint(MIN_READ_COUNT, MAX_READ_COUNT)
        selected_links = raw_links[:target_count]
        
        print(f"[PLAN] 计划阅读 {len(selected_links)} 篇 (Random Select)")
        
        read_results = []
        
        for idx, item in enumerate(selected_links, start=1):
            url = item['href']
            # 这里 item['text'] 是从列表页提取的标题，我们先存着
            list_title = item['text']
            
            # 模拟点击动作前摇
            await human_mouse_move(page, random.randint(300, 1000), random.randint(300, 800))
            await human_delay(0.5, 1.0)
            
            # 进入文章详情页
            res = await simulate_reading_article(context, url, idx, list_title)
            
            # 如果成功，加入结果；如果是验证码或失败，也记录
            if res["status"] == "success":
                # 优先使用页面内抓取的标题，如果未知则回退到列表标题
                final_title = res["page_title"] if res["page_title"] and res["page_title"] != "unknown" else list_title
                
                read_results.append({
                    "title": final_title,
                    "url": url,
                    "read_time": res["read_time_seconds"],
                    "scraped_at": datetime.utcnow().isoformat() + "Z"
                })
            
            # 冷却时间 (读完一篇休息一下)
            cooldown = random.randint(3, 8)
            print(f"[COOL] 休息 {cooldown} 秒...")
            await asyncio.sleep(cooldown)

        # --- 阶段四：保存结果 ---
        if read_results:
            final_data = {
                "source_url": TOUTIAO_URL,
                "task_time": datetime.utcnow().isoformat() + "Z",
                "total_scanned_links": len(raw_links),
                "read_count": len(read_results),
                "articles": read_results
            }
            
            LINKS_FILE.write_text(json.dumps(final_data, indent=2, ensure_ascii=False), encoding="utf-8")
            print(f"[DONE] 数据已保存至 {LINKS_FILE}")
        else:
            print("[DONE] 本次未成功阅读任何文章，未更新数据文件。")

        await browser.close()

if __name__ == "__main__":
    asyncio.run(run_crawler())
