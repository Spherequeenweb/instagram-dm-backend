from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from instagrapi import Client
from supabase import create_client
import time, os
from datetime import datetime, timedelta

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

SUPABASE_URL = os.environ.get("SUPABASE_URL")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY")
sb = create_client(SUPABASE_URL, SUPABASE_KEY)

cl = Client()
is_logged_in = False

class InstagramCreds(BaseModel):
    username: str
    password: str

class RunCampaignRequest(BaseModel):
    dry_run: bool = False

@app.get("/health")
def health():
    return {"status": "online", "logged_in": is_logged_in}

@app.post("/instagram/verify")
def verify_instagram(creds: InstagramCreds):
    global cl, is_logged_in
    try:
        cl = Client()
        cl.login(creds.username, creds.password)
        is_logged_in = True
        settings = sb.table("settings").select("id").limit(1).execute().data
        if settings:
            sb.table("settings").update({
                "instagram_username": creds.username,
                "instagram_password": creds.password
            }).eq("id", settings[0]["id"]).execute()
        return {"success": True, "message": "Instagram connected!"}
    except Exception as e:
        is_logged_in = False
        return {"success": False, "error": str(e)}

@app.on_event("startup")
def auto_login():
    global cl, is_logged_in
    try:
        settings = sb.table("settings").select("*").limit(1).execute().data
        if settings and settings[0].get("instagram_username"):
            s = settings[0]
            cl = Client()
            cl.login(s["instagram_username"], s["instagram_password"])
            is_logged_in = True
            print("Auto login successful!")
    except Exception as e:
        print(f"Auto login failed: {e}")

@app.post("/campaign/run")
def run_campaign(req: RunCampaignRequest):
    global cl, is_logged_in
    try:
        settings = sb.table("settings").select("*").limit(1).execute().data[0]
        templates = sb.table("message_templates").select("*").execute().data
        initial_msg = next(t["content"] for t in templates if t["type"] == "initial")

        if not is_logged_in:
            cl = Client()
            cl.login(settings["instagram_username"], settings["instagram_password"])
            is_logged_in = True

        today_start = datetime.now().replace(hour=0, minute=0, second=0).isoformat()
        sent_today = sb.table("dm_logs").select("id").gte("sent_at", today_start).eq("status", "sent").execute().data
        remaining = settings["daily_limit"] - len(sent_today)

        if remaining <= 0:
            return {"success": False, "message": "Aaj ka daily limit complete ho gaya!"}

        prospects = sb.table("prospects").select("*").eq("status", "pending").limit(remaining).execute().data

        if not prospects:
            return {"success": False, "message": "Koi pending prospect nahi hai!"}

        sent = 0
        failed = 0
        errors = []

        for p in prospects:
            try:
                url = p["instagram_url"].strip().rstrip("/")
                username = url.split("/")[-1].replace("@", "")
                msg = initial_msg.replace("{{name}}", p.get("name") or username)
                now = datetime.now().isoformat()

                if not req.dry_run:
                    user_id = cl.user_id_from_username(username)
                    cl.direct_send(msg, [user_id])

                next_followup = (datetime.now() + timedelta(days=2)).isoformat()

                sb.table("prospects").update({
                    "status": "dm_sent",
                    "instagram_username": username,
                    "initial_dm_sent_at": now,
                    "last_contacted_at": now,
                    "next_followup_at": next_followup
                }).eq("id", p["id"]).execute()

                sb.table("dm_logs").insert({
                    "prospect_id": p["id"],
                    "message_type": "initial",
                    "sent_at": now,
                    "status": "sent" if not req.dry_run else "dry_run"
                }).execute()

                sent += 1
                if not req.dry_run:
                    time.sleep(settings["delay_seconds"])

            except Exception as e:
                failed += 1
                errors.append(str(e))
                sb.table("dm_logs").insert({
                    "prospect_id": p["id"],
                    "message_type": "initial",
                    "sent_at": datetime.now().isoformat(),
                    "status": "failed",
                    "error_message": str(e)
                }).execute()

        return {"success": True, "sent": sent, "failed": failed, "dry_run": req.dry_run, "errors": errors}

    except Exception as e:
        return {"success": False, "error": str(e)}

@app.post("/campaign/followups")
def run_followups():
    global cl, is_logged_in
    try:
        settings = sb.table("settings").select("*").limit(1).execute().data[0]
        templates = sb.table("message_templates").select("*").execute().data

        if not is_logged_in:
            cl = Client()
            cl.login(settings["instagram_username"], settings["instagram_password"])
            is_logged_in = True

        now = datetime.now()
        due_prospects = sb.table("prospects").select("*").lte(
            "next_followup_at", now.isoformat()
        ).in_("status", ["dm_sent", "followup_1_sent", "followup_2_sent"]).execute().data

        sent = 0
        failed = 0

        for p in due_prospects:
            if p["status"] == "dm_sent":
                msg_type = "followup_1"
                next_status = "followup_1_sent"
                days_next = 3
            elif p["status"] == "followup_1_sent":
                msg_type = "followup_2"
                next_status = "followup_2_sent"
                days_next = 3
            else:
                msg_type = "followup_3"
                next_status = "followup_3_sent"
                days_next = 999

            msg_template = next((t["content"] for t in templates if t["type"] == msg_type), None)
            if not msg_template:
                continue

            try:
                username = p.get("instagram_username") or p["instagram_url"].split("/")[-1].rstrip("/")
                msg = msg_template.replace("{{name}}", p.get("name") or username)
                user_id = cl.user_id_from_username(username)
                cl.direct_send(msg, [user_id])

                sent_at = now.isoformat()
                next_followup = (now + timedelta(days=days_next)).isoformat()

                sb.table("prospects").update({
                    "status": next_status,
                    "last_contacted_at": sent_at,
                    "next_followup_at": next_followup
                }).eq("id", p["id"]).execute()

                sb.table("dm_logs").insert({
                    "prospect_id": p["id"],
                    "message_type": msg_type,
                    "sent_at": sent_at,
                    "status": "sent"
                }).execute()

                sent += 1
                time.sleep(settings["delay_seconds"])

            except Exception as e:
                failed += 1
                sb.table("dm_logs").insert({
                    "prospect_id": p["id"],
                    "message_type": msg_type,
                    "sent_at": now.isoformat(),
                    "status": "failed",
                    "error_message": str(e)
                }).execute()

        return {"success": True, "sent": sent, "failed": failed}

    except Exception as e:
        return {"success": False, "error": str(e)}

@app.get("/campaign/status")
def get_status():
    try:
        today_start = datetime.now().replace(hour=0, minute=0, second=0).isoformat()
        sent_today = sb.table("dm_logs").select("id").gte("sent_at", today_start).eq("status", "sent").execute().data
        pending_count = sb.table("prospects").select("id").eq("status", "pending").execute().data
        followup_due = sb.table("prospects").select("id").lte("next_followup_at", datetime.now().isoformat()).in_("status", ["dm_sent", "followup_1_sent", "followup_2_sent"]).execute().data
        recent_logs = sb.table("dm_logs").select("*").order("sent_at", desc=True).limit(10).execute().data

        return {
            "sent_today": len(sent_today),
            "pending_prospects": len(pending_count),
            "followups_due": len(followup_due),
            "recent_logs": recent_logs,
            "backend_online": True,
            "instagram_connected": is_logged_in
        }
    except Exception as e:
        return {"error": str(e)}
