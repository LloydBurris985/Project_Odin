"""
grokdemo2.py -- BNS Base-64 encode/decode verification loop (CORRECTED)
                                                     Changes from the original:                             1. Deterministic payload sequence (matches the paper's Table I bits)                                         instead of random.randint, so output is reproducible for the paper.                                    2. Decode now uses the exact closed-form inverse implied by the paper's
     own Eq. (8)-(10):                                        dist        = C1(i) - ( (C1(i) - R) // BASE + 1 )                                                         C1(i-1)     = C1(i) - dist
         C2(i-1)     = C2(i) - dist * BASE
         bit         = dist  - (C1(i-1) - R) * (BASE - 1)
     This is a single, exact integer computation -- no branching
     "if too high / if too low" correction loop is needed, because with
     Python's arbitrary-precision ints there is no truncation to correct.
     (The original script's V2/V3/diff bookkeeping was internally
     inconsistent, which is why it kept tripping its own error branches
     even though the underlying C1/C2 chart math was already exact.)
  3. V is kept as a second, independently-updated track (same formula,
     same R, same bit) purely to demonstrate Theorem 4 (two isolated
     trajectories computed from identical inputs stay identical) --
     note that V and C1 are mathematically guaranteed to be equal at
     every step, so this is a consistency check, not an independent
     verification.
  4. No interactive input() calls -- prints straight through so output
     can be piped directly into the paper's tables.
"""

BASE = 64
R = 1

# Deterministic payload sequence (same bits used in the paper's Table I)
PAYLOADS = [42, 15, 57, 9, 31, 18]

def encode(payloads):
    C1, C2, V = 1, 2, 1
    rows = [(0, None, None, C1, C2, V, C2 + 1)]
    for i, bit in enumerate(payloads, start=1):
        dist = (C1 - R) * (BASE - 1) + bit
        C1 = C1 + dist
        C2 = C2 + dist * BASE
        V = V + (V - R) * (BASE - 1) + bit
        rows.append((i, bit, dist, C1, C2, V, C2 + 1))
    return rows

def decode(rows):
    # rows[-1] is the final encoded state; walk backward and verify
    # against every earlier row.
    results = []
    C1, C2, V = rows[-1][3], rows[-1][4], rows[-1][5]
    for i in range(len(rows) - 1, 0, -1):
        dist = C1 - ((C1 - R) // BASE + 1)
        C1_prev = C1 - dist
        C2_prev = C2 - dist * BASE
        bit_recovered = dist - (C1_prev - R) * (BASE - 1)
        V_prev = V - dist  # V and C1 track identically, so same dist applies

        expected_bit = rows[i][1]
        expected_C1 = rows[i - 1][3]
        expected_C2 = rows[i - 1][4]
        status = "Passed" if (
            bit_recovered == expected_bit and
            C1_prev == expected_C1 and
            C2_prev == expected_C2
        ) else "FAILED"

        results.append((f"{i} -> {i-1}", C1, V, C1_prev, C2_prev, V_prev, status))
        C1, C2, V = C1_prev, C2_prev, V_prev
    return results

if __name__ == "__main__":
    print("=== Encoding Phase ===")
    enc_rows = encode(PAYLOADS)
    print(f"{'Step':>4} {'Payload':>8} {'Dist':>15} {'Chart C1':>16} {'Chart C2':>18} {'Derived V':>16} {'Upper V2':>18}")
    for i, bit, dist, C1, C2, V, V2 in enc_rows:
        bit_s = "Initial" if bit is None else bit
        dist_s = "--" if dist is None else dist
        print(f"{i:>4} {bit_s!s:>8} {dist_s!s:>15} {C1:>16} {C2:>18} {V:>16} {V2:>18}")

    print("\n=== Decoding Phase ===")
    dec_rows = decode(enc_rows)
    print(f"{'Step':>8} {'Target C3':>16} {'Target V3':>16} {'Restored C1':>16} {'Restored C2':>18} {'Restored V':>16} {'Status':>8}")
    for step, C3, V3, C1p, C2p, Vp, status in dec_rows:
        print(f"{step:>8} {C3:>16} {V3:>16} {C1p:>16} {C2p:>18} {Vp:>16} {status:>8}")

    all_passed = all(r[-1] == "Passed" for r in dec_rows)
    print("\nALL STEPS PASSED" if all_passed else "DECODE MISMATCH DETECTED")
