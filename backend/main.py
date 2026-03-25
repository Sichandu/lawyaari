from fastapi import FastAPI, HTTPException, Request, Depends, Header
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
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

app = FastAPI(title="Bklchai API", version="2.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ─── DB ──────────────────────────────────────────────────────────────────────
MONGO_URL = os.getenv("MONGO_URL", "mongodb://localhost:27017")
client = MongoClient(MONGO_URL)
db = client["bklchai"]

users_col       = db["users"]
sessions_col    = db["sessions"]
otps_col        = db["otps"]
chats_col       = db["chats"]
payments_col    = db["payments"]
analytics_col   = db["analytics"]
triggers_col    = db["triggers"]
partners_col    = db["partners"]       # ✅ fixed
referrals_col   = db["referrals"]      # ✅ fixed
payouts_col     = db["payouts"]        # ✅ fixed

# ─── API KEYS ─────────────────────────────────────────────────────────────────
GROQ_API_KEY     = os.getenv("GROQ_API_KEY", "")
OPENAI_API_KEY   = os.getenv("OPENAI_API_KEY", "")
RAZORPAY_KEY_ID  = os.getenv("RAZORPAY_KEY_ID", "")
RAZORPAY_SECRET  = os.getenv("RAZORPAY_SECRET", "")

GROQ_MODEL   = "llama-3.3-70b-versatile"
OPENAI_MODEL = "gpt-4o"

# ─── ADMIN FREE ACCESS ────────────────────────────────────────────────────────
ADMIN_MOBILES = set(filter(None, os.getenv("ADMIN_MOBILE", "").split(",")))

def is_admin_mobile(mobile):
    return bool(mobile and mobile in ADMIN_MOBILES)

def check_payment(payment_id, service, mobile=None):
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
    if language == "te":
        return "openai"
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
    try:
        if model == "openai":
            return await call_openai(messages, system)
        else:
            return await call_groq(messages, system)
    except Exception as primary_err:
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
    if category == "legal":
        return """You are Bklchai, a professional legal document drafting assistant for Indian law.

LANGUAGE: Write ALL legal documents in formal English only. No Hindi, Telugu, or Hinglish.

DATA BINDING RULES (most important):
- Use ONLY the exact names, amounts, and dates explicitly provided in the input
- If a required value is missing or blank, write [REQUIRED: field_name] — NEVER invent or guess it
- Do NOT invent dates — only use dates explicitly given in the input — if a date is not given, write [INSERT DATE] as a placeholder
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

    return f"""You are Bklchai, an AI legal assistant for Indian citizens — especially MSMEs, workers, and common people.
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
    name: Optional[str] = None

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

class PartnerRegisterRequest(BaseModel):
    name: str
    mobile: str
    email: str
    upi_id: str
    address: str
    referral_name: str
    referral_mobile: str

class MarkPayoutRequest(BaseModel):
    ref_code: str
    amount: float
    utr: Optional[str] = ""

# ─── AUTH ─────────────────────────────────────────────────────────────────────
@app.post("/api/auth/send-otp")
async def send_otp(req: SendOTPRequest, request: Request):
    mobile = req.mobile.strip()
    if not re.match(r"^\d{10}$", mobile):
        raise HTTPException(400, "Invalid mobile number")

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
            "name": req.name or "User",
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

    today = datetime.utcnow().date().isoformat()
    limit = {"hi": 5, "hinglish": 5, "te": 3}.get(language, 5)

    if mobile:
        if is_admin_mobile(mobile):
            limit = 9999
        user_doc = users_col.find_one({"mobile": mobile})
        plan = user_doc.get("plan", "free") if user_doc else "free"
        if plan == "pro":
            limit = 10 if language == "te" else 999
        elif plan == "unlimited":
            limit = 999

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
                ]
            }
    
    intent = detect_intent(req.message)

    history = req.conversation_history or []
    messages = history + [{"role": "user", "content": req.message}]

    system = get_system_prompt(language)
    response_text = await call_ai(messages, system, task="normal_chat", language=language)

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
    mobile = getattr(req, "mobile", None)
    if not check_payment(req.payment_id, "cheque_notice", mobile):
        raise HTTPException(402, "Payment required or not verified")

    today = datetime.utcnow().strftime('%d %B %Y')
    system = get_system_prompt(req.language, "legal")
    prompt = (
        "Generate a complete, ready-to-print-and-send legal notice under Section 138 "
        "of the Negotiable Instruments Act, 1881. "
        "READY-TO-USE RULE: Write every line with actual data. Zero square brackets. Zero blank fields.\n\n"
        f"DATA — USE EXACTLY AS GIVEN:\n"
        f"Complainant: {req.client_name}\n"
        f"Complainant Address: {req.client_address}\n"
        f"Cheque Drawer: {req.drawer_name}\n"
        f"Drawer Address: {req.drawer_address}\n"
        f"Cheque No: {req.cheque_number}  |  Date: {req.cheque_date}  |  Amount: Rs. {req.cheque_amount}\n"
        f"Bank: {req.bank_name}\n"
        f"Dishonour Reason: {req.dishonour_reason}\n"
        f"Notice Date: {today}\n\n"
        "OUTPUT — write all 4 sections as a final document:\n\n"
        "A. Section 138 Suitability Check\n"
        f"Assess all 4 conditions using the actual facts. Cheque No {req.cheque_number} for "
        f"Rs. {req.cheque_amount} dishonoured due to {req.dishonour_reason}. "
        "State whether eligible for Section 138 proceedings in 3-4 complete sentences.\n\n"
        "B. Formal Legal Notice\n"
        f"Write the complete notice as it will be physically sent:\n"
        f"{today}\n\n"
        f"To,\n{req.drawer_name}\n{req.drawer_address}\n\n"
        f"Sub: Legal Notice under Section 138 of the Negotiable Instruments Act, 1881 for "
        f"dishonour of Cheque No. {req.cheque_number} dated {req.cheque_date} for Rs. {req.cheque_amount} "
        f"drawn on {req.bank_name}\n\n"
        "Dear Sir/Madam,\n\n"
        "Write the full body paragraphs covering: (1) the cheque was issued for a legitimate debt/liability, "
        f"(2) it was presented to {req.bank_name} for payment but dishonoured with reason "
        f"'{req.dishonour_reason}', (3) demand Rs. {req.cheque_amount} within 15 days of "
        "receipt of this notice, (4) failure will result in criminal proceedings under Section 138 "
        "of the Negotiable Instruments Act, 1881.\n\n"
        f"Yours faithfully,\n\n"
        f"Sd/-\n{req.client_name}\n{req.client_address}\n\n"
        "C. Document Checklist\n"
        "Numbered list of specific documents to keep ready for court filing.\n\n"
        "D. Next Steps\n"
        "Numbered steps with timelines: send by Speed Post / Registered Post AD within 30 days "
        "of dishonour, wait 15 days for response, then file complaint under Section 138 before "
        "the Magistrate court having jurisdiction."
    )

    response = await call_ai([{"role": "user", "content": prompt}], system, task="legal_document", language=req.language)
    log_analytics("cheque_notice_generated", {"payment_id": req.payment_id})
    return {"content": response, "service": "cheque_notice"}

