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


def fetch_shareholding(company_id):
    key = "sh_" + str(company_id)
    if key in cache:
        return cache[key]
    # Try multiple Screener shareholding endpoints
    endpoints = [
        "/api/company/" + company_id + "/shareholding/",
        "/api/company/" + company_id + "/shareholding/?period=quarterly",
        "/api/company/" + company_id + "/shareholding/?period=annual",
    ]
    for ep in endpoints:
        try:
            r = requests.get("https://www.screener.in" + ep,
                             headers=API_HEADERS, timeout=15)
            if r.status_code == 200 and len(r.text) > 100:
                cache[key] = r.text
                return r.text, ep
        except Exception:
            pass
    return None, None


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


def parse_insights_roe(html):
    """
    Extract ROE averages from Screener's key insights text.
    Example: 'return on equity of 8.91% over last 3 years'
    Confirmed present in page HTML from earlier debug.
    """
    result = {}
    patterns = [
        (r'return on equity of ([\d.]+)%\s*over\s*(?:the\s*)?last\s*3\s*years?', 'roe3yr'),
        (r'return on equity of ([\d.]+)%\s*over\s*(?:the\s*)?last\s*5\s*years?', 'roe5yr'),
        (r'return on equity of ([\d.]+)%\s*over\s*(?:the\s*)?last\s*10\s*years?', 'roe10yr'),
    ]
    for pattern, key in patterns:
        m = re.search(pattern, html, re.I)
        if m:
            result[key] = float(m.group(1))
    return result


def parse_fii_dii_from_html(html):
    """
    Try to extract FII/DII from shareholding section in HTML.
    Screener embeds some shareholding data in the page for charts.
    """
    result = {}

    # Try JSON data embedded in script tags
    scripts = re.findall(r'<script[^>]*>(.*?)</script>', html, re.S | re.I)
    for script in scripts:
        # Look for FII percentage in JSON
        fii_m = re.search(r'["\'](?:FII|Foreign Institutional)["\'].*?:\s*["\']?([\d.]+)', script, re.I)
        dii_m = re.search(r'["\'](?:DII|Domestic Institutional)["\'].*?:\s*["\']?([\d.]+)', script, re.I)
        if fii_m:
            result['fiiholding'] = float(fii_m.group(1))
        if dii_m:
            result['diiholding'] = float(dii_m.group(1))
        if result:
            break

    # Try shareholding table in HTML
    if 'fiiholding' not in result:
        fii_m = re.search(r'FII[^<]{0,50}?</td>\s*<td[^>]*>\s*([\d.]+)', html, re.I)
        if fii_m:
            result['fiiholding'] = float(fii_m.group(1))

    if 'diiholding' not in result:
        dii_m = re.search(r'DII[^<]{0,50}?</td>\s*<td[^>]*>\s*([\d.]+)', html, re.I)
        if dii_m:
            result['diiholding'] = float(dii_m.group(1))

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

    # Start with top-ratios (9 confirmed correct metrics)
    result = parse_li_items(extract_ul_section(html, "top-ratios"))

    # Add quick_ratios from API (sales growth, profit growth, returns)
    company_id = get_company_id(html)
    if company_id:
        qr_html = fetch_quick_ratios(company_id)
        if qr_html:
            result.update(parse_li_items(qr_html))

    # Override ROE 3/5/10Yr with values from insights text
    # (confirmed more accurate than quick_ratios API)
    insights_roe = parse_insights_roe(html)
    result.update(insights_roe)

    # Try FII/DII from HTML
    fii_dii = parse_fii_dii_from_html(html)
    result.update(fii_dii)

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

    ratios = get_all_ratios(ticker)
    values = {k: v for k, v in ratios.items() if not k.startswith('_label_')}

    # Also test shareholding API endpoints
    company_id = get_company_id(html)
    sh_data, sh_ep = fetch_shareholding(company_id) if company_id else (None, None)

    return jsonify({
        "values": values,
        "roe_from_insights": parse_insights_roe(html),
        "fii_dii_from_html": parse_fii_dii_from_html(html),
        "shareholding_endpoint": sh_ep,
        "shareholding_preview": sh_data[:500] if sh_data else None
    })


@app.route("/debug-shareholding")
def debug_shareholding():
    ticker     = request.args.get("ticker", "RELIANCE").upper()
    html       = fetch_screener(ticker)
    if not html:
        return jsonify({"error": "no html"})
    company_id = get_company_id(html)
    if not company_id:
        return jsonify({"error": "no company id"})

    results = {}
    endpoints = [
        "/api/company/" + company_id + "/shareholding/",
        "/api/company/" + company_id + "/shareholding/?period=quarterly",
        "/api/company/" + company_id + "/shareholding/?period=annual",
        "/company/" + ticker + "/shareholding/",
    ]
    for ep in endpoints:
        try:
            r = requests.get("https://www.screener.in" + ep,
                             headers=API_HEADERS, timeout=10)
            results[ep] = {"status": r.status_code, "preview": r.text[:300]}
        except Exception as e:
            results[ep] = {"error": str(e)}
    return jsonify(results)


@app.route("/clear-cache")
def clear_cache():
    cache.clear()
    return jsonify({"status": "cache cleared"})


@app.route("/health")
def health():
    return jsonify({"status": "ok", "cookie_set": bool(COOKIE), "groq_key_set": bool(GROQ_KEY)})


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
