#!/usr/bin/env python3
"""
rfmap.py — Local RF mapping tool for wardrive / Meshtastic / sensor data.
Produces a self-contained HTML map; no server required.

Usage:
    python3 rfmap.py /path/to/wardrive/dir [--out map.html] [--watch]
    python3 rfmap.py session1.log session2.log --out map.html
"""

import argparse
import datetime
import os
import struct
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

import folium
import pandas as pd
from folium.plugins import HeatMap, MarkerCluster, TimestampedGeoJson

# ---------------------------------------------------------------------------
# CSV / log parsing — the brittle part
# ---------------------------------------------------------------------------

WIGLE_REQUIRED = {"currentlatitude", "currentlongitude", "mac"}

# Known column aliases from AWOK / Marauder / plain exports
COLUMN_ALIASES = {
    "lat": "currentlatitude",
    "latitude": "currentlatitude",
    "lon": "currentlongitude",
    "lng": "currentlongitude",
    "longitude": "currentlongitude",
    "bssid": "mac",
    "network": "ssid",
    "name": "ssid",
    "signal": "rssi",
    "signal_level": "rssi",
    "encryption": "authmode",
    "security": "authmode",
    "capabilities": "authmode",
}


def _normalize_columns(df: pd.DataFrame) -> pd.DataFrame:
    df.columns = [c.strip().lower() for c in df.columns]
    df.rename(columns=COLUMN_ALIASES, inplace=True)
    return df


def _detect_preamble(path: Path) -> int:
    """
    Return number of rows to skip before the real column header.
    Marauder/WiGLE prepend a 'WigleWifi-1.4,...' metadata line.
    """
    with path.open(newline="", encoding="utf-8", errors="replace") as f:
        for i, line in enumerate(f):
            if not line.strip().lower().startswith("wiglewifi"):
                return i
    return 0


def load_wardrive(path: Path) -> pd.DataFrame:
    """
    Load one wardrive log/CSV (WiGLE, Marauder, or AWOK format).
    Returns a normalized DataFrame; attaches a 'source' column with the filename.
    """
    skip = _detect_preamble(path)

    try:
        df = pd.read_csv(path, skiprows=skip, low_memory=False)
    except Exception as e:
        print(f"Warning: could not read {path.name}: {e}", file=sys.stderr)
        return pd.DataFrame()

    if df.empty:
        return df

    df = _normalize_columns(df)

    missing = WIGLE_REQUIRED - set(df.columns)
    if missing:
        print(
            f"Warning: skipping {path.name} — missing columns {missing}. "
            f"Found: {list(df.columns)}",
            file=sys.stderr,
        )
        return pd.DataFrame()

    df["currentlatitude"] = pd.to_numeric(df["currentlatitude"], errors="coerce")
    df["currentlongitude"] = pd.to_numeric(df["currentlongitude"], errors="coerce")
    df.dropna(subset=["currentlatitude", "currentlongitude"], inplace=True)
    # Drop 0,0 rows — GPS not locked
    df = df[(df["currentlatitude"] != 0) | (df["currentlongitude"] != 0)]

    if "rssi" not in df.columns:
        df["rssi"] = -80
    else:
        df["rssi"] = pd.to_numeric(df["rssi"], errors="coerce").fillna(-80)

    df["ssid"] = df.get("ssid", pd.Series("", index=df.index)).fillna("").astype(str)
    df["mac"] = df["mac"].fillna("").astype(str).str.upper()
    df["authmode"] = df.get("authmode", pd.Series("", index=df.index)).fillna("").astype(str)
    df["type"] = df.get("type", pd.Series("WIFI", index=df.index)).fillna("WIFI").astype(str).str.upper()
    df["source"] = path.name

    if "firstseen" not in df.columns:
        df["firstseen"] = pd.NaT
    else:
        df["firstseen"] = pd.to_datetime(df["firstseen"], errors="coerce")

    return df.reset_index(drop=True)


def load_all(paths: list[Path]) -> pd.DataFrame:
    frames = []
    for p in paths:
        df = load_wardrive(p)
        if not df.empty:
            frames.append(df)
            print(f"  {p.name}: {len(df)} rows")
    if not frames:
        sys.exit("No usable data found in any input file.")
    return pd.concat(frames, ignore_index=True)


def dedup_aps(df: pd.DataFrame) -> pd.DataFrame:
    """
    Collapse duplicate MACs to one row (best RSSI).
    Adds 'session_count' — number of sessions where the MAC appeared.
    """
    if df.empty:
        return df
    session_counts = df.groupby("mac")["source"].nunique().rename("session_count")
    idx = df.groupby("mac")["rssi"].idxmax()
    deduped = df.loc[idx].copy()
    deduped = deduped.join(session_counts, on="mac")
    return deduped.reset_index(drop=True)


