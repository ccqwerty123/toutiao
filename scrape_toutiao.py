import asyncio
import random
import time
from datetime import datetime
from pathlib import Path
from playwright.async_api import async_playwright

# 尝试导入 stealth
try:
    from playwright_stealth import stealth_async
    HAS_STEALTH = True
except ImportError:
    HAS_STEALTH = False

# 配置
TOUTIAO_URL = "https://www.toutiao.com/c/user/token/CiyRLPHkUyTCD9FmHodOGQVcmZh5-NRKyfiTSF0XMms-tSja0FdhrUWRp-T-DBpJCjwAAAAAAAAAAAAAT8lExjCbDHcWTgszQQjqU0Ohh9qtuXbuEOe6CQdqJEZ7yIpoM-NJ93_Sty1iMpOe_FUQ9ZmDDhjDxYPqBCIBA9GPpzc="
HOME_URL = "https://www.toutiao.com/"

DEBUG_DIR = Path("data/debug_test")
DEBUG_DIR.mkdir(parents=True, exist_ok=True)

DEFAULT_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"

# 文章检测JS
DETECT_ARTICLES_JS = """
() => {
    const result = {
        title: document.title,
        url: location.href,
        bodyHeight: document.body.scrollHeight,
        bodyTextLength: document.body.innerText.length,
        bodyTextPreview: document.body.innerText.substring(0, 300),
        
        allLinks: document.querySelectorAll('a').length,
        articleLinks: 0,
        
        feedCards: document.querySelectorAll('.feed-card-wrapper, .feed-card, .article-card, [class*="feed-card"]').length,
        userInfo: document.querySelectorAll('.user-info, .author-info, [class*="user-name"]').length,
        
        mainContent: null,
        contentHeight: 0,
        
        hasError: false,
        errorMessage: "",
        
        articleTitles: [],
        allClassNames: []
    };
    
    // 收集所有class名（用于分析页面结构）
    const allElements = document.querySelectorAll('*');
    const classSet = new Set();
    allElements.forEach(el => {
        if (el.className && typeof el.className === 'string') {
            el.className.split(' ').forEach(c => {
                if (c && c.length > 3) classSet.add(c);
            });
        }
    });
    result.allClassNames = Array.from(classSet).slice(0, 50);
    
    // 检测文章链接
    const anchors = document.querySelectorAll('a[href]');
    for (const a of anchors) {
        const href = a.getAttribute('href') || '';
        if (href.includes('/article/') || href.includes('/w/') || href.includes('/video/')) {
            result.articleLinks++;
            const text = (a.innerText || a.textContent || '').trim();
            if (text && text.length > 4 && text.length < 100 && result.articleTitles.length < 10) {
                result.articleTitles.push(text.substring(0, 60));
            }
        }
    }
    
    // 检测主内容区域
    const contentSelectors = [
        '.feed-list', '.article-list', '.user-article-list',
        '.ugc-list', 'main', '.main-content', '.user-content'
    ];
    for (const sel of contentSelectors) {
        const el = document.querySelector(sel);
        if (el) {
            result.mainContent = sel;
            result.contentHeight = el.scrollHeight;
            break;
        }
    }
    
    // 错误检测
    const bodyText = document.body.innerText;
    if (bodyText.includes('404') || bodyText.includes('页面不存在')) {
        result.hasError = true;
        result.errorMessage = "404";
    }
    if (bodyText.includes('验证') || bodyText.includes('captcha')) {
        result.hasError = true;
        result.errorMessage = "验证码";
    }
    
    return result;
}
"""

def print_result(result, test_name):
    """打印检测结果"""
    if not result:
        print("   ❌ 检测失败，无结果")
        return
    
    print(f"\n   📊 检测结果:")
    print(f"   ├─ 页面标题: {result['title']}")
    print(f"   ├─ 页面高度: {result['bodyHeight']}px")
    print(f"   ├─ 文本长度: {result['bodyTextLength']} 字符")
    print(f"   ├─ 总链接数: {result['allLinks']}")
    print(f"   ├─ 文章链接: {result['articleLinks']} ⬅️ {'✅' if result['articleLinks'] > 0 else '❌ 关键指标!'}")
    print(f"   ├─ Feed卡片: {result['feedCards']}")
    print(f"   ├─ 内容区域: {result['mainContent']} (高度: {result['contentHeight']}px)")
    print(f"   ├─ 检测错误: {result['hasError']} {result['errorMessage']}")
    
    if result['articleTitles']:
        print(f"   ├─ 文章标题样本:")
        for i, title in enumerate(result['articleTitles'][:5], 1):
            print(f"   │    {i}. {title}")
    else:
        print(f"   ├─ ⚠️ 未检测到任何文章标题!")
    
    # 显示部分class名用于调试
    if result['articleLinks'] == 0:
        print(f"   └─ 页面class样本: {result['allClassNames'][:15]}")


