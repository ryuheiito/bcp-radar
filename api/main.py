"""
BCP RADAR — ハザードリスク分析 API サーバー
公的データ（J-SHIS・国土地理院・国交省）へのプロキシ + リスクスコア算定
"""

from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
import httpx
import asyncio
import math
import io
from PIL import Image
import logging

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = FastAPI(title="BCP RADAR API", version="1.0.0")

# CORS設定（フロントエンドのオリジンを許可）
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # 本番では特定ドメインに絞る
    allow_methods=["GET"],
    allow_headers=["*"],
)

TIMEOUT = httpx.Timeout(30.0, connect=10.0)


# ──────────────────────────────────────
# ユーティリティ
# ──────────────────────────────────────

def lat_lon_to_tile(lat: float, lon: float, zoom: int):
    """緯度経度 → タイル座標 + ピクセル位置"""
    pow2 = 2 ** zoom
    wx = (lon + 180) / 360 * pow2
    sin_lat = math.sin(math.radians(lat))
    wy = (0.5 - math.log((1 + sin_lat) / (1 - sin_lat)) / (4 * math.pi)) * pow2
    tx, ty = int(wx), int(wy)
    px, py = int((wx - tx) * 256), int((wy - ty) * 256)
    return tx, ty, px, py


def flood_color_to_risk(r, g, b, a) -> dict:
    """洪水タイルのピクセル色 → 浸水深・リスクレベル"""
    if a < 5:
        return {"depth": 0, "label": "区域外", "risk": "低"}
    if r < 80 and g < 80 and b > 160:
        return {"depth": 10, "label": "10m以上", "risk": "高"}
    if r > 160 and g < 60 and b < 60:
        return {"depth": 7,  "label": "5〜10m",  "risk": "高"}
    if r > 200 and g < 100 and b < 100:
        return {"depth": 4,  "label": "3〜5m",   "risk": "高"}
    if r > 200 and 80 < g < 160 and b < 80:
        return {"depth": 2,  "label": "1〜3m",   "risk": "中"}
    if r > 200 and g > 160 and b < 80:
        return {"depth": 0.7, "label": "0.5〜1m", "risk": "中"}
    if r > 220 and g > 210 and b < 140:
        return {"depth": 0.3, "label": "0〜0.5m", "risk": "低"}
    if a > 20:
        return {"depth": 0.5, "label": "浸水想定域", "risk": "中"}
    return {"depth": 0, "label": "区域外", "risk": "低"}


def naisui_color_to_risk(r, g, b, a) -> dict:
    return flood_color_to_risk(r, g, b, a)


def hightide_color_to_risk(r, g, b, a) -> dict:
    if a < 5:
        return {"depth": 0, "label": "区域外", "risk": "低"}
    if r < 80 and g < 100 and b > 160:
        return {"depth": 5,   "label": "5m以上",  "risk": "高"}
    if r > 160 and g < 80 and b < 80:
        return {"depth": 3,   "label": "3〜5m",   "risk": "高"}
    if r > 200 and g > 80 and b < 80:
        return {"depth": 2,   "label": "1〜3m",   "risk": "中"}
    if r > 200 and g > 180 and b < 80:
        return {"depth": 0.5, "label": "0.5〜1m", "risk": "中"}
    if a > 20:
        return {"depth": 0.5, "label": "高潮浸水想定域", "risk": "中"}
    return {"depth": 0, "label": "区域外", "risk": "低"}


def tsunami_color_to_risk(r, g, b, a) -> dict:
    if a < 5:
        return {"depth": 0, "label": "想定域外", "risk": "低"}
    if r > 180 and g < 60 and b < 60:
        return {"depth": 10, "label": "10m以上", "risk": "高"}
    if r > 200 and g < 120 and b < 80:
        return {"depth": 5,  "label": "5〜10m",  "risk": "高"}
    if r > 200 and g > 100 and b < 80:
        return {"depth": 3,  "label": "3〜5m",   "risk": "高"}
    if r > 200 and g > 180 and b < 80:
        return {"depth": 1.5, "label": "1〜3m",  "risk": "中"}
    if a > 20:
        return {"depth": 0.5, "label": "津波浸水想定域", "risk": "中"}
    return {"depth": 0, "label": "想定域外", "risk": "低"}


