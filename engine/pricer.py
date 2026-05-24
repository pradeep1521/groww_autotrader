"""
Black-Scholes options pricer with Greeks.
"""

import numpy as np
from scipy.stats import norm

RISK_FREE_RATE = 0.07   # 7% — Indian benchmark


def black_scholes(S: float, K: float, T: float, sigma: float,
                  r: float = RISK_FREE_RATE, opt_type: str = "CE") -> float:
    """European call/put price via Black-Scholes."""
    if T <= 0:
        return max(0.0, S - K) if opt_type == "CE" else max(0.0, K - S)
    d1 = (np.log(S / K) + (r + 0.5 * sigma**2) * T) / (sigma * np.sqrt(T))
    d2 = d1 - sigma * np.sqrt(T)
    if opt_type == "CE":
        return max(0.0, float(S * norm.cdf(d1) - K * np.exp(-r * T) * norm.cdf(d2)))
    return max(0.0, float(K * np.exp(-r * T) * norm.cdf(-d2) - S * norm.cdf(-d1)))


def greeks(S: float, K: float, T: float, sigma: float,
           r: float = RISK_FREE_RATE, opt_type: str = "CE") -> dict:
    """Compute delta, gamma, theta, vega."""
    if T <= 0:
        return {"delta": 1.0 if opt_type == "CE" else -1.0,
                "gamma": 0.0, "theta": 0.0, "vega": 0.0}
    d1     = (np.log(S / K) + (r + 0.5 * sigma**2) * T) / (sigma * np.sqrt(T))
    d2     = d1 - sigma * np.sqrt(T)
    nd1    = norm.pdf(d1)
    delta  = norm.cdf(d1)  if opt_type == "CE" else norm.cdf(d1) - 1
    gamma  = nd1 / (S * sigma * np.sqrt(T))
    theta  = (-(S * nd1 * sigma) / (2 * np.sqrt(T)) - r * K * np.exp(-r * T) * norm.cdf(d2)) / 365
    vega   = S * nd1 * np.sqrt(T) / 100
    return {
        "delta": round(float(delta), 4),
        "gamma": round(float(gamma), 6),
        "theta": round(float(theta), 4),
        "vega":  round(float(vega),  4),
    }