async def save_debug_files(page, test_name):
    """保存截图和HTML"""
    try:
        await page.screenshot(path=DEBUG_DIR / f"{test_name}.png", full_page=False)
        html = await page.content()
        (DEBUG_DIR / f"{test_name}.html").write_text(html, encoding='utf-8')
        print(f"   📸 已保存: {test_name}.png / .html")
    except Exception as e:
        print(f"   ⚠️ 保存文件失败: {e}")


async def detect(page, test_name):
    """检测并保存"""
    result = await page.evaluate(DETECT_ARTICLES_JS)
    print_result(result, test_name)
    await save_debug_files(page, test_name)
    return result


# ============= 测试用例 =============

async def test_01_direct_domcontentloaded():
    """测试1: 直接访问，domcontentloaded"""
    print("\n" + "="*60)
    print("🧪 测试1: 直接访问用户主页 (wait=domcontentloaded)")
    print("="*60)
    
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True, args=["--no-sandbox"])
        context = await browser.new_context(user_agent=DEFAULT_UA, viewport={"width": 1920, "height": 1080})
        page = await context.new_page()
        
        print("   → 直接goto用户主页...")
        await page.goto(TOUTIAO_URL, wait_until="domcontentloaded", timeout=60000)
        print("   → 等待3秒...")
        await asyncio.sleep(3)
        
        result = await detect(page, "test01")
        await browser.close()
        return result


async def test_02_direct_networkidle():
    """测试2: 直接访问，networkidle"""
    print("\n" + "="*60)
    print("🧪 测试2: 直接访问用户主页 (wait=networkidle)")
    print("="*60)
    
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True, args=["--no-sandbox"])
        context = await browser.new_context(user_agent=DEFAULT_UA, viewport={"width": 1920, "height": 1080})
        page = await context.new_page()
        
        print("   → 直接goto用户主页 (networkidle可能较慢)...")
        try:
            await page.goto(TOUTIAO_URL, wait_until="networkidle", timeout=60000)
        except Exception as e:
            print(f"   ⚠️ 超时: {e}")
        
        result = await detect(page, "test02")
        await browser.close()
        return result


async def test_03_warmup_new_page():
    """测试3: 首页热身 → 关闭 → 新Page访问"""
    print("\n" + "="*60)
    print("🧪 测试3: 首页热身后，新Page访问用户主页")
    print("="*60)
    
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True, args=["--no-sandbox"])
        context = await browser.new_context(user_agent=DEFAULT_UA, viewport={"width": 1920, "height": 1080})
        
        print("   → 创建Page访问首页...")
        warmup = await context.new_page()
        await warmup.goto(HOME_URL, wait_until="networkidle", timeout=60000)
        print("   → 首页停留5秒...")
        await asyncio.sleep(5)
        await warmup.close()
        print("   → 关闭热身Page，创建新Page访问用户主页...")
        
        page = await context.new_page()
        await page.goto(TOUTIAO_URL, wait_until="domcontentloaded", timeout=60000)
        await asyncio.sleep(3)
        
        result = await detect(page, "test03")
        await browser.close()
        return result


async def test_04_warmup_same_page():
    """测试4: 首页热身 → 同一Page跳转 (模拟旧代码)"""
    print("\n" + "="*60)
    print("🧪 测试4: 首页热身后，同一Page跳转用户主页 ⬅️ 旧代码模式")
    print("="*60)
    
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True, args=["--no-sandbox"])
        context = await browser.new_context(user_agent=DEFAULT_UA, viewport={"width": 1920, "height": 1080})
        page = await context.new_page()
        
        print("   → 访问首页...")
        await page.goto(HOME_URL, wait_until="networkidle", timeout=60000)
        print("   → 首页停留5秒...")
        await asyncio.sleep(5)
        
        print("   → 同一Page跳转用户主页...")
        await page.goto(TOUTIAO_URL, wait_until="domcontentloaded", timeout=60000)
        await asyncio.sleep(3)
        
        result = await detect(page, "test04")
        await browser.close()
        return result


