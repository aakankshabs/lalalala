# app.py — BharatQualify Flask Backend
# Deploy on Render (render.com) as a Web Service

from flask import Flask, request, jsonify
from flask_cors import CORS
import requests
import json
import os
from datetime import datetime

app = Flask(__name__)
CORS(app)

# ─── CONFIG — put these in Render environment variables ───────────────────────
VAPI_API_KEY        = os.environ.get("VAPI_API_KEY")
VAPI_PHONE_NUMBER_ID= os.environ.get("VAPI_PHONE_NUMBER_ID")   # from Vapi dashboard
VAPI_ASSISTANT_ID   = os.environ.get("VAPI_ASSISTANT_ID")      # Maya's assistant ID

AIRTABLE_API_KEY    = os.environ.get("AIRTABLE_API_KEY")
AIRTABLE_BASE_ID    = os.environ.get("AIRTABLE_BASE_ID")
AIRTABLE_TABLE_NAME = os.environ.get("AIRTABLE_TABLE_NAME", "Leads")

TWILIO_ACCOUNT_SID  = os.environ.get("TWILIO_ACCOUNT_SID")
TWILIO_AUTH_TOKEN   = os.environ.get("TWILIO_AUTH_TOKEN")
TWILIO_WHATSAPP_FROM= os.environ.get("TWILIO_WHATSAPP_FROM")   # e.g. whatsapp:+14155238886
SALES_REP_WHATSAPP  = os.environ.get("SALES_REP_WHATSAPP")     # e.g. whatsapp:+919876543210
# ─────────────────────────────────────────────────────────────────────────────


# ═══════════════════════════════════════════════════════════
# ROUTE 1: Netlify form submission webhook
# Netlify calls this when someone submits the lead form
# ═══════════════════════════════════════════════════════════
@app.route("/webhook/lead", methods=["POST"])
def handle_lead():
    data = request.json or request.form.to_dict()
    
    name        = data.get("name", "")
    phone       = data.get("phone", "")
    company     = data.get("company", "")
    team_size   = data.get("team_size", "")
    lead_volume = data.get("lead_volume", "")
    challenge   = data.get("challenge", "")

    if not phone:
        return jsonify({"error": "phone required"}), 400

    # 1. Save lead to Airtable immediately
    airtable_id = save_to_airtable({
        "Name": name,
        "Phone": phone,
        "Company": company,
        "Team Size": team_size,
        "Monthly Lead Volume": lead_volume,
        "Challenge": challenge,
        "Status": "New",
        "Submitted At": datetime.utcnow().isoformat()
    })

    # 2. Send WhatsApp to lead: "Maya will call in 60s"
    send_whatsapp(
        to=f"whatsapp:{phone}",
        message=f"Namaste {name}! 🙏 Main Maya hoon, BharatQualify se. Aapka form receive ho gaya. Main aapko abhi 60 seconds mein call karti hoon aapke business ke baare mein jaanne ke liye. Please apna phone ready rakhein! 📞"
    )

    # 3. Trigger Maya's call via Vapi
    call_id = trigger_vapi_call(phone, name, company, team_size, challenge)

    return jsonify({
        "success": True,
        "airtable_id": airtable_id,
        "call_id": call_id,
        "message": "Lead received. Maya is calling!"
    })


# ═══════════════════════════════════════════════════════════
# ROUTE 2: Vapi call completion webhook
# Vapi calls this when Maya finishes the conversation
# ═══════════════════════════════════════════════════════════
@app.route("/webhook/vapi", methods=["POST"])
def handle_vapi_webhook():
    data = request.json
    event_type = data.get("message", {}).get("type", "")

    if event_type != "end-of-call-report":
        return jsonify({"received": True})

    call_data    = data.get("message", {})
    transcript   = call_data.get("transcript", "")
    summary      = call_data.get("summary", "")
    call_id      = call_data.get("call", {}).get("id", "")
    phone_number = call_data.get("customer", {}).get("number", "")
    duration_sec = call_data.get("durationSeconds", 0)

    # Extract structured answers from transcript
    answers = extract_answers_from_transcript(transcript)

    # Score the lead
    score, score_breakdown = score_lead(answers, duration_sec)

    # Update Airtable with results
    update_airtable_by_phone(phone_number, {
        "Score": score,
        "Budget": answers.get("budget", ""),
        "Timeline": answers.get("timeline", ""),
        "Decision Maker": answers.get("decision_maker", ""),
        "Pain Point": answers.get("pain_point", ""),
        "Call Duration (s)": duration_sec,
        "Transcript": transcript[:10000],  # Airtable field limit
        "Summary": summary,
        "Status": "Qualified" if score >= 60 else "Not Qualified",
        "Call ID": call_id
    })

    # If hot lead (score ≥ 60), notify sales rep on WhatsApp
    if score >= 60:
        rep_message = build_rep_message(answers, score, phone_number, summary)
        send_whatsapp(to=SALES_REP_WHATSAPP, message=rep_message)

    return jsonify({"success": True, "score": score})


