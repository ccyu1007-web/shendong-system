"""
沈董投資系統 — Flask 後端
port 5001（避免與逍遙系統 5000 衝突）
"""
from flask import Flask, render_template, jsonify, request
import sqlite3
import os
from datetime import date

DATABASE_URL = os.environ.get('DATABASE_URL')
is_cloud = bool(DATABASE_URL)

if not is_cloud:
    from fetcher import init_db, fetch_stock_list, fetch_monthly_revenue, fetch_all_quarterly, DB_PATH

app = Flask(__name__, template_folder='templates', static_folder='static')


def get_db():
    if is_cloud:
        import psycopg2, psycopg2.extras
        url = DATABASE_URL.replace('postgres://', 'postgresql://', 1)
        conn = psycopg2.connect(url)
        conn.cursor_factory = psycopg2.extras.RealDictCursor
        return conn
    else:
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        return conn


@app.route('/')
def index():
    return render_template('index.html')


@app.route('/api/stocks')
def api_stocks():
    conn = get_db()
    c = conn.cursor()
    ph = '%s' if is_cloud else '?'

    current_year = date.today().year
    last_year = current_year - 1

    # 股票清單（雲端直接讀 close/change/div）
    if is_cloud:
        c.execute('SELECT code, name, market, industry, close, change, div_c1, div_s1, div_1_label FROM stocks ORDER BY code')
    else:
        c.execute('SELECT code, name, market, industry FROM stocks ORDER BY code')
    stocks = c.fetchall()

    # 月營收（當年度）— 單位：千元
    c.execute(f'SELECT code, month, revenue FROM monthly_revenue WHERE year={ph} ORDER BY month', (current_year,))
    monthly = c.fetchall()

    available_months = sorted(set(r['month'] for r in monthly))

    monthly_map = {}
    for r in monthly:
        monthly_map.setdefault(r['code'], {})[r['month']] = r['revenue']

    # 季度資料（去年+今年）
    c.execute(f'''SELECT code, year, quarter, revenue, eps FROM quarterly_financial
           WHERE year IN ({ph},{ph}) ORDER BY year, quarter''', (last_year, current_year))
    qdata = c.fetchall()

    # 整理季度營收和EPS
    qrev_map = {}
    qeps_map = {}
    all_q_keys = set()
    for r in qdata:
        key = f"{r['year']}Q{r['quarter']}"
        all_q_keys.add((r['year'], r['quarter'], key))
        if r['revenue'] is not None:
            qrev_map.setdefault(r['code'], {})[key] = r['revenue']
        if r['eps'] is not None:
            qeps_map.setdefault(r['code'], {})[key] = r['eps']

    # EPS 更新日期：最新一季EPS首次寫入的日期
    eps_date_map = {}
    c.execute('''SELECT code, year, quarter, updated_at
           FROM quarterly_financial WHERE eps IS NOT NULL
           ORDER BY code, year DESC, quarter DESC''')
    for r in c.fetchall():
        code = r['code']
        if code not in eps_date_map:
            dt = r['updated_at']
            dt_str = str(dt)[:10] if dt else None
            eps_date_map[code] = {
                'date': dt_str,
                'quarter': f"{r['year']}Q{r['quarter']}"
            }

    # 排序季度欄位
    sorted_q = sorted(all_q_keys, key=lambda x: (x[0], x[1]))
    q_cols = [k[2] for k in sorted_q]
    last_year_q = [k for k in q_cols if k.startswith(str(last_year))]
    current_year_q = [k for k in q_cols if k.startswith(str(current_year))]

    # 從逍遙系統 DB 讀取收盤價（僅本機）
    price_map = {}
    if not is_cloud:
        stock_db_path = os.path.join(os.path.dirname(DB_PATH), '..', 'stock_system', 'stocks.db')
        if os.path.exists(stock_db_path):
            try:
                pconn = sqlite3.connect(stock_db_path)
                pconn.row_factory = sqlite3.Row
                for pr in pconn.execute('SELECT code, close, change, div_c1, div_s1, div_1_label FROM stocks'):
                    price_map[pr['code']] = {
                        'close': pr['close'], 'change': pr['change'],
                        'div_cash': pr['div_c1'], 'div_stock': pr['div_s1'], 'div_label': pr['div_1_label'],
                    }
                pconn.close()
            except:
                pass

    # 組裝結果
    result = []
    for s in stocks:
        code = s['code']
        if is_cloud:
            row = {
                'code': code, 'name': s['name'], 'market': s['market'],
                'industry': s['industry'] or '',
                'close': s.get('close'), 'change': s.get('change'),
                'div_cash': s.get('div_c1'), 'div_stock': s.get('div_s1'),
                'div_label': s.get('div_1_label'),
                'eps_date': (eps_date_map.get(code) or {}).get('date'),
                'eps_latest_q': (eps_date_map.get(code) or {}).get('quarter'),
            }
        else:
            pdata = price_map.get(code, {})
            row = {
                'code': code, 'name': s['name'], 'market': s['market'],
                'industry': s['industry'] or '',
                'close': pdata.get('close'), 'change': pdata.get('change'),
                'div_cash': pdata.get('div_cash'), 'div_stock': pdata.get('div_stock'),
                'div_label': pdata.get('div_label'),
                'eps_date': (eps_date_map.get(code) or {}).get('date'),
                'eps_latest_q': (eps_date_map.get(code) or {}).get('quarter'),
            }

        # 月營收
        m_data = monthly_map.get(code, {})
        row['monthly'] = {}
        for m in available_months:
            row['monthly'][str(m)] = m_data.get(m)

        # 近兩月變動率
        row['mom_change'] = None
        if len(available_months) >= 2:
            m1 = m_data.get(available_months[-2])
            m2 = m_data.get(available_months[-1])
            if m1 and m2 and m1 > 0:
                row['mom_change'] = round((m2 - m1) / m1 * 100, 2)

        # 季營收（百萬→億顯示由前端處理，這裡傳原始值）
        qr = qrev_map.get(code, {})
        row['quarterly_revenue'] = {q: qr.get(q) for q in q_cols}

        # 季EPS（累計）
        qe = qeps_map.get(code, {})
        cum_eps = {}
        cum = 0
        prev_year = None
        for q in q_cols:
            parts = q.split('Q')
            yr = parts[0]
            if yr != prev_year:
                cum = 0
                prev_year = yr
            v = qe.get(q)
            if v is not None:
                cum += v
                cum_eps[q] = round(cum, 2)
            else:
                cum_eps[q] = None
        row['quarterly_eps'] = cum_eps

        # 沈董EPS：當年度已公佈季度年化推算
        shen_eps = None
        qe_raw = qeps_map.get(code, {})
        cur_q = [(int(k.split('Q')[1]), v) for k, v in qe_raw.items()
                 if k.startswith(str(current_year)) and v is not None]
        if len(cur_q) >= 4:
            shen_eps = round(sum(v for _, v in cur_q), 2)
        elif len(cur_q) > 0:
            s_sum = sum(v for _, v in cur_q)
            shen_eps = round(s_sum / len(cur_q) * 4, 2)
        else:
            # fallback：去年四季加總
            last_q = [(int(k.split('Q')[1]), v) for k, v in qe_raw.items()
                      if k.startswith(str(last_year)) and v is not None]
            if last_q:
                shen_eps = round(sum(v for _, v in last_q), 2)
        row['shen_eps'] = shen_eps

        # 沈董本益比
        close = row.get('close')
        row['shen_pe'] = round(close / shen_eps, 2) if shen_eps and shen_eps > 0 and close else None

        result.append(row)

    conn.close()

    return jsonify({
        'stocks': result,
        'months': available_months,
        'quarterly_cols': q_cols,
        'last_year_q': last_year_q,
        'current_year_q': current_year_q,
        'current_year': current_year,
        'last_year': last_year,
        'total': len(result),
    })