# ─── PREMIUM: MSME NOTICE ─────────────────────────────────────────────────────
@app.post("/api/msme-notice")
async def msme_notice(req: MSMENoticeRequest):
    mobile = getattr(req, "mobile", None)
    if not check_payment(req.payment_id, "msme_notice", mobile):
        raise HTTPException(402, "Payment required or not verified")

    if not req.outstanding_amount or req.outstanding_amount.strip() in ['', '0']:
        return {"content": "Error: Outstanding amount is required. Please enter the invoice amount.", "service": "msme_notice"}
    if not req.business_name or not req.business_name.strip():
        return {"content": "Error: Your business name is required.", "service": "msme_notice"}
    if not req.buyer_name or not req.buyer_name.strip():
        return {"content": "Error: Buyer name is required.", "service": "msme_notice"}

    from datetime import date as _date, datetime as _dt
    today      = datetime.utcnow().strftime('%d %B %Y')
    today_date = _date.today()

    def parse_date(s):
        s = s.strip().replace('-', '/')
        parts = s.split('/')
        if len(parts) != 3:
            return None
        try:
            if len(parts[0]) == 4:
                return _date(int(parts[0]), int(parts[1]), int(parts[2]))
            else:
                return _date(int(parts[2]), int(parts[1]), int(parts[0]))
        except Exception:
            return None

    due_dt  = parse_date(req.due_date)
    inv_dt  = parse_date(req.invoice_date)

    if due_dt:
        days_overdue = max(0, (today_date - due_dt).days)
        days_since_invoice = max(0, (today_date - inv_dt).days) if inv_dt else None
    else:
        days_overdue = 90
        days_since_invoice = None

    overdue_str = f"{days_overdue} days" if days_overdue > 0 else "as of notice date"

    RBI_BANK_RATE  = 6.50
    MSME_INT_RATE  = round(RBI_BANK_RATE * 3, 2)

    try:
        principal = float(
            str(req.outstanding_amount)
            .replace(',', '').replace('Rs.', '').replace('Rs', '')
            .replace('₹', '').replace(' ', '').strip()
        )
        interest_amount = round(principal * (MSME_INT_RATE / 100) * (days_overdue / 365), 2)
        total_amount    = round(principal + interest_amount, 2)

        interest_label = f"Rs. {interest_amount:,.2f}"
        total_label    = f"Rs. {total_amount:,.2f}"
        principal_label = f"Rs. {principal:,.2f}"
    except Exception:
        principal       = None
        interest_amount = None
        total_amount    = None
        interest_label  = "to be computed at 19.5% p.a."
        total_label     = f"Rs. {req.outstanding_amount} + applicable interest"
        principal_label = f"Rs. {req.outstanding_amount}"

    udyam = req.udyam_number.strip() if req.udyam_number and req.udyam_number.strip() else "Not registered / to be verified"

    eligibility_block = (
        f"Supplier {req.business_name} holds Udyam Registration No. {udyam}, confirming its status "
        f"as a Micro, Small or Medium Enterprise under the MSMED Act, 2006. "
        f"Invoice No. {req.invoice_number} dated {req.invoice_date} for {principal_label} "
        f"was due for payment on {req.due_date}. As on {today}, the payment is overdue by {overdue_str}, "
        f"well beyond the 45-day mandatory payment window stipulated under Section 15 of the MSMED Act, 2006. "
        f"Interest at {MSME_INT_RATE}% per annum ({RBI_BANK_RATE}% RBI Bank Rate x 3) "
        f"is accordingly payable under Section 16 of the Act."
    )

    interest_calculation = (
        f"Section 16 Interest Calculation:\n"
        f"  Principal Amount       : {principal_label}\n"
        f"  Payment Due Date       : {req.due_date}\n"
        f"  Notice Date            : {today}\n"
        f"  Delay Period           : {overdue_str}\n"
        f"  RBI Bank Rate          : {RBI_BANK_RATE}% per annum\n"
        f"  Applicable MSME Rate   : {MSME_INT_RATE}% per annum (3 x RBI Bank Rate)\n"
        f"  Estimated Interest     : {interest_label}\n"
        f"  TOTAL AMOUNT DEMANDED  : {total_label}"
    )

    notice_header = (
        f"{req.business_name}\n"
        f"Udyam Registration No: {udyam}\n\n"
        f"{today}\n\n"
        f"To,\n"
        f"{req.buyer_name}\n"
        f"{req.buyer_address}\n\n"
        f"By Registered Post / Speed Post with Acknowledgement Due\n\n"
        f"Sub: DEMAND NOTICE for Payment of {principal_label} with Interest of {interest_label} "
        f"(Total: {total_label}) under the Micro, Small and Medium Enterprises Development Act, 2006 "
        f"— Invoice No. {req.invoice_number} dated {req.invoice_date}\n\n"
        f"Dear Sir / Madam,\n"
    )

    next_steps = (
        f"1. File a complaint on the MSME Samadhaan Portal "
        f"(https://samadhaan.msme.gov.in) against {req.buyer_name} "
        f"for non-payment of Invoice {req.invoice_number}.\n"
        f"2. Await response or facilitation by the MSME Facilitation Council "
        f"under Section 18 of the MSMED Act, 2006.\n"
        f"3. If unresolved within the statutory period, initiate proceedings before "
        f"the MSME Facilitation Council under Section 18 of the MSMED Act, 2006 "
        f"for recovery of {total_label} with continuing interest.\n"
        f"4. Consider reporting the buyer to the Ministry of MSME for delayed payment "
        f"on the TReDS platform and MSME Delayed Payment Monitoring System (MSME DEPREMS)."
    )

    evidence_checklist = (
        f"1. Udyam Registration Certificate of {req.business_name} (Udyam No: {udyam})\n"
        f"2. Copy of Invoice No. {req.invoice_number} dated {req.invoice_date} "
        f"for {principal_label}\n"
        f"3. Proof of delivery / acknowledgement of goods or services by {req.buyer_name}\n"
        f"4. Bank statement showing non-receipt of payment since {req.due_date}\n"
        f"5. All prior correspondence / emails / WhatsApp messages with {req.buyer_name} "
        f"regarding payment\n"
        f"6. Copy of this demand notice with postal acknowledgement (AD card)\n"
        f"7. Purchase order / work order from {req.buyer_name} (if available)"
    )

    system = get_system_prompt(req.language, "legal")
    prompt = f"""You are drafting a production-grade MSME payment recovery notice for a real business.
Use ONLY the data provided. Zero placeholders. Zero soft language. Every word must be print-ready.

=== PRE-FILLED DATA (do not change any figure or name) ===
Supplier (Claimant): {req.business_name}
Udyam No: {udyam}
Buyer (Defaulter): {req.buyer_name}
Buyer Address: {req.buyer_address}
Invoice No: {req.invoice_number} dated {req.invoice_date}
Principal Due: {principal_label}
Due Date: {req.due_date}
Days Overdue: {overdue_str}
Interest Rate: {MSME_INT_RATE}% p.a. (Section 16, 3x RBI Bank Rate)
Interest Amount: {interest_label}
Total Demanded: {total_label}
Notice Date: {today}

=== PRE-COMPUTED ELIGIBILITY ===
{eligibility_block}

=== PRE-COMPUTED INTEREST ===
{interest_calculation}

PARTY BINDING RULE: Notice is FROM {req.business_name} TO {req.buyer_name}. DO NOT swap these parties.

=== OUTPUT INSTRUCTIONS ===
Write EXACTLY 4 sections. Use the pre-filled blocks above verbatim where indicated.
TONE: Firm, authoritative, legally assertive. No "we request" or "we hope".
Use: "You are hereby called upon...", "Failing which, we shall be constrained to..."

A. MSME Eligibility Check
Write this section using the pre-computed eligibility text above, verbatim.

B. Formal Demand Notice
Start with the notice header below VERBATIM, then write 5 assertive paragraphs:

{notice_header}
Para 1: State that {req.business_name} supplied goods/services to {req.buyer_name} as per Invoice {req.invoice_number} dated {req.invoice_date} for {principal_label}. Payment was due on {req.due_date}.
Para 2: Despite {overdue_str} having elapsed and despite repeated reminders, {req.buyer_name} has willfully withheld payment without lawful cause. This constitutes a violation of Section 15 of the MSMED Act, 2006.
Para 3: Include the full interest calculation table above verbatim. State that interest continues to accrue daily.
Para 4: "You are hereby called upon to pay {total_label} to {req.business_name} within 15 days of receipt of this notice. Failing which, {req.business_name} shall be constrained to initiate proceedings before the MSME Facilitation Council under Section 18 of the MSMED Act, 2006, and pursue all other remedies available under law, the costs whereof shall be borne by you."
Para 5: "This notice is being issued via registered post/speed post and shall be treated as final intimation before legal action. All rights of {req.business_name} are expressly reserved."

Close with:
Yours faithfully,

{req.business_name}
Udyam No: {udyam}
Date: {today}

C. Next Steps
Use the pre-written next steps below verbatim:
{next_steps}

D. Evidence Checklist
Use the pre-written checklist below verbatim:
{evidence_checklist}"""

    response = await call_ai(
        [{"role": "user", "content": prompt}],
        system, task="legal_document", language=req.language
    )
    log_analytics("msme_notice_generated", {"payment_id": req.payment_id})
    return {"content": response, "service": "msme_notice"}

