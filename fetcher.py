"""
fetcher.py — 沈董投資系統資料抓取
資料來源優先級：群益 > 政府API（與逍遙投資系統同原則）

功能：
1. 股票清單：TWSE t187ap03_L + TPEX tpex_mainboard_peratio_analysis（白名單）
2. 月營收：政府API t187ap05（批次，上市+上櫃）
3. 季度損益表（含營收+EPS）：群益 zce（逐支，最高優先級）
"""
import requests
import sqlite3
import time
import random
import re
import os
from datetime import datetime, date
from bs4 import BeautifulSoup
from concurrent.futures import ThreadPoolExecutor, as_completed

DB_PATH = os.path.join(os.path.dirname(__file__), 'shendong.db')

_session = requests.Session()
_session.headers.update({
    'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36'
})


def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    """建立資料表"""
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()

    c.execute('''CREATE TABLE IF NOT EXISTS stocks (
        code TEXT PRIMARY KEY,
        name TEXT,
        market TEXT,
        industry TEXT,
        updated_at TEXT
    )''')

    c.execute('''CREATE TABLE IF NOT EXISTS monthly_revenue (
        code TEXT,
        year INTEGER,
        month INTEGER,
        revenue REAL,
        PRIMARY KEY (code, year, month)
    )''')

    c.execute('''CREATE TABLE IF NOT EXISTS quarterly_financial (
        code TEXT,
        year INTEGER,
        quarter INTEGER,
        revenue REAL,
        cost REAL,
        gross_profit REAL,
        operating_expense REAL,
        operating_income REAL,
        non_operating REAL,
        pretax_income REAL,
        net_income REAL,
        eps REAL,
        updated_at TEXT,
        PRIMARY KEY (code, year, quarter)
    )''')

    conn.commit()
    c.execute('''CREATE TABLE IF NOT EXISTS user_estimates (
        code TEXT PRIMARY KEY,
        data TEXT,
        updated_at TEXT
    )''')

    c.execute('''CREATE TABLE IF NOT EXISTS user_watchlist (
        code TEXT PRIMARY KEY,
        added_at TEXT
    )''')

    conn.commit()
    conn.close()


# ── 工具函式 ──────────────────────────────────────────────

def _parse_num(s):
    """解析群益的數值（含千分位逗號、負號）"""
    if not s:
        return None
    s = s.replace(',', '').replace('%', '').strip()
    if s in ('', '-', '--', 'N/A'):
        return None
    try:
        return float(s)
    except:
        return None


def safe_float(val):
    if val is None:
        return None
    try:
        return float(str(val).replace(',', ''))
    except:
        return None


def fetch_json(url, timeout=30):
    """抓取 JSON，失敗回 None"""
    try:
        r = _session.get(url, timeout=timeout)
        return r.json()
    except Exception as e:
        print(f"  [錯誤] {url}: {e}")
        return None


# ── 1. 股票清單（政府API白名單）───────────────────────────

def fetch_stock_list():
    """
    從 TWSE + TPEX 抓取全部上市櫃公司
    用 t187ap03（公司清單）當白名單，只留普通股
    """
    init_db()
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    now_str = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    total = 0

    # 上市白名單
    print("[TWSE] 抓取上市公司清單...")
    twse_list = fetch_json("https://openapi.twse.com.tw/v1/openData/t187ap03_L")
    twse_codes = set()
    if twse_list:
        for r in twse_list:
            code = str(r.get('公司代號', '')).strip()
            name = str(r.get('公司簡稱', '')).strip()
            if code and name and re.match(r'^\d{4}$', code):
                twse_codes.add(code)
                c.execute('''INSERT INTO stocks (code, name, market, updated_at)
                    VALUES (?, ?, '上市', ?)
                    ON CONFLICT(code) DO UPDATE SET
                    name=excluded.name, market=excluded.market, updated_at=excluded.updated_at''',
                    (code, name, now_str))
                total += 1
        print(f"  上市公司：{len(twse_codes)} 家")

    # 上櫃白名單
    print("[TPEX] 抓取上櫃公司清單...")
    tpex_list = fetch_json("https://www.tpex.org.tw/openapi/v1/tpex_mainboard_peratio_analysis")
    tpex_codes = set()
    if tpex_list:
        for r in tpex_list:
            code = str(r.get('SecuritiesCompanyCode', '')).strip()
            name = str(r.get('CompanyName', '')).strip()
            if code and name and re.match(r'^\d{4}$', code):
                tpex_codes.add(code)
                c.execute('''INSERT INTO stocks (code, name, market, updated_at)
                    VALUES (?, ?, '上櫃', ?)
                    ON CONFLICT(code) DO UPDATE SET
                    name=excluded.name, market=excluded.market, updated_at=excluded.updated_at''',
                    (code, name, now_str))
                total += 1
        print(f"  上櫃公司：{len(tpex_codes)} 家")

    conn.commit()
    conn.close()
    print(f"[股票清單] 共 {total} 支")
    return total


