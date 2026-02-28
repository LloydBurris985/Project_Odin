# demo_odins_eye.py - Quick test of OdinsEye

from odins_net.core import OdinsEye  # Adjust path/class if needed

eye = OdinsEye()

test_data = b"Odin's Net is alive!"
coord = eye.encode(test_data, mask=54321)  # adapt args
print("Coordinate:", coord)

decoded = eye.decode(coord)
print("Decoded:", decoded)
print("Success!" if decoded == test_data else "Fail")
