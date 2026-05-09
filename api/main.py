"""
BCP RADAR — ハザードリスク分析 API サーバー v2
公的データ（J-SHIS・国土地理院・国交省浸水ナビ）へのプロキシ + リスクスコア算定
"""

from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
import httpx
import asyncio
import logging

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = FastAPI(title="BCP RADAR API", version="2.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["GET"],
    allow_headers=["*"],
)

TIMEOUT = httpx.Timeout(30.0, connect=10.0)


# ─── API取得関数 ───────────────────────────────

async def fetch_geocode(address: str, client: httpx.AsyncClient) -> dict:
    url = f"https://msearch.gsi.go.jp/address-search/AddressSearch?q={address}"
    r = await client.get(url)
    r.raise_for_status()
    data = r.json()
    if not data:
        raise ValueError("住所が見つかりません")
    coords = data[0]["geometry"]["coordinates"]
    return {"lat": coords[1], "lon": coords[0], "name": data[0]["properties"]["title"]}


async def fetch_elevation(lat, lon, client) -> float | None:
    try:
        url = f"https://cyberjapandata2.gsi.go.jp/general/dem/scripts/getelevation.php?lon={lon}&lat={lat}&outtype=JSON"
        r = await client.get(url)
        return r.json().get("elevation")
    except Exception as e:
        logger.warning(f"標高失敗: {e}")
        return None


async def fetch_jshis_pshm(lat, lon, client) -> dict | None:
    try:
        url = f"https://www.j-shis.bosai.go.jp/map/api/pshm/Y2024/AVR/TTL_MTTL/meshinfo.geojson?position={lon},{lat}&epsg=4326"
        r = await client.get(url)
        d = r.json()
        if d.get("status") != "Success" or not d.get("features"):
            logger.warning(f"J-SHIS pshm: {d.get('status')} / {d.get('error')}")
            return None
        p = d["features"][0]["properties"]
        return {
            "i45": f"{p['T30_I45_PS']*100:.1f}%" if p.get("T30_I45_PS") is not None else None,
            "i50": f"{p['T30_I50_PS']*100:.1f}%" if p.get("T30_I50_PS") is not None else None,
            "i55": f"{p['T30_I55_PS']*100:.1f}%" if p.get("T30_I55_PS") is not None else None,
            "i60": f"{p['T30_I60_PS']*100:.1f}%" if p.get("T30_I60_PS") is not None else None,
            "si3": f"{p['T30_P03_SI']:.1f}"       if p.get("T30_P03_SI") is not None else None,
        }
    except Exception as e:
        logger.warning(f"J-SHIS pshm失敗: {type(e).__name__}: {e}")
        return None


async def fetch_jshis_sstrct(lat, lon, client) -> dict | None:
    try:
        url = f"https://www.j-shis.bosai.go.jp/map/api/sstrct/V4/meshinfo.geojson?position={lon},{lat}&epsg=4326"
        r = await client.get(url)
        d = r.json()
        if d.get("status") != "Success" or not d.get("features"):
            return None
        p = d["features"][0]["properties"]
        return {
            "vs30":       f"{round(p['AVS'])} m/s" if p.get("AVS") is not None else None,
            "arv":        f"{p['ARV']:.2f}"         if p.get("ARV") is not None else None,
            "micro_topo": p.get("JNAME"),
        }
    except Exception as e:
        logger.warning(f"J-SHIS sstrct失敗: {type(e).__name__}: {e}")
        return None


async def fetch_jshis_landslide(lat, lon, client) -> dict | None:
    try:
        url = f"https://www.j-shis.bosai.go.jp/map/api/landslide/isContaining.json?position={lon},{lat}&epsg=4326"
        r = await client.get(url)
        d = r.json()
        if d.get("status") != "Success":
            return None
        raw = d.get("isContaining", 0)
        return {"is_landslide": raw not in (0, "0", False, None)}
    except Exception as e:
        logger.warning(f"J-SHIS landslide失敗: {type(e).__name__}: {e}")
        return None


