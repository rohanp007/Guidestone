# API

Deterministic endpoints only for now; the agent endpoints come later. Start it from `backend/`:

```bash
../.venv/bin/python3 -m uvicorn app.main:app --reload      # ready in about a second with the warm cache
```

Interactive docs: `http://localhost:8000/docs`. Profiles, transactions and statements are loaded at startup, so a data problem
crashes on boot instead of mid-demo.

| Endpoint | Returns |
|---|---|
| `GET /api/health` | `{status, profiles_loaded, as_of, data_hash}` |
| `GET /api/profiles` | persona-switcher list: `{id, age, employment, housing, credit_tier, net_worth, monthly_surplus, flags[]}`; featured personas (`config.FEATURED_PERSONAS`) first |
| `GET /api/profiles/{id}` | the full `Profile` (404 `{"detail": "profile C999 not found"}` if unknown) |
| `GET /api/profiles/{id}/transactions` | `{items, total, page, page_size}`, newest first. Filters: `account_id`, `category`, `flow_class`, `start`, `end` (inclusive), `q`; `page_size` default 50, capped at 200 |
| `GET /api/formulas` | `[{name, category, description, args_schema, result_schema}]` (the schemas are what the debug page builds forms from and what agents will see as tools) |
| `POST /api/profiles/{id}/formulas/{name}` | runs a registered formula with the JSON body as arguments. 404 unknown formula, 422 bad arguments, 501 not implemented, 500 formula failure. `X-Compute-Ms` and `X-Cache` headers; results are cached per (profile, formula, arguments) |
| `GET /api/assumptions` | `assumptions.json` plus the calibration note |

Every response is strict JSON: numpy / pandas values are converted and NaN becomes `null`. Formulas are only ever looked up in the
registry (`formulas/registry.py`); a name that is not registered is a 404, never a call. Profile lookup goes through the single
`get_profile` dependency, so adding auth later is one change. CORS allows `http://localhost:3000` plus `FRONTEND_ORIGIN` if set.

## Frontend types

Generated from the live schema, so a backend model change becomes a compile error:

```bash
cd frontend && npx openapi-typescript http://localhost:8000/openapi.json --default-non-nullable false -o src/lib/api-types.ts
```

Use `--default-non-nullable false`: without it, arguments that have defaults (e.g. every `ForecastArgs` field) become required.
Responses stay strict either way. Add it as `"gen:api"` in `frontend/package.json` (with `openapi-typescript` as a dev dependency).
`src/lib/api.ts` holds typed fetch wrappers on top of the generated types.

## Tests

```bash
cd backend && ../.venv/bin/python3 -m pytest -q
```
