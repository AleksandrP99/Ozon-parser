#parse_ozon.py — сбор данных о товарах Ozon по списку SKU.

import os, re, csv, json, time, random, logging
from typing import Any, Dict, List, Optional

import requests
from dotenv import load_dotenv
from playwright.sync_api import sync_playwright

load_dotenv()
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

COOKIES_STATE = os.getenv("COOKIES_STATE", "cookies_state.json")
COOKIES_FILE = os.getenv("COOKIES_FILE", "cookies.json")
CSV_PATH = os.getenv("CSV_PATH", "ozon_products.csv")

FIELDS = ["sku", "title", "price", "rating", "reviews_total",
          "cover_image", "photos_seller", "videos_seller",
          "color", "material", "art_set", "has_rich_content", "error"]

# У одной характеристики в разных категориях бывают разные названия.
CHAR_MAP = {
    "color": ["цвет", "color"],
    "material": ["материал", "material"],
    "art_set": ["артикул", "артикул производителя", "артикул товара",
                "комплектация", "партномер", "модель"],
}

RICH_TAGS = re.compile(r"<(img|table|ul|ol)\b", re.I)


def to_num(v, cast=float):
    #Приводит значение к числу.
    if v is None:
        return None
    if isinstance(v, (int, float)):
        return cast(v)
    cleaned = re.sub(r"[^\d,.]", "", str(v)).replace(",", ".")
    if not cleaned:
        return None
    try:
        return cast(float(cleaned)) if cast is int else float(cleaned)
    except (ValueError, TypeError):
        return None


def extract_text(obj):
    #Достаёт текст
    if isinstance(obj, str):
        return obj
    if not isinstance(obj, dict):
        return None
    if obj.get("content") or obj.get("text"):
        return obj.get("content") or obj.get("text")
    trs = obj.get("textRs") or []
    return "".join(t.get("content", "") for t in trs).strip() or None


def first_widget(states, *prefixes):
    return next((v for p in prefixes for k, v in states.items() if k.startswith(p)), None)


def empty_row(sku, error=None):
    #Пустая строка результата для всех веток с ошибкой
    return {**{f: None for f in FIELDS}, "sku": sku, "has_rich_content": False, "error": error}


def unpack(v):
    if isinstance(v, str) and v.startswith(("{", "[")):
        try:
            return json.loads(v)
        except json.JSONDecodeError:
            pass
    return v



def find_widget_states(obj, depth=0):
    #Рекурсивно ищет dict с ключом widgetStates
    if depth > 12 or not isinstance(obj, (dict, list)):
        return None
    if isinstance(obj, dict) and isinstance(obj.get("widgetStates"), dict):
        return obj["widgetStates"]
    items = obj.values() if isinstance(obj, dict) else obj
    for v in items:
        found = find_widget_states(v, depth + 1)
        if found:
            return found
    return None


_JSON_SOURCES = [
    (re.compile(r'<script[^>]*type="application/json"[^>]*>(.*?)</script>', re.S), "script[type=json]"),
    (re.compile(r'<script[^>]*id="__NEXT_DATA__"[^>]*>(.*?)</script>', re.S), "__NEXT_DATA__"),
]


def extract_json_from_html(html):
    for rx, name in _JSON_SOURCES:
        for m in rx.finditer(html):
            try:
                data = json.loads(m.group(1).strip())
            except json.JSONDecodeError:
                continue
            if find_widget_states(data):
                log.info("JSON найден в %s", name)
                return data
    return None



