import asyncio
import json
from datetime import datetime
from pathlib import Path

from playwright.async_api import async_playwright

# 这里换成你的主页地址（可以先不用 ?wid 参数，让前端自己加）
TOUTIAO_URL = "https://www.toutiao.com/c/user/token/CiyRLPHkUyTCD9FmHodOGQVcmZh5-NRKyfiTSF0XMms-tSja0FdhrUWRp-T-DBpJCjwAAAAAAAAAAAAAT8lExjCbDHcWTgszQQjqU0Ohh9qtuXbuEOe6CQdqJEZ7yIpoM-NJ93_Sty1iMpOe_FUQ9ZmDDhjDxYPqBCIBA9GPpzc="

OUTPUT_DIR = Path("data")
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
LINKS_FILE = OUTPUT_DIR / "toutiao_links.json"
DEBUG_HTML = OUTPUT_DIR / "debug_toutiao.html"
DEBUG_PNG = OUTPUT_DIR / "debug_toutiao.png"
RAW_HTML = OUTPUT_DIR / "raw_goto_response.html"


async def main():
    async with async_playwright() as p:
        # 尽量伪装成正常桌面浏览器
        browser = await p.chromium.launch(
            headless=True,
            args=[
                "--disable-blink-features=AutomationControlled",
            ],
        )
        context = await browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/120.0.0.0 Safari/537.36"
            ),
            locale="zh-CN",
            timezone_id="Asia/Shanghai",
            viewport={"width": 1280, "height": 720},
        )
        page = await context.new_page()

        # 打印浏览器控制台日志，方便调试
        page.on(
            "console",
            lambda msg: print(f"[BROWSER_CONSOLE] {msg.type}: {msg.text}")
        )

        # 打印关键接口返回情况（只打印含 user/feed 的）
        async def log_response(response):
            url = response.url
            if "api/pc/list/user/feed" in url:
                try:
                    status = response.status
                    print(f"[XHR] {status} {url}")
                except Exception:
                    pass

        page.on("response", log_response)

        # 先访问一次首页，很多站点会在这里发 cookie（比如 ttwid）
        print("[INFO] 先访问 toutiao 首页，获取初始 cookie...")
        home_resp = await page.goto(
            "https://www.toutiao.com/",
            wait_until="domcontentloaded",
            timeout=60000,
        )
        if home_resp:
            print(f"[INFO] 首页状态码: {home_resp.status}")
        await page.wait_for_timeout(3000)

        # 再访问你的主页
        print(f"[INFO] 打开主页: {TOUTIAO_URL}")
        resp = await page.goto(
            TOUTIAO_URL,
            wait_until="domcontentloaded",
            timeout=60000,
        )

        if resp is None:
            print("[WARN] goto 返回的 Response 是 None，可能是通过 JS 跳转。")
        else:
            status = resp.status
            print(f"[INFO] 主页首包状态码: {status}")
            try:
                text = await resp.text()
                # 保存一份原始首包 HTML，方便排查（不一定是最终 DOM）
                RAW_HTML.write_text(text, encoding="utf-8")
                print("[INFO] 已保存首包 HTML 到:", RAW_HTML)
                print("[INFO] 首包 HTML 前 400 字符预览：")
                print(text[:400].replace("\n", " ")[:400])
            except Exception as e:
                print("[WARN] 无法读取首包 HTML:", e)

        # 等待网络空闲 + 再多等几秒，给前端 JS 充足时间渲染
        await page.wait_for_load_state("networkidle")
        await page.wait_for_timeout(5000)

        title = await page.title()
        current_url = page.url
        print(f"[INFO] 页面标题: {repr(title)}")
        print(f"[INFO] 当前 URL: {current_url}")

        # 再滚动几次，触发更多内容加载
        for i in range(3):
            print(f"[INFO] 向下滚动第 {i + 1} 次...")
            await page.evaluate(
                "window.scrollBy(0, document.body.scrollHeight / 2)"
            )
            await page.wait_for_timeout(2000)

        print("[INFO] 开始从 DOM 中提取文章链接...")

        # 提取所有 <a>，筛选出像文章的链接（/article/ 或 /group/）
        links = await page.eval_on_selector_all(
            "a",
            """elements => elements
                .map(a => ({ href: a.href, text: a.innerText.trim() }))
                .filter(x =>
                    x.href &&
                    (x.href.includes('/article/') || x.href.includes('/group/')) &&
                    x.text.length > 0
                )
            """,
        )

        # 去重
        seen = set()
        unique_links = []
        for l in links:
            href = l["href"]
            if href not in seen:
                seen.add(href)
                unique_links.append(l)

        print(
            f"[INFO] 共提取到链接 {len(unique_links)} 条（去重前 {len(links)} 条）。"
        )

        # 控制台打印前几条
        for i, item in enumerate(unique_links[:10], start=1):
            print(f"[DEBUG] #{i} {item['href']}  标题: {item['text'][:50]}")

        timestamp = datetime.utcnow().isoformat() + "Z"
        result = {
            "source_url": TOUTIAO_URL,
            "scraped_at": timestamp,
            "count": len(unique_links),
            "links": unique_links,
        }

        LINKS_FILE.write_text(
            json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        print(f"[INFO] 已写入文件: {LINKS_FILE}")

        # 不管有没有抓到，都保存一份最终 DOM 和截图，方便你调试
        html_content = await page.content()
        DEBUG_HTML.write_text(html_content, encoding="utf-8")
        await page.screenshot(path=str(DEBUG_PNG), full_page=True)
        print(f"[INFO] 已保存最终 DOM HTML: {DEBUG_HTML}")
        print(f"[INFO] 已保存最终截图: {DEBUG_PNG}")

        await browser.close()


if __name__ == "__main__":
    asyncio.run(main())
