#!/usr/bin/env python3
import json
import time
import os
import sys
import logging
import paho.mqtt.client as mqtt
from logging.handlers import RotatingFileHandler
import threading
from prometheus_client import start_http_server, Gauge, Summary
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from prometheus_client import exposition
from prometheus_client import Counter
from dotenv import load_dotenv

load_dotenv()
temps_lock = threading.Lock()
mqtt_connected = threading.Event()
received_temps = {}

# Конфигурация через .env
MQTT_BROKER = os.getenv("MQTT_BROKER", "192.168.1.170")
MQTT_PORT = int(os.getenv("MQTT_PORT", 1883))
TOPIC_PREFIX = os.getenv("MQTT_TOPIC_PREFIX", "wrthomed")
CMD_TOPIC = os.getenv("CMD_TOPIC", f"{TOPIC_PREFIX}/command/modbus")
DEVICE = os.getenv("DEVICE", "1.50")
SENSORS = os.getenv("SENSORS", "1,2").split(",")
POLL_INTERVAL_SEC = int(os.getenv("POLL_INTERVAL_SEC", 180))  # 3 мин
CACHE_DIR = os.getenv("CACHE_DIR", "/opt/modbus-wb/tmp-temp")
LOG_FILE = os.getenv("LOG_FILE", "/opt/modbus-wb/prom_exporter.log")
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
HTTP_PORT = int(os.getenv("HTTP_PORT", 8010))
MAX_CACHE_AGE_SEC = int(os.getenv("MAX_CACHE_AGE_SEC", "600"))

# Логгер
log_formatter = logging.Formatter('%(asctime)s [%(levelname)s] %(message)s')
file_handler = RotatingFileHandler(LOG_FILE, maxBytes=256*1024, backupCount=3)
file_handler.setFormatter(log_formatter)
console_handler = logging.StreamHandler()
console_handler.setFormatter(log_formatter)
logger = logging.getLogger("modbus_exporter")
logger.setLevel(LOG_LEVEL)
logger.addHandler(file_handler)
logger.addHandler(console_handler)

if MAX_CACHE_AGE_SEC < POLL_INTERVAL_SEC:
    logger.warning(
        f"MAX_CACHE_AGE_SEC ({MAX_CACHE_AGE_SEC} сек) меньше POLL_INTERVAL_SEC "
        f"({POLL_INTERVAL_SEC} сек) - кэш может устареть раньше следующего опроса"
    )

# Prometheus метрики
temp_gauge = Gauge('modbus_temperature', 'Temperature from Modbus sensor', ['device', 'sensor'])
up_gauge = Gauge('modbus_up', '1 if last poll successful')
last_update = Gauge('modbus_last_update_timestamp', 'Unix timestamp of last successful update', ['device', 'sensor'])
data_age = Gauge('modbus_data_age_seconds', 'Seconds since last update', ['device', 'sensor'])
failed_polls = Counter('modbus_failed_polls_total', 'Total number of failed poll attempts')

# Кэш функции
def load_from_cache(sensor):
    path = os.path.join(CACHE_DIR, f"modbus_temp_{sensor}.cache")
    if os.path.exists(path):
        try:
            mtime = os.path.getmtime(path)
            age = time.time() - mtime
            
            if age > MAX_CACHE_AGE_SEC:
                logger.warning(f"[sensor {sensor}] Кэш устарел ({int(age)} сек > {MAX_CACHE_AGE_SEC} сек) - сбрасываем значение")
                return None  # если кэш устарел - не используем
            
            with open(path, "r") as f:
                return float(f.read().strip())
        except Exception as e:
            logger.error(f"Ошибка чтения кэша {sensor}: {e}")
    return None

