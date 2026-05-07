import os, re, json
from flask import Flask, request, jsonify
import requests
from cachetools import TTLCache

app = Flask(__name__)
cache = TTLCache(maxsize=100, ttl=1800)  # cache 30 min

COOKIE   = os.environ.get("SCREENER_COOKIE", "")
HEADERS  = {
    "User-Agent"     : "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Accept"         : "text/html,application/xhtml+xml",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer"        : "https://www.screener.in/",
    "Cookie"         : f"sessionid={COOKIE}"
}

def fetch_screener(ticker):
    if ticker in cache:
        return cache[ticker]
    for mode in ["consolidated", ""]:
        path = f"/{mode}/" if mode else "/"
        url  = f"https://www.screener.in/company/{ticker.upper()}{path}"
        r    = requests.get(url, headers=HEADERS, timeout=15)
        if "top-ratios" in r.text:
            cache[ticker] = r.text
            return r.text
    return None

def parse_top_ratios(html):
    result = {}
    sec = re.search(r']*id="top-ratios"[^>]*>(.*?)', html, re.S|re.I)
    if not sec:
        return result
    for li in re.findall(r']*>(.*?)', sec.group(1), re.S|re.I):
        nm = re.search(r'class="[^"]*name[^"]*"[^>]*>(.*?)', li, re.S|re.I)
        vl = re.search(r'class="[^"]*number[^"]*"[^>]*>(.*?)', li, re.S|re.I) or \
             re.search(r'class="[^"]*value[^"]*"[^>]*>(.*?)',  li, re.S|re.I)
        if not nm or not vl:
            continue
        name = re.sub(r'<[^>]+>', '', nm.group(1)).strip()
        raw  = re.sub(r'<[^>]+>', '', vl.group(1)).strip()
        raw  = re.sub(r'[₹%,\s]', '', raw)
        nums = re.findall(r'[\d.]+', raw)
        key  = re.sub(r'[\s/\-]', '', name).lower()
        result[key] = float(nums[0]) if nums else raw
        result['_label_' + key] = name  # store original label for debug
    return result

def parse_ttm(html, row_label):
    pattern = re.compile(
        r']*>.*?' + re.escape(row_label) + r'.*?', re.S|re.I)
    m = pattern.search(html)
    if not m:
        return None
    cells = re.findall(r']*>(.*?)', m.group(0), re.S|re.I)
    if not cells:
        return None
    last = re.sub(r'<[^>]+>', '', cells[-1]).replace(',', '').strip()
    try:
        return float(last)
    except:
        return None

def parse_roce_by_year(html, year):
    # Key Metrics table — find ROCE row, find column for year
    yr_pattern = re.compile(
        r'ROCE.*?' + str(year) + r'.*?(\d+\.?\d*)', re.S|re.I)
    m = yr_pattern.search(html)
    if m:
        return float(m.group(1))
    return None

METRIC_MAP = {
    "MARKETCAP"      : "marketcap",
    "BOOKVALUE"      : "bookvalue",
    "PE"             : "stockpe",
    "ROE"            : "roe",
    "ROCE"           : "roce",
    "DIVIDENDYIELD"  : "dividendyield",
    "PRICE"          : "currentprice",
    "SALESGROWTH3Y"  : "salesgrowth3years",
    "SALESGROWTH5Y"  : "salesgrowth5years",
    "SALESGROWTH10Y" : "salesvar10yrs",
    "PROFITGROWTH3Y" : "profitvar3yrs",
    "PROFITGROWTH5Y" : "profitvar5yrs",
    "PROFITGROWTH10Y": "profitvar10yrs",
    "ROE3Y"          : "roe3yr",
    "ROE5Y"          : "roe5yr",
    "ROE10Y"         : "roe10yr",
    "CAGR3Y"         : "3years",
    "CAGR5Y"         : "5years",
    "CAGR10Y"        : "10years",
    "FII"            : "fiiholding",
    "DII"            : "diiholding",
}

@app.route("/stock")
def stock():
    ticker = request.args.get("ticker", "").upper().strip()
    metric = request.args.get("metric", "").upper().replace(" ","").replace("_","")

    if not ticker or not metric:
        return jsonify({"error": "ticker and metric required"}), 400

    html = fetch_screener(ticker)
    if not html:
        return jsonify({"error": "Could not fetch Screener page"}), 503

    # PB = Price / Book Value
    if metric == "PB":
        ratios = parse_top_ratios(html)
        price  = ratios.get("currentprice")
        bv     = ratios.get("bookvalue")
        if price and bv and bv != 0:
            return jsonify({"value": round(price / bv, 2)})
        return jsonify({"value": "N/A"})

    # TTM Sales
    if metric == "SALESTTM":
        v = parse_ttm(html, "Sales")
        return jsonify({"value": v if v is not None else "N/A"})

    # TTM PAT
    if metric == "PATTTM":
        v = parse_ttm(html, "Net Profit")
        return jsonify({"value": v if v is not None else "N/A"})

    # Historical ROCE
    if metric in ("ROCE2023","ROCE2024","ROCE2025"):
        year = metric.replace("ROCE","")
        v    = parse_roce_by_year(html, year)
        return jsonify({"value": v if v is not None else "N/A"})

    # Debug — dump all parsed keys
    if metric == "DEBUG":
        ratios = parse_top_ratios(html)
        labels = {k: v for k, v in ratios.items() if k.startswith('_label_')}
        return jsonify(labels)

    # All other metrics from top-ratios
    lookup = METRIC_MAP.get(metric)
    if not lookup:
        return jsonify({"error": f"Unknown metric: {metric}"}), 400

    ratios = parse_top_ratios(html)
    value  = ratios.get(lookup, "N/A")
    return jsonify({"value": value})

@app.route("/health")
def health():
    return jsonify({"status": "ok"})

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
