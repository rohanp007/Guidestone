#!/usr/bin/env python3
"""
Convert the generator's CSVs in data/ to typed Parquet (data/*.parquet).

Every column has an explicit type, so nothing is inferred:
  * IDs are strings, dates are date32, money is decimal(14, 2) (exact, no float drift), rates are float64.
  * Empty CSV cells become nulls.
  * monthly_statements.month ('YYYY-MM') becomes the first day of that month, as a date.
A CSV whose header doesn't match its schema fails loudly instead of being guessed at.

Run:  python3 scripts/to_parquet.py            (reads/writes <repo>/data)
"""
import argparse, csv, os, sys

import pyarrow as pa
import pyarrow.compute as pc
import pyarrow.csv as pacsv
import pyarrow.parquet as pq

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

S, D, F = pa.string(), pa.date32(), pa.float64()
M = pa.decimal128(14, 2)
I8, I16, I32, I64 = pa.int8(), pa.int16(), pa.int32(), pa.int64()

SCHEMAS = {
    'profiles': {'customer_id': S, 'monthly_income': M, 'monthly_non_debt_expenses': M, 'credit_limit': M},
    'accounts': {'customer_id': S, 'account_type': S, 'balance': M, 'monthly_payment': M, 'apr_pct': F, 'account_id': S,
                 'opening_balance': M, 'history_start': D, 'as_of_date': D, 'annual_yield_pct': F},
    'transactions': {'transaction_id': S, 'customer_id': S, 'account_id': S, 'account_type': S, 'posted_date': D,
                     'transaction_type': S, 'category': S, 'description': S, 'amount': M, 'balance_change': M,
                     'balance_after': M, 'flow_class': S, 'transfer_id': S},
    'monthly_statements': {'customer_id': S, 'account_id': S, 'account_type': S, 'month': D, 'opening_balance': M,
                           'balance_increases': M, 'balance_decreases': M, 'closing_balance': M, 'transaction_count': I32},
    'customers': {'customer_id': S, 'age': I16, 'employment_status': S, 'housing_status': S, 'annual_gross_income': I64,
                  'income_band': S, 'credit_score': I16, 'credit_tier': S, 'pay_frequency': S, 'has_savings': I8,
                  'has_cd': I8, 'has_ira': I8, 'has_credit_card': I8, 'carries_card_balance': I8, 'has_auto_loan': I8,
                  'has_personal_loan': I8, 'has_mortgage': I8, 'ira_type': S, 'overdraft_plug_deposits': I32,
                  'overdraft_plug_withdrawals': I32},
    'account_terms': {'account_id': S, 'customer_id': S, 'account_type': S, 'subtype': S, 'open_date': D,
                      'original_amount': M, 'term_months': I32, 'interest_rate_pct': F, 'maturity_date': D,
                      'payments_made_before_history': I32, 'auto_renew': S, 'early_withdrawal_penalty_days': I32,
                      'credit_limit': M},
    'ira_holdings': {'customer_id': S, 'account_id': S, 'as_of_date': D, 'fund_ticker': S, 'fund_name': S,
                     'weight_pct': F, 'market_value': M},
}


def convert(name, schema, data_dir):
    src, dst = os.path.join(data_dir, f'{name}.csv'), os.path.join(data_dir, f'{name}.parquet')
    with open(src, newline='') as f:
        header = next(csv.reader(f))
    if header != list(schema):
        sys.exit(f"{name}.csv: columns don't match the schema\n  csv:    {header}\n  schema: {list(schema)}")

    types = dict(schema)
    if 'month' in types: types['month'] = S                      # parsed below
    table = pacsv.read_csv(src, convert_options=pacsv.ConvertOptions(column_types=types, strings_can_be_null=True))

    if name == 'monthly_statements':
        month = pc.strptime(pc.binary_join_element_wise(table['month'], pa.scalar('-01'), ''), '%Y-%m-%d', 's').cast(D)
        table = table.set_column(table.schema.get_field_index('month'), 'month', month)

    table = table.cast(pa.schema(schema.items()))                 # enforce exact types incl. month -> date32
    pq.write_table(table, dst, compression='zstd')
    print(f'{name}: {table.num_rows} rows -> {os.path.relpath(dst, ROOT)}')


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--data-dir', default=os.path.join(ROOT, 'data'))
    args = ap.parse_args()
    for name, schema in SCHEMAS.items():
        convert(name, schema, args.data_dir)


if __name__ == '__main__':
    main()