# ── 2. 月營收（政府API t187ap05，批次）────────────────────

def fetch_monthly_revenue():
    """
    從政府 API t187ap05 批次抓取最新月營收（上市+上櫃）
    單位：千元（API 原始值）
    同時補寫產業別
    """
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    saved = 0

    # 取得 DB 裡的股票白名單
    valid_codes = set(r[0] for r in c.execute('SELECT code FROM stocks').fetchall())

    sources = [
        ('上市', 'https://openapi.twse.com.tw/v1/openData/t187ap05_L'),
        ('上櫃', 'https://www.tpex.org.tw/openapi/v1/mopsfin_t187ap05_O'),
    ]

    for label, url in sources:
        data = fetch_json(url)
        if not data:
            print(f"  [錯誤] 月營收({label}) 抓取失敗")
            continue

        for d in data:
            code = str(d.get('公司代號', '')).strip()
            if code not in valid_codes:
                continue

            # 產業別順便更新
            industry = str(d.get('產業別', '')).strip()
            if industry:
                c.execute("UPDATE stocks SET industry=? WHERE code=?", (industry, code))

            rev_str = d.get('營業收入-當月營收', '')
            revenue = safe_float(rev_str)
            if revenue is None:
                continue

            year_str = str(d.get('資料年月', '')).strip()
            if not year_str or len(year_str) < 4:
                continue

            # 格式：11503 → 民國115年3月
            try:
                roc_year = int(year_str[:-2])
                month = int(year_str[-2:])
                west_year = roc_year + 1911
            except:
                continue

            c.execute('''INSERT INTO monthly_revenue (code, year, month, revenue)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(code, year, month) DO UPDATE SET revenue=excluded.revenue''',
                (code, west_year, month, revenue))
            saved += 1

        print(f"  月營收({label}) 處理完成")

    conn.commit()
    conn.close()
    print(f"[月營收] 共更新 {saved} 筆（最新一期，政府API）")
    return saved


# ── 2b. 歷史月營收（群益 zch，逐支）─────────────────────

def fetch_capital_monthly_revenue(code):
    """
    從群益抓取個股歷史月營收，存入 monthly_revenue
    群益 zch 單位：千元（與 t187ap05 一致）
    不覆蓋已有資料（政府API優先）
    """
    try:
        url = f"https://stock.capital.com.tw/z/zc/zch/zch.djhtm?a={code}"
        r = _session.get(url, timeout=15)
        r.encoding = 'big5'
        soup = BeautifulSoup(r.text, 'html.parser')
    except:
        return 0

    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    saved = 0

    for t in soup.find_all('table'):
        for row in t.find_all('tr'):
            cells = [td.get_text(strip=True) for td in row.find_all(['td', 'th'])]
            if not cells or not re.match(r'\d+/\d+', cells[0]):
                continue
            if len(cells) < 2:
                continue

            # 格式: "115/03", "12,412,837", ...
            ym = cells[0]
            m = re.match(r'(\d+)/(\d+)', ym)
            if not m:
                continue

            roc_year = int(m.group(1))
            month = int(m.group(2))
            west_year = roc_year + 1911
            revenue = _parse_num(cells[1])

            if revenue is None or revenue <= 0:
                continue

            # 群益 zch 單位就是千元，直接存（與 t187ap05 一致）
            try:
                # 不覆蓋已有值（政府API優先）
                c.execute('''INSERT OR IGNORE INTO monthly_revenue (code, year, month, revenue)
                    VALUES (?,?,?,?)''',
                    (code, west_year, month, revenue))
                if c.rowcount:
                    saved += 1
            except:
                pass

    conn.commit()
    conn.close()
    return saved


