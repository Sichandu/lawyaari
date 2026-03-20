from fastapi import FastAPI, HTTPException, Request, Depends, Header
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel
from typing import Optional, List
from datetime import datetime, timedelta
from pymongo import MongoClient
import os, random, string, hashlib, hmac, json, re
from dotenv import load_dotenv
import httpx
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, HRFlowable
from reportlab.lib.units import inch
from reportlab.lib import colors
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
import io
from fastapi.responses import StreamingResponse
import razorpay

load_dotenv()

app = FastAPI(title="Lawyaari API", version="2.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ─── DB ──────────────────────────────────────────────────────────────────────
# MONGO_URL = os.getenv("MONGO_URL", "mongodb://localhost:27017")
# client = MongoClient(MONGO_URL)
# db = client["lawyaari"]

# users_col       = db["users"]
# sessions_col    = db["sessions"]
# otps_col        = db["otps"]
# chats_col       = db["chats"]
# payments_col    = db["payments"]
# analytics_col   = db["analytics"]
# triggers_col    = db["triggers"]

import os
from pymongo import MongoClient

MONGO_URL = os.getenv("MONGO_URL")

if not MONGO_URL:
    raise Exception("MONGO_URL is not set!")

client = MongoClient(MONGO_URL)
db = client["lawyaari"]

users_col       = db["users"]
sessions_col    = db["sessions"]
otps_col        = db["otps"]
chats_col       = db["chats"]
payments_col    = db["payments"]
analytics_col   = db["analytics"]
triggers_col    = db["triggers"]

# ─── API KEYS ─────────────────────────────────────────────────────────────────
GROQ_API_KEY     = os.getenv("GROQ_API_KEY", "")
OPENAI_API_KEY   = os.getenv("OPENAI_API_KEY", "")
RAZORPAY_KEY_ID  = os.getenv("RAZORPAY_KEY_ID", "")
RAZORPAY_SECRET  = os.getenv("RAZORPAY_SECRET", "")

GROQ_MODEL   = "llama-3.3-70b-versatile"
OPENAI_MODEL = "gpt-4o"  # swap to gpt-5 when available

# ─── ADMIN FREE ACCESS ────────────────────────────────────────────────────────
# Set ADMIN_MOBILE=9876543210 in .env (comma-separate multiple numbers)
ADMIN_MOBILES = set(filter(None, os.getenv("ADMIN_MOBILE", "").split(",")))

def is_admin_mobile(mobile):
    return bool(mobile and mobile in ADMIN_MOBILES)

def check_payment(payment_id, service, mobile=None):
    """Pass if admin mobile OR verified payment exists."""
    if is_admin_mobile(mobile):
        return True
    rec = payments_col.find_one({"payment_id": payment_id, "service": service, "status": "verified"})
    return rec is not None

# ─── INTENT TRIGGERS ─────────────────────────────────────────────────────────
INTENT_MAP = {
    "cheque_bounce": {
        "keywords": ["cheque bounce", "cheque bounced", "check bounce", "dishonour", "138",
                     "cheque wapas", "cheque return", "నిజమైన చెక్", "చెక్ బౌన్స్"],
        "service": "cheque_notice",
        "cta": {"hi": "क़ानूनी नोटिस बनाएं (₹149)", "te": "లీగల్ నోటీస్ రూ.149", "hinglish": "Legal Notice generate karo (₹149)"},
        "price": 149
    },
    "msme_payment": {
        "keywords": ["payment nahi mila", "payment not received", "invoice due", "msme payment",
                     "outstanding payment", "amount nahi diya", "చెల్లించలేదు", "పేమెంట్"],
        "service": "msme_notice",
        "cta": {"hi": "MSME रिकवरी नोटिस (₹249)", "te": "MSME నోటీస్ రూ.249", "hinglish": "MSME Recovery Notice banao (₹249)"},
        "price": 249
    },
    "complaint": {
        "keywords": ["complaint", "shikayat", "police complaint", "consumer complaint",
                     "cybercrime", "fraud", "धोखा", "ఫిర్యాదు", "పోలీస్"],
        "service": "complaint_draft",
        "cta": {"hi": "शिकायत ड्राफ्ट करें (₹69)", "te": "ఫిర్యాదు తయారు చేయండి ₹69", "hinglish": "Complaint draft karo (₹69)"},
        "price": 69
    },
    "legal_reply": {
        "keywords": ["notice mila", "notice received", "reply karna", "jawab dena",
                     "bank notice", "threat message", "recovery agent", "నోటీస్ వచ్చింది"],
        "service": "legal_reply",
        "cta": {"hi": "क़ानूनी जवाब बनाएं (₹19)", "te": "లీగల్ రిప్లై రూ.19", "hinglish": "Legal Reply generate karo (₹19)"},
        "price": 19
    }
}

# ─── HELPERS ─────────────────────────────────────────────────────────────────
def detect_intent(text: str) -> Optional[dict]:
    text_lower = text.lower()
    for intent_key, intent_data in INTENT_MAP.items():
        for kw in intent_data["keywords"]:
            if kw.lower() in text_lower:
                return {"intent": intent_key, **intent_data}
    return None

def route_model(task: str, language: str) -> str:
    """Smart model routing:
    - Groq (llama-3.3-70b): Hindi, Hinglish free chat
    - OpenAI (gpt-4o): Telugu (all), legal documents, premium, high accuracy
    Telugu routing to OpenAI because Groq's llama model has weak Telugu support.
    If OpenAI key not set, bidirectional fallback to Groq handles it.
    """
    if language == "te":
        return "openai"  # Telugu always → OpenAI (better multilingual support)
    if task in ("legal_document", "high_accuracy", "premium"):
        return "openai"
    return "groq"

async def call_groq(messages: list, system: str = "") -> str:
    if not GROQ_API_KEY:
        return "⚠️ Groq API key not configured. Please add GROQ_API_KEY to .env"
    headers = {"Authorization": f"Bearer {GROQ_API_KEY}", "Content-Type": "application/json"}
    payload = {
        "model": GROQ_MODEL,
        "messages": [{"role": "system", "content": system}] + messages if system else messages,
        "max_tokens": 1024,
        "temperature": 0.7
    }
    async with httpx.AsyncClient(timeout=30) as c:
        r = await c.post("https://api.groq.com/openai/v1/chat/completions", headers=headers, json=payload)
        r.raise_for_status()
        return r.json()["choices"][0]["message"]["content"]

async def call_openai(messages: list, system: str = "") -> str:
    if not OPENAI_API_KEY:
        return "⚠️ OpenAI API key not configured. Please add OPENAI_API_KEY to .env"
    headers = {"Authorization": f"Bearer {OPENAI_API_KEY}", "Content-Type": "application/json"}
    payload = {
        "model": OPENAI_MODEL,
        "messages": [{"role": "system", "content": system}] + messages if system else messages,
        "max_tokens": 2048,
        "temperature": 0.5
    }
    async with httpx.AsyncClient(timeout=60) as c:
        r = await c.post("https://api.openai.com/v1/chat/completions", headers=headers, json=payload)
        r.raise_for_status()
        return r.json()["choices"][0]["message"]["content"]

async def call_ai(messages: list, system: str, task: str = "normal_chat", language: str = "hi") -> str:
    model = route_model(task, language)
    
    # Try primary model first
    try:
        if model == "openai":
            return await call_openai(messages, system)
        else:
            return await call_groq(messages, system)
    except Exception as primary_err:
        pass
    
    # Always fallback to Groq if OpenAI fails (no key / 401 / quota)
    # Always fallback to OpenAI if Groq fails
    try:
        if model == "openai":
            return await call_groq(messages, system)
        else:
            return await call_openai(messages, system)
    except Exception as fallback_err:
        return f"⚠️ Both AI services unavailable. Check your API keys in .env file. (Groq: GROQ_API_KEY, OpenAI: OPENAI_API_KEY)"

def get_token(authorization: Optional[str] = Header(None)) -> Optional[str]:
    if authorization and authorization.startswith("Bearer "):
        return authorization[7:]
    return None

def get_user_from_token(token: str) -> Optional[dict]:
    if not token:
        return None
    session = sessions_col.find_one({"token": token, "expires_at": {"$gt": datetime.utcnow()}})
    if not session:
        return None
    user = users_col.find_one({"mobile": session["mobile"]})
    return user

def log_analytics(event: str, data: dict):
    analytics_col.insert_one({"event": event, "data": data, "timestamp": datetime.utcnow()})

def generate_token(length=32):
    return ''.join(random.choices(string.ascii_letters + string.digits, k=length))

def get_system_prompt(language: str, category: str = "general") -> str:
    # Legal documents MUST be in formal English regardless of UI language
    # Hinglish/casual language in a legal notice is unprofessional and can be rejected
    if category == "legal":
        return """You are Lawyaari, a professional legal document drafting assistant for Indian law.

LANGUAGE: Write ALL legal documents in formal English only. No Hindi, Telugu, or Hinglish.

DATA BINDING RULES (most important):
- Use ONLY the exact names, amounts, and dates explicitly provided in the input
- If a required value is missing or blank, write [REQUIRED: field_name] — NEVER invent or guess it
- Do NOT hallucinate dates — if a date is not given, write [INSERT DATE] as a placeholder
- Do NOT mix up party names — Supplier/Claimant is the one sending the notice, Buyer/Opponent receives it
- AMOUNT FORMAT: Always write as "Rs. X,XX,XXX" (Indian format, e.g. Rs. 1,80,000 not 180000)

LEGAL ACCURACY RULES:
- Cite ONLY laws that directly apply to the specific facts given
- Do NOT apply laws from unrelated contexts (e.g. Section 138 NI Act is ONLY for cheque bounce)
- If unsure whether a section applies, omit it — better to cite nothing than cite wrongly
- Legal context mapping: bank/loan → RBI guidelines, Banking Ombudsman; landlord/tenant → Rent Control Act, Transfer of Property Act; police → IPC Sections 166/323; consumer → Consumer Protection Act 2019; MSME → MSMED Act 2006

FORMATTING RULES:
- Use A. B. C. D. for section headers
- Use numbered lists 1. 2. 3. for steps
- Use bullet points with - for checklists
- No markdown asterisks ** anywhere
- Keep paragraphs short and precise

End every document with: "Note: This document is AI-generated. Consult a qualified lawyer before sending."
"""

    lang_instruction = {
        "hi": "Always respond in Hindi (Devanagari script). Be helpful and simple.",
        "te": "IMPORTANT: Always respond ONLY in Telugu script (తెలుగు). Every word of your response must be in Telugu. Do NOT mix English words unless they are proper legal terms with no Telugu equivalent. Use simple, clear Telugu that common people can understand. Cite Indian laws by their Telugu names where possible.",
        "hinglish": "Respond in Hinglish (Hindi written in English script). Keep it casual yet accurate.",
    }.get(language, "Respond in Hindi.")

    return f"""You are Lawyaari, an AI legal assistant for Indian citizens — especially MSMEs, workers, and common people.
{lang_instruction}

Scope: Indian law only. Categories: MSME, Banking, Cyber Crime, Police Rights, Medical Rights, Consumer Rights, Labour Law, Property, Women Rights, Tax.

Rules:
- Give practical, actionable advice
- Cite relevant Indian laws/sections when applicable
- Do NOT assume facts not given
- Never give advice on illegal activities
- Add disclaimer: "यह कानूनी सलाह नहीं है। किसी वकील से सलाह लें।" (in relevant language)
- Keep responses concise and clear"""

# ─── MODELS ──────────────────────────────────────────────────────────────────
class SendOTPRequest(BaseModel):
    mobile: str

class VerifyOTPRequest(BaseModel):
    mobile: str
    otp: str

class ChatRequest(BaseModel):
    message: str
    language: str = "hi"
    session_token: Optional[str] = None
    conversation_history: Optional[List[dict]] = []

class ChequeNoticeRequest(BaseModel):
    client_name: str
    client_address: str
    drawer_name: str
    drawer_address: str
    cheque_number: str
    cheque_date: str
    cheque_amount: str
    bank_name: str
    dishonour_reason: str
    language: str = "hi"
    payment_id: str
    mobile: Optional[str] = None

class MSMENoticeRequest(BaseModel):
    business_name: str
    buyer_name: str
    buyer_address: str
    invoice_number: str
    invoice_date: str
    outstanding_amount: str
    due_date: str
    udyam_number: Optional[str] = ""
    language: str = "hi"
    payment_id: str
    mobile: Optional[str] = None

class LegalReplyRequest(BaseModel):
    received_message: str
    context: str
    tone: str
    user_name: str
    language: str = "hi"
    payment_id: str
    mobile: Optional[str] = None
    your_facts: Optional[str] = ""

class ComplaintDraftRequest(BaseModel):
    issue_type: str
    issue_description: str
    location: str
    opponent_name: Optional[str] = ""
    date: str
    user_name: str
    language: str = "hi"
    payment_id: str
    mobile: Optional[str] = None

class PriorityAnswerRequest(BaseModel):
    question: str
    priority_flag: bool = False
    language: str = "hi"
    payment_id: Optional[str] = None
    mobile: Optional[str] = None

class CreateOrderRequest(BaseModel):
    amount: int
    service: str
    mobile: Optional[str] = None

class VerifyPaymentRequest(BaseModel):
    razorpay_order_id: str
    razorpay_payment_id: str
    razorpay_signature: str
    service: str
    mobile: Optional[str] = None

class GeneratePDFRequest(BaseModel):
    content: str
    title: str
    service_type: str

# ─── AUTH ─────────────────────────────────────────────────────────────────────
@app.post("/api/auth/send-otp")
async def send_otp(req: SendOTPRequest, request: Request):
    mobile = req.mobile.strip()
    if not re.match(r"^\d{10}$", mobile):
        raise HTTPException(400, "Invalid mobile number")

    # Rate limit: max 3 OTPs per 10 min
    ten_min_ago = datetime.utcnow() - timedelta(minutes=10)
    recent = otps_col.count_documents({"mobile": mobile, "created_at": {"$gt": ten_min_ago}})
    if recent >= 3:
        raise HTTPException(429, "Too many OTP requests. Wait 10 minutes.")

    otp = str(random.randint(100000, 999999))
    otps_col.insert_one({
        "mobile": mobile,
        "otp": otp,
        "created_at": datetime.utcnow(),
        "expires_at": datetime.utcnow() + timedelta(minutes=5),
        "status": "unused"
    })

    log_analytics("otp_sent", {"mobile": mobile, "ip": request.client.host})
    return {"success": True, "otp": otp, "message": "OTP sent (shown for demo)"}

@app.post("/api/auth/verify-otp")
async def verify_otp(req: VerifyOTPRequest, request: Request):
    record = otps_col.find_one({
        "mobile": req.mobile,
        "otp": req.otp,
        "status": "unused",
        "expires_at": {"$gt": datetime.utcnow()}
    })
    if not record:
        log_analytics("otp_invalid", {"mobile": req.mobile, "ip": request.client.host})
        raise HTTPException(400, "Invalid or expired OTP")

    otps_col.update_one({"_id": record["_id"]}, {"$set": {"status": "used"}})

    user = users_col.find_one({"mobile": req.mobile})
    if not user:
        users_col.insert_one({
            "mobile": req.mobile,
            "created_at": datetime.utcnow(),
            "last_login_at": datetime.utcnow(),
            "login_count": 1,
            "plan": "free",
            "daily_chats": 0,
            "last_chat_date": None
        })
    else:
        users_col.update_one({"mobile": req.mobile}, {
            "$set": {"last_login_at": datetime.utcnow()},
            "$inc": {"login_count": 1}
        })

    token = generate_token()
    sessions_col.insert_one({
        "mobile": req.mobile,
        "token": token,
        "created_at": datetime.utcnow(),
        "expires_at": datetime.utcnow() + timedelta(days=30)
    })

    log_analytics("otp_verified", {"mobile": req.mobile, "ip": request.client.host})
    return {"success": True, "token": token, "mobile": req.mobile}

@app.get("/api/auth/me")
async def get_me(authorization: Optional[str] = Header(None)):
    token = get_token(authorization)
    user = get_user_from_token(token)
    if not user:
        raise HTTPException(401, "Unauthorized")
    mobile = user["mobile"]
    return {
        "mobile": mobile,
        "plan": user.get("plan", "free"),
        "created_at": user["created_at"],
        "is_admin": is_admin_mobile(mobile)
    }

# ─── CHAT ─────────────────────────────────────────────────────────────────────
@app.post("/api/chat")
async def chat(req: ChatRequest, request: Request):
    language = req.language
    mobile = None
    user = None

    if req.session_token:
        user = get_user_from_token(req.session_token)
        if user:
            mobile = user["mobile"]

    # Usage limits
    today = datetime.utcnow().date().isoformat()
    limit = {"hi": 5, "hinglish": 5, "te": 3}.get(language, 5)

    if mobile:
        # Admin mobile gets unlimited chats
        if is_admin_mobile(mobile):
            limit = 9999
        user_doc = users_col.find_one({"mobile": mobile})
        plan = user_doc.get("plan", "free") if user_doc else "free"
        if plan == "pro":
            limit = 10 if language == "te" else 999
        elif plan == "unlimited":
            limit = 999

        # Check daily count
        last_date = user_doc.get("last_chat_date") if user_doc else None
        daily_count = user_doc.get("daily_chats", 0) if user_doc else 0
        if last_date != today:
            daily_count = 0
            users_col.update_one({"mobile": mobile}, {"$set": {"daily_chats": 0, "last_chat_date": today}})

        if daily_count >= limit:
            return {
                "paywall": True,
                "language": language,
                "plans": [
                    {"name": "Pro", "price": 49, "chats": 10 if language == "te" else "10/day", "plan_id": "pro"},
                    {"name": "Unlimited", "price": 99, "chats": "Unlimited", "plan_id": "unlimited"}
                ] if language == "te" else [
                    {"name": "Pro", "price": 49, "chats": "10/day", "plan_id": "pro"},
                    {"name": "Unlimited", "price": 99, "chats": "Unlimited", "plan_id": "unlimited"}
                ]
            }
    
    # Detect intent for smart CTAs
    intent = detect_intent(req.message)

    # Build conversation
    history = req.conversation_history or []
    messages = history + [{"role": "user", "content": req.message}]

    system = get_system_prompt(language)
    response_text = await call_ai(messages, system, task="normal_chat", language=language)

    # Track usage
    if mobile:
        users_col.update_one({"mobile": mobile}, {"$inc": {"daily_chats": 1}, "$set": {"last_chat_date": today}})
        daily_count += 1

    chats_col.insert_one({
        "mobile": mobile,
        "message": req.message,
        "response": response_text,
        "language": language,
        "intent": intent["intent"] if intent else None,
        "timestamp": datetime.utcnow()
    })
    log_analytics("chat", {"mobile": mobile, "language": language, "intent": intent["intent"] if intent else None})

    # Get remaining chats
    remaining = max(0, limit - (daily_count if mobile else 0))

    result = {
        "response": response_text,
        "remaining_chats": remaining,
        "total_limit": limit,
        "intent": None
    }

    if intent:
        cta_text = intent["cta"].get(language, intent["cta"].get("hi", ""))
        result["intent"] = {
            "type": intent["intent"],
            "service": intent["service"],
            "cta": cta_text,
            "price": intent["price"]
        }

    return result

# ─── PREMIUM: CHEQUE BOUNCE ───────────────────────────────────────────────────
@app.post("/api/cheque-notice")
async def cheque_notice(req: ChequeNoticeRequest):
    # Verify payment (admin mobile bypasses)
    mobile = getattr(req, "mobile", None)
    if not check_payment(req.payment_id, "cheque_notice", mobile):
        raise HTTPException(402, "Payment required or not verified")

    today = datetime.utcnow().strftime('%d %B %Y')
    system = get_system_prompt(req.language, "legal")
    prompt = f"""Generate a professional Section 138 NI Act legal notice for a dishonoured cheque.

DATA PROVIDED — USE EXACTLY AS GIVEN, DO NOT MODIFY OR INVENT:
Complainant (Notice Sender): {req.client_name}
Complainant Address: {req.client_address}
Cheque Drawer (Notice Recipient): {req.drawer_name}
Drawer Address: {req.drawer_address}
Cheque Number: {req.cheque_number}
Cheque Date: {req.cheque_date}
Cheque Amount: Rs. {req.cheque_amount} (write in Indian format e.g. Rs. 1,80,000)
Bank: {req.bank_name}
Dishonour Reason: {req.dishonour_reason}
Notice Date: {today}

DATE RULES:
- Notice date = {today} (use this exact date)
- Cheque presentation date: NOT PROVIDED — write "[Date of Presentation to Bank]" as placeholder
- Do NOT invent any dates — use only what is given above

REQUIRED OUTPUT:

A. Section 138 Suitability Check
[Assess whether facts meet all 4 conditions for Section 138: (1) cheque for debt/liability (2) presented within 3 months (3) dishonoured (4) notice within 30 days. State clearly if eligible.]

B. Formal Legal Notice
[Complete ready-to-send notice. Structure:
- Notice date: {today}
- To: drawer name and address
- Re: Section 138 NI Act, cheque no, date, amount
- Body: state cheque details, dishonour reason, demand payment within 15 days
- Consequence: criminal proceedings under Section 138 NI Act
- Sd/-: complainant name and address]

C. Document Checklist
[Numbered list of documents to keep ready for court]

D. Next Steps
[Numbered steps: send by registered post, 15-day wait, filing procedure]"""

    response = await call_ai([{"role": "user", "content": prompt}], system, task="legal_document", language=req.language)
    log_analytics("cheque_notice_generated", {"payment_id": req.payment_id})
    return {"content": response, "service": "cheque_notice"}

# ─── PREMIUM: MSME NOTICE ─────────────────────────────────────────────────────
@app.post("/api/msme-notice")
async def msme_notice(req: MSMENoticeRequest):
    mobile = getattr(req, "mobile", None)
    if not check_payment(req.payment_id, "msme_notice", mobile):
        raise HTTPException(402, "Payment required or not verified")

    today = datetime.utcnow().strftime('%d %B %Y')
    # Validate critical fields
    if not req.outstanding_amount or req.outstanding_amount.strip() in ['', '0']:
        return {"content": "Error: Outstanding amount is required. Please fill the amount field and try again.", "service": "msme_notice"}
    if not req.business_name or req.business_name.strip() == '':
        return {"content": "Error: Your business name is required. Please fill the supplier/business name field.", "service": "msme_notice"}

    system = get_system_prompt(req.language, "legal")
    prompt = f"""Generate a professional MSME Payment Recovery Demand Notice under MSMED Act, 2006.

DATA PROVIDED — USE EXACTLY AS GIVEN, DO NOT MODIFY OR SWAP PARTIES:
Supplier / Claimant (the one sending this notice): {req.business_name}
Udyam Registration No: {req.udyam_number or '[REQUIRED: Udyam Registration Number]'}
Buyer / Defaulter (the one who owes money): {req.buyer_name}
Buyer Address: {req.buyer_address}
Invoice Number: {req.invoice_number}
Invoice Date: {req.invoice_date}
Outstanding Amount: Rs. {req.outstanding_amount}
Payment Due Date: {req.due_date}
Notice Date: {today}

PARTY BINDING RULE: The notice is FROM {req.business_name} TO {req.buyer_name}.
{req.business_name} is the SUPPLIER who is owed money.
{req.buyer_name} is the BUYER who must pay.
DO NOT swap or confuse these two parties anywhere in the document.

REQUIRED OUTPUT:

A. MSME Eligibility Check
[Verify: Is Udyam number provided? Is the supplier an MSME? Is the payment overdue beyond 45 days per Section 15 MSMED Act?]

B. Formal Demand Notice
[Complete ready-to-send letter. Must include:
- From: {req.business_name} (Udyam No: {req.udyam_number or '[REQUIRED]'})
- Date: {today}
- To: {req.buyer_name}, {req.buyer_address}
- Subject: Demand Notice under MSMED Act 2006
- Body: invoice details, amount Rs. {req.outstanding_amount}, overdue since {req.due_date}, cite Section 15 and 16 MSMED Act, demand payment + interest within 15 days, warn of Section 18 proceedings
- Signed: {req.business_name}]

C. Next Steps
[Step-by-step: MSME Samadhaan portal → Facilitation Council → Section 18 court proceedings]

D. Evidence Checklist
[Numbered list of documents: Udyam certificate, invoice copy, delivery proof, bank statement, correspondence]"""

    response = await call_ai([{"role": "user", "content": prompt}], system, task="legal_document", language=req.language)
    log_analytics("msme_notice_generated", {"payment_id": req.payment_id})
    return {"content": response, "service": "msme_notice"}

# ─── PREMIUM: LEGAL REPLY ─────────────────────────────────────────────────────
@app.post("/api/legal-reply")
async def legal_reply(req: LegalReplyRequest):
    if not check_payment(req.payment_id, "legal_reply", getattr(req, "mobile", None)):
        raise HTTPException(402, "Payment required or not verified")

    # GTM Fix: validate this is actually a legal notice, not a random message
    legal_keywords = ["notice", "demand", "legal", "court", "dues", "payment",
                      "landlord", "recovery", "loan", "emi", "eviction", "cheque",
                      "police", "fir", "arrest", "complaint", "advocate", "lawyer",
                      "suit", "proceedings", "section", "act", "default", "bank"]
    msg_lower = req.received_message.lower()
    is_legal_notice = any(kw in msg_lower for kw in legal_keywords)

    if not is_legal_notice:
        return {
            "content": (
                "Advisory: The message you provided does not appear to be a legal notice.\n\n"
                "This service is for replying to:\n"
                "- Legal notices from banks, courts, or advocates\n"
                "- Demand letters from landlords or employers\n"
                "- Recovery agent communications\n"
                "- Police or government notices\n\n"
                "Please paste the actual legal notice text you received and try again."
            ),
            "service": "legal_reply"
        }

    today = datetime.utcnow().strftime('%d %B %Y')
    system = get_system_prompt(req.language, "legal")
    prompt = (
        f"Generate a professional legal reply letter for an Indian legal context.\n\n"
        f"The recipient received this notice:\n---\n{req.received_message}\n---\n\n"
        f"Reply sender: {req.user_name}\n"
        f"Context: {req.context}\n"
        + (f"KEY FACTS TO ASSERT (user-provided — the reply MUST be built around these):\n{req.your_facts}\n\n" if req.your_facts and req.your_facts.strip() else "")
        + f"Tone: {req.tone} (polite=cooperative but firm; firm=assertive; strict_legal=maximum legal pressure)\n"
        f"Today's date: {today}\n\n"
        "STRICT RULES:\n"
        "- Write in formal English only\n"
        "- Do NOT assume facts not in the notice\n"
        f"- Always use today's date {today} — never leave [date] as placeholder\n"
        "- CONTEXT-SPECIFIC LAW MAPPING — cite ONLY from the correct context:\n"
        "  bank/loan context → RBI Fair Practice Code, Banking Ombudsman Scheme 2006, RBI Circular on loan recovery\n"
        "  landlord/tenant context → Transfer of Property Act 1882, relevant State Rent Control Act, NOT Section 138 NI Act\n"
        "  employer context → Industrial Disputes Act 1947, Payment of Wages Act 1936\n"
        "  recovery agent context → RBI Guidelines on Recovery Agents, Section 504/506 IPC (criminal intimidation)\n"
        "  police context → Section 166 IPC (public servant misconduct), Section 154 CrPC (right to FIR)\n"
        "- Section 138 NI Act is ONLY for cheque bounce — NEVER use it in landlord/tenant or loan disputes\n"
        "- Keep legally safe — no false statements\n\n"
        "REQUIRED OUTPUT FORMAT:\n"
        f"Date: {today}\n\n"
        "To,\n[Name and designation of notice sender]\n\n"
        "Subject: Response to your notice/communication\n\n"
        "Dear Sir/Madam,\n\n"
        "[Reply body: acknowledge notice → clarify position → assert rights → firm close]\n\n"
        f"Yours sincerely,\n{req.user_name}\n[Space for signature]"
    )

    response = await call_ai([{"role": "user", "content": prompt}], system,
                              task="legal_document", language=req.language)
    log_analytics("legal_reply_generated", {"payment_id": req.payment_id})
    return {"content": response, "service": "legal_reply"}


# ─── PREMIUM: COMPLAINT DRAFT ─────────────────────────────────────────────────
@app.post("/api/complaint-draft")
async def complaint_draft(req: ComplaintDraftRequest):
    if not check_payment(req.payment_id, "complaint_draft", getattr(req, "mobile", None)):
        raise HTTPException(402, "Payment required or not verified")

    # Auto-select authority and IPC sections based on issue type
    ISSUE_MAP = {
        "police": {
            "authority": "The Superintendent of Police",
            "sections": "Section 166 IPC (public servant disobeying law), Section 323 IPC (causing hurt), Section 341 IPC (wrongful restraint) — cite only if applicable to facts",
            "copy_to": "Copy to: Inspector General of Police; National Human Rights Commission (NHRC)"
        },
        "cybercrime": {
            "authority": "The Cyber Crime Cell / SP Cyber Crime",
            "sections": "Section 66 IT Act 2000 (computer-related offences), Section 66C IT Act (identity theft), Section 420 IPC (cheating) — cite only if applicable",
            "copy_to": "Copy to: National Cyber Crime Reporting Portal (cybercrime.gov.in)"
        },
        "consumer": {
            "authority": "The District Consumer Disputes Redressal Commission",
            "sections": "Section 35 Consumer Protection Act 2019 (complaint filing), Section 2(7) (definition of consumer) — cite only if applicable",
            "copy_to": "Copy to: Concerned company/seller; State Consumer Commission"
        },
        "labour": {
            "authority": "The Labour Commissioner / Deputy Labour Commissioner",
            "sections": "Payment of Wages Act 1936, Industrial Disputes Act 1947 Section 2A (dismissal dispute), Minimum Wages Act 1948 — cite only if applicable",
            "copy_to": "Copy to: District Collector; Labour Court"
        },
        "medical": {
            "authority": "The State Medical Council / District Health Officer",
            "sections": "Indian Medical Council (Professional Conduct) Regulations 2002, Consumer Protection Act 2019 Section 35 — cite only if applicable",
            "copy_to": "Copy to: National Medical Commission; District Consumer Forum"
        },
        "other": {
            "authority": "The Competent Authority / Concerned Department",
            "sections": "Relevant sections as applicable to the facts",
            "copy_to": ""
        }
    }
    issue_info = ISSUE_MAP.get(req.issue_type, ISSUE_MAP["other"])

    today = datetime.utcnow().strftime('%d %B %Y')
    system = get_system_prompt(req.language, "legal")
    prompt = f"""Generate a complete, court-ready formal complaint in Indian legal format.

DATA PROVIDED — USE EXACTLY AS GIVEN:
Complainant: {req.user_name}
Issue Type: {req.issue_type}
Date of Incident: {req.date}
Location: {req.location}
Opposite Party: {req.opponent_name or '[REQUIRED: Name of opposite party]'}
Facts: {req.issue_description}
Today's Date: {today}

Filing Authority: {issue_info['authority']}, {req.location}
Applicable Laws: {issue_info['sections']}
{issue_info['copy_to']}

STRICT RULES:
- Use ONLY facts given above — do NOT invent any additional facts
- Cite laws ONLY if facts clearly support them
- Amount format: Rs. X,XX,XXX
- Use firm, direct language — not "I would appreciate" but "I hereby demand" or "You are directed to"

REQUIRED OUTPUT — include ALL sections below:

[Complainant name and address]
[Date: {today}]

A. To: [Full authority name], {req.location}

B. Subject: [One clear subject line — specific, not generic]

C. Facts:
[Numbered facts — only what is given, no additions or inventions]

D. Legal Grounds:
[Cite applicable sections. For consumer cases ALWAYS cite:
  - Section 2(7) Consumer Protection Act 2019 (definition of consumer)
  - Section 2(47) Consumer Protection Act 2019 (deficiency of service / unfair trade practice)
  - Section 35 Consumer Protection Act 2019 (right to file complaint)
  For other types, cite only what clearly applies]

E. Jurisdiction:
[State WHY this specific forum has jurisdiction — for consumer: "The value of goods/services is Rs. X which falls within the pecuniary jurisdiction of the District Commission under Section 34 CPA 2019"]

F. Relief Sought:
[Numbered list of SPECIFIC remedies — replacement/refund + compensation + litigation costs]

G. Verification:
I, {req.user_name}, do hereby verify and declare that the contents of this complaint are true and correct to the best of my knowledge and belief. Nothing material has been concealed.
Place: {req.location}
Date: {today}
Signature: _______________
{req.user_name}

H. List of Annexures:
[Numbered list — Annexure A, B, C etc. mapping to each piece of evidence mentioned in the facts]

{issue_info['copy_to']}"""

    response = await call_ai([{"role": "user", "content": prompt}], system, task="legal_document", language=req.language)
    log_analytics("complaint_draft_generated", {"payment_id": req.payment_id})
    return {"content": response, "service": "complaint_draft"}

# ─── PRIORITY ANSWER ──────────────────────────────────────────────────────────
@app.post("/api/priority-answer")
async def priority_answer(req: PriorityAnswerRequest):
    if req.priority_flag:
        # Admin mobile bypasses payment check
        admin_ok = is_admin_mobile(getattr(req, "mobile", None))
        if not admin_ok:
            if not req.payment_id:
                raise HTTPException(402, "Payment required for priority answer")
            if not check_payment(req.payment_id, "priority_answer", None):
                raise HTTPException(402, "Payment not verified")

    system = get_system_prompt(req.language)

    if req.priority_flag:
        prompt = f"""Answer the following legal question with maximum detail and structure.
Question: "{req.question}"

Rules:
- Give clear, actionable steps
- Do NOT give generic advice
- Cite exact Indian laws/sections
- Be precise, not vague

Output format:
🔍 Situation:
[Understanding of the situation]

⚖️ Legal Position:
[Relevant laws, sections, rights]

✅ What You Should Do:
1.
2.
3.

💬 What You Can Say/Write:
[Exact words or template]

⚠️ Important Note:
[Any warnings or caveats]"""
        task = "high_accuracy"
    else:
        prompt = f"""Answer this legal question concisely: "{req.question}" """
        task = "normal_chat"

    response = await call_ai([{"role": "user", "content": prompt}], system, task=task, language=req.language)
    return {"content": response, "priority": req.priority_flag, "service": "priority_answer"}

# ─── FONT SETUP (Vera — bundled with ReportLab, works on all servers) ────────
import os as _os, logging as _logging
_VERA_PATH      = _os.path.join(_os.path.dirname(__import__('reportlab').__file__), 'fonts', 'Vera.ttf')
_VERA_BOLD_PATH = _os.path.join(_os.path.dirname(__import__('reportlab').__file__), 'fonts', 'VeraBd.ttf')

def _register_fonts():
    try:
        registered = pdfmetrics.getRegisteredFontNames()
        if 'LD-Regular' not in registered:
            pdfmetrics.registerFont(TTFont('LD-Regular', _VERA_PATH))
        if 'LD-Bold' not in registered:
            pdfmetrics.registerFont(TTFont('LD-Bold', _VERA_BOLD_PATH))
        return True
    except Exception as e:
        _logging.warning(f"Font registration failed: {e}")
        return False

_FONTS_OK = _register_fonts()
_FN      = 'LD-Regular' if _FONTS_OK else 'Helvetica'
_FN_BOLD = 'LD-Bold'    if _FONTS_OK else 'Helvetica-Bold'

def _clean(text: str) -> str:
    """Convert markdown to ReportLab XML. Escape special chars safely."""
    import re as _r
    # Bold / italic
    text = _r.sub(r'\*\*(.+?)\*\*', r'<b>\1</b>', text)
    text = _r.sub(r'\*(.+?)\*',   r'<i>\1</i>', text)
    # Strip # headers (keep text)
    text = _r.sub(r'^#+\s*', '', text, flags=_r.MULTILINE)
    text = text.replace('`', '')
    # Protect our injected tags during XML escaping
    text = text.replace('<b>', '\x00B\x00').replace('</b>', '\x00/B\x00')
    text = text.replace('<i>', '\x00I\x00').replace('</i>', '\x00/I\x00')
    # Escape
    text = text.replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')
    # Restore tags
    text = text.replace('\x00B\x00', '<b>').replace('\x00/B\x00', '</b>')
    text = text.replace('\x00I\x00', '<i>').replace('\x00/I\x00', '</i>')
    return text

def _para(text: str, style) -> Paragraph:
    """Safe paragraph with fallback."""
    try:
        return Paragraph(_clean(text), style)
    except Exception:
        safe = re.sub(r'<[^>]+>', '', text)
        safe = safe.replace('&','&amp;').replace('<','&lt;').replace('>','&gt;')
        try:
            return Paragraph(safe, style)
        except Exception:
            return Paragraph('', style)

# ─── PDF GENERATION ───────────────────────────────────────────────────────────
@app.post("/api/generate-pdf")
async def generate_pdf(req: GeneratePDFRequest):
    try:
        buffer = io.BytesIO()
        doc = SimpleDocTemplate(
            buffer, pagesize=A4,
            rightMargin=0.85*inch, leftMargin=0.85*inch,
            topMargin=0.9*inch,   bottomMargin=0.9*inch
        )

        # Styles — use module-level font names
        s_header  = ParagraphStyle("s_hdr",  fontName=_FN_BOLD, fontSize=11,
                        textColor=colors.HexColor("#f59e0b"), spaceAfter=1)
        s_sub     = ParagraphStyle("s_sub",  fontName=_FN, fontSize=8,
                        textColor=colors.HexColor("#999999"), spaceAfter=14)
        s_title   = ParagraphStyle("s_ttl",  fontName=_FN_BOLD, fontSize=14,
                        textColor=colors.HexColor("#1a1a2e"), spaceAfter=6)
        s_section = ParagraphStyle("s_sec",  fontName=_FN_BOLD, fontSize=11,
                        textColor=colors.HexColor("#1a1a2e"), spaceAfter=4, spaceBefore=12)
        s_body    = ParagraphStyle("s_bod",  fontName=_FN, fontSize=10.5,
                        leading=17, spaceAfter=5, textColor=colors.HexColor("#222222"))
        s_bullet  = ParagraphStyle("s_bul",  fontName=_FN, fontSize=10.5,
                        leading=16, spaceAfter=4, leftIndent=14,
                        textColor=colors.HexColor("#333333"))
        s_footer  = ParagraphStyle("s_ftr",  fontName=_FN, fontSize=7.5,
                        textColor=colors.HexColor("#999999"), spaceBefore=8, alignment=1)

        els = []

        # ── Letterhead ──
        els.append(_para("Lawyaari  |  lawyaari.com", s_header))
        els.append(_para("Your legal rights, explained.", s_sub))
        els.append(HRFlowable(width="100%", thickness=1.5,
            color=colors.HexColor("#f59e0b"), spaceAfter=10))
        els.append(_para(req.title, s_title))
        els.append(HRFlowable(width="100%", thickness=0.5,
            color=colors.HexColor("#dddddd"), spaceAfter=8))
        els.append(Spacer(1, 0.08*inch))

        # ── Smart content parsing ──
        for raw_line in req.content.split("\n"):
            line = raw_line.strip()
            if not line:
                els.append(Spacer(1, 0.05*inch))
                continue

            is_section_header = (
                (len(line) < 90 and len(line) > 2
                 and line[0].upper() in "ABCDEFGHIJ"
                 and len(line) > 1 and line[1] == ".")
                or (line.endswith(":") and len(line) < 75)
            )
            is_bullet = (
                line.startswith("-") or line.startswith("*")
                or line.startswith("\u2022")
            )
            is_numbered = (
                len(line) > 2
                and line[0].isdigit()
                and line[1] in ".)"
            )

            if is_section_header:
                els.append(_para(line, s_section))
            elif is_bullet:
                els.append(_para("\u2022  " + line.lstrip("-*\u2022 "), s_bullet))
            elif is_numbered:
                els.append(_para(line, s_bullet))
            else:
                els.append(_para(line, s_body))

        # ── Footer ──
        els.append(Spacer(1, 0.25*inch))
        els.append(HRFlowable(width="100%", thickness=0.5,
            color=colors.HexColor("#dddddd"), spaceAfter=6))
        els.append(_para(
            f"Generated: {datetime.utcnow().strftime('%d %B %Y')}  |  "
            "Lawyaari is not a legal firm. This document is AI-generated. "
            "Consult a qualified lawyer before taking any legal action.",
            s_footer
        ))

        doc.build(els)
        buffer.seek(0)

        fname = f"legaldost_{req.service_type}_{datetime.utcnow().strftime('%Y%m%d%H%M%S')}.pdf"
        return StreamingResponse(
            buffer, media_type="application/pdf",
            headers={"Content-Disposition": f"attachment; filename={fname}"}
        )

    except Exception as e:
        _logging.error(f"PDF generation error: {e}", exc_info=True)
        raise HTTPException(500, f"PDF generation failed: {str(e)}")


# ─── PAYMENTS ─────────────────────────────────────────────────────────────────
@app.post("/api/create-order")
async def create_order(req: CreateOrderRequest):
    if not RAZORPAY_KEY_ID or not RAZORPAY_SECRET:
        # Demo mode
        demo_order_id = f"order_demo_{generate_token(16)}"
        payments_col.insert_one({
            "order_id": demo_order_id,
            "amount": req.amount,
            "service": req.service,
            "mobile": req.mobile,
            "status": "created",
            "created_at": datetime.utcnow()
        })
        return {"order_id": demo_order_id, "amount": req.amount, "currency": "INR", "demo": True}

    rz = razorpay.Client(auth=(RAZORPAY_KEY_ID, RAZORPAY_SECRET))
    order = rz.order.create({
        "amount": req.amount * 100,
        "currency": "INR",
        "notes": {"service": req.service, "mobile": req.mobile or ""}
    })
    payments_col.insert_one({
        "order_id": order["id"],
        "amount": req.amount,
        "service": req.service,
        "mobile": req.mobile,
        "status": "created",
        "created_at": datetime.utcnow()
    })
    return {"order_id": order["id"], "amount": req.amount, "currency": "INR", "key": RAZORPAY_KEY_ID}

@app.post("/api/verify-payment")
async def verify_payment(req: VerifyPaymentRequest):
    if not RAZORPAY_SECRET:
        # Demo mode: auto-verify
        payment_id = f"pay_demo_{generate_token(16)}"
        payments_col.update_one(
            {"order_id": req.razorpay_order_id},
            {"$set": {"status": "verified", "payment_id": payment_id, "verified_at": datetime.utcnow()}}
        )
        log_analytics("payment_verified_demo", {"service": req.service, "order_id": req.razorpay_order_id})
        return {"success": True, "payment_id": payment_id}

    # Real verification
    body = f"{req.razorpay_order_id}|{req.razorpay_payment_id}"
    expected = hmac.new(RAZORPAY_SECRET.encode(), body.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, req.razorpay_signature):
        raise HTTPException(400, "Invalid payment signature")

    payments_col.update_one(
        {"order_id": req.razorpay_order_id},
        {"$set": {"status": "verified", "payment_id": req.razorpay_payment_id, "verified_at": datetime.utcnow()}}
    )

    # Upgrade plan if subscription
    if req.service in ("pro", "unlimited") and req.mobile:
        users_col.update_one({"mobile": req.mobile}, {"$set": {"plan": req.service}})

    log_analytics("payment_verified", {"service": req.service, "payment_id": req.razorpay_payment_id})
    return {"success": True, "payment_id": req.razorpay_payment_id}

# ─── ADMIN ────────────────────────────────────────────────────────────────────
@app.get("/api/admin/stats")
async def admin_stats(x_admin_key: Optional[str] = Header(None)):
    if x_admin_key != os.getenv("ADMIN_KEY", "lawyaari-admin-2024"):
        raise HTTPException(403, "Forbidden")

    total_users = users_col.count_documents({})
    total_chats = chats_col.count_documents({})
    paid_services = payments_col.count_documents({"status": "verified"})
    total_revenue = sum(p.get("amount", 0) for p in payments_col.find({"status": "verified"}))

    # Intent breakdown
    intent_pipeline = [
        {"$match": {"intent": {"$ne": None}}},
        {"$group": {"_id": "$intent", "count": {"$sum": 1}}},
        {"$sort": {"count": -1}}
    ]
    intents = list(chats_col.aggregate(intent_pipeline))

    # Language breakdown
    lang_pipeline = [
        {"$group": {"_id": "$language", "count": {"$sum": 1}}},
        {"$sort": {"count": -1}}
    ]
    languages = list(chats_col.aggregate(lang_pipeline))

    # Recent chats
    recent_chats = list(chats_col.find({}, {"_id": 0, "mobile": 1, "message": 1, "intent": 1, "language": 1, "timestamp": 1})
                        .sort("timestamp", -1).limit(20))
    for c in recent_chats:
        if "timestamp" in c:
            c["timestamp"] = c["timestamp"].isoformat()

    # Conversion rate
    users_who_paid = payments_col.distinct("mobile", {"status": "verified"})
    conversion_rate = round(len(users_who_paid) / max(total_users, 1) * 100, 1)

    return {
        "total_users": total_users,
        "total_chats": total_chats,
        "paid_services": paid_services,
        "total_revenue": total_revenue,
        "conversion_rate": f"{conversion_rate}%",
        "intent_breakdown": intents,
        "language_breakdown": languages,
        "recent_chats": recent_chats
    }

@app.get("/health")
async def health():
    return {"status": "ok", "version": "2.0.0", "service": "lawyaari"}