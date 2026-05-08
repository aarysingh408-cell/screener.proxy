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
    return None


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
        if key and value is not None:
            result[key] = value
            result['_label_' + key] = name
    return result


def parse_roe_from_insights(html):
    result = {}
    for years, key in [("3", "roe3yr"), ("5", "roe5yr"), ("10", "roe10yr")]:
        m = re.search(
            r'return on equity of ([\d.]+)%[^<]{0,50}?' + years + r'\s*years?',
            html, re.I)
        if m:
            result[key] = float(m.group(1))
    return result


def parse_ranges_tables(html):
    result = {}
    section_map = {
        'compounded sales growth':  {'10 years': 'salesgrowth10y', '5 years': 'salesgrowth5y',  '3 years': 'salesgrowth3y'},
        'compounded profit growth': {'10 years': 'profitgrowth10y', '5 years': 'profitgrowth5y', '3 years': 'profitgrowth3y'},
        'stock price cagr':         {'10 years': 'cagr10y',          '5 years': 'cagr5y',         '3 years': 'cagr3y'},
        'return on equity':         {'10 years': 'roe10yr',          '5 years': 'roe5yr',         '3 years': 'roe3yr'},
    }
    tables = re.findall(
        r'<table[^>]*class="[^"]*ranges-table[^"]*"[^>]*>(.*?)</table>',
        html, re.S | re.I)
    for table in tables:
        th = re.search(r'<th[^>]*>(.*?)</th>', table, re.S | re.I)
        if not th:
            continue
        heading = re.sub(r'<[^>]+>', '', th.group(1)).strip().lower()
        section = None
        for key in section_map:
            if key in heading:
                section = section_map[key]
                break
        if not section:
            continue
        rows = re.findall(r'<tr[^>]*>(.*?)</tr>', table, re.S | re.I)
        for row in rows:
            cells = re.findall(r'<td[^>]*>(.*?)</td>', row, re.S | re.I)
            if len(cells) < 2:
                continue
            label = re.sub(r'<[^>]+>', '', cells[0]).strip().lower()
            val   = re.sub(r'<[^>]+>', '', cells[1]).replace('%', '').replace(',', '').strip()
            for period, metric_key in section.items():
                if period in label:
                    try:
                        result[metric_key] = float(val)
                    except Exception:
                        pass
    return result


def parse_annual_ttm(html, row_label):
    anchor = "Mar 2015"
    anchor_idx = html.find(anchor)
    if anchor_idx == -1:
        return None
    section = html[max(0, anchor_idx - 3000):anchor_idx + 80000]
    escaped = re.escape(row_label)
    for m in re.finditer(r'<tr[^>]*>((?:(?!</tr>).){0,5000}?)</tr>', section, re.S | re.I):
        row = m.group(1)
        if row_label not in row:
            continue
        cells = re.findall(r'<td[^>]*>(.*?)</td>', row, re.S | re.I)
        values = []
        for cell in cells:
            text = re.sub(r'<[^>]+>', '', cell).replace(',', '').strip()
            try:
                v = float(text)
                if v > 0:
                    values.append(v)
            except Exception:
                pass
        if values:
            return values[-1]
    return None


def parse_roce_by_year(html, year):
    """
    Extract ROCE% for a specific year from the Ratios table.
    Uses id='ratios' section. Finds header row, maps Mar YEAR column,
    extracts value from ROCE % row at that column position.
    """
    ratios_idx = html.find('id="ratios"')
    if ratios_idx == -1:
        return None
    ratios_html = html[ratios_idx:ratios_idx + 80000]

    # Find the header row — look for a <tr> containing multiple <th> with year patterns
    header_row = None
    for m in re.finditer(r'<tr[^>]*>(.*?)</tr>', ratios_html, re.S | re.I):
        row = m.group(1)
        ths = re.findall(r'<th[^>]*>(.*?)</th>', row, re.S | re.I)
        if len(ths) >= 3:
            header_texts = [re.sub(r'<[^>]+>', '', h).strip() for h in ths]
            # Check if this row has year-like headers (Mar 20XX)
            if any('Mar' in h and '20' in h for h in header_texts):
                header_row = header_texts
                break

    if not header_row:
        return None

    target = "Mar " + str(year)
    if target not in header_row:
        return None
    # col_idx is 0-based in the header list
    # First th might be empty (label column) or might start with Mar dates directly
    col_idx = header_row.index(target)

    # Find ROCE % row — look for tr containing "ROCE" and "%"
    for m in re.finditer(r'<tr[^>]*>(.*?)</tr>', ratios_html, re.S | re.I):
        row = m.group(1)
        if 'ROCE' not in row:
            continue
        # Get the text content of all td elements
        cells = re.findall(r'<td[^>]*>(.*?)</td>', row, re.S | re.I)
        cell_texts = [re.sub(r'<[^>]+>', '', c).strip() for c in cells]
        # First cell should be the label
        if not cell_texts:
            continue
        label = cell_texts[0]
        if 'ROCE' not in label:
            continue
        # Data cells start at index 1 (cell_texts[0] is label)
        # col_idx in header corresponds to col_idx-1 in data cells
        # because header[0] is empty label column
        data_idx = col_idx - 1 if col_idx > 0 else col_idx
        if data_idx < len(cell_texts) - 1:
            raw = cell_texts[data_idx + 1].replace('%', '').strip()
            try:
                return float(raw)
            except Exception:
                pass
        # Try direct position match
        if col_idx < len(cell_texts):
            raw = cell_texts[col_idx].replace('%', '').strip()
            try:
                v = float(raw)
                if 0 < v < 100:
                    return v
            except Exception:
                pass

    return None