async def test_05_scroll_15_times():
    """测试5: 强制滚动15次"""
    print("\n" + "="*60)
    print("🧪 测试5: 同一Page跳转 + 强制滚动15次")
    print("="*60)
    
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True, args=["--no-sandbox"])
        context = await browser.new_context(user_agent=DEFAULT_UA, viewport={"width": 1920, "height": 1080})
        page = await context.new_page()
        
        await page.goto(HOME_URL, wait_until="networkidle", timeout=60000)
        await asyncio.sleep(3)
        await page.goto(TOUTIAO_URL, wait_until="domcontentloaded", timeout=60000)
        
        print("   → 初始状态:")
        await detect(page, "test05_before")
        
        print("   → 强制滚动15次...")
        for i in range(15):
            await page.mouse.wheel(0, 500)
            await asyncio.sleep(1.5)
        
        print("   → 滚动后状态:")
        result = await detect(page, "test05_after")
        await browser.close()
        return result


async def test_06_wait_for_selector():
    """测试6: 等待特定选择器"""
    print("\n" + "="*60)
    print("🧪 测试6: 尝试wait_for_selector等待文章元素")
    print("="*60)
    
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True, args=["--no-sandbox"])
        context = await browser.new_context(user_agent=DEFAULT_UA, viewport={"width": 1920, "height": 1080})
        page = await context.new_page()
        
        await page.goto(HOME_URL, wait_until="networkidle", timeout=60000)
        await asyncio.sleep(3)
        await page.goto(TOUTIAO_URL, wait_until="domcontentloaded", timeout=60000)
        
        selectors = [
            'a[href*="/article/"]',
            'a[href*="/w/"]',
            '.feed-card-wrapper',
            '.feed-card',
            '[class*="feed"]',
            '[class*="article"]',
        ]
        
        for sel in selectors:
            try:
                print(f"   → 等待: {sel} ...")
                await page.wait_for_selector(sel, timeout=8000)
                print(f"      ✅ 找到!")
                break
            except:
                print(f"      ❌ 超时未找到")
        
        result = await detect(page, "test06")
        await browser.close()
        return result


async def test_07_with_stealth():
    """测试7: 使用stealth"""
    print("\n" + "="*60)
    print("🧪 测试7: 使用 playwright-stealth")
    print("="*60)
    
    if not HAS_STEALTH:
        print("   ⚠️ 未安装stealth，跳过")
        return None
    
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True, args=["--no-sandbox"])
        context = await browser.new_context(user_agent=DEFAULT_UA, viewport={"width": 1920, "height": 1080})
        page = await context.new_page()
        await stealth_async(page)
        
        await page.goto(HOME_URL, wait_until="networkidle", timeout=60000)
        await asyncio.sleep(3)
        await page.goto(TOUTIAO_URL, wait_until="domcontentloaded", timeout=60000)
        await asyncio.sleep(5)
        
        result = await detect(page, "test07")
        await browser.close()
        return result


async def test_08_long_wait_30s():
    """测试8: 超长等待30秒"""
    print("\n" + "="*60)
    print("🧪 测试8: 超长等待30秒（测试是否需要更多时间）")
    print("="*60)
    
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True, args=["--no-sandbox"])
        context = await browser.new_context(user_agent=DEFAULT_UA, viewport={"width": 1920, "height": 1080})
        page = await context.new_page()
        
        await page.goto(HOME_URL, wait_until="networkidle", timeout=60000)
        await page.goto(TOUTIAO_URL, wait_until="domcontentloaded", timeout=60000)
        
        print("   → 等待30秒...")
        for i in range(6):
            await asyncio.sleep(5)
            print(f"      已等待 {(i+1)*5} 秒...")
        
        result = await detect(page, "test08")
        await browser.close()
        return result


