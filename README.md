# Yandex Disk backup

Одностороннее зеркало Яндекс.Диска на NAS с карантином удалённых и заменённых
файлов. Источник — облако, назначение — локальный каталог; команды проекта не
загружают файлы в облако. Для OAuth приложения нужны только права чтения всего
Диска и информации о Диске. Уже работающий отдельный клиент второго аккаунта
не заменяется: дополнительный backup-сервис находится в профиле `secondary`.

## Конфигурация

Скопируйте `.env.example` в локальный `.env` и укажите **существующие** каталоги:
`BACKUP_DATA_DIR`, `MAIN_BACKUP_DIR`, `MAIN_VERSIONS_DIR`. Не подменяйте рабочие
пути примерами при обновлении. Реальные `rclone.conf`, OAuth, логи и файлы состояния
хранятся вне Git. Пример структуры remotes: `examples/rclone.conf.example`.

```sh
docker compose config --quiet
docker compose up -d main
docker compose ps
docker compose logs --tail 100 main
```

Compose закрепляет rclone 1.75.2 по digest. Скрипты монтируются только на чтение.
Для тестов структуры без локальных настроек:

```sh
docker compose --env-file .env.example config --quiet
```

`EXPECTED_FS_MAGIC=6969` означает NFS. Перед любым sync и очисткой **оба** каталога
должны быть самостоятельными mountpoints с заданным filesystem magic и проходить
короткую проверку записи. Наличие пустого локального каталога или одного marker
не разрешает backup. Для другого намеренно выбранного хранилища задайте его magic
явно после проверки `stat -f -c %t`. Backup, versions и state должны быть разными
каноническими абсолютными каталогами; versions/state не могут находиться в зеркале.

## Состояние и ошибки

Каждый проход сначала проверяет targets и возможность **перечислить файлы**
источника. `rclone about` не является достаточной проверкой: информация о Диске
может читаться, когда доступ к файлам запрещён. Ошибки 410 записываются как
`class=provider_access`, 401/403 — `class=authentication`. После них sync и pruning
не запускаются, следующий автоматический запрос откладывается на `RETRY_MAX_SECONDS`.
Временные сбои используют ограниченный экспоненциальный backoff. Предельные времена:
`PREFLIGHT_TIMEOUT_SECONDS` и `MAX_RUN_SECONDS`; остановка завершает owned process group.

`last-attempt` фиксирует начало, `last-status` — текущую фазу, `last-error` — сбой.
`last-success` обновляется **временем завершения** только после успешного sync,
проверки targets и retention. Ошибка `find` не разрешает удалять частичный список;
сбой любого удаления даёт ненулевой итог и сохраняет прежний success marker.
Статусы записываются заменой временного файла, без частичного содержимого.

Docker healthcheck требует недавний полный успех и отсутствие последней ошибки.
При интервале 1800 секунд default `HEALTH_MAX_AGE_SECONDS=3900`. Первый ещё не
завершённый backup и заблокированный OAuth **не healthy**. Healthcheck ничего не
пишет в облако и не создаёт ложного success для живого процесса. Подключайте к
Docker health независимое существующее оповещение; проект не отправляет сообщения
внешним получателям и сам по себе не гарантирует доставку аварийного уведомления.

## Журналы и карантин

Rclone пишет в stdout/stderr, включая неуспешные проходы. Лимиты Docker json-file
`10m × 3` ограничивают эти сообщения; новых `/logs/<run_id>.log` больше нет.
Прежние файловые журналы сохраняются на хосте и автоматически не удаляются.
Карантин очищается только после успешного sync; `RETENTION_DAYS=0` отключает его
удаление. Очистка принимает только прямые timestamp-каталоги внутри versions,
не трогает неизвестные имена и не следует directory symlinks.

## Восстановление OAuth-доступа

При 410 не меняйте источник на пустую папку и не отключайте проверки. Изменение
повторов не восстанавливает запрещённый доступ. Такой сбой описан в
[upstream rclone](https://github.com/rclone/rclone/issues/10067); в обсуждении
сообщают об успешном собственном OAuth-приложении. Это не гарантия состояния
конкретного аккаунта или доступности каждого файла.

Создайте своё приложение с **read-only** правами через
[Яндекс OAuth](https://oauth.yandex.ru/). Порядок получения токена — в
[официальной документации](https://yandex.ru/dev/id/doc/ru/tokens/debug-token).
Сохраните token/ClientID и `expires_in` из ответа OAuth в owner-only файл вне repo:

```dotenv
YANDEX_ACCESS_TOKEN=<значение из OAuth>
YANDEX_CLIENT_ID=<ClientID собственного приложения>
YANDEX_EXPIRES_IN=<реальный expires_in, если выдан>
```

Не помещайте токен в argv, чат или Git. На хосте есть вспомогательный stdlib tool:

```sh
python3 tools/update_oauth.py --config /private/rclone.conf --token-file /private/new-oauth.env
```

Без `--apply` он выполняет только два GET: сведения о Диске и metadata корня.
Он отвергает redirects и не передаёт OAuth header другому хосту. Не печатает
credential/response body. Оба файла должны быть regular owner-only, не symlinks.

Для применения сначала остановите **только main backup**, чтобы refresh rclone не
соревновался с заменой файла. Добавьте `--apply`: перед заменой создаётся приватная
точная копия, сохраняются uid/gid и mode 600; меняется только выбранный remote.
Старые refresh_token/client_secret не переносятся к новому приложению. Если время
истечения не выдано, helper его не выдумывает; сервер остаётся источником истины.

После замены проверьте root listing и download/check небольшого известного файла
в **отдельный** каталог, затем запускайте main. Зафиксируйте настоящий успешный
проход и отдельно проверьте восстановление из зеркала/versions. Авторизация и
реальные sync/prune не являются частью offline regression tests.

## Тесты

```sh
python3 -W error::ResourceWarning -m unittest discover -s tests -v
YDB_SHELL_TEST_IMAGE=ghcr.io/rclone/rclone:1.75.2 python3 -m unittest discover -s tests -v
sh -n scripts/run-loop.sh
sh -n scripts/healthcheck.sh
```

Shell tests запускают настоящий entrypoint с fake rclone/find/rm/mount checks и
изолированными временными данными. В варианте с image контейнеры имеют
`--network none`, без рабочих mounts/credentials. OAuth HTTP boundary подменён.
Проверяются частичные ошибки cleanup, ложный success, 410/403, deadlines, stop,
health freshness, сохранение старой конфигурации и безопасная замена нового token.

Общий проектный план и восстановление — в [PLAN.md](PLAN.md).
