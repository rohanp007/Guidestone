#!/usr/bin/env python3
"""
Print an argon2 hash for DEMO_PASSWORD_HASH (and a fresh SESSION_SECRET if you ask for one).

Run:  .venv/bin/python3 scripts/hash_password.py 'the password'
      .venv/bin/python3 scripts/hash_password.py --secret          # prints a random SESSION_SECRET
Put the output in .env (never commit it). The server stores only the hash.
"""
import secrets
import sys

from argon2 import PasswordHasher

if len(sys.argv) != 2:
    sys.exit(__doc__)
print(secrets.token_urlsafe(48) if sys.argv[1] == "--secret" else PasswordHasher().hash(sys.argv[1]))