def parse_shareholding(html):
    """
    Extract FII and DII from Shareholding Pattern table (most recent quarter).
    Tries multiple label patterns for robustness.
    """
    result = {}

    # Find the shareholding section first
    sh_idx = html.find('Shareholding Pattern')
    if sh_idx == -1:
        sh_idx = html.find('shareholding')
    if sh_idx == -1:
        return result

    # Work within the shareholding section
    sh_html = html[sh_idx:sh_idx + 30000]

    for search_label, key in [("FIIs", "fiiholding"), ("DIIs", "diiholding"),
                                ("FII", "fiiholding"),  ("DII", "diiholding")]:
        if key in result:
            continue

        label_idx = sh_html.find(search_label)
        if label_idx == -1:
            continue

        # Find containing <tr>
        tr_start = sh_html.rfind('<tr', 0, label_idx)
        tr_end   = sh_html.find('</tr>', label_idx)
        if tr_start == -1 or tr_end == -1:
            continue

        row = sh_html[tr_start:tr_end]
        cells = re.findall(r'<td[^>]*>(.*?)</td>', row, re.S | re.I)

        # Get last cell (most recent quarter)
        for cell in reversed(cells):
            raw = re.sub(r'<[^>]+>', '', cell).replace('%', '').strip()
            try:
                v = float(raw)
                if 0 < v < 100:
                    result[key] = v
                    break
            except Exception:
                pass

    return result


def get_all_ratios(ticker):
    html = fetch_screener(ticker)
    if not html:
        return {}
    result = parse_li_items(extract_ul_section(html, "top-ratios"))
    result.update(parse_ranges_tables(html))
    result.update(parse_roe_from_insights(html))
    result.update(parse_shareholding(html))
    return result


METRIC_MAP = {
    "MARKETCAP":       "marketcap",
    "BOOKVALUE":       "bookvalue",
    "PE":              "stockpe",
    "ROE":             "roe",
    "ROCE":            "roce",
    "DIVIDENDYIELD":   "dividendyield",
    "PRICE":           "currentprice",
    "SALESGROWTH3Y":   "salesgrowth3y",
    "SALESGROWTH5Y":   "salesgrowth5y",
    "SALESGROWTH10Y":  "salesgrowth10y",
    "PROFITGROWTH3Y":  "profitgrowth3y",
    "PROFITGROWTH5Y":  "profitgrowth5y",
    "PROFITGROWTH10Y": "profitgrowth10y",
    "ROE3Y":           "roe3yr",
    "ROE5Y":           "roe5yr",
    "ROE10Y":          "roe10yr",
    "CAGR3Y":          "cagr3y",
    "CAGR5Y":          "cagr5y",
    "CAGR10Y":         "cagr10y",
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
        return jsonify({"error": "Could not fetch Screener"}), 503

    if metric == "SALESTTM":
        v = parse_annual_ttm(html, "Sales")
        return jsonify({"value": v if v is not None else "N/A"})
    if metric == "PATTTM":
        v = parse_annual_ttm(html, "Net Profit")
        return jsonify({"value": v if v is not None else "N/A"})
    if metric in ("ROCE2023", "ROCE2024", "ROCE2025"):
        v = parse_roce_by_year(html, metric.replace("ROCE", ""))
        return jsonify({"value": v if v is not None else "N/A"})

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
    ratios       = get_all_ratios(ticker)
    values       = {k: v for k, v in ratios.items() if not k.startswith('_label_')}
    shareholding = parse_shareholding(html)
    roce2023     = parse_roce_by_year(html, "2023")
    roce2024     = parse_roce_by_year(html, "2024")
    roce2025     = parse_roce_by_year(html, "2025")
    sales_ttm    = parse_annual_ttm(html, "Sales")
    pat_ttm      = parse_annual_ttm(html, "Net Profit")
    return jsonify({
        "values":       values,
        "shareholding": shareholding,
        "roce_by_year": {"2023": roce2023, "2024": roce2024, "2025": roce2025},
        "ttm":          {"sales": sales_ttm, "pat": pat_ttm},
    })


@app.route("/debug-roce-headers")
def debug_roce_headers():
    """Show the header row found in the ratios section"""
    ticker = request.args.get("ticker", "RELIANCE").upper()
    html   = fetch_screener(ticker)
    if not html:
        return jsonify({"error": "no html"})
    ratios_idx = html.find('id="ratios"')
    if ratios_idx == -1:
        return jsonify({"error": "ratios section not found"})
    ratios_html = html[ratios_idx:ratios_idx + 80000]
    headers_found = []
    for m in re.finditer(r'<tr[^>]*>(.*?)</tr>', ratios_html, re.S | re.I):
        row = m.group(1)
        ths = re.findall(r'<th[^>]*>(.*?)</th>', row, re.S | re.I)
        if len(ths) >= 3:
            texts = [re.sub(r'<[^>]+>', '', h).strip() for h in ths]
            if any('Mar' in t and '20' in t for t in texts):
                headers_found = texts
                break
    return jsonify({"header_row": headers_found, "total_headers": len(headers_found)})


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
