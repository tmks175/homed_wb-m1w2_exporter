## Prometheus HOMEd + Wiren Board (WB-M1W2) Exporter

Кастомный экспортер температуры для датчиков ***DS18B20***, подключенных к ***Wiren Board [WB-M1W2](https://wirenboard.com/ru/product/WB-M1W2/)***, который преобразует данные из *MQTT* в формат *Prometheus*. В качестве источника - контроллер *[HOMEd Nano Gateway](https://mediawiki.homed.dev/page/Hardware/HOMEd_Gateway_Nano)*.
Основное отличие экспортера от других решений - реализация комбинированной модели сбора данных (pull+push).

**Принцип работы:** экспортер подписывается на `mqtt`-топик с данными от датчиков, периодически получает показания от них (по мере изменения температуры). Дополнительно, один раз за заданный интервал, экспортер отправляет команду `getProperties` (согласно [документации](https://mediawiki.homed.dev/page/ZigBee/Topics) проекта **[HOMEd](https://wiki.homed.dev/)**), чтобы получить актуальное или последнее известное (на момент запроса) состояние температуры.


### Дополнительно
- Кэширование значений температуры.
- Метрики: температура, возраст данных, успешность опроса, счётчик неудач.
- Стандартные эндпоинты: `/metrics` и `/health` (код `200/ОК` или `503` в случае ошибки).


### Системные требования
- Python 3.11+
- Linux (тестировался в ОС Debian 12)
- MQTT-брокер (Mosquitto)
- Контроллер HOMEd (с интерфейсом RS-485) или его софтовый аналог: ПО HOMEd (Modbus) + преобразователь интерфейсов RS-485 в Ethernet/USB (как вариант).
- Wiren Board WB-M1W2


### Инструкция
- Клонируйте и перейдите `(cd)` в репозиторий;
- Cоздайте виртуальное окружение Python `(venv)`;
- Активируйте venv и установите все зависимости из `requirements.txt`;
- Настройте переменные окружения (переименуйте `env.example` в `.env` - с точкой в начале);
- Для быстрого тестирования (не рекомендуется) запустить можно так: \
`python3 prom_exporter.py`
- Проверьте метрики: \
`http://ip:port/metrics` \
`http://ip:port/health`

В дальнейшем, рабочий вариант, рекомедуется запускать как сервис через ***systemd:***
- откройте и отредактируйте переменные: `WorkingDirectory` и `ExecStart` в `modbus-wb-exporter.service`
- скопируйте или создайте симлинк файла службы в каталог: `/etc/systemd/system/`

- активируйте сервис и запустите его: \
`systemctl daemon-reload` \
`systemctl start modbus-wb-exporter.service` \
`systemctl enable modbus-wb-exporter.service`

- проверить статус можно так: \
`systemctl status modbus-wb-exporter.service`

---

### Метрики
- `modbus_temperature` *(Gauge)* - температура по сенсорам;
- `modbus_up` *(Gauge)* - `1` = последний опрос успешен, `0` = неудача;
- `modbus_data_age_seconds` *(Gauge)* - возраст самых свежих данных (сек.);
- `modbus_last_update_timestamp` *(Gauge)* - время последнего обновления (unix timestamp);
- `modbus_failed_polls_total` *(Counter)* - количество неудачных опросов (всего);

---

### Пример дашборда 
***(Grafana)***
- Импортируйте `JSON template` из директории `grafana` \
(в директории `images` можно посмотреть скрин-пример дашборда).

---

### Пример job 
***(Prometheus)***
```
global:
  scrape_interval: 30s

scrape_configs:
  - job_name: "wb_modbus_temp"
    scrape_interval: 90s
    static_configs:
      - targets: ["localhost:8010"]
        labels:
          instance: "wb-modbus-controller"
```

---
