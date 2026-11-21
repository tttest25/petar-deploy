# petar-deploy

Compose-конфигурация совместной установки PostgreSQL 18, Go API и DokuWiki
с плагином Petar. Runner работает на хосте WSL, а не в контейнере.

| Сценарий / проект Compose | Привязки wiki / API / PostgreSQL | Каталоги wiki / PostgreSQL | `SPOOL_DIR` runner | `API_BASE_URL` runner |
|---|---|---|---|---|
| Разработка / `petar-dev` | `127.0.0.1:81` / `127.0.0.1:8081` / `127.0.0.1:5433` | `/opt/petar-data/dev/wiki` / `/opt/go-pet/postgres/deploy/dev/data` | `/opt/petar-data/dev/spool` | `http://127.0.0.1:8081/api/v1` |
| Репетиция / `petar-rehearsal` | `127.0.0.1:82` / `127.0.0.1:8082` / `127.0.0.1:5434` | `/opt/petar-data/rehearsal/wiki` / `/opt/go-pet/postgres/deploy/rehearsal/data` | `/opt/petar-data/rehearsal/spool` | `http://127.0.0.1:8082/api/v1` |
| Рабочая установка / `petar-production` | `0.0.0.0:80` / `127.0.0.1:8080` / `127.0.0.1:5432` | `/opt/petar-data/production/wiki` / `/opt/go-pet/postgres/deploy/production/data` | `/opt/go-pet/spool` | `http://127.0.0.1:8080/api/v1` |

У каждого проекта своя Compose-сеть. Адрес backend для DokuWiki внутри сети —
`http://app:8080` (без `/api/v1`); `petar_app` — совместимый сетевой псевдоним.
Порт рабочего API и его spool сейчас принадлежат старому комплекту. Новый
production не запускать до отдельного рабочего переключения.

- **Dev:** `compose.yaml` + `compose.dev.yaml`; API собирается из `/opt/go-pet`,
  но `scripts/dev.sh` передаёт сборщику только копию исходников, без spool и БД.
  Настройки PG и read-only плагин берутся из рабочих копий.
- **Rehearsal / production:** только `compose.yaml`; образы и read-only файлы
  берутся из [манифеста `deploy-001`](releases/001.env), без сборки и монтирования checkout.

[Инструкция оператора](docs/operations.md) описывает `dev.sh up|down`, read-only
`preflight.sh`, чистую установку через `deploy.sh` и один проход `run-once.sh`.
Результат чистой установки — **подготовлено, ожидает сквозной проверки**.
[Сквозной smoke](docs/operations.md#сквозная-проверка-deploy-07) через
`scripts/smoke.sh` создаёт два задания из DokuWiki и проверяет их публикации;
только в rehearsal он перезапускает стек и повторно открывает те же результаты.
Реальный production cutover, обновление существующей БД и включение cron сюда не входят.
[Ручной рецепт DEPLOY-05](docs/release-001.md) оставлен как свидетельство проверенной базы.
При изменении портов, путей или команд обновляйте конфигурацию, проверку и
соответствующий документ в том же коммите или PR.
