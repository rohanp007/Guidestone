#!/usr/bin/env python3
"""
Give every customer a demo login: an email on the reserved example.com domain (it can never belong to a real person) and a
fake, seeded first and last name, so the app can say "Hi, Jordan". Writes data/users.parquet.

Deterministic: the same seed always gives the same names. Names are invented from short lists and carry no meaning (they are not
tied to the customer's age, income or background).

Run:  .venv/bin/python3 scripts/gen_users.py          (from the repo root; run after to_parquet.py)
"""
import os
import random
import sys

import pandas as pd

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA = os.path.join(ROOT, "data")
SEED = 20261107
EMAIL_DOMAIN = "example.com"          # RFC 2606: reserved for documentation and testing

FIRST = ("Jordan Avery Riley Morgan Taylor Casey Quinn Jamie Dana Skyler Rowan Emerson Harper Sage Reese Parker Logan Devon Finley Kai "
         "Maya Noah Priya Marcus Elena Diego Aisha Owen Mei Samuel Lucia Andre Hana Victor Imani Tomas Nadia Caleb Sofia Ravi Zoe Ibrahim "
         "Clara Mateo Leah Omar Tessa Felix Amara Julian Ingrid Dmitri Bianca Hugo Keiko Nolan Selene Wesley Anika Gideon Marisol").split()
LAST = ("Whitfield Okafor Delgado Nakamura Brennan Castillo Lindqvist Abernathy Takahashi Moreau Calloway Vasquez Hartwell Oyelaran Petrov "
        "Sandoval Fairchild Nguyen Kowalski Ellison Montero Ashworth Bergstrom Chaudhry Donnelly Espinoza Fontaine Gallagher Hollis "
        "Iverson Jankowski Kessler Lombardi Mbeki Novak Okonkwo Pemberton Quintero Rasmussen Salazar Thackeray Underhill Vandermeer "
        "Winslow Yamamoto Zielinski Aldridge Baptiste Cardenas Dunmore Everhart Fitzgerald Grayson Haldane Ishikawa Jovanovic").split()


def main() -> None:
    ids = sorted(pd.read_parquet(os.path.join(DATA, "customers.parquet"), columns=["customer_id"])["customer_id"].astype(str))
    rng = random.Random(SEED)
    pairs = [(f, l) for f in FIRST for l in LAST]
    rng.shuffle(pairs)
    if len(pairs) < len(ids):
        sys.exit(f"only {len(pairs)} distinct names for {len(ids)} customers")
    rows = [dict(customer_id=cid, email=f"{cid.lower()}@{EMAIL_DOMAIN}", first_name=first, last_name=last)
            for cid, (first, last) in zip(ids, pairs)]
    out = pd.DataFrame(rows)
    assert out["email"].is_unique and not out.duplicated(["first_name", "last_name"]).any()
    out.to_parquet(os.path.join(DATA, "users.parquet"), index=False)
    print(f"wrote data/users.parquet: {len(out)} users, e.g. {rows[1]['email']} -> {rows[1]['first_name']} {rows[1]['last_name']}")


if __name__ == "__main__":
    main()