@app.route('/api/update/quick', methods=['POST'])
def api_update_quick():
    """快速更新（清單+月營收）"""
    if is_cloud:
        return jsonify({'ok': False, 'msg': '雲端不執行更新'}), 403
    fetch_stock_list()
    rev = fetch_monthly_revenue()
    return jsonify({'ok': True, 'revenue_updated': rev})


@app.route('/api/update/full', methods=['POST'])
def api_update_full():
    """完整更新（含群益季報）"""
    if is_cloud:
        return jsonify({'ok': False, 'msg': '雲端不執行更新'}), 403
    fetch_stock_list()
    fetch_monthly_revenue()
    q = fetch_all_quarterly()
    return jsonify({'ok': True, 'quarterly_updated': q})


@app.route('/api/stats')
def api_stats():
    """資料統計"""
    conn = get_db()
    c = conn.cursor()
    c.execute('SELECT COUNT(*) as cnt FROM stocks')
    stock_cnt = c.fetchone()['cnt'] if is_cloud else c.fetchone()[0]
    c.execute('SELECT COUNT(*) as cnt FROM monthly_revenue')
    monthly_cnt = c.fetchone()['cnt'] if is_cloud else c.fetchone()[0]
    c.execute('SELECT COUNT(*) as cnt FROM quarterly_financial')
    quarterly_cnt = c.fetchone()['cnt'] if is_cloud else c.fetchone()[0]
    c.execute('SELECT year, month FROM monthly_revenue ORDER BY year DESC, month DESC LIMIT 1')
    latest_month = c.fetchone()
    conn.close()
    return jsonify({
        'stocks': stock_cnt,
        'monthly_revenue_records': monthly_cnt,
        'quarterly_records': quarterly_cnt,
        'latest_month': f"{latest_month['year']}/{latest_month['month']}" if latest_month else None,
    })


