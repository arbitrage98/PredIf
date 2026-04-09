from dotenv import load_dotenv
from pathlib import Path
import serial
import threading
import json
import httpx
import asyncio

ROOT_DIR = Path(__file__).parent
load_dotenv(ROOT_DIR / '.env')

from fastapi import FastAPI, APIRouter, HTTPException, Request, Response, Depends
from starlette.middleware.cors import CORSMiddleware
from motor.motor_asyncio import AsyncIOMotorClient
import os
import logging
import bcrypt
import jwt
import secrets
import random
import math
from pydantic import BaseModel, Field, ConfigDict
from typing import List, Optional
from datetime import datetime, timezone, timedelta
from bson import ObjectId

# MongoDB connection
mongo_url = os.environ['MONGO_URL']
client = AsyncIOMotorClient(mongo_url)
db = client[os.environ['DB_NAME']]

# ─────────────────────────────────────────────────────────────
#  DATA SOURCE LAYER
#
#  Two sources feed latest_esp32_data:
#    1. Serial (USB cable) — listen_to_esp32() thread
#    2. Consentium IoT cloud — poll_consentium() async task
#
#  The source is chosen per motor via the `consentium_config`
#  collection in MongoDB. If a motor has a Consentium board key
#  stored, cloud polling is used for that motor. Otherwise the
#  serial thread is the fallback.
#
#  latest_esp32_data  — live values from serial (always updated)
#  latest_cloud_data  — live values from Consentium cloud
#  latest_ack         — last ACK string received from ESP32
# ─────────────────────────────────────────────────────────────

latest_esp32_data: dict = {"temperature": 0.0, "vibration": 0.0, "state": 0}

# Fix 2: keyed by motor_id so multiple motors don't overwrite each other
latest_cloud_data: dict = {}   # { motor_id: { temperature, vibration, state, source } }

latest_ack: str = ""

# Consentium IoT REST endpoint
CONSENTIUM_RECEIVE_URL = "https://app.consentiumiot.com/api/v1/sensor/receive/"

# ── 1. Serial bridge with auto-reconnect (Fix 3) ─────────────

def listen_to_esp32():
    """
    Background thread that reads from ESP32 over USB serial.
    Fix 3: wraps the connection in an outer retry loop so a
    momentary USB disconnect doesn't kill the thread permanently.
    Retries every 5 seconds until the port is available again.
    """
    global latest_esp32_data, latest_ack
    com_port = os.environ.get("ESP32_COM_PORT", "COM3")

    while True:   # outer reconnect loop
        try:
            ser = serial.Serial(com_port, 115200, timeout=1)
            logger.info(f"SERIAL: Connected to ESP32 on {com_port}")

            while True:   # inner read loop
                if ser.in_waiting > 0:
                    raw = ser.readline().decode("utf-8", errors="ignore").strip()

                    # ── Uplink: sensor data ──────────────────
                    if "TEMP:" in raw and ",VIB:" in raw:
                        try:
                            parts = raw.split(",")
                            t_val = float(parts[0].split(":")[1])
                            v_val = float(parts[1].split(":")[1])
                            latest_esp32_data["temperature"] = t_val
                            latest_esp32_data["vibration"]   = v_val

                            # Downlink command based on thresholds
                            if t_val > 70 or v_val > 2.5:
                                ser.write(b"CMD:CRITICAL\n")
                            elif t_val > 55 or v_val > 1.2:
                                ser.write(b"CMD:WARNING\n")
                            else:
                                ser.write(b"CMD:NORMAL\n")
                        except Exception as parse_err:
                            logger.error(f"SERIAL: Parse error: {parse_err}")

                    # ── ACK from ESP32 ───────────────────────
                    elif raw.startswith("ACK:"):
                        latest_ack = raw
                        latest_esp32_data["last_ack"] = raw
                        logger.info(f"SERIAL ACK: {raw}")

        except serial.SerialException as e:
            logger.error(f"SERIAL: Connection lost ({e}). Retrying in 5s...")
            try:
                ser.close()
            except Exception:
                pass
            import time
            time.sleep(5)   # wait before reconnecting
        except Exception as e:
            logger.error(f"SERIAL: Unexpected error: {e}")
            import time
            time.sleep(5)

