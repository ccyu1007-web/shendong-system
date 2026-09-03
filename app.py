"""
沈董投資系統 — Flask 後端
port 5001（避免與逍遙系統 5000 衝突）
共用逍遙投資系統 DB（本機 stocks.db / Render PostgreSQL）
使用者資料（清單/估值/筆記）存 shendong_ 開頭的獨立表
"""
import logging
from flask import Flask, render_template, jsonify, request
import os
import threading
import json
import time as _time
from datetime import date, datetime

# ── DB 抽象層（從逍遙複製的 db.py）──
import db as sqlite3

IS_CLOUD = os.environ.get('DATABASE_URL') is not None

logger = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(name)s] %(levelname)s: %(message)s')

# ── 本機 DB 路徑：逍遙系統的 stocks.db ──
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE_DIR, '..', '..', 'stock_system', 'stocks.db')

app = Flask(__name__, template_folder='templates', static_folder='static')
app.config['SEND_FILE_MAX_AGE_DEFAULT'] = 0

# ── 回應壓縮 ──
try:
    from flask_compress import Compress
    Compress(app)
except ImportError:
    pass

# ── 快取控制 ──
@app.after_request
def add_cache_headers(response):
    if response.content_type and 'text/html' in response.content_type:
        response.headers['Cache-Control'] = 'no-cache, no-store, must-revalidate, max-age=0'
        response.headers['Pragma'] = 'no-cache'
        response.headers['Expires'] = '0'
        response.headers.pop('ETag', None)
        response.headers.pop('Last-Modified', None)
        response.headers['Vary'] = '*'
    return response

# ── 快取 ──
_stocks_cache = None
_stocks_cache_time = 0
_cache_lock = threading.Lock()

# ── 全域設定快取 ──
_global_settings_cache = None
_global_settings_time = 0


# ═══════════════════════════════════════════════════════════
#  Helper functions
# ═══════════════════════════════════════════════════════════

def query_db(sql, args=()):
    with sqlite3.get_conn(path=DB_PATH, row_factory=True) as conn:
        c = conn.cursor()
        c.execute(sql, args)
        rows = [dict(r) for r in c.fetchall()]
    return rows


def _get_global_settings():
    """從 DB 讀取全域設定（逍遙的 user_settings），30 秒快取"""
    global _global_settings_cache, _global_settings_time
    now = _time.time()
    if _global_settings_cache and now - _global_settings_time < 30:
        return _global_settings_cache
    defaults = {
        'div_weights': [30, 30, 20, 10, 10],
        'blend_ratio': {'shen': 50, 'wt': 50},
        'pe_high': 18, 'pe_low': 10,
        'yld_floor': 5, 'yld_high': 5.5, 'yld_max': 6, 'lt_yld': 6,
    }
    try:
        conn = sqlite3.connect(DB_PATH)
        rows = conn.execute(
            "SELECT key, value FROM user_settings WHERE key IN ('global_val_params','blend_ratio','global_div_weights')"
        ).fetchall()
        conn.close()
        for key, val in rows:
            try:
                d = json.loads(val)
                if key == 'global_val_params':
                    for k, dk in [('peHigh','pe_high'),('peLow','pe_low'),('yldFloor','yld_floor'),
                                  ('yldHigh','yld_high'),('yldMax','yld_max'),('ltYld','lt_yld')]:
                        if d.get(k) is not None: defaults[dk] = float(d[k])
                elif key == 'blend_ratio':
                    defaults['blend_ratio'] = d
                elif key == 'global_div_weights':
                    if isinstance(d, list): defaults['div_weights'] = d
            except Exception:
                pass
    except Exception:
        pass
    _global_settings_cache = defaults
    _global_settings_time = now
    return defaults


def _ensure_shendong_tables():
    """確保沈董專用表存在"""
    conn = sqlite3.connect(DB_PATH)
    try:
        conn.execute("""CREATE TABLE IF NOT EXISTS shendong_lists (
            list_type TEXT NOT NULL, code TEXT NOT NULL,
            added_at TEXT, price_at REAL,
            PRIMARY KEY (list_type, code))""")
        conn.execute("""CREATE TABLE IF NOT EXISTS shendong_estimates (
            code TEXT PRIMARY KEY, params TEXT, updated_at TEXT, est_year INTEGER)""")
        conn.execute("""CREATE TABLE IF NOT EXISTS shendong_notes (
            code TEXT PRIMARY KEY, content TEXT, updated_at TEXT)""")
        conn.execute("""CREATE TABLE IF NOT EXISTS shendong_settings (
            key TEXT PRIMARY KEY, value TEXT, updated_at TEXT)""")
        # 補齊可能缺少的欄位
        for col, typ in [('price_at', 'REAL'), ('params', 'TEXT'), ('est_year', 'INTEGER')]:
            for tbl in ['shendong_lists', 'shendong_estimates']:
                try: conn.execute(f"ALTER TABLE {tbl} ADD COLUMN {col} {typ}")
                except Exception: pass
        conn.commit()
    except Exception:
        pass
    conn.close()


