eye = OdinsEye(start_mask=50000)
data = b"Hello, Odins Net! This is a longer test to trigger bounce."
coord = eye.encode(data)
decoded = eye.decode(coord)
print("Success!" if decoded == data else "Fail")
print("Coord:", coord)
