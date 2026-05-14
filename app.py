"""
沈董投資系統 — Flask 後端
port 5001（避免與逍遙系統 5000 衝突）
共用逍遙投資系統 DB（本機 stocks.db / Render PostgreSQL）
"""
from flask import Flask, render_template, jsonify, request
try:
    from flask_compress import Compress
except ImportError:
    Compress = None
import sqlite3
import os
import json
import time as _time
from datetime import date

DATABASE_URL = os.environ.get('DATABASE_URL')
is_cloud = bool(DATABASE_URL)
SYNC_TOKEN = os.environ.get('SYNC_TOKEN', 'shendong-sync-2026')

# 本機 DB 路徑：逍遙系統的 stocks.db
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
STOCK_DB_PATH = os.path.join(BASE_DIR, '..', 'stock_system', 'stocks.db')

app = Flask(__name__, template_folder='templates', static_folder='static')
if Compress:
    Compress(app)


def get_db():
    """連線到逍遙系統的 DB（本機 SQLite / Render PostgreSQL）"""
    if is_cloud:
        import psycopg2, psycopg2.extras
        url = DATABASE_URL.replace('postgres://', 'postgresql://', 1)
        sep = '&' if '?' in url else '?'
        if 'sslmode' not in url:
            url += sep + 'sslmode=require'
        conn = psycopg2.connect(url, cursor_factory=psycopg2.extras.RealDictCursor)
        return conn
    else:
        conn = sqlite3.connect(STOCK_DB_PATH)
        conn.row_factory = sqlite3.Row
        conn.execute('PRAGMA journal_mode=WAL')
        conn.execute('PRAGMA busy_timeout=5000')
        return conn


def _ensure_shendong_tables(conn):
    """確保沈董專用表存在"""
    c = conn.cursor()
    if is_cloud:
        for sql in [
            """CREATE TABLE IF NOT EXISTS shendong_estimates (
                code TEXT PRIMARY KEY, data TEXT, updated_at TIMESTAMP DEFAULT NOW())""",
            """CREATE TABLE IF NOT EXISTS shendong_lists (
                list_type TEXT NOT NULL, code TEXT NOT NULL,
                added_at TIMESTAMP DEFAULT NOW(),
                PRIMARY KEY (list_type, code))""",
        ]:
            try:
                c.execute(sql)
                conn.commit()
            except Exception:
                try: conn.rollback()
                except: pass
    else:
        c.execute("""CREATE TABLE IF NOT EXISTS shendong_estimates (
            code TEXT PRIMARY KEY, data TEXT, updated_at TEXT)""")
        c.execute("""CREATE TABLE IF NOT EXISTS shendong_lists (
            list_type TEXT NOT NULL, code TEXT NOT NULL,
            added_at TEXT, PRIMARY KEY (list_type, code))""")
        conn.commit()


def check_sync_token():
    token = request.headers.get('X-Sync-Token') or request.args.get('token')
    return token == SYNC_TOKEN


# ── 頁面 ──

@app.route('/')
def index():
    return render_template('index.html')


@app.route('/company')
def company_page():
    return render_template('company.html')


# ── 快取 ──

_stocks_cache = None
_stocks_cache_time = 0
_STOCKS_CACHE_TTL = 30  # 30 秒


# ── 主 API ──

