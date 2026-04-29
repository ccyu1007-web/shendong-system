"""
沈董投資系統 — Flask 後端
port 5001（避免與逍遙系統 5000 衝突）
"""
from flask import Flask, render_template, jsonify, request
try:
    from flask_compress import Compress
except ImportError:
    Compress = None
import sqlite3
import os
from datetime import date

DATABASE_URL = os.environ.get('DATABASE_URL')
is_cloud = bool(DATABASE_URL)
SYNC_TOKEN = os.environ.get('SYNC_TOKEN', 'shendong-sync-2026')

if not is_cloud:
    from fetcher import init_db, fetch_stock_list, fetch_monthly_revenue, fetch_all_quarterly, DB_PATH

app = Flask(__name__, template_folder='templates', static_folder='static')
if Compress:
    Compress(app)


def get_db():
    if is_cloud:
        import psycopg2, psycopg2.extras
        url = DATABASE_URL.replace('postgres://', 'postgresql://', 1)
        # 加 sslmode=require（跨 region 需要 SSL）
        sep = '&' if '?' in url else '?'
        if 'sslmode' not in url:
            url += sep + 'sslmode=require'
        conn = psycopg2.connect(url, cursor_factory=psycopg2.extras.RealDictCursor)
        return conn
    else:
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        return conn


def check_sync_token():
    token = request.headers.get('X-Sync-Token') or request.args.get('token')
    return token == SYNC_TOKEN


@app.route('/')
def index():
    return render_template('index.html')


TOCK_API = 'https://tock-system.onrender.com'

# 雲端快取
_stocks_cache = None
_stocks_cache_time = 0
_STOCKS_CACHE_TTL = 300  # 5 分鐘

