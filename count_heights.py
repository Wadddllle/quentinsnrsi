import json

with open("sg_buildings_v5.geojson") as f:
    data = json.load(f)

zero = 0
non_zero = 0

for feature in data["features"]:
    height = feature["properties"].get("height")
    if height == 0:
        zero += 1
    else:
        non_zero += 1

print(f"Zero height:     {zero}")
print(f"Non-zero height: {non_zero}")
print(f"Total:           {zero + non_zero}")
