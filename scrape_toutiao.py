import asyncio
import json
from datetime import datetime
from pathlib import Path

from playwright.async_api import async_playwright

# 改成你的主页地址
TOUTIAO_URL = "https://www.toutiao.com/c/user/token/你的token"

OUTPUT_DIR = Path("data")
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
LINKS_FILE = OUTPUT_DIR / "toutiao_links.json"
DEBUG_HTML = OUTPUT_DIR / "debug_toutiao.html"
DEBUG_PNG = OUTPUT_DIR / "debug_toutiao.png"
RAW_HTML = OUTPUT_DIR / "raw_goto_response.html"


# JS：统计当前页面上“识别为作品”的链接个数
ARTICLE_COUNT_JS = r"""
() => {
  const anchors = Array.from(document.querySelectorAll("a[href]"));
  const origin = window.location.origin;

  const isArticleLike = (href) => {
    if (!href) return false;
    if (href === "/" || href === "#" || href.trim() === "") return false;
    if (href.startsWith("sslocal://")) return false; // APP 内链

    try {
      const url = new URL(href, origin);
      if (url.origin !== origin) return false;

      const path = url.pathname || "/";

      // 排除明显不是作品的路径
      if (path.startsWith("/c/user/")) return false;
      if (path.startsWith("/license")) return false;
      if (path.startsWith("/business_license")) return false;
      if (path.startsWith("/a3642705768")) return false; // 跟帖评论自律管理承诺书

      const segments = path.split("/").filter(Boolean);
      if (segments.length === 0) return false;
      const last = segments[segments.length - 1];

      const pure = last.split("#")[0].split("?")[0];
      if (!pure) return false;

      const digits = pure.replace(/\D/g, "").length;
      if (digits < 6) return false;                 // 至少一定长度的数字
      if (digits / pure.length < 0.6) return false; // 大部分字符是数字

      return true;
    } catch (e) {
      return false;
    }
  };

  const set = new Set();
  for (const a of anchors) {
    const rawHref = a.getAttribute("href") || "";
    if (!isArticleLike(rawHref)) continue;
    const url = new URL(rawHref, origin);
    const canonical = origin + url.pathname; // 去掉 query/hash
    set.add(canonical);
  }
  return set.size;
}
"""


