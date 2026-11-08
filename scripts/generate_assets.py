#!/usr/bin/env python3
"""
Generate estimated non-cash assets (data/assets.parquet) so homeowners and car owners aren't shown as pure debt.
These are ASSUMPTIONS, not ledger data; the ledger files are untouched. Label them "Estimated value" in the UI.

Columns: customer_id, home_value, home_value_at_start, vehicle_value, vehicle_value_at_start
  (as_of = 2026-10-31, start = history_start = 2024-11-01; null = does not own one)

  home      mortgage holders: original_amount / 0.8 (the generator sized loans at 80% LTV), +3%/yr from open_date.
            owners without a mortgage: 2-4.5x gross income, same rule the generator uses for home value (capped at $2M).
            renters: none. The value at start is the as_of value deflated at 3%/yr.
  vehicle   auto-loan holders: original_amount, -20% in year 1, then -12%/yr, from open_date.
            everyone else: 55% own a car outright, worth $3k-$20k, aged 2-10 years (assumption).
Deterministic: random.Random(f"{seed}-{customer_id}-assets").
Run:  python3 scripts/generate_assets.py            (reads/writes <repo>/data; run after to_parquet.py)
"""
import argparse, os, random
from datetime import date

import pyarrow as pa
import pyarrow.parquet as pq

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
START, AS_OF = date(2024, 11, 1), date(2026, 10, 31)
APPRECIATION = 1.03
P_OWNS_CAR_OUTRIGHT = .55


def years(a, b): return (b - a).days / 365.25
def vehicle_factor(age): return 1 - .2 * age if age < 1 else .8 * .88 ** (age - 1)


def assets_for(cust, mortgage, auto, r):
    home = home0 = veh = veh0 = None

    if mortgage:
        base = mortgage['original_amount'] / .8
        home, home0 = (base * APPRECIATION ** years(mortgage['open_date'], d) for d in (AS_OF, START))
    elif cust['housing_status'] == 'owner_outright':
        home = min(cust['annual_gross_income'] * r.uniform(2, 4.5), 2.0e6)
        home0 = home / APPRECIATION ** years(START, AS_OF)

    if auto:
        veh, veh0 = (auto['original_amount'] * vehicle_factor(years(auto['open_date'], d)) for d in (AS_OF, START))
    elif r.random() < P_OWNS_CAR_OUTRIGHT:
        age = r.uniform(2, 10)
        veh = r.uniform(3000, 20000)
        veh0 = veh * vehicle_factor(age - years(START, AS_OF)) / vehicle_factor(age)

    rnd = lambda v: None if v is None else round(v, 2)
    return dict(customer_id=cust['customer_id'], home_value=rnd(home), home_value_at_start=rnd(home0),
                vehicle_value=rnd(veh), vehicle_value_at_start=rnd(veh0))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--data-dir', default=os.path.join(ROOT, 'data'))
    ap.add_argument('--seed', default='20261107')
    args = ap.parse_args()
    read = lambda n: pq.read_table(os.path.join(args.data_dir, f'{n}.parquet')).to_pylist()

    customers = sorted(read('customers'), key=lambda c: c['customer_id'])
    terms = {}
    for t in read('account_terms'):
        if t['account_type'] in ('mortgage', 'auto_loan'):
            terms[(t['customer_id'], t['account_type'])] = dict(original_amount=float(t['original_amount']), open_date=t['open_date'])

    rows = [assets_for(c, terms.get((c['customer_id'], 'mortgage')), terms.get((c['customer_id'], 'auto_loan')),
                       random.Random(f"{args.seed}-{c['customer_id']}-assets")) for c in customers]

    # validation
    by_id = {r['customer_id']: r for r in rows}
    assert set(by_id) == {c['customer_id'] for c in customers}, 'every customer needs an assets row'
    for c in customers:
        a, cid = by_id[c['customer_id']], c['customer_id']
        assert (a['home_value'] is None) == (c['housing_status'] == 'renter'), f'{cid}: home vs housing status'
        assert (cid, 'auto_loan') not in terms or a['vehicle_value'], f'{cid}: auto loan but no vehicle'
        for k in ('home_value', 'vehicle_value'):
            assert a[k] is None or a[k] > 0, f'{cid}: {k} not positive'

    f64 = pa.float64()
    schema = pa.schema([('customer_id', pa.string()), ('home_value', f64), ('home_value_at_start', f64),
                        ('vehicle_value', f64), ('vehicle_value_at_start', f64)])
    dst = os.path.join(args.data_dir, 'assets.parquet')
    pq.write_table(pa.Table.from_pylist(rows, schema=schema), dst, compression='zstd')
    print(f"assets: {len(rows)} rows -> {os.path.relpath(dst, ROOT)} | homes {sum(r['home_value'] is not None for r in rows)}"
          f" | vehicles {sum(r['vehicle_value'] is not None for r in rows)}")


if __name__ == '__main__':
    main()