# ---------------------------------------------------------------------------
# Drive track extraction from wardrive log data
# ---------------------------------------------------------------------------

def extract_log_tracks(df: pd.DataFrame) -> list[tuple[str, list[tuple[float, float]]]]:
    """
    Build one ordered route per source file from the wardrive DataFrame.
    Returns [(session_name, [(lat, lon), ...]), ...].
    """
    tracks = []
    for source, group in df.groupby("source", sort=False):
        sortable = group.dropna(subset=["firstseen"])
        if sortable.empty:
            sortable = group
        else:
            sortable = sortable.sort_values("firstseen")

        # Round to ~11m precision to collapse near-identical GPS fixes
        pts_raw = sortable[["currentlatitude", "currentlongitude"]].round(4).values.tolist()

        # Drop consecutive duplicates
        pts: list[tuple[float, float]] = []
        for pt in pts_raw:
            p = (pt[0], pt[1])
            if not pts or pts[-1] != p:
                pts.append(p)

        if len(pts) >= 2:
            tracks.append((str(source), pts))
    return tracks


# ---------------------------------------------------------------------------
# Encryption classification
# ---------------------------------------------------------------------------

def _enc_color(authmode: str) -> str:
    am = str(authmode).upper()
    if not am or am in ("", "NONE", "[ESS]", "[]"):
        return "red"
    if "WPA3" in am:
        return "green"
    if "WPA2" in am or "WPA" in am:
        return "blue"
    if "WEP" in am:
        return "orange"
    return "gray"


def _enc_label(authmode: str) -> str:
    am = str(authmode).upper()
    if not am or am in ("", "NONE", "[ESS]", "[]"):
        return "OPEN"
    if "WPA3" in am:
        return "WPA3"
    if "WPA2" in am:
        return "WPA2"
    if "WPA" in am:
        return "WPA"
    if "WEP" in am:
        return "WEP"
    return "OTHER"


# ---------------------------------------------------------------------------
# POI loader — lat,lon,label CSV
# ---------------------------------------------------------------------------

def load_poi(path: Path) -> pd.DataFrame:
    try:
        df = pd.read_csv(path, header=None, names=["lat", "lon", "label"])
    except Exception as e:
        print(f"Warning: could not load POI file {path}: {e}", file=sys.stderr)
        return pd.DataFrame(columns=["lat", "lon", "label"])
    df["lat"] = pd.to_numeric(df["lat"], errors="coerce")
    df["lon"] = pd.to_numeric(df["lon"], errors="coerce")
    df.dropna(subset=["lat", "lon"], inplace=True)
    df["label"] = df["label"].fillna("").astype(str)
    return df.reset_index(drop=True)


# ---------------------------------------------------------------------------
# GPX parsing — drive tracks and waypoints
# ---------------------------------------------------------------------------

_GPX_NS = {"gpx": "http://www.topografix.com/GPX/1/1"}


def _gpx_coords(elem, tag: str) -> list[tuple[float, float]]:
    coords = []
    for pt in elem.findall(f".//{{{_GPX_NS['gpx']}}}{tag}") or elem.findall(f".//{tag}"):
        try:
            lat = float(pt.get("lat"))
            lon = float(pt.get("lon"))
            coords.append((lat, lon))
        except (TypeError, ValueError):
            pass
    return coords


def load_gpx_tracks(paths: list[Path]) -> list[list[tuple[float, float]]]:
    """Return a list of track-point sequences, one per GPX file with <trk>."""
    tracks = []
    for p in paths:
        try:
            tree = ET.parse(p)
            root = tree.getroot()
        except ET.ParseError:
            continue
        pts = _gpx_coords(root, "trkpt")
        if pts:
            tracks.append(pts)
    return tracks


def load_gpx_waypoints(paths: list[Path]) -> pd.DataFrame:
    """Return a DataFrame of waypoints from GPX files containing <wpt>."""
    rows = []
    for p in paths:
        try:
            tree = ET.parse(p)
            root = tree.getroot()
        except ET.ParseError:
            continue
        for wpt in root.findall(f"{{{_GPX_NS['gpx']}}}wpt") or root.findall("wpt"):
            try:
                lat = float(wpt.get("lat"))
                lon = float(wpt.get("lon"))
                name_el = wpt.find(f"{{{_GPX_NS['gpx']}}}name") or wpt.find("name")
                label = name_el.text.strip() if name_el is not None and name_el.text else p.stem
                rows.append({"lat": lat, "lon": lon, "label": label})
            except (TypeError, ValueError):
                pass
    if not rows:
        return pd.DataFrame(columns=["lat", "lon", "label"])
    return pd.DataFrame(rows)


