import os
import re
import json
from flask import Flask, request, jsonify
import requests
from cachetools import TTLCache

app = Flask(__name__)
cache = TTLCache(maxsize=100, ttl=1800)

COOKIE      = os.environ.get("SCREENER_COOKIE", "")
GROQ_KEY    = os.environ.get("GROQ_API_KEY", "")

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


# ── FETCH ─────────────────────────────────────────────────────
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


# ── EXTRACT READABLE TEXT FROM HTML ──────────────────────────
def extract_ratio_text(html, qr_html):
    lines = []
    for fragment in [html, qr_html or ""]:
        items = re.findall(r'<li[^>]*>(.*?)</li>', fragment, re.S | re.I)
        for item in items:
            nm = re.search(r'class="name"[^>]*>(.*?)</span>', item, re.S | re.I)
            vl = re.search(r'class="number"[^>]*>(.*?)</span>', item, re.S | re.I)
            if nm and vl:
                name = re.sub(r'<[^>]+>', '', nm.group(1)).strip()
                val  = re.sub(r'<[^>]+>', '', vl.group(1)).strip()
                if name and val:
                    lines.append(name + ": " + val)
    return "\n".join(lines)


# ── GROQ EXTRACTION ───────────────────────────────────────────
def extract_via_groq(ratio_text):
    if not GROQ_KEY:
        return None

    prompt = """You are a financial data extractor. Below is raw metric data from Screener.in for an Indian stock.
Extract EXACTLY these metrics and return ONLY a JSON object with numeric values (no units, no % sign).
If a metric is not found, use null.

Metrics to extract:
- MarketCap (in Crores)
- Price (current price)
- PE (Stock P/E)
- BookValue
- ROE (current)
- ROCE (current)
- DividendYield
- SalesGrowth3Y
- SalesGrowth5Y
- SalesGrowth10Y
- ProfitGrowth3Y
- ProfitGrowth5Y
- ProfitGrowth10Y
- ROE3Y
- ROE5Y
- ROE10Y
- CAGR3Y (Return over 3years)
- CAGR5Y (Return over 5years)
- CAGR10Y (Return over 10years)
- FII
- DII

Raw data:
""" + ratio_text + """

Return only valid JSON, no explanation, no markdown.
Example: {"MarketCap": 194500, "Price": 1436, "PE": 24.0, ...}"""

    try:
        r = requests.post(
            "https://api.groq.com/openai/v1/chat/completions",
            headers={
                "Authorization": "Bearer " + GROQ_KEY,
                "Content-Type": "application/json"
            },
            json={
                "model": "llama-3.1-8b-instant",
                "messages": [{"role": "user", "content": prompt}],
                "temperature": 0,
                "max_tokens": 500
            },
            timeout=15
        )
        content = r.json()["choices"][0]["message"]["content"].strip()
        content = re.sub(r'^```[a-z]*\n?', '', content)
        content = re.sub(r'\n?```$', '', content)
        return json.loads(content)
    except Exception as e:
        return None


# ── FALLBACK DIRECT PARSER ────────────────────────────────────
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


DIRECT_MAP = {
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


# ── MAIN ROUTE ────────────────────────────────────────────────
@app.route("/stock")
def stock():
    ticker = request.args.get("ticker", "").upper().strip()
    metric = request.args.get("metric", "").upper().replace(" ", "").replace("_", "")
    if not ticker or not metric:
        return jsonify({"error": "ticker and metric required"}), 400

    html = fetch_screener(ticker)
    if not html:
        return jsonify({"error": "Could not fetch Screener"}), 503

    company_id = get_company_id(html)
    qr_html    = fetch_quick_ratios(company_id) if company_id else None

    # TTM and historical ROCE bypass Groq
    if metric == "SALESTTM":
        return jsonify({"value": parse_ttm(html, "Sales") or "N/A"})
    if metric == "PATTTM":
        return jsonify({"value": parse_ttm(html, "Net Profit") or "N/A"})
    if metric in ("ROCE2023", "ROCE2024", "ROCE2025"):
        v = parse_roce_by_year(html, metric.replace("ROCE", ""))
        return jsonify({"value": v or "N/A"})

    # PB is calculated
    if metric == "PB":
        direct = parse_li_items(extract_ul_section(html, "top-ratios"))
        price = direct.get("currentprice")
        bv    = direct.get("bookvalue")
        if price and bv and float(bv) != 0:
            return jsonify({"value": round(float(price) / float(bv), 2)})
        return jsonify({"value": "N/A"})

    # Try Groq first if key is set
    if GROQ_KEY:
        ratio_text = extract_ratio_text(html, qr_html)
        groq_data  = extract_via_groq(ratio_text)
        if groq_data and metric in groq_data and groq_data[metric] is not None:
            return jsonify({"value": groq_data[metric], "source": "groq"})

    # Fallback to direct parser
    ratios = parse_li_items(extract_ul_section(html, "top-ratios"))
    if qr_html:
        ratios.update(parse_li_items(qr_html))

    lookup = DIRECT_MAP.get(metric)
    if not lookup:
        return jsonify({"error": "Unknown metric: " + metric}), 400

    return jsonify({"value": ratios.get(lookup, "N/A")})


@app.route("/clear-cache")
def clear_cache():
    cache.clear()
    return jsonify({"status": "cache cleared"})


@app.route("/debug-labels")
def debug_labels():
    ticker  = request.args.get("ticker", "RELIANCE").upper()
    html    = fetch_screener(ticker)
    if not html:
        return jsonify({"error": "no html"})
    company_id = get_company_id(html)
    qr_html    = fetch_quick_ratios(company_id) if company_id else None
    ratio_text = extract_ratio_text(html, qr_html)

    # Show what Groq would receive
    direct = parse_li_items(extract_ul_section(html, "top-ratios"))
    if qr_html:
        direct.update(parse_li_items(qr_html))
    values = {k: v for k, v in direct.items() if not k.startswith('_label_')}

    return jsonify({
        "ratio_text_for_groq": ratio_text,
        "direct_parsed_values": values,
        "groq_key_set": bool(GROQ_KEY)
    })


@app.route("/health")
def health():
    return jsonify({
        "status": "ok",
        "cookie_set": bool(COOKIE),
        "groq_key_set": bool(GROQ_KEY)
    })


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