# ═══════════════════════════════════════════════════════════
# HELPER: Trigger Vapi call
# ═══════════════════════════════════════════════════════════
def trigger_vapi_call(phone, name, company, team_size, challenge):
    url = "https://api.vapi.ai/call/phone"
    headers = {
        "Authorization": f"Bearer {VAPI_API_KEY}",
        "Content-Type": "application/json"
    }
    
    payload = {
        "phoneNumberId": VAPI_PHONE_NUMBER_ID,
        "assistantId": VAPI_ASSISTANT_ID,
        "customer": {
            "number": phone,
            "name": name
        },
        # Pass context to Maya so she knows who she's calling
        "assistantOverrides": {
            "variableValues": {
                "lead_name": name,
                "company_name": company,
                "team_size": team_size,
                "challenge": challenge
            }
        }
    }

    try:
        response = requests.post(url, headers=headers, json=payload, timeout=10)
        return response.json().get("id", "")
    except Exception as e:
        print(f"Vapi call error: {e}")
        return ""


# ═══════════════════════════════════════════════════════════
# HELPER: Send WhatsApp via Twilio
# ═══════════════════════════════════════════════════════════
def send_whatsapp(to, message):
    from twilio.rest import Client
    try:
        client = Client(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)
        client.messages.create(
            from_=TWILIO_WHATSAPP_FROM,
            to=to,
            body=message
        )
    except Exception as e:
        print(f"WhatsApp error: {e}")


# ═══════════════════════════════════════════════════════════
# HELPER: Save lead to Airtable
# ═══════════════════════════════════════════════════════════
def save_to_airtable(fields):
    url = f"https://api.airtable.com/v0/{AIRTABLE_BASE_ID}/{AIRTABLE_TABLE_NAME}"
    headers = {
        "Authorization": f"Bearer {AIRTABLE_API_KEY}",
        "Content-Type": "application/json"
    }
    try:
        response = requests.post(url, headers=headers, json={"fields": fields}, timeout=10)
        return response.json().get("id", "")
    except Exception as e:
        print(f"Airtable save error: {e}")
        return ""


# ═══════════════════════════════════════════════════════════
# HELPER: Update Airtable record by phone number
# ═══════════════════════════════════════════════════════════
def update_airtable_by_phone(phone, fields):
    # First find the record
    search_url = f"https://api.airtable.com/v0/{AIRTABLE_BASE_ID}/{AIRTABLE_TABLE_NAME}"
    headers = {"Authorization": f"Bearer {AIRTABLE_API_KEY}"}
    
    try:
        params = {"filterByFormula": f"{{Phone}}='{phone}'", "maxRecords": 1}
        response = requests.get(search_url, headers=headers, params=params, timeout=10)
        records = response.json().get("records", [])
        
        if records:
            record_id = records[0]["id"]
            update_url = f"{search_url}/{record_id}"
            requests.patch(
                update_url,
                headers={**headers, "Content-Type": "application/json"},
                json={"fields": fields},
                timeout=10
            )
    except Exception as e:
        print(f"Airtable update error: {e}")


