import os
import re
import json
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


def parse_table_row(html, row_label):
    """
    Find a table row by its label and return all cell values as a list.
    Works for P&L, Ratios, Shareholding tables.
    """
    escaped = re.escape(row_label)
    pattern = re.compile(
        r'<tr[^>]*>.*?' + escaped + r'.*?</tr>', re.S | re.I)
    m = pattern.search(html)
    if not m:
        return []
    row = m.group(0)
    cells = re.findall(r'<t[dh][^>]*>(.*?)</t[dh]>', row, re.S | re.I)
    values = []
    for cell in cells:
        text = re.sub(r'<[^>]+>', '', cell).replace(',', '').replace('%', '').strip()
        if text:
            values.append(text)
    return values


def parse_table_headers(html, near_label):
    """
    Find the header row of a table that contains near_label nearby.
    Returns list of header text values.
    """
    idx = html.find(near_label)
    if idx == -1:
        return []
    # Look for thead or header tr before this point
    chunk = html[max(0, idx-5000):idx+500]
    # Find last <tr> with <th> elements before the label
    header_matches = list(re.finditer(r'<tr[^>]*>(.*?)</tr>', chunk, re.S | re.I))
    for m in reversed(header_matches):
        row = m.group(1)
        if '<th' in row.lower():
            headers = re.findall(r'<th[^>]*>(.*?)</th>', row, re.S | re.I)
            return [re.sub(r'<[^>]+>', '', h).strip() for h in headers]
    return []


def parse_ttm_from_pl(html, row_label):
    """Get the rightmost (most recent/TTM) value from a P&L table row."""
    values = parse_table_row(html, row_label)
    # Skip the first cell (row label) and get the last numeric value
    for v in reversed(values[1:]):
        try:
            return float(v.replace(',', ''))
        except Exception:
            pass
    return None


def parse_roce_by_year(html, year):
    """
    Parse ROCE% from the Ratios table for a specific year (e.g. 2023, 2024, 2025).
    Matches Mar YEAR column.
    """
    # Find ROCE % row
    roce_values = parse_table_row(html, "ROCE %")
    if not roce_values:
        roce_values = parse_table_row(html, "ROCE")

    if not roce_values:
        return None

    # Find header row near "ROCE" to match column positions
    idx = html.find("ROCE %")
    if idx == -1:
        idx = html.find("ROCE")
    if idx == -1:
        return None

    # Search backwards for the header row with Mar years
    chunk = html[max(0, idx-8000):idx]
    header_m = None
    for m in re.finditer(r'<tr[^>]*>(.*?)</tr>', chunk, re.S | re.I):
        row = m.group(1)
        if 'Mar' in row and '<th' in row.lower():
            header_m = m

    if not header_m:
        return None

    headers = re.findall(r'<th[^>]*>(.*?)</th>', header_m.group(1), re.S | re.I)
    headers = [re.sub(r'<[^>]+>', '', h).strip() for h in headers]

    # Find column index for the target year
    target = "Mar " + str(year)
    col_idx = None
    for i, h in enumerate(headers):
        if target in h:
            col_idx = i
            break

    if col_idx is None:
        return None

    # roce_values[0] is "ROCE %" label, so col_idx maps to roce_values[col_idx]
    try:
        return float(roce_values[col_idx].replace('%', ''))
    except Exception:
        return None


def parse_shareholding(html):
    """
    Parse FII and DII from the Shareholding Pattern table.
    Gets the most recent quarter (last column).
    """
    result = {}

    for label, key in [("FIIs", "fiiholding"), ("DIIs", "diiholding"),
                        ("FII", "fiiholding"), ("DII", "diiholding")]:
        if key in result:
            continue
        values = parse_table_row(html, label)
        if not values:
            continue
        # Skip label cell, get last numeric value (most recent quarter)
        for v in reversed(values):
            cleaned = v.replace('%', '').replace(',', '').strip()
            try:
                num = float(cleaned)
                if 0 < num < 100:
                    result[key] = num
                    break
            except Exception:
                pass

    return result


