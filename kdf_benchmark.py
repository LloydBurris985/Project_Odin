"""
kdf_benchmark.py — Phase 2B KDF benchmark (Argon2id vs scrypt vs PBKDF2)
============================================================================
Run this on your actual phone (Termux) to confirm the mobile-tuned Argon2id
parameters in stateless_comms.py are safe (no OOM, reasonable latency)
before relying on them. I could not test the Argon2id path myself — no
network access in my sandbox to install argon2-cffi. Only scrypt and
PBKDF2 fallback were verified there.

Usage
-----
    pip install argon2-cffi --break-system-packages   # if not already installed
    python kdf_benchmark.py
"""

import hashlib
import time

try:
    from argon2 import low_level as argon2_low_level
    HAVE_ARGON2 = True
except ImportError:
    HAVE_ARGON2 = False

try:
    from cryptography.hazmat.primitives.kdf.scrypt import Scrypt
    HAVE_SCRYPT = True
except ImportError:
    HAVE_SCRYPT = False

# Same constants as stateless_comms.py — keep these in sync if you tune them.
ARGON2_TIME_COST = 2
ARGON2_MEMORY_COST_KIB = 19456   # ~19 MB, OWASP low-memory-safe profile
ARGON2_PARALLELISM = 1

SCRYPT_N = 2 ** 14
SCRYPT_R = 8
SCRYPT_P = 1

PBKDF2_ITERATIONS = 600_000

TEST_INPUT = b"OdinNet-PassphraseGeometry-v1:benchmark-only-not-a-real-passphrase"
TEST_SALT = b"0123456789abcdef"


def bench(label, fn, samples=5):
    times_ms = []
    for _ in range(samples):
        t0 = time.perf_counter()
        fn()
        t1 = time.perf_counter()
        times_ms.append((t1 - t0) * 1000)
    mean = sum(times_ms) / len(times_ms)
    print(f"  {label}: mean={mean:.1f}ms  min={min(times_ms):.1f}ms  max={max(times_ms):.1f}ms  (n={samples})")
    return mean


def main():
    print("=" * 66)
    print("  ODINNET — PHASE 2B KDF BENCHMARK (Argon2id / scrypt / PBKDF2)")
    print("=" * 66)
    print()
    print(f"  argon2-cffi available: {HAVE_ARGON2}")
    print(f"  scrypt (cryptography) available: {HAVE_SCRYPT}")
    print()

    if HAVE_ARGON2:
        print(f"  Argon2id params: time_cost={ARGON2_TIME_COST}  "
              f"memory_cost={ARGON2_MEMORY_COST_KIB}KiB (~{ARGON2_MEMORY_COST_KIB/1024:.0f}MB)  "
              f"parallelism={ARGON2_PARALLELISM}")

        def argon2_call():
            argon2_low_level.hash_secret_raw(
                secret=TEST_INPUT, salt=TEST_SALT,
                time_cost=ARGON2_TIME_COST, memory_cost=ARGON2_MEMORY_COST_KIB,
                parallelism=ARGON2_PARALLELISM, hash_len=32,
                type=argon2_low_level.Type.ID,
            )
        argon2_mean = bench("Argon2id", argon2_call)
        print()
        if argon2_mean > 1000:
            print("  ⚠️  Argon2id mean latency exceeds 1000ms — this runs once per")
            print("     4-hour window (not per poll), so this is likely still fine,")
            print("     but confirm it doesn't cause a UI stall on window refresh.")
        else:
            print("  ✅ Argon2id latency looks reasonable for a once-per-window cost.")
    else:
        print("  ⚠️  argon2-cffi not installed. Install with:")
        print("     pip install argon2-cffi --break-system-packages")
        print("     Then re-run this script to get real Argon2id numbers.")

    print()

    if HAVE_SCRYPT:
        print(f"  scrypt params: n=2^{SCRYPT_N.bit_length()-1}  r={SCRYPT_R}  p={SCRYPT_P}")

        def scrypt_call():
            kdf = Scrypt(salt=TEST_SALT, length=32, n=SCRYPT_N, r=SCRYPT_R, p=SCRYPT_P)
            kdf.derive(TEST_INPUT)
        scrypt_mean = bench("scrypt (fallback)", scrypt_call)
    else:
        print("  scrypt unavailable (unexpected — should ship with 'cryptography').")

    print()
    print(f"  PBKDF2-HMAC-SHA256 iterations={PBKDF2_ITERATIONS:,} (last-resort fallback)")

    def pbkdf2_call():
        hashlib.pbkdf2_hmac("sha256", TEST_INPUT, TEST_SALT, PBKDF2_ITERATIONS, dklen=32)
    pbkdf2_mean = bench("PBKDF2 (last resort)", pbkdf2_call)

    print()
    print("=" * 66)
    print("  All three run ONCE PER 4-HOUR WINDOW, not per poll — even the")
    print("  slowest option here is very unlikely to matter for battery life")
    print("  at that frequency. This benchmark is about confirming no OOM")
    print("  kill and no multi-second stall on your actual device, not about")
    print("  micro-optimizing milliseconds.")
    print("=" * 66)


if __name__ == "__main__":
    main()