@app.route('/api/stocks')
def api_stocks():
  try:
    import time as _time
    global _stocks_cache, _stocks_cache_time

    # 雲端快取
    if is_cloud and _stocks_cache and (_time.time() - _stocks_cache_time < _STOCKS_CACHE_TTL):
        return jsonify(_stocks_cache)

    conn = get_db()
    c = conn.cursor()
    ph = '%s' if is_cloud else '?'

    current_year = date.today().year
    last_year = current_year - 1

    # 股票清單
    c.execute('SELECT code, name, market, industry FROM stocks ORDER BY code')
    stocks = c.fetchall()

    # 月營收（去年+今年）— 單位：千元
    c.execute(f'SELECT code, year, month, revenue FROM monthly_revenue WHERE year IN ({ph},{ph}) ORDER BY year, month', (last_year, current_year))
    monthly = c.fetchall()

    available_months_last = sorted(set(r['month'] for r in monthly if r['year'] == last_year))
    available_months = sorted(set(r['month'] for r in monthly if r['year'] == current_year))

    monthly_map = {}       # 今年
    monthly_map_last = {}  # 去年
    for r in monthly:
        if r['year'] == current_year:
            monthly_map.setdefault(r['code'], {})[r['month']] = r['revenue']
        else:
            monthly_map_last.setdefault(r['code'], {})[r['month']] = r['revenue']

    # 季度資料（近5年，供歷史EPS用）
    year_3ago = last_year - 3
    c.execute(f'''SELECT code, year, quarter, revenue, gross_profit, eps FROM quarterly_financial
           WHERE year >= {ph} ORDER BY year, quarter''', (year_3ago,))
    qdata = c.fetchall()

    # 整理季度營收、毛利率、EPS
    qrev_map = {}
    qeps_map = {}
    qgm_map = {}   # 季毛利率
    annual_eps_map = {}  # 年度EPS合計 {code: {year: sum}}
    all_q_keys = set()
    for r in qdata:
        key = f"{r['year']}Q{r['quarter']}"
        # 只有去年+今年的季度才列入欄位
        if r['year'] >= last_year:
            all_q_keys.add((r['year'], r['quarter'], key))
        if r['revenue'] is not None:
            qrev_map.setdefault(r['code'], {})[key] = r['revenue']
        if r['eps'] is not None:
            qeps_map.setdefault(r['code'], {})[key] = r['eps']
            # 累加年度EPS
            annual_eps_map.setdefault(r['code'], {}).setdefault(r['year'], 0)
            annual_eps_map[r['code']][r['year']] += r['eps']
        # 季毛利率
        if r['revenue'] and r['gross_profit'] is not None and r['revenue'] > 0:
            qgm_map.setdefault(r['code'], {})[key] = round(r['gross_profit'] / r['revenue'] * 100, 2)

    # 排序季度欄位
    sorted_q = sorted(all_q_keys, key=lambda x: (x[0], x[1]))
    q_cols = [k[2] for k in sorted_q]
    last_year_q = [k for k in q_cols if k.startswith(str(last_year))]
    current_year_q = [k for k in q_cols if k.startswith(str(current_year))]

    # 讀取股價 + EPS（本機從逍遙系統 DB，雲端從 stock_info 表）
    price_map = {}
    tock_eps = {}  # code -> {quarter_key: eps}
    info_rows = []
    if is_cloud:
        try:
            c.execute('SELECT code, close, change, div_c1, div_s1, div_1_label, eps_date, eps_1, eps_1q, eps_2, eps_2q, eps_3, eps_3q, eps_4, eps_4q, eps_5, eps_5q, revenue_note FROM stock_info')
            info_rows = c.fetchall()
        except:
            pass
    else:
        stock_db_path = os.path.join(os.path.dirname(DB_PATH), '..', 'stock_system', 'stocks.db')
        if os.path.exists(stock_db_path):
            try:
                pconn = sqlite3.connect(stock_db_path)
                pconn.row_factory = sqlite3.Row
                info_rows = pconn.execute('''SELECT code, close, change, div_c1, div_s1, div_1_label,
                        eps_date, eps_1, eps_1q, eps_2, eps_2q, eps_3, eps_3q, eps_4, eps_4q, eps_5, eps_5q,
                        revenue_note FROM stocks''').fetchall()
                pconn.close()
            except:
                pass

    for pr in info_rows:
        pd = {
            'close': pr['close'], 'change': pr['change'],
            'div_cash': pr['div_c1'], 'div_stock': pr['div_s1'], 'div_label': pr['div_1_label'],
            'eps_date': pr['eps_date'], 'eps_latest_q': pr['eps_1q'],
            'revenue_note': pr['revenue_note'],
        }
        for i in range(1, 6):
            pd[f'eps_{i}q'] = pr[f'eps_{i}q']
            pd[f'eps_{i}'] = pr[f'eps_{i}']
        price_map[pr['code']] = pd
        sq = {}
        for i in range(1, 6):
            q = pr[f'eps_{i}q']
            v = pr[f'eps_{i}']
            if q and v is not None:
                roc_yr, qn = q.split('Q')
                west_yr = int(roc_yr) + 1911
                if west_yr in (last_year, current_year):
                    key = f'{west_yr}Q{qn}'
                    sq[key] = v
                    all_q_keys.add((west_yr, int(qn), key))
        tock_eps[pr['code']] = sq

    # 重新排序（可能有新欄位加入）
    sorted_q = sorted(all_q_keys, key=lambda x: (x[0], x[1]))
    q_cols = [k[2] for k in sorted_q]
    last_year_q = [k for k in q_cols if k.startswith(str(last_year))]
    current_year_q = [k for k in q_cols if k.startswith(str(current_year))]

    # 組裝結果
    result = []
    for s in stocks:
        code = s['code']
        pdata = price_map.get(code, {})
        row = {
            'code': code, 'name': s['name'], 'market': s['market'],
            'industry': s['industry'] or '',
            'close': pdata.get('close'), 'change': pdata.get('change'),
            'div_cash': pdata.get('div_cash'), 'div_stock': pdata.get('div_stock'),
            'div_label': pdata.get('div_label'),
            'eps_date': pdata.get('eps_date'),
            'eps_latest_q': pdata.get('eps_latest_q'),
            'revenue_note': pdata.get('revenue_note') or '',
        }

        # 去年月營收
        ml_data = monthly_map_last.get(code, {})
        row['monthly_last'] = {}
        for m in available_months_last:
            row['monthly_last'][str(m)] = ml_data.get(m)

        # 今年月營收
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

        # 季營收
        qr = qrev_map.get(code, {})
        row['quarterly_revenue'] = {q: qr.get(q) for q in q_cols}

        # 季毛利率
        gm = qgm_map.get(code, {})
        row['quarterly_gm'] = {q: gm.get(q) for q in q_cols}

        # 上季比較增減(%) — 比較最後兩個有值的季度
        row['qoq_change'] = None
        filled_qrevs = [(q, qr[q]) for q in q_cols if qr.get(q)]
        if len(filled_qrevs) >= 2:
            prev_qrev = filled_qrevs[-2][1]
            cur_qrev = filled_qrevs[-1][1]
            if prev_qrev > 0:
                row['qoq_change'] = round((cur_qrev - prev_qrev) / prev_qrev * 100, 2)

        # 歷史年度EPS（3年：year_3ago ~ last_year-1）
        ae = annual_eps_map.get(code, {})
        row['annual_eps'] = {}
        for yr in range(year_3ago, last_year):
            if yr in ae:
                row['annual_eps'][str(yr)] = round(ae[yr], 2)

        # 去年EPS合計（114年）
        if last_year in ae:
            row['annual_eps_total'] = round(ae[last_year], 2)
        else:
            row['annual_eps_total'] = None

        # 季EPS（用逍遙系統的 eps_1~eps_5 轉累計）
        qe = tock_eps.get(code, {})
        cum_eps = {}
        cum = 0
        prev_year = None
        for q in q_cols:
            yr = q.split('Q')[0]
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

        # 沈董EPS（從 eps_1~eps_5 計算，跟逍遙系統同邏輯）
        cur_roc = current_year - 1911
        all_eps = []
        for i in range(1, 6):
            q = pdata.get(f'eps_{i}q') if pdata else None
            v = pdata.get(f'eps_{i}') if pdata else None
            if q and v is not None:
                all_eps.append((q, v))
        cur_year_eps = [(q, v) for q, v in all_eps if q and int(q.split('Q')[0]) == cur_roc]
        n = len(cur_year_eps)
        if n >= 4:
            shen_eps = round(sum(v for _, v in cur_year_eps), 2)
        elif n > 0:
            shen_eps = round(sum(v for _, v in cur_year_eps) / n * 4, 2)
        else:
            # fallback: 近四季加總
            eps4 = [v for _, v in all_eps[:4]] if len(all_eps) >= 4 else []
            shen_eps = round(sum(eps4), 2) if len(eps4) == 4 else None
        row['shen_eps'] = shen_eps
        close = row.get('close')
        row['shen_pe'] = round(close / shen_eps, 2) if shen_eps and shen_eps > 0 and close else None

        result.append(row)

    conn.close()

    resp_data = {
        'stocks': result,
        'months': available_months,
        'months_last': available_months_last,
        'quarterly_cols': q_cols,
        'last_year_q': last_year_q,
        'current_year_q': current_year_q,
        'current_year': current_year,
        'last_year': last_year,
        'year_3ago': year_3ago,
        'total': len(result),
    }

    if is_cloud:
        _stocks_cache = resp_data
        _stocks_cache_time = _time.time()

    return jsonify(resp_data)
  except Exception as e:
    import traceback
    return jsonify({'error': str(e), 'trace': traceback.format_exc()}), 500


