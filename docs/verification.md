# Проверки

Финальная проверка запускается из корня проекта в Ubuntu:

```bash
sudo python3 scripts/verify_release.py
```

Нужен уже развёрнутый Minikube-сервис. Скрипт выполняет unit-тесты, server-side dry run Kustomize, сравнивает код и lock с образом, проверяет обычный/SSE-ответ, авторизацию и серверные логи. Сеть проверяется с двух Pod: клиент достигает контрольного сервиса, а vLLM с NetworkPolicy — нет. Модельный Pod при этом не пересоздаётся.

В наборе 53 теста: ASGI и поля журнала, загрузка и повреждение файлов, offline-кэш, перенаправления с токеном, SSE, расчёт метрик и DNS-пробы. Проверки настоящей модели перечислены ниже.

| Что проверено | Отчёт |
| --- | --- |
| Текущие исходники, зависимости, API, SSE, логи и запрет сети | [Финальная проверка](../results/release-verification-current.json) |
| Пустой PVC, восемь файлов, размеры и SHA-256, UID 10001 | [Свежая загрузка](../results/fresh-download-verification-2026-10-28.json) |
| Новый Pod с прежним PVC, запрет сети и рабочий API | [Kubernetes](../results/kubernetes-verification-2026-10-28.json) |
| Подавление сырого вывода/traceback, SIGTERM, полный lock | [Docker-сервис](../results/service-verification-2026-10-27.json) |
| Настоящие файлы в загрузчике с network=none и read-only корнем | [Offline-загрузчик](../results/offline-downloader-verification-2026-10-27.json) |
| ConfigMap меняется без пересборки образа | [2 → 1](../results/config-transition-seqs2-to-1-2026-10-28.json), [1 → 2](../results/config-transition-seqs1-to-2-2026-10-28.json) |
| Два лимита, два размера входа, 96 запросов без ошибок | [Сравнение](../results/benchmark-comparison-2026-10-28.md) |

Свежая загрузка и полный запуск vLLM проверены отдельными опытами. Публичная модель использует пустой HF Secret; реальный gated-доступ с непустым токеном отдельно не проверялся.

Локальные результаты находятся в `results/local/` и исключены из Git. Перед чтением JSON нужно проверить `*-status.txt` и время: после ошибки на диске может остаться старый успешный отчёт.
