"""
BCP RADAR — ハザードリスク分析 API v5
追加: 土地利用タイル・気象庁地震履歴・J-SHIS追加指標
"""
from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
import httpx, asyncio, logging, ssl, math, io
from PIL import Image

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = FastAPI(title="BCP RADAR API", version="5.0.0")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["GET"], allow_headers=["*"])

HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; BCP-RADAR/5.0)"}
TIMEOUT  = httpx.Timeout(30.0, connect=10.0)


def _ssl_ctx():
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    ctx.options |= 0x4  # SSL_OP_LEGACY_SERVER_CONNECT
    return ctx


def new_client():
    return httpx.AsyncClient(timeout=TIMEOUT, verify=_ssl_ctx(), headers=HEADERS, follow_redirects=True)


# ─── タイル座標計算 ───────────────────────────

def tile_pos(lat, lon, z):
    pow2 = 2 ** z
    wx = (lon + 180) / 360 * pow2
    sin_lat = math.sin(math.radians(lat))
    wy = (0.5 - math.log((1 + sin_lat) / (1 - sin_lat)) / (4 * math.pi)) * pow2
    x, y = int(wx), int(wy)
    px, py = int((wx - x) * 256), int((wy - y) * 256)
    return x, y, px, py


# ─── 土地利用凡例 ────────────────────────────

LAND_USE_MAP = {
    (129, 107, 121): "建物用地",
    (184, 237, 169): "田",
    (137, 196, 104): "その他農用地",
    (106, 163,  78): "森林",
    (195, 195, 195): "荒地",
    (255, 255, 153): "ゴルフ場",
    ( 86, 170, 213): "湖沼",
    ( 74, 119, 198): "河川地",
    (255, 255, 255): "海浜",
    ( 74,  74, 198): "海水域",
    (255, 165,   0): "道路",
    (192,  77, 255): "鉄道",
    (255, 100, 100): "その他用地",
}

def land_use_label(r, g, b, a):
    if a < 10:
        return None
    min_dist = float('inf')
    label = "市街地・その他"
    for (cr, cg, cb), name in LAND_USE_MAP.items():
        dist = (r-cr)**2 + (g-cg)**2 + (b-cb)**2
        if dist < min_dist:
            min_dist = dist
            label = name
    return label if min_dist < 3000 else "市街地・その他"


# ─── API取得関数 ──────────────────────────────

async def fetch_geocode(address: str) -> dict:
    async with new_client() as c:
        r = await c.get(f"https://msearch.gsi.go.jp/address-search/AddressSearch?q={address}")
        data = r.json()
    if not data:
        raise ValueError("住所が見つかりません")
    lon, lat = data[0]["geometry"]["coordinates"]
    return {"lat": lat, "lon": lon, "name": data[0]["properties"]["title"]}


async def fetch_elevation(lat, lon) -> float | None:
    try:
        async with new_client() as c:
            r = await c.get(f"https://cyberjapandata2.gsi.go.jp/general/dem/scripts/getelevation.php?lon={lon}&lat={lat}&outtype=JSON")
        d = r.json()
        return d.get("elevation"), d.get("hsrc")  # 標高 + データソース
    except Exception as e:
        logger.warning(f"標高失敗: {e}")
        return None, None


async def fetch_jshis_pshm(lat, lon) -> dict | None:
    try:
        async with new_client() as c:
            r = await c.get(f"https://www.j-shis.bosai.go.jp/map/api/pshm/Y2024/AVR/TTL_MTTL/meshinfo.geojson?position={lon},{lat}&epsg=4326")
        d = r.json()
        if d.get("status") != "Success" or not d.get("features"):
            return None
        p = d["features"][0]["properties"]
        def pct(k): v = p.get(k); return f"{float(v)*100:.1f}%" if v is not None else None
        def fv(k):  v = p.get(k); return f"{float(v):.1f}"      if v is not None else None
        return {
            "i45": pct("T30_I45_PS"), "i50": pct("T30_I50_PS"),
            "i55": pct("T30_I55_PS"), "i60": pct("T30_I60_PS"),
            "si3": fv("T30_P03_SI"),  "si6": fv("T30_P06_SI"),
            "sv3": fv("T30_P03_SV"),  "sv6": fv("T30_P06_SV"),
            # 50年超過確率
            "i50y_02": pct("T50_P02_SI"), "sv50y": fv("T50_P02_SV"),
        }
    except Exception as e:
        logger.warning(f"pshm失敗: {type(e).__name__}: {e}")
        return None