# ---------------------------------------------------------------------------
# Time animation data extraction
# ---------------------------------------------------------------------------

def extract_animation_features(df: pd.DataFrame) -> list[dict]:
    """
    Build a GeoJSON feature list from wardrive log timestamps for TimestampedGeoJson.
    One feature per unique (time, lat, lon) observation, sorted chronologically.
    """
    anim = df.dropna(subset=["firstseen"]).copy()
    if anim.empty:
        return []

    anim["lat_r"] = anim["currentlatitude"].round(4)
    anim["lon_r"]  = anim["currentlongitude"].round(4)
    anim = anim.drop_duplicates(subset=["firstseen", "lat_r", "lon_r"])
    anim = anim.sort_values("firstseen")

    # Assign a color per session
    sources = anim["source"].unique().tolist()
    palette = ["#00d4ff", "#00ff9f", "#f59e0b", "#f472b6", "#a78bfa", "#fb923c"]
    color_map = {s: palette[i % len(palette)] for i, s in enumerate(sources)}

    features = []
    for _, row in anim.iterrows():
        ts = row["firstseen"].isoformat()
        color = color_map.get(row["source"], "#00d4ff")
        features.append({
            "type": "Feature",
            "geometry": {
                "type": "Point",
                "coordinates": [float(row["lon_r"]), float(row["lat_r"])]
            },
            "properties": {
                "time": ts,
                "popup": row["source"],
                "icon": "circle",
                "iconstyle": {
                    "fillColor": color,
                    "fillOpacity": 0.85,
                    "stroke": False,
                    "radius": 5,
                },
            },
        })
    return features


# ---------------------------------------------------------------------------
# OUI vendor lookup
# ---------------------------------------------------------------------------

_OUI_DB_CACHE: dict[str, str] | None = None


