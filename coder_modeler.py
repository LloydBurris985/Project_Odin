# coder_modeler.py - Universal Coder/Decoder Modeler for Project Odin
import os, json
from pathlib import Path

class CoderModeler:
    _registry = {}

    @classmethod
    def register(cls, name: str, encode_func, decode_func, default_params=None):
        cls._registry[name] = {
            "encode": encode_func,
            "decode": decode_func,
            "params": default_params or {}
        }
        print(f"[MODEL] Registered coder: {name}")

    @classmethod
    def encode(cls, data, coder_name: str, **extra_params):
        if coder_name not in cls._registry:
            raise ValueError(f"Unknown coder: {coder_name}. Register it first!")
        coder = cls._registry[coder_name]
        params = {**coder["params"], **extra_params}
        return coder["encode"](data, **params)

    @classmethod
    def decode(cls, encoded, coder_name: str, **extra_params):
        coder = cls._registry[coder_name]
        params = {**coder["params"], **extra_params}
        return coder["decode"](encoded, **params)

# ====================== REGISTER YOUR FAVORITES HERE ======================

# 1. Paper BNS Chart-based (fixed version that actually roundtrips)
def bns_chart_encode(data_bits, limit=4, **_):
    # Paste your PDF Listing 1 here (or my fixed version that works)
    # For now: placeholder - replace with your working encode
    states = []  # your real states list
    # ... your real bns_chart_encode code ...
    return states  # list of (V, R, ...) tuples

def bns_chart_decode(states, target_size=None, **_):
    # Paste your PDF Listing 2 (or my fixed final version)
    # ... your real decode ...
    return bits, bytes_data  # returns raw bits + bytes

CoderModeler.register(
    "bns_chart",
    bns_chart_encode,
    bns_chart_decode,
    {"limit": 4, "target_size": None}
)

# 2. Your current Odin's Eye (drop-in)
def odin_eye_encode(data, start_p=1000000, seed="2026-03-20", **_):
    # Call your existing odins_eye.py --mode encode
    # For simplicity you can subprocess or import
    return "lattice_file.coord.json"  # whatever it returns

def odin_eye_decode(lattice_file, **_):
    # your decode with --any
    return b"decoded_data"

CoderModeler.register(
    "odin_eye",
    odin_eye_encode,
    odin_eye_decode,
    {"start_p": 1000000}
)

# Add any other coders the same way - takes 5 seconds each

# ====================== HOW TO USE IN AIRPORT / MAILBOX ======================
# In airport.py or mailbox.py just do:
# encoded = CoderModeler.encode(message_bits, "bns_chart", start_coord=500000)
# decoded_data = CoderModeler.decode(encoded_states, "bns_chart", target_size=1024)
