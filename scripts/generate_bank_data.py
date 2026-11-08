#!/usr/bin/env python3
"""
Synthetic retail-banking data generator (PNC 360 Financial Health Navigator track).

Outputs (in --out):
  PNC-compatible, column-for-column:  profiles.csv  accounts.csv  transactions.csv  monthly_statements.csv
  Side files (extra context, safe to ignore):  customers.csv  account_terms.csv  ira_holdings.csv

Design rules
  * Ledger model: opening_balance + sum(balance_change) = balance, for every account. All math is in integer cents.
  * Liability balances are positive amounts owed. `amount` is unsigned, `balance_change` is signed per account.
  * Transfers between a customer's own accounts are two-leg pairs sharing a transfer_id.
  * Fully deterministic: same --seed and --n give byte-identical files. Each customer has its own RNG stream.
Run:  python3 scripts/generate_bank_data.py --n 100      (--out defaults to <repo>/data)
"""
import argparse, calendar, contextlib, csv, math, os, random
from datetime import date, timedelta

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
START, END = date(2024, 11, 1), date(2026, 10, 31)
ACCT_ORDER = ['checking', 'savings', 'cd', 'ira', 'credit_card', 'auto_loan', 'personal_loan', 'mortgage']
LIABS = {'credit_card', 'auto_loan', 'personal_loan', 'mortgage'}

# ------------------------------------------------------------------ helpers
def add_months(d, n):
    y, m = divmod(d.year * 12 + d.month - 1 + n, 12)
    m += 1
    return date(y, m, min(d.day, calendar.monthrange(y, m)[1]))

def month_end(d): return date(d.year, d.month, calendar.monthrange(d.year, d.month)[1])
def prev_bd(d):
    while d.weekday() >= 5: d -= timedelta(days=1)
    return d
def next_bd(d):
    while d.weekday() >= 5: d += timedelta(days=1)
    return d
def due(y, m, day, mode):
    d = date(y, m, min(day, calendar.monthrange(y, m)[1]))
    return next_bd(d) if mode == 'next' else prev_bd(d) if mode == 'prev' else d

def money(c):  # integer cents -> '1234.50' (handles negatives)
    s = '-' if c < 0 else ''
    c = abs(c)
    return f"{s}{c // 100}.{c % 100:02d}"

def interp(x, pts):  # piecewise-linear in log(x)
    if x <= pts[0][0]: return pts[0][1]
    for (x0, y0), (x1, y1) in zip(pts, pts[1:]):
        if x <= x1:
            t = (math.log(x) - math.log(x0)) / (math.log(x1) - math.log(x0))
            return y0 + t * (y1 - y0)
    return pts[-1][1]

def clip(x, lo, hi): return max(lo, min(hi, x))
def lognorm(r, median, sigma): return median * math.exp(r.gauss(0, sigma))
def wchoice(r, items):  # items = [(value, weight)]
    t = r.random() * sum(w for _, w in items)
    for v, w in items:
        t -= w
        if t <= 0: return v
    return items[-1][0]

MONTHS = []
_d = date(START.year, START.month, 1)
while _d <= END:
    MONTHS.append((_d.year, _d.month)); _d = add_months(_d, 1)

# ------------------------------------------------------------------ real-population parameters (assumptions; cite sources before publishing)
AGE_BANDS = [((22, 34), .23), ((35, 44), .17), ((45, 54), .17), ((55, 64), .18), ((65, 74), .15), ((75, 84), .10)]
SCORE_PTS = [(24e3, 650), (45e3, 675), (65e3, 700), (90e3, 730), (130e3, 755), (200e3, 775), (450e3, 785)]  # median FICO by income
NET_PTS = [(24e3, .89), (40e3, .85), (70e3, .80), (120e3, .74), (200e3, .68), (450e3, .62)]              # take-home share of gross
SCF_LIQUID_MEDIAN = [(35, 5000), (45, 6600), (55, 9400), (65, 10000), (75, 10000), (99, 14000)]          # SCF 2025 transaction accts by age
REVOLVE_BY_INCOME = [(15e3, .28), (27e3, .40), (40e3, .50), (52e3, .42), (68e3, .58), (85e3, .57), (105e3, .61), (135e3, .49), (190e3, .46), (1e12, .26)]
# annual BLS CE (2021 $) by income quintile: mean income, groceries, dining, utilities, transportation, apparel, entertainment, healthcare
BLS_INC = [13165, 34767, 61214, 100527, 226386]
BLS = {'groceries': [3526, 4021, 4766, 5834, 8095], 'dining': [1350, 1787, 2610, 3528, 5824],
       'utilities': [2775, 3511, 4068, 4821, 5905], 'transportation': [4287, 8014, 10284, 13004, 19197],
       'apparel': [940, 1201, 1538, 1763, 3294], 'entertainment': [1395, 2054, 2541, 3910, 7878],
       'healthcare': [3089, 4258, 4814, 6358, 8734]}
INFLATION_2021_TO_2026 = 1.18  # assumption, approximate CPI change
MORT30 = {1998: 6.94, 1999: 7.44, 2000: 8.05, 2001: 6.97, 2002: 6.54, 2003: 5.83, 2004: 5.84, 2005: 5.87, 2006: 6.41, 2007: 6.34,
          2008: 6.03, 2009: 5.04, 2010: 4.69, 2011: 4.45, 2012: 3.66, 2013: 3.98, 2014: 4.17, 2015: 3.85, 2016: 3.65, 2017: 3.99,
          2018: 4.54, 2019: 3.94, 2020: 3.11, 2021: 2.96, 2022: 5.34, 2023: 6.81, 2024: 6.72, 2025: 6.60, 2026: 6.80}