async def fetch_jshis_sstrct(lat, lon) -> dict | None:
    try:
        async with new_client() as c:
            r = await c.get(f"https://www.j-shis.bosai.go.jp/map/api/sstrct/V4/meshinfo.geojson?position={lon},{lat}&epsg=4326")
        d = r.json()
        if d.get("status") != "Success" or not d.get("features"):
            return None
        p = d["features"][0]["properties"]
        return {
            "vs30":       f"{round(float(p['AVS']))} m/s" if p.get("AVS") is not None else None,
            "arv":        f"{float(p['ARV']):.2f}"         if p.get("ARV") is not None else None,
            "micro_topo": p.get("JNAME"),
            "jcode":      p.get("JCODE"),
        }
    except Exception as e:
        logger.warning(f"sstrct失敗: {type(e).__name__}: {e}")
        return None


async def fetch_jshis_landslide(lat, lon) -> dict | None:
    try:
        async with new_client() as c:
            r = await c.get(f"https://www.j-shis.bosai.go.jp/map/api/landslide/isContaining.json?position={lon},{lat}&epsg=4326")
        d = r.json()
        if d.get("status") != "Success":
            return None
        raw = d.get("isContaining", 0)
        return {"is_landslide": raw not in (0, "0", False, None)}
    except Exception as e:
        logger.warning(f"landslide失敗: {type(e).__name__}: {e}")
        return None


async def fetch_flood_depth(lat, lon) -> dict | None:
    try:
        async with new_client() as c:
            r = await c.get(f"http://suiboumap.gsi.go.jp/shinsuimap/Api/Public/GetMaxDepthFromLatlon?lon={lon}&lat={lat}")
        ct = r.headers.get("content-type", "")
        if "html" in ct:
            return None
        data = r.json()
        if not data:
            return {"depth": 0.0, "river": None, "in_zone": False}
        entry = data[0] if isinstance(data, list) else data
        return {"depth": float(entry.get("Depth", 0)), "river": entry.get("EntryRiverName"), "in_zone": True}
    except Exception as e:
        logger.warning(f"浸水ナビ失敗: {type(e).__name__}: {e}")
        return None


async def fetch_land_use(lat, lon) -> dict | None:
    """国土数値情報 200m土地利用メッシュ（タイル解析）"""
    try:
        z = 14
        x, y, px, py = tile_pos(lat, lon, z)
        async with new_client() as c:
            r = await c.get(f"https://cyberjapandata.gsi.go.jp/xyz/lum200k/{z}/{x}/{y}.png")
        if r.status_code != 200:
            return None
        img = Image.open(io.BytesIO(r.content)).convert("RGBA")
        pixel = img.getpixel((px, py))
        label = land_use_label(*pixel)
        return {"label": label, "rgb": pixel[:3]}
    except Exception as e:
        logger.warning(f"土地利用失敗: {type(e).__name__}: {e}")
        return None


async def fetch_recent_quakes() -> list:
    """気象庁 最近の地震リスト（M3以上、最新20件）"""
    try:
        async with new_client() as c:
            r = await c.get("https://www.jma.go.jp/bosai/quake/data/list.json")
        quakes = r.json()
        result = []
        for q in quakes[:100]:
            try:
                mag = float(q.get("mag", 0))
                if mag >= 3.0:
                    result.append({
                        "time":    q.get("at", "")[:19].replace("T", " "),
                        "mag":     mag,
                        "area":    q.get("anm", q.get("en_anm", "")),
                        "depth":   q.get("dep", ""),
                        "max_int": q.get("mxint", ""),
                    })
                    if len(result) >= 10:
                        break
            except:
                continue
        return result
    except Exception as e:
        logger.warning(f"気象庁地震失敗: {type(e).__name__}: {e}")
        return []


# ─── リスクスコア ────────────────────────────