@app.route('/api/sync/table', methods=['POST'])
def sync_table():
    """通用全表同步 API — 本機 push 資料到 Render PostgreSQL"""
    if not check_sync_token():
        return jsonify({'status': 'error', 'msg': 'unauthorized'}), 403
    if not request.is_json:
        return jsonify({'status': 'error', 'msg': 'not json'}), 400

    table = request.json.get('table', '').strip()
    columns = request.json.get('columns', [])
    pk = request.json.get('pk', [])
    rows = request.json.get('data', [])
    create_sql = request.json.get('create_sql', '')

    ALLOWED_TABLES = {'stocks', 'monthly_revenue', 'quarterly_financial', 'stock_info',
                      'user_estimates', 'user_watchlist'}
    if table not in ALLOWED_TABLES:
        return jsonify({'status': 'error', 'msg': f"table '{table}' not allowed"}), 400
    if not columns or not rows:
        return jsonify({'status': 'ok', 'updated': 0, 'msg': 'no data'})

    conn = get_db()
    c = conn.cursor()

    if create_sql:
        try:
            c.execute(create_sql)
            conn.commit()
        except:
            try: conn.rollback()
            except: pass

    updated = 0
    errors = []
    ph = '%s' if is_cloud else '?'
    placeholders = ','.join([ph] * len(columns))

    if pk:
        non_pk = [col for col in columns if col not in pk]
        conflict_clause = ','.join(pk)
        if non_pk:
            update_clause = ','.join(f'{col}=EXCLUDED.{col}' for col in non_pk)
            sql = f"INSERT INTO {table} ({','.join(columns)}) VALUES ({placeholders}) ON CONFLICT({conflict_clause}) DO UPDATE SET {update_clause}"
        else:
            sql = f"INSERT INTO {table} ({','.join(columns)}) VALUES ({placeholders}) ON CONFLICT({conflict_clause}) DO NOTHING"
    else:
        try:
            c.execute(f"DELETE FROM {table}")
            conn.commit()
        except:
            try: conn.rollback()
            except: pass
        sql = f"INSERT INTO {table} ({','.join(columns)}) VALUES ({placeholders})"

    all_vals = []
    for r in rows:
        all_vals.append([r.get(col) for col in columns])

    try:
        if is_cloud:
            import psycopg2.extras
            psycopg2.extras.execute_batch(c, sql, all_vals, page_size=100)
        else:
            c.executemany(sql, all_vals)
        updated = len(all_vals)
        conn.commit()
    except Exception as e:
        errors.append(str(e))
        try: conn.rollback()
        except: pass
    conn.close()

    # 清除快取讓下次請求讀新資料
    global _stocks_cache
    _stocks_cache = None

    result = {'status': 'ok', 'updated': updated}
    if errors:
        result['errors'] = errors
    return jsonify(result)


