import random
from typing import Optional

import numpy as np


def update_heat(heat: list[float], speaker_idx: int, gamma: float = 0.7) -> list[float]:
    """Decay every agent's heat, then add 1 to the just-spoken agent."""
    new_heat = [gamma * h for h in heat]
    if 0 <= speaker_idx < len(new_heat):
        new_heat[speaker_idx] += 1.0
    return new_heat


def willingness_select(
    scores: list[float],
    heat: list[float],
    lam: float = 2.0,
    tau: float = 1.5,
    forbid_consecutive: bool = True,
    last_speaker: Optional[int] = None,
    rng: Optional[random.Random] = None,
) -> int:
    """Numerically-stable softmax speaker selection with anti-monopoly heat."""
    s = np.asarray(scores, dtype=float)
    c = np.asarray(heat, dtype=float)
    s_eff = (s - lam * c) / tau

    if forbid_consecutive and last_speaker is not None and 0 <= last_speaker < len(s_eff):
        s_eff[last_speaker] = -np.inf

    finite = np.isfinite(s_eff)
    if not np.any(finite):
        # Fallback: uniform over everyone except the forbidden consecutive speaker.
        allowed = list(range(len(s_eff)))
        if forbid_consecutive and last_speaker is not None and last_speaker in allowed and len(allowed) > 1:
            allowed.remove(last_speaker)
        rng = rng or random
        return int(rng.choice(allowed))

    m = np.max(s_eff[finite])
    exp = np.exp(s_eff - m)
    exp[~finite] = 0.0
    probs = exp / exp.sum()

    rng = rng or random
    return int(rng.choices(range(len(probs)), weights=probs, k=1)[0])