# ─── PREMIUM: LEGAL REPLY ─────────────────────────────────────────────────────
@app.post("/api/legal-reply")
async def legal_reply(req: LegalReplyRequest):
    if not check_payment(req.payment_id, "legal_reply", getattr(req, "mobile", None)):
        raise HTTPException(402, "Payment required or not verified")

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
        "REQUIRED OUTPUT FORMAT — complete, ready-to-send letter, zero square brackets:\n"
        f"Date: {today}\n\n"
        "To,\n"
        "[From the notice above, extract and write the actual sender name, designation, firm/bank name — no brackets]\n\n"
        "Subject: [Write a specific subject line referencing the notice date and subject — no brackets]\n\n"
        "Dear Sir/Madam,\n\n"
        "[Write the complete reply: (1) Acknowledge the notice with its exact date and subject. (2) State your position clearly using the KEY FACTS provided. (3) Assert applicable legal rights with specific law sections. (4) Make a firm demand or request. Each paragraph complete and substantive — no placeholders.]\n\n"
        f"Yours sincerely,\n{req.user_name}\n_______________\n(Signature)\n{today}"
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
    prompt = f"""Generate a complete, ready-to-file formal complaint. Write every section fully — no square brackets, no instructions to fill in later.

DATA — USE EXACTLY:
Complainant: {req.user_name}
Issue: {req.issue_type}
Date of Incident: {req.date}
Location: {req.location}
Opposite Party: {req.opponent_name or "Not specified"}
Facts: {req.issue_description}
Today: {today}
Authority: {issue_info['authority']}, {req.location}
Laws: {issue_info['sections']}

WRITE THE COMPLETE COMPLAINT DOCUMENT:

{req.user_name}
{req.location}
{today}

A. To:
{issue_info['authority']}, {req.location}

B. Subject:
Write one complete, specific subject line using actual issue type and opponent name

C. Facts:
Write numbered paragraphs using ONLY the facts given. Each sentence complete and specific.

D. Legal Grounds:
Write out the applicable sections by name. For consumer complaints ALWAYS cite: Section 2(7) Consumer Protection Act 2019 (definition of consumer), Section 2(47) Consumer Protection Act 2019 (deficiency of service), Section 35 Consumer Protection Act 2019 (right to file complaint). For others use only: {issue_info['sections']}

E. Jurisdiction:
Write one complete sentence explaining why this forum has jurisdiction — use actual location and issue type

F. Relief Sought:
Write numbered specific reliefs using firm language: "direct", "order", "award compensation"

G. Verification:
I, {req.user_name}, do hereby verify and declare that the contents of this complaint are true and correct to the best of my knowledge and belief. Nothing material has been concealed.
Place: {req.location}
Date: {today}
Signature: _______________
{req.user_name}

H. List of Annexures:
Write numbered annexures based on evidence mentioned in the facts

{issue_info['copy_to']}"""

    response = await call_ai([{"role": "user", "content": prompt}], system, task="legal_document", language=req.language)
    log_analytics("complaint_draft_generated", {"payment_id": req.payment_id})
    return {"content": response, "service": "complaint_draft"}

