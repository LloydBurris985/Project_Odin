import json
print("Runway setup")
name = input("Runway name: ")
hours = int(input("Poll hours (1=test / 24=prod): "))
length = 10000 if hours == 1 else 1000000
mode = input("text or binary: ")
data = {"name": name, "length": length, "mode": mode, "poll_hours": hours}
with open("runways.json", "w") as f:
    json.dump(data, f, indent=2)
print(f"runways.json created – length {length} coords")