@app.route('/api/stocks')
def api_stocks():
    try:
        global _stocks_cache, _stocks_cache_time
        if _stocks_cache and (_time.time() - _stocks_cache_time < _STOCKS_CACHE_TTL):
            return jsonify(_stocks_cache)

        conn = get_db()
        _ensure_shendong_tables(conn)
        c = conn.cursor()
        ph = '%s' if is_cloud else '?'

        current_year = date.today().year
        last_year = current_year - 1
        year_3ago = last_year - 3

        # 股票清單（從逍遙 DB）
        c.execute('SELECT code, name, market, industry FROM stocks ORDER BY code')
        stocks_rows = c.fetchall()

        # 月營收
        c.execute(f'SELECT code, year, month, revenue FROM monthly_revenue WHERE year IN ({ph},{ph}) ORDER BY year, month',
                  (last_year, current_year))
        monthly = c.fetchall()

        available_months_last = sorted(set(r['month'] for r in monthly if r['year'] == last_year))
        available_months = sorted(set(r['month'] for r in monthly if r['year'] == current_year))

        monthly_map = {}
        monthly_map_last = {}
        for r in monthly:
            if r['year'] == current_year:
                monthly_map.setdefault(r['code'], {})[r['month']] = r['revenue']
            else:
                monthly_map_last.setdefault(r['code'], {})[r['month']] = r['revenue']

        # 季度資料（quarter 格式為民國年如 "114Q1"）
        roc_3ago = year_3ago - 1911
        # 產生篩選條件：列出所有需要的民國年 Q1~Q4
        q_filters = []
        for yr in range(year_3ago, current_year + 1):
            roc = yr - 1911
            for qn in range(1, 5):
                q_filters.append(f'{roc}Q{qn}')
        placeholders_q = ','.join([ph] * len(q_filters))
        c.execute(f'SELECT code, quarter, revenue, gross_profit, eps FROM quarterly_financial WHERE quarter IN ({placeholders_q})', q_filters)
        qdata_all = c.fetchall()

        qrev_map = {}
        qeps_map = {}
        qgm_map = {}
        annual_eps_map = {}
        all_q_keys = set()

        for r in qdata_all:
            raw_key = str(r['quarter'])  # e.g. "114Q1"
            if 'Q' not in raw_key:
                continue
            parts = raw_key.split('Q')
            roc_yr = int(parts[0])
            qn = int(parts[1])
            west_yr = roc_yr + 1911
            key = f'{west_yr}Q{qn}'  # 統一用西元年 key

            if west_yr < year_3ago:
                continue

            if west_yr >= last_year:
                all_q_keys.add((west_yr, qn, key))

            code = r['code']
            if r['revenue'] is not None:
                qrev_map.setdefault(code, {})[key] = r['revenue']
            if r['eps'] is not None:
                qeps_map.setdefault(code, {})[key] = r['eps']
                annual_eps_map.setdefault(code, {}).setdefault(west_yr, 0)
                annual_eps_map[code][west_yr] += r['eps']
            if r['revenue'] and r['gross_profit'] is not None and r['revenue'] > 0:
                qgm_map.setdefault(code, {})[key] = round(r['gross_profit'] / r['revenue'] * 100, 2)

        # 從 stocks 表讀取股價/EPS/股利/沈董
        c.execute('''SELECT code, close, change, div_c1, div_s1, div_1_label,
                     eps_date, eps_1, eps_1q, eps_2, eps_2q, eps_3, eps_3q,
                     eps_4, eps_4q, eps_5, eps_5q,
                     revenue_note, shen_eps, shen_div, shen_pe, shen_yld
                     FROM stocks''')
        info_rows = c.fetchall()

        price_map = {}
        tock_eps = {}
        for pr in info_rows:
            pd = {
                'close': pr['close'], 'change': pr['change'],
                'div_cash': pr['div_c1'], 'div_stock': pr['div_s1'],
                'div_label': pr['div_1_label'],
                'eps_date': pr['eps_date'],
                'eps_latest_q': pr['eps_1q'],
                'revenue_note': pr['revenue_note'],
                'shen_eps': pr['shen_eps'], 'shen_div': pr['shen_div'],
                'shen_pe': pr['shen_pe'], 'shen_yld': pr['shen_yld'],
            }
            for i in range(1, 6):
                pd[f'eps_{i}q'] = pr[f'eps_{i}q']
                pd[f'eps_{i}'] = pr[f'eps_{i}']
            price_map[pr['code']] = pd

            # EPS 轉季度 key
            sq = {}
            for i in range(1, 6):
                q = pr[f'eps_{i}q']
                v = pr[f'eps_{i}']
                if q and v is not None:
                    roc_yr_str, qn_str = str(q).split('Q')
                    west_yr = int(roc_yr_str) + 1911
                    if west_yr in (last_year, current_year):
                        ekey = f'{west_yr}Q{qn_str}'
                        sq[ekey] = v
                        all_q_keys.add((west_yr, int(qn_str), ekey))
            tock_eps[pr['code']] = sq

        # 排序季度欄位
        sorted_q = sorted(all_q_keys, key=lambda x: (x[0], x[1]))
        q_cols = [k[2] for k in sorted_q]
        last_year_q = [k for k in q_cols if k.startswith(str(last_year))]
        current_year_q = [k for k in q_cols if k.startswith(str(current_year))]

        # 產業清單（供篩選用）
        industries = sorted(set(
            s['industry'] for s in stocks_rows
            if s['industry'] and s['industry'].strip()
        ))

        # 組裝結果
        result = []
        for s in stocks_rows:
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
                'shen_eps': pdata.get('shen_eps'),
                'shen_div': pdata.get('shen_div'),
                'shen_pe': pdata.get('shen_pe'),
                'shen_yld': pdata.get('shen_yld'),
            }

            # 去年月營收
            ml_data = monthly_map_last.get(code, {})
            row['monthly_last'] = {str(m): ml_data.get(m) for m in available_months_last}

            # 今年月營收
            m_data = monthly_map.get(code, {})
            row['monthly'] = {str(m): m_data.get(m) for m in available_months}

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

            # 上季變動
            row['qoq_change'] = None
            filled_qrevs = [(q, qr[q]) for q in q_cols if qr.get(q)]
            if len(filled_qrevs) >= 2:
                prev_qrev = filled_qrevs[-2][1]
                cur_qrev = filled_qrevs[-1][1]
                if prev_qrev > 0:
                    row['qoq_change'] = round((cur_qrev - prev_qrev) / prev_qrev * 100, 2)

            # 歷史年度 EPS
            ae = annual_eps_map.get(code, {})
            row['annual_eps'] = {}
            for yr in range(year_3ago, last_year):
                if yr in ae:
                    row['annual_eps'][str(yr)] = round(ae[yr], 2)

            # 去年 EPS 合計
            row['annual_eps_total'] = round(ae[last_year], 2) if last_year in ae else None

            # 季 EPS（累計）
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
            'industries': industries,
        }

        _stocks_cache = resp_data
        _stocks_cache_time = _time.time()

        return jsonify(resp_data)
    except Exception as e:
        import traceback
        return jsonify({'error': str(e), 'trace': traceback.format_exc()}), 500