def extract_characteristics(states) -> Dict[str, str]:
    chars: Dict[str, str] = {}

    w = first_widget(states, "webShortCharacteristics-", "webCharacteristics-") or {}
    for a in w.get("characteristics") or w.get("attributes") or []:
        name = str(extract_text(a.get("title")) or extract_text(a.get("name")) or "").strip().lower()
        if not name:
            continue
        values = a.get("values") or []
        v0 = values[0] if values else a.get("value")
        val = extract_text(v0) if isinstance(v0, dict) else v0
        chars[name] = str(val).strip() if val else ""

    w = first_widget(states, "webAspects-") or {}
    for a in w.get("aspects") or []:
        name = (a.get("aspectName") or "").strip().lower()
        if not name or name in chars:
            continue
        value = "".join(d.get("content") or "" for d in a.get("descriptionRs") or [])
        # Чистим HTML и пробелы
        value = value.replace(f"{a.get('aspectName')}:", "").strip()
        if value:
            chars[name] = value

    return chars


def extract_data(sku: str, states: Optional[dict]) -> Dict[str, Any]:
    #Собирает все поля товара из widgetStates
    result = empty_row(sku)
    if not states:
        result["error"] = "no_data"
        return result

    states = {k: unpack(v) for k, v in states.items()}

    w = first_widget(states, "webProductHeading-") or {}
    t = w.get("title") or w.get("name")
    t = extract_text(t) if isinstance(t, dict) else t
    result["title"] = str(t).strip() if t else None

    w = first_widget(states, "webPrice-") or {}
    raw = w.get("price")
    if raw is None: raw = w.get("cardPrice")
    if raw is None: raw = w.get("originalPrice")
    result["price"] = to_num(raw)

    w = first_widget(states, "webReviewProductScore-", "webReviewScore-") or {}
    result["rating"] = to_num(w.get("totalScore") or w.get("score"))
    result["reviews_total"] = to_num(w.get("reviewsCount") or w.get("reviewCount"), int)

    w = first_widget(states, "webGallery-") or {}
    imgs = w.get("images") or w.get("photos") or []
    if imgs:
        result["photos_seller"] = len(imgs)
        first = imgs[0]
        result["cover_image"] = (first if isinstance(first, str)
                                 else (first.get("image") or first.get("url") or first.get("src")))
    result["videos_seller"] = len(w.get("videos") or w.get("videoList") or [])

    chars = extract_characteristics(states)
    for field, keys in CHAR_MAP.items():
        for k in keys:
            if chars.get(k):
                result[field] = chars[k]
                break

    # has_rich_content: ищем HTML-теги только в виджетах описания,
    # иначе поймаем <img> из галереи и получим ложное True.
    desc = [json.dumps(v, ensure_ascii=False) for k, v in states.items()
            if any(x in k.lower() for x in ("description", "annotation", "textblock", "customhtml"))]
    if desc:
        result["has_rich_content"] = bool(RICH_TAGS.search("".join(desc)))

    return result



def make_session() -> requests.Session:
    #requests.Session с cookies из cookies.json.
    session = requests.Session()
    session.headers.update({
        "Accept": "application/json",
        "x-o3-app-name": "dweb_client",
        "x-o3-app-version": "release",
        "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                       "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"),
    })
    if os.path.exists(COOKIES_FILE):
        try:
            with open(COOKIES_FILE, encoding="utf-8") as f:
                for c in json.load(f):
                    session.cookies.set(c["name"], c["value"],
                                        domain=c.get("domain"), path=c.get("path", "/"))
            log.info("cookies в requests.Session: %d шт.", len(session.cookies))
        except Exception as e:
            log.warning("cookies.json не прочитан: %s", e)
    return session


def fetch_via_requests(session, sku):
    #composer-api напрямую через requests.Session.
    #Ozon обычно возвращает 403
    try:
        r = session.get(
            f"https://www.ozon.ru/api/composer-api.bx/page/json/v2?url=/product/{sku}/",
            timeout=30)
        r.raise_for_status()
        states = r.json().get("widgetStates")
        if states:
            log.info("SKU %s: данные из composer-api (requests)", sku)
            return states
    except Exception as e:
        log.warning("requests к composer-api: %s", e)
    return None


