# Ozon parser

Тестовое задание: парсер карточек Ozon по списку SKU.

## Установка
1. `pip install -r requirements.txt`
2. `playwright install chromium`
3. Положить `credentials.json` от Google Cloud (Gmail API включён).
4. Скопировать `.env.example` в `.env` и заполнить.

## Настройка Gmail API

По умолчанию в `.env` стоит `USE_GMAIL=false` — вход по телефону, код вводится руками. Это проще, потому что Ozon часто шлёт код звонком, а не письмом.

Если хочется тянуть код из почты автоматически:
1. Создать проект в Google Cloud Console.
2. Включить Gmail API.
3. Создать OAuth client (Desktop app), скачать `credentials.json` в корень проекта.
4. Поставить `USE_GMAIL=true` в `.env`.
5. При первом запуске `get_cookies.py` откроется браузер для OAuth, токен сохранится в `token.pickle`.

`credentials.json` и `token.pickle` в репозиторий не коммитятся — они в `.gitignore`.

## Запуск
1. `python get_cookies.py` — логин в Ozon, сохранение `cookies.json` и `cookies_state.json`.
2. `python parse_ozon.py` — парсинг SKU из `.env`, результат в `ozon_products.csv`.



## Известные ограничения Ozon

1. **Ozon не гарантирует доставку кода на email.** Даже при входе по телефону
   или по почте система может отправить код звонком. В этом случае
   `code_from_gmail` не находит письмо за таймаут, и срабатывает ручной
   ввод через `input()`. 

2. **`requests.Session` получает 403 от composer-api.** Ozon защищается
   от прямых запросов TLS-отпечатком и CORS. `requests` оставлен как
   демонстрация работы с cookies (требование ТЗ), но основной источник
   данных — `fetch` внутри браузера Playwright, где TLS-отпечаток
   настоящего Chrome. В логах видно `WARNING requests к composer-api: 403`
   — это ожидаемое поведение, дальше срабатывает fallback.

## Три стратегии получения данных

1. `requests.Session` — демонстрация работы с cookies (ТЗ). Обычно 403.
2. `fetch` внутри браузера Playwright — основной рабочий путь.
3. JSON, встроенный в HTML — резерв, если оба предыдущих не сработали.

## Бонус (не реализовано)

- Airflow DAG: обернуть `python parse_ozon.py` в `BashOperator`,
  `schedule_interval="@daily"`.
- DataLens: подключить CSV или ClickHouse как источник.