def save_to_cache(sensor, value):
    path = os.path.join(CACHE_DIR, f"modbus_temp_{sensor}.cache")
    new_temp = float(value)
    formatted = "{:.1f}".format(new_temp)
    
    try:
        with open(path, "r") as f:
            old_str = f.read().strip()
            old_temp = float(old_str)
        
        # Порог изменения - например 0.1 °C (влияет на частоту записей файлов кэша на диск)
        if abs(new_temp - old_temp) < 0.1:
            logger.debug(f"[sensor {sensor}] Изменение < 0.1 °C ({old_temp} → {new_temp}) - пропускаем запись")
            return
        
    except (FileNotFoundError, ValueError):
        pass
    
    try:
        with open(path, "w") as f:
            f.write(formatted)
        logger.debug(f"[sensor {sensor}] Сохранено в кэш: {formatted} (изменение ≥ 0.1 °C)")
    except Exception as e:
        logger.error(f"Ошибка записи кэша {sensor}: {e}")

# MQTT клиент
client = mqtt.Client(
    callback_api_version=mqtt.CallbackAPIVersion.VERSION2,
    protocol=mqtt.MQTTv311
)

client.reconnect_delay_set(min_delay=1, max_delay=120)  # Авто-reconnect

def on_connect(client, userdata, flags, rc, properties=None):
    if rc == 0:
        logger.info("Подключено к MQTT")
        mqtt_connected.set()
        
        for s in SENSORS:
            client.subscribe(f"{TOPIC_PREFIX}/fd/modbus/{DEVICE}/{s}")
    else:
        logger.error(f"Ошибка подключения: {rc}")

def on_message(client, userdata, msg):
    try:
        payload = json.loads(msg.payload.decode())
        sensor = msg.topic.split("/")[-1]
        if sensor in SENSORS and "temperature" in payload:
            temp = payload["temperature"]
            
            with temps_lock:
                received_temps[sensor] = temp
            
            temp_gauge.labels(device=DEVICE, sensor=sensor).set(round(temp, 1))
            last_update.labels(device=DEVICE, sensor=sensor).set(time.time())
            data_age.labels(device=DEVICE, sensor=sensor).set(0)  # если есть обновиление - age=0
            save_to_cache(sensor, temp)
            logger.info(f"[sensor {sensor}] Температура: {temp}")
        else:
            logger.warning(f"[{sensor}] Нет поля temperature или неизвестный датчик")
    except Exception as e:
        logger.error(f"Ошибка обработки сообщения из {msg.topic}: {e}")

def on_disconnect(client, userdata, flags, rc, properties=None):
    logger.warning("Отключено от MQTT, reconnect...")
    mqtt_connected.clear()

client.on_connect = on_connect
client.on_message = on_message
client.on_disconnect = on_disconnect