def fetch_all_monthly_revenue(max_workers=5, delay=0.3):
    """
    批次抓取所有股票的群益歷史月營收
    用來補齊當年度所有月份（t187ap05 只回最新一期）
    """
    conn = sqlite3.connect(DB_PATH)
    codes = [r[0] for r in conn.execute('SELECT code FROM stocks ORDER BY code').fetchall()]
    conn.close()

    if not codes:
        print("[月營收歷史] 無股票清單")
        return 0

    total = len(codes)
    done = 0
    saved_total = 0

    print(f"[月營收歷史] 開始抓取 {total} 支股票的���益月營收...")

    def _fetch_one(code):
        time.sleep(random.uniform(delay * 0.5, delay * 1.5))
        return code, fetch_capital_monthly_revenue(code)

    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = {executor.submit(_fetch_one, code): code for code in codes}
        for future in as_completed(futures):
            code = futures[future]
            try:
                _, saved = future.result()
                saved_total += saved
                done += 1
                if done % 100 == 0:
                    print(f"  進度: {done}/{total} (已存 {saved_total} 筆月營收)")
            except:
                done += 1

    print(f"[月營收歷史] 完成！共新增 {saved_total} 筆")
    return saved_total


# ── 3. 季度損益表（群益 zce，逐支）────────────────────────

def fetch_capital_quarterly(code):
    """
    從群益抓取個股季度損益表
    群益數值單位：百萬，存入DB要乘 1,000,000
    群益損益表 = 最高優先級，直接覆蓋
    """
    try:
        url = f"https://stock.capital.com.tw/z/zc/zce/zce_{code}.djhtm"
        r = _session.get(url, timeout=15)
        r.encoding = 'big5'
        soup = BeautifulSoup(r.text, 'html.parser')
    except:
        return 0

    # 找有「季別」+「營業收入」表頭的表格
    target_table = None
    for t in soup.find_all('table'):
        rows = t.find_all('tr')
        for row in rows[:3]:
            cells = [td.get_text(strip=True) for td in row.find_all(['td', 'th'])]
            if '季別' in cells and '營業收入' in cells:
                target_table = t
                break
        if target_table:
            break

    if not target_table:
        return 0

    rows = target_table.find_all('tr')
    now_str = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    saved = 0

    for row in rows:
        cells = [td.get_text(strip=True) for td in row.find_all(['td', 'th'])]
        if len(cells) < 10 or not re.match(r'\d+\.\d+Q', cells[0]):
            continue

        q_label = cells[0]
        m = re.match(r'(\d+)\.(\d+)Q', q_label)
        if not m:
            continue

        roc_year = int(m.group(1))
        quarter = int(m.group(2))
        west_year = roc_year + 1911

        revenue = _parse_num(cells[1])
        cost = _parse_num(cells[2])
        gross_profit = _parse_num(cells[3])
        operating_income = _parse_num(cells[5])
        non_operating = _parse_num(cells[7])
        pretax_income = _parse_num(cells[8])
        net_income = _parse_num(cells[9])
        eps = _parse_num(cells[10]) if len(cells) > 10 else None

        # 群益單位：百萬 → 乘 1,000,000
        mul = 1_000_000
        for var_name in ['revenue', 'cost', 'gross_profit', 'operating_income',
                         'non_operating', 'pretax_income', 'net_income']:
            val = locals()[var_name]
            if val is not None:
                locals()[var_name] = val * mul

        revenue = revenue * mul if revenue is not None else None
        cost = cost * mul if cost is not None else None
        gross_profit = gross_profit * mul if gross_profit is not None else None
        operating_income = operating_income * mul if operating_income is not None else None
        non_operating = non_operating * mul if non_operating is not None else None
        pretax_income = pretax_income * mul if pretax_income is not None else None
        net_income = net_income * mul if net_income is not None else None

        # 反算營業費用 = 毛利 - 營業利益
        opex = None
        if gross_profit is not None and operating_income is not None:
            opex = round(gross_profit - operating_income, 4)

        try:
            c.execute('''INSERT INTO quarterly_financial
                (code, year, quarter, revenue, cost, gross_profit, operating_expense,
                 operating_income, non_operating, pretax_income, net_income, eps, updated_at)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(code, year, quarter) DO UPDATE SET
                revenue=excluded.revenue, cost=excluded.cost,
                gross_profit=excluded.gross_profit, operating_expense=excluded.operating_expense,
                operating_income=excluded.operating_income, non_operating=excluded.non_operating,
                pretax_income=excluded.pretax_income, net_income=excluded.net_income,
                eps=excluded.eps, updated_at=excluded.updated_at''',
                (code, west_year, quarter, revenue, cost, gross_profit, opex,
                 operating_income, non_operating, pretax_income, net_income, eps, now_str))
            saved += 1
        except:
            pass

    conn.commit()
    conn.close()
    return saved