async def test_09_monitor_network():
    """测试9: 监控网络请求"""
    print("\n" + "="*60)
    print("🧪 测试9: 监控API网络请求")
    print("="*60)
    
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True, args=["--no-sandbox"])
        context = await browser.new_context(user_agent=DEFAULT_UA, viewport={"width": 1920, "height": 1080})
        page = await context.new_page()
        
        api_requests = []
        api_responses = []
        
        def on_request(req):
            url = req.url
            if any(k in url for k in ['api', 'feed', 'list', 'article', 'user']):
                api_requests.append(url)
        
        def on_response(resp):
            url = resp.url
            if any(k in url for k in ['api', 'feed', 'list', 'article', 'user']):
                api_responses.append({'url': url, 'status': resp.status})
        
        page.on('request', on_request)
        page.on('response', on_response)
        
        await page.goto(HOME_URL, wait_until="networkidle", timeout=60000)
        await page.goto(TOUTIAO_URL, wait_until="domcontentloaded", timeout=60000)
        await asyncio.sleep(10)
        
        print(f"\n   📡 API请求 ({len(api_requests)} 个):")
        for url in api_requests[:8]:
            print(f"      → {url[:90]}...")
        
        print(f"\n   📡 API响应 ({len(api_responses)} 个):")
        for r in api_responses[:8]:
            status_icon = "✅" if r['status'] == 200 else "❌"
            print(f"      {status_icon} [{r['status']}] {r['url'][:80]}...")
        
        # 保存完整日志
        with open(DEBUG_DIR / "test09_network.log", "w", encoding="utf-8") as f:
            f.write("=== REQUESTS ===\n")
            for url in api_requests:
                f.write(url + "\n")
            f.write("\n=== RESPONSES ===\n")
            for r in api_responses:
                f.write(f"[{r['status']}] {r['url']}\n")
        
        result = await detect(page, "test09")
        await browser.close()
        return result


async def test_10_different_viewport():
    """测试10: 不同视窗大小"""
    print("\n" + "="*60)
    print("🧪 测试10: 使用较小视窗 1366x768")
    print("="*60)
    
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True, args=["--no-sandbox"])
        context = await browser.new_context(
            user_agent=DEFAULT_UA, 
            viewport={"width": 1366, "height": 768}
        )
        page = await context.new_page()
        
        await page.goto(HOME_URL, wait_until="networkidle", timeout=60000)
        await asyncio.sleep(3)
        await page.goto(TOUTIAO_URL, wait_until="domcontentloaded", timeout=60000)
        await asyncio.sleep(5)
        
        result = await detect(page, "test10")
        await browser.close()
        return result


# ============= 主程序 =============

async def main():
    print("="*60)
    print("🔍 头条用户主页加载诊断工具")
    print(f"⏰ 测试时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"🎯 目标URL: {TOUTIAO_URL[:60]}...")
    print(f"📁 输出目录: {DEBUG_DIR}")
    print(f"🛡️ Stealth: {'可用' if HAS_STEALTH else '不可用'}")
    print("="*60)
    
    all_results = {}
    
    tests = [
        ("01_直接访问_domcontentloaded", test_01_direct_domcontentloaded),
        ("02_直接访问_networkidle", test_02_direct_networkidle),
        ("03_热身后_新Page", test_03_warmup_new_page),
        ("04_热身后_同Page跳转", test_04_warmup_same_page),
        ("05_强制滚动15次", test_05_scroll_15_times),
        ("06_等待选择器", test_06_wait_for_selector),
        ("07_使用stealth", test_07_with_stealth),
        ("08_超长等待30秒", test_08_long_wait_30s),
        ("09_监控网络请求", test_09_monitor_network),
        ("10_小视窗1366x768", test_10_different_viewport),
    ]
    
    for name, func in tests:
        try:
            result = await func()
            all_results[name] = result
        except Exception as e:
            print(f"   ❌ 测试异常: {e}")
            all_results[name] = None
        
        await asyncio.sleep(2)
    
    # 汇总
    print("\n" + "="*60)
    print("📊 测试结果汇总")
    print("="*60)
    
    for name, result in all_results.items():
        if result:
            count = result.get('articleLinks', 0)
            icon = "✅" if count > 0 else "❌"
            print(f"   {icon} {name}: 文章链接={count}")
        else:
            print(f"   ⚠️ {name}: 无结果")
    
    print(f"\n📁 所有文件已保存到: {DEBUG_DIR}")
    print("请检查截图和HTML文件进行进一步分析")


if __name__ == "__main__":
    asyncio.run(main())