def _init_checklist_db():
    """確保 stock_checklist 表存在（由逍遙維護，這裡只確保表和欄位存在）"""
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""CREATE TABLE IF NOT EXISTS stock_checklist (
        code TEXT PRIMARY KEY, pass_count INTEGER, total_count INTEGER,
        detail TEXT, updated_at TEXT)""")
    # 加欄位
    for col, typ in [('profit_count','INTEGER'),('safety_count','INTEGER'),
                     ('value_count','INTEGER'),('growth_eval_count','INTEGER'),
                     ('red_flags','TEXT'),('borderline','TEXT'),
                     ('gi_neff_a','REAL'),('gi_neff_b','REAL'),
                     ('gi_neff_3a','REAL'),('gi_neff_3b','REAL'),
                     ('gi_neff_c','REAL'),('gi_neff_d','REAL'),
                     ('gi_intrinsic_growth','REAL'),
                     ('gi_lynch_a','REAL'),('gi_lynch_b','REAL'),
                     ('gi_lynch_c','REAL'),('gi_lynch_d','REAL'),
                     ('gi_rev_cagr_3y','REAL'),('gi_rev_cagr_5y','REAL'),('gi_shares_change','REAL'),
                     ('gi_yield','REAL'),('gi_pe','REAL'),
                     ('gi_gray','INTEGER'),('gi_neff_gray','INTEGER'),('gi_lynch_gray','INTEGER'),
                     ('gi_warnings','TEXT'),
                     ('gi_shiller_avg_eps','REAL'),('gi_shiller_pe','REAL'),('gi_shiller_alert','REAL'),
                     ('gi_roic_avg','REAL'),('gi_roe_avg','REAL'),('gi_opm_avg','REAL'),('gi_fcf_rev_avg','REAL'),
                     ('growth_signal','TEXT'),('growth_rev_momentum','REAL'),
                     ('growth_eps_trend','REAL'),('growth_inv_risk','INTEGER'),
                     ('gi_rev_3m_yoy','REAL'),('gi_rev_12m_yoy','REAL')]:
        try: conn.execute(f"ALTER TABLE stock_checklist ADD COLUMN {col} {typ}")
        except Exception: pass
    conn.commit()
    conn.close()


# ═══════════════════════════════════════════════════════════
#  Constants (from 逍遙)
# ═══════════════════════════════════════════════════════════

CHECKLIST_ITEMS = [
    {'key': 'roic_avg5',   'category': 'profit', 'label': 'ROIC 近5年平均', 'threshold': '≥ 15%', 'weight': '核心', 'hint': '衡量公司長期投入資本的回報效率，排除財務槓桿影響'},
    {'key': 'roic_latest', 'category': 'profit', 'label': 'ROIC 最近一年', 'threshold': '≥ 15%', 'weight': '核心', 'hint': '確認目前獲利能力仍維持在高水準'},
    {'key': 'roic_trend',  'category': 'profit', 'label': 'ROIC 趨勢：最近一年 ≥ 近3年平均', 'threshold': '是', 'weight': '重要', 'hint': '確認獲利效率沒有走下坡'},
    {'key': 'roic_min5',   'category': 'profit', 'label': 'ROIC 近5年最低值', 'threshold': '≥ 10%', 'weight': '重要', 'hint': '即使在最差年度仍有基本獲利能力，代表護城河穩固'},
    {'key': 'opm_avg5',    'category': 'profit', 'label': '營益率近5年平均', 'threshold': '≥ 10%', 'weight': '核心', 'hint': '本業獲利能力，排除業外收支干擾'},
    {'key': 'opm_trend',   'category': 'profit', 'label': '營益率趨勢：最近一年 ≥ 近3年平均', 'threshold': '是', 'weight': '重要', 'hint': '確認本業獲利沒有衰退'},
    {'key': 'opm_min5',    'category': 'profit', 'label': '營益率近5年最低值', 'threshold': '≥ 5%', 'weight': '輔助', 'hint': '景氣谷底仍能維持正營益率，不至於虧損'},
    {'key': 'gm_trend',    'category': 'profit', 'label': '毛利率趨勢：最近一年 ≥ 近3年平均', 'threshold': '是', 'weight': '輔助', 'hint': '毛利率上升代表產品競爭力或定價權改善'},
    {'key': 'gm_median',   'category': 'profit', 'label': '毛利率位置：最近一年 ≥ 近5年中位數', 'threshold': '是', 'weight': '重要', 'hint': '確認毛利率在歷史水準之上，未被壓縮'},
    {'key': 'gm_q_trend',  'category': 'profit', 'label': '毛利率季趨勢：近4季平均 ≥ 近12季平均', 'threshold': '是', 'weight': '輔助', 'hint': '用季度資料捕捉更即時的毛利率變化方向'},
    {'key': 'debt_ratio_ok',  'category': 'safety', 'label': '負債比', 'threshold': '≤ 50%', 'weight': '核心', 'hint': '負債比過高代表財務風險大，景氣反轉時容易出問題'},
    {'key': 'fin_debt_ok',    'category': 'safety', 'label': '長短期金融負債比', 'threshold': '< 30%', 'weight': '核心', 'hint': '金融負債（銀行借款）佔比過高代表依賴借貸經營'},
    {'key': 'current_ratio',  'category': 'safety', 'label': '流動比率', 'threshold': '≥ 150%', 'weight': '重要', 'hint': '短期償債能力，流動資產能否覆蓋流動負債'},
    {'key': 'quick_ratio',    'category': 'safety', 'label': '速動比率', 'threshold': '≥ 100%', 'weight': '重要', 'hint': '扣除存貨後的短期償債能力，比流動比率更嚴格'},
    {'key': 'icr_ok',         'category': 'safety', 'label': '利息保障倍數', 'threshold': '> 5', 'weight': '重要', 'hint': '營業利益能否輕鬆支付利息費用'},
    {'key': 'icr_min5',       'category': 'safety', 'label': '利息保障倍數近5年最低值', 'threshold': '> 3', 'weight': '重要', 'hint': '即使在最差年度也不至於付不出利息'},
    {'key': 'fcf_5y_pos',     'category': 'safety', 'label': '自由現金流連續5年為正', 'threshold': '是', 'weight': '核心', 'hint': '公司能持續產生現金，不需靠借貸或增資維持營運'},
    {'key': 'fcf_latest_pos', 'category': 'safety', 'label': '最近一年自由現金流 > 0', 'threshold': '是', 'weight': '重要', 'hint': '確認目前仍有正現金流，非靠吃老本'},
    {'key': 'eq_ok',          'category': 'safety', 'label': '盈餘品質率', 'threshold': '≥ 70%', 'weight': '重要', 'hint': '營業現金流 / 稅後淨利，確認獲利有實際現金支撐而非紙上富貴'},
    {'key': 'eq_min5',        'category': 'safety', 'label': '盈餘品質率近5年最低值', 'threshold': '> 60%', 'weight': '重要', 'hint': '長期盈餘品質穩定，非一次性灌水'},
    {'key': 'inv_days_avg',   'category': 'safety', 'label': '存貨週轉天數 ≤ 近5年平均', 'threshold': '是', 'weight': '重要', 'hint': '存貨消化速度正常，沒有庫存堆積風險'},
    {'key': 'inv_days_high',  'category': 'safety', 'label': '存貨週轉天數未創5年新高', 'threshold': '是', 'weight': '輔助', 'hint': '存貨天數創新高可能代表產品滯銷或需求下滑'},
    {'key': 'qinv_4v20',      'category': 'safety', 'label': '近4季存貨週轉天數 < 近20季平均', 'threshold': '是', 'weight': '輔助', 'hint': '用季度資料捕捉更即時的存貨變化趨勢'},
    {'key': 'ar_days_avg',    'category': 'safety', 'label': '應收帳款週轉天數 ≤ 近5年平均', 'threshold': '是', 'weight': '重要', 'hint': '收款速度正常，沒有客戶賴帳風險'},
    {'key': 'ar_days_high',   'category': 'safety', 'label': '應收帳款週轉天數未創5年新高', 'threshold': '是', 'weight': '輔助', 'hint': '應收天數創新高可能代表客戶還款能力變差'},
    {'key': 'grade_a_ok',     'category': 'value', 'label': '預估(沈董)等級為A級以上', 'threshold': '是', 'weight': '核心', 'group': '沈董法', 'hint': '矩陣等級A以上代表PE和殖利率都在合理範圍'},
    {'key': 'blend_grade_ok', 'category': 'value', 'label': '綜合等級為A級以上', 'threshold': '是', 'weight': '核心', 'group': '沈董法', 'hint': '綜合EPS加權後的矩陣等級，A以上代表整體評價合理'},
    {'key': 'eps_vs_multi',   'category': 'value', 'label': '預估(沈董)EPS ≥ 近5年/近3年/十年均EPS 中至少2個', 'threshold': '是', 'weight': '重要', 'group': '沈董法', 'hint': '確認EPS不是異常偏低，估值基礎可靠'},
    {'key': 'eps_vs_10y',     'category': 'value', 'label': '預估(沈董)EPS / 十年平均EPS', 'threshold': '≥ 1', 'weight': '重要', 'group': '沈董法', 'hint': '長期視角確認EPS水準，排除短期高低波動'},
    {'key': 'core_ratio',     'category': 'value', 'label': '累計營業利益 / 累計稅前淨利', 'threshold': '> 80%', 'weight': '重要', 'group': '沈董法', 'hint': '獲利主要來自本業，非靠業外收入撐場'},
    {'key': 'price_val_ok',   'category': 'value', 'label': '現價 ≤ A級評價；≤ AA更佳', 'threshold': '是', 'weight': '重要', 'group': '沈董法', 'hint': '股價低於評價門檻，有安全邊際'},
    {'key': 'eps_5y_pos',     'category': 'value', 'label': '近5年EPS逐年皆 > 0', 'threshold': '是', 'weight': '核心', 'group': 'EPS 品質', 'hint': '穩定獲利是估值的前提，有虧損年度代表風險高'},
    {'key': 'eps_5y_stable',  'category': 'value', 'label': '近5年最高EPS / 最低EPS', 'threshold': '< 3', 'weight': '重要', 'group': 'EPS 品質', 'hint': 'EPS波動太大代表獲利不穩定，估值可靠性低'},
    {'key': 'wt_yld_ok',      'category': 'value', 'label': '綜合殖利率', 'threshold': '≥ 5%', 'weight': '核心', 'group': '殖利率法', 'hint': '股利報酬率夠高，提供持有期間的現金回報'},
    {'key': 'wt_payout_ok',   'category': 'value', 'label': '加權配息率', 'threshold': '40%~80%', 'weight': '重要', 'group': '殖利率法', 'hint': '配息率太低代表股利少，太高代表可能超發不可持續'},
    {'key': 'val_ddm_return', 'category': 'value', 'label': '股利折現現價潛在年報酬', 'threshold': '≥ 10%', 'weight': '重要', 'group': 'DDM', 'hint': '以股利折現模型估算，現價買入的預期年化報酬'},
    {'key': 'dcf_safe_ok',    'category': 'value', 'label': '現價 ≤ DCF安全邊際價', 'threshold': '是', 'weight': '重要', 'group': 'DCF', 'hint': '自由現金流折現後，現價低於內在價值打折後的安全價'},
    {'key': 'ge_neff_ratio',  'category': 'value', 'label': '聶夫 Neff 比率', 'threshold': '≥ 0.7', 'weight': '輔助', 'group': '林區／聶夫法', 'hint': '(EPS成長率+殖利率)/PE，越高代表成長性相對股價越被低估'},
    {'key': 'ge_lynch_peg',   'category': 'value', 'label': '林區 PEG', 'threshold': '≤ 1.0', 'weight': '輔助', 'group': '林區／聶夫法', 'hint': 'PE/EPS成長率，越低代表股價相對成長越便宜'},
    {'key': 'cum_rev_pos',    'category': 'growth_eval', 'label': '累積營收年增率', 'threshold': '≥ 0%', 'weight': '重要', 'hint': '今年以來累積營收是否成長，反映整體趨勢'},
    {'key': 'rev_12m_pos',    'category': 'growth_eval', 'label': '長期12M營收年增率', 'threshold': '≥ 0%', 'weight': '重要', 'hint': '近12個月累計營收年增率，過濾短期波動看長期趨勢'},
    {'key': 'rev_3m_pos',     'category': 'growth_eval', 'label': '短期3M營收年增率', 'threshold': '≥ 0%', 'weight': '重要', 'hint': '近3個月累計營收年增率，捕捉最近的營收動能'},
    {'key': 'rev_both_pos',   'category': 'growth_eval', 'label': '短期3M ≥ 0% 且 長期12M ≥ 0%（一致向上）', 'threshold': '是', 'weight': '輔助', 'hint': '短期和長期營收同時正成長，趨勢一致性高'},
    {'key': 'rev_3m_gt_12m',  'category': 'growth_eval', 'label': '短期3M ≥ 長期12M', 'threshold': '是', 'weight': '重要', 'hint': '短期成長加速，營收動能正在增強而非減弱'},
    {'key': 'growth_green',   'category': 'growth_eval', 'label': '趨勢燈號為多頭', 'threshold': 'green', 'weight': '輔助', 'hint': '綜合營收和EPS趨勢的多空判斷'},
]
CHECKLIST_PROFIT_KEYS = [item['key'] for item in CHECKLIST_ITEMS if item['category'] == 'profit']
CHECKLIST_SAFETY_KEYS = [item['key'] for item in CHECKLIST_ITEMS if item['category'] == 'safety']
CHECKLIST_VALUE_KEYS = [item['key'] for item in CHECKLIST_ITEMS if item['category'] == 'value']
CHECKLIST_GROWTH_EVAL_KEYS = [item['key'] for item in CHECKLIST_ITEMS if item['category'] == 'growth_eval']
CHECKLIST_ALL_KEYS = [item['key'] for item in CHECKLIST_ITEMS]


# ═══════════════════════════════════════════════════════════
#  頁面路由
# ═══════════════════════════════════════════════════════════

@app.route('/')
def index():
    return render_template('index.html')

@app.route('/company')
@app.route('/company.html')
def company_page():
    return render_template('company.html')


# ═══════════════════════════════════════════════════════════
#  主要 API
# ═══════════════════════════════════════════════════════════

@app.route("/api/stocks")
def get_stocks():
    global _stocks_cache, _stocks_cache_time

    q      = request.args.get("q", "").strip()
    market = request.args.get("market", "")
    exact  = request.args.get("exact", "")

    sql = """SELECT code, name, market, industry, close, change, change_240d, volume,
                    revenue_date, revenue_year, revenue_month,
                    revenue_yoy, revenue_mom, revenue_cum_yoy,
                    eps_date, eps_1, eps_1q, eps_2, eps_2q,
                    eps_3, eps_3q, eps_4, eps_4q, eps_5, eps_5q,
                    eps_y1, eps_y1_label, eps_y2, eps_y2_label,
                    eps_y3, eps_y3_label, eps_y4, eps_y4_label,
                    eps_y5, eps_y5_label, eps_y6, eps_y6_label,
                    eps_ytd, eps_ytd_label,
                    div_c1, div_s1, div_1_label, div_c2, div_s2, div_2_label,
                    div_c3, div_s3, div_3_label, div_c4, div_s4, div_4_label,
                    div_c5, div_s5, div_5_label, div_c6, div_s6, div_6_label,
                    contract_1, contract_1q, contract_2, contract_2q,
                    contract_3, contract_3q,
                    fin_grade_1, fin_grade_1y, fin_grade_2, fin_grade_2y,
                    fin_grade_3, fin_grade_3y, fin_grade_4, fin_grade_4y,
                    fin_grade_5, fin_grade_5y, fin_grade_6, fin_grade_6y,
                    price_pos, fair_low, fair_high,
                    inst_foreign, inst_trust, inst_dealer,
                    revenue_note,
                    sys_est_eps, sys_est_quarter, sys_est_confidence,
                    sys_ann_eps, sys_ann_div, sys_ann_pe, sys_ann_yld, sys_ann_confidence,
                    shen_eps, shen_div, shen_pe, shen_yld, shen_grade,
                    weighted_eps, weighted_div, weighted_pe, weighted_yld, weighted_grade, weighted_payout,
                    blend_eps, blend_div, blend_pe, blend_yld, blend_grade,
                    eps_4q_sum, trailing_div, trailing_pe, trailing_yld, trailing_grade,
                    contract_chg, listed_date,
                    payout_1, payout_2, payout_3, payout_4, payout_5, payout_6,
                    val_aa, val_a1, val_a2, val_a, val_lt6,
                    val_eps_used, val_div_used,
                    est_eps, est_div, est_pe, est_yld, est_grade,
                    sys_pe, sys_yld, sys_grade,
                    gb_roic, gb_ey, gb_roic_rank, gb_ey_rank, gb_total_rank
             FROM stocks WHERE 1=1"""
    params = []
    if exact:
        sql += " AND code = ?"
        params.append(exact)
    elif q:
        sql += " AND (code LIKE ? OR name LIKE ?)"
        params += [f"%{q}%", f"%{q}%"]
    if market in ("上市", "上櫃"):
        sql += " AND market = ?"
        params.append(market)
    sql += " ORDER BY code ASC"

    use_cache = not q and not market and not exact
    if use_cache:
        with _cache_lock:
            if _stocks_cache and (_time.time() - _stocks_cache_time < 30):
                return app.response_class(_stocks_cache, content_type='application/json')

    rows = query_db(sql, params)

    # ETF 持股
    etf_map = {}
    try:
        etf_rows = query_db("""
            SELECT h.stock_code,
                   GROUP_CONCAT(h.etf_code || ':' || COALESCE(i.name,''), ',') as etf_list
            FROM etf_holdings h
            LEFT JOIN etf_info i ON h.etf_code = i.code
            GROUP BY h.stock_code
        """)
        for r in etf_rows:
            etf_map[r["stock_code"]] = r["etf_list"]
    except Exception: pass

    # 月營收
    rev_map = {}
    try:
        cur_west = date.today().year
        rev_rows = query_db(
            """SELECT r.code, r.month, r.revenue, r2.revenue as prev_revenue
               FROM monthly_revenue r
               LEFT JOIN monthly_revenue r2 ON r.code = r2.code AND r2.year = r.year - 1 AND r2.month = r.month
               WHERE r.year = ?
               ORDER BY r.code, r.month""", (cur_west,))
        for r in rev_rows:
            code = r['code']
            if code not in rev_map:
                rev_map[code] = []
            yoy = None
            if r['revenue'] and r['prev_revenue'] and r['prev_revenue'] > 0:
                yoy = round((r['revenue'] - r['prev_revenue']) / r['prev_revenue'] * 100, 2)
            rev_map[code].append({'month': r['month'], 'revenue': r['revenue'], 'yoy': yoy})
    except Exception: pass

    # Checklist
    chk_map = {}
    try:
        _init_checklist_db()
        chk_rows = query_db("""SELECT code, pass_count, total_count,
                                profit_count, safety_count, value_count, growth_eval_count,
                                red_flags,
                                gi_neff_a, gi_neff_b, gi_neff_3a, gi_neff_3b,
                                gi_neff_c, gi_neff_d, gi_intrinsic_growth,
                                gi_lynch_a, gi_lynch_b, gi_lynch_c, gi_lynch_d,
                                gi_rev_cagr_3y, gi_rev_cagr_5y, gi_shares_change, gi_yield, gi_pe,
                                gi_gray, gi_neff_gray, gi_lynch_gray, gi_warnings,
                                gi_shiller_avg_eps, gi_shiller_pe, gi_shiller_alert,
                                gi_roic_avg, gi_roe_avg, gi_opm_avg, gi_fcf_rev_avg,
                                growth_signal, growth_rev_momentum, growth_eps_trend, growth_inv_risk,
                                gi_rev_3m_yoy, gi_rev_12m_yoy
                             FROM stock_checklist""")
        for cr in chk_rows:
            chk_map[cr['code']] = cr
    except Exception: pass

    # 沈董自己的估算參數（覆蓋逍遙的預估欄位）
    _sd_est_map = {}
    try:
        _ensure_shendong_tables()
        _sd_rows = query_db("SELECT code, params FROM shendong_estimates WHERE params IS NOT NULL")
        for _sr in _sd_rows:
            try:
                _sd_est_map[_sr['code']] = json.loads(_sr['params'])
            except Exception: pass
    except Exception: pass

    _cur_year = date.today().year
    for row in rows:
        ld = row.get('listed_date')
        row['listed_years'] = _cur_year - int(ld[:4]) if ld and len(ld) >= 4 else None
        row["etf_tags"] = etf_map.get(row["code"], "")
        row["monthly_rev"] = rev_map.get(row["code"], [])

        chk = chk_map.get(row["code"])
        row["_chk_pass"] = chk['pass_count'] if chk else None
        row["_chk_total"] = chk['total_count'] if chk else None
        row["_chk_profit"] = chk['profit_count'] if chk else None
        row["_chk_profit_total"] = len(CHECKLIST_PROFIT_KEYS)
        row["_chk_safety"] = chk['safety_count'] if chk else None
        row["_chk_safety_total"] = len(CHECKLIST_SAFETY_KEYS)
        row["_chk_value"] = chk['value_count'] if chk else None
        row["_chk_value_total"] = len(CHECKLIST_VALUE_KEYS)
        row["_chk_growth"] = chk['growth_eval_count'] if chk else None
        row["_chk_growth_total"] = len(CHECKLIST_GROWTH_EVAL_KEYS)
        try:
            _rf = json.loads(chk['red_flags']) if chk and chk.get('red_flags') else []
            row["_chk_red_flags"] = len(_rf)
        except Exception:
            row["_chk_red_flags"] = 0
        row["_growth_signal"] = chk.get('growth_signal') if chk else None
        row["_growth_rev"] = chk.get('growth_rev_momentum') if chk else None
        row["_growth_eps"] = chk.get('growth_eps_trend') if chk else None
        row["_growth_inv"] = chk.get('growth_inv_risk') if chk else None

        if chk:
            row["_gi"] = {
                'neff_a': chk['gi_neff_a'], 'neff_b': chk['gi_neff_b'],
                'neff_3a': chk['gi_neff_3a'], 'neff_3b': chk['gi_neff_3b'],
                'neff_c': chk['gi_neff_c'], 'neff_d': chk['gi_neff_d'],
                'intrinsic_growth': chk['gi_intrinsic_growth'],
                'lynch_a': chk['gi_lynch_a'], 'lynch_b': chk['gi_lynch_b'],
                'lynch_c': chk['gi_lynch_c'], 'lynch_d': chk['gi_lynch_d'],
                'rev_cagr_3y': chk['gi_rev_cagr_3y'],
                'rev_cagr_5y': chk['gi_rev_cagr_5y'], 'shares_change': chk['gi_shares_change'],
                'yield': chk['gi_yield'], 'pe': chk['gi_pe'],
                'gray': bool(chk['gi_gray']), 'neff_gray': bool(chk['gi_neff_gray']),
                'lynch_gray': bool(chk['gi_lynch_gray']),
                'warnings': json.loads(chk['gi_warnings']) if chk['gi_warnings'] else [],
                'shiller_avg_eps': chk.get('gi_shiller_avg_eps'),
                'shiller_pe': chk.get('gi_shiller_pe'),
                'shiller_alert': chk.get('gi_shiller_alert'),
                'roic_avg': chk.get('gi_roic_avg'),
                'roe_avg': chk.get('gi_roe_avg'),
                'opm_avg': chk.get('gi_opm_avg'),
                'fcf_rev_avg': chk.get('gi_fcf_rev_avg'),
                'rev_3m_yoy': chk.get('gi_rev_3m_yoy'),
                'rev_12m_yoy': chk.get('gi_rev_12m_yoy'),
            }
        else:
            row["_gi"] = None

        # 用沈董自己的估算覆蓋預估欄位
        _sd_up = _sd_est_map.get(row['code'])
        if _sd_up:
            close = row.get('close')
            _sd_eps = None
            _sd_div = None
            # vmEps 優先 > q1~q4 加總
            if _sd_up.get('vmEps') and float(_sd_up.get('vmEps', 0) or 0):
                _sd_eps = round(float(_sd_up['vmEps']), 2)
            else:
                qs = [_sd_up.get(f'q{i}') for i in range(1, 5)]
                qs_vals = [float(v) for v in qs if v]
                if qs_vals:
                    _sd_eps = round(sum(qs_vals), 2)
            if _sd_up.get('vmDiv') and float(_sd_up.get('vmDiv', 0) or 0):
                _sd_div = round(float(_sd_up['vmDiv']), 2)
            elif _sd_up.get('div'):
                _sd_div = round(float(_sd_up['div']), 2)
            if _sd_eps is not None:
                row['est_eps'] = _sd_eps
                row['est_pe'] = round(close / _sd_eps, 2) if _sd_eps > 0 and close else None
            if _sd_div is not None:
                row['est_div'] = _sd_div
                row['est_yld'] = round(_sd_div / close * 100, 2) if _sd_div > 0 and close and close > 0 else None

    result_data = {"count": len(rows), "data": rows}
    resp = jsonify(result_data)
    if use_cache:
        with _cache_lock:
            _stocks_cache = resp.get_data(as_text=True)
            _stocks_cache_time = _time.time()
    return resp


@app.route("/api/status")
def status():
    rows = query_db("SELECT updated_at FROM stocks ORDER BY updated_at DESC LIMIT 1")
    updated = rows[0]["updated_at"] if rows else None
    total = query_db("SELECT COUNT(*) as n FROM stocks")[0]["n"]
    return jsonify({
        "updated_at": updated,
        "api_alerts": [],
        "total": total,
        "is_refreshing": False,
        "bg_done_at": None,
    })


@app.route("/api/realtime")
def realtime():
    import requests as req
    codes_param = request.args.get("codes", "")
    if not codes_param:
        return jsonify([])
    code_list = [c.strip() for c in codes_param.split(",") if c.strip()]
    if not code_list:
        return jsonify([])

    rows = query_db("SELECT code, market FROM stocks WHERE code IN ({})".format(
        ",".join("?" for _ in code_list)), code_list)
    market_map = {r['code']: r['market'] for r in rows}

    all_results = []
    ex_codes = []
    for code in code_list:
        mkt = market_map.get(code, '上市')
        prefix = 'tse' if mkt == '上市' else 'otc'
        ex_codes.append(f"{prefix}_{code}.tw")

    for i in range(0, len(ex_codes), 50):
        batch = ex_codes[i:i+50]
        try:
            url = f"https://mis.twse.com.tw/stock/api/getStockInfo.jsp?ex_ch={'|'.join(batch)}"
            r = req.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=10)
            data = r.json()
            for s in data.get("msgArray", []):
                price = s.get("z")
                if price == "-" or not price:
                    bid = s.get("b", "")
                    if bid and "_" in bid:
                        price = bid.split("_")[0]
                if price == "-" or not price:
                    price = s.get("y")
                parsed_price = float(price) if price else None
                if parsed_price == 0:
                    parsed_price = None
                all_results.append({
                    "code": s.get("c"),
                    "name": s.get("n"),
                    "price": parsed_price,
                    "open": float(s["o"]) if s.get("o") else None,
                    "high": float(s["h"]) if s.get("h") else None,
                    "low": float(s["l"]) if s.get("l") else None,
                    "volume": int(s["v"]) if s.get("v") else None,
                    "time": s.get("t"),
                    "yesterday": float(s["y"]) if s.get("y") else None,
                })
        except Exception: pass
    return jsonify(all_results)


@app.route("/api/news-flags")
def news_flags():
    rows = query_db("""SELECT code, COUNT(*) as cnt FROM material_news
                       WHERE status='important' AND created_at > datetime('now', '-30 days')
                       GROUP BY code""")
    return jsonify({r['code']: r['cnt'] for r in rows})


@app.route("/api/news")
def news():
    code = request.args.get("code")
    limit = int(request.args.get("limit", 50))
    if request.args.get("important") == "1" and code:
        try:
            rows = query_db("""SELECT * FROM material_news
                              WHERE code=? AND status='important' AND created_at > datetime('now', '-30 days')
                              ORDER BY created_at DESC LIMIT ?""", (code, limit))
        except Exception:
            rows = []
        return jsonify(rows)
    # 一般新聞
    if code:
        rows = query_db("SELECT * FROM material_news WHERE code=? ORDER BY created_at DESC LIMIT ?",
                        (code, limit))
    else:
        rows = query_db("SELECT * FROM material_news ORDER BY created_at DESC LIMIT ?", (limit,))
    return jsonify(rows)


# ═══════════════════════════════════════════════════════════
#  個股 API
# ═══════════════════════════════════════════════════════════

@app.route("/api/stocks/<code>/financials")
def get_financials(code):
    max_annual_year = datetime.now().year - 1

    rows = query_db(
        "SELECT * FROM financial_annual WHERE code = ? AND year <= ? ORDER BY year DESC LIMIT 6",
        (code, max_annual_year)
    )

    data = []
    for r in rows:
        d = dict(r)
        rev = d.get('revenue')
        ni  = d.get('net_income')
        ocf = d.get('operating_cf')
        capex = d.get('capex')
        ta  = d.get('total_assets')
        te  = d.get('total_equity')
        cs  = d.get('common_stock')
        eps_val = d.get('eps')
        cd  = d.get('cash_dividend')
        sd  = d.get('stock_dividend')
        oi  = d.get('operating_income')
        pti = d.get('pretax_income')
        nip = d.get('net_income_parent')
        opex = d.get('operating_expense')

        if nip is None and ni is not None:
            nip = ni
            d['net_income_parent'] = nip
        if pti is not None and ni is not None and abs(pti - ni) < 1 and pti > 1000000:
            ni = round(pti * 0.80, 2)
            nip = ni
            d['net_income'] = ni
            d['net_income_parent'] = nip

        d['gross_margin'] = round(d['gross_profit'] / rev * 100, 2) if rev and d.get('gross_profit') is not None else None
        d['opex_ratio'] = round(opex / rev * 100, 2) if rev and opex is not None else None
        d['operating_margin'] = round(oi / rev * 100, 2) if rev and oi is not None else None
        d['pretax_margin'] = round(pti / rev * 100, 2) if rev and pti is not None else None

        tax_val = d.get('tax')
        if pti is not None and ni is not None:
            calc_tax = round(pti - ni, 2)
            if tax_val is None or (tax_val == 0 and abs(calc_tax) > 100):
                tax_val = calc_tax
                d['tax'] = tax_val
        if pti and pti > 0 and tax_val is not None:
            raw_rate = tax_val / pti * 100
            d['tax_rate'] = round(min(max(raw_rate, 0), 100), 2)
        else:
            d['tax_rate'] = None

        d['net_margin'] = round(ni / rev * 100, 2) if rev and ni is not None else None
        d['continuing_income'] = ni
        d['parent_weight'] = round(nip / ni * 100, 2) if ni and ni != 0 and nip is not None else None
        d['roa'] = round(ni / ta * 100, 2) if ta and ni is not None else None
        d['roe'] = round(ni / te * 100, 2) if te and ni is not None else None
        d['earnings_quality'] = round(ocf / ni * 100, 2) if ni and ni > 0 and ocf is not None else None

        _ta = d.get('total_assets')
        _te = d.get('total_equity')
        d['debt_ratio'] = round((_ta - _te) / _ta * 100, 2) if _ta and _ta > 0 and _te is not None else None
        _fin_debt = sum(d.get(f, 0) or 0 for f in
                        ['short_term_debt', 'short_term_notes', 'current_long_term_debt',
                         'long_term_bank_debt', 'other_long_term_debt', 'bonds_payable'])
        d['fin_debt_ratio'] = round(_fin_debt / _ta * 100, 2) if _ta and _ta > 0 and _fin_debt > 0 else (0.0 if _ta and _ta > 0 else None)
        d['fcf'] = round(ocf + capex, 2) if ocf is not None and capex is not None else None

        if eps_val and eps_val != 0 and nip is not None:
            shares_raw = nip / eps_val
            d['weighted_shares'] = round(shares_raw / 1000, 0)
        else:
            shares_raw = None
            d['weighted_shares'] = None

        shares = cs / 10 if cs and cs > 0 else None
        d['fcf_per_share'] = round(d['fcf'] / shares, 2) if d.get('fcf') is not None and shares else None

        if d.get('eps_core') is None and oi is not None and pti and pti != 0 and eps_val is not None:
            d['eps_core'] = round(oi / pti * eps_val, 2)
        if d.get('eps_nonop') is None:
            nop = d.get('non_operating')
            if nop is not None and pti and pti != 0 and eps_val is not None:
                d['eps_nonop'] = round(nop / pti * eps_val, 2)

        total_div = ((cd or 0) + (sd or 0))
        if total_div > 0 and eps_val is not None and eps_val > 0:
            d['payout_ratio'] = round(total_div / eps_val * 100, 2)
        elif total_div > 0 and (eps_val is None or eps_val <= 0):
            d['payout_ratio'] = 100.0
        else:
            d['payout_ratio'] = None

        d['year_label'] = str(d['year'] - 1911)
        data.append(d)

    stock_info = query_db("SELECT name, market FROM stocks WHERE code = ?", (code,))
    name = stock_info[0]['name'] if stock_info else code
    return jsonify({"code": code, "name": name, "data": data})


@app.route("/api/stocks/<code>/quarterly")
def get_quarterly(code):
    q_order = """ORDER BY CAST(SUBSTR(quarter, 1, INSTR(quarter, 'Q') - 1) AS INTEGER) DESC,
                    CAST(SUBSTR(quarter, INSTR(quarter, 'Q') + 1) AS INTEGER) DESC"""
    rows = query_db(
        f"SELECT * FROM quarterly_financial WHERE code = ? {q_order} LIMIT 8", (code,))

    _shares_map = {}
    try:
        fa_rows = query_db("SELECT year, weighted_shares FROM financial_annual WHERE code=?", (code,))
        for fr in fa_rows:
            if fr.get('weighted_shares'):
                _shares_map[fr['year']] = fr['weighted_shares']
    except Exception: pass

    _fallback_shares = None
    for r in rows:
        e = r.get('eps')
        n = r.get('net_income_parent')
        if e and e != 0 and n is not None:
            _fallback_shares = n / e
            break

    data = []
    for r in rows:
        d = dict(r)
        rev = d.get('revenue')
        pti = d.get('pretax_income')
        tax = d.get('tax')
        oi  = d.get('operating_income')
        ci  = d.get('continuing_income')
        nip = d.get('net_income_parent')
        eps_val = d.get('eps')
        opex = d.get('operating_expense')

        if pti is not None and nip is not None and abs(pti - nip) < 1 and pti > 1000000:
            nip = round(pti * 0.80, 2)
            d['net_income_parent'] = nip

        if pti is not None and nip is not None:
            calc_tax = round(pti - nip, 2)
            if tax is None or (tax == 0 and abs(calc_tax) > 100):
                tax = calc_tax
                d['tax'] = tax

        if ci is None and nip is not None:
            ci = nip
            d['continuing_income'] = ci

        d['weighted_shares'] = None
        shares_raw = None
        quarter = d.get('quarter', '')
        if quarter:
            try:
                roc_yr = int(quarter.split('Q')[0])
                west_yr = roc_yr + 1911
                ann_shares = _shares_map.get(west_yr)
                if ann_shares:
                    d['weighted_shares'] = round(ann_shares, 0)
                    shares_raw = ann_shares * 1000
            except Exception: pass
        if shares_raw is None and eps_val is not None and eps_val != 0 and nip is not None:
            shares_raw = nip / eps_val
            d['weighted_shares'] = round(shares_raw / 1000, 0)
        if shares_raw is None and _fallback_shares:
            shares_raw = _fallback_shares
            d['weighted_shares'] = round(shares_raw / 1000, 0)

        d['gross_margin'] = round(d['gross_profit'] / rev * 100, 2) if rev and d.get('gross_profit') is not None else None
        d['opex_ratio'] = round(opex / rev * 100, 2) if rev and opex is not None else None
        if pti and pti > 0 and tax is not None:
            raw_rate = tax / pti * 100
            d['tax_rate'] = round(min(max(raw_rate, 0), 100), 2)
        else:
            d['tax_rate'] = None
        if nip is not None and ci and ci != 0:
            d['parent_weight'] = round(nip / ci * 100, 2)
        else:
            d['parent_weight'] = None

        if d.get('eps_core') is None and oi is not None and pti and pti != 0 and eps_val is not None:
            d['eps_core'] = round(oi / pti * eps_val, 2)
        if d.get('eps_nonop') is None:
            nop = d.get('non_operating')
            if nop is not None and pti and pti != 0 and eps_val is not None:
                d['eps_nonop'] = round(nop / pti * eps_val, 2)

        data.append(d)

    stock_info = query_db("SELECT name FROM stocks WHERE code = ?", (code,))
    name = stock_info[0]['name'] if stock_info else code
    return jsonify({"code": code, "name": name, "data": data})


@app.route("/api/stocks/<code>/checklist")
def get_checklist(code):
    _init_checklist_db()
    rows = query_db("SELECT * FROM stock_checklist WHERE code=?", (code,))
    if rows:
        r = dict(rows[0])
        if r.get('detail'):
            try: r['detail'] = json.loads(r['detail'])
            except Exception: pass
        if r.get('borderline'):
            try: r['_borderline'] = json.loads(r['borderline'])
            except Exception: r['_borderline'] = {}
        else:
            r['_borderline'] = {}
        if r.get('red_flags'):
            try: r['_red_flags'] = json.loads(r['red_flags'])
            except Exception: r['_red_flags'] = []
        else:
            r['_red_flags'] = []
        r['_items'] = CHECKLIST_ITEMS
        return jsonify(r)
    return jsonify({'_items': CHECKLIST_ITEMS})


@app.route("/api/stocks/<code>/system-estimate")
def get_system_estimate(code):
    """讀取逍遙已計算好的系統估算"""
    rows = query_db("SELECT sys_est_eps, sys_est_quarter, sys_est_confidence FROM stocks WHERE code=?", (code,))
    if rows and rows[0].get('sys_est_eps'):
        r = rows[0]
        return jsonify({
            "eps": r['sys_est_eps'],
            "quarter": r['sys_est_quarter'],
            "confidence": r['sys_est_confidence'] or 'N/A',
        })
    return jsonify({"error": "無系統估算", "confidence": "N/A"})


@app.route("/api/stocks/<code>/system-estimate-multi")
def get_system_estimate_multi(code):
    return jsonify({"quarters": []})


@app.route("/api/stocks/<code>/system-estimate-annual")
def get_system_estimate_annual(code):
    rows = query_db("SELECT sys_ann_eps, sys_ann_div, sys_ann_confidence FROM stocks WHERE code=?", (code,))
    if rows and rows[0].get('sys_ann_eps'):
        r = rows[0]
        return jsonify({
            "annual_eps": r['sys_ann_eps'],
            "annual_div": r['sys_ann_div'],
            "confidence": r['sys_ann_confidence'] or 'N/A',
        })
    return jsonify({"error": "無系統年估算"})


@app.route("/api/stocks/<code>/monthly-revenue")
def get_monthly_revenue(code):
    rows = query_db(
        "SELECT * FROM monthly_revenue WHERE code = ? ORDER BY year DESC, month ASC", (code,))

    rev_map = {}
    for r in rows:
        rev_map[(r['year'], r['month'])] = r['revenue']

    all_years = sorted(set(r['year'] for r in rows), reverse=True)
    display_years = all_years[:3]
    if not display_years:
        stock_info = query_db("SELECT name FROM stocks WHERE code = ?", (code,))
        name = stock_info[0]['name'] if stock_info else code
        return jsonify({"code": code, "name": name, "years": [], "data": []})

    data = []
    for m in range(1, 13):
        row = {"month": m}
        for yr in display_years:
            cur = rev_map.get((yr, m))
            if m == 1:
                prev_m = rev_map.get((yr - 1, 12))
            else:
                prev_m = rev_map.get((yr, m - 1))
            prev_y = rev_map.get((yr - 1, m))
            if cur is None:
                row[str(yr)] = {"revenue": None, "mom": None, "yoy": None, "cum_yoy": None}
                continue
            mom = round((cur / prev_m - 1) * 100, 2) if prev_m else None
            yoy = round((cur / prev_y - 1) * 100, 2) if prev_y else None
            cum_cur = sum(rev_map.get((yr, i), 0) for i in range(1, m + 1) if rev_map.get((yr, i)))
            cum_prev = sum(rev_map.get((yr - 1, i), 0) for i in range(1, m + 1) if rev_map.get((yr - 1, i)))
            cum_yoy = round((cum_cur / cum_prev - 1) * 100, 2) if cum_prev and cum_cur else None
            row[str(yr)] = {"revenue": cur, "mom": mom, "yoy": yoy, "cum_yoy": cum_yoy}
        data.append(row)

    stock_info = query_db("SELECT name FROM stocks WHERE code = ?", (code,))
    name = stock_info[0]['name'] if stock_info else code
    return jsonify({"code": code, "name": name, "years": sorted(display_years), "data": data})


@app.route("/api/stocks/<code>/pe-history")
def get_pe_history(code):
    import statistics
    rows = query_db("SELECT * FROM pe_history WHERE code = ? ORDER BY year ASC", (code,))

    data = [dict(r) for r in rows]
    data = data[-8:] if len(data) > 8 else data

    est = {}
    if len(data) >= 3:
        highs = [d['pe_high'] for d in data if d.get('pe_high') is not None]
        lows  = [d['pe_low'] for d in data if d.get('pe_low') is not None]
        if highs:
            est['avg_high'] = round(sum(highs) / len(highs), 2)
            est['median_high'] = round(statistics.median(highs), 2)
        if lows:
            est['avg_low']  = round(sum(lows) / len(lows), 2)
            est['median_low']  = round(statistics.median(lows), 2)
        if len(highs) >= 5:
            trimmed_h = sorted(highs)[1:-1]
            est['trimmed_avg_high'] = round(sum(trimmed_h) / len(trimmed_h), 2)
        if len(lows) >= 5:
            trimmed_l = sorted(lows)[1:-1]
            est['trimmed_avg_low']  = round(sum(trimmed_l) / len(trimmed_l), 2)

    stock_info = query_db("SELECT name FROM stocks WHERE code = ?", (code,))
    name = stock_info[0]['name'] if stock_info else code
    return jsonify({"code": code, "name": name, "data": data, "estimate": est})


@app.route("/api/industry-compare/<code>")
def industry_compare(code):
    import statistics
    target = query_db("SELECT code, name, industry FROM stocks WHERE code = ?", (code,))
    if not target or not target[0].get("industry"):
        return jsonify({"error": "找不到股票或無產業分類"}), 404
    industry = target[0]["industry"]

    peers = query_db("""
        SELECT code, name, close, eps_y1, eps_y2, eps_y1_label,
               revenue_yoy, revenue_cum_yoy, div_c1, div_1_label,
               price_pos, change_240d, market
        FROM stocks
        WHERE industry = ? AND close IS NOT NULL AND close > 0
        ORDER BY code
    """, (industry,))

    for p in peers:
        eps = p.get("eps_y1")
        close = p.get("close")
        p["pe"] = round(close / eps, 2) if eps and eps > 0 and close else None
        div = p.get("div_c1") or 0
        p["yield_pct"] = round(div / close * 100, 2) if close and close > 0 else None
        eps1 = p.get("eps_y1")
        eps2 = p.get("eps_y2")
        p["eps_growth"] = round((eps1 - eps2) / abs(eps2) * 100, 2) if eps1 is not None and eps2 is not None and eps2 != 0 else None

    def rank_desc(lst, key):
        vals = [(i, x.get(key)) for i, x in enumerate(lst)]
        valid = sorted([(i, v) for i, v in vals if v is not None], key=lambda t: t[1], reverse=True)
        ranks = {i: rank for rank, (i, _) in enumerate(valid, 1)}
        return ranks, len(valid)

    def rank_asc(lst, key):
        vals = [(i, x.get(key)) for i, x in enumerate(lst)]
        valid = sorted([(i, v) for i, v in vals if v is not None], key=lambda t: t[1])
        ranks = {i: rank for rank, (i, _) in enumerate(valid, 1)}
        return ranks, len(valid)

    metrics = [
        ("pe", "asc"), ("eps_y1", "desc"), ("eps_growth", "desc"),
        ("revenue_yoy", "desc"), ("revenue_cum_yoy", "desc"),
        ("yield_pct", "desc"), ("change_240d", "desc"),
    ]

    ranking_data = {}
    for key, direction in metrics:
        ranks, total = rank_desc(peers, key) if direction == "desc" else rank_asc(peers, key)
        ranking_data[key] = {"ranks": ranks, "total": total}

    target_idx = next((i for i, p in enumerate(peers) if p["code"] == code), None)

    summary = {}
    if target_idx is not None:
        for key, _ in metrics:
            rd = ranking_data[key]
            rank = rd["ranks"].get(target_idx)
            total = rd["total"]
            if rank and total:
                summary[key] = {"rank": rank, "total": total,
                               "percentile": round((1 - (rank - 1) / total) * 100, 1)}
            else:
                summary[key] = None

    for i, p in enumerate(peers):
        p["rankings"] = {}
        for key, _ in metrics:
            rd = ranking_data[key]
            rank = rd["ranks"].get(i)
            total = rd["total"]
            p["rankings"][key] = {"rank": rank, "total": total} if rank else None

    medians = {}
    for key, _ in metrics:
        vals = [p.get(key) for p in peers if p.get(key) is not None]
        medians[key] = round(statistics.median(vals), 2) if vals else None

    return jsonify({
        "code": code, "name": target[0]["name"], "industry": industry,
        "peer_count": len(peers), "summary": summary, "medians": medians, "peers": peers,
    })


# ═══════════════════════════════════════════════════════════
#  使用者資料 API（沈董獨立表）
# ═══════════════════════════════════════════════════════════

@app.route("/api/user-lists")
def get_user_lists():
    _ensure_shendong_tables()
    rows = query_db("SELECT list_type, code, added_at, price_at FROM shendong_lists ORDER BY list_type, code")
    result = {}
    for r in rows:
        lt = r['list_type']
        if lt not in result:
            result[lt] = []
        result[lt].append({'code': r['code'], 'added_at': r['added_at'], 'price_at': r['price_at']})
    return jsonify(result)


@app.route("/api/user-lists/<list_type>", methods=["POST"])
def update_user_list(list_type):
    _ensure_shendong_tables()
    if list_type not in ('watch', 'hold', 'focus', 'quality', 'skip', 'track'):
        return jsonify({"error": "invalid list_type"}), 400
    data = request.json
    action = data.get('action')
    code = data.get('code')

    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()

    if action == 'add' and code:
        now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        price = data.get('price')
        c.execute("INSERT OR REPLACE INTO shendong_lists (list_type, code, added_at, price_at) VALUES (?,?,?,?)",
                  (list_type, code, now, price))
    elif action == 'remove' and code:
        c.execute("DELETE FROM shendong_lists WHERE list_type=? AND code=?", (list_type, code))
    elif action == 'sync':
        codes = data.get('codes', [])
        c.execute("DELETE FROM shendong_lists WHERE list_type=?", (list_type,))
        now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        for item in codes:
            if isinstance(item, str):
                c.execute("INSERT OR IGNORE INTO shendong_lists (list_type, code, added_at) VALUES (?,?,?)",
                          (list_type, item, now))
            elif isinstance(item, dict):
                c.execute("INSERT OR IGNORE INTO shendong_lists (list_type, code, added_at, price_at) VALUES (?,?,?,?)",
                          (list_type, item.get('code',''), now, item.get('price')))

    conn.commit()
    conn.close()
    return jsonify({"status": "ok"})


@app.route("/api/user-notes/<code>", methods=["GET"])
def get_user_note(code):
    _ensure_shendong_tables()
    rows = query_db("SELECT content, updated_at FROM shendong_notes WHERE code=?", (code,))
    if rows:
        return jsonify(rows[0])
    return jsonify({"content": "", "updated_at": None})


@app.route("/api/user-notes/<code>", methods=["POST"])
def save_user_note(code):
    _ensure_shendong_tables()
    content = request.json.get('content', '')
    now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    if content.strip():
        c.execute("INSERT OR REPLACE INTO shendong_notes (code, content, updated_at) VALUES (?,?,?)",
                  (code, content, now))
    else:
        c.execute("DELETE FROM shendong_notes WHERE code=?", (code,))
    conn.commit()
    conn.close()
    return jsonify({"status": "ok"})


@app.route("/api/user-estimates-all")
def get_all_user_estimates():
    _ensure_shendong_tables()
    rows = query_db("SELECT code, params FROM shendong_estimates WHERE params IS NOT NULL")
    result = {}
    for r in rows:
        try:
            result[r['code']] = json.loads(r['params'])
        except Exception: pass
    return jsonify(result)


@app.route("/api/user-estimates/<code>", methods=["GET"])
def get_user_estimate(code):
    _ensure_shendong_tables()
    rows = query_db("SELECT params, updated_at FROM shendong_estimates WHERE code=?", (code,))
    if rows and rows[0]['params']:
        return jsonify(json.loads(rows[0]['params']))
    return jsonify({})


@app.route("/api/user-estimates/<code>", methods=["POST"])
def save_user_estimate(code):
    _ensure_shendong_tables()
    params = request.json
    now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    est_year = datetime.now().year - 1911
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("INSERT OR REPLACE INTO shendong_estimates (code, params, updated_at, est_year) VALUES (?,?,?,?)",
              (code, json.dumps(params, ensure_ascii=False), now, est_year))
    conn.commit()
    conn.close()
    # 沈董系統不觸發 recalc（由逍遙負責）
    return jsonify({"status": "ok"})


@app.route("/api/user-settings")
def get_user_settings():
    """讀取全域設定：優先 shendong_settings，沒有的 key 才 fallback 到逍遙 user_settings"""
    _ensure_shendong_tables()
    conn = sqlite3.connect(DB_PATH)
    try:
        conn.execute("""CREATE TABLE IF NOT EXISTS user_settings (
            key TEXT PRIMARY KEY, value TEXT, updated_at TEXT)""")
        conn.commit()
    except Exception: pass
    # 先讀逍遙的作為底層預設
    fallback = {}
    for r in conn.execute("SELECT key, value, updated_at FROM user_settings").fetchall():
        fallback[r[0]] = (r[1], r[2] or '2000-01-01')
    # 再讀沈董自己的，覆蓋同名 key
    sd = {}
    for r in conn.execute("SELECT key, value, updated_at FROM shendong_settings").fetchall():
        sd[r[0]] = (r[1], r[2] or '2000-01-01')
    conn.close()
    # 合併：沈董有就用沈董，沒有才用逍遙
    merged = {**fallback, **sd}
    result = {}
    max_time = None
    for k, (v, t) in merged.items():
        result[k] = v
        result[k + '_time'] = t
        if max_time is None or t > max_time:
            max_time = t
    result['_updated_at'] = max_time
    return jsonify(result)


@app.route("/api/user-settings", methods=["POST"])
def save_user_settings():
    """儲存到沈董自己的 settings 表（不影響逍遙）"""
    _ensure_shendong_tables()
    data = request.json
    if not data:
        return jsonify({"error": "no data"}), 400
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    for key, value in data.items():
        c.execute("INSERT OR REPLACE INTO shendong_settings (key, value, updated_at) VALUES (?,?,?)",
                  (key, value, now))
    conn.commit()
    conn.close()
    return jsonify({"status": "ok"})


# ═══════════════════════════════════════════════════════════
#  其他 API（沈董不需要的功能回傳空值）
# ═══════════════════════════════════════════════════════════

@app.route("/api/recalc-derived", methods=["POST"])
def api_recalc_derived():
    """沈董不執行重算（由逍遙負責）"""
    return jsonify({"status": "ok", "updated": 0, "elapsed_sec": 0, "msg": "沈董系統不執行重算"})


@app.route("/api/refresh", methods=["POST"])
def api_refresh():
    return jsonify({"status": "ok", "msg": "沈董系統不執行資料更新"})


@app.route("/api/refresh/status")
def api_refresh_status():
    return jsonify({"is_refreshing": False})


@app.route("/api/refresh/revenue", methods=["POST"])
def api_refresh_revenue():
    return jsonify({"status": "ok", "msg": "沈董系統不執行營收更新"})


@app.route("/api/checklist/refresh", methods=["POST"])
def refresh_checklist():
    return jsonify({"status": "ok", "count": 0, "msg": "沈董系統不執行檢核表重算"})


# ═══════════════════════════════════════════════════════════
#  啟動
# ═══════════════════════════════════════════════════════════

if __name__ == '__main__':
    if not IS_CLOUD:
        if not os.path.exists(DB_PATH):
            print(f"錯誤：找不到逍遙系統 DB：{DB_PATH}")
            print("請先確認 stock_system 已安裝且 stocks.db 存在")
            exit(1)
    _ensure_shendong_tables()
    app.run(port=5001, debug=True)