# ── 除錯（確認連到哪個 DB）──

@app.route('/api/debug-db')
def debug_db():
    try:
        conn = get_db()
        c = conn.cursor()
        if is_cloud:
            c.execute("SELECT current_database()")
            db_name = c.fetchone()['current_database']
            c.execute("SELECT COUNT(*) as cnt FROM stocks")
            cnt = c.fetchone()['cnt']
            # 確認所有欄位
            c.execute("""SELECT column_name, data_type FROM information_schema.columns
                        WHERE table_name='quarterly_financial' ORDER BY ordinal_position""")
            cols = {r['column_name']: r['data_type'] for r in c.fetchall()}
            # 抽樣一筆
            c.execute("SELECT * FROM quarterly_financial LIMIT 1")
            sample = dict(c.fetchone()) if c.rowcount else {}
        else:
            db_name = 'sqlite'
            cnt = 0
            cols = {}
            sample = {}
        conn.close()
        return jsonify({
            'db_name': db_name,
            'stocks_count': cnt,
            'quarterly_columns': cols,
            'sample_row': sample,
        })
    except Exception as e:
        import traceback
        return jsonify({'error': str(e), 'trace': traceback.format_exc()[:500]})


# ── 多清單 API ──

LIST_TYPES = ('watch', 'hold', 'focus', 'skip')


@app.route('/api/lists')
def api_get_lists():
    """取得所有清單"""
    try:
        conn = get_db()
        _ensure_shendong_tables(conn)
        c = conn.cursor()
        c.execute('SELECT list_type, code FROM shendong_lists ORDER BY list_type, added_at')
        rows = c.fetchall()
        conn.close()
        result = {t: [] for t in LIST_TYPES}
        for r in rows:
            lt = r['list_type']
            if lt in result:
                result[lt].append(r['code'])
        return jsonify(result)
    except Exception:
        return jsonify({t: [] for t in LIST_TYPES})


@app.route('/api/lists/<list_type>', methods=['POST'])
def api_save_list(list_type):
    """儲存指定清單（整份覆蓋）"""
    if list_type not in LIST_TYPES:
        return jsonify({'error': f'invalid list_type: {list_type}'}), 400
    try:
        codes = request.json.get('codes', [])
        conn = get_db()
        _ensure_shendong_tables(conn)
        c = conn.cursor()
        ph = '%s' if is_cloud else '?'
        c.execute(f'DELETE FROM shendong_lists WHERE list_type={ph}', (list_type,))
        for code in codes:
            if is_cloud:
                c.execute(f"INSERT INTO shendong_lists (list_type, code, added_at) VALUES ({ph},{ph},NOW())",
                          (list_type, code))
            else:
                c.execute(f"INSERT OR IGNORE INTO shendong_lists (list_type, code, added_at) VALUES ({ph},{ph},datetime('now'))",
                          (list_type, code))
        conn.commit()
        conn.close()
        return jsonify({'ok': True})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


# ── 估算 API ──