def fetch_all_quarterly(max_workers=5, delay=0.3):
    """
    批次抓取所有股票的群益季度損益表
    ~1800支，每支延遲0.3秒，5並發，約需20分鐘
    """
    conn = sqlite3.connect(DB_PATH)
    codes = [r[0] for r in conn.execute('SELECT code FROM stocks ORDER BY code').fetchall()]
    conn.close()

    if not codes:
        print("[季報] 無股票清單，請先執行 fetch_stock_list()")
        return 0

    total = len(codes)
    done = 0
    saved_total = 0
    failed = []

    print(f"[季報] 開始抓取 {total} 支股票的群益季度損益表...")

    def _fetch_one(code):
        time.sleep(random.uniform(delay * 0.5, delay * 1.5))
        return code, fetch_capital_quarterly(code)

    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = {executor.submit(_fetch_one, code): code for code in codes}
        for future in as_completed(futures):
            code = futures[future]
            try:
                _, saved = future.result()
                saved_total += saved
                done += 1
                if done % 100 == 0:
                    print(f"  進度: {done}/{total} (已存 {saved_total} 筆季報)")
            except Exception as e:
                failed.append(code)
                done += 1

    print(f"[季報] 完成！共 {saved_total} 筆，失敗 {len(failed)} 支")
    if failed:
        print(f"  失敗清單（前20）: {failed[:20]}")
    return saved_total


# ── Push 到 Render ──────────────────────────────────────────

RENDER_URL = 'https://shendong-system.onrender.com'
SYNC_TOKEN = os.environ.get('SYNC_TOKEN', 'shendong-sync-2026')
SYNC_HEADERS = {'X-Sync-Token': SYNC_TOKEN}


def _push_table_to_render(table, columns, pk, create_sql=None, where=None, batch_size=500):
    """通用全表同步：把本機資料表 push 到 Render"""
    conn = sqlite3.connect(DB_PATH)
    col_str = ','.join(columns)
    sql = f"SELECT {col_str} FROM {table}"
    if where:
        sql += f" {where}"
    rows = conn.execute(sql).fetchall()
    conn.close()

    if not rows:
        print(f"  [{table}] 無資料")
        return 0

    data = [{columns[j]: r[j] for j in range(len(columns))} for r in rows]

    failed = 0
    for i in range(0, len(data), batch_size):
        batch = data[i:i+batch_size]
        payload = {'table': table, 'columns': columns, 'pk': pk, 'data': batch}
        if i == 0 and create_sql:
            payload['create_sql'] = create_sql
        ok = False
        for attempt in range(3):
            try:
                resp = requests.post(
                    f'{RENDER_URL}/api/sync/table',
                    json=payload,
                    headers=SYNC_HEADERS, timeout=180
                )
                if resp.status_code == 200:
                    ok = True
                    break
                elif resp.status_code == 502 and attempt < 2:
                    time.sleep(5)
                    continue
                else:
                    print(f"  [{table}] batch {i//batch_size+1} HTTP {resp.status_code}")
                    break
            except Exception as e:
                if attempt < 2:
                    time.sleep(5)
                    continue
                print(f"  [{table}] batch {i//batch_size+1} 失敗: {e}")
                break
        if not ok:
            failed += len(batch)

    msg = f"  [{table}] {len(data)} 筆"
    if failed:
        msg += f"（{failed} 筆失敗）"
    print(msg)
    return len(data) - failed