# Функция опроса
def poll_data():
    with temps_lock:
        received_temps.clear()

    success = False

    for attempt in range(5):        # Retry 5 раз
        try:
            payload = {"action": "getProperties", "device": DEVICE}
            result = client.publish(CMD_TOPIC, json.dumps(payload), qos=0)

            if result.rc != mqtt.MQTT_ERR_SUCCESS:
                logger.warning(f"Publish rc={result.rc} (attempt {attempt+1})")

                if result.rc == mqtt.MQTT_ERR_NO_CONN:
                    logger.info("MQTT not connected - trying reconnect")
                    try:
                        client.reconnect()
                    except Exception as e:
                        logger.debug(f"Reconnect failed: {e}")

                time.sleep(2 ** attempt)
                continue

            logger.info(f"Отправлен запрос для wb-m1w2 с адресом {DEVICE}")
            
            # ждем ответы с таймаутом 10 сек
            start = time.time()
            while time.time() - start < 10:
                with temps_lock:
                    # if len(received_temps) >= 1:
                    # ok - если ответили оба сенсора (выше - один сенсор)
                    if len(received_temps) >= len(SENSORS):
                        success = True
                        break
                time.sleep(0.5)

            if success:
                break

            logger.warning(f"Ответы от датчиков не получены (attempt {attempt+1})")

        except Exception as e:
            logger.error(f"Попытка {attempt+1} упала с исключением: {e}")
            time.sleep(2 ** attempt)

    up_gauge.set(1 if success else 0)
    
    if not success:
            failed_polls.inc()  # увеличиваем на 1 каждый неудачный опрос
            logger.warning("Опрос неудачен - используем кэш")
        
            for s in SENSORS:
                path = os.path.join(CACHE_DIR, f"modbus_temp_{s}.cache")
                if os.path.exists(path):
                    try:
                        mtime = os.path.getmtime(path)
                        age = time.time() - mtime
                    
                        # проверяем возраст кэша
                        if age > MAX_CACHE_AGE_SEC:
                            logger.warning(f"[sensor {s}] Кэш устарел ({int(age)} сек > {MAX_CACHE_AGE_SEC} сек) - сбрасываем значение")
                            temp_gauge.labels(device=DEVICE, sensor=s).set(float('nan'))
                            data_age.labels(device=DEVICE, sensor=s).set(age)
                            continue  # переходим к следующему сенсору
                    
                        with open(path, "r") as f:
                            cached_str = f.read().strip()
                        cached = float(cached_str)
                        temp_gauge.labels(device=DEVICE, sensor=s).set(cached)
                        data_age.labels(device=DEVICE, sensor=s).set(age)
                        logger.info(f"[sensor {s}] Данные из кэша, возраст: {int(age)} сек")
                
                    except Exception as e:
                        logger.error(f"Ошибка чтения кэша {s}: {e}")
                        temp_gauge.labels(device=DEVICE, sensor=s).set(float('nan'))
                        data_age.labels(device=DEVICE, sensor=s).set(999999)            
            
                else:
                    logger.warning(f"[sensor {s}] Кэш-файл отсутствует")
                    temp_gauge.labels(device=DEVICE, sensor=s).set(float('nan'))
                    data_age.labels(device=DEVICE, sensor=s).set(999999)

class HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == '/metrics':
            self.send_response(200)
            self.send_header('Content-Type', exposition.CONTENT_TYPE_LATEST)
            self.end_headers()
            self.wfile.write(exposition.generate_latest())
        
        elif self.path == '/health':
            up_value = up_gauge._value.get()
            status = 200 if up_value == 1 else 503
            self.send_response(status)
            self.send_header('Content-Type', 'text/plain')
            self.end_headers()
            message = "OK" if status == 200 else f"DOWN: Last poll failed (up={up_value})"
            self.wfile.write(message.encode())
        
        else:
            self.send_response(404)
            self.end_headers()

def run_server():
    server_address = ('', HTTP_PORT)
    httpd = ThreadingHTTPServer(server_address, HealthHandler)
    logger.info(f"HTTP-сервер запущен на: {HTTP_PORT} (/metrics и /health)")
    httpd.serve_forever()


if __name__ == '__main__':
    logger.warning("[INFO] Start exporter")
    
    os.makedirs(CACHE_DIR, exist_ok=True)
    logger.info(f"Директория для кэша проверена/создана: {CACHE_DIR}")
 
    up_gauge.set(0)
    logger.debug("Начальное значение up=0")

    # HTTP-сервер с /metrics и /health в отдельном потоке
    threading.Thread(target=run_server, daemon=True).start()
    
    client.connect_async(MQTT_BROKER, MQTT_PORT, 60)
    client.loop_start()  # Фоновый цикл обработки MQTT
    
    logger.info("Ждём подключения к MQTT...")
    if not mqtt_connected.wait(timeout=15):
        logger.error("MQTT connection timeout")
    
    logger.info("Ждём 3 секунды перед первым опросом")
    time.sleep(3)

    try:
        poll_data()
        
        while True:
            time.sleep(POLL_INTERVAL_SEC)
            poll_data()
        
    except KeyboardInterrupt:
        logger.info("Отключаем брокер...")
        client.loop_stop()
        client.disconnect()
        logger.info("Работа экспортера успешно завершена")
