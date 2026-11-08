#!/usr/bin/env python3
"""Independent checks on the generated CSVs (reads only the files). Exit code 1 if any hard check fails."""
import sys, pandas as pd, numpy as np
D = sys.argv[1] if len(sys.argv) > 1 else 'data'
t = pd.read_csv(f'{D}/transactions.csv'); a = pd.read_csv(f'{D}/accounts.csv'); m = pd.read_csv(f'{D}/monthly_statements.csv')
p = pd.read_csv(f'{D}/profiles.csv'); c = pd.read_csv(f'{D}/customers.csv'); k = pd.read_csv(f'{D}/account_terms.csv')
fails = []
def check(name, ok, detail=''):
    print(('PASS ' if ok else 'FAIL ') + name + (f'  [{detail}]' if detail else ''))
    if not ok: fails.append(name)

cents = lambda s: (s * 100).round().astype('int64')
t['chg'] = cents(t.balance_change); t['amt'] = cents(t.amount); t['bal'] = cents(t.balance_after)
a['open'] = cents(a.opening_balance); a['bal'] = cents(a.balance)

# 1 ledger identity per account
g = t.groupby('account_id').chg.sum()
x = a.set_index('account_id'); err = (x.open + g.reindex(x.index).fillna(0) - x.bal).abs().max()
check('opening + sum(balance_change) == balance (every account)', err == 0, f'max err {err} cents')
# 2 running balance continuity
t['prev'] = t.groupby('account_id').bal.shift().fillna(t.account_id.map(x.open)).astype('int64')
check('balance_after == previous balance + change (every row)', ((t.prev + t.chg) == t.bal).all())
# 3 amount unsigned == |change|
check('amount >= 0 and == |balance_change|', ((t.amt >= 0) & (t.amt == t.chg.abs())).all())
# 4 statements
m['o'] = cents(m.opening_balance); m['cl'] = cents(m.closing_balance); m['inc'] = cents(m.balance_increases); m['dec'] = cents(m.balance_decreases)
check('statement identity open + inc - dec == close', ((m.o + m.inc - m.dec) == m.cl).all())
m = m.sort_values(['account_id', 'month']); chain = m.groupby('account_id').cl.shift()
check('statement chain: close(t-1) == open(t)', (chain.dropna() == m.o[chain.notna()]).all())
last = m.groupby('account_id').cl.last()
check('last statement close == accounts.balance', (last.reindex(x.index) == x.bal).all())
tc = t.assign(mo=t.posted_date.str[:7]).groupby(['account_id', 'mo']).size()
mm = m.set_index(['account_id', 'month']).transaction_count
check('statement transaction_count matches transactions', (tc.reindex(mm.index).fillna(0).astype(int) == mm).all())
# 5 ids
check('transaction_id unique', t.transaction_id.is_unique)
check('every account has 24 statement months', (m.groupby('account_id').size() == 24).all())
# 6 transfers
tr = t[t.transfer_id.notna()]
legs = tr.groupby('transfer_id').agg(n=('amt', 'size'), amts=('amt', 'nunique'), accts=('account_id', 'nunique'), dates=('posted_date', 'nunique'))
check('every transfer_id has exactly 2 legs, same amount, same date, 2 accounts', ((legs.n == 2) & (legs.amts == 1) & (legs.accts == 2) & (legs.dates == 1)).all(), f'{len(legs)} transfers')
# 7 sign rules
liab = ['credit_card', 'auto_loan', 'personal_loan', 'mortgage']
check('payments on liabilities reduce balance', (t[(t.account_type.isin(liab)) & (t.transaction_type == 'payment')].chg < 0).all())
check('interest/fees/purchases on liabilities increase balance', (t[(t.account_type.isin(liab)) & t.transaction_type.isin(['interest_accrual', 'fee', 'purchase'])].chg > 0).all())
check('no negative balances anywhere', (t.bal >= 0).all() and (x.bal >= 0).all())
# 8 card within limit
lim = p.set_index('customer_id').credit_limit
cc = t[t.account_type == 'credit_card']
over = cc[cc.bal > (cc.customer_id.map(lim) * 100)]
check('credit card balance never exceeds credit_limit', len(over) == 0, f'{len(over)} rows over')
# 9 loans never grow except by interest; loans never receive new principal
check('loans: only interest/payment rows', set(t[t.account_type.isin(['auto_loan', 'personal_loan', 'mortgage'])].transaction_type) <= {'interest_accrual', 'payment'})
# 10 CD: one funding at most, nothing else but interest
cdt = t[t.account_type == 'cd']
check('CD: only interest + a single opening deposit (no recurring deposits)', cdt[cdt.transaction_type == 'deposit'].groupby('account_id').size().max() <= 1 and set(cdt.transaction_type) <= {'interest_accrual', 'deposit'})
# 11 business-day posting
wd = pd.to_datetime(t.posted_date).dt.weekday >= 5
bd_types = t.transaction_type.isin(['paycheck', 'transfer_out']) | (t.description.str.contains('Payment|Rent|Premium|Property', regex=True) & (t.account_type == 'checking'))
check('payroll / ACH / bill payments never post on weekends', not (wd & bd_types).any(), f'{(wd & bd_types).sum()} weekend rows')
print(f'INFO  weekend share of all rows: {wd.mean():.1%} (card purchases/interest legitimately post on weekends)')
# 12 date range
check('all dates inside 2024-11-01..2026-10-31', t.posted_date.between('2024-11-01', '2026-10-31').all())
# 13 profiles
check('profiles: one row per customer, income > 0', p.customer_id.is_unique and (p.monthly_income > 0).all())

