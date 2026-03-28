"""Kelly-Kriterium für Positionsgrößenberechnung."""

from __future__ import annotations


def calculate_position_size(
    win_rate: float,
    avg_win: float,
    avg_loss: float,
    portfolio_value: float,
    max_pct: float = 0.08,
) -> float:
    """Berechnet optimale Positionsgröße mit halbem Kelly.

    Args:
        win_rate: Gewinnrate (0-1)
        avg_win: Durchschnittlicher Gewinn pro Trade (absolut)
        avg_loss: Durchschnittlicher Verlust pro Trade (absolut)
        portfolio_value: Gesamtwert des Portfolios
        max_pct: Hard Cap als Anteil des Portfolios (default 8%)

    Returns:
        Positionsgröße in USD
    """
    if avg_win <= 0 or portfolio_value <= 0:
        return 0.0

    kelly = (win_rate * avg_win - (1 - win_rate) * avg_loss) / avg_win
    kelly = max(0.0, min(kelly, 0.25))  # Cap bei 25%
    fractional_kelly = kelly * 0.5  # Halbes Kelly — konservativ

    position = portfolio_value * fractional_kelly
    max_position = portfolio_value * max_pct
    return min(position, max_position)