@app.route('/company')
def company_page():
    return render_template('company.html')


@app.route('/api/company/<code>/quarterly')
def api_company_quarterly(code):
    """個股季報資料（最近8季）"""
    try:
        if is_cloud:
            import requests as req
            r = req.get(f'{TOCK_API}/api/stocks/{code}/quarterly', timeout=30)
            return jsonify(r.json())
        else:
            # 從逍遙系統 DB 讀取
            stock_db_path = os.path.join(os.path.dirname(DB_PATH), '..', 'stock_system', 'stocks.db')
            conn = sqlite3.connect(stock_db_path)
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                """SELECT * FROM quarterly_financial WHERE code = ?
                   ORDER BY CAST(SUBSTR(quarter, 1, INSTR(quarter, 'Q') - 1) AS INTEGER) DESC,
                            CAST(SUBSTR(quarter, INSTR(quarter, 'Q') + 1) AS INTEGER) DESC
                   LIMIT 8""", (code,)
            ).fetchall()
            # 股票名稱
            name_row = conn.execute("SELECT name FROM stocks WHERE code = ?", (code,)).fetchone()
            name = name_row['name'] if name_row else code

            # 從 financial_annual 讀各年度加權平均股數（千股）
            _year_shares = {}
            for sr in conn.execute("SELECT year, weighted_shares FROM financial_annual WHERE code=?", (code,)).fetchall():
                if sr['weighted_shares']:
                    _year_shares[sr['year']] = sr['weighted_shares']  # 千股
            # fallback：EPS 反算
            _fallback_shares = None
            for r in rows:
                e = r['eps']
                n = r['net_income_parent']
                if e and e != 0 and n is not None:
                    _fallback_shares = n / e  # 股
                    break

            data = []
            for r in rows:
                d = dict(r)
                rev = d.get('revenue')
                pti = d.get('pretax_income')
                nip = d.get('net_income_parent')
                oi = d.get('operating_income')
                tax = d.get('tax')
                eps_val = d.get('eps')
                ci = d.get('continuing_income')

                # 反算稅額
                if pti is not None and nip is not None:
                    calc_tax = round(pti - nip, 2)
                    if tax is None or (tax == 0 and abs(calc_tax) > 100):
                        tax = calc_tax
                        d['tax'] = tax

                # 繼續營業單位損益 fallback
                if ci is None and nip is not None:
                    ci = nip
                    d['continuing_income'] = ci

                # 毛利率
                d['gross_margin'] = round(d['gross_profit'] / rev * 100, 2) if rev and d.get('gross_profit') else None
                # 營業費用占營收比率
                opex = d.get('operating_expense')
                d['opex_ratio'] = round(opex / rev * 100, 2) if rev and opex else None
                # 稅率（虧損不算）
                if pti and pti > 0 and tax is not None:
                    d['tax_rate'] = round(min(max(tax / pti * 100, 0), 100), 2)
                else:
                    d['tax_rate'] = None

                # 加權平均股數（千股）— 優先用 financial_annual，fallback EPS 反算
                quarter = d.get('quarter', '')
                shares_k = None
                shares_raw = None
                if quarter:
                    try:
                        roc_yr = int(quarter.split('Q')[0])
                        west_yr = roc_yr + 1911
                        shares_k = _year_shares.get(west_yr)
                    except: pass
                if shares_k:
                    shares_raw = shares_k * 1000  # 千股→股
                    d['weighted_shares'] = round(shares_k, 0)
                elif eps_val and eps_val != 0 and nip is not None:
                    shares_raw = nip / eps_val
                    d['weighted_shares'] = round(shares_raw / 1000, 0)
                elif _fallback_shares:
                    shares_raw = _fallback_shares
                    d['weighted_shares'] = round(shares_raw / 1000, 0)
                else:
                    d['weighted_shares'] = None

                # 歸屬母公司權重 = 歸屬母公司淨利 / 繼續營業單位損益
                if nip is not None and ci and ci != 0:
                    d['parent_weight'] = round(nip / ci * 100, 2)
                else:
                    d['parent_weight'] = None

                # 本業/業外 EPS
                eff_tax = tax / pti if pti and pti != 0 and tax is not None else None
                if oi is not None and shares_raw and eff_tax is not None:
                    d['eps_core'] = round(oi * (1 - eff_tax) / shares_raw, 2)
                    d['eps_nonop'] = round(eps_val - d['eps_core'], 2) if eps_val is not None else None
                else:
                    d['eps_core'] = None
                    d['eps_nonop'] = None

                data.append(d)

            conn.close()
            return jsonify({"code": code, "name": name, "data": data})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route('/api/company/<code>/info')