@app.route('/api/estimates', methods=['GET'])
def api_get_all_estimates():
    """取得所有估算（總表用）"""
    try:
        conn = get_db()
        _ensure_shendong_tables(conn)
        c = conn.cursor()
        c.execute('SELECT code, data FROM shendong_estimates')
        rows = c.fetchall()
        conn.close()
        result = {}
        for r in rows:
            try:
                result[r['code']] = json.loads(r['data'])
            except Exception:
                pass
        return jsonify(result)
    except Exception:
        return jsonify({})


@app.route('/api/estimates/<code>', methods=['GET'])
def api_get_estimate(code):
    """取得個股估算"""
    try:
        conn = get_db()
        _ensure_shendong_tables(conn)
        c = conn.cursor()
        ph = '%s' if is_cloud else '?'
        c.execute(f'SELECT data FROM shendong_estimates WHERE code={ph}', (code,))
        row = c.fetchone()
        conn.close()
        return jsonify(json.loads(row['data']) if row else {})
    except Exception:
        return jsonify({})


@app.route('/api/estimates/<code>', methods=['POST'])
def api_save_estimate(code):
    """儲存個股估算"""
    try:
        data = json.dumps(request.json)
        conn = get_db()
        _ensure_shendong_tables(conn)
        c = conn.cursor()
        ph = '%s' if is_cloud else '?'
        if is_cloud:
            c.execute(f"""INSERT INTO shendong_estimates (code, data, updated_at)
                         VALUES ({ph},{ph},NOW())
                         ON CONFLICT(code) DO UPDATE SET data=EXCLUDED.data, updated_at=NOW()""",
                      (code, data))
        else:
            c.execute(f"INSERT OR REPLACE INTO shendong_estimates (code, data, updated_at) VALUES ({ph},{ph},datetime('now'))",
                      (code, data))
        conn.commit()
        conn.close()
        return jsonify({'ok': True})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


# ── 統計 ──

@app.route('/api/stats')
def api_stats():
    try:
        conn = get_db()
        c = conn.cursor()
        c.execute('SELECT COUNT(*) as cnt FROM stocks')
        stocks = c.fetchone()['cnt']
        c.execute('SELECT COUNT(*) as cnt FROM monthly_revenue')
        monthly = c.fetchone()['cnt']
        c.execute('SELECT COUNT(*) as cnt FROM quarterly_financial')
        quarterly = c.fetchone()['cnt']
        c.execute('SELECT year, month FROM monthly_revenue ORDER BY year DESC, month DESC LIMIT 1')
        latest_month = c.fetchone()
        conn.close()
        return jsonify({
            'stocks': stocks,
            'monthly_revenue_records': monthly,
            'quarterly_records': quarterly,
            'latest_month': f"{latest_month['year']}/{latest_month['month']}" if latest_month else None,
        })
    except Exception:
        return jsonify({'stocks': 0, 'monthly_revenue_records': 0, 'quarterly_records': 0, 'latest_month': None})


# ── 即時報價 ──

@app.route('/api/realtime')
def api_realtime():
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
        except Exception:
            pass

    return jsonify(all_results)


# ── 個股頁 ──

TOCK_API = 'https://tock-system.onrender.com'