CD_NATIONAL = {3: 1.13, 6: 1.41, 12: 1.73, 18: 1.67, 24: 1.61, 36: 1.36, 60: 1.38}  # FDIC national avg rate, Sep 2026
CD_TERMS = [(6, .12), (12, .42), (18, .06), (24, .14), (36, .10), (60, .16)]
IRA_LIMIT = {2024: (7000, 1000), 2025: (7000, 1000), 2026: (7500, 1100)}
FUNDS = [('VOO', 'Vanguard S&P 500 ETF', 'eq'), ('VTI', 'Vanguard Total Stock Market ETF', 'eq'),
         ('QQQ', 'Invesco QQQ Trust', 'eq'), ('VXUS', 'Vanguard Total International Stock ETF', 'eq'),
         ('BND', 'Vanguard Total Bond Market ETF', 'bond')]

MERCH = {
    'groceries': ['Giant Eagle', 'Aldi', 'Walmart Supercenter', 'Trader Joe\'s', 'Costco Wholesale', 'Whole Foods Market', 'Kroger', 'Target'],
    'dining': ['Eat\'n Park', 'Chipotle', 'Starbucks', 'Panera Bread', 'Primanti Bros', 'McDonald\'s', 'DoorDash', 'Chick-fil-A', 'Local Pizzeria'],
    'transport': ['Sheetz', 'GetGo', 'Shell', 'Sunoco', 'Uber', 'Lyft', 'Pittsburgh Parking Authority', 'Jiffy Lube', 'Pittsburgh Regional Transit'],
    'shopping': ['Amazon', 'Target', 'Walmart', 'Best Buy', 'Old Navy', 'Home Depot', 'TJ Maxx', 'IKEA', 'Etsy'],
    'entertainment': ['AMC Theatres', 'Steam', 'Ticketmaster', 'Dave & Buster\'s', 'Bowling Alley', 'Apple App Store', 'Kennywood'],
    'healthcare': ['UPMC', 'CVS Pharmacy', 'Walgreens', 'Quest Diagnostics', 'Allegheny Health Network', 'Vision Center'],
    'other_spending': ['USPS', 'Pet Supplies Plus', 'Great Clips', 'Venmo', 'Goodwill', 'Dry Cleaner', 'Charity Donation'],
}
UTILS = [('Duquesne Light', .38), ('Peoples Natural Gas', .17), ('Pittsburgh Water', .10), ('Comcast Xfinity', .20), ('Verizon Wireless', .15)]
INSURERS = ['GEICO', 'State Farm', 'Progressive', 'Allstate', 'UPMC Health Plan']
SUBS = [('Netflix', 15.49), ('Spotify', 11.99), ('Amazon Prime', 14.99), ('YouTube Premium', 13.99), ('Hulu', 17.99),
        ('Apple iCloud', 2.99), ('Disney+', 13.99), ('New York Times', 17.00), ('Planet Fitness', 24.99)]
EMPLOYERS = ['UPMC', 'PNC Financial', 'Highmark', 'Giant Eagle Inc', 'Carnegie Mellon University', 'US Steel', 'Alcoa', 'Duolingo',
             'Allegheny County', 'Kraft Heinz', 'Google Pittsburgh', 'Wabtec', 'Pittsburgh Public Schools', 'Heinz Healthcare', 'Local Small Business LLC']
SEASON = {'shopping': {12: 1.35, 11: 1.15, 1: .85}, 'groceries': {11: 1.08, 12: 1.10}, 'utilities': {12: 1.15, 1: 1.18, 2: 1.15, 7: 1.12, 8: 1.12},
          'entertainment': {12: 1.15, 7: 1.10}, 'transport': {7: 1.10, 8: 1.08}}

class Acct:
    def __init__(s, cid, typ):
        s.cid, s.type, s.id, s.bal, s.rows = cid, typ, f"A-{cid}-{typ.upper()}", 0, []
        s.opening = 0; s.pmt = 0; s.apr = 0.0; s.yld = 0.0; s.limit = 0
    def post(s, d, ttype, cat, desc, change, flow, xid=''):
        if change == 0: return          # never write zero-value rows
        s.bal += change
        s.rows.append((d, ttype, cat, desc, abs(change), change, s.bal, flow, xid))

def amortize(P, apr, n):
    r = apr / 1200
    return int(round(P * r / (1 - (1 + r) ** -n))) if r else int(round(P / n))