# ─── PRIORITY ANSWER ──────────────────────────────────────────────────────────
@app.post("/api/priority-answer")
async def priority_answer(req: PriorityAnswerRequest):
    if req.priority_flag:
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

Output format — use PLAIN TEXT headers only, NO emoji:

Situation:
[Clear understanding of the legal situation]

Legal Position:
[Exact Indian laws, sections, acts that apply]

What You Should Do:
1. [First action]
2. [Second action]
3. [Third action]

What You Can Say or Write:
[Exact words, draft text, or template the user can use]

Important Note:
[Any warnings, caveats, or next steps]"""
        task = "high_accuracy"
    else:
        prompt = f"""Answer this legal question concisely: "{req.question}" """
        task = "normal_chat"

    response = await call_ai([{"role": "user", "content": prompt}], system, task=task, language=req.language)
    return {"content": response, "priority": req.priority_flag, "service": "priority_answer"}

# ─── FONT SETUP ──────────────────────────────────────────────────────────────
import os as _os, logging as _logging

_FREESANS_PATH      = '/usr/share/fonts/truetype/freefont/FreeSans.ttf'
_FREESANS_BOLD_PATH = '/usr/share/fonts/truetype/freefont/FreeSansBold.ttf'

def _register_fonts():
    try:
        registered = pdfmetrics.getRegisteredFontNames()
        if 'LD-Regular' not in registered:
            pdfmetrics.registerFont(TTFont('LD-Regular', _FREESANS_PATH))
        if 'LD-Bold' not in registered:
            pdfmetrics.registerFont(TTFont('LD-Bold', _FREESANS_BOLD_PATH))
        _logging.info("PDF fonts registered: FreeSans (supports Devanagari/Hindi)")
        return True
    except Exception as e:
        _logging.error(f"Font registration failed: {e}")
        return False

_FONTS_OK = _register_fonts()
_FN      = 'LD-Regular' if _FONTS_OK else 'Helvetica'
_FN_BOLD = 'LD-Bold'    if _FONTS_OK else 'Helvetica-Bold'

_EMOJI_FULL_PATTERNS = [
    ("🔍 Situation:",        "Situation:"),
    ("🔍Situation:",         "Situation:"),
    ("⚖️ Legal Position:",   "Legal Position:"),
    ("⚖️Legal Position:",    "Legal Position:"),
    ("✅ What You Should Do:","What You Should Do:"),
    ("✅What You Should Do:", "What You Should Do:"),
    ("💬 What You Can Say/Write:","What You Can Say:"),
    ("💬 What You Can Say:",  "What You Can Say:"),
    ("💬What You Can Say:",   "What You Can Say:"),
    ("⚠️ Important Note:",   "Important Note:"),
    ("⚠️Important Note:",    "Important Note:"),
]
_EMOJI_LABELS = {
    "🔍": "", "⚖️": "", "✅": "", "💬": "", "⚠️": "",
    "📝": "", "📦": "", "📣": "", "💡": "", "⭐": "",
    "🎉": "", "🏆": "", "🔥": "", "👑": "", "💀": "",
    "😤": "", "😎": "", "🗿": "", "🤌": "", "🥷": "",
    "🇮🇳": "", "📋": "", "📄": "", "🔒": "", "❓": "",
    "📞": "", "⚡": "", "→": "->", "←": "<-",
}

def _strip_emoji(text: str) -> str:
    import re as _r
    for pattern, replacement in _EMOJI_FULL_PATTERNS:
        text = text.replace(pattern, replacement)
    for emoji, label in _EMOJI_LABELS.items():
        text = text.replace(emoji, label)
    text = _r.sub(r"[𐀀-􏿿]", "", text)
    text = _r.sub(r"[☀-➿]", "", text)
    text = _r.sub(r"[⌀-⏿]", "", text)
    text = _r.sub(r"[︀-﻿]", "", text)
    text = _r.sub(r"  +", " ", text)
    return text.strip()

def _clean(text: str) -> str:
    import re as _r
    text = _strip_emoji(text)
    text = _r.sub(r'\*\*(.+?)\*\*', r'<b>\1</b>', text)
    text = _r.sub(r'\*(.+?)\*',   r'<i>\1</i>', text)
    text = _r.sub(r'^#+\s*', '', text, flags=_r.MULTILINE)
    text = text.replace('`', '')
    text = text.replace('<b>', '\x00B\x00').replace('</b>', '\x00/B\x00')
    text = text.replace('<i>', '\x00I\x00').replace('</i>', '\x00/I\x00')
    text = text.replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')
    text = text.replace('\x00B\x00', '<b>').replace('\x00/B\x00', '</b>')
    text = text.replace('\x00I\x00', '<i>').replace('\x00/I\x00', '</i>')
    return text

def _para(text: str, style) -> Paragraph:
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

        els.append(_para("Bklchai  |  bklchai.com", s_header))
        els.append(_para("Your legal rights, explained.", s_sub))
        els.append(HRFlowable(width="100%", thickness=1.5,
            color=colors.HexColor("#f59e0b"), spaceAfter=10))
        els.append(_para(_strip_emoji(req.title), s_title))
        els.append(HRFlowable(width="100%", thickness=0.5,
            color=colors.HexColor("#dddddd"), spaceAfter=8))
        els.append(Spacer(1, 0.08*inch))

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

        els.append(Spacer(1, 0.25*inch))
        els.append(HRFlowable(width="100%", thickness=0.5,
            color=colors.HexColor("#dddddd"), spaceAfter=6))
        els.append(_para(
            f"Generated: {datetime.utcnow().strftime('%d %B %Y')}  |  "
            "Bklchai is not a legal firm. This document is AI-generated. "
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
        payment_id = f"pay_demo_{generate_token(16)}"
        payments_col.update_one(
            {"order_id": req.razorpay_order_id},
            {"$set": {"status": "verified", "payment_id": payment_id, "verified_at": datetime.utcnow()}}
        )
        log_analytics("payment_verified_demo", {"service": req.service, "order_id": req.razorpay_order_id})
        return {"success": True, "payment_id": payment_id}

    body = f"{req.razorpay_order_id}|{req.razorpay_payment_id}"
    expected = hmac.new(RAZORPAY_SECRET.encode(), body.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, req.razorpay_signature):
        raise HTTPException(400, "Invalid payment signature")

    payments_col.update_one(
        {"order_id": req.razorpay_order_id},
        {"$set": {"status": "verified", "payment_id": req.razorpay_payment_id, "verified_at": datetime.utcnow()}}
    )

    if req.service in ("pro", "unlimited") and req.mobile:
        users_col.update_one({"mobile": req.mobile}, {"$set": {"plan": req.service}})

    log_analytics("payment_verified", {"service": req.service, "payment_id": req.razorpay_payment_id})
    return {"success": True, "payment_id": req.razorpay_payment_id}

@app.get("/api/config")
async def get_config():
    return {
        "razorpay_key_id": RAZORPAY_KEY_ID
    }

import razorpay
import hmac, hashlib
from fastapi import HTTPException
from pydantic import BaseModel

razorpay_client = razorpay.Client(auth=(RAZORPAY_KEY_ID, RAZORPAY_SECRET))

class PaymentVerifyRequest(BaseModel):
    razorpay_order_id: str
    razorpay_payment_id: str
    razorpay_signature: str

@app.post("/api/payment/verify")
async def verify_payment_route(req: PaymentVerifyRequest):
    body = f"{req.razorpay_order_id}|{req.razorpay_payment_id}"
    expected_signature = hmac.new(
        RAZORPAY_SECRET.encode(),
        body.encode(),
        hashlib.sha256
    ).hexdigest()

    if expected_signature != req.razorpay_signature:
        raise HTTPException(status_code=400, detail="Payment verification failed")

    return {"status": "verified", "payment_id": req.razorpay_payment_id}

# ─── ADMIN ────────────────────────────────────────────────────────────────────

def check_admin_key(x_admin_key: Optional[str]):
    if x_admin_key != os.getenv("ADMIN_KEY", "bklchai-admin-2024"):
        raise HTTPException(403, "Forbidden")

def safe_iso(dt):
    return dt.isoformat() if isinstance(dt, datetime) else dt

@app.get("/api/admin/stats")
async def admin_stats(x_admin_key: Optional[str] = Header(None)):
    check_admin_key(x_admin_key)

    total_users = users_col.count_documents({})
    total_chats = chats_col.count_documents({})
    total_payments = payments_col.count_documents({})
    verified_payments = payments_col.count_documents({"status": "verified"})
    total_revenue = sum(p.get("amount", 0) for p in payments_col.find({"status": "verified"}))

    today_start = datetime.utcnow().replace(hour=0, minute=0, second=0, microsecond=0)
    week_start = datetime.utcnow() - timedelta(days=7)

    today_users = users_col.count_documents({"created_at": {"$gte": today_start}})
    today_chats = chats_col.count_documents({"timestamp": {"$gte": today_start}})
    today_verified_payments = payments_col.count_documents({
        "status": "verified",
        "verified_at": {"$gte": today_start}
    })

    users_last_7_days = users_col.count_documents({"created_at": {"$gte": week_start}})
    chats_last_7_days = chats_col.count_documents({"timestamp": {"$gte": week_start}})
    revenue_last_7_days = sum(
        p.get("amount", 0)
        for p in payments_col.find({
            "status": "verified",
            "verified_at": {"$gte": week_start}
        })
    )

    users_who_paid = payments_col.distinct("mobile", {"status": "verified", "mobile": {"$ne": None}})
    conversion_rate = round((len(users_who_paid) / max(total_users, 1)) * 100, 1)

    plan_breakdown = [
        {"_id": doc["_id"], "count": doc["count"]}
        for doc in users_col.aggregate([
            {"$group": {"_id": {"$ifNull": ["$plan", "free"]}, "count": {"$sum": 1}}},
            {"$sort": {"count": -1}}
        ])
    ]

    language_breakdown = [
        {"_id": doc["_id"] or "unknown", "count": doc["count"]}
        for doc in chats_col.aggregate([
            {"$group": {"_id": "$language", "count": {"$sum": 1}}},
            {"$sort": {"count": -1}}
        ])
    ]

    intent_breakdown = [
        {"_id": doc["_id"] or "unknown", "count": doc["count"]}
        for doc in chats_col.aggregate([
            {"$match": {"intent": {"$ne": None}}},
            {"$group": {"_id": "$intent", "count": {"$sum": 1}}},
            {"$sort": {"count": -1}}
        ])
    ]

    return {
        "overview": {
            "total_users": total_users,
            "today_users": today_users,
            "total_chats": total_chats,
            "today_chats": today_chats,
            "total_payments": total_payments,
            "verified_payments": verified_payments,
            "today_verified_payments": today_verified_payments,
            "total_revenue": total_revenue,
            "revenue_last_7_days": revenue_last_7_days,
            "users_last_7_days": users_last_7_days,
            "chats_last_7_days": chats_last_7_days,
            "conversion_rate": conversion_rate
        },
        "plan_breakdown": plan_breakdown,
        "language_breakdown": language_breakdown,
        "intent_breakdown": intent_breakdown
    }

@app.get("/api/admin/users")
async def admin_users(
    x_admin_key: Optional[str] = Header(None),
    limit: int = 50
):
    check_admin_key(x_admin_key)

    docs = list(
        users_col.find(
            {},
            {
                "_id": 0,
                "mobile": 1,
                "plan": 1,
                "created_at": 1,
                "last_login_at": 1,
                "login_count": 1,
                "daily_chats": 1,
                "last_chat_date": 1
            }
        ).sort("created_at", -1).limit(min(limit, 200))
    )

    for d in docs:
        d["created_at"] = safe_iso(d.get("created_at"))
        d["last_login_at"] = safe_iso(d.get("last_login_at"))

    return {"items": docs}

@app.get("/api/admin/mobiles")
async def admin_mobiles(
    x_admin_key: Optional[str] = Header(None),
    limit: int = 100
):
    check_admin_key(x_admin_key)

    docs = list(
        users_col.find(
            {"mobile": {"$exists": True, "$ne": None}},
            {"_id": 0, "mobile": 1, "plan": 1, "created_at": 1}
        ).sort("created_at", -1).limit(min(limit, 300))
    )

    seen = set()
    cleaned = []
    for d in docs:
        mobile = str(d.get("mobile", "")).strip()
        if not mobile or mobile in seen:
            continue
        seen.add(mobile)
        cleaned.append({
            "mobile": mobile,
            "plan": d.get("plan", "free"),
            "created_at": safe_iso(d.get("created_at"))
        })

    return {"items": cleaned}

@app.get("/api/admin/payments")
async def admin_payments(
    x_admin_key: Optional[str] = Header(None),
    limit: int = 50
):
    check_admin_key(x_admin_key)

    docs = list(
        payments_col.find(
            {},
            {
                "_id": 0,
                "order_id": 1,
                "payment_id": 1,
                "mobile": 1,
                "service": 1,
                "amount": 1,
                "status": 1,
                "created_at": 1,
                "verified_at": 1
            }
        ).sort("created_at", -1).limit(min(limit, 200))
    )

    for d in docs:
        d["created_at"] = safe_iso(d.get("created_at"))
        d["verified_at"] = safe_iso(d.get("verified_at"))

    return {"items": docs}

@app.get("/api/admin/recent-chats")
async def admin_recent_chats(
    x_admin_key: Optional[str] = Header(None),
    limit: int = 20
):
    check_admin_key(x_admin_key)

    docs = list(
        chats_col.find(
            {},
            {
                "_id": 0,
                "mobile": 1,
                "message": 1,
                "response": 1,
                "language": 1,
                "intent": 1,
                "timestamp": 1
            }
        ).sort("timestamp", -1).limit(min(limit, 100))
    )

    for d in docs:
        d["timestamp"] = safe_iso(d.get("timestamp"))
        if d.get("response"):
            d["response"] = str(d["response"])[:180]

    return {"items": docs}

@app.get("/api/admin/health")
async def admin_health(x_admin_key: Optional[str] = Header(None)):
    check_admin_key(x_admin_key)
    return {"ok": True, "service": "bklchai-admin"}

# ═══════════════════════════════════════════════════════════════════════════════
# PARTNER PROGRAM ROUTES
# ═══════════════════════════════════════════════════════════════════════════════

COMMISSION_RATE = 0.20
EXCLUDED_SERVICES = {"expert_answer", "plan_pro", "plan_unlimited"}

def generate_ref_code(name: str, mobile: str) -> str:
    base = re.sub(r'[^a-z]', '', name.lower())[:4]
    suffix = mobile[-4:]
    code = base + suffix
    while partners_col.find_one({"ref_code": code}):
        code = base + str(random.randint(1000, 9999))
    return code

@app.post("/api/partner/register")
async def register_partner(req: PartnerRegisterRequest):
    mobile = req.mobile.strip()
    if not re.match(r"^\d{10}$", mobile):
        raise HTTPException(400, "Invalid mobile number")

    existing = partners_col.find_one({"mobile": mobile})
    if existing:
        return {
            "ref_code": existing["ref_code"],
            "message": "Already registered",
            "partner_name": existing["name"]
        }

    ref_code = generate_ref_code(req.name, mobile)

    partners_col.insert_one({
        "name": req.name.strip(),
        "mobile": mobile,
        "email": req.email.strip().lower(),
        "upi_id": req.upi_id.strip(),
        "address": req.address.strip(),
        "ref_code": ref_code,
        "created_at": datetime.utcnow(),
        "total_earned": 0.0,
        "total_paid_out": 0.0,
        "status": "active"
    })

    referrals_col.insert_one({
        "partner_mobile": mobile,
        "partner_ref_code": ref_code,
        "referred_name": req.referral_name.strip(),
        "referred_mobile": req.referral_mobile.strip(),
        "status": "pending",
        "created_at": datetime.utcnow(),
        "paid": False,
        "commission": 0.0
    })

    log_analytics("partner_registered", {"mobile": mobile, "ref_code": ref_code})
    return {
        "ref_code": ref_code,
        "message": "Partner registered successfully",
        "partner_name": req.name
    }

@app.get("/api/partner/earnings/{ref_code}")
async def get_partner_earnings(ref_code: str):
    partner = partners_col.find_one({"ref_code": ref_code}, {"_id": 0})
    if not partner:
        raise HTTPException(404, "Partner not found. Please register first.")

    refs = list(referrals_col.find(
        {"partner_ref_code": ref_code},
        {"_id": 0, "referred_name": 1, "referred_mobile": 1, "paid": 1,
         "commission": 1, "created_at": 1, "paid_at": 1}
    ).sort("created_at", -1).limit(50))

    paid_refs = [r for r in refs if r.get("paid")]
    total_earned = sum(r.get("commission", 0) for r in paid_refs)

    paid_out = sum(
        p.get("amount", 0)
        for p in payouts_col.find({"partner_ref_code": ref_code, "status": "completed"})
    )
    pending_payout = max(0, total_earned - paid_out)

    referrals_out = []
    for r in refs:
        referrals_out.append({
            "name": r.get("referred_name", "User"),
            "mobile": r.get("referred_mobile", ""),
            "paid": r.get("paid", False),
            "commission": round(r.get("commission", 0), 2),
            "date": r["created_at"].strftime("%d %b %Y") if isinstance(r.get("created_at"), datetime) else ""
        })

    return {
        "partner_name": partner["name"],
        "upi_id": partner["upi_id"],
        "ref_code": ref_code,
        "paid_referrals": len(paid_refs),
        "total_referrals": len(refs),
        "total_earned": round(total_earned, 2),
        "pending_payout": round(pending_payout, 2),
        "total_paid_out": round(paid_out, 2),
        "referrals": referrals_out
    }

async def credit_partner_commission(buyer_mobile: str, service: str, amount: float):
    if service in EXCLUDED_SERVICES:
        return

    referral = referrals_col.find_one({
        "referred_mobile": buyer_mobile,
        "paid": False
    })
    if not referral:
        return

    commission = round(amount * COMMISSION_RATE, 2)
    ref_code = referral["partner_ref_code"]

    referrals_col.update_one(
        {"_id": referral["_id"]},
        {"$set": {"paid": True, "commission": commission, "paid_at": datetime.utcnow(),
                  "service_paid": service, "payment_amount": amount}}
    )

    partners_col.update_one(
        {"ref_code": ref_code},
        {"$inc": {"total_earned": commission}}
    )

    log_analytics("partner_commission_credited", {
        "ref_code": ref_code,
        "buyer_mobile": buyer_mobile,
        "service": service,
        "amount": amount,
        "commission": commission
    })

@app.get("/api/admin/partners")
async def admin_partners(x_admin_key: Optional[str] = Header(None)):
    check_admin_key(x_admin_key)
    docs = list(partners_col.find({}, {"_id": 0}).sort("created_at", -1).limit(200))
    for d in docs:
        d["created_at"] = safe_iso(d.get("created_at"))
    return {"items": docs}

@app.get("/api/admin/partner-payouts")
async def admin_partner_payouts(x_admin_key: Optional[str] = Header(None)):
    check_admin_key(x_admin_key)
    partners_list = list(partners_col.find({"status": "active"}, {"_id": 0}))
    payouts_due = []
    for p in partners_list:
        paid_out = sum(
            py.get("amount", 0)
            for py in payouts_col.find({"partner_ref_code": p["ref_code"], "status": "completed"})
        )
        pending = round(p.get("total_earned", 0) - paid_out, 2)
        if pending > 0:
            payouts_due.append({
                "name": p["name"],
                "mobile": p["mobile"],
                "upi_id": p["upi_id"],
                "ref_code": p["ref_code"],
                "pending_payout": pending
            })
    payouts_due.sort(key=lambda x: x["pending_payout"], reverse=True)
    return {"payouts_due": payouts_due, "total": sum(p["pending_payout"] for p in payouts_due)}

@app.post("/api/admin/mark-payout")
async def mark_payout(req: MarkPayoutRequest, x_admin_key: Optional[str] = Header(None)):
    check_admin_key(x_admin_key)
    payouts_col.insert_one({
        "partner_ref_code": req.ref_code,
        "amount": req.amount,
        "utr": req.utr,
        "status": "completed",
        "paid_at": datetime.utcnow()
    })
    return {"success": True, "message": f"Payout of ₹{req.amount} marked for {req.ref_code}"}

@app.get("/health")
async def health():
    return {"status": "ok", "version": "2.0.0", "service": "bklchai"}

# ─── STATIC FILES (serve HTML) ───────────────────────────────────────────────
# This MUST be the last route so it doesn't override API endpoints
app.mount("/", StaticFiles(directory=".", html=True), name="static")