def api_company_info(code):
    """個股基本資訊（股價/股利/EPS）"""
    try:
        if is_cloud:
            import requests as req
            r = req.get(f'{TOCK_API}/api/stocks?exact={code}', timeout=30)
            d = r.json()
            stock = d.get('data', [{}])[0]
            return jsonify(stock)
        else:
            stock_db_path = os.path.join(os.path.dirname(DB_PATH), '..', 'stock_system', 'stocks.db')
            conn = sqlite3.connect(stock_db_path)
            conn.row_factory = sqlite3.Row
            row = conn.execute("SELECT * FROM stocks WHERE code = ?", (code,)).fetchone()
            conn.close()
            return jsonify(dict(row) if row else {})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route('/api/estimates/<code>', methods=['GET'])
def api_get_estimate(code):
    """取得個股估算"""
    try:
        if is_cloud:
            import requests as req
            r = req.get(f'{TOCK_API}/api/shendong/estimates/{code}', timeout=10)
            return jsonify(r.json())
        conn = sqlite3.connect(DB_PATH)
        row = conn.execute("SELECT data FROM user_estimates WHERE code=?", (code,)).fetchone()
        conn.close()
        import json
        return jsonify(json.loads(row[0]) if row else {})
    except:
        return jsonify({})


@app.route('/api/estimates/<code>', methods=['POST'])
def api_save_estimate(code):
    """儲存個股估算"""
    try:
        import json
        data = json.dumps(request.json)
        if is_cloud:
            import requests as req
            r = req.post(f'{TOCK_API}/api/shendong/estimates/{code}', json=request.json, timeout=10)
            return jsonify(r.json())
        conn = sqlite3.connect(DB_PATH)
        conn.execute("INSERT OR REPLACE INTO user_estimates (code, data, updated_at) VALUES (?, ?, datetime('now'))", (code, data))
        conn.commit()
        conn.close()
        return jsonify({'ok': True})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/estimates', methods=['GET'])