def parse_compounded_tables(html):
    """
    Parse the compounded growth and return-on-equity boxes at the bottom of P&L.
    Structure:
      <h2>Compounded Sales Growth</h2>
      <table>
        <tr><td>10 Years:</td><td>15%</td></tr>
        <tr><td>5 Years:</td><td>18%</td></tr>
        <tr><td>3 Years:</td><td>6%</td></tr>
        <tr><td>TTM:</td><td>10%</td></tr>
      </table>
    """
    result = {}

    sections = {
        "Compounded Sales Growth":   {"10 Years": "salesgrowth10y", "5 Years": "salesgrowth5y",
                                       "3 Years": "salesgrowth3y"},
        "Compounded Profit Growth":  {"10 Years": "profitgrowth10y", "5 Years": "profitgrowth5y",
                                       "3 Years": "profitgrowth3y"},
        "Stock Price CAGR":          {"10 Years": "cagr10y", "5 Years": "cagr5y",
                                       "3 Years": "cagr3y"},
        "Return on Equity":          {"10 Years": "roe10yr", "5 Years": "roe5yr",
                                       "3 Years": "roe3yr"},
    }

    for section_title, metric_map in sections.items():
        idx = html.find(section_title)
        if idx == -1:
            continue
        # Get the table right after this heading (within 1000 chars)
        chunk = html[idx:idx+1500]
        table_m = re.search(r'<table[^>]*>(.*?)</table>', chunk, re.S | re.I)
        if not table_m:
            continue
        rows = re.findall(r'<tr[^>]*>(.*?)</tr>', table_m.group(1), re.S | re.I)
        for row in rows:
            cells = re.findall(r'<td[^>]*>(.*?)</td>', row, re.S | re.I)
            if len(cells) < 2:
                continue
            label = re.sub(r'<[^>]+>', '', cells[0]).strip()
            value_raw = re.sub(r'<[^>]+>', '', cells[1]).replace('%', '').replace(',', '').strip()
            for period, key in metric_map.items():
                if period.lower() in label.lower():
                    try:
                        result[key] = float(value_raw)
                    except Exception:
                        pass

    return result


def get_all_ratios(ticker):
    html = fetch_screener(ticker)
    if not html:
        return {}

    # 1. Top-ratios (9 confirmed correct default metrics)
    result = parse_li_items(extract_ul_section(html, "top-ratios"))

    # 2. Compounded growth tables from P&L section (Sales/Profit Growth, CAGR, ROE averages)
    compound = parse_compounded_tables(html)
    result.update(compound)

    # 3. ROE 3Yr from insights (confirmed accurate — override if better)
    insights_roe = parse_roe_from_insights(html)
    result.update(insights_roe)

    # 4. FII/DII from Shareholding table
    shareholding = parse_shareholding(html)
    result.update(shareholding)

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

    # TTM financials from P&L table
    if metric == "SALESTTM":
        v = parse_ttm_from_pl(html, "Sales")
        return jsonify({"value": v if v is not None else "N/A"})
    if metric == "PATTTM":
        v = parse_ttm_from_pl(html, "Net Profit")
        return jsonify({"value": v if v is not None else "N/A"})

    # ROCE by FY from Ratios table
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

    ratios      = get_all_ratios(ticker)
    values      = {k: v for k, v in ratios.items() if not k.startswith('_label_')}
    compound    = parse_compounded_tables(html)
    shareholding = parse_shareholding(html)
    roce2023    = parse_roce_by_year(html, "2023")
    roce2024    = parse_roce_by_year(html, "2024")
    roce2025    = parse_roce_by_year(html, "2025")
    sales_ttm   = parse_ttm_from_pl(html, "Sales")
    pat_ttm     = parse_ttm_from_pl(html, "Net Profit")

    return jsonify({
        "all_values":    values,
        "compound_tables": compound,
        "shareholding":  shareholding,
        "roce_by_year":  {"2023": roce2023, "2024": roce2024, "2025": roce2025},
        "ttm":           {"sales": sales_ttm, "pat": pat_ttm},
    })


@app.route("/debug-raw-row")
def debug_raw_row():
    """Show raw cells from any table row by label"""
    ticker = request.args.get("ticker", "RELIANCE").upper()
    label  = request.args.get("label", "ROCE %")
    html   = fetch_screener(ticker)
    if not html:
        return jsonify({"error": "no html"})
    values = parse_table_row(html, label)
    idx    = html.find(label)
    context = html[max(0,idx-200):idx+500] if idx != -1 else "not found"
    return jsonify({"label": label, "parsed_values": values, "context": context})


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