def dosa_color_to_risk(r, g, b, a) -> dict:
    if a < 5:
        return {"in_zone": False, "label": "区域外", "risk": "低"}
    if r > 160 and g < 80:
        return {"in_zone": True, "label": "特別警戒区域", "risk": "高"}
    if r > 120 and g < 140:
        return {"in_zone": True, "label": "警戒区域",     "risk": "中"}
    if a > 20:
        return {"in_zone": True, "label": "警戒区域",     "risk": "中"}
    return {"in_zone": False, "label": "区域外", "risk": "低"}


def calc_score(risk_label: str, base_high=85, base_mid=50, base_low=10) -> int:
    return {"高": base_high, "中": base_mid, "低": base_low}.get(risk_label, 40)


# ──────────────────────────────────────
# 外部API取得関数
# ──────────────────────────────────────

async def fetch_geocode(address: str, client: httpx.AsyncClient) -> dict:
    url = f"https://msearch.gsi.go.jp/address-search/AddressSearch?q={address}"
    r = await client.get(url)
    r.raise_for_status()
    data = r.json()
    if not data:
        raise ValueError("住所が見つかりません")
    coords = data[0]["geometry"]["coordinates"]
    return {"lat": coords[1], "lon": coords[0], "name": data[0]["properties"]["title"]}


async def fetch_elevation(lat: float, lon: float, client: httpx.AsyncClient) -> float | None:
    try:
        url = f"https://cyberjapandata2.gsi.go.jp/general/dem/scripts/getelevation.php?lon={lon}&lat={lat}&outtype=JSON"
        r = await client.get(url)
        data = r.json()
        return data.get("elevation")
    except Exception as e:
        logger.warning(f"標高取得失敗: {e}")
        return None


async def fetch_jshis_pshm(lat: float, lon: float, client: httpx.AsyncClient) -> dict | None:
    try:
        url = f"https://www.j-shis.bosai.go.jp/map/api/pshm/Y2024/AVR/TTL_MTTL/meshinfo.geojson?position={lon},{lat}&epsg=4326"
        r = await client.get(url)
        data = r.json()
        if data.get("status") != "Success" or not data.get("features"):
            return None
        p = data["features"][0]["properties"]
        return {
            "i45": f"{p['T30_I45_PS']*100:.1f}%" if p.get("T30_I45_PS") is not None else None,
            "i50": f"{p['T30_I50_PS']*100:.1f}%" if p.get("T30_I50_PS") is not None else None,
            "i55": f"{p['T30_I55_PS']*100:.1f}%" if p.get("T30_I55_PS") is not None else None,
            "i60": f"{p['T30_I60_PS']*100:.1f}%" if p.get("T30_I60_PS") is not None else None,
            "si3": f"{p['T30_P03_SI']:.1f}" if p.get("T30_P03_SI") is not None else None,
            "sv6": f"{p['T30_P06_SV']:.0f} cm/s" if p.get("T30_P06_SV") is not None else None,
        }
    except Exception as e:
        logger.warning(f"J-SHIS pshm取得失敗: {type(e).__name__}: {e}")
        return None


async def fetch_jshis_sstrct(lat: float, lon: float, client: httpx.AsyncClient) -> dict | None:
    try:
        url = f"https://www.j-shis.bosai.go.jp/map/api/sstrct/V4/meshinfo.geojson?position={lon},{lat}&epsg=4326"
        r = await client.get(url)
        data = r.json()
        if data.get("status") != "Success" or not data.get("features"):
            return None
        p = data["features"][0]["properties"]
        return {
            "vs30":       f"{round(p['AVS'])} m/s" if p.get("AVS") is not None else None,
            "arv":        f"{p['ARV']:.2f}"         if p.get("ARV") is not None else None,
            "micro_topo": p.get("JNAME"),
        }
    except Exception as e:
        logger.warning(f"J-SHIS sstrct取得失敗: {type(e).__name__}: {e}")
        return None


async def fetch_jshis_landslide(lat: float, lon: float, client: httpx.AsyncClient) -> dict | None:
    try:
        url = f"https://www.j-shis.bosai.go.jp/map/api/landslide/isContaining.json?position={lon},{lat}&epsg=4326"
        r = await client.get(url)
        data = r.json()
        if data.get("status") != "Success":
            return None
        raw = data.get("isContaining", 0)
        return {"is_landslide": bool(raw) and raw not in (0, "0", False)}
    except Exception as e:
        logger.warning(f"J-SHIS landslide取得失敗: {e}")
        return None