threading.Thread(target=listen_to_esp32, daemon=True).start()

# ── 2. Consentium IoT cloud polling (async task) ─────────────

async def poll_consentium():
    """
    Polls the Consentium IoT REST API every 8 seconds.
    Fix 2: stores results in latest_cloud_data[motor_id] so
    multiple motors never overwrite each other's data.
    """
    global latest_cloud_data
    await asyncio.sleep(5)

    while True:
        try:
            configs = await db.consentium_configs.find({}).to_list(100)
            for cfg in configs:
                receive_key = cfg.get("receive_key", "")
                board_key   = cfg.get("board_key", "")
                motor_id    = cfg.get("motor_id", "")
                if not receive_key or not board_key:
                    continue

                url = f"{CONSENTIUM_RECEIVE_URL}?receivekey={receive_key}&boardkey={board_key}"
                try:
                    async with httpx.AsyncClient(timeout=6.0) as client_http:
                        resp = await client_http.get(url)
                except httpx.RequestError as e:
                    logger.error(f"CONSENTIUM: Network error for motor {motor_id}: {e}")
                    continue

                if resp.status_code != 200:
                    logger.warning(f"CONSENTIUM: HTTP {resp.status_code} for motor {motor_id}")
                    continue

                payload     = resp.json()
                sensor_data = payload.get("sensors", {}).get("sensorData", [])
                parsed: dict = {}
                for entry in sensor_data:
                    info  = entry.get("info", "").lower()
                    value = entry.get("data", "0")
                    if "temperature" in info:
                        parsed["temperature"] = float(value)
                    elif "vibration" in info:
                        parsed["vibration"] = float(value)
                    elif "systemstate" in info:
                        parsed["state"] = int(float(value))

                if parsed:
                    parsed["source"]   = "consentium"
                    parsed["motor_id"] = motor_id
                    parsed.setdefault("temperature", 0.0)
                    parsed.setdefault("vibration",   0.0)
                    parsed.setdefault("state",       0)
                    # Fix 2: store under motor_id key, not as flat global
                    latest_cloud_data[motor_id] = parsed
                    logger.info(f"CONSENTIUM: motor={motor_id} temp={parsed['temperature']} vib={parsed['vibration']} state={parsed['state']}")

        except Exception as e:
            logger.error(f"CONSENTIUM: Poll error: {e}")

        await asyncio.sleep(8)

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

app = FastAPI()
api_router = APIRouter(prefix="/api")

JWT_ALGORITHM = "HS256"

def get_jwt_secret():
    return os.environ["JWT_SECRET"]

def hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")

def verify_password(plain: str, hashed: str) -> bool:
    return bcrypt.checkpw(plain.encode("utf-8"), hashed.encode("utf-8"))

def create_access_token(user_id: str, email: str) -> str:
    payload = {"sub": user_id, "email": email, "exp": datetime.now(timezone.utc) + timedelta(minutes=15), "type": "access"}
    return jwt.encode(payload, get_jwt_secret(), algorithm=JWT_ALGORITHM)

def create_refresh_token(user_id: str) -> str:
    payload = {"sub": user_id, "exp": datetime.now(timezone.utc) + timedelta(days=7), "type": "refresh"}
    return jwt.encode(payload, get_jwt_secret(), algorithm=JWT_ALGORITHM)

async def get_current_user(request: Request) -> dict:
    token = request.cookies.get("access_token")
    if not token:
        auth_header = request.headers.get("Authorization", "")
        if auth_header.startswith("Bearer "):
            token = auth_header[7:]
    if not token:
        raise HTTPException(status_code=401, detail="Not authenticated")
    try:
        payload = jwt.decode(token, get_jwt_secret(), algorithms=[JWT_ALGORITHM])
        if payload.get("type") != "access":
            raise HTTPException(status_code=401, detail="Invalid token type")
        user = await db.users.find_one({"_id": ObjectId(payload["sub"])})
        if not user:
            raise HTTPException(status_code=401, detail="User not found")
        user["_id"] = str(user["_id"])
        user.pop("password_hash", None)
        return user
    except jwt.ExpiredSignatureError:
        raise HTTPException(status_code=401, detail="Token expired")
    except jwt.InvalidTokenError:
        raise HTTPException(status_code=401, detail="Invalid token")