def depth_to_label(depth) -> str:
    if depth is None: return "区域外"
    if depth >= 10: return "10m以上"
    if depth >= 5:  return "5〜10m"
    if depth >= 3:  return "3〜5m"
    if depth >= 1:  return "1〜3m"
    if depth >= 0.5: return "0.5〜1m"
    if depth > 0:   return "0〜0.5m"
    return "区域外"


def score_earthquake(pshm, sstrct) -> dict:
    if not pshm and not sstrct:
        return {"score": 50, "level": "不明"}
    score = 40
    if pshm and pshm.get("i60"):
        p = float(pshm["i60"].replace("%", ""))
        score = 90 if p >= 26 else 72 if p >= 10 else 52 if p >= 3 else 30
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
    score = 95 if depth >= 5 else 82 if depth >= 3 else 65 if depth >= 1 else 45 if depth >= 0.5 else 30
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


# ─── エンドポイント ───────────────────────────

@app.get("/health")
async def health():
    return {"status": "ok", "version": "5.0.0"}


@app.get("/api/geocode")
async def geocode_ep(address: str = Query(...)):
    try:
        return await fetch_geocode(address)
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.get("/api/analyze")
async def analyze(lat: float = Query(...), lon: float = Query(...)):
    if not (20.0 <= lat <= 47.0 and 122.0 <= lon <= 154.0):
        raise HTTPException(status_code=400, detail="日本国内の座標を指定してください")

    (elev, elev_src), pshm, sstrct, landslide, flood, land_use, recent_quakes = await asyncio.gather(
        fetch_elevation(lat, lon),
        fetch_jshis_pshm(lat, lon),
        fetch_jshis_sstrct(lat, lon),
        fetch_jshis_landslide(lat, lon),
        fetch_flood_depth(lat, lon),
        fetch_land_use(lat, lon),
        fetch_recent_quakes(),
    )

    return {
        "coordinate": {"lat": lat, "lon": lon},
        "elevation":  elev,
        "elevation_src": elev_src,
        "jshis": {"pshm": pshm, "sstrct": sstrct, "landslide": landslide},
        "flood": {
            "depth":   flood.get("depth")   if flood else None,
            "label":   depth_to_label(flood.get("depth")) if flood and flood.get("in_zone") else "区域外",
            "river":   flood.get("river")   if flood else None,
            "in_zone": flood.get("in_zone", False) if flood else False,
        },
        "land_use":     land_use,
        "recent_quakes": recent_quakes,
        "scores": {
            "earthquake": score_earthquake(pshm, sstrct),
            "flood":      score_flood(flood, elev),
            "landslide":  score_landslide(landslide, elev),
        },
        "disclaimer": "本データはJ-SHIS（防災科学技術研究所）・浸水ナビ・国土地理院・気象庁の公開データを元に算定した参考情報です。現地調査は実施していないため、実際の状況と乖離が生じる場合があります。",
        "sources": [
            "J-SHIS（防災科学技術研究所）https://www.j-shis.bosai.go.jp/",
            "浸水ナビ（国土地理院）https://suiboumap.gsi.go.jp/",
            "国土数値情報（国土交通省）https://nlftp.mlit.go.jp/",
            "気象庁 https://www.jma.go.jp/",
        ]
    }


@app.get("/api/full")
async def full_analysis(address: str = Query(None), lat: float = Query(None), lon: float = Query(None)):
    if address:
        try:
            geo = await fetch_geocode(address)
            lat, lon, place_name = geo["lat"], geo["lon"], geo["name"]
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
    results = {}
    async def safe(name, coro):
        try:
            v = await coro
            results[name] = {"ok": v is not None and v != (None, None), "value": v}
        except Exception as e:
            results[name] = {"ok": False, "error": f"{type(e).__name__}: {e}"}
    await asyncio.gather(
        safe("elevation",        fetch_elevation(lat, lon)),
        safe("jshis_pshm",       fetch_jshis_pshm(lat, lon)),
        safe("jshis_sstrct",     fetch_jshis_sstrct(lat, lon)),
        safe("jshis_landslide",  fetch_jshis_landslide(lat, lon)),
        safe("flood_depth",      fetch_flood_depth(lat, lon)),
        safe("land_use",         fetch_land_use(lat, lon)),
        safe("recent_quakes",    fetch_recent_quakes()),
    )
    return results