async def fetch_hazard_tile(url_tpl: str, lat: float, lon: float, client: httpx.AsyncClient, zoom: int = 17) -> dict | None:
    """国交省WMTSタイルをピクセル解析"""
    try:
        tx, ty, px, py = lat_lon_to_tile(lat, lon, zoom)
        url = url_tpl.format(z=zoom, x=tx, y=ty)
        r = await client.get(url)
        if r.status_code != 200:
            return None
        img = Image.open(io.BytesIO(r.content)).convert("RGBA")
        pixel = img.getpixel((px, py))
        return {"r": pixel[0], "g": pixel[1], "b": pixel[2], "a": pixel[3]}
    except Exception as e:
        logger.warning(f"タイル取得失敗 {url_tpl[:50]}: {e}")
        return None


# 国交省 WMTSタイルURL（正式エンドポイント）
TILE_URLS = {
    "flood":    "https://disaportaldata.gsi.go.jp/raster/01_flood_l2_shinsuishin_data/{z}/{x}/{y}.png",
    "naisui":   "https://disaportaldata.gsi.go.jp/raster/02_naisui_data/{z}/{x}/{y}.png",
    "hightide": "https://disaportaldata.gsi.go.jp/raster/03_hightide_l2_shinsuishin_data/{z}/{x}/{y}.png",
    "tsunami":  "https://disaportaldata.gsi.go.jp/raster/04_tsunami_newlegend_data/{z}/{x}/{y}.png",
    "doseki":   "https://disaportaldata.gsi.go.jp/raster/05_dosekiryukeikaikuiki/{z}/{x}/{y}.png",
    "kyukei":   "https://disaportaldata.gsi.go.jp/raster/05_kyukeishakeikaikuiki/{z}/{x}/{y}.png",
    "jisuberi": "https://disaportaldata.gsi.go.jp/raster/05_jisuberikeikaikuiki/{z}/{x}/{y}.png",
}

COLOR_PARSERS = {
    "flood":    flood_color_to_risk,
    "naisui":   naisui_color_to_risk,
    "hightide": hightide_color_to_risk,
    "tsunami":  tsunami_color_to_risk,
    "doseki":   dosa_color_to_risk,
    "kyukei":   dosa_color_to_risk,
    "jisuberi": dosa_color_to_risk,
}


# ──────────────────────────────────────
# エンドポイント
# ──────────────────────────────────────

@app.get("/health")
async def health():
    return {"status": "ok"}



@app.get("/api/debug")
async def debug(lat: float = 35.6555, lon: float = 139.7454):
    """J-SHISへの接続を詳細デバッグ"""
    results = {}
    
    # SSL検証あり
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(30.0)) as client:
            url = f"https://www.j-shis.bosai.go.jp/map/api/pshm/Y2024/AVR/TTL_MTTL/meshinfo.geojson?position={lon},{lat}&epsg=4326"
            r = await client.get(url)
            d = r.json()
            results["pshm_ssl_on"] = {
                "status": d.get("status"),
                "http_status": r.status_code,
                "error": d.get("error"),
                "has_data": bool(d.get("features") and d["features"][0].get("properties"))
            }
    except Exception as e:
        results["pshm_ssl_on"] = {"error": f"{type(e).__name__}: {e}"}

    # SSL検証オフ
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(30.0), verify=False) as client:
            url = f"https://www.j-shis.bosai.go.jp/map/api/pshm/Y2024/AVR/TTL_MTTL/meshinfo.geojson?position={lon},{lat}&epsg=4326"
            r = await client.get(url)
            d = r.json()
            results["pshm_ssl_off"] = {
                "status": d.get("status"),
                "http_status": r.status_code,
                "has_data": bool(d.get("features") and d["features"][0].get("properties"))
            }
    except Exception as e:
        results["pshm_ssl_off"] = {"error": f"{type(e).__name__}: {e}"}

    # タイル取得テスト
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(30.0), verify=False) as client:
            url = "https://disaportaldata.gsi.go.jp/raster/04_tsunami_newlegend_data/17/116415/51624.png"
            r = await client.get(url)
            results["tsunami_tile"] = {
                "http_status": r.status_code,
                "content_type": r.headers.get("content-type"),
                "size": len(r.content)
            }
    except Exception as e:
        results["tsunami_tile"] = {"error": f"{type(e).__name__}: {e}"}

    return results