# --- Models ---
class RegisterInput(BaseModel):
    email: str
    password: str
    name: str

class LoginInput(BaseModel):
    email: str
    password: str

class MotorInput(BaseModel):
    name: str
    location: str
    rated_power: float = 5.0
    rated_voltage: float = 220.0
    rated_rpm: int = 1500

class ConsentiumConfigInput(BaseModel):
    motor_id: str
    receive_key: str   # "Send API Key" from Consentium dashboard — used to receive data
    board_key: str     # "Board API Key" from Consentium dashboard

class AlertResponse(BaseModel):
    model_config = ConfigDict(extra="ignore")
    id: str
    motor_id: str
    motor_name: str
    alert_type: str
    severity: str
    message: str
    value: float
    threshold: float
    timestamp: str
    resolved: bool = False

# --- Auth Endpoints ---
@api_router.post("/auth/register")
async def register(inp: RegisterInput, response: Response):
    email = inp.email.lower().strip()
    existing = await db.users.find_one({"email": email})
    if existing:
        raise HTTPException(status_code=400, detail="Email already registered")
    hashed = hash_password(inp.password)
    doc = {"email": email, "password_hash": hashed, "name": inp.name, "role": "user", "created_at": datetime.now(timezone.utc).isoformat()}
    result = await db.users.insert_one(doc)
    user_id = str(result.inserted_id)
    access = create_access_token(user_id, email)
    refresh = create_refresh_token(user_id)
    response.set_cookie(key="access_token", value=access, httponly=True, secure=False, samesite="lax", max_age=900, path="/")
    response.set_cookie(key="refresh_token", value=refresh, httponly=True, secure=False, samesite="lax", max_age=604800, path="/")
    return {"id": user_id, "email": email, "name": inp.name, "role": "user"}

@api_router.post("/auth/login")
async def login(inp: LoginInput, request: Request, response: Response):
    email = inp.email.lower().strip()
    ip = request.client.host if request.client else "unknown"
    identifier = f"{ip}:{email}"
    attempt = await db.login_attempts.find_one({"identifier": identifier})
    if attempt and attempt.get("count", 0) >= 5:
        lockout_until = attempt.get("locked_until")
        if lockout_until and datetime.now(timezone.utc) < datetime.fromisoformat(lockout_until):
            raise HTTPException(status_code=429, detail="Too many failed attempts. Try again later.")
        else:
            await db.login_attempts.delete_one({"identifier": identifier})
    user = await db.users.find_one({"email": email})
    if not user or not verify_password(inp.password, user["password_hash"]):
        await db.login_attempts.update_one(
            {"identifier": identifier},
            {"$inc": {"count": 1}, "$set": {"locked_until": (datetime.now(timezone.utc) + timedelta(minutes=15)).isoformat()}},
            upsert=True
        )
        raise HTTPException(status_code=401, detail="Invalid email or password")
    await db.login_attempts.delete_one({"identifier": identifier})
    user_id = str(user["_id"])
    access = create_access_token(user_id, email)
    refresh = create_refresh_token(user_id)
    response.set_cookie(key="access_token", value=access, httponly=True, secure=False, samesite="lax", max_age=900, path="/")
    response.set_cookie(key="refresh_token", value=refresh, httponly=True, secure=False, samesite="lax", max_age=604800, path="/")
    return {"id": user_id, "email": email, "name": user.get("name", ""), "role": user.get("role", "user")}

@api_router.post("/auth/logout")
async def logout(response: Response):
    response.delete_cookie("access_token", path="/")
    response.delete_cookie("refresh_token", path="/")
    return {"message": "Logged out"}

@api_router.get("/auth/me")
async def get_me(request: Request):
    user = await get_current_user(request)
    return user