async def fetch_flood_depth(lat, lon, client) -> dict | None:
    """
    浸水ナビ API（国土地理院）
    洪水最大浸水深取得: GetMaxDepthFromLatlon
    """
    try:
        url = f"https://suiboumap.gsi.go.jp/shinsuimap/Api/Public/GetMaxDepthFromLatlon?lon={lon}&lat={lat}"
        r = await client.get(url)
        ct = r.headers.get("content-type", "")
        if "html" in ct:
            logger.warning("浸水ナビ: HTMLが返却")
            return None
        data = r.json()
        if not data:
            return {"depth": 0.0, "river": None, "in_zone": False}
        entry = data[0] if isinstance(data, list) else data
        return {
            "depth":   float(entry.get("Depth", 0)),
            "river":   entry.get("EntryRiverName"),
            "in_zone": True,
        }
    except Exception as e:
        logger.warning(f"浸水ナビ失敗: {type(e).__name__}: {e}")
        return None


async def fetch_flood_start_time(lat, lon, client) -> dict | None:
    """浸水ナビ: 洪水最短浸水開始時間取得"""
    try:
        url = f"https://suiboumap.gsi.go.jp/shinsuimap/Api/Public/GetMinStartTime?lon={lon}&lat={lat}"
        r = await client.get(url)
        ct = r.headers.get("content-type", "")
        if "html" in ct:
            return None
        data = r.json()
        if not data:
            return None
        entry = data[0] if isinstance(data, list) else data
        return {"start_time_min": entry.get("StartTime")}
    except Exception as e:
        logger.warning(f"浸水開始時間失敗: {type(e).__name__}: {e}")
        return None


# ─── リスクスコア算定 ────────────────────────────

def depth_to_label(depth: float | None) -> str:
    if depth is None: return "区域外"
    if depth >= 10:   return "10m以上"
    if depth >= 5:    return "5〜10m"
    if depth >= 3:    return "3〜5m"
    if depth >= 1:    return "1〜3m"
    if depth >= 0.5:  return "0.5〜1m"
    if depth > 0:     return "0〜0.5m"
    return "区域外"


def score_earthquake(pshm, sstrct) -> dict:
    if not pshm and not sstrct:
        return {"score": 50, "level": "不明"}
    score = 40
    if pshm and pshm.get("i60"):
        p = float(pshm["i60"].replace("%", ""))
        if p >= 26:   score = 90
        elif p >= 10: score = 72
        elif p >= 3:  score = 52
        else:         score = 30
    if sstrct and sstrct.get("vs30"):
        vs = int(sstrct["vs30"].replace(" m/s", ""))
        if vs < 150:   score = min(100, score + 12)
        elif vs < 300: score = min(100, score + 5)
    return {"score": score, "level": "高" if score >= 70 else "中" if score >= 45 else "低"}


def score_flood(flood, elev) -> dict:
    if not flood:
        return {"score": 30, "level": "不明"}
    if not flood.get("in_zone"):
        score = 20 if (elev is not None and elev < 2) else 10
        return {"score": score, "level": "低"}
    depth = flood.get("depth", 0)
    if depth >= 5:     score = 95
    elif depth >= 3:   score = 82
    elif depth >= 1:   score = 65
    elif depth >= 0.5: score = 45
    else:              score = 30
    if elev is not None and elev < 2:
        score = min(100, score + 8)
    return {"score": score, "level": "高" if score >= 70 else "中" if score >= 40 else "低"}


def score_landslide(landslide, elev) -> dict:
    score = 10
    if landslide and landslide.get("is_landslide"):
        score = 60
    elif elev is not None and elev > 100:
        score = 25
    return {"score": score, "level": "中" if score >= 50 else "低"}


# ─── エンドポイント ──────────────────────────────

@app.get("/health")
async def health():
    return {"status": "ok", "version": "2.0.0"}


@app.get("/api/geocode")
async def geocode(address: str = Query(...)):
    async with httpx.AsyncClient(timeout=TIMEOUT, verify=False) as client:
        try:
            return await fetch_geocode(address, client)
        except Exception as e:
            raise HTTPException(status_code=400, detail=str(e))


