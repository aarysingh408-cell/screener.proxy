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


def fetch_quick_ratios(company_id):
    cache_key = "qr_" + str(company_id)
    if cache_key in cache:
        return cache[cache_key]
    url = "https://www.screener.in/api/company/" + str(company_id) + "/quick_ratios/"
    try:
        r = requests.get(url, headers=API_HEADERS, timeout=15)
        if r.status_code == 200:
            cache[cache_key] = r.text
            return r.text
    except Exception:
        pass
    return None


def parse_li_items(html_fragment):
    result = {}
    if not html_fragment:
        return result
    items = re.findall(r'<li[^>]*>(.*?)</li>', html_fragment, re.S | re.I)
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


def get_all_ratios(ticker):
    html = fetch_screener(ticker)
    if not html:
        return {}

    # Parse default top-ratios from main page
    result = parse_li_items(extract_ul_section(html, "top-ratios"))

    # Fetch and parse quick ratios from dedicated API endpoint
    company_id = get_company_id(html)
    if company_id:
        qr_html = fetch_quick_ratios(company_id)
        if qr_html:
            result.update(parse_li_items(qr_html))

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

    ratios = get_all_ratios(ticker)

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
    ratios = get_all_ratios(ticker)
    if not ratios:
        return jsonify({"error": "no data"})
    labels = {k.replace('_label_', ''): v for k, v in ratios.items() if k.startswith('_label_')}
    values = {k: v for k, v in ratios.items() if not k.startswith('_label_')}
    return jsonify({"total_found": len(labels), "labels": labels, "values": values})


@app.route("/health")
def health():
    return jsonify({"status": "ok", "cookie_set": bool(COOKIE)})


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
