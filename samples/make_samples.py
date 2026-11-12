#!/usr/bin/env python3
"""Generate synthetic demo wardrive data (WiGLE CSV 1.4 + a GPX track).

Everything here is FAKE: random locally-administered MACs (02:xx…), invented
SSIDs, a made-up drive loop. Used for screenshots, the demo, and smoke tests so
no real capture data ever ships with the product.
"""
import argparse, math, random
from datetime import datetime, timedelta
from pathlib import Path

SSIDS = ["CoffeeBar_Guest", "NETGEAR42", "xfinitywifi", "Linksys-5G", "TP-Link_8C21",
         "HOME-7F3A", "Library_Public", "PrinterSetup-HP", "Starlink", "ATT9xQ2",
         "Pizza_Place", "CityHall-Guest", "DIRECT-roku-551", "Verizon_8KJ", ""]
AUTH = ["[WPA2-PSK-CCMP][ESS]", "[WPA3-SAE][ESS]", "[WPA2-PSK-CCMP][WPS][ESS]",
        "[ESS]", "[WEP][ESS]", "[WPA-PSK-TKIP][ESS]"]
AUTH_W = [55, 15, 10, 12, 3, 5]


def mac():
    return "02:" + ":".join(f"{random.randint(0, 255):02x}" for _ in range(5))


def drive(center, n, radius_km, phase):
    lat0, lon0 = center
    for i in range(n):
        a = 2 * math.pi * i / n + phase
        r = radius_km * (0.75 + 0.25 * math.sin(3 * a))
        yield (lat0 + r / 111 * math.sin(a),
               lon0 + r / (111 * math.cos(math.radians(lat0))) * math.cos(a))


def session(out: Path, center, start, seed, aps):
    random.seed(seed)
    rows, t = [], start
    track = list(drive(center, 240, 1.4, seed))
    for lat, lon in track:
        t += timedelta(seconds=4)
        for _ in range(random.randint(0, 4)):
            m, ssid = random.choice(aps)
            rows.append(f"{m},{ssid},{random.choices(AUTH, AUTH_W)[0]},{t:%Y-%m-%d %H:%M:%S},"
                        f"{random.choice([1, 6, 11, 36, 44, 149])},{random.randint(-92, -38)},"
                        f"{lat + random.gauss(0, 0.0004):.6f},{lon + random.gauss(0, 0.0004):.6f},"
                        f"180,5,WIFI")
        if random.random() < 0.15:
            rows.append(f"{mac()},,Misc [LE],{t:%Y-%m-%d %H:%M:%S},0,{random.randint(-95, -55)},"
                        f"{lat:.6f},{lon:.6f},180,5,BLE")
    out.write_text("WigleWifi-1.4,appRelease=rfmap-suite-demo,model=synthetic,release=1,"
                   "device=demo,display=demo,board=demo,brand=demo\n"
                   "MAC,SSID,AuthMode,FirstSeen,Channel,RSSI,CurrentLatitude,CurrentLongitude,"
                   "AltitudeMeters,AccuracyMeters,Type\n" + "\n".join(rows) + "\n")
    return track


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("out", type=Path, nargs="?", default=Path(__file__).parent / "demo")
    ap.add_argument("--center", default="41.8819,-87.6278", help="lat,lon (default: a generic downtown)")
    a = ap.parse_args()
    a.out.mkdir(parents=True, exist_ok=True)
    center = tuple(map(float, a.center.split(",")))
    random.seed(7)
    pool = [(mac(), random.choice(SSIDS)) for _ in range(900)]
    base = datetime(2026, 1, 10, 18, 0)
    for k in range(3):  # 3 overlapping sessions -> exercises cross-session dedup
        track = session(a.out / f"demo_session_{k+1}.csv", center, base + timedelta(days=k), k + 1, pool)
    gpx = "".join(f'<trkpt lat="{la:.6f}" lon="{lo:.6f}"/>' for la, lo in track)
    (a.out / "demo_track.gpx").write_text(
        '<?xml version="1.0"?><gpx version="1.1" xmlns="http://www.topografix.com/GPX/1/1">'
        f"<trk><name>demo</name><trkseg>{gpx}</trkseg></trk></gpx>")
    print(f"wrote demo data to {a.out}")


if __name__ == "__main__":
    main()
