#!/usr/bin/env python3
"""
Generate the bio side file (data/bio.parquet) from data/customers.parquet.

Columns: customer_id, state, dependents (list of kid ages), has_401k, employer_match_pct (decimal of salary), target_age.
Deterministic: each customer has its own RNG, random.Random(f"{seed}-{customer_id}-bio"), same pattern as the generator.

Rules tied to existing attributes
  * state       ~80% PA, rest split OH / WV / NY (merchants are Pittsburgh-based; a few other states exercise 529 tax-deduction logic).
  * dependents  P(has kids) by age: <30 15%, 30-44 55%, 45-54 45%, 55+ 15% (adult / college-age kids); owners likelier than renters.
                1-3 kids, each 0-22 and at least 20 years younger than the parent.
  * 401(k)      retirees none; employed: 45% under $35k rising linearly to 85% at $100k+.
                Match 0.03-0.06 of salary (e.g. "50% up to 6%" = 0.03); ~15% of employers give none.
  * target_age  employed 62-70 (mostly 65-67); retirees = current age.
Run:  python3 scripts/generate_bio.py            (reads/writes <repo>/data)
"""
import argparse, os, random

import pyarrow as pa
import pyarrow.parquet as pq

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

MIN_PARENT_GAP = 20      # generation rule; validation only requires 18
MAX_KID_AGE = 22
STATES = [('PA', .80), ('OH', .08), ('WV', .06), ('NY', .06)]
KID_COUNT = [(1, .50), (2, .35), (3, .15)]
TARGET_AGE = [(62, .05), (63, .05), (64, .07), (65, .25), (66, .15), (67, .25), (68, .06), (69, .06), (70, .06)]


def wchoice(r, items):
    t = r.random() * sum(w for _, w in items)
    for v, w in items:
        t -= w
        if t <= 0: return v
    return items[-1][0]


def p_kids(age, housing):
    p = .15 if age < 30 else .55 if age < 45 else .45 if age < 55 else .15
    return min(.95, p * (.85 if housing == 'renter' else 1.2))


def p_401k(gross):
    if gross <= 35e3: return .45
    if gross >= 100e3: return .85
    return .45 + (gross - 35e3) / (100e3 - 35e3) * (.85 - .45)


def bio_for(row, seed):
    r = random.Random(f"{seed}-{row['customer_id']}-bio")
    age, retired = row['age'], row['employment_status'] == 'retired'

    state = wchoice(r, STATES)

    kids = []
    oldest = min(MAX_KID_AGE, age - MIN_PARENT_GAP)
    if oldest >= 0 and r.random() < p_kids(age, row['housing_status']):
        lo = 12 if age >= 55 else 0                      # older parents: kids are teens / college-age
        lo = min(lo, oldest)
        kids = sorted((r.randint(lo, oldest) for _ in range(wchoice(r, KID_COUNT))), reverse=True)

    has_401k = (not retired) and r.random() < p_401k(row['annual_gross_income'])
    match = 0.0
    if has_401k and r.random() >= .15:
        match = round(r.uniform(.03, .06) * 200) / 200   # steps of 0.005

    target = age if retired else wchoice(r, TARGET_AGE)

    return dict(customer_id=row['customer_id'], state=state, dependents=kids, has_401k=has_401k,
                employer_match_pct=match, target_age=target)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--data-dir', default=os.path.join(ROOT, 'data'))
    ap.add_argument('--seed', default='20261107')
    args = ap.parse_args()

    customers = pq.read_table(os.path.join(args.data_dir, 'customers.parquet')).to_pylist()
    customers.sort(key=lambda c: c['customer_id'])
    rows = [bio_for(c, args.seed) for c in customers]

    # validation
    assert {b['customer_id'] for b in rows} == {c['customer_id'] for c in customers}, 'every customer needs a bio row'
    for c, b in zip(customers, rows):
        assert all(c['age'] - k >= 20 for k in b['dependents']), f"{c['customer_id']}: kid too close in age to parent"
        assert all(0 <= k <= MAX_KID_AGE for k in b['dependents'])
        if c['employment_status'] == 'retired': assert not b['has_401k'], f"{c['customer_id']}: retiree has a 401k"
        if not b['has_401k']: assert b['employer_match_pct'] == 0.0

    schema = pa.schema([('customer_id', pa.string()), ('state', pa.string()), ('dependents', pa.list_(pa.int16())),
                        ('has_401k', pa.bool_()), ('employer_match_pct', pa.float64()), ('target_age', pa.int16())])
    dst = os.path.join(args.data_dir, 'bio.parquet')
    pq.write_table(pa.Table.from_pylist(rows, schema=schema), dst, compression='zstd')

    n = len(rows)
    with_kids = sum(bool(b['dependents']) for b in rows)
    teens = sum(any(10 <= k <= 17 for k in b['dependents']) for b in rows)
    print(f"bio: {n} rows -> {os.path.relpath(dst, ROOT)} | PA {sum(b['state'] == 'PA' for b in rows)} | "
          f"kids {with_kids} (kids 10-17: {teens}) | 401k {sum(b['has_401k'] for b in rows)}")


if __name__ == '__main__':
    main()
