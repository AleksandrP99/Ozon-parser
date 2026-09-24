"""
get_cookies.py — авторизация в Ozon и сохранение cookies.
USE_GMAIL=true  — вход по email, код из Gmail.
USE_GMAIL=false — вход по телефону: сначала пробуем Gmail, если письма нет
                  (Ozon обычно шлёт код звонком) — ручной ввод.
"""
import os, re, time, json, pickle, base64, logging
from dotenv import load_dotenv
from google.auth.transport.requests import Request
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from playwright.sync_api import sync_playwright

load_dotenv()
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]
USE_GMAIL = os.getenv("USE_GMAIL", "true").lower() == "true"
GMAIL_TIMEOUT = int(os.getenv("GMAIL_TIMEOUT", "30"))
# Для входа по телефону ждём письмо недолго: Ozon почти всегда шлёт код звонком.
GMAIL_TIMEOUT_PHONE = int(os.getenv("GMAIL_TIMEOUT_PHONE", "10"))
COOKIES_FILE = os.getenv("COOKIES_FILE", "cookies.json")


def gmail_client():
    #Gmail API клиент. OAuth-токен кэшируется в token.pickle
    creds = None
    if os.path.exists("token.pickle"):
        with open("token.pickle", "rb") as f:
            creds = pickle.load(f)
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            flow = InstalledAppFlow.from_client_secrets_file("credentials.json", SCOPES)
            creds = flow.run_local_server(port=0, timeout_seconds=300)
        with open("token.pickle", "wb") as f:
            pickle.dump(creds, f)
    return build("gmail", "v1", credentials=creds)


def _walk_body(part):
    #Рекурсивно собирает текст из MIME-дерева письма
    data = part.get("body", {}).get("data")
    if data:
        return base64.urlsafe_b64decode(data).decode("utf-8", errors="ignore")
    for sub in part.get("parts") or []:
        text = _walk_body(sub)
        if text:
            return text
    return ""


# Формулировки кода в письмах Ozon: "код 123456" и "123456 код".
CODE_RE = [
    re.compile(r"(?:код|code)[^\d]{0,40}(\d{4,6})", re.I),
    re.compile(r"(\d{4,6})[^\d]{0,20}(?:код|code)", re.I),
]


def code_from_gmail(since=None, timeout=GMAIL_TIMEOUT):
    #Опрашивает Gmail каждые 3с: ищет письмо от Ozon свежее since и вытаскивает код
    svc = gmail_client()
    since = since or int(time.time()) - 60
    query = os.getenv("GMAIL_QUERY", "from:ozon.ru")
    deadline = time.time() + timeout
    seen = set()

    while time.time() < deadline:
        resp = svc.users().messages().list(userId="me", q=query, maxResults=20).execute()
        for m in resp.get("messages", []):
            if m["id"] in seen:
                continue
            seen.add(m["id"])
            msg = svc.users().messages().get(userId="me", id=m["id"], format="full").execute()
            if int(msg.get("internalDate", 0)) // 1000 < since:
                continue
            # Чистим HTML и пробелы — так регулярки работают стабильнее.
            text = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", _walk_body(msg["payload"])))
            for rx in CODE_RE:
                hit = rx.search(text)
                if hit:
                    log.info("код из письма: %s", hit.group(1))
                    return hit.group(1)
        time.sleep(3)
    raise TimeoutError(f"код не пришёл за {timeout}s")


def interact(page, selectors, what, value=None):
    #Клик или заполнение первого сработавшего селектора. True/False
    for sel in selectors:
        try:
            el = page.locator(sel).first
            el.wait_for(timeout=5000, state="visible")
            el.click(timeout=5000)
            if value is None:
                log.info("нажали: %s", what)
            else:
                page.wait_for_timeout(300)
                el.fill("")
                el.type(value, delay=100)
                log.info("заполнили: %s", what)
            return True
        except Exception:
            pass
    return False


# Ozon разметки
BY_EMAIL = ['button:has-text("Войти по почте")', 'text="Войти по почте"']
EMAIL_INPUTS = ['input[type="email"]', 'input[placeholder*="почт" i]']
PHONE_INPUTS = ['input[type="tel"]', 'input[inputmode="numeric"]']
SUBMIT_BTNS = ['button:has-text("Войти")', 'button:has-text("Продолжить")',
               'button:has-text("Подтвердить")', 'button[type="submit"]']


def auth_email(page):
    #Вход по email? код из Gmail, иначе ручной ввод
    email = os.getenv("OZON_EMAIL")
    if not email:
        log.error("OZON_EMAIL пустой")
        return None
    log.info("логин по email")
    if not interact(page, BY_EMAIL, "войти по почте"):
        return None
    page.wait_for_timeout(2500)
    if not interact(page, EMAIL_INPUTS, "email", email):
        return None
    page.wait_for_timeout(800)
    started = int(time.time())
    interact(page, SUBMIT_BTNS, "войти")
    page.wait_for_timeout(4000)

    try:
        return code_from_gmail(since=started)
    except TimeoutError:
        log.warning("письма нет за %ds, вводим руками", GMAIL_TIMEOUT)
    except Exception as e:
        log.warning("gmail api упал: %s", e)
    return input("код: ").strip()