def _parse_wireshark_manuf(path: Path) -> dict[str, str]:
    db: dict[str, str] = {}
    try:
        with path.open(encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                parts = line.split("\t")
                if len(parts) < 2:
                    continue
                oui_raw = parts[0].split("/")[0].replace(":", "").replace("-", "").upper()
                if len(oui_raw) != 6:
                    continue
                vendor = parts[2].strip() if len(parts) > 2 and parts[2].strip() else parts[1].strip()
                db[oui_raw] = vendor
    except Exception:
        pass
    return db


def _parse_ieee_oui(path: Path) -> dict[str, str]:
    db: dict[str, str] = {}
    try:
        with path.open(encoding="utf-8", errors="replace") as f:
            for line in f:
                if "(hex)" not in line:
                    continue
                parts = line.split("\t")
                if len(parts) < 2:
                    continue
                oui_raw = parts[0].strip().replace("-", "").upper()
                if len(oui_raw) != 6:
                    continue
                vendor = "\t".join(parts[1:]).strip()
                if vendor:
                    db[oui_raw] = vendor
    except Exception:
        pass
    return db


def load_oui_db() -> dict[str, str]:
    """Load OUI vendor database. Tries wireshark manuf, then cached IEEE file, then download."""
    global _OUI_DB_CACHE
    if _OUI_DB_CACHE is not None:
        return _OUI_DB_CACHE

    for p in [
        Path("/usr/share/wireshark/manuf"),
        Path("/usr/share/wireshark-common/manuf"),
        Path("/usr/local/share/wireshark/manuf"),
    ]:
        if p.exists():
            db = _parse_wireshark_manuf(p)
            if db:
                _OUI_DB_CACHE = db
                print(f"[rfmap] OUI: loaded {len(db):,} entries from {p}", file=sys.stderr)
                return db

    cache = Path.home() / ".local/share/rfmap-suite/oui.txt"
    if cache.exists():
        db = _parse_ieee_oui(cache)
        if db:
            _OUI_DB_CACHE = db
            print(f"[rfmap] OUI: loaded {len(db):,} entries from cache", file=sys.stderr)
            return db

    try:
        import urllib.request
        cache.parent.mkdir(parents=True, exist_ok=True)
        print("[rfmap] OUI: downloading IEEE database...", file=sys.stderr)
        urllib.request.urlretrieve("https://standards-oui.ieee.org/oui/oui.txt", str(cache))
        db = _parse_ieee_oui(cache)
        if db:
            _OUI_DB_CACHE = db
            print(f"[rfmap] OUI: loaded {len(db):,} entries", file=sys.stderr)
            return db
    except Exception as e:
        print(f"[rfmap] OUI download failed: {e}", file=sys.stderr)

    _OUI_DB_CACHE = {}
    return {}


def lookup_vendor(mac: str, oui_db: dict[str, str]) -> str:
    if not oui_db or not mac:
        return ""
    prefix = mac.replace(":", "").replace("-", "").upper()[:6]
    return oui_db.get(prefix, "")


# ---------------------------------------------------------------------------
# Probe request PCAP parser (stdlib-only, no scapy/dpkt required)
# ---------------------------------------------------------------------------

def _parse_80211_probe(frame: bytes) -> dict | None:
    """Return {mac, ssid} if frame is an 802.11 probe request, else None."""
    if len(frame) < 24:
        return None
    fc = struct.unpack_from("<H", frame, 0)[0]
    ftype    = (fc >> 2) & 0x3
    fsubtype = (fc >> 4) & 0xf
    if ftype != 0 or fsubtype != 4:  # management / probe-request
        return None
    src = frame[10:16]
    if src[0] & 1:  # multicast/broadcast source
        return None
    mac = ":".join(f"{b:02x}" for b in src).upper()
    pos, ssid = 24, ""
    while pos + 2 <= len(frame):
        tag  = frame[pos]
        tlen = frame[pos + 1]
        pos += 2
        if pos + tlen > len(frame):
            break
        if tag == 0:
            try:
                ssid = frame[pos:pos + tlen].decode("utf-8", errors="replace").strip("\x00")
            except Exception:
                ssid = ""
            break
        pos += tlen
    return {"mac": mac, "ssid": ssid}


def load_probes(paths: list[Path]) -> pd.DataFrame:
    """
    Parse 802.11 probe-request PCAPs (libpcap format, radiotap or raw 802.11).
    Returns DataFrame with columns: mac, ssid, timestamp, source.
    """
    rows: list[dict] = []
    for path in paths:
        try:
            with open(path, "rb") as f:
                gh = f.read(24)
                if len(gh) < 24:
                    continue
                magic = struct.unpack_from("<I", gh, 0)[0]
                if magic in (0xA1B2C3D4, 0xA1B23C4D):
                    endian, ns = "<", (magic == 0xA1B23C4D)
                elif magic in (0xD4C3B2A1, 0x4D3CB2A1):
                    endian, ns = ">", (magic == 0x4D3CB2A1)
                else:
                    continue

                linktype = struct.unpack_from(f"{endian}I", gh, 20)[0]
                if linktype not in (105, 127):
                    continue  # not 802.11 or radiotap

                count = 0
                while True:
                    ph = f.read(16)
                    if len(ph) < 16:
                        break
                    ts_sec, ts_frac, incl_len, _ = struct.unpack_from(f"{endian}IIII", ph)
                    data = f.read(incl_len)
                    if len(data) < incl_len:
                        break

                    ts = datetime.datetime.fromtimestamp(
                        ts_sec + ts_frac / (1e9 if ns else 1e6)
                    )
                    frame = data
                    if linktype == 127:
                        if len(data) < 4:
                            continue
                        rt_len = struct.unpack_from("<H", data, 2)[0]
                        if rt_len >= len(data):
                            continue
                        frame = data[rt_len:]

                    result = _parse_80211_probe(frame)
                    if result:
                        rows.append({
                            "mac":       result["mac"],
                            "ssid":      result["ssid"],
                            "timestamp": ts,
                            "source":    path.name,
                        })
                        count += 1
                if count:
                    print(f"  {path.name}: {count} probe requests", file=sys.stderr)
        except Exception as e:
            print(f"Warning: skipping {path.name}: {e}", file=sys.stderr)

    if not rows:
        return pd.DataFrame(columns=["mac", "ssid", "timestamp", "source"])
    return pd.DataFrame(rows)


def correlate_probes(probes_df: pd.DataFrame, wardrive_df: pd.DataFrame) -> pd.DataFrame:
    """
    Assign GPS coordinates to probe requests by matching their timestamps to
    the nearest firstseen timestamp in the wardrive data.
    """
    if probes_df.empty or wardrive_df.empty:
        return probes_df

    wd = wardrive_df.dropna(subset=["firstseen"])[
        ["firstseen", "currentlatitude", "currentlongitude"]
    ].copy()

    probes = probes_df.copy()
    probes["timestamp"] = pd.to_datetime(probes["timestamp"], errors="coerce")
    probes = probes.dropna(subset=["timestamp"]).sort_values("timestamp").reset_index(drop=True)

    if wd.empty or probes.empty:
        avg_lat = wardrive_df["currentlatitude"].mean()
        avg_lon = wardrive_df["currentlongitude"].mean()
        probes["currentlatitude"]  = avg_lat
        probes["currentlongitude"] = avg_lon
        return probes

    wd = wd.sort_values("firstseen").reset_index(drop=True)

    # Align timezone awareness
    if wd["firstseen"].dt.tz is not None and probes["timestamp"].dt.tz is None:
        probes["timestamp"] = probes["timestamp"].dt.tz_localize("UTC")
    elif wd["firstseen"].dt.tz is None and probes["timestamp"].dt.tz is not None:
        probes["timestamp"] = probes["timestamp"].dt.tz_localize(None)

    result = pd.merge_asof(
        probes.rename(columns={"timestamp": "firstseen"}),
        wd,
        on="firstseen",
        direction="nearest",
    )
    return result


# ---------------------------------------------------------------------------
# Channel color helper
# ---------------------------------------------------------------------------

def _channel_color(channel: int) -> str:
    if 1 <= channel <= 5:   return "#ff4455"   # ch-1 interference zone
    if 6 <= channel <= 10:  return "#ffaa00"   # ch-6 zone
    if 11 <= channel <= 14: return "#00ff9f"   # ch-11 zone
    if channel >= 36:       return "#aa44ff"   # 5 GHz
    return "#888888"


# ---------------------------------------------------------------------------
# Map assembly
# ---------------------------------------------------------------------------

def build_map(
    df: pd.DataFrame,
    poi_df: pd.DataFrame | None = None,
    tracks: list | None = None,
    log_tracks: list | None = None,
    auto_refresh_secs: int = 0,
    center: tuple[float, float] | None = None,
    animate: bool = False,
    probes_df: pd.DataFrame | None = None,
    oui_db: dict | None = None,
    show_channels: bool = False,
) -> folium.Map:
    center_lat = df["currentlatitude"].mean() if center is None else center[0]
    center_lon = df["currentlongitude"].mean() if center is None else center[1]

    m = folium.Map(
        location=[center_lat, center_lon],
        zoom_start=14,
        tiles=None,
        prefer_canvas=True,
    )
    # Basemap. CARTO's dark_matter started requiring an API key (tiles come back
    # stamped "API KEY REQUIRED"), so the default is plain OpenStreetMap tiles
    # darkened client-side with a CSS filter — no key, no account. Users with
    # their own provider can point RFMAP_TILES at any XYZ URL (+ RFMAP_TILES_ATTR).
    custom = os.environ.get("RFMAP_TILES")
    folium.TileLayer(
        tiles=custom or "https://tile.openstreetmap.org/{z}/{x}/{y}.png",
        attr=os.environ.get("RFMAP_TILES_ATTR", "&copy; OpenStreetMap contributors"),
        name="Dark",
        max_zoom=19,
        className="" if custom else "rf-dark",
    ).add_to(m)
    m.get_root().header.add_child(folium.Element(
        "<style>.rf-dark{filter:invert(1) hue-rotate(180deg) brightness(.85) contrast(.9) saturate(.6)}</style>"))

    wifi = df[df["type"] == "WIFI"]
    ble = df[df["type"] == "BLE"]

    # -- Layer 1: WIFI AP density heatmap --
    if not wifi.empty:
        heat_layer = folium.FeatureGroup(name="AP Density Heatmap", show=True)
        HeatMap(
            wifi[["currentlatitude", "currentlongitude"]].values.tolist(),
            radius=12,
            blur=15,
            min_opacity=0.3,
            gradient={0.2: "blue", 0.5: "lime", 0.8: "orange", 1.0: "red"},
        ).add_to(heat_layer)
        heat_layer.add_to(m)

    # -- Layer 2: Clustered WIFI network markers --
    if not wifi.empty:
        network_layer = folium.FeatureGroup(name="WiFi Networks", show=True)
        cluster = MarkerCluster(
            options={"maxClusterRadius": 40, "disableClusteringAtZoom": 17}
        )
        for _, row in wifi.iterrows():
            color = _enc_color(row["authmode"])
            enc = _enc_label(row["authmode"])
            rssi = int(row["rssi"])
            ssid = row["ssid"] or "(hidden)"
            mac = row["mac"]
            count = int(row.get("session_count", 1))
            vendor = lookup_vendor(mac, oui_db) if oui_db else ""
            ch_val = row.get("channel", "")
            ch_str = f"<br>Channel: {int(float(ch_val))}" if ch_val != "" and str(ch_val) not in ("", "nan") else ""
            vendor_str = f"<br><i>{vendor}</i>" if vendor else ""
            popup_html = (
                f"<b>{ssid}</b>{vendor_str}<br>"
                f"MAC: {mac}<br>"
                f"Enc: {enc}<br>"
                f"RSSI: {rssi} dBm"
                f"{ch_str}<br>"
                f"Sessions: {count}"
            )
            folium.CircleMarker(
                location=[row["currentlatitude"], row["currentlongitude"]],
                radius=5,
                color=color,
                fill=True,
                fill_color=color,
                fill_opacity=0.75,
                popup=folium.Popup(popup_html, max_width=200),
                tooltip=ssid,
            ).add_to(cluster)
        cluster.add_to(network_layer)
        network_layer.add_to(m)

    # -- Layer 3: BLE devices --
    if not ble.empty:
        ble_layer = folium.FeatureGroup(name="BLE Devices", show=False)
        ble_cluster = MarkerCluster(
            options={"maxClusterRadius": 30, "disableClusteringAtZoom": 18}
        )
        for _, row in ble.iterrows():
            rssi = int(row["rssi"])
            mac = row["mac"]
            ssid = row["ssid"] or "(unnamed)"
            count = int(row.get("session_count", 1))
            popup_html = (
                f"<b>BLE: {ssid}</b><br>"
                f"MAC: {mac}<br>"
                f"RSSI: {rssi} dBm<br>"
                f"Sessions: {count}"
            )
            folium.CircleMarker(
                location=[row["currentlatitude"], row["currentlongitude"]],
                radius=4,
                color="#a855f7",
                fill=True,
                fill_color="#a855f7",
                fill_opacity=0.65,
                popup=folium.Popup(popup_html, max_width=200),
                tooltip=f"BLE: {ssid or mac}",
            ).add_to(ble_cluster)
        ble_cluster.add_to(ble_layer)
        ble_layer.add_to(m)

    # -- Layer 4: Drive tracks (GPX files + log-derived) --
    all_tracks: list[tuple[str, list]] = []
    if tracks:
        for i, pts in enumerate(tracks):
            all_tracks.append((f"GPX track {i+1}", pts))
    if log_tracks:
        all_tracks.extend(log_tracks)

    if all_tracks:
        track_layer = folium.FeatureGroup(name="Drive Tracks", show=True)
        colors = ["#f59e0b", "#34d399", "#60a5fa", "#f472b6", "#a78bfa", "#fb923c"]
        for i, (name, pts) in enumerate(all_tracks):
            folium.PolyLine(
                pts,
                color=colors[i % len(colors)],
                weight=2,
                opacity=0.7,
                tooltip=name,
            ).add_to(track_layer)
        track_layer.add_to(m)

    # -- Layer 5: POI overlay (optional) --
    if poi_df is not None and not poi_df.empty:
        poi_layer = folium.FeatureGroup(name="POI Overlay", show=True)
        for _, row in poi_df.iterrows():
            folium.Marker(
                location=[row["lat"], row["lon"]],
                popup=folium.Popup(row["label"], max_width=150),
                tooltip=row["label"],
                icon=folium.Icon(color="orange", icon="camera", prefix="fa"),
            ).add_to(poi_layer)
        poi_layer.add_to(m)

    # -- Layer: Channel map --
    if show_channels and "channel" in df.columns:
        ch_df = df[df["type"] == "WIFI"].copy()
        ch_df["channel"] = pd.to_numeric(ch_df["channel"], errors="coerce")
        ch_df = ch_df.dropna(subset=["channel"])
        if not ch_df.empty:
            ch_layer = folium.FeatureGroup(name="Channel Map", show=True)
            for _, row in ch_df.iterrows():
                ch = int(row["channel"])
                color = _channel_color(ch)
                tag = f"{'5GHz' if ch >= 36 else '2.4GHz'} CH{ch}"
                folium.CircleMarker(
                    location=[row["currentlatitude"], row["currentlongitude"]],
                    radius=5,
                    color=color,
                    fill=True,
                    fill_color=color,
                    fill_opacity=0.65,
                    tooltip=tag,
                    popup=folium.Popup(
                        f"<b>{row['ssid'] or '(hidden)'}</b><br>Channel: {ch}<br>"
                        f"<span style='color:{color}'>{tag}</span>",
                        max_width=160,
                    ),
                ).add_to(ch_layer)
            ch_layer.add_to(m)

    # -- Layer: Probe requests --
    if probes_df is not None and not probes_df.empty:
        if "currentlatitude" in probes_df.columns and "currentlongitude" in probes_df.columns:
            # Privacy by design: client devices are NOT individually mapped. Probe
            # requests carry a phone's MAC and the networks it remembers (often the
            # owner's home SSID), so the shipped product only renders anonymous
            # density — one point per unique client, no MACs, no SSIDs, no popups.
            probe_layer = folium.FeatureGroup(name="Client Activity (anonymous)", show=True)
            pts = (probes_df.groupby("mac")[["currentlatitude", "currentlongitude"]]
                   .first().dropna().values.tolist())
            if pts:
                HeatMap(pts, radius=18, blur=22, min_opacity=0.25,
                        gradient={0.3: "#3b0764", 0.6: "#ca8a04", 1.0: "#fde047"}).add_to(probe_layer)
                probe_layer.add_to(m)

    # -- Time animation layer --
    if animate:
        features = extract_animation_features(df)
        if features:
            TimestampedGeoJson(
                {"type": "FeatureCollection", "features": features},
                period="PT2M",
                duration="PT5M",
                auto_play=False,
                loop=False,
                max_speed=20,
                loop_button=True,
                date_options="YYYY-MM-DD HH:mm",
                time_slider_drag_update=True,
                add_last_point=True,
            ).add_to(m)

    # Stats summary in corner
    n_wifi = len(wifi)
    n_ble = len(ble)
    legend_html = f"""
    <div style="position:fixed;bottom:30px;left:30px;z-index:9999;
                background:#1a1a1a;color:#ccc;padding:10px 14px;
                border-radius:6px;font-size:12px;line-height:1.9;
                border:1px solid #444;min-width:140px;">
      <b style="font-size:13px">rfmap</b><br>
      WiFi APs: {n_wifi}<br>
      BLE devices: {n_ble}<br>
      <hr style="border-color:#444;margin:4px 0">
      <span style="color:#e74c3c">&#9679;</span> Open &nbsp;
      <span style="color:#e67e22">&#9679;</span> WEP<br>
      <span style="color:#3498db">&#9679;</span> WPA/WPA2 &nbsp;
      <span style="color:#2ecc71">&#9679;</span> WPA3<br>
      <span style="color:#a855f7">&#9679;</span> BLE &nbsp;
      <span style="color:#ca8a04">&#9679;</span> Client activity
    </div>
    """
    m.get_root().html.add_child(folium.Element(legend_html))
    folium.TileLayer(
        tiles="https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}",
        attr="Esri World Imagery",
        name="Satellite",
        show=False,
    ).add_to(m)

    folium.LayerControl(collapsed=False).add_to(m)

    if auto_refresh_secs > 0:
        refresh_js = f"""
        <script>
        (function() {{
            var INTERVAL = {auto_refresh_secs * 1000};
            var VIEW_KEY = 'rfmap_view';

            // Save view to localStorage before the page reloads
            window.addEventListener('beforeunload', function() {{
                var map = _findMap();
                if (map) {{
                    var c = map.getCenter();
                    localStorage.setItem(VIEW_KEY, JSON.stringify(
                        {{lat: c.lat, lng: c.lng, zoom: map.getZoom()}}
                    ));
                }}
            }});

            // Restore saved view once Leaflet finishes initialising
            var _attempts = 0;
            var _restore = setInterval(function() {{
                var map = _findMap();
                if (map || _attempts++ > 30) {{
                    clearInterval(_restore);
                    if (!map) return;
                    var saved = localStorage.getItem(VIEW_KEY);
                    if (saved) {{
                        try {{
                            var v = JSON.parse(saved);
                            map.setView([v.lat, v.lng], v.zoom, {{animate: false}});
                        }} catch(e) {{}}
                    }}
                }}
            }}, 200);

            function _findMap() {{
                for (var k in window) {{
                    try {{
                        var o = window[k];
                        if (o && o._leaflet_id && typeof o.getCenter === 'function') return o;
                    }} catch(e) {{}}
                }}
                return null;
            }}

            // Reload the page on interval
            setTimeout(function reload() {{
                location.reload();
            }}, INTERVAL);
        }})();
        </script>
        """
        m.get_root().html.add_child(folium.Element(refresh_js))

    return m


# ---------------------------------------------------------------------------
# Watch mode
# ---------------------------------------------------------------------------

def _snapshot(paths: list[Path]) -> dict:
    """Return {path: mtime} for all .log, .csv, and .gpx files in the given paths."""
    result = {}
    for p in paths:
        if p.is_dir():
            for f in p.iterdir():
                if f.suffix in (".log", ".csv", ".gpx"):
                    try:
                        result[f] = f.stat().st_mtime
                    except OSError:
                        pass
        elif p.exists():
            try:
                result[p] = p.stat().st_mtime
            except OSError:
                pass
    return result


def _scan_dir(directory: Path) -> tuple[list[Path], list[Path], list[Path]]:
    """Return (log_files, track_gpx, waypoint_gpx) from a directory."""
    logs, track_gpx, wpt_gpx = [], [], []
    for f in sorted(directory.iterdir()):
        if f.suffix in (".log", ".csv") and f.stat().st_size > 0:
            logs.append(f)
        elif f.suffix == ".gpx":
            try:
                tree = ET.parse(f)
                root = tree.getroot()
                has_trk = bool(_gpx_coords(root, "trkpt"))
                has_wpt = bool(
                    root.findall(f"{{{_GPX_NS['gpx']}}}wpt") or root.findall("wpt")
                )
                if has_trk:
                    track_gpx.append(f)
                if has_wpt:
                    wpt_gpx.append(f)
            except ET.ParseError:
                pass
    return logs, track_gpx, wpt_gpx


def _run_build(
    file_paths: list[Path],
    poi: Path | None,
    out: Path,
    no_dedup: bool,
    auto_refresh_secs: int = 0,
    track_gpx: list[Path] | None = None,
    wpt_gpx: list[Path] | None = None,
):
    print(f"\n[rfmap] Building from {len(file_paths)} log file(s)...")
    df = load_all(file_paths)
    print(f"Total: {len(df)} rows.")
    if not no_dedup:
        before = len(df)
        df = dedup_aps(df)
        print(f"De-duplicated: {before} -> {len(df)} unique devices.")

    # Merge GPX waypoints into POI
    poi_df = None
    frames = []
    if poi:
        frames.append(load_poi(poi))
    if wpt_gpx:
        wpts = load_gpx_waypoints(wpt_gpx)
        if not wpts.empty:
            frames.append(wpts)
            print(f"GPX waypoints: {len(wpts)} points from {len(wpt_gpx)} file(s).")
    if frames:
        poi_df = pd.concat(frames, ignore_index=True)

    gpx_tracks = load_gpx_tracks(track_gpx) if track_gpx else None
    log_tracks = extract_log_tracks(df)
    total_tracks = len(gpx_tracks or []) + len(log_tracks)
    if total_tracks:
        print(f"Drive tracks: {total_tracks} ({len(log_tracks)} from logs, {len(gpx_tracks or [])} from GPX).")

    m = build_map(df, poi_df, tracks=gpx_tracks, log_tracks=log_tracks, auto_refresh_secs=auto_refresh_secs)
    m.save(str(out))
    print(f"[rfmap] Saved -> {out}")


def watch(watch_paths: list[Path], poi: Path | None, out: Path, no_dedup: bool, interval: int = 5):
    import time
    print(f"[rfmap] Watch mode — polling every {interval}s. Ctrl-C to stop.")

    def _do_build():
        dirs = [p for p in watch_paths if p.is_dir()]
        loose = [p for p in watch_paths if not p.is_dir()]
        logs, track_gpx, wpt_gpx = [], [], []
        for d in dirs:
            dl, dt, dw = _scan_dir(d)
            logs += dl; track_gpx += dt; wpt_gpx += dw
        logs += loose
        if logs:
            _run_build(logs, poi, out, no_dedup,
                       auto_refresh_secs=interval,
                       track_gpx=track_gpx, wpt_gpx=wpt_gpx)
        else:
            print("[rfmap] No data files found yet, waiting...")

    last = _snapshot(watch_paths)
    _do_build()

    try:
        while True:
            time.sleep(interval)
            current = _snapshot(watch_paths)
            if current != last:
                added = set(current) - set(last)
                changed = {p for p in current if p in last and current[p] != last[p]}
                if added:
                    print(f"[rfmap] New: {[p.name for p in added]}")
                if changed:
                    print(f"[rfmap] Changed: {[p.name for p in changed]}")
                last = current
                _do_build()
    except KeyboardInterrupt:
        print("\n[rfmap] Stopped.")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Build a local RF map from wardrive logs."
    )
    parser.add_argument(
        "wardrives", nargs="+", type=Path,
        help="Files or a directory to watch (*.log / *.csv)"
    )
    parser.add_argument("--poi", type=Path, default=None,
                        help="Optional POI CSV (lat,lon,label)")
    parser.add_argument("--out", type=Path, default=Path("map.html"),
                        help="Output HTML file (default: map.html)")
    parser.add_argument("--no-dedup", action="store_true",
                        help="Skip MAC de-duplication")
    parser.add_argument("--watch", action="store_true",
                        help="Watch for new/changed files and rebuild automatically")
    parser.add_argument("--interval", type=int, default=5,
                        help="Poll interval in seconds for --watch (default: 5)")
    args = parser.parse_args()

    if args.watch:
        watch(args.wardrives, args.poi, args.out, args.no_dedup, args.interval)
        return

    logs, track_gpx, wpt_gpx = [], [], []
    for p in args.wardrives:
        if p.is_dir():
            dl, dt, dw = _scan_dir(p)
            logs += dl; track_gpx += dt; wpt_gpx += dw
        elif p.exists():
            logs.append(p)
        else:
            sys.exit(f"Not found: {p}")

    if not logs:
        sys.exit("No .log or .csv files found.")

    _run_build(logs, args.poi, args.out, args.no_dedup,
               track_gpx=track_gpx, wpt_gpx=wpt_gpx)


if __name__ == "__main__":
    main()