def split_cents(r, total, n, floor=100):
    if n <= 0 or total <= 0: return []
    n = max(1, min(n, total // floor)) if total >= floor else 1
    w = [r.uniform(.4, 1.6) for _ in range(n)]
    parts = [max(floor, int(total * x / sum(w))) for x in w[:-1]]
    parts.append(max(1, total - sum(parts)))
    return parts

def income_band(g):
    for hi, name in [(35e3, '<35k'), (50e3, '35-50k'), (75e3, '50-75k'), (100e3, '75-100k'), (150e3, '100-150k'),
                     (200e3, '150-200k'), (300e3, '200-300k'), (450e3, '300-450k')]:
        if g < hi: return name
    return '450k+'

def tier(score):
    return 'poor' if score < 580 else 'fair' if score < 670 else 'good' if score < 740 else 'very_good' if score < 800 else 'exceptional'

# ------------------------------------------------------------------ market (shared by all IRAs; simulated, not historical)
def market_returns(seed):
    r = random.Random(f"{seed}-market")
    out = []
    for _ in MONTHS:
        f = r.gauss(0.007, 0.042)                     # common US equity factor
        out.append({'VOO': f + r.gauss(0, .004), 'VTI': f + r.gauss(0, .005),
                    'QQQ': 1.25 * f + r.gauss(.002, .012), 'VXUS': .85 * f + r.gauss(0, .02),
                    'BND': r.gauss(.003, .014)})
    return out

# ------------------------------------------------------------------ one customer
def build_customer(idx, args, mkt, cd_share):
    cid = f"C{idx:03d}"
    r = random.Random(f"{args.seed}-{cid}")

    # --- demographics & income
    lo, hi = wchoice(r, AGE_BANDS)
    age = r.randint(lo, hi)
    p_ret = 0 if age < 62 else .25 if age < 67 else .70 if age < 75 else .90
    retired = r.random() < p_ret
    age_f = .85 if age < 35 else 1.10 if age < 45 else 1.20 if age < 55 else 1.10 if age < 65 else .80 if age < 75 else .65
    gross = clip(lognorm(r, 42000, .55), 18000, 220000) if retired else clip(lognorm(r, 78000 * age_f, .85), 24000, 450000)
    gross = round(gross / 100) * 100
    net_ratio = min(.95, interp(gross, NET_PTS) + (.08 if retired else 0))
    net_m = int(round(gross * net_ratio / 12 * 100))                       # cents, average monthly take-home
    gm = gross / 12
    score = int(clip(interp(gross, SCORE_PTS) + 8 + .9 * clip(age - 45, -25, 25) + r.gauss(0, 82), 400, 850))

    # --- housing
    base_own = .39 if age < 35 else .62 if age < 45 else .70 if age < 55 else .76 if age < 65 else .79
    own_f = .65 if gross < 35e3 else .9 if gross < 60e3 else 1.05 if gross < 100e3 else 1.15
    owner = r.random() < min(.95, base_own * own_f)
    p_mort = .90 if age < 35 else .88 if age < 45 else .75 if age < 55 else .60 if age < 65 else .38 if age < 75 else .18
    has_mort = owner and r.random() < p_mort
    housing = 'owner_with_mortgage' if has_mort else 'owner_outright' if owner else 'renter'

    # --- product ownership
    p_sav = .55 if gross < 40e3 else .75 if gross < 80e3 else .88 if gross < 150e3 else .94
    has_sav = r.random() < p_sav
    has_card = r.random() < (.86 if score >= 620 else .55)
    rv = next(p for t_, p in REVOLVE_BY_INCOME if gross < t_)
    rv = rv + .20 if score < 620 else rv * .6 if score >= 760 else rv
    revolver = has_card and r.random() < clip(rv, .05, .85)
    a_base = .38 if age < 35 else .45 if age < 45 else .42 if age < 55 else .33 if age < 65 else .22 if age < 75 else .10
    a_f = .65 if gross < 30e3 else .9 if gross < 50e3 else .7 if gross >= 250e3 else 1.0
    has_auto = r.random() < a_base * a_f * (.5 if retired else 1)
    has_pers = 25 <= age <= 70 and r.random() < .10 * (1.5 if score < 680 else .5 if score >= 760 else 1)
    p_ira = .44 * (gross / 72000) ** .25 * (.6 if age < 30 else 1.0 if age < 45 else 1.15) * (1.2 if retired else 1)
    has_ira = r.random() < clip(p_ira, .12, .8)
    p_cd = cd_share * (1.5 if age >= 55 else .4 if age < 35 else 1.0) * (gross / 72000) ** .1
    has_cd = r.random() < clip(p_cd, .02, .5)

    accts = {'checking': Acct(cid, 'checking')}
    for t, flag in [('savings', has_sav), ('cd', has_cd), ('ira', has_ira), ('credit_card', has_card),
                    ('auto_loan', has_auto), ('personal_loan', has_pers), ('mortgage', has_mort)]:
        if flag: accts[t] = Acct(cid, t)
    chk = accts['checking']; sav = accts.get('savings')
    terms = {}   # account_id -> dict for account_terms.csv
    open_date_hist = lambda yrs: add_months(START, -int(yrs * 12) - r.randint(0, 11))

    # --- liquid balances
    med = next(m for a_, m in SCF_LIQUID_MEDIAN if age < a_)
    L = clip(lognorm(r, .6 * med * (gross / 72000) ** .55, 1.0), 400, 400000) * (1.8 if has_cd else 1.0)   # CD holders skew wealthier
    if sav: checking_open = max(L * r.uniform(.2, .45), net_m / 100 * r.uniform(.5, 1.2))
    else: checking_open = max(L, net_m / 100 * r.uniform(.5, 1.2))
    chk.bal = chk.opening = int(round(checking_open * 100))
    chk_yrs = clip(age - 18, 1, 35) * r.uniform(.2, .9)
    terms[chk.id] = dict(open_date=open_date_hist(chk_yrs), subtype='')
    if sav:
        sav.bal = sav.opening = int(round(max(L - chk.bal / 100, 50) * 100))
        hy = r.random() < .25
        sav.yld = round(r.uniform(3.4, 4.2), 2) if hy else round(clip(.38 * r.uniform(.2, 1.8), .01, .9), 2)
        terms[sav.id] = dict(open_date=open_date_hist(chk_yrs * r.uniform(.3, 1)), subtype='high_yield' if hy else 'standard', rate=sav.yld)

    # --- IRA
    contrib_m = 0; ira_type = ''
    if 'ira' in accts:
        ira = accts['ira']
        ira_med = [(35, 12000), (45, 30000), (55, 55000), (65, 90000), (99, 120000)]
        ira.bal = ira.opening = int(round(clip(lognorm(r, next(m for a_, m in ira_med if age < a_) * (gross / 72000) ** .4, 1.0), 500, 1.5e6) * 100))
        ira_type = 'roth' if r.random() < (.5 if gross < 153e3 else .25) else 'traditional'
        cap = min(b + (c if age >= 50 else 0) for b, c in IRA_LIMIT.values())  # never above any year's limit
        if not retired and r.random() < .30:
            annual = clip(lognorm(r, 3600, .55), 600, cap)
            contrib_m = int(annual / 12 // 5) * 500  # cents, multiples of $5
        w_eq = clip(clip((110 - age) / 100, .3, .9) + r.gauss(0, .04), .2, .95)
        g = [r.gammavariate(a_, 1) for a_ in (3.5, 3.0, 1.0, 2.5)]
        ira.weights = [w_eq * x / sum(g) for x in g] + [1 - w_eq]
        ira.yld = round(sum(w * (7.0 if k == 'eq' else 3.8) for w, (_, _, k) in zip(ira.weights, FUNDS)), 2)  # expected-return assumption
        terms[ira.id] = dict(open_date=open_date_hist(clip(age - 25, 1, 30) * r.uniform(.2, 1)), subtype=ira_type)

    # --- credit card
    card_pref = r.uniform(.35, .9)
    paydown = 0.0
    if 'credit_card' in accts:
        cc = accts['credit_card']
        cc.limit = int(round(clip(gross * r.uniform(.08, .25) * clip((score - 560) / 160 + .5, .5, 1.6), 1000, 40000) / 500) * 500) * 100
        cc.apr = round(interp(score, [(500, 29.5), (620, 26.5), (680, 23.5), (740, 20.5), (800, 18.5), (850, 17.5)]) + r.uniform(-.5, .5), 2)
        util = {'poor': .79, 'fair': .61, 'good': .39, 'very_good': .15, 'exceptional': .07}[tier(score)]
        cc.revolver = revolver
        cc.bal = cc.opening = int(clip(cc.limit * (clip(r.gauss(util * 1.2, .15), .1, .95) if revolver else r.uniform(.02, .12)), 0, cc.limit * .90))
        paydown = r.uniform(-.01, .05) if revolver else 0
        cc.prev_post = cc.bal
        terms[cc.id] = dict(open_date=open_date_hist(clip(age - 20, 1, 25) * r.uniform(.2, .8)), subtype='revolving' if revolver else 'pay_in_full',
                            rate=cc.apr, limit=cc.limit)
    # --- installment loans (all originated before history_start)
    def make_loan(acct, P, apr, n, subtype, k0, label):
        acct.apr, acct.n = apr, n
        acct.pmt = amortize(P, apr, n)
        bal = P
        for _ in range(k0):
            bal = bal + int(round(bal * apr / 1200)) - acct.pmt
        acct.bal = acct.opening = max(bal, 0)
        od = add_months(START, -k0); od = date(od.year, od.month, r.randint(1, 28))
        terms[acct.id] = dict(open_date=od, subtype=subtype, amount=P, term=n, rate=apr, maturity=add_months(od, n),
                              paid_before=k0)
    net_dollars = net_m / 100
    if has_mort:
        n = 360 if r.random() < .85 else 180
        kmax = n - 30
        k0 = int(clip(r.expovariate(1 / (84 if n == 360 else 48)) + 3, 3, kmax))
        k0 = int(min(k0, max(3, (age - 24) * 12)))
        oy = add_months(START, -k0).year
        apr = round(MORT30.get(oy, 6.8) + (0 if n == 360 else -.65) + r.uniform(-.2, .2), 3)
        P = gross * r.uniform(1.6, 3.4)
        P = min(P, (gm * .30) / (apr / 1200 / (1 - (1 + apr / 1200) ** -n)))
        P = int(round(max(P, 60000) / 1000) * 1000) * 100
        make_loan(accts['mortgage'], P, apr, n, f"fixed_{n // 12}yr", k0, 'mortgage')
        home_value = P / 100 / .8
    elif owner:
        home_value = gross * r.uniform(2, 4.5)
    home_value = clip(home_value if owner else 0, 0, 2.0e6)
    if has_auto:
        new = r.random() < .40
        n = wchoice(r, [(48, .15), (60, .30), (72, .35), (84, .20)])
        avg = 43925 if new else 27070
        P = clip(lognorm(r, avg * (gross / 62000) ** .4, .35), 6000, 90000)
        apr = round(interp(score, [(500, 17.0), (580, 13.5), (660, 9.5), (720, 6.9), (780, 5.5), (850, 5.0)]) + (0 if new else 1.5) + r.uniform(-.4, .4), 2)
        pm = amortize(int(P * 100), apr, n)
        if pm > net_m * .18: P = P * net_m * .18 / pm
        P = int(round(P / 100) * 100) * 100
        make_loan(accts['auto_loan'], P, apr, n, 'new' if new else 'used', r.randint(1, n - 8), 'auto')
    if has_pers:
        n = wchoice(r, [(24, .25), (36, .35), (48, .15), (60, .25)])
        P = int(round(clip(lognorm(r, 8000, .7), 1000, 35000) / 100) * 100) * 100
        apr = round(interp(score, [(500, 28.0), (580, 24.0), (670, 15.0), (740, 11.0), (800, 9.0), (850, 8.5)]) + r.uniform(-.5, .5), 2)
        make_loan(accts['personal_loan'], P, apr, n, 'unsecured', r.randint(1, n - 6), 'personal')

    # --- CD (standard: one funding, fixed rate, monthly interest credited to the CD, auto-renews)
    cd_sched = []; cd_fund = None
    if has_cd:
        cd = accts['cd']
        term = wchoice(r, CD_TERMS)
        principal = int(round(clip(lognorm(r, 15000 * (gross / 72000) ** .3, .8), 1000, 250000) / 500) * 500) * 100
        promo = r.random() < .40
        nat = CD_NATIONAL[term]
        rate0 = round(r.uniform(3.5, 4.5), 2) if promo else round(nat * r.uniform(1.0, 1.4), 2)
        liquid_c = chk.bal + (sav.bal if sav else 0)
        if r.random() < .40:    # opened inside the history window; funded mostly from the customer's own cash
            principal = int(max(100000, min(principal, liquid_c * .9)) // 50000 * 50000) or 100000
            od = START + timedelta(days=r.randint(0, 700))
            od = next_bd(od)
            cd_fund = od
        else:                    # opened before the window
            od = add_months(START, -r.randint(1, 30)); od = date(od.year, od.month, r.randint(1, 28))
        s, rate = od, rate0
        while s <= END:
            e = add_months(s, term)
            cd_sched.append((s, e, rate)); s = e
            rate = round(nat * r.uniform(.95, 1.15), 2)   # renewal at "current" market rate
        def cd_rate(d):
            for s_, e_, rt in cd_sched:
                if s_ <= d < e_: return rt
            return cd_sched[-1][2]
        cd.rate_at = cd_rate
        cd.open = od
        bal = 0 if cd_fund else principal
        if not cd_fund:
            me = month_end(od)
            while me < START:
                f = ((me - od).days + 1) / me.day if me.month == od.month and me.year == od.year else 1.0
                bal += int(round(bal * cd_rate(me) / 1200 * f)); me = month_end(date(me.year + (me.month == 12), me.month % 12 + 1, 1))
        cd.bal = cd.opening = bal; cd.principal = principal
        cur = next(x for x in cd_sched if x[0] <= END < x[1])
        cd.yld = cur[2]
        pen = 90 if term <= 12 else 180 if term <= 36 else 365
        terms[cd.id] = dict(open_date=od, subtype='promotional' if promo else 'standard', amount=principal, term=term, rate=cd.yld,
                            maturity=cur[1], auto_renew='yes', penalty=pen)

    # --- spending model
    inc_pts = BLS_INC
    def bls(cat):
        v = BLS[cat]
        if gross <= inc_pts[-1]:
            x = interp(max(gross, inc_pts[0]), list(zip(inc_pts, v)))
        else:
            x = v[-1] * (gross / inc_pts[-1]) ** .35
        return x * INFLATION_2021_TO_2026 / 12 * 100      # cents / month
    pers = {k: math.exp(r.gauss(0, .18)) for k in ['groceries', 'dining', 'transport', 'shopping', 'entertainment', 'healthcare', 'other_spending', 'utilities', 'insurance']}
    util_c = bls('utilities') * pers['utilities']
    insur_c = bls('transportation') * .22 * pers['insurance']
    health_c = bls('healthcare') * pers['healthcare'] * .9
    var_c = {'groceries': bls('groceries') * pers['groceries'], 'dining': bls('dining') * pers['dining'],
             'transport': bls('transportation') * .45 * pers['transport'], 'shopping': bls('apparel') * 1.8 * pers['shopping'],
             'entertainment': bls('entertainment') * .8 * pers['entertainment']}
    var_c['other_spending'] = .10 * sum(var_c.values()) * pers['other_spending']
    subs = r.sample(SUBS, r.randint(0, 4))
    subs_c = sum(int(round(p * 100)) for _, p in subs)
    if housing == 'renter':
        housing_c = int(round(clip(gm * r.uniform(.20, .30), 700, 5500) / 25) * 2500)
        housing_desc = 'Rent - ' + r.choice(['Oak Hill Apartments', 'Penn Ave Properties', 'Riverfront Rentals', 'Squirrel Hill Property Mgmt', 'Local Landlord'])
    else:
        housing_c = int(round(home_value * .0125 / 12 * 100)); housing_desc = 'Property Tax & Homeowners Insurance'
    debt_pmt = sum(accts[t].pmt for t in ('mortgage', 'auto_loan', 'personal_loan') if t in accts)
    card_extra = 0
    if 'credit_card' in accts and revolver:
        cc = accts['credit_card']; card_extra = int(cc.bal * (cc.apr / 1200 + paydown))
    fixed = housing_c + subs_c + debt_pmt + card_extra + contrib_m
    s_target = clip(r.gauss(.03 + .12 * clip((math.log10(gross) - 4.4) / 1.25, 0, 1), .05), .02, .30)
    desired = sum(var_c.values()) + util_c + insur_c + health_c       # everything except housing/debt/subscriptions flexes together
    budget = net_m * (1 - s_target) - fixed
    scale = clip(budget / desired, .45, 1.25) if desired > 0 else 1
    var_c = {k: v * scale for k, v in var_c.items()}
    util_c, insur_c, health_c = util_c * scale, insur_c * scale, health_c * scale
    residual = net_m - fixed - sum(var_c.values()) - util_c - insur_c - health_c
    sav_frac = r.uniform(.4, .9)
    sav_plan = int(round(max(0, residual * sav_frac) / 5000)) * 5000 if sav else 0
    sav_day = r.choice([2, 5, 16, 28])
    card_share = {'groceries': card_pref * .7, 'dining': card_pref, 'transport': card_pref, 'shopping': card_pref,
                  'entertainment': card_pref, 'healthcare': card_pref * .5, 'other_spending': card_pref}

    # --- pay schedule
    pay_dates = []
    if retired:
        for y, m in MONTHS: pay_dates.append(due(y, m, 3, 'next'))
        pay_freq = 'monthly'; per = 1
    else:
        pay_freq = wchoice(r, [('semimonthly', .45), ('biweekly', .45), ('monthly', .10)])
        if pay_freq == 'semimonthly':
            for y, m in MONTHS: pay_dates += [due(y, m, 15, 'prev'), due(y, m, 31, 'prev')]
        elif pay_freq == 'monthly':
            day = r.choice([15, 31])
            for y, m in MONTHS: pay_dates.append(due(y, m, day, 'prev'))
        else:
            d0 = START + timedelta(days=(4 - START.weekday()) % 7 + 7 * r.randint(0, 1))
            while d0 <= END: pay_dates.append(d0); d0 += timedelta(days=14)
    pay_dates = sorted(x for x in set(pay_dates) if START <= x <= END)
    per_pay = None
    employer = 'Social Security / Pension' if retired else r.choice(EMPLOYERS)
    per_year = 12 if retired or pay_freq == 'monthly' else 24 if pay_freq == 'semimonthly' else 26
    pay_c = int(round(net_m * 12 / per_year))

    # ============ schedules
    bills = {}          # date -> list of (kind, payload)
    def sched(d, item):
        if START <= d <= END: bills.setdefault(d, []).append(item)
    for y, m in MONTHS:
        sched(due(y, m, 1, 'next'), ('housing', housing_c, housing_desc))
        for i, (name, share) in enumerate(UTILS):
            dday = 8 + (hash_i(cid, i) % 17)
            sched(due(y, m, dday, 'next'), ('utility', int(util_c * share * SEASON['utilities'].get(m, 1.0) * math.exp(r.gauss(0, .06))), name))
        sched(due(y, m, 3 + hash_i(cid, 9) % 20, 'next'), ('insurance', int(insur_c), r.choice(INSURERS) + ' Premium'))
        for j, (name, p) in enumerate(subs):
            sched(due(y, m, 4 + (hash_i(cid, 20 + j) % 22), 'next'), ('sub', int(round(p * 100)), name))
        if contrib_m and 'ira' in accts: sched(due(y, m, 5, 'next'), ('ira', contrib_m, ''))
        if sav and sav_plan: sched(due(y, m, sav_day, 'next'), ('save', int(sav_plan * math.exp(r.gauss(0, .08)) // 100) * 100, ''))
        for t in ('mortgage', 'auto_loan', 'personal_loan'):
            if t in accts: sched(due(y, m, 21, 'next'), ('loan_pay', t, ''))
        if 'credit_card' in accts: sched(due(y, m, 28, 'next'), ('card_pay', 0, ''))
        sched(due(y, m, 27, 'next'), ('sweep', 0, ''))
        # variable spending events for the month
        for cat, tot in var_c.items():
            amt = int(tot * SEASON.get(cat, {}).get(m, 1.0) * math.exp(r.gauss(0, .08)))
            n = {'groceries': r.randint(4, 9), 'dining': clip(round(amt / 2800), 1, 14), 'shopping': clip(round(amt / 6000), 1, 8),
                 'transport': clip(round(amt / 4500), 2, 10), 'entertainment': clip(round(amt / 4500), 1, 5),
                 'other_spending': r.randint(1, 3)}[cat]
            for part in split_cents(r, amt, int(n)):
                day = r.randint(1, calendar.monthrange(y, m)[1])
                sched(date(y, m, day), ('spend', part, cat))
        if health_c > 0:
            for part in split_cents(r, int(health_c * math.exp(r.gauss(0, .15))), r.randint(1, 2), 500):
                sched(date(y, m, r.randint(1, calendar.monthrange(y, m)[1])), ('spend', part, 'healthcare'))
    # ============ simulation
    stats = {'plug_in': 0, 'plug_out': 0}
    xc = {}
    def xid(key, kind):
        xc[(key, kind)] = xc.get((key, kind), -1) + 1
        return f"X-{cid}-{key}-{xc[(key, kind)]:02d}-{kind}"
    def ensure(d, need):
        """make sure checking can cover `need` cents: savings first, then an outside account."""
        short = need - chk.bal
        if short <= 0: return
        if sav and sav.bal > 0:
            pull = min(sav.bal, -(-short // 5000) * 5000)
            x = xid('savings', 'withdrawal')
            sav.post(d, 'withdrawal', 'savings_withdrawal', 'Transfer to Checking', -pull, 'internal_transfer', x)
            chk.post(d, 'deposit', 'savings_withdrawal', 'Transfer from Savings', pull, 'internal_transfer', x)
            short = need - chk.bal
        if short > 0:
            amt = -(-short // 10000) * 10000
            chk.post(d, 'deposit', 'external_cash_management', 'Transfer from external account', amt, 'external_transfer')
            stats['plug_in'] += 1
    def chk_out(d, ttype, cat, desc, amt, flow, xi=''):
        ensure(d, amt)
        chk.post(d, ttype, cat, desc, -amt, flow, xi)

    pay_set = set(pay_dates)
    day = START
    while day <= END:
        items = bills.get(day, [])
        # 0 income
        if day in pay_set:
            if retired: chk.post(day, 'deposit', 'retirement_income', 'Social Security / Pension Deposit', pay_c, 'income')
            else: chk.post(day, 'paycheck', 'payroll', f'Payroll - {employer}', pay_c, 'income')
        # 1 interest on liabilities (20th loans, 26th card)
        for t in ('mortgage', 'auto_loan', 'personal_loan'):
            a = accts.get(t)
            if a and day.day == 20 and a.bal > 0:
                a.post(day, 'interest_accrual', 'debt_interest', 'Interest accrual', int(round(a.bal * a.apr / 1200)), 'non_cash')
        cc = accts.get('credit_card')
        if cc and day.day == 26:
            if cc.revolver and cc.bal > 0:
                cc.post(day, 'interest_accrual', 'debt_interest', 'Interest accrual', int(round(cc.bal * cc.apr / 1200)), 'non_cash')
                if score < 640 and r.random() < .04:
                    cc.post(day, 'fee', 'card_fee', 'Late payment fee', 3000, 'non_cash')
        # 2-5 scheduled items in a fixed priority
        order = {'housing': 2, 'utility': 2, 'insurance': 2, 'sub': 2, 'loan_pay': 3, 'card_pay': 3, 'save': 4, 'ira': 4, 'sweep': 7, 'spend': 5}
        for kind, amt, extra in sorted(items, key=lambda it: order[it[0]]):
            if kind in ('housing', 'utility', 'insurance'):
                if amt > 0:
                    cat = {'housing': 'housing', 'utility': 'utilities', 'insurance': 'insurance'}[kind]
                    chk_out(day, 'payment', cat, extra, amt, 'expense')
            elif kind == 'sub':
                cat, merch = 'subscriptions', extra
                if cc and r.random() < card_pref and cc.bal + amt <= cc.limit * .93:
                    cc.post(day, 'purchase', cat, merch, amt, 'expense')
                else:
                    chk_out(day, 'payment', cat, merch, amt, 'expense')
            elif kind == 'loan_pay':
                a = accts[amt]       # payload is (kind, account_type, '')
                if a.bal > 0:
                    pay = min(a.pmt, a.bal)
                    x = xid(a.type, 'payment')
                    label = {'mortgage': 'Mortgage', 'auto_loan': 'Auto Loan', 'personal_loan': 'Personal Loan'}[a.type]
                    chk_out(day, 'payment', 'debt_payment', f'PNC {label} Payment', pay, 'internal_transfer', x)
                    a.post(day, 'payment', 'debt_payment', 'Payment received', -pay, 'internal_transfer', x)
            elif kind == 'card_pay':
                if cc and cc.bal > 0:
                    if cc.revolver:
                        minimum = min(cc.bal, max(3500, int(cc.bal * .02)))
                        pay = int(clip(cc.bal - cc.prev_post * (1 - paydown), minimum, cc.bal))
                    else:
                        pay = cc.bal
                    x = xid('credit_card', 'payment')
                    chk_out(day, 'payment', 'debt_payment', 'PNC Credit Card Payment', pay, 'internal_transfer', x)
                    cc.post(day, 'payment', 'debt_payment', 'Payment received', -pay, 'internal_transfer', x)
                    cc.prev_post = cc.bal
            elif kind == 'save':
                if sav and amt > 0 and chk.bal - amt >= net_m // 4:      # only save what is actually spare
                    x = xid('savings', 'deposit')
                    chk.post(day, 'transfer_out', 'savings_contribution', 'Transfer to Savings', -amt, 'internal_transfer', x)
                    sav.post(day, 'deposit', 'savings_contribution', 'Transfer from Checking', amt, 'internal_transfer', x)
            elif kind == 'ira':
                x = xid('ira', 'deposit')
                chk_out(day, 'transfer_out', 'investment_contribution', 'Transfer to IRA', amt, 'internal_transfer', x)
                accts['ira'].post(day, 'deposit', 'investment_contribution', 'IRA contribution', amt, 'internal_transfer', x)
            elif kind == 'sweep':
                if chk.bal > net_m * 3.5:
                    ex = int((chk.bal - net_m * 2.5) // 10000 * 10000)
                    if ex > 0:
                        if sav:
                            x = xid('savings', 'deposit')
                            chk.post(day, 'transfer_out', 'savings_contribution', 'Transfer to Savings', -ex, 'internal_transfer', x)
                            sav.post(day, 'deposit', 'savings_contribution', 'Transfer from Checking', ex, 'internal_transfer', x)
                        else:
                            chk.post(day, 'withdrawal', 'external_cash_management', 'Transfer to external account', -ex, 'external_transfer')
                            stats['plug_out'] += 1
            elif kind == 'spend':
                cat = extra
                merch = r.choice(MERCH[cat])
                if cc and r.random() < card_share[cat] and cc.bal + amt <= cc.limit * .93:
                    cc.post(day, 'purchase', cat, merch, amt, 'expense')
                else:
                    chk_out(day, 'withdrawal', cat, merch + (' (debit)' if r.random() < .5 else ''), amt, 'expense')
        # CD funding (in-window opening)
        if cd_fund and day == cd_fund:
            cd = accts['cd']
            x = xid('cd', 'deposit')
            chk_out(day, 'transfer_out', 'investment_contribution', 'Transfer to CD', cd.principal, 'internal_transfer', x)
            cd.post(day, 'deposit', 'investment_contribution', 'CD opening deposit', cd.principal, 'internal_transfer', x)
        # 9 month-end: interest on savings & CD, market return on IRA
        if day == month_end(day):
            mi = MONTHS.index((day.year, day.month))
            if sav and sav.bal > 0:
                sav.post(day, 'interest_accrual', 'asset_interest', 'Interest accrual', int(round(sav.bal * sav.yld / 1200)), 'non_cash')
            if 'cd' in accts and accts['cd'].bal > 0:
                cd = accts['cd']
                f = 1.0
                if cd_fund and cd_fund.year == day.year and cd_fund.month == day.month: f = ((day - cd_fund).days + 1) / day.day
                cd.post(day, 'interest_accrual', 'asset_interest', 'Interest accrual', int(round(cd.bal * cd.rate_at(day) / 1200 * f)), 'non_cash')
            if 'ira' in accts and accts['ira'].bal > 0:
                ira = accts['ira']
                ret = sum(w * mkt[mi][t] for w, (t, _, _) in zip(ira.weights, FUNDS))
                chg = int(round(ira.bal * ret))
                ira.post(day, 'market_return', 'investment_return', 'Market gain' if chg >= 0 else 'Market loss', chg, 'non_cash')
        day += timedelta(days=1)

    # --- terms & holdings
    for t, a in accts.items():
        a.balance_final = a.bal
    holdings = []
    if 'ira' in accts:
        ira = accts['ira']
        for w, (tk, nm, _) in zip(ira.weights, FUNDS):
            holdings.append(dict(customer_id=cid, account_id=ira.id, as_of_date=END.isoformat(), fund_ticker=tk, fund_name=nm,
                                 weight_pct=round(w * 100, 2), market_value=money(int(round(ira.bal * w)))))
    cust = dict(customer_id=cid, age=age, employment_status='retired' if retired else 'employed', housing_status=housing,
                annual_gross_income=int(gross), income_band=income_band(gross), credit_score=score, credit_tier=tier(score),
                pay_frequency=pay_freq, has_savings=int(bool(sav)), has_cd=int(has_cd), has_ira=int(has_ira), has_credit_card=int(has_card),
                carries_card_balance=int(revolver), has_auto_loan=int(has_auto), has_personal_loan=int(has_pers), has_mortgage=int(has_mort),
                ira_type=ira_type, overdraft_plug_deposits=stats['plug_in'], overdraft_plug_withdrawals=stats['plug_out'])
    return cust, accts, terms, holdings, dict(monthly_income_c=net_m)

def hash_i(cid, i):
    return (sum(ord(ch) * (k + 3) for k, ch in enumerate(cid)) * 31 + i * 17) % 1009

# ------------------------------------------------------------------ writers
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--out', default=os.path.join(ROOT, 'data')); ap.add_argument('--n', type=int, default=100)
    ap.add_argument('--seed', default='20261107')
    ap.add_argument('--cd-rate', type=float, default=.18, help='baseline share of customers with a CD (real US households ~5-8%%; boosted so every product appears)')
    args = ap.parse_args()
    os.makedirs(args.out, exist_ok=True)
    mkt = market_returns(args.seed)
    with contextlib.ExitStack() as stack:
        W = lambda name, cols: csv.DictWriter(stack.enter_context(open(os.path.join(args.out, name), 'w', newline='')), fieldnames=cols, lineterminator='\n')
        wp = W('profiles.csv', ['customer_id', 'monthly_income', 'monthly_non_debt_expenses', 'credit_limit']); wp.writeheader()
        wa = W('accounts.csv', ['customer_id', 'account_type', 'balance', 'monthly_payment', 'apr_pct', 'account_id', 'opening_balance', 'history_start', 'as_of_date', 'annual_yield_pct']); wa.writeheader()
        wt = W('transactions.csv', ['transaction_id', 'customer_id', 'account_id', 'account_type', 'posted_date', 'transaction_type', 'category', 'description', 'amount', 'balance_change', 'balance_after', 'flow_class', 'transfer_id']); wt.writeheader()
        wm = W('monthly_statements.csv', ['customer_id', 'account_id', 'account_type', 'month', 'opening_balance', 'balance_increases', 'balance_decreases', 'closing_balance', 'transaction_count']); wm.writeheader()
        wc = None; wk = W('account_terms.csv', ['account_id', 'customer_id', 'account_type', 'subtype', 'open_date', 'original_amount', 'term_months', 'interest_rate_pct', 'maturity_date', 'payments_made_before_history', 'auto_renew', 'early_withdrawal_penalty_days', 'credit_limit']); wk.writeheader()
        wh = W('ira_holdings.csv', ['customer_id', 'account_id', 'as_of_date', 'fund_ticker', 'fund_name', 'weight_pct', 'market_value']); wh.writeheader()
        tid = 0
        for i in range(1, args.n + 1):
            cust, accts, terms, holdings, extra = build_customer(i, args, mkt, args.cd_rate)
            if wc is None:
                wc = W('customers.csv', list(cust.keys())); wc.writeheader()
            wc.writerow(cust)
            cid = cust['customer_id']
            inc = exp = 0
            for t in ACCT_ORDER:
                a = accts.get(t)
                if not a: continue
                monthly = {}
                for d, tt, cat, desc, amt, chg, bal, flow, x in a.rows:
                    tid += 1
                    wt.writerow(dict(transaction_id=f"T{tid:07d}", customer_id=cid, account_id=a.id, account_type=t, posted_date=d.isoformat(),
                                     transaction_type=tt, category=cat, description=desc, amount=money(amt), balance_change=money(chg),
                                     balance_after=money(bal), flow_class=flow, transfer_id=x))
                    m = monthly.setdefault(d.strftime('%Y-%m'), [0, 0, 0])
                    m[0 if chg > 0 else 1] += abs(chg) if chg else 0; m[2] += 1
                    if flow == 'income': inc += amt
                    if flow == 'expense': exp += amt
                bal = a.opening
                for y, mo in MONTHS:
                    key = f"{y}-{mo:02d}"
                    up, down, n = monthly.get(key, [0, 0, 0])
                    wm.writerow(dict(customer_id=cid, account_id=a.id, account_type=t, month=key, opening_balance=money(bal),
                                     balance_increases=money(up), balance_decreases=money(down), closing_balance=money(bal + up - down), transaction_count=n))
                    bal += up - down
                pmt = a.pmt
                if t == 'credit_card': pmt = min(a.bal, max(3500, int(a.bal * .02))) if a.bal > 0 else 0
                if t in LIABS and a.bal == 0 and t != 'credit_card': pmt = 0
                wa.writerow(dict(customer_id=cid, account_type=t, balance=money(a.bal), monthly_payment=money(pmt) if t in LIABS else 0,
                                 apr_pct=a.apr if t in LIABS else 0, account_id=a.id, opening_balance=money(a.opening),
                                 history_start=START.isoformat(), as_of_date=END.isoformat(), annual_yield_pct=a.yld if t in ('savings', 'cd', 'ira') else 0))
                tm = terms.get(a.id, {})
                wk.writerow(dict(account_id=a.id, customer_id=cid, account_type=t, subtype=tm.get('subtype', ''),
                                 open_date=(a.open if t == 'cd' else tm.get('open_date', '')).isoformat() if tm else '',
                                 original_amount=money(tm['amount']) if 'amount' in tm else '', term_months=tm.get('term', ''),
                                 interest_rate_pct=tm.get('rate', ''), maturity_date=tm['maturity'].isoformat() if 'maturity' in tm else '',
                                 payments_made_before_history=tm.get('paid_before', ''), auto_renew=tm.get('auto_renew', ''),
                                 early_withdrawal_penalty_days=tm.get('penalty', ''), credit_limit=money(tm['limit']) if 'limit' in tm else ''))
            cc = accts.get('credit_card')
            wp.writerow(dict(customer_id=cid, monthly_income=f"{inc / 100 / len(MONTHS):.2f}", monthly_non_debt_expenses=f"{exp / 100 / len(MONTHS):.2f}",
                             credit_limit=money(cc.limit) if cc else '0.00'))
            for h in holdings: wh.writerow(h)
    print(f"wrote {args.n} customers, {tid} transactions -> {args.out}")

if __name__ == '__main__':
    main()