@app.route('/api/company/<code>/quarterly')
def api_company_quarterly(code):
    """個股季報資料（最近8季）"""
    try:
        conn = get_db()
        c = conn.cursor()
        ph = '%s' if is_cloud else '?'

        if is_cloud:
            c.execute(f"""SELECT * FROM quarterly_financial WHERE code = {ph}
                         ORDER BY CAST(SPLIT_PART(quarter, 'Q', 1) AS INTEGER) DESC,
                                  CAST(SPLIT_PART(quarter, 'Q', 2) AS INTEGER) DESC
                         LIMIT 8""", (code,))
        else:
            c.execute(f"""SELECT * FROM quarterly_financial WHERE code = {ph}
                         ORDER BY CAST(SUBSTR(quarter, 1, INSTR(quarter, 'Q') - 1) AS INTEGER) DESC,
                                  CAST(SUBSTR(quarter, INSTR(quarter, 'Q') + 1) AS INTEGER) DESC
                         LIMIT 8""", (code,))
        rows = c.fetchall()

        c.execute(f"SELECT name FROM stocks WHERE code = {ph}", (code,))
        name_row = c.fetchone()
        name = name_row['name'] if name_row else code

        # 加權平均股數
        _year_shares = {}
        try:
            c.execute(f"SELECT year, weighted_shares FROM financial_annual WHERE code={ph}", (code,))
            for sr in c.fetchall():
                if sr['weighted_shares']:
                    _year_shares[sr['year']] = sr['weighted_shares']
        except Exception:
            pass

        _fallback_shares = None
        for r in rows:
            e = r.get('eps') if isinstance(r, dict) else r['eps']
            n = r.get('net_income_parent') if isinstance(r, dict) else (r['net_income_parent'] if 'net_income_parent' in (r.keys() if hasattr(r, 'keys') else []) else None)
            if e and e != 0 and n is not None:
                _fallback_shares = n / e
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

            if pti is not None and nip is not None:
                calc_tax = round(pti - nip, 2)
                if tax is None or (tax == 0 and abs(calc_tax) > 100):
                    tax = calc_tax
                    d['tax'] = tax

            if ci is None and nip is not None:
                ci = nip
                d['continuing_income'] = ci

            d['gross_margin'] = round(d['gross_profit'] / rev * 100, 2) if rev and d.get('gross_profit') else None
            opex = d.get('operating_expense')
            d['opex_ratio'] = round(opex / rev * 100, 2) if rev and opex else None
            if pti and pti > 0 and tax is not None:
                d['tax_rate'] = round(min(max(tax / pti * 100, 0), 100), 2)
            else:
                d['tax_rate'] = None

            quarter = d.get('quarter', '')
            shares_k = None
            shares_raw = None
            if quarter:
                try:
                    roc_yr = int(str(quarter).split('Q')[0])
                    west_yr = roc_yr + 1911
                    shares_k = _year_shares.get(west_yr)
                except Exception:
                    pass
            if shares_k:
                shares_raw = shares_k * 1000
                d['weighted_shares'] = round(shares_k, 0)
            elif eps_val and eps_val != 0 and nip is not None:
                shares_raw = nip / eps_val
                d['weighted_shares'] = round(shares_raw / 1000, 0)
            elif _fallback_shares:
                shares_raw = _fallback_shares
                d['weighted_shares'] = round(shares_raw / 1000, 0)
            else:
                d['weighted_shares'] = None

            if nip is not None and ci and ci != 0:
                d['parent_weight'] = round(nip / ci * 100, 2)
            else:
                d['parent_weight'] = None

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
    """個股基本資訊"""
    try:
        conn = get_db()
        c = conn.cursor()
        ph = '%s' if is_cloud else '?'
        c.execute(f"SELECT * FROM stocks WHERE code = {ph}", (code,))
        row = c.fetchone()
        conn.close()
        return jsonify(dict(row) if row else {})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ── Sync API（接收本機 push）──

@app.route('/api/sync/table', methods=['POST'])
def sync_table():
    """通用全表同步 API"""
    if not check_sync_token():
        return jsonify({'status': 'error', 'msg': 'unauthorized'}), 403
    if not request.is_json:
        return jsonify({'status': 'error', 'msg': 'not json'}), 400

    table = request.json.get('table', '').strip()
    columns = request.json.get('columns', [])
    pk = request.json.get('pk', [])
    rows = request.json.get('data', [])
    create_sql = request.json.get('create_sql', '')

    ALLOWED_TABLES = {'shendong_estimates', 'shendong_lists'}
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
        except Exception:
            try: conn.rollback()
            except: pass

    ph = '%s' if is_cloud else '?'
    placeholders = ','.join([ph] * len(columns))
    updated = 0
    errors = []

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
        except Exception:
            try: conn.rollback()
            except: pass
        sql = f"INSERT INTO {table} ({','.join(columns)}) VALUES ({placeholders})"

    all_vals = [tuple(r.get(col) for col in columns) for r in rows]

    try:
        if is_cloud:
            from psycopg2.extras import execute_values
            tpl = f"({','.join(['%s'] * len(columns))})"
            execute_values(c, sql.replace(f"VALUES ({placeholders})", "VALUES %s"), all_vals, template=tpl, page_size=200)
        else:
            c.executemany(sql, all_vals)
        updated = len(all_vals)
        conn.commit()
    except Exception as e:
        errors.append(str(e))
        try: conn.rollback()
        except: pass

    conn.close()
    global _stocks_cache
    _stocks_cache = None

    result = {'status': 'ok', 'updated': updated}
    if errors:
        result['errors'] = errors
    return jsonify(result)


if __name__ == '__main__':
    if not is_cloud:
        if not os.path.exists(STOCK_DB_PATH):
            print(f"錯誤：找不到逍遙系統 DB：{STOCK_DB_PATH}")
            print("請先確認 stock_system 已安裝且 stocks.db 存在")
            exit(1)
        conn = get_db()
        _ensure_shendong_tables(conn)
        conn.close()
    app.run(port=5001, debug=True)