@app.get("/api/geocode")
async def geocode(address: str = Query(..., description="住所")):
    async with httpx.AsyncClient(timeout=TIMEOUT) as client:
        try:
            return await fetch_geocode(address, client)
        except Exception as e:
            raise HTTPException(status_code=400, detail=str(e))


@app.get("/api/analyze")
async def analyze(
    lat: float = Query(..., description="緯度"),
    lon: float = Query(..., description="経度"),
):
    """
    指定座標の全ハザードデータを取得・算定して返す
    """
    if not (20.0 <= lat <= 47.0 and 122.0 <= lon <= 154.0):
        raise HTTPException(status_code=400, detail="日本国内の座標を指定してください（緯度20〜47、経度122〜154）")

    async with httpx.AsyncClient(timeout=TIMEOUT, verify=False) as client:

        # 並列取得（標高・J-SHIS 3種）
        elev_task     = fetch_elevation(lat, lon, client)
        pshm_task     = fetch_jshis_pshm(lat, lon, client)
        sstrct_task   = fetch_jshis_sstrct(lat, lon, client)
        landslide_task = fetch_jshis_landslide(lat, lon, client)

        elev, pshm, sstrct, landslide = await asyncio.gather(
            elev_task, pshm_task, sstrct_task, landslide_task
        )

        # 国交省タイル（並列取得）
        tile_tasks = {
            key: fetch_hazard_tile(url, lat, lon, client)
            for key, url in TILE_URLS.items()
        }
        tile_pixels = dict(zip(
            tile_tasks.keys(),
            await asyncio.gather(*tile_tasks.values())
        ))

    # ピクセル → リスク変換
    hazard_tiles = {}
    for key, px in tile_pixels.items():
        if px:
            hazard_tiles[key] = COLOR_PARSERS[key](px["r"], px["g"], px["b"], px["a"])
        else:
            hazard_tiles[key] = None

    # リスクスコア算定
    def eq_score():
        if not pshm:
            return {"score": 50, "level": "不明"}
        score = 40
        if pshm.get("i60"):
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

    def tile_score(key, elev_bonus=False):
        t = hazard_tiles.get(key)
        if not t:
            return {"score": 30, "level": "不明"}
        score = calc_score(t.get("risk", "低"), 82, 50, 10)
        if elev_bonus and elev is not None and elev < 2:
            score = min(100, score + 8)
        return {"score": score, "level": "高" if score >= 65 else "中" if score >= 35 else "低"}

    def dosa_score():
        score = 10
        for key in ["doseki", "kyukei", "jisuberi"]:
            t = hazard_tiles.get(key)
            if t and t.get("in_zone"):
                score = 75
                break
        if score < 75 and landslide and landslide.get("is_landslide"):
            score = 55
        return {"score": score, "level": "中" if score >= 55 else "低"}

    scores = {
        "earthquake": eq_score(),
        "flood":      tile_score("flood", elev_bonus=True),
        "naisui":     tile_score("naisui"),
        "hightide":   tile_score("hightide"),
        "tsunami":    tile_score("tsunami"),
        "landslide":  dosa_score(),
    }

    return {
        "coordinate": {"lat": lat, "lon": lon},
        "elevation":  elev,
        "jshis": {
            "pshm":      pshm,
            "sstrct":    sstrct,
            "landslide": landslide,
        },
        "hazard_tiles": hazard_tiles,
        "scores":       scores,
        "disclaimer":   "本データはJ-SHIS（防災科学技術研究所）・ハザードマップポータルサイト（国土交通省）・国土地理院の公開データを元に算定した参考情報です。現地調査は実施していないため、実際の状況と乖離が生じる場合があります。",
        "sources": [
            "J-SHIS（防災科学技術研究所） https://www.j-shis.bosai.go.jp/",
            "ハザードマップポータルサイト（国土交通省） https://disaportal.gsi.go.jp/",
            "国土地理院 https://www.gsi.go.jp/",
        ]
    }


@app.get("/api/full")
async def full_analysis(
    address: str = Query(None, description="住所（address か lat/lon のいずれか必須）"),
    lat: float = Query(None),
    lon: float = Query(None),
):
    """住所 or 緯度経度を受け取り、ジオコーディング → 全分析を一括実行"""
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
