MQTT_SAMPLE = '{\n  "sensor_name": "main_imu",\n  "sensor_type": "imu",\n  "imu_type": "MPU6500",\n  "timestamp": 1793770000000,\n  "device_id": "esp32-001",\n  "accelerometer": {\n    "x": 0.196,\n    "y": -0.412,\n    "z": 9.806,\n    "unit": "g"\n  },\n  "gyroscope": {\n    "x": 0.512,\n    "y": -0.128,\n    "z": 9.124,\n    "unit": "°/s"\n  },\n  "temperature": 26.4,\n  "temperature_unit": "°C"\n}'

def trace_sample(payload):
    return '{\n  "version": 1,\n  "steps": [\n    {"type": "send", "payload": ' + payload + '},\n    {"type": "delay", "duration_ms": 1000},\n    {"type": "send", "payload": ' + payload + '}\n  ]\n}'