@api_router.post("/auth/refresh")
async def refresh_token(request: Request, response: Response):
    token = request.cookies.get("refresh_token")
    if not token:
        raise HTTPException(status_code=401, detail="No refresh token")
    try:
        payload = jwt.decode(token, get_jwt_secret(), algorithms=[JWT_ALGORITHM])
        if payload.get("type") != "refresh":
            raise HTTPException(status_code=401, detail="Invalid token type")
        user = await db.users.find_one({"_id": ObjectId(payload["sub"])})
        if not user:
            raise HTTPException(status_code=401, detail="User not found")
        new_access = create_access_token(str(user["_id"]), user["email"])
        response.set_cookie(key="access_token", value=new_access, httponly=True, secure=False, samesite="lax", max_age=900, path="/")
        return {"message": "Token refreshed"}
    except jwt.ExpiredSignatureError:
        raise HTTPException(status_code=401, detail="Refresh token expired")
    except jwt.InvalidTokenError:
        raise HTTPException(status_code=401, detail="Invalid refresh token")

# --- Motor Endpoints ---
@api_router.post("/motors")
async def create_motor(inp: MotorInput, request: Request):
    user = await get_current_user(request)
    doc = {
        "name": inp.name,
        "location": inp.location,
        "rated_power": inp.rated_power,
        "rated_voltage": inp.rated_voltage,
        "rated_rpm": inp.rated_rpm,
        "status": "operational",
        "user_id": user["_id"],
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    result = await db.motors.insert_one(doc)
    doc["id"] = str(result.inserted_id)
    doc.pop("_id", None)
    return doc

@api_router.get("/motors")
async def get_motors(request: Request):
    user = await get_current_user(request)
    motors = await db.motors.find({"user_id": user["_id"]}, {"_id": 1}).to_list(100)
    result = []
    for m in motors:
        motor = await db.motors.find_one({"_id": m["_id"]})
        motor["id"] = str(motor["_id"])
        del motor["_id"]
        # Get latest reading
        latest = await db.sensor_readings.find_one({"motor_id": motor["id"]}, sort=[("timestamp", -1)])
        if latest:
            motor["latest_temp"] = latest.get("temperature")
            motor["latest_vibration"] = latest.get("vibration")
        result.append(motor)
    return result

@api_router.get("/motors/{motor_id}")
async def get_motor(motor_id: str, request: Request):
    await get_current_user(request)
    motor = await db.motors.find_one({"_id": ObjectId(motor_id)})
    if not motor:
        raise HTTPException(status_code=404, detail="Motor not found")
    motor["id"] = str(motor["_id"])
    del motor["_id"]
    return motor

@api_router.delete("/motors/{motor_id}")
async def delete_motor(motor_id: str, request: Request):
    await get_current_user(request)
    await db.motors.delete_one({"_id": ObjectId(motor_id)})
    await db.sensor_readings.delete_many({"motor_id": motor_id})
    await db.alerts.delete_many({"motor_id": motor_id})
    return {"message": "Motor deleted"}

# --- Sensor Data ---
# --- Real-Time Sensor Data ---
@api_router.get("/motors/{motor_id}/live")
async def get_live_motor_data(motor_id: str, request: Request):
    await get_current_user(request)

    cfg        = await db.consentium_configs.find_one({"motor_id": motor_id})
    # Fix 2: look up by motor_id key, not a flat global
    cloud_data = latest_cloud_data.get(motor_id)
    state_map  = {0: "normal", 1: "warning", 2: "critical", 3: "sensor_error"}

    if cfg and cloud_data:
        return {
            "motor_id":     motor_id,
            "temperature":  cloud_data["temperature"],
            "vibration":    cloud_data["vibration"],
            "system_state": state_map.get(cloud_data.get("state", 0), "normal"),
            "timestamp":    datetime.now(timezone.utc).isoformat(),
            "source":       "consentium_cloud",
            "status":       "online",
        }
    else:
        return {
            "motor_id":     motor_id,
            "temperature":  latest_esp32_data["temperature"],
            "vibration":    latest_esp32_data["vibration"],
            "system_state": state_map.get(latest_esp32_data.get("state", 0), "normal"),
            "last_ack":     latest_esp32_data.get("last_ack", ""),
            "timestamp":    datetime.now(timezone.utc).isoformat(),
            "source":       "serial",
            "status":       "online",
        }

# ── Consentium IoT Config Endpoints ─────────────────────────

@api_router.post("/consentium/config")
async def save_consentium_config(inp: ConsentiumConfigInput, request: Request):
    """
    Save the Consentium receive_key and board_key for a motor.
    Once saved, /live for that motor will pull from the cloud
    instead of serial. The poll_consentium() task picks this up
    automatically on its next cycle.
    """
    await get_current_user(request)
    motor = await db.motors.find_one({"_id": ObjectId(inp.motor_id)})
    if not motor:
        raise HTTPException(status_code=404, detail="Motor not found")

    await db.consentium_configs.update_one(
        {"motor_id": inp.motor_id},
        {"$set": {
            "motor_id":    inp.motor_id,
            "receive_key": inp.receive_key,
            "board_key":   inp.board_key,
            "updated_at":  datetime.now(timezone.utc).isoformat(),
        }},
        upsert=True,
    )
    return {"message": "Consentium config saved", "motor_id": inp.motor_id}

@api_router.get("/consentium/config/{motor_id}")
async def get_consentium_config(motor_id: str, request: Request):
    """Returns the stored Consentium config for a motor (keys redacted for security)."""
    await get_current_user(request)
    cfg = await db.consentium_configs.find_one({"motor_id": motor_id}, {"_id": 0})
    if not cfg:
        raise HTTPException(status_code=404, detail="No Consentium config for this motor")
    # Redact keys — only show last 4 chars
    cfg["receive_key"] = "****" + cfg["receive_key"][-4:]
    cfg["board_key"]   = "****" + cfg["board_key"][-4:]
    return cfg

@api_router.delete("/consentium/config/{motor_id}")
async def delete_consentium_config(motor_id: str, request: Request):
    """Remove cloud config — motor reverts to serial as data source."""
    await get_current_user(request)
    await db.consentium_configs.delete_one({"motor_id": motor_id})
    return {"message": "Consentium config removed. Motor reverts to serial source."}

@api_router.get("/consentium/live/{motor_id}")
async def get_consentium_live(motor_id: str, request: Request):
    """
    Fetch the latest reading directly from Consentium IoT cloud
    on demand (bypasses the poll cache).
    """
    await get_current_user(request)
    cfg = await db.consentium_configs.find_one({"motor_id": motor_id})
    if not cfg:
        raise HTTPException(status_code=404, detail="No Consentium config for this motor")

    receive_key = cfg["receive_key"]
    board_key   = cfg["board_key"]
    url = f"{CONSENTIUM_RECEIVE_URL}?receivekey={receive_key}&boardkey={board_key}"

    try:
        async with httpx.AsyncClient(timeout=8.0) as client_http:
            resp = await client_http.get(url)
        if resp.status_code != 200:
            raise HTTPException(status_code=502, detail=f"Consentium returned HTTP {resp.status_code}")

        payload    = resp.json()
        sensor_data = payload.get("sensors", {}).get("sensorData", [])
        parsed: dict = {"motor_id": motor_id, "source": "consentium_cloud",
                        "timestamp": datetime.now(timezone.utc).isoformat()}
        for entry in sensor_data:
            info  = entry.get("info", "").lower()
            value = entry.get("data", "0")
            if "temperature" in info:
                parsed["temperature"] = float(value)
            elif "vibration" in info:
                parsed["vibration"] = float(value)
            elif "systemstate" in info:
                state_map = {0: "normal", 1: "warning", 2: "critical", 3: "sensor_error"}
                s = int(float(value))
                parsed["system_state"] = state_map.get(s, "normal")
                parsed["system_state_code"] = s

        parsed.setdefault("temperature", 0.0)
        parsed.setdefault("vibration",   0.0)
        parsed.setdefault("system_state", "normal")
        return parsed

    except httpx.RequestError as e:
        raise HTTPException(status_code=503, detail=f"Could not reach Consentium IoT: {e}")

@api_router.post("/motors/{motor_id}/readings")
async def generate_readings(motor_id: str, request: Request):
    await get_current_user(request)
    motor = await db.motors.find_one({"_id": ObjectId(motor_id)})
    if not motor:
        raise HTTPException(status_code=404, detail="Motor not found")
    readings = []
    now = datetime.now(timezone.utc)
    for i in range(24):
        ts = now - timedelta(hours=23 - i)
        hour = ts.hour
        temp_cycle = 5 * math.sin(2 * math.pi * hour / 24)
        anomaly = random.random() < 0.08
        base_temp = random.uniform(40, 50)
        noise_t = random.gauss(0, 2)
        temperature = base_temp + temp_cycle + noise_t
        vibration = 2.5 + 0.5 * math.sin(2 * math.pi * hour / 12) + random.gauss(0, 0.3)
        if anomaly:
            temperature += random.uniform(15, 30)
            vibration += random.uniform(2, 5)
        reading = {
            "motor_id": motor_id,
            "temperature": round(temperature, 2),
            "vibration": round(vibration, 2),
            "current": round(random.uniform(3.5, 6.5), 2),
            "rpm": round(random.uniform(1400, 1550), 0),
            "timestamp": ts.isoformat(),
        }
        readings.append(reading)
    if readings:
        await db.sensor_readings.insert_many(readings)
    # Check for alerts
    for r in readings:
        if r["temperature"] > 70:
            alert = {
                "motor_id": motor_id,
                "motor_name": motor.get("name", "Unknown"),
                "alert_type": "overheating",
                "severity": "critical" if r["temperature"] > 85 else "warning",
                "message": f"Temperature {r['temperature']}°C exceeds threshold",
                "value": r["temperature"],
                "threshold": 70.0,
                "timestamp": r["timestamp"],
                "resolved": False,
            }
            await db.alerts.insert_one(alert)
        if r["vibration"] > 5.0:
            alert = {
                "motor_id": motor_id,
                "motor_name": motor.get("name", "Unknown"),
                "alert_type": "vibration",
                "severity": "critical" if r["vibration"] > 7.0 else "warning",
                "message": f"Vibration {r['vibration']}mm/s exceeds threshold",
                "value": r["vibration"],
                "threshold": 5.0,
                "timestamp": r["timestamp"],
                "resolved": False,
            }
            await db.alerts.insert_one(alert)
    # Update motor status
    latest = readings[-1]
    status = "operational"
    if latest["temperature"] > 85 or latest["vibration"] > 7.0:
        status = "critical"
    elif latest["temperature"] > 70 or latest["vibration"] > 5.0:
        status = "warning"
    await db.motors.update_one({"_id": ObjectId(motor_id)}, {"$set": {"status": status}})
    return {"count": len(readings), "readings": readings}

@api_router.get("/motors/{motor_id}/readings")
async def get_readings(motor_id: str, request: Request):
    await get_current_user(request)
    readings = await db.sensor_readings.find({"motor_id": motor_id}, {"_id": 0}).sort("timestamp", -1).to_list(100)
    return readings

# --- Alerts ---
@api_router.get("/alerts")
async def get_alerts(request: Request):
    user = await get_current_user(request)
    motors = await db.motors.find({"user_id": user["_id"]}, {"_id": 1}).to_list(100)
    motor_ids = [str(m["_id"]) for m in motors]
    alerts = await db.alerts.find({"motor_id": {"$in": motor_ids}}, {"_id": 0}).sort("timestamp", -1).to_list(50)
    return alerts

@api_router.put("/alerts/{alert_id}/resolve")
async def resolve_alert(alert_id: str, request: Request):
    await get_current_user(request)
    await db.alerts.update_one({"_id": ObjectId(alert_id)}, {"$set": {"resolved": True}})
    return {"message": "Alert resolved"}

# --- Dashboard Stats ---
@api_router.get("/dashboard/stats")
async def get_dashboard_stats(request: Request):
    user = await get_current_user(request)
    motors = await db.motors.find({"user_id": user["_id"]}).to_list(100)
    motor_ids = [str(m["_id"]) for m in motors]
    total_motors = len(motors)
    operational = sum(1 for m in motors if m.get("status") == "operational")
    warning_count = sum(1 for m in motors if m.get("status") == "warning")
    critical_count = sum(1 for m in motors if m.get("status") == "critical")
    active_alerts = await db.alerts.count_documents({"motor_id": {"$in": motor_ids}, "resolved": False})
    # Avg latest readings
    avg_temp = 0
    avg_vib = 0
    count = 0
    for mid in motor_ids:
        latest = await db.sensor_readings.find_one({"motor_id": mid}, sort=[("timestamp", -1)])
        if latest:
            avg_temp += latest.get("temperature", 0)
            avg_vib += latest.get("vibration", 0)
            count += 1
    if count > 0:
        avg_temp = round(avg_temp / count, 1)
        avg_vib = round(avg_vib / count, 2)
    return {
        "total_motors": total_motors,
        "operational": operational,
        "warning": warning_count,
        "critical": critical_count,
        "active_alerts": active_alerts,
        "avg_temperature": avg_temp,
        "avg_vibration": avg_vib,
    }

# --- AI Prediction ---
@api_router.post("/motors/{motor_id}/predict")
async def predict_motor_health(motor_id: str, request: Request):
    await get_current_user(request)
    motor = await db.motors.find_one({"_id": ObjectId(motor_id)})
    if not motor:
        raise HTTPException(status_code=404, detail="Motor not found")
    readings = await db.sensor_readings.find({"motor_id": motor_id}, {"_id": 0}).sort("timestamp", -1).to_list(24)
    if not readings:
        raise HTTPException(status_code=400, detail="No sensor data available. Generate readings first.")
    temps = [r["temperature"] for r in readings]
    vibs = [r["vibration"] for r in readings]
    avg_t = round(sum(temps) / len(temps), 1)
    max_t = round(max(temps), 1)
    avg_v = round(sum(vibs) / len(vibs), 2)
    max_v = round(max(vibs), 2)
    data_summary = f"Motor: {motor.get('name')}, Location: {motor.get('location')}, Rated Power: {motor.get('rated_power')}kW, Rated RPM: {motor.get('rated_rpm')}"
    data_summary += f"Last 24h stats - Avg Temp: {avg_t}°C, Max Temp: {max_t}°C, Avg Vibration: {avg_v}mm/s, Max Vibration: {max_v}mm/s"
    data_summary += f"Readings count: {len(readings)}"
    try:
        from emergentintegrations.llm.chat import LlmChat, UserMessage
        api_key = os.environ.get("EMERGENT_LLM_KEY", "")
        chat = LlmChat(
            api_key=api_key,
            session_id=f"predict-{motor_id}-{datetime.now(timezone.utc).isoformat()}",
            system_message="You are a predictive maintenance AI expert for DC motors. Analyze sensor data and provide: 1) Current health assessment (Good/Fair/Poor/Critical), 2) Failure risk percentage, 3) Predicted issues, 4) Recommended actions. Be concise and specific. Format response as JSON with keys: health_status, risk_percentage, predicted_issues (array), recommendations (array), summary."
        )
        chat.with_model("openai", "gpt-5.2")
        user_msg = UserMessage(text=f"Analyze this DC motor sensor data for predictive maintenance:\n{data_summary}")
        ai_response = await chat.send_message(user_msg)
        import json
        try:
            if "```json" in ai_response:
                ai_response = ai_response.split("```json")[1].split("```")[0].strip()
            elif "```" in ai_response:
                ai_response = ai_response.split("```")[1].split("```")[0].strip()
            prediction = json.loads(ai_response)
        except (json.JSONDecodeError, IndexError):
            prediction = {
                "health_status": "Fair",
                "risk_percentage": 25,
                "predicted_issues": ["Unable to parse AI response"],
                "recommendations": ["Review sensor data manually"],
                "summary": ai_response[:500] if isinstance(ai_response, str) else "Analysis complete",
            }
    except Exception as e:
        logger.error(f"AI prediction error: {e}")
        # Fallback to rule-based
        risk = 10
        issues = []
        recs = []
        if max_t > 85:
            risk += 40
            issues.append("Critical overheating detected")
            recs.append("Immediate inspection required - check cooling system")
        elif max_t > 70:
            risk += 20
            issues.append("Elevated temperature trend")
            recs.append("Schedule cooling system maintenance")
        if max_v > 7.0:
            risk += 30
            issues.append("Severe vibration anomaly")
            recs.append("Check bearings and shaft alignment immediately")
        elif max_v > 5.0:
            risk += 15
            issues.append("Above-normal vibration levels")
            recs.append("Plan bearing inspection within next maintenance window")
        health = "Good" if risk < 20 else "Fair" if risk < 40 else "Poor" if risk < 70 else "Critical"
        prediction = {
            "health_status": health,
            "risk_percentage": min(risk, 100),
            "predicted_issues": issues if issues else ["No significant issues detected"],
            "recommendations": recs if recs else ["Continue regular monitoring"],
            "summary": f"Rule-based analysis: {health} condition with {risk}% failure risk.",
        }
    prediction["motor_id"] = motor_id
    prediction["motor_name"] = motor.get("name", "Unknown")
    prediction["analyzed_at"] = datetime.now(timezone.utc).isoformat()
    prediction["data_points"] = len(readings)
    return prediction

# --- Seed Admin ---
async def seed_admin():
    admin_email = os.environ.get("ADMIN_EMAIL", "admin@example.com")
    admin_password = os.environ.get("ADMIN_PASSWORD", "admin123")
    existing = await db.users.find_one({"email": admin_email})
    if existing is None:
        hashed = hash_password(admin_password)
        await db.users.insert_one({
            "email": admin_email, "password_hash": hashed,
            "name": "Admin", "role": "admin",
            "created_at": datetime.now(timezone.utc).isoformat()
        })
        logger.info(f"Admin seeded: {admin_email}")
    elif not verify_password(admin_password, existing["password_hash"]):
        await db.users.update_one({"email": admin_email}, {"$set": {"password_hash": hash_password(admin_password)}})
        logger.info("Admin password updated")

@app.on_event("startup")
async def startup():
    await db.users.create_index("email", unique=True)
    await db.login_attempts.create_index("identifier")
    await db.sensor_readings.create_index([("motor_id", 1), ("timestamp", -1)])
    await db.alerts.create_index([("motor_id", 1), ("timestamp", -1)])
    await db.consentium_configs.create_index("motor_id", unique=True)
    await seed_admin()
    # Start Consentium IoT cloud polling task
    asyncio.create_task(poll_consentium())
    logger.info("CONSENTIUM: Cloud polling task started")
    # Write test credentials
    Path("/app/memory").mkdir(exist_ok=True)
    with open("/app/memory/test_credentials.md", "w") as f:
        f.write("# Test Credentials\n\n")
        f.write(f"## Admin\n- Email: {os.environ.get('ADMIN_EMAIL', 'admin@example.com')}\n- Password: {os.environ.get('ADMIN_PASSWORD', 'admin123')}\n- Role: admin\n\n")
        f.write("## Auth Endpoints\n- POST /api/auth/register\n- POST /api/auth/login\n- POST /api/auth/logout\n- GET /api/auth/me\n- POST /api/auth/refresh\n")
        f.write("\n## Consentium IoT Endpoints\n")
        f.write("- POST /api/consentium/config          — save receive_key + board_key for a motor\n")
        f.write("- GET  /api/consentium/config/{motor_id} — view config (keys redacted)\n")
        f.write("- DELETE /api/consentium/config/{motor_id} — remove config (reverts to serial)\n")
        f.write("- GET  /api/consentium/live/{motor_id}  — on-demand cloud fetch\n")
    logger.info("Application started")

@app.on_event("shutdown")
async def shutdown_db_client():
    client.close()

app.include_router(api_router)

app.add_middleware(
    CORSMiddleware,
    allow_origins=[os.environ.get("FRONTEND_URL", "http://localhost:3000")],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)