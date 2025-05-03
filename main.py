import os
import json
import uuid
import time
import threading
from typing import Dict, Any, List
from fastapi import FastAPI, HTTPException, Query, UploadFile, File, Form
from pydantic import BaseModel
from instagrapi import Client
from instagrapi.exceptions import ChallengeRequired, TwoFactorRequired
import requests

app = FastAPI(title="Instagram Automation API v14", version="14.0.0")
FOLLOW_WEBHOOK_URL = os.getenv("FOLLOW_WEBHOOK_URL", "")
FOLLOW_POLL_INTERVAL = int(os.getenv("FOLLOW_POLL_INTERVAL_SECONDS", "60"))

clients: Dict[str, Client] = {}
SESSIONS_DIR = "/data/sessions"
os.makedirs(SESSIONS_DIR, exist_ok=True)

class LoginReq(BaseModel):
    username: str
    password: str

class VerifyReq(BaseModel):
    session_id: str
    code: str

class CookieLoginReq(BaseModel):
    sessionid: str

class SendReq(BaseModel):
    session_id: str
    recipients: List[str]
    message: str
    interval: float = 0.0

class LikeReq(BaseModel):
    session_id: str
    media_id: str

class CommentReq(BaseModel):
    session_id: str
    media_id: str
    comment: str

def session_file(sid: str) -> str:
    return os.path.join(SESSIONS_DIR, f"{sid}.json")

def save_session(sid: str, data: Dict[str, Any]):
    with open(session_file(sid), "w") as f:
        json.dump(data, f)

def load_session(sid: str) -> Dict[str, Any]:
    path = session_file(sid)
    if not os.path.exists(path):
        raise HTTPException(404, "Session not found")
    return json.load(open(path))

def start_follow_poll(session_id: str):
    def poll():
        while True:
            data = load_session(session_id)
            settings = data["settings"]
            followers_old = data.get("followers", [])
            initialized = data.get("initialized", False)
            client = clients.get(session_id)
            if not client:
                client = Client(settings=settings)
                clients[session_id] = client
            try:
                me = client.user_id
                current = client.user_followers(me)
                current_ids = list(current.keys())
                if not initialized:
                    save_session(session_id, {"settings": settings, "followers": current_ids, "initialized": True})
                else:
                    new = [uid for uid in current_ids if uid not in followers_old]
                    for uid in new:
                        info = client.user_info(uid)
                        payload = {"session_id": session_id, "follower_id": uid, "follower_username": info.username}
                        if FOLLOW_WEBHOOK_URL:
                            try:
                                requests.post(FOLLOW_WEBHOOK_URL, json=payload, timeout=5)
                            except:
                                pass
                    save_session(session_id, {"settings": settings, "followers": current_ids, "initialized": True})
            except:
                pass
            time.sleep(FOLLOW_POLL_INTERVAL)
    threading.Thread(target=poll, daemon=True).start()

@app.post("/login")
def login(req: LoginReq):
    client = Client()
    sid = str(uuid.uuid4())
    try:
        # Perform login without skip_challenge
        client.login(req.username, req.password)
    except TwoFactorRequired:
        clients[sid] = client
        save_session(sid, {"settings": {}, "followers": [], "initialized": False, "credentials": {"user": req.username, "pass": req.password}})
        return {"session_id": sid, "two_factor": True}
    except ChallengeRequired:
        clients[sid] = client
        save_session(sid, {"settings": {}, "followers": [], "initialized": False, "credentials": {"user": req.username, "pass": req.password}})
        return {"session_id": sid, "challenge": True}
    settings = client.get_settings()
    clients[sid] = client
    save_session(sid, {"settings": settings, "followers": [], "initialized": False})
    start_follow_poll(sid)
    return {"session_id": sid}

@app.post("/login-verify")
def login_verify(req: VerifyReq):
    data = load_session(req.session_id)
    creds = data.get("credentials")
    if not creds:
        raise HTTPException(400, "No credentials stored")
    client = clients.get(req.session_id) or Client()
    client.login(creds["user"], creds["pass"], verification_code=req.code)
    settings = client.get_settings()
    clients[req.session_id] = client
    save_session(req.session_id, {"settings": settings, "followers": [], "initialized": False})
    start_follow_poll(req.session_id)
    return {"status": "ok"}

