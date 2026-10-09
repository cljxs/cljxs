#!/usr/bin/env python3
"""
indicators.py — the arithmetic half of the anti-hallucination rule.

Every number an agent is allowed to cite is computed here, in plain code,
and written to a JSON file. No language model ever calculates an RSI.

Belfort keeps its own inline copy of this math. That is deliberate: it is
working, it is scheduled, and the agent spec says not to touch other agents.
When you next have reason to open belfort-fetch.py, point it here instead and
delete its copy - but do it as its own change, with its output compared before
and after, not as a side effect of adding an agent.

Standard library only.
"""


def sma(values, n):
    """Simple moving average of the last n values."""
    if len(values) < n:
        return None
    return sum(values[-n:]) / n


def ema_series(values, n):
    """EMA seeded with the first n-value SMA, as every charting package does."""
    if len(values) < n:
        return []
    k = 2.0 / (n + 1)
    out = [sum(values[:n]) / n]
    for v in values[n:]:
        out.append(v * k + out[-1] * (1 - k))
    return out


def rsi(values, n=14):
    """Wilder's RSI - the smoothing is Wilder's, not a plain average."""
    if len(values) < n + 1:
        return None
    gains, losses = [], []
    for i in range(1, n + 1):
        d = values[i] - values[i - 1]
        gains.append(max(d, 0.0))
        losses.append(max(-d, 0.0))
    avg_gain = sum(gains) / n
    avg_loss = sum(losses) / n
    for i in range(n + 1, len(values)):
        d = values[i] - values[i - 1]
        avg_gain = (avg_gain * (n - 1) + max(d, 0.0)) / n
        avg_loss = (avg_loss * (n - 1) + max(-d, 0.0)) / n
    if avg_loss == 0:
        return 100.0
    return 100.0 - (100.0 / (1.0 + avg_gain / avg_loss))


def macd(values, fast=12, slow=26, signal=9):
    """Returns (macd_line, signal_line, histogram), all latest values."""
    if len(values) < slow + signal:
        return None, None, None
    ema_fast = ema_series(values, fast)
    ema_slow = ema_series(values, slow)
    offset = len(ema_fast) - len(ema_slow)
    line = [f - s for f, s in zip(ema_fast[offset:], ema_slow)]
    sig = ema_series(line, signal)
    if not sig:
        return None, None, None
    return line[-1], sig[-1], line[-1] - sig[-1]


def pct_change(values, bars):
    """Percent change over the last `bars` sessions. None if there is not
    enough history - never a wrong number silently."""
    if len(values) <= bars or values[-1 - bars] == 0:
        return None
    return (values[-1] / values[-1 - bars] - 1) * 100.0


def r2(x):
    return None if x is None else round(x, 2)