@app.route('/api/realtime')
def api_realtime():
    """盤中即時報價（TWSE mis API）"""
    import requests as req
    codes_param = request.args.get('codes', '')
    if not codes_param:
        return jsonify([])

    code_list = [c.strip() for c in codes_param.split(',') if c.strip()]
    if not code_list:
        return jsonify([])

    conn = get_db()
    c = conn.cursor()
    placeholders = ','.join(['%s' if is_cloud else '?'] * len(code_list))
    c.execute(f'SELECT code, market FROM stocks WHERE code IN ({placeholders})', code_list)
    rows = c.fetchall()
    conn.close()
    market_map = {r['code']: r['market'] for r in rows}

    all_results = []
    ex_codes = []
    for code in code_list:
        mkt = market_map.get(code, '上市')
        prefix = 'tse' if mkt == '上市' else 'otc'
        ex_codes.append(f'{prefix}_{code}.tw')

    for i in range(0, len(ex_codes), 50):
        batch = ex_codes[i:i+50]
        try:
            url = f"https://mis.twse.com.tw/stock/api/getStockInfo.jsp?ex_ch={'|'.join(batch)}"
            r = req.get(url, headers={'User-Agent': 'Mozilla/5.0'}, timeout=10)
            data = r.json()
            for s in data.get('msgArray', []):
                price = s.get('z')
                if price == '-' or not price:
                    bid = s.get('b', '')
                    if bid and '_' in bid:
                        price = bid.split('_')[0]
                if price == '-' or not price:
                    price = s.get('y')
                all_results.append({
                    'code': s.get('c'),
                    'price': float(price) if price else None,
                    'yesterday': float(s['y']) if s.get('y') else None,
                })
        except:
            pass

    return jsonify(all_results)


if __name__ == '__main__':
    if not is_cloud:
        init_db()
        conn = get_db()
        count = conn.execute('SELECT COUNT(*) FROM stocks').fetchone()[0]
        conn.close()
    if count == 0:
        print("DB 是空的，先執行快速更新（清單+月營收）...")
        fetch_stock_list()
        fetch_monthly_revenue()

    app.run(port=5001, debug=True)