@app.post("/login-cookie")
def login_cookie(req: CookieLoginReq):
    sid = str(uuid.uuid4())
    sess = requests.Session()
    sess.cookies.set("sessionid", req.sessionid, domain=".instagram.com")
    sess.get("https://www.instagram.com")
    ck = sess.cookies.get_dict()
    settings = {"cookies": ck}
    client = Client(settings=settings)
    clients[sid] = client
    save_session(sid, {"settings": settings, "followers": [], "initialized": False})
    start_follow_poll(sid)
    return {"session_id": sid}

@app.post("/send")
def send_dm(req: SendReq):
    client = clients.get(req.session_id)
    if not client:
        raise HTTPException(404, "Session not found")
    results = {}
    for u in req.recipients:
        try:
            uid = client.user_id_from_username(u)
            client.direct_send(req.message, [uid])
            results[u] = "sent"
        except Exception as e:
            results[u] = f"error: {e}"
        if req.interval > 0:
            time.sleep(req.interval)
    return {"results": results}

@app.post("/upload_reel")
async def upload_reel(session_id: str = Form(...), file: UploadFile = File(...), caption: str = Form("")):
    client = clients.get(session_id)
    if not client:
        raise HTTPException(404, "Session not found")
    path_tmp = f"/tmp/{uuid.uuid4().hex}_{file.filename}"
    with open(path_tmp, "wb") as f:
        f.write(await file.read())
    try:
        res = client.clip_upload(path_tmp, caption)
    except Exception as e:
        raise HTTPException(400, f"Reel upload failed: {e}")
    finally:
        os.remove(path_tmp)
    return {"result": res}

@app.post("/like")
def like_media(req: LikeReq):
    client = clients.get(req.session_id)
    if not client:
        raise HTTPException(404, "Session not found")
    try:
        client.media_like(req.media_id)
        return {"status": "liked"}
    except Exception as e:
        raise HTTPException(400, f"Like failed: {e}")

@app.post("/comment")
def comment_media(req: CommentReq):
    client = clients.get(req.session_id)
    if not client:
        raise HTTPException(404, "Session not found")
    try:
        client.media_comment(req.media_id, req.comment)
        return {"status": "commented"}
    except Exception as e:
        raise HTTPException(400, f"Comment failed: {e}")

@app.get("/feed")
def get_feed(session_id: str = Query(...), limit: int = Query(10)):
    client = clients.get(session_id)
    if not client:
        raise HTTPException(404, "Session not found")
    feed = client.feed_timeline()[:limit]
    return {"feed": feed}

@app.get("/hashtag/{tag}")
def get_hashtag(tag: str, session_id: str = Query(...), limit: int = Query(10)):
    client = clients.get(session_id)
    if not client:
        raise HTTPException(404, "Session not found")
    medias = client.hashtag_medias(tag, amount=limit)
    return {"medias": medias}

@app.get("/profile/{username}")
def get_profile(username: str, session_id: str = Query(...)):
    client = clients.get(session_id)
    if not client:
        raise HTTPException(404, "Session not found")
    return client.get_profile(username)

@app.get("/search")
def search_profiles(keywords: str, session_id: str = Query(...), limit: int = Query(10)):
    client = clients.get(session_id)
    if not client:
        raise HTTPException(404, "Session not found")
    return {"results": client.search_people(keywords=keywords)[:limit]}

@app.get("/connections")
def get_connections(session_id: str = Query(...), limit: int = Query(None)):
    client = clients.get(session_id)
    if not client:
        raise HTTPException(404, "Session not found")
    conns = client.user_following(client.user_id)
    return {"connections": conns[:limit] if limit else conns}

@app.get("/sessions")
def list_sessions():
    return {"sessions": [f[:-5] for f in os.listdir(SESSIONS_DIR) if f.endswith(".json")]}

@app.delete("/sessions/{session_id}")
def delete_session(session_id: str):
    fp = session_file(session_id)
    if os.path.exists(fp):
        os.remove(fp)
        clients.pop(session_id, None)
        return {"deleted": session_id}
    raise HTTPException(404, "Session not found")