print('\n--- population report -----------------------------------------------------')
n = len(c); print(f'customers {n}, accounts {len(a)}, transactions {len(t)}')
print('ownership %:', (a.groupby('account_type').size() / n * 100).round(0).to_dict())
print('age median', c.age.median(), '| income median', int(c.annual_gross_income.median()), '| mean score', round(c.credit_score.mean()), '| tiers', (c.credit_tier.value_counts(normalize=True) * 100).round(0).to_dict())
print('housing', (c.housing_status.value_counts(normalize=True) * 100).round(0).to_dict(), '| retired', round((c.employment_status == 'retired').mean() * 100), '%')
print('card revolvers among card holders: %.0f%%' % (c.carries_card_balance.sum() / max(c.has_credit_card.sum(), 1) * 100))
cust = c.set_index('customer_id'); pp = p.set_index('customer_id')
sur = (pp.monthly_income - pp.monthly_non_debt_expenses)
print('non-debt expense / income: median %.2f, p10 %.2f, p90 %.2f | negative surplus before debt: %d' % ((pp.monthly_non_debt_expenses / pp.monthly_income).median(), (pp.monthly_non_debt_expenses / pp.monthly_income).quantile(.1), (pp.monthly_non_debt_expenses / pp.monthly_income).quantile(.9), (sur < 0).sum()))
print('overdraft plug deposits per customer: mean %.1f, customers with any: %d, with >6: %d' % (c.overdraft_plug_deposits.mean(), (c.overdraft_plug_deposits > 0).sum(), (c.overdraft_plug_deposits > 6).sum()))
for ty in ['checking', 'savings', 'cd', 'ira', 'credit_card', 'auto_loan', 'personal_loan', 'mortgage']:
    s = a[a.account_type == ty]
    if len(s): print(f'{ty:14s} n={len(s):3d} balance median {s.balance.median():>10,.0f}  p10 {s.balance.quantile(.1):>9,.0f}  p90 {s.balance.quantile(.9):>10,.0f}', '| payment med %.0f' % s.monthly_payment.median() if ty in liab else '')
print('card util median (cards with balance): %.2f' % ((a[a.account_type == "credit_card"].set_index("customer_id").balance / pp.credit_limit).dropna().median()))
print('transaction types:', t.transaction_type.value_counts().to_dict())
print('tx per customer: mean %.0f' % (len(t) / n))
print('\nFAILED: ' + ', '.join(fails) if fails else '\nALL HARD CHECKS PASSED')
sys.exit(1 if fails else 0)
