import os
import re
from flask import Flask, request, jsonify
import requests
from cachetools import TTLCache

app = Flask(__name__)
cache = TTLCache(maxsize=100, ttl=1800)

COOKIE = os.environ.get("SCREENER_COOKIE", "")
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": "https://www.screener.in/",
    "Cookie": "sessionid=" + COOKIE
}


def fetch_screener(ticker):
    if ticker in cache:
        return cache[ticker]
    for mode in ["consolidated", ""]:
        if mode:
            url = "https://www.screener.in/company/" + ticker.upper() + "/" + mode + "/"
        else:
            url = "https://www.screener.in/company/" + ticker.upper() + "/"
        try:
            r = requests.get(url, headers=HEADERS, timeout=20)
            if "top-ratios" in r.text:
                cache[ticker] = r.text
                return r.text
        except Exception as e:
            pass
    return None


def parse_top_ratios(html):
    result = {}

    sec = None
    for p in [r'id=["\']top-ratios["\'][^>]*>(.*?)', r'top-ratios[^>]*>(.*?)']:
        m = re.search(p, html, re.S | re.I)
        if m:
            sec = m.group(1)
            break

    if not sec:
        return result

    items = re.findall(r']*>(.*?)', sec, re.S | re.I)

    for item in items:
        spans = re.findall(r']*>(.*?)', item, re.S | re.I)
        texts = []
        for s in spans:
            t = re.sub(r'<[^>]+>', '', s).strip()
            if t:
                texts.append(t)

        if len(texts) < 2:
            pass
        else:
            name = texts[0]
            val = texts[-1]
            raw = re.sub(r'[₹%,\s]', '', val)
            nums = re.findall(r'[\d.]+', raw)
            key = re.sub(r'[\s/\-]', '', name).lower()
            if key:
                if nums:
                    result[key] = float(nums[0])
                else:
                    result[key] = raw
                result['_label_' + key] = name

    return result


def parse_ttm(html, row_label):
    pattern = re.compile(r']*>.*?' + re.escape(row_label) + r'.*?', re.S | re.I)
    m = pattern.search(html)
    if not m:
        return None
    cells = re.findall(r']*>(.*?)', m.group(0), re.S | re.I)
    if not cells:
        return None
    last = re.sub(r'<[^>]+>', '', cells[-1]).replace(',', '').strip()
    try:
        return float(last)
    except Exception:
        return None


def parse_roce_by_year(html, year):
    p1 = re.compile(r'ROCE[^<]{0,200}?' + str(year) + r'[^<]{0,100}?([\d.]+)', re.S | re.I)
    p2 = re.compile(str(year) + r'[^<]{0,300}?ROCE[^<]{0,100}?([\d.]+)', re.S | re.I)
    for p in [p1, p2]:
        m = p.search(html)
        if m:
            return float(m.group(1))
    return None


METRIC_MAP = {
    "MARKETCAP": "marketcap",
    "BOOKVALUE": "bookvalue",
    "PE": "stockpe",
    "ROE": "roe",
    "ROCE": "roce",
    "DIVIDENDYIELD": "dividendyield",
    "PRICE": "currentprice",
    "SALESGROWTH3Y": "salesgrowth3years",
    "SALESGROWTH5Y": "salesgrowth5years",
    "SALESGROWTH10Y": "salesvar10yrs",
    "PROFITGROWTH3Y": "profitvar3yrs",
    "PROFITGROWTH5Y": "profitvar5yrs",
    "PROFITGROWTH10Y": "profitvar10yrs",
    "ROE3Y": "roe3yr",
    "ROE5Y": "roe5yr",
    "ROE10Y": "roe10yr",
    "CAGR3Y": "3years",
    "CAGR5Y": "5years",
    "CAGR10Y": "10years",
    "FII": "fiiholding",
    "DII": "diiholding",
}


@app.route("/stock")
def stock():
    ticker = request.args.get("ticker", "").upper().strip()
    metric = request.args.get("metric", "").upper().replace(" ", "").replace("_", "")

    if not ticker or not metric:
        return jsonify({"error": "ticker and metric required"}), 400

    html = fetch_screener(ticker)
    if not html:
        return jsonify({"error": "Could not fetch Screener — check cookie"}), 503

    ratios = parse_top_ratios(html)

    if metric == "PB":
        price = ratios.get("currentprice")
        bv = ratios.get("bookvalue")
        if price and bv and float(bv) != 0:
            return jsonify({"value": round(float(price) / float(bv), 2)})
        return jsonify({"value": "N/A"})

    if metric == "SALESTTM":
        v = parse_ttm(html, "Sales")
        return jsonify({"value": v if v is not None else "N/A"})

    if metric == "PATTTM":
        v = parse_ttm(html, "Net Profit")
        return jsonify({"value": v if v is not None else "N/A"})

    if metric in ("ROCE2023", "ROCE2024", "ROCE2025"):
        year = metric.replace("ROCE", "")
        v = parse_roce_by_year(html, year)
        return jsonify({"value": v if v is not None else "N/A"})

    lookup = METRIC_MAP.get(metric)
    if not lookup:
        return jsonify({"error": "Unknown metric: " + metric}), 400

    value = ratios.get(lookup, "N/A")
    return jsonify({"value": value})


@app.route("/debug-section")
def debug_section():
    ticker = request.args.get("ticker", "RELIANCE").upper()
    html = fetch_screener(ticker)
    if not html:
        return jsonify({"error": "no html"})
    m = re.search(r'id=["\']top-ratios["\'][^>]*>(.*?)', html, re.S | re.I)
    if not m:
        idx = html.find("top-ratios")
        return jsonify({
            "found": False,
            "context": html[max(0, idx - 100):idx + 500] if idx != -1 else "NOT FOUND"
        })
    section = m.group(1)
    return jsonify({
        "found": True,
        "section_length": len(section),
        "section_html": section[:2000]
    })


@app.route("/debug-labels")
def debug_labels():
    ticker = request.args.get("ticker", "RELIANCE").upper()
    html = fetch_screener(ticker)
    if not html:
        return jsonify({"error": "no html"})
    ratios = parse_top_ratios(html)
    labels = {}
    for k, v in ratios.items():
        if k.startswith('_label_'):
            labels[k.replace('_label_', '')] = v
    return jsonify({"total_found": len(labels), "labels": labels})


@app.route("/debug-raw")
def debug_raw():
    ticker = request.args.get("ticker", "RELIANCE").upper()
    html = fetch_screener(ticker)
    if not html:
        return jsonify({"error": "fetch returned nothing"})
    return jsonify({
        "length": len(html),
        "has_top_ratios": "top-ratios" in html,
        "has_login": "login" in html.lower(),
        "first_500": html[:500]
    })


@app.route("/health")
def health():
    return jsonify({"status": "ok", "cookie_set": bool(COOKIE)})


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
