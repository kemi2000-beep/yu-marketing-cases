#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
飞书「策略平台案例库」周更抓取脚本
====================================
功能：从飞书公开表格抓取营销案例数据，解析为结构化 JSON，供网页展示。
用法：
    python3 scraper.py              # 抓取并输出 data.json
    python3 scraper.py --output /path/data.json
    python3 scraper.py --cache /tmp/feishu_clipboard.txt  # 用本地缓存调试，跳过浏览器
依赖：playwright (pip install playwright && playwright install chromium)
作者：策略平台自动化
"""

import argparse
import csv
import json
import os
import re
import sys
from datetime import datetime
from pathlib import Path

# ============================================================
# 一、飞书表格抓取（Playwright 模拟复制）
# ============================================================

FEISHU_URL = "https://ydsmarketing.feishu.cn/sheets/OIzTsirIPhe3NztPMsmcewfHnnh"

def fetch_from_feishu(timeout_ms: int = 50000) -> str:
    """访问飞书公开表格，全选复制，返回 TSV 文本。"""
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        sys.exit("[错误] 需要安装 playwright：pip install playwright && playwright install chromium")

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True, args=["--no-sandbox", "--disable-gpu"])
        context = browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                       "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            viewport={"width": 1920, "height": 1080},
            permissions=["clipboard-read", "clipboard-write"],
        )
        page = context.new_page()
        print(f"[1/4] 正在访问飞书表格…")
        try:
            page.goto(FEISHU_URL, wait_until="networkidle", timeout=timeout_ms)
        except Exception as e:
            print(f"  (页面加载提示: {e})")
        page.wait_for_timeout(8000)

        # 点击表格画布并全选复制
        canvas = page.query_selector("canvas")
        if canvas:
            box = canvas.bounding_box()
            if box:
                page.mouse.click(box["x"] + 120, box["y"] + 80)
        page.wait_for_timeout(800)
        print(f"[2/4] 全选复制表格内容…")
        page.keyboard.press("Control+a")
        page.wait_for_timeout(800)
        page.keyboard.press("Control+a")  # 二次全选确保覆盖整表
        page.wait_for_timeout(800)
        page.keyboard.press("Control+c")
        page.wait_for_timeout(2500)

        clip = page.evaluate("() => navigator.clipboard.readText()")
        browser.close()
        print(f"[3/4] 已获取表格数据（{len(clip)} 字符）")
        return clip


# ============================================================
# 二、TSV 解析为结构化案例数据
# ============================================================

URL_RE = re.compile(r"https?://[^\s，。）)\"'\u201c\u201d\u3001；;]+|小程序://[^\s，。）)\"'\u201c\u201d]+")

def clean_url(url: str) -> str:
    """清理 URL 末尾多余标点。"""
    return url.rstrip("?=.&;:，。；、）)")

def split_case(text: str):
    """从「案例名称及链接」单元格中分离出标题与链接。"""
    text = (text or "").strip()
    if not text:
        return None
    urls = URL_RE.findall(text)
    title = URL_RE.sub("", text)
    title = re.sub(r"案例链接[：:\s]*", "", title)
    title = title.strip().strip("\n").strip()
    title = re.sub(r"\s+", " ", title)
    if not title:
        # 整个单元格就是链接
        title = "（未命名案例）"
    link = clean_url(urls[0]) if urls else ""
    return {"title": title, "link": link}

def is_week_row(r: list) -> bool:
    """判断是否为周次分隔行：列0 含两个「年X月」日期且较短。"""
    if not r or not r[0].strip():
        return False
    s = r[0].strip()
    if len(s) > 45:
        return False
    dates = re.findall(r"\d{4}年\d+月", s)
    return len(dates) >= 2

def norm_week(s: str) -> str:
    """标准化周次：「2026年6月-29日-2026年7月-5日」→「2026-06-29 ~ 2026-07-05」。"""
    s = s.strip()
    dates = re.findall(r"(\d{4})年(\d+)月[-]?(\d+)日?", s)
    if len(dates) >= 2:
        d1 = f"{dates[0][0]}-{int(dates[0][1]):02d}-{int(dates[0][2]):02d}"
        d2 = f"{dates[1][0]}-{int(dates[1][1]):02d}-{int(dates[1][2]):02d}"
        return f"{d1} ~ {d2}"
    if len(dates) == 1:
        return f"{dates[0][0]}-{int(dates[0][1]):02d}-{int(dates[0][2]):02d}"
    return s

def week_sort_key(week_str: str):
    """生成排序键，用于按时间倒序（最新在前）。"""
    m = re.match(r"(\d{4})-(\d{2})-(\d{2})", week_str)
    if m:
        return f"{m.group(1)}-{m.group(2)}-{m.group(3)}"
    return "0000-00-00"

def parse_tsv(raw: str) -> dict:
    """将 TSV 文本解析为结构化数据。"""
    rows = list(csv.reader(raw.split("\n"), delimiter="\t"))

    weeks = []
    current_week = None
    current_cases = []

    for i, r in enumerate(rows):
        if i < 2:  # 跳过标题行
            continue
        while len(r) < 7:
            r.append("")

        if is_week_row(r):
            if current_week:
                weeks.append({"week": current_week, "cases": current_cases})
            current_week = norm_week(r[0])
            current_cases = []
            _add_cases(r, current_cases)
        else:
            _add_cases(r, current_cases)

    if current_week:
        weeks.append({"week": current_week, "cases": current_cases})

    # 过滤空周 + 按时间倒序
    weeks = [w for w in weeks if w["cases"]]
    weeks.sort(key=lambda w: week_sort_key(w["week"]), reverse=True)

    # 编号 + 统计
    for idx, w in enumerate(weeks):
        auto_n = sum(1 for c in w["cases"] if c["category"] == "汽车营销")
        cross_n = sum(1 for c in w["cases"] if c["category"] == "跨界营销")
        w["index"] = idx
        w["auto_count"] = auto_n
        w["cross_count"] = cross_n

    total_auto = sum(w["auto_count"] for w in weeks)
    total_cross = sum(w["cross_count"] for w in weeks)

    return {
        "meta": {
            "source": FEISHU_URL,
            "updated_at": datetime.now().strftime("%Y-%m-%d %H:%M"),
            "total_weeks": len(weeks),
            "total_auto": total_auto,
            "total_cross": total_cross,
            "total_cases": total_auto + total_cross,
            "latest_week": weeks[0]["week"] if weeks else "",
        },
        "weeks": weeks,
    }

def _add_cases(r: list, cases: list):
    """从一行中提取汽车案例与跨界案例。"""
    if r[3].strip():
        c = split_case(r[3])
        if c:
            cases.append({"category": "汽车营销", **c, "reason": r[4].strip()})
    if r[5].strip():
        c = split_case(r[5])
        if c:
            cases.append({"category": "跨界营销", **c, "reason": r[6].strip()})


def build_standalone(data: dict, html_path: str = "index.html", out_path: str = "index_standalone.html"):
    """生成内联数据的独立预览版 HTML（可直接双击打开，无需服务器）。"""
    html_path = Path(html_path)
    if not html_path.exists():
        return
    html = html_path.read_text(encoding="utf-8")
    inline = f'<script>window.__CASE_DATA__={json.dumps(data, ensure_ascii=False)}</script>'
    standalone = html.replace("<body>", "<body>\n" + inline, 1)
    Path(out_path).write_text(standalone, encoding="utf-8")
    print(f"   独立版: {Path(out_path).resolve()}")


# ============================================================
# 三、主流程
# ============================================================

def main():
    parser = argparse.ArgumentParser(description="飞书案例库抓取脚本")
    parser.add_argument("--output", "-o", default="data.json", help="输出 JSON 路径")
    parser.add_argument("--cache", default=None, help="使用本地缓存 TSV 调试（跳过浏览器）")
    args = parser.parse_args()

    if args.cache and os.path.exists(args.cache):
        print(f"[调试] 使用本地缓存：{args.cache}")
        with open(args.cache, "r") as f:
            raw = f.read()
    else:
        raw = fetch_from_feishu()

    print("[4/4] 解析数据并生成 JSON…")
    data = parse_tsv(raw)

    out_path = Path(args.output)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

    m = data["meta"]
    print(f"\n✅ 完成！")
    print(f"   周数: {m['total_weeks']}  |  汽车案例: {m['total_auto']}  |  跨界案例: {m['total_cross']}")
    print(f"   最新周次: {m['latest_week']}")
    print(f"   数据文件: {out_path.resolve()}")

    # 生成独立预览版（内联数据，可直接打开）
    standalone_path = out_path.parent / "index_standalone.html"
    build_standalone(data, str(out_path.parent / "index.html"), str(standalone_path))


if __name__ == "__main__":
    main()
