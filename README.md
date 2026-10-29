# Yandex Disk backup

Одностороннее зеркало основного аккаунта Яндекс.Диска на NAS с 180-дневным карантином удалённых и заменённых файлов. Второй аккаунт пока остаётся на старой схеме; его сервис находится в отключённом профиле `secondary`.

Подробная архитектура и порядок переключения описаны в [PLAN.md](./PLAN.md).

## Основные команды

Проверка Compose:

```bash
docker compose config --quiet
```

Проверка удалённых аккаунтов после авторизации:

```bash
docker run --rm \
  -v /srv/yandexdisk-backup/config:/config \
  rclone/rclone:1.75.0 --config /config/rclone.conf about yandex_main:
```

Запуск:

```bash
docker compose up -d
```

Состояние:

```bash
docker compose ps
docker compose logs --tail 100
cat /srv/yandexdisk-backup/state/main/last-success
```

Остановка:

```bash
docker compose down --timeout 120
```

## Важное свойство

Проект выполняет только `rclone sync yandex_account: /backup`: Яндекс.Диск всегда является источником, а NAS — назначением. OAuth-токен имеет доступ только для чтения, поэтому локальные данные не могут попасть в облако через этот проект. Удаляемые из актуального зеркала файлы сначала переносятся в `/mnt/backup/yandexdisk_backup_versions/main` и хранятся 180 дней.

## Настройка путей

Публичный снимок использует нейтральное имя `secondary` для второго аккаунта и примерные пути. Скопируйте `.env.example` в локальный `.env` и укажите существующие локальные каталоги состояния и смонтированного хранилища. Пути `/srv/yandexdisk-backup` и `/mnt/backup` в документации являются примерами. Не запускайте сервис до проверки монтирования и конфигурации.

`examples/rclone.conf.example` содержит только названия remotes и тип backend. Авторизуйте аккаунты локально с доступом только для чтения и сохраните итоговый файл в `${BACKUP_DATA_DIR}/config/rclone.conf`. Сам `.env`, токены rclone, журналы, состояние и резервные данные в Git не сохраняются.
