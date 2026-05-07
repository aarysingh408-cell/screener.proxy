import os
import re
from flask import Flask, request, jsonify
import requests
from cachetools import TTLCache

app = Flask(__name__)
cache = TTLCache(maxsize=100, ttl=1800)

COOKIE = os.environ.get("SCREENER_COOKIE", "")
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": "https://www.screener.in/",
    "Cookie": "sessionid=" + COOKIE
}

API_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Accept": "application/json, text/javascript, */*; q=0.01",
    "Accept-Language": "en-US,en;q=0.9",
    "X-Requested-With": "XMLHttpRequest",
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
        except Exception:
            pass
    return None


def get_company_id(html):
    m = re.search(r'data-company-id=["\'](\d+)["\']', html, re.I)
    if m:
        return m.group(1)
    return None


def extract_ul_section(html, section_id):
    marker = 'id="' + section_id + '"'
    start = html.find(marker)
    if start == -1:
        return ""
    open_end = html.find('>', start)
    if open_end == -1:
        return ""
    pos = open_end + 1
    depth = 1
    while pos < len(html) and depth > 0:
        next_open = html.find('<ul', pos)
        next_close = html.find('</ul>', pos)
        if next_close == -1:
            break
        if next_open != -1 and next_open < next_close:
            depth += 1
            pos = next_open + 3
        else:
            depth -= 1
            if depth == 0:
                return html[open_end + 1:next_close]
            pos = next_close + 5
    return ""


def parse_ul_section(section):
    result = {}
    if not section:
        return result
    items = re.findall(r'<li[^>]*>(.*?)</li>', section, re.S | re.I)
    for item in items:
        nm = re.search(r'class="name"[^>]*>(.*?)</span>', item, re.S | re.I)
        if not nm:
            continue
        name = re.sub(r'<[^>]+>', '', nm.group(1)).strip()
        if not name:
            continue
        vl = re.search(r'class="number"[^>]*>(.*?)</span>', item, re.S | re.I)
        if not vl:
            continue
        raw = re.sub(r'<[^>]+>', '', vl.group(1)).strip()
        raw = re.sub(r'[,\s]', '', raw)
        nums = re.findall(r'[\d.]+', raw)
        key = re.sub(r'[\s/\-]', '', name).lower()
        if key:
            result[key] = float(nums[0]) if nums else raw
            result['_label_' + key] = name
    return result


def parse_ttm(html, row_label):
    pattern = re.compile(r'<tr[^>]*>.*?' + re.escape(row_label) + r'.*?</tr>', re.S | re.I)
    m = pattern.search(html)
    if not m:
        return None
    cells = re.findall(r'<td[^>]*>(.*?)</td>', m.group(0), re.S | re.I)
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
    "MARKETCAP":       "marketcap",
    "BOOKVALUE":       "bookvalue",
    "PE":              "stockpe",
    "ROE":             "roe",
    "ROCE":            "roce",
    "DIVIDENDYIELD":   "dividendyield",
    "PRICE":           "currentprice",
    "SALESGROWTH3Y":   "salesgrowth3years",
    "SALESGROWTH5Y":   "salesgrowth5years",
    "SALESGROWTH10Y":  "salesvar10yrs",
    "PROFITGROWTH3Y":  "profitvar3yrs",
    "PROFITGROWTH5Y":  "profitvar5yrs",
    "PROFITGROWTH10Y": "profitvar10yrs",
    "ROE3Y":           "roe3yr",
    "ROE5Y":           "roe5yr",
    "ROE10Y":          "roe10yr",
    "CAGR3Y":          "returnover3years",
    "CAGR5Y":          "returnover5years",
    "CAGR10Y":         "returnover10years",
    "FII":             "fiiholding",
    "DII":             "diiholding",
}


@app.route("/stock")
def stock():
    ticker = request.args.get("ticker", "").upper().strip()
    metric = request.args.get("metric", "").upper().replace(" ", "").replace("_", "")
    if not ticker or not metric:
        return jsonify({"error": "ticker and metric required"}), 400
    html = fetch_screener(ticker)
    if not html:
        return jsonify({"error": "Could not fetch Screener - check cookie"}), 503
    ratios = parse_ul_section(extract_ul_section(html, "top-ratios"))
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


@app.route("/debug-labels")
def debug_labels():
    ticker = request.args.get("ticker", "RELIANCE").upper()
    html = fetch_screener(ticker)
    if not html:
        return jsonify({"error": "no html"})
    ratios = parse_ul_section(extract_ul_section(html, "top-ratios"))
    labels = {k.replace('_label_', ''): v for k, v in ratios.items() if k.startswith('_label_')}
    values = {k: v for k, v in ratios.items() if not k.startswith('_label_')}
    return jsonify({"total_found": len(labels), "labels": labels, "values": values})


@app.route("/debug-api-try")
def debug_api_try():
    ticker = request.args.get("ticker", "RELIANCE").upper()
    html = fetch_screener(ticker)
    if not html:
        return jsonify({"error": "no html"})
    company_id = get_company_id(html)
    if not company_id:
        return jsonify({"error": "no company id"})

    results = {}
    # Try every plausible endpoint pattern
    endpoints = [
        "/api/company/" + company_id + "/quick_ratios/",
        "/api/company/" + company_id + "/quick-ratios/",
        "/api/company/" + company_id + "/ratios/",
        "/api/company/" + company_id + "/",
        "/company/" + ticker + "/quick_ratios/",
        "/company/" + company_id + "/quick_ratios/",
    ]
    for ep in endpoints:
        try:
            r = requests.get("https://www.screener.in" + ep,
                             headers=API_HEADERS, timeout=10)
            results[ep] = {
                "status": r.status_code,
                "preview": r.text[:200]
            }
        except Exception as e:
            results[ep] = {"error": str(e)}

    return jsonify(results)


@app.route("/debug-find-values")
def debug_find_values():
    ticker = request.args.get("ticker", "RELIANCE").upper()
    html = fetch_screener(ticker)
    if not html:
        return jsonify({"error": "no html"})

    # Search for known values from the screenshot to find where data lives
    search_values = ["6.45", "18.7", "8.91", "20.5", "8.73", "9.26", "12.8", "6.53"]
    found = {}
    for val in search_values:
        idx = html.find(val)
        if idx != -1:
            found[val] = html[max(0, idx - 150):idx + 150]
        else:
            found[val] = "NOT FOUND"

    # Also check all script tags for JSON data
    scripts = re.findall(r'<script[^>]*>(.*?)</script>', html, re.S | re.I)
    json_scripts = []
    for s in scripts:
        if any(v in s for v in search_values):
            json_scripts.append(s[:500])

    return jsonify({
        "value_search": found,
        "scripts_with_data": json_scripts[:3]
    })


@app.route("/health")
def health():
    return jsonify({"status": "ok", "cookie_set": bool(COOKIE)})


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