def api_get_all_estimates():
    """取得所有估算（總表用）"""
    try:
        if is_cloud:
            import requests as req
            r = req.get(f'{TOCK_API}/api/shendong/estimates', timeout=10)
            return jsonify(r.json())
        conn = sqlite3.connect(DB_PATH)
        rows = conn.execute("SELECT code, data FROM user_estimates").fetchall()
        conn.close()
        import json
        result = {}
        for r in rows:
            result[r[0]] = json.loads(r[1])
        return jsonify(result)
    except:
        return jsonify({})


@app.route('/api/watchlist', methods=['GET'])
def api_get_watchlist():
    """取得觀察名單"""
    try:
        if is_cloud:
            import requests as req
            r = req.get(f'{TOCK_API}/api/shendong/watchlist', timeout=10)
            return jsonify(r.json())
        conn = sqlite3.connect(DB_PATH)
        rows = conn.execute("SELECT code FROM user_watchlist ORDER BY added_at").fetchall()
        conn.close()
        return jsonify([r[0] for r in rows])
    except:
        return jsonify([])


@app.route('/api/watchlist', methods=['POST'])
def api_save_watchlist():
    """儲存觀察名單（整份覆蓋）"""
    try:
        codes = request.json.get('codes', [])
        if is_cloud:
            import requests as req
            r = req.post(f'{TOCK_API}/api/shendong/watchlist', json={'codes': codes}, timeout=10)
            return jsonify(r.json())
        conn = sqlite3.connect(DB_PATH)
        conn.execute("DELETE FROM user_watchlist")
        for code in codes:
            conn.execute("INSERT OR IGNORE INTO user_watchlist (code, added_at) VALUES (?, datetime('now'))", (code,))
        conn.commit()
        conn.close()
        return jsonify({'ok': True})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


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


@app.route('/api/debug-tables')
def debug_tables():
    try:
        conn = get_db()
        c = conn.cursor()
        if is_cloud:
            c.execute("SELECT table_schema, table_name FROM information_schema.tables WHERE table_type='BASE TABLE' AND table_schema NOT IN ('pg_catalog','information_schema')")
            tables = [f"{r['table_schema']}.{r['table_name']}" for r in c.fetchall()]
            c.execute("SELECT current_database()")
            db_name = c.fetchone()['current_database']
            c.execute("SELECT datname FROM pg_database WHERE datistemplate = false")
            all_dbs = [r['datname'] for r in c.fetchall()]
        else:
            c.execute("SELECT name FROM sqlite_master WHERE type='table'")
            tables = [r['name'] for r in c.fetchall()]
        conn.close()
        extra = {'all_dbs': all_dbs} if is_cloud else {}
        return jsonify({'tables': tables, 'is_cloud': is_cloud, 'db_name': db_name if is_cloud else 'sqlite', 'db_url_prefix': DATABASE_URL[:30] if DATABASE_URL else None, **extra})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/stats')
def api_stats():
    """資料統計"""
    try:
        conn = get_db()
        c = conn.cursor()
        stocks = c.execute('SELECT COUNT(*) FROM stocks').fetchone()[0]
        monthly = c.execute('SELECT COUNT(*) FROM monthly_revenue').fetchone()[0]
        quarterly = c.execute('SELECT COUNT(*) FROM quarterly_financial').fetchone()[0]
        latest_month = c.execute('SELECT year, month FROM monthly_revenue ORDER BY year DESC, month DESC LIMIT 1').fetchone()
        conn.close()
        return jsonify({
            'stocks': stocks,
            'monthly_revenue_records': monthly,
            'quarterly_records': quarterly,
            'latest_month': f"{latest_month['year']}/{latest_month['month']}" if latest_month else None,
        })
    except:
        return jsonify({'stocks': 0, 'monthly_revenue_records': 0, 'quarterly_records': 0, 'latest_month': None})


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
