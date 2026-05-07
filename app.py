import os
import re
import json
from flask import Flask, request, jsonify
import requests
from cachetools import TTLCache

app = Flask(__name__)
cache = TTLCache(maxsize=100, ttl=1800)

COOKIE   = os.environ.get("SCREENER_COOKIE", "")
GROQ_KEY = os.environ.get("GROQ_API_KEY", "")

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
    "X-Requested-With": "XMLHttpRequest",
    "Referer": "https://www.screener.in/",
    "Cookie": "sessionid=" + COOKIE
}


def fetch_screener(ticker):
    if ticker in cache:
        return cache[ticker]
    for mode in ["consolidated", ""]:
        url = "https://www.screener.in/company/" + ticker.upper() + ("/" + mode + "/" if mode else "/")
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
    return m.group(1) if m else None


def fetch_quick_ratios(company_id):
    key = "qr_" + str(company_id)
    if key in cache:
        return cache[key]
    try:
        r = requests.get(
            "https://www.screener.in/api/company/" + str(company_id) + "/quick_ratios/",
            headers=API_HEADERS, timeout=15)
        if r.status_code == 200:
            cache[key] = r.text
            return r.text
    except Exception:
        pass
    return None


def extract_ul_section(html, section_id):
    start = html.find('id="' + section_id + '"')
    if start == -1:
        return ""
    open_end = html.find('>', start)
    if open_end == -1:
        return ""
    pos = open_end + 1
    depth = 1
    while pos < len(html) and depth > 0:
        next_open  = html.find('<ul', pos)
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


def clean_number(raw):
    raw = re.sub(r'<[^>]+>', '', raw).strip()
    raw = re.sub(r'[₹\s,]', '', raw)
    m = re.search(r'-?[\d.]+', raw)
    if m:
        try:
            return float(m.group(0))
        except Exception:
            pass
    return raw


def parse_li_items(html_fragment):
    result = {}
    if not html_fragment:
        return result
    items = re.findall(r'<li[^>]*>(.*?)</li>', html_fragment, re.S | re.I)
    for item in items:
        nm = re.search(r'class="name"[^>]*>(.*?)</span>', item, re.S | re.I)
        vl = re.search(r'class="number"[^>]*>(.*?)</span>', item, re.S | re.I)
        if not nm or not vl:
            continue
        name = re.sub(r'<[^>]+>', '', nm.group(1)).strip()
        if not name:
            continue
        value = clean_number(vl.group(1))
        key = re.sub(r'[\s/\-]', '', name).lower()
        if key:
            result[key] = value
            result['_label_' + key] = name
    return result


def parse_insights(html):
    """
    Extract metrics from Screener's key insights sentences.
    ROE 3Yr confirmed working. Searching for 5Yr/10Yr and FII/DII patterns.
    """
    result = {}

    # ROE averages
    for years, key in [("3", "roe3yr"), ("5", "roe5yr"), ("10", "roe10yr")]:
        m = re.search(
            r'return on equity of ([\d.]+)%\s*over\s*(?:the\s*)?last\s*' + years + r'\s*years?',
            html, re.I)
        if m:
            result[key] = float(m.group(1))

    # FII — Screener sometimes embeds FII in meta/insights as "FII holding of X%"
    fii_patterns = [
        r'FII\s*holding[^<]{0,30}?([\d.]+)\s*%',
        r'Foreign\s*Institutional[^<]{0,50}?([\d.]+)\s*%',
        r'FII[^<]{0,20}?:\s*([\d.]+)',
    ]
    for p in fii_patterns:
        m = re.search(p, html, re.I)
        if m:
            result['fiiholding'] = float(m.group(1))
            break

    # DII
    dii_patterns = [
        r'DII\s*holding[^<]{0,30}?([\d.]+)\s*%',
        r'Domestic\s*Institutional[^<]{0,50}?([\d.]+)\s*%',
        r'DII[^<]{0,20}?:\s*([\d.]+)',
    ]
    for p in dii_patterns:
        m = re.search(p, html, re.I)
        if m:
            result['diiholding'] = float(m.group(1))
            break

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
    for p in [
        re.compile(r'ROCE[^<]{0,200}?' + str(year) + r'[^<]{0,100}?([\d.]+)', re.S | re.I),
        re.compile(str(year) + r'[^<]{0,300}?ROCE[^<]{0,100}?([\d.]+)', re.S | re.I)
    ]:
        m = p.search(html)
        if m:
            return float(m.group(1))
    return None