def auth_phone(page):
    # Вход по телефону
    # Пробуем Gmail, но озон всегда шлёт код звонком
    # поэтому таймаут короткий, и скорее всего сработает ручной ввод.
    phone = os.getenv("OZON_PHONE")
    if not phone:
        log.error("OZON_PHONE пустой")
        return None
    log.info("логин по телефону")

    # Иногда форма телефона скрыта за кнопкой "Войти".
    if not any(page.locator(s).count() for s in PHONE_INPUTS):
        interact(page, ['button:has-text("Войти")', 'text="Войти"'], "войти")
        page.wait_for_timeout(2500)

    if not interact(page, PHONE_INPUTS, "телефон", phone):
        return None
    page.wait_for_timeout(800)

    started = int(time.time())
    interact(page, ['button:has-text("Войти")', 'button:has-text("Получить код")',
                    'button[type="submit"]'], "отправить телефон")
    page.wait_for_timeout(4000)

    try:
        return code_from_gmail(since=started, timeout=GMAIL_TIMEOUT_PHONE)
    except TimeoutError:
        log.warning("письма нет за %ds (Ozon шлёт код звонком), вводим руками",
                    GMAIL_TIMEOUT_PHONE)
    except Exception as e:
        log.warning("gmail api упал: %s", e)
    return input("код: ").strip()


def submit_code(page, code):
    #Форма кода бывает: одно поле, N полей под цифры или скрытое поле
    inputs = page.locator('input[autocomplete="one-time-code"], input[inputmode="numeric"], '
                          'input[type="tel"], input[type="text"]')
    n = inputs.count()
    if 0 < n < 6:
        inputs.first.click()
        page.wait_for_timeout(300)
        inputs.first.fill("")
        inputs.first.type(code, delay=100)
    elif n >= 6:
        for i, d in enumerate(code[:n]):
            inputs.nth(i).click()
            page.wait_for_timeout(200)
            inputs.nth(i).type(d, delay=80)
            page.wait_for_timeout(150)
    else:
        page.keyboard.type(code, delay=200)
    page.wait_for_timeout(1500)
    interact(page, SUBMIT_BTNS, "подтвердить")


def save_cookies(ctx, path):
    #Сохраняем cookies в двух форматах
    raw = ctx.cookies()
    simplified = [{"name": c["name"], "value": c["value"],
                   "domain": c["domain"], "path": c.get("path", "/")} for c in raw]
    with open(path, "w", encoding="utf-8") as f:
        json.dump(simplified, f, ensure_ascii=False, indent=2)
    ctx.storage_state(path=path.replace(".json", "_state.json"))
    log.info("cookies сохранены: %s (%d шт.)", path, len(simplified))


def main():
    if USE_GMAIL and not os.getenv("OZON_EMAIL"):
        raise SystemExit("USE_GMAIL=true, но OZON_EMAIL не задан")
    if not USE_GMAIL and not os.getenv("OZON_PHONE"):
        raise SystemExit("USE_GMAIL=false, но OZON_PHONE не задан")
    log.info("режим: %s", "email+gmail" if USE_GMAIL else "телефон+gmail")

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=False, slow_mo=200,
                                    args=["--disable-blink-features=AutomationControlled"])
        try:
            ctx = browser.new_context(
                user_agent=("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"),
                locale="ru-RU", timezone_id="Europe/Moscow")
            page = ctx.new_page()

            log.info("открываю data.ozon.ru")
            page.goto("https://data.ozon.ru/", wait_until="domcontentloaded", timeout=60000)
            page.wait_for_timeout(4000)
            if not interact(page, ['button:has-text("Перейти к аналитике")',
                                   'a:has-text("Перейти к аналитике")'], "перейти к аналитике"):
                raise SystemExit("кнопка 'Перейти к аналитике' не нашлась")
            page.wait_for_timeout(5000)

            # Уходим на SSO тут происходи авторизация.
            try:
                page.wait_for_url("**/sso.ozon.ru/**", timeout=30000)
            except Exception:
                pass
            page.wait_for_timeout(3000)

            code = auth_email(page) if USE_GMAIL else auth_phone(page)
            if not code:
                raise SystemExit("не залогинились")
            submit_code(page, code)

            page.wait_for_timeout(8000)
            # Возвращаемся на data.ozon.ru — значит вход прошёл.
            try:
                page.wait_for_url("**/data.ozon.ru/**", timeout=30000)
            except Exception:
                pass
            page.wait_for_timeout(3000)
            save_cookies(ctx, COOKIES_FILE)
        finally:
            browser.close()


if __name__ == "__main__":
    main()