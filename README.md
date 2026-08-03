# Advanced Proxy

**Локальный прокси для Kodi на sing-box и Xray — с профилями, подписками, автоматическим выбором сервера и интеграцией с системным прокси Kodi.**

English: [README (EN)](#english) · Русский: [README (RU)](#русский)

---

# English

## What is this?

Advanced Proxy is a Kodi service addon that runs a **local proxy on your device** — no separate computer, router or VPN client needed. It starts together with Kodi, listens on `127.0.0.1` (default port `1080`), and routes Kodi's traffic through a proxy server that you choose.

Under the hood it uses the two most popular open-source proxy engines — **sing-box** and **Xray-core** — bundled right inside the addon, so nothing extra needs to be installed.

## What it can do

- **One click proxy for all of Kodi.** The addon points Kodi's own network proxy (and addons like YouTube that use it) at the local proxy automatically, and restores your previous settings when it stops.
- **Connect with profile links.** Paste a link and you're ready:
  `vless://`, `vmess://`, `trojan://`, `ss://` (Shadowsocks), `hy2://` (Hysteria2), `wireguard://`, `tuic://`, `socks://`, `http://`.
- **Subscriptions.** Paste a subscription URL — plain text, base64, or JSON with multiple server configs — and the addon imports all servers from it, keeps them updated on a schedule, and cleans up when you remove the subscription.
- **Automatic or manual server choice.**
  - **urltest**: the addon measures latency and automatically picks the fastest working server.
  - **Manual**: you pick the active server from the addon menu.
- **Availability check.** Before activating a server, the addon checks that it is reachable — unreachable servers are skipped, not broken.
- **DNS control.** Set a DNS resolver of your choice: plain UDP, DoH (`https://…`) or DoT (`tls://…`), plus an IPv4/IPv6 preference.
- **Torrent-friendly.** Optional "Direct BitTorrent" toggle sends torrent traffic straight to the internet, bypassing the proxy.
- **Geo rules (Xray).** Optional geoip/geosite databases (default: Russia-blocked lists) send listed networks straight to `direct`.
- **Both engines, all protocols.** sing-box 1.13.15 and Xray 26.7.28, each building what it supports (tuic is sing-box only).
- **Resilient.** Watchdog restarts a crashed engine with backoff, port fallback picks a free port when `1080` is busy, and your chosen port stays stable for the session.

## What it cannot do (yet)

- **Clash / Surge YAML subscriptions** are not supported — only plain text, base64 and JSON config lists.
- **SSH, shadowtls, naive, shadowsocksr** links are not parsed.
- **TUIC on Xray** — Xray does not support TUIC in any version; tuic works with sing-box.
- **Geo rules on sing-box** — sing-box 1.12+ removed embedded geoip/geosite databases, so geo rules apply to Xray only (sing-box would need external rule-sets).
- **QR codes, auto-restart of YouTube**, per-subscription refresh intervals (the interval is shared).
- It is a **proxy**, not a firewall or ad-blocker.

## Requirements & compatibility

- **Kodi 20 (Nexus) or newer** (Kodi 19 Matrix has the right Python, but the repository manifest requires 20+).
- Works on **LibreELEC / CoreELEC / OSMC / Android / Windows / macOS / Linux** — any Kodi device.
- ARM (Raspberry Pi 3/4/5, Amlogic boxes), x86_64, Windows and macOS builds are all shipped.

## Installation

**From the Kodi repository (recommended — auto-updates):**

1. Download `repository.bigping-<version>.zip` from the [latest release](https://github.com/maratdob118/kodi-advanced-proxy/releases).
2. In Kodi: **Add-ons → Install from zip file** → pick the zip.
3. Then **Install from repository → BigPing → Services → Advanced Proxy**.

**Manual install:**

1. Grab the ZIP matching your platform from the [latest release](https://github.com/maratdob118/kodi-advanced-proxy/releases):
   `service.advancedproxy-<version>.linux_arm64.zip` (Raspberry Pi 4/5, LibreELEC ARM64),
   `service.advancedproxy-<version>.linux_armv7.zip` (Raspberry Pi 3),
   `service.advancedproxy-<version>.windows_x64.zip`, `...darwin_arm64.zip`, etc.
2. In Kodi: **Add-ons → Install from zip file** → pick the ZIP.

## Getting started

1. Open **Advanced Proxy** from the addons menu.
2. Press **Add** and paste either:
   - a single profile link (`vless://…`, `hy2://…`, `trojan://…`, …), or
   - a subscription URL — the addon detects which one it is automatically.
3. Pick the mode in settings: **urltest** (automatic) or **manual** (you choose).
4. Go to **Settings → Subscriptions** to set the refresh interval, DNS server, torrent toggle and geo databases.
5. In Kodi's network settings the proxy will already point at `127.0.0.1:1080` (or the fallback port the addon chose). Done — browse and stream.

---

# Русский

## Что это?

Advanced Proxy — сервисный аддон Kodi, который запускает **локальный прокси прямо на вашем устройстве** — без отдельного компьютера, роутера или VPN-клиента. Аддон стартует вместе с Kodi, слушает `127.0.0.1` (по умолчанию порт `1080`) и направляет трафик Kodi через выбранный вами прокси-сервер.

Внутри используются два самых популярных open-source движка — **sing-box** и **Xray-core** — встроенные прямо в аддон: ничего дополнительно устанавливать не нужно.

## Что умеет

- **Прокси одним кликом для всего Kodi.** Аддон сам указывает сетевой прокси Kodi (и аддоны вроде YouTube, которые его используют) на локальный прокси, а при остановке восстанавливает прежние настройки.
- **Подключение по ссылкам.** Вставьте ссылку — и готово:
  `vless://`, `vmess://`, `trojan://`, `ss://` (Shadowsocks), `hy2://` (Hysteria2), `wireguard://`, `tuic://`, `socks://`, `http://`.
- **Подписки.** Вставьте URL подписки — plain-текст, base64 или JSON с несколькими конфигами серверов — и аддон импортирует все серверы, обновляет их по расписанию и убирает при удалении подписки.
- **Автоматический или ручной выбор сервера.**
  - **urltest**: аддон измеряет задержку и сам выбирает самый быстрый рабочий сервер.
  - **Вручную**: вы выбираете активный сервер в меню аддона.
- **Проверка доступности.** Перед активацией сервер проверяется — недоступные пропускаются, а не «ломаются».
- **Управление DNS.** Свой резолвер: обычный UDP, DoH (`https://…`) или DoT (`tls://…`), плюс предпочтение IPv4/IPv6.
- **Дружелюбен к торрентам.** Опция «Прямой BitTorrent» отправляет торрент-трафик напрямую в интернет, в обход прокси.
- **Гео-правила (Xray).** Опциональные geoip/geosite базы (по умолчанию — списки заблокированных в РФ сетей) отправляют перечисленные сети напрямую в `direct`.
- **Оба движка, все протоколы.** sing-box 1.13.15 и Xray 26.7.28, каждый строит то, что поддерживает (tuic — только sing-box).
- **Устойчивость.** Watchdog перезапускает упавший движок с экспоненциальной паузой, при занятом порте `1080` выбирается свободный, и выбранный порт остаётся стабильным на всю сессию.

## Что пока не умеет

- **Подписки Clash / Surge YAML** не поддерживаются — только plain-текст, base64 и JSON-списки конфигов.
- **Ссылки SSH, shadowtls, naive, shadowsocksr** не парсятся.
- **TUIC на Xray** — Xray не поддерживает TUIC ни в одной версии; tuic работает только с sing-box.
- **Гео-правила на sing-box** — sing-box 1.12+ убрал встроенные geoip/geosite базы, поэтому гео-правила работают только на Xray (для sing-box нужны внешние rule-set'ы).
- **QR-коды, автоперезапуск YouTube**, отдельные интервалы обновления для каждой подписки (интервал общий).
- Это **прокси**, а не файрвол и не блокировщик рекламы.

## Требования и совместимость

- **Kodi 20 (Nexus) или новее** (в Kodi 19 Matrix Python подходит, но манифест репозитория требует 20+).
- Работает на **LibreELEC / CoreELEC / OSMC / Android / Windows / macOS / Linux** — на любом устройстве с Kodi.
- ARM (Raspberry Pi 3/4/5, Amlogic-приставки), x86_64, Windows и macOS — все сборки в комплекте.

## Установка

**Из репозитория Kodi (рекомендуется — автообновления):**

1. Скачайте `repository.bigping-<version>.zip` из [последнего релиза](https://github.com/maratdob118/kodi-advanced-proxy/releases).
2. В Kodi: **Аддоны → Установить из zip-файла** → выберите zip.
3. Затем: **Установить из репозитория → BigPing → Службы → Advanced Proxy**.

**Установка вручную:**

1. Возьмите ZIP под вашу платформу из [последнего релиза](https://github.com/maratdob118/kodi-advanced-proxy/releases):
   `service.advancedproxy-<version>.linux_arm64.zip` (Raspberry Pi 4/5, LibreELEC ARM64),
   `service.advancedproxy-<version>.linux_armv7.zip` (Raspberry Pi 3),
   `service.advancedproxy-<version>.windows_x64.zip`, `...darwin_arm64.zip` и т.д.
2. В Kodi: **Аддоны → Установить из zip-файла** → выберите ZIP.

## Быстрый старт

1. Откройте **Advanced Proxy** в меню аддонов.
2. Нажмите **Добавить** и вставьте:
   - либо ссылку профиля (`vless://…`, `hy2://…`, `trojan://…`, …),
   - либо URL подписки — аддон сам определит, что это.
3. Выберите режим в настройках: **urltest** (автоматически) или **вручную** (выбираете сами).
4. В **Настройки → Подписки** задайте интервал обновления, DNS-сервер, торрент-переключатель и гео-базы.
5. В сетевых настройках Kodi прокси уже будет указывать на `127.0.0.1:1080` (или на запасной порт, выбранный аддоном). Готово — смотрите и стримьте.
