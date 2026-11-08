"""Importing the package registers every formula (the registry is the only way one is looked up)."""
from . import forecast, health, rules, stress, debt, payoff  # noqa: F401
from ..engine import simulate  # noqa: F401,E402  (registers simulate_scenario / simulate_payoff; after payoff, whose models it imports)
from . import buy  # noqa: F401,E402  (after simulate: it runs the scenario simulation)