# JS：真正提取作品链接 + 文本
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
      if (path.startsWith("/a3642705768")) return false;

      const segments = path.split("/").filter(Boolean);
      if (segments.length === 0) return false;
      const last = segments[segments.length - 1];

      const pure = last.split("#")[0].split("?")[0];
      if (!pure) return false;

      const digits = pure.replace(/\D/g, "").length;
      if (digits < 6) return false;
      if (digits / pure.length < 0.6) return false;

      return true;
    } catch (e) {
      return false;
    }
  };

  // 文本中包含这些词的，多半是底部版权/举报等非作品链接
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
    if (!entry) {
      entry = { href: canonical, texts: [] };
      map.set(canonical, entry);
    }
    entry.texts.push(text);
  }

  const results = [];
  for (const { href, texts } of map.values()) {
    let title = href;
    if (texts && texts.length > 0) {
      title = texts.reduce(
        (best, cur) => (cur.length > best.length ? cur : best),
        texts[0]
      );
    }
    results.push({ href, text: title });
  }

  return results;
}
"""


async def slow_scroll_load(page):
    """
    慢慢下滑加载更多内容：
    - 每次滑到底部后等几秒
    - 统计“识别到的作品链接数量”，如果连续几轮没有增长，就停止
    """
    max_scrolls = 40          # 最多下滑 40 次
    wait_after_scroll = 5000  # 每次滑动后等待 5 秒（毫秒）
    no_growth_limit = 4       # 连续 4 次没有新作品就停止

    last_count = 0
    same_count_times = 0

    for i in range(max_scrolls):
        count_before = await page.evaluate(ARTICLE_COUNT_JS)
        print(f"[SCROLL] 第 {i + 1} 轮前，识别到作品数：{count_before}")

        print("[SCROLL] 滑动到页面底部...")
        await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
        await page.wait_for_timeout(wait_after_scroll)

        count_after = await page.evaluate(ARTICLE_COUNT_JS)
        print(f"[SCROLL] 第 {i + 1} 轮后，作品数：{count_after}")

        if count_after <= last_count:
            same_count_times += 1
        else:
            same_count_times = 0

        last_count = count_after

        if same_count_times >= no_growth_limit:
            print("[SCROLL] 连续多次没有新增作品，认为已经到底，停止下滑。")
            break

    print(f"[SCROLL] 下滑结束，最终识别到作品数：{last_count}")


async def main():
    async with async_playwright() as p:
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

        print("[INFO] 先访问 toutiao 首页，获取初始 cookie...")
        home_resp = await page.goto(
            "https://www.toutiao.com/",
            wait_until="domcontentloaded",
            timeout=60000,
        )
        if home_resp:
            print(f"[INFO] 首页状态码: {home_resp.status}")
        await page.wait_for_timeout(3000)

        print(f"[INFO] 打开主页: {TOUTIAO_URL}")
        resp = await page.goto(
            TOUTIAO_URL,
            wait_until="domcontentloaded",
            timeout=60000,
        )

        page_ok = False
        if resp is None:
            print("[WARN] goto 返回的 Response 是 None，可能是通过 JS 重定向。")
        else:
            status = resp.status
            print(f"[INFO] 主页首包状态码: {status}")
            if 200 <= status < 400:
                page_ok = True
            else:
                print("[WARN] 主页 HTTP 状态码异常，将视为打开失败。")

            try:
                text = await resp.text()
                RAW_HTML.write_text(text, encoding="utf-8")
                print("[INFO] 已保存首包 HTML 到:", RAW_HTML)
                print("[INFO] 首包 HTML 前 400 字符预览：")
                print(text[:400].replace("\n", " ")[:400])
            except Exception as e:
                print("[WARN] 无法读取首包 HTML:", e)

        # 首屏加载后多等一会，给 JS / 首次接口时间
        await page.wait_for_load_state("networkidle")
        await page.wait_for_timeout(10000)

        title = await page.title()
        current_url = page.url
        print(f"[INFO] 页面标题: {repr(title)}")
        print(f"[INFO] 当前 URL: {current_url}")

        # 慢慢下滑，尽量把所有作品加载出来
        await slow_scroll_load(page)

        print("[INFO] 开始从页面所有链接中识别作品链接...")
        links = await page.evaluate(EXTRACT_ARTICLES_JS)

        print(f"[INFO] 共识别到作品链接 {len(links)} 条。")
        for i, item in enumerate(links[:20], start=1):
            print(f"[DEBUG] #{i} {item['href']}  标题: {item['text'][:50]}")

        # ===== 决定是否更新数据文件 =====
        should_update = False
        if not page_ok:
            print("[WARN] 主页打开失败（HTTP 状态码异常），不更新 toutiao_links.json，保留历史数据。")
        elif len(links) == 0:
            print("[WARN] 没有识别到任何作品链接，不更新 toutiao_links.json，保留历史数据。")
        else:
            should_update = True

        timestamp = datetime.utcnow().isoformat() + "Z"
        result = {
            "source_url": TOUTIAO_URL,
            "scraped_at": timestamp,
            "count": len(links),
            "links": links,
        }

        if should_update:
            LINKS_FILE.write_text(
                json.dumps(result, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            print(f"[INFO] 已更新文件: {LINKS_FILE}")
        else:
            print("[INFO] 本次运行不更新 toutiao_links.json（避免用空数据覆盖历史有效数据）。")

        # 无论是否更新数据文件，都保存最终 DOM 和截图，方便调试
        html_content = await page.content()
        DEBUG_HTML.write_text(html_content, encoding="utf-8")
        await page.screenshot(path=str(DEBUG_PNG), full_page=True)
        print(f"[INFO] 已保存最终 DOM HTML: {DEBUG_HTML}")
        print(f"[INFO] 已保存最终截图: {DEBUG_PNG}")

        await browser.close()


if __name__ == "__main__":
    asyncio.run(main())