def _push_to_render():
    """本機更新完後，push 所有資料到 Render"""
    if os.environ.get('DATABASE_URL'):
        return
    print("\n[Push to Render] 開始同步...")

    SYNC_TABLES = [
        {
            'table': 'stocks',
            'columns': ['code', 'name', 'market', 'industry', 'updated_at'],
            'pk': ['code'],
        },
        {
            'table': 'monthly_revenue',
            'columns': ['code', 'year', 'month', 'revenue'],
            'pk': ['code', 'year', 'month'],
            'create_sql': """CREATE TABLE IF NOT EXISTS monthly_revenue (
                code TEXT NOT NULL, year INTEGER NOT NULL, month INTEGER NOT NULL,
                revenue REAL, PRIMARY KEY (code, year, month))""",
            'where': f"WHERE year >= {date.today().year - 1}",
        },
        {
            'table': 'quarterly_financial',
            'columns': ['code', 'year', 'quarter', 'revenue', 'cost', 'gross_profit',
                        'operating_expense', 'operating_income', 'non_operating',
                        'pretax_income', 'net_income', 'eps', 'updated_at'],
            'pk': ['code', 'year', 'quarter'],
            'create_sql': """CREATE TABLE IF NOT EXISTS quarterly_financial (
                code TEXT NOT NULL, year INTEGER NOT NULL, quarter INTEGER NOT NULL,
                revenue REAL, cost REAL, gross_profit REAL, operating_expense REAL,
                operating_income REAL, non_operating REAL, pretax_income REAL,
                net_income REAL, eps REAL, updated_at TEXT,
                PRIMARY KEY (code, year, quarter))""",
            'where': f"WHERE year >= {date.today().year - 3}",
        },
    ]

    for cfg in SYNC_TABLES:
        try:
            _push_table_to_render(
                table=cfg['table'],
                columns=cfg['columns'],
                pk=cfg['pk'],
                create_sql=cfg.get('create_sql'),
                where=cfg.get('where'),
            )
        except Exception as e:
            print(f"  [{cfg['table']}] 失敗: {e}")

    # Push stock_info（從逍遙系統 DB 讀取股價/EPS/股利）
    stock_db_path = os.path.join(os.path.dirname(DB_PATH), '..', 'stock_system', 'stocks.db')
    if os.path.exists(stock_db_path):
        try:
            pconn = sqlite3.connect(stock_db_path)
            cols = ['code', 'close', 'change', 'div_c1', 'div_s1', 'div_1_label',
                    'eps_date', 'eps_1', 'eps_1q', 'eps_2', 'eps_2q', 'eps_3', 'eps_3q',
                    'eps_4', 'eps_4q', 'eps_5', 'eps_5q', 'revenue_note']
            rows = pconn.execute(f"SELECT {','.join(cols)} FROM stocks").fetchall()
            pconn.close()

            if rows:
                data = [{cols[j]: r[j] for j in range(len(cols))} for r in rows]
                create_sql = """CREATE TABLE IF NOT EXISTS stock_info (
                    code TEXT PRIMARY KEY, close REAL, change REAL,
                    div_c1 REAL, div_s1 REAL, div_1_label TEXT,
                    eps_date TEXT, eps_1 REAL, eps_1q TEXT, eps_2 REAL, eps_2q TEXT,
                    eps_3 REAL, eps_3q TEXT, eps_4 REAL, eps_4q TEXT,
                    eps_5 REAL, eps_5q TEXT, revenue_note TEXT)"""

                failed = 0
                for i in range(0, len(data), 200):
                    batch = data[i:i+200]
                    try:
                        payload = {'table': 'stock_info', 'columns': cols, 'pk': ['code'], 'data': batch}
                        if i == 0:
                            payload['create_sql'] = create_sql
                        resp = requests.post(
                            f'{RENDER_URL}/api/sync/table',
                            json=payload,
                            headers=SYNC_HEADERS, timeout=180
                        )
                        if resp.status_code != 200:
                            failed += len(batch)
                    except Exception as e:
                        print(f"  [stock_info] batch 失敗: {e}")
                        failed += len(batch)
                msg = f"  [stock_info] {len(data)} 筆"
                if failed:
                    msg += f"（{failed} 筆失敗）"
                print(msg)
        except Exception as e:
            print(f"  [stock_info] 失敗: {e}")

    print("[Push to Render] 完成")


# ── 完整更新流程 ──────────────────────────────────────────

def run_full():
    """完整更新：清單 → 月營收(政府API) → 月營收歷史(群益) → 群益季報"""
    t0 = time.time()
    print("=" * 50)
    print("沈董投資系統 — 完整資料更新")
    print(f"時間：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print("=" * 50)
    init_db()

    print("\n[Step 1] 抓取股票清單（政府API）...")
    fetch_stock_list()

    print("\n[Step 2] 抓取最新月營收（政府API t187ap05 批次）...")
    fetch_monthly_revenue()

    print("\n[Step 3] 抓取歷史月營收（群益 zch 逐支）...")
    fetch_all_monthly_revenue(max_workers=5, delay=0.3)

    print("\n[Step 4] 抓取季度損益表（群益 zce 逐支）...")
    fetch_all_quarterly(max_workers=5, delay=0.3)

    elapsed = time.time() - t0
    print(f"\n{'=' * 50}")
    print(f"更新完成！耗時 {elapsed:.1f} 秒")

    _push_to_render()


def run_quick():
    """快速更新：清單 + 最新月營收（政府API批次，看誰今天公佈了）"""
    t0 = time.time()
    print("=" * 50)
    print("沈董投資系統 — 快速更新（清單+最新月營收）")
    print("=" * 50)
    init_db()

    fetch_stock_list()
    fetch_monthly_revenue()

    elapsed = time.time() - t0
    print(f"快速更新完成！耗時 {elapsed:.1f} 秒")

    _push_to_render()


if __name__ == '__main__':
    import sys
    if len(sys.argv) > 1 and sys.argv[1] == '--quick':
        run_quick()
    else:
        run_full()
