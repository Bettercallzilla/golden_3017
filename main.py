import os
import json
import uuid
import time
import threading
from typing import Dict
from fastapi import FastAPI, HTTPException, Query, UploadFile, File, Form
from pydantic import BaseModel
from instagrapi import Client
import requests
from requests.utils import cookiejar_from_dict

# Config from env
FOLLOW_WEBHOOK_URL = os.getenv("FOLLOW_WEBHOOK_URL", "")
FOLLOW_POLL_INTERVAL = int(os.getenv("FOLLOW_POLL_INTERVAL_SECONDS", "60"))

app = FastAPI(title="Instagram Automation API v9", version="9.0.0")

# In-memory store of clients
clients: Dict[str, Client] = {}

# Sessions dir
SESSIONS_DIR = "/data/sessions"
os.makedirs(SESSIONS_DIR, exist_ok=True)

class LoginReq(BaseModel):
    username: str
    password: str

class ChallengeVerifyReq(BaseModel):
    session_id: str
    code: str

class SendReq(BaseModel):
    session_id: str
    recipients: list
    message: str
    interval: float = 0.0

@app.post("/login")
def login(req: LoginReq):
    client = Client()
    session_id = str(uuid.uuid4())
    try:
        client.login(req.username, req.password)
    except Exception as e:
        if "CHALLENGE" in str(e).upper():
            clients[session_id] = client
            _start_follow_poll(session_id)
            return {"session_id": session_id, "challenge": True}
        raise HTTPException(400, f"Login failed: {e}")
    clients[session_id] = client
    _save_session(session_id, client.get_settings(), [])
    _start_follow_poll(session_id)
    return {"session_id": session_id}

@app.get("/challenge")
def get_challenge(session_id: str = Query(...)):
    client = clients.get(session_id)
    if not client:
        raise HTTPException(404, "Session not found")
    methods = client.challenge_resolve()
    return {"methods": methods}

@app.post("/challenge/verify")
def verify_challenge(req: ChallengeVerifyReq):
    client = clients.get(req.session_id)
    if not client:
        raise HTTPException(404, "Session not found")
    try:
        client.challenge_code(req.code)
    except Exception as e:
        raise HTTPException(400, f"Verification failed: {e}")
    clients[req.session_id] = client
    settings = client.get_settings()
    _save_session(req.session_id, settings, [])
    return {"status": "ok"}

@app.post("/send")
def send_dm(req: SendReq):
    client = clients.get(req.session_id)
    if not client:
        raise HTTPException(404, "Session not found")
    results = {}
    for user in req.recipients:
        try:
            uid = client.user_id_from_username(user)
            client.direct_send(req.message, [uid])
            results[user] = "sent"
        except Exception as e:
            results[user] = f"error: {e}"
        if req.interval > 0:
            time.sleep(req.interval)
    return {"results": results}

@app.post("/upload_reel")
async def upload_reel(
    session_id: str = Form(...),
    file: UploadFile = File(...),
    caption: str = Form("")
):
    client = clients.get(session_id)
    if not client:
        raise HTTPException(404, "Session not found")
    save_path = f"/tmp/{uuid.uuid4().hex}_{file.filename}"
    with open(save_path, "wb") as f:
        f.write(await file.read())
    try:
        res = client.clip_upload(save_path, caption)
    except Exception as e:
        raise HTTPException(400, f"Reel upload failed: {e}")
    finally:
        os.remove(save_path)
    return {"result": res}

def _session_file(session_id: str):
    return os.path.join(SESSIONS_DIR, f"{session_id}.json")

def _save_session(session_id: str, settings: dict, followers: list):
    data = {"settings": settings, "followers": followers}
    with open(_session_file(session_id), "w") as f:
        json.dump(data, f)

def _load_session(session_id: str):
    path = _session_file(session_id)
    if not os.path.exists(path):
        return None
    with open(path) as f:
        return json.load(f)

def _start_follow_poll(session_id: str):
    def poll():
        while True:
            sess = _load_session(session_id)
            if not sess:
                break
            client = clients.get(session_id)
            if not client:
                client = Client(settings=sess["settings"])
                clients[session_id] = client
            try:
                me = client.user_id
                current = client.user_followers(me)
                current_ids = list(current.keys())
                old = sess.get("followers", [])
                new = [uid for uid in current_ids if uid not in old]
                for uid in new:
                    info = client.user_info(uid)
                    payload = {
                        "session_id": session_id,
                        "follower_id": uid,
                        "follower_username": info.username
                    }
                    if FOLLOW_WEBHOOK_URL:
                        try:
                            requests.post(FOLLOW_WEBHOOK_URL, json=payload, timeout=5)
                        except:
                            pass
                _save_session(session_id, sess["settings"], current_ids)
            except:
                pass
            time.sleep(FOLLOW_POLL_INTERVAL)
    threading.Thread(target=poll, daemon=True).start()

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=3017)