@app.get("/api/analyze")
async def analyze(lat: float = Query(...), lon: float = Query(...)):
    if not (20.0 <= lat <= 47.0 and 122.0 <= lon <= 154.0):
        raise HTTPException(status_code=400, detail="日本国内の座標を指定してください")

    async with httpx.AsyncClient(timeout=TIMEOUT, verify=False) as client:
        elev, pshm, sstrct, landslide, flood, flood_start = await asyncio.gather(
            fetch_elevation(lat, lon, client),
            fetch_jshis_pshm(lat, lon, client),
            fetch_jshis_sstrct(lat, lon, client),
            fetch_jshis_landslide(lat, lon, client),
            fetch_flood_depth(lat, lon, client),
            fetch_flood_start_time(lat, lon, client),
        )

    return {
        "coordinate": {"lat": lat, "lon": lon},
        "elevation":  elev,
        "jshis": {
            "pshm":      pshm,
            "sstrct":    sstrct,
            "landslide": landslide,
        },
        "flood": {
            "depth":          flood.get("depth")          if flood else None,
            "label":          depth_to_label(flood.get("depth")) if flood and flood.get("in_zone") else "区域外",
            "river":          flood.get("river")          if flood else None,
            "in_zone":        flood.get("in_zone", False) if flood else False,
            "start_time_min": flood_start.get("start_time_min") if flood_start else None,
        },
        "scores": {
            "earthquake": score_earthquake(pshm, sstrct),
            "flood":      score_flood(flood, elev),
            "landslide":  score_landslide(landslide, elev),
        },
        "disclaimer": "本データはJ-SHIS（防災科学技術研究所）・浸水ナビ（国土地理院）の公開データを元に算定した参考情報です。現地調査は実施していないため、実際の状況と乖離が生じる場合があります。",
        "sources": [
            "J-SHIS（防災科学技術研究所） https://www.j-shis.bosai.go.jp/",
            "浸水ナビ（国土地理院） https://suiboumap.gsi.go.jp/",
            "国土地理院 https://www.gsi.go.jp/",
        ]
    }


@app.get("/api/full")
async def full_analysis(
    address: str = Query(None),
    lat: float = Query(None),
    lon: float = Query(None),
):
    async with httpx.AsyncClient(timeout=TIMEOUT, verify=False) as client:
        if address:
            try:
                geo = await fetch_geocode(address, client)
                lat, lon = geo["lat"], geo["lon"]
                place_name = geo["name"]
            except Exception as e:
                raise HTTPException(status_code=400, detail=str(e))
        elif lat is not None and lon is not None:
            place_name = f"緯度{lat:.5f}, 経度{lon:.5f}"
        else:
            raise HTTPException(status_code=400, detail="address または lat/lon を指定してください")

    result = await analyze(lat=lat, lon=lon)
    result["place_name"] = place_name
    return result


@app.get("/api/debug")
async def debug(lat: float = 35.731, lon: float = 139.795):
    """各APIの疎通確認（デフォルト: 荒川区南千住）"""
    async with httpx.AsyncClient(timeout=httpx.Timeout(20.0), verify=False) as client:
        elev, pshm, sstrct, ls, flood, ft = await asyncio.gather(
            fetch_elevation(lat, lon, client),
            fetch_jshis_pshm(lat, lon, client),
            fetch_jshis_sstrct(lat, lon, client),
            fetch_jshis_landslide(lat, lon, client),
            fetch_flood_depth(lat, lon, client),
            fetch_flood_start_time(lat, lon, client),
        )
    return {
        "elevation":      {"ok": elev  is not None, "value": elev},
        "jshis_pshm":     {"ok": pshm  is not None, "value": pshm},
        "jshis_sstrct":   {"ok": sstrct is not None, "value": sstrct},
        "jshis_landslide":{"ok": ls    is not None, "value": ls},
        "flood_depth":    {"ok": flood is not None, "value": flood},
        "flood_start":    {"ok": ft    is not None, "value": ft},
    }