def get_all_ratios(ticker):
    html = fetch_screener(ticker)
    if not html:
        return {}

    # 1. Default top-ratios (9 confirmed correct)
    result = parse_li_items(extract_ul_section(html, "top-ratios"))

    # 2. Quick ratios API (sales/profit growth, returns)
    company_id = get_company_id(html)
    if company_id:
        qr_html = fetch_quick_ratios(company_id)
        if qr_html:
            result.update(parse_li_items(qr_html))

    # 3. Override with insights-parsed values (more accurate for ROE + FII/DII)
    result.update(parse_insights(html))

    return result


METRIC_MAP = {
    "MARKETCAP": "marketcap", "BOOKVALUE": "bookvalue",
    "PE": "stockpe", "ROE": "roe", "ROCE": "roce",
    "DIVIDENDYIELD": "dividendyield", "PRICE": "currentprice",
    "SALESGROWTH3Y": "salesgrowth3years", "SALESGROWTH5Y": "salesgrowth5years",
    "SALESGROWTH10Y": "salesvar10yrs",
    "PROFITGROWTH3Y": "profitvar3yrs", "PROFITGROWTH5Y": "profitvar5yrs",
    "PROFITGROWTH10Y": "profitvar10yrs",
    "ROE3Y": "roe3yr", "ROE5Y": "roe5yr", "ROE10Y": "roe10yr",
    "CAGR3Y": "returnover3years", "CAGR5Y": "returnover5years",
    "CAGR10Y": "returnover10years",
    "FII": "fiiholding", "DII": "diiholding",
}


@app.route("/stock")
def stock():
    ticker = request.args.get("ticker", "").upper().strip()
    metric = request.args.get("metric", "").upper().replace(" ", "").replace("_", "")
    if not ticker or not metric:
        return jsonify({"error": "ticker and metric required"}), 400

    html = fetch_screener(ticker)
    if not html:
        return jsonify({"error": "Could not fetch Screener"}), 503

    if metric == "SALESTTM":
        return jsonify({"value": parse_ttm(html, "Sales") or "N/A"})
    if metric == "PATTTM":
        return jsonify({"value": parse_ttm(html, "Net Profit") or "N/A"})
    if metric in ("ROCE2023", "ROCE2024", "ROCE2025"):
        v = parse_roce_by_year(html, metric.replace("ROCE", ""))
        return jsonify({"value": v or "N/A"})

    ratios = get_all_ratios(ticker)

    if metric == "PB":
        price = ratios.get("currentprice")
        bv    = ratios.get("bookvalue")
        if price and bv and float(bv) != 0:
            return jsonify({"value": round(float(price) / float(bv), 2)})
        return jsonify({"value": "N/A"})

    lookup = METRIC_MAP.get(metric)
    if not lookup:
        return jsonify({"error": "Unknown metric: " + metric}), 400
    return jsonify({"value": ratios.get(lookup, "N/A")})


@app.route("/debug-labels")
def debug_labels():
    ticker = request.args.get("ticker", "RELIANCE").upper()
    html   = fetch_screener(ticker)
    if not html:
        return jsonify({"error": "no html"})
    ratios  = get_all_ratios(ticker)
    values  = {k: v for k, v in ratios.items() if not k.startswith('_label_')}
    insights = parse_insights(html)
    return jsonify({
        "values": values,
        "insights_parsed": insights
    })


@app.route("/debug-insights-raw")
def debug_insights_raw():
    """Shows all insight sentences from the page so we can find exact patterns"""
    ticker = request.args.get("ticker", "RELIANCE").upper()
    html   = fetch_screener(ticker)
    if not html:
        return jsonify({"error": "no html"})

    # Extract all insight list items
    insights_section = extract_ul_section(html, "key-insights")
    if not insights_section:
        # Try finding any insights/pros/cons section
        idx = html.find("key-insights")
        if idx == -1:
            idx = html.find("pros")
        context = html[max(0, idx-100):idx+3000] if idx != -1 else ""
        return jsonify({
            "key_insights_section": "not found with id",
            "context": context[:2000]
        })

    items = re.findall(r'<li[^>]*>(.*?)</li>', insights_section, re.S | re.I)
    texts = [re.sub(r'<[^>]+>', '', item).strip() for item in items]
    return jsonify({"insight_sentences": texts})


@app.route("/clear-cache")
def clear_cache():
    cache.clear()
    return jsonify({"status": "cache cleared"})


@app.route("/health")
def health():
    return jsonify({"status": "ok", "cookie_set": bool(COOKIE)})


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