def fetch_via_browser(page, sku):
    # composer-api через fetch внутри браузера.
    try:
        result = page.evaluate(
            """async (path) => {
                const r = await fetch(path, {
                    credentials: 'include',
                    headers: {'Accept': 'application/json',
                              'x-o3-app-name': 'dweb_client',
                              'x-o3-app-version': 'release'}
                });
                return r.ok ? await r.json() : { error: `HTTP ${r.status}` };
            }""",
            f"/api/composer-api.bx/page/json/v2?url=/product/{sku}/")
    except Exception as e:
        log.error("Ошибка fetch внутри браузера: %s", e)
        return None

    if isinstance(result, dict) and result.get("error"):
        log.error("composer-api вернул: %s", result["error"])
        return None
    states = (result or {}).get("widgetStates") if isinstance(result, dict) else None
    if states:
        log.info("SKU %s: данные из composer-api (browser)", sku)
    return states


def fetch_via_html(html, sku):
    #JSON, встроенный в HTML
    data = extract_json_from_html(html)
    if not data:
        return None
    states = find_widget_states(data)
    if states:
        log.info("SKU %s: данные из JSON в HTML", sku)
    return states


def parse_sku(session, page, sku: str, idx: int = 0, total: int = 1) -> Dict[str, Any]:
    #Открывает карточку и пробуем
    start = time.time()
    log.info("[%d/%d] SKU %s: открываю https://www.ozon.ru/product/%s/", idx, total, sku, sku)

    states = fetch_via_requests(session, sku)

    if not states:
        try:
            page.goto(f"https://www.ozon.ru/product/{sku}/",
                      wait_until="domcontentloaded", timeout=60000)
            page.wait_for_timeout(random.randint(2500, 4000))
        except Exception as e:
            log.error("Не загрузилась страница %s: %s", sku, e)
            return empty_row(sku, "page_load_failed")

        html = page.content()
        if "Доступ ограничен" in html or "Похоже, нет соединения" in html:
            log.warning("Заглушка/капча для SKU %s", sku)
            return empty_row(sku, "captcha")

        states = fetch_via_browser(page, sku) or fetch_via_html(html, sku)

    if not states:
        log.warning("[%d/%d] SKU %s: все стратегии провалились (%.1fs)",
                    idx, total, sku, time.time() - start)
        return empty_row(sku, "no_data_source")

    try:
        result = extract_data(sku, states)
    except Exception as e:
        log.exception("Ошибка извлечения SKU %s: %s", sku, e)
        return empty_row(sku, str(e))

    log.info("[%d/%d] SKU %s: готово за %.1fs — title=%r, price=%s",
             idx, total, sku, time.time() - start,
             result.get("title"), result.get("price"))
    return result



def main():
    skus = [s.strip() for s in os.getenv("SKUS", "").split(",") if s.strip()]
    if not skus:
        raise SystemExit("Укажите SKUS в .env")
    if not os.path.exists(COOKIES_STATE):
        raise SystemExit(f"Нет {COOKIES_STATE} — сначала запустите get_cookies.py")

    session = make_session()
    rows: List[Dict[str, Any]] = []

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=False, slow_mo=100,
                                    args=["--disable-blink-features=AutomationControlled"])
        try:
            context = browser.new_context(
                storage_state=COOKIES_STATE,
                user_agent=("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"),
                locale="ru-RU", timezone_id="Europe/Moscow")
            page = context.new_page()

            # Заходим на главную Ozon, чтобы fetch к composer-api шёл
            # с домена ozon.ru (иначе 403).
            page.goto("https://www.ozon.ru/", wait_until="domcontentloaded", timeout=60000)
            page.wait_for_timeout(2000)

            for i, sku in enumerate(skus, 1):
                rows.append(parse_sku(session, page, sku, i, len(skus)))
                time.sleep(random.uniform(1.5, 3.0))
        finally:
            browser.close()

    with open(CSV_PATH, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=FIELDS)
        writer.writeheader()
        writer.writerows(rows)
    log.info("Сохранено в %s (%d строк)", CSV_PATH, len(rows))


if __name__ == "__main__":
    main()