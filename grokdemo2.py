"""
grokdemo2.py -- BNS base-b encode/decode verification (revised)

What this script shows
  1. Encoding: V(i+1) = V(i) + (V(i) - R)(b - 1) + beta_i, which equals
     R + (V(i) - R)*b + beta_i. In other words U = V - R is built by
     appending base-b digits (Horner form).
  2. Decoding uses ONLY the final coordinate and the step count n:
         beta_i  = (V(i) - R) mod b
         V(i-1)  = R + (V(i) - R) // b
     Exact integer arithmetic, so no correction branches are needed.
     (The general formula uses +R; the paper's Eq. 3 used +1, which is
     the same only when R = 1.)
  3. The chart coordinate C1 equals V at every step, and C2 satisfies
         C2 - b*C1 = constant
     so C2 carries no information beyond C1. Both facts are asserted.
  4. Round-trip tests on random payloads, several bases and anchors R.
  5. The sequence length n must be known to decode (a leading zero digit
     does not change the value).

Run: python grokdemo2.py
"""

import random

BASE = 64
R = 1
PAYLOADS = [42, 15, 57, 9, 31, 18]   # same payloads as the paper's Table I


def encode(payloads, base=BASE, r=R):
    """Return rows (i, beta, dist, C1, C2, V, V2). Start state: C1=V=r, C2=r+1."""
    c1, c2, v = r, r + 1, r
    rows = [(0, None, None, c1, c2, v, c2 + 1)]
    for i, beta in enumerate(payloads, start=1):
        if not 0 <= beta < base:
            raise ValueError(f"payload {beta} outside [0, {base - 1}]")
        dist = (c1 - r) * (base - 1) + beta
        c1 += dist
        c2 += dist * base
        v = v + (v - r) * (base - 1) + beta   # independent track, same rule
        assert v == c1, "V and C1 must agree"
        rows.append((i, beta, dist, c1, c2, v, c2 + 1))
    return rows


def decode(c1, c2, n, base=BASE, r=R):
    """Recover n payloads from the final (C1, C2) only. Returns (payloads, steps)."""
    out, steps = [], []
    for i in range(n, 0, -1):
        u = c1 - r
        beta = u % base
        c1_prev = r + u // base
        dist = c1 - c1_prev
        c2_prev = c2 - dist * base
        out.append(beta)
        steps.append((f"{i} -> {i - 1}", c1, c1_prev, c2_prev, beta))
        c1, c2 = c1_prev, c2_prev
    out.reverse()
    assert (c1, c2) == (r, r + 1), "did not return to the start state"
    return out, steps


def check_chart_redundancy(rows, base=BASE, r=R):
    """C2 - base*C1 is constant, so C2 is a function of C1."""
    k = (r + 1) - base * r
    assert all(row[4] - base * row[3] == k for row in rows)
    return k


def random_round_trips(trials=500, seed=1):
    rng = random.Random(seed)
    for _ in range(trials):
        base = rng.choice([2, 3, 8, 16, 64, 256])
        r = rng.randint(-5, 50)
        n = rng.randint(1, 200)
        payloads = [rng.randrange(base) for _ in range(n)]
        rows = encode(payloads, base, r)
        recovered, _ = decode(rows[-1][3], rows[-1][4], n, base, r)
        assert recovered == payloads, (base, r, n)
    return trials


if __name__ == "__main__":
    print("=== Encoding Phase ===")
    enc = encode(PAYLOADS)
    print(f"{'Step':>4} {'Payload':>8} {'Dist':>15} {'Chart C1':>16} "
          f"{'Chart C2':>18} {'Derived V':>16} {'Upper V2':>18}")
    for i, beta, dist, c1, c2, v, v2 in enc:
        print(f"{i:>4} {'Initial' if beta is None else beta:>8} "
              f"{'--' if dist is None else dist:>15} {c1:>16} {c2:>18} {v:>16} {v2:>18}")

    print("\n=== Decoding Phase (from final coordinate and n only) ===")
    final = enc[-1]
    recovered, steps = decode(final[3], final[4], len(PAYLOADS))
    print(f"{'Step':>8} {'Target C1':>16} {'Restored C1':>16} "
          f"{'Restored C2':>18} {'Digit':>6}")
    for step, c1, c1p, c2p, beta in steps:
        print(f"{step:>8} {c1:>16} {c1p:>16} {c2p:>18} {beta:>6}")

    ok = recovered == PAYLOADS
    print("\nRecovered payloads:", recovered)
    print("ALL STEPS PASSED" if ok else "DECODE MISMATCH DETECTED")

    k = check_chart_redundancy(enc)
    print(f"\nChart check: C2 = {BASE}*C1 + ({k}) at every step, "
          "so C2 adds no information beyond C1.")
    print(f"Random round trips passed: {random_round_trips()}")
