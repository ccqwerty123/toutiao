import asyncio
import json
from datetime import datetime
from pathlib import Path

from playwright.async_api import async_playwright

# 这里换成你的主页地址（带 token 的那种）
TOUTIAO_URL = "https://www.toutiao.com/c/user/token/CiyRLPHkUyTCD9FmHodOGQVcmZh5-NRKyfiTSF0XMms-tSja0FdhrUWRp-T-DBpJCjwAAAAAAAAAAAAAT8lExjCbDHcWTgszQQjqU0Ohh9qtuXbuEOe6CQdqJEZ7yIpoM-NJ93_Sty1iMpOe_FUQ9ZmDDhjDxYPqBCIBA9GPpzc="

OUTPUT_DIR = Path("data")
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
LINKS_FILE = OUTPUT_DIR / "toutiao_links.json"
DEBUG_HTML = OUTPUT_DIR / "debug_toutiao.html"
DEBUG_PNG = OUTPUT_DIR / "debug_toutiao.png"


async def main():
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        page = await browser.new_page()

        print(f"[INFO] 打开页面: {TOUTIAO_URL}")
        await page.goto(TOUTIAO_URL, wait_until="networkidle", timeout=60000)

        title = await page.title()
        current_url = page.url
        print(f"[INFO] 页面标题: {title}")
        print(f"[INFO] 当前 URL: {current_url}")

        # 等几秒，保证首屏渲染完成
        await page.wait_for_timeout(3000)

        # 向下滚动几次，触发更多内容加载
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

        # 控制台打印前几条，方便在 Actions 日志里直接看到
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

        # 如果一条都没抓到，保存HTML和截图用于调试
        if len(unique_links) == 0:
            print(
                "[WARN] 没有抓到任何链接，将保存调试 HTML 和截图，方便排查。"
            )
            html_content = await page.content()
            DEBUG_HTML.write_text(html_content, encoding="utf-8")
            await page.screenshot(path=str(DEBUG_PNG), full_page=True)
            print(f"[INFO] 已保存调试 HTML: {DEBUG_HTML}")
            print(f"[INFO] 已保存调试截图: {DEBUG_PNG}")

        await browser.close()


if __name__ == "__main__":
    asyncio.run(main())