# ═══════════════════════════════════════════════════════════
# SCORING ENGINE — BANT Framework
# Budget · Authority · Need · Timeline
# ═══════════════════════════════════════════════════════════
def score_lead(answers, duration_sec):
    score = 0
    breakdown = {}

    # Budget (0–25 pts)
    budget = answers.get("budget", "").lower()
    if any(x in budget for x in ["5 lakh", "10 lakh", "50k", "1 lakh", "budget hai"]):
        breakdown["budget"] = 25; score += 25
    elif any(x in budget for x in ["sochna", "discuss", "check"]):
        breakdown["budget"] = 15; score += 15
    elif budget:
        breakdown["budget"] = 8; score += 8
    else:
        breakdown["budget"] = 0

    # Authority (0–25 pts)
    authority = answers.get("decision_maker", "").lower()
    if any(x in authority for x in ["main", "hum", "i decide", "mujhe", "owner", "founder", "ceo", "head"]):
        breakdown["authority"] = 25; score += 25
    elif any(x in authority for x in ["team", "boss", "manager"]):
        breakdown["authority"] = 12; score += 12
    else:
        breakdown["authority"] = 5; score += 5

    # Need (0–25 pts)
    need = answers.get("pain_point", "").lower()
    if len(need) > 30:  # Detailed pain point = real need
        breakdown["need"] = 25; score += 25
    elif len(need) > 10:
        breakdown["need"] = 15; score += 15
    else:
        breakdown["need"] = 5; score += 5

    # Timeline (0–25 pts)
    timeline = answers.get("timeline", "").lower()
    if any(x in timeline for x in ["abhi", "this month", "asap", "jaldi", "immediately", "1 month", "2 month"]):
        breakdown["timeline"] = 25; score += 25
    elif any(x in timeline for x in ["quarter", "3 month", "6 month"]):
        breakdown["timeline"] = 15; score += 15
    else:
        breakdown["timeline"] = 5; score += 5

    # Engagement bonus: if they talked for > 2 minutes
    if duration_sec > 120:
        score = min(100, score + 5)
        breakdown["engagement_bonus"] = 5

    return min(100, score), breakdown


# ═══════════════════════════════════════════════════════════
# HELPER: Extract answers from transcript text
# Simple keyword matching — upgrade to GPT extraction later
# ═══════════════════════════════════════════════════════════
def extract_answers_from_transcript(transcript):
    answers = {
        "budget": "",
        "timeline": "",
        "decision_maker": "",
        "pain_point": ""
    }

    lines = transcript.split("\n")
    capture_next = None

    for line in lines:
        line_lower = line.lower()

        # Budget
        if "budget" in line_lower or "kitna kharch" in line_lower:
            capture_next = "budget"
        # Timeline
        elif "kab" in line_lower or "timeline" in line_lower or "when" in line_lower:
            capture_next = "timeline"
        # Decision maker
        elif "kaun decide" in line_lower or "who decides" in line_lower or "authority" in line_lower:
            capture_next = "decision_maker"
        # Pain point
        elif "problem" in line_lower or "challenge" in line_lower or "issue" in line_lower or "dikkat" in line_lower:
            capture_next = "pain_point"
        elif capture_next and line.startswith("User:"):
            # This is the lead's response to the previous question
            answers[capture_next] = line.replace("User:", "").strip()
            capture_next = None

    return answers


# ═══════════════════════════════════════════════════════════
# HELPER: Build rep notification message
# ═══════════════════════════════════════════════════════════
def build_rep_message(answers, score, phone, summary):
    emoji = "🔥" if score >= 80 else "✅"
    return f"""
{emoji} *HOT LEAD ALERT — BharatQualify*

📞 Phone: {phone}
🎯 Score: *{score}/100*

📋 *BANT Summary:*
💰 Budget: {answers.get('budget', 'N/A')}
⏰ Timeline: {answers.get('timeline', 'N/A')}
👤 Decision Maker: {answers.get('decision_maker', 'N/A')}
😤 Pain Point: {answers.get('pain_point', 'N/A')}

📝 *Maya's Summary:*
{summary}

👉 Call them NOW while they're warm!
    """.strip()


# ═══════════════════════════════════════════════════════════
# ROUTE 3: Health check (Render needs this)
# ═══════════════════════════════════════════════════════════
@app.route("/health", methods=["GET"])
def health():
    return jsonify({"status": "ok", "agent": "Maya", "version": "1.0"})


if __name__ == "__main__":
    app.run(debug=False, host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
