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
 
app = FastAPI(title="LegalDost API", version="2.0.0")
 
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
    """Smart model routing: Groq for free, GPT for premium/Telugu/legal docs"""
    if task in ("legal_document", "high_accuracy", "premium"):
        return "openai"
    if language == "te" and task == "critical":
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
        return """You are LegalDost, a professional legal document drafting assistant for Indian law.
 
CRITICAL RULE: Write ALL legal documents in formal English only.
Do NOT use Hindi, Telugu, or Hinglish in the actual legal notice/document body.
The document will be sent to courts, lawyers, and opposing parties — it must be in formal English.
 
Rules:
- Use formal legal English throughout the document
- Cite exact Indian laws, sections, and acts (e.g., Section 138 NI Act, MSMED Act 2006)
- Do NOT assume or invent facts not provided
- Do NOT use markdown asterisks (**) — write in plain text with clear section headers
- Use A. B. C. D. for section labels
- Use numbered lists 1. 2. 3. for steps
- The ₹ symbol is fine to use for amounts
- Be precise, professional, and legally accurate
- End with: "Note: This document is AI-generated. Consult a qualified lawyer before sending."
"""
 
    lang_instruction = {
        "hi": "Always respond in Hindi (Devanagari script). Be helpful and simple.",
        "te": "Always respond in Telugu script. Be clear and legally accurate.",
        "hinglish": "Respond in Hinglish (Hindi written in English script). Keep it casual yet accurate.",
    }.get(language, "Respond in Hindi.")
 
    return f"""You are LegalDost, an AI legal assistant for Indian citizens — especially MSMEs, workers, and common people.
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
 
    system = get_system_prompt(req.language, "legal")
    prompt = f"""Generate a professional cheque bounce legal notice under Section 138 of the Negotiable Instruments Act.
 
Details:
- Complainant: {req.client_name}, {req.client_address}
- Cheque Drawer: {req.drawer_name}, {req.drawer_address}
- Cheque No: {req.cheque_number}, Date: {req.cheque_date}
- Amount: ₹{req.cheque_amount}
- Bank: {req.bank_name}
- Dishonour Reason: {req.dishonour_reason}
 
Output must include:
A. Section 138 Suitability Check
B. Formal Legal Notice (ready to send)
C. Document Checklist
D. Next Steps
 
Use formal legal language. Add date fields where needed."""
 
    response = await call_ai([{"role": "user", "content": prompt}], system, task="legal_document", language=req.language)
    log_analytics("cheque_notice_generated", {"payment_id": req.payment_id})
    return {"content": response, "service": "cheque_notice"}
 
# ─── PREMIUM: MSME NOTICE ─────────────────────────────────────────────────────
@app.post("/api/msme-notice")
async def msme_notice(req: MSMENoticeRequest):
    mobile = getattr(req, "mobile", None)
    if not check_payment(req.payment_id, "msme_notice", mobile):
        raise HTTPException(402, "Payment required or not verified")
 
    system = get_system_prompt(req.language, "legal")
    prompt = f"""Generate an MSME Payment Recovery Notice under MSMED Act 2006.
 
Details:
- Supplier: {req.business_name}
- Buyer: {req.buyer_name}, {req.buyer_address}
- Invoice: {req.invoice_number} dated {req.invoice_date}
- Outstanding: ₹{req.outstanding_amount}
- Due Date: {req.due_date}
- Udyam No: {req.udyam_number or 'Not provided'}
 
Output:
A. MSME Eligibility Check
B. Formal Demand Notice
C. Next Steps (MSME Samadhaan / Court)
D. Evidence Checklist"""
 
    response = await call_ai([{"role": "user", "content": prompt}], system, task="legal_document", language=req.language)
    log_analytics("msme_notice_generated", {"payment_id": req.payment_id})
    return {"content": response, "service": "msme_notice"}
 
# ─── PREMIUM: LEGAL REPLY ─────────────────────────────────────────────────────
@app.post("/api/legal-reply")
async def legal_reply(req: LegalReplyRequest):
    if not check_payment(req.payment_id, "legal_reply", getattr(req, "mobile", None)):
        raise HTTPException(402, "Payment required or not verified")
 
    system = get_system_prompt(req.language, "legal")
    prompt = f"""Generate a professional legal reply in Indian context.
 
User received this message:
\"\"\"{req.received_message}\"\"\"
 
Context: {req.context}
Tone: {req.tone}
Sender name: {req.user_name}
 
Rules:
- Do NOT assume facts not stated
- Use clear, professional tone
- Keep it legally safe
- Cite relevant Indian law if applicable
 
Structure:
1. Acknowledge message
2. Clarify user's position  
3. Assert rights (if applicable)
4. Polite but firm close
 
Output format:
Subject: Response regarding your message
 
Dear Sir/Madam,
 
[Generated reply body]
 
Yours sincerely,
{req.user_name}"""
 
    response = await call_ai([{"role": "user", "content": prompt}], system, task="legal_document", language=req.language)
    log_analytics("legal_reply_generated", {"payment_id": req.payment_id})
    return {"content": response, "service": "legal_reply"}
 
# ─── PREMIUM: COMPLAINT DRAFT ─────────────────────────────────────────────────
@app.post("/api/complaint-draft")
async def complaint_draft(req: ComplaintDraftRequest):
    if not check_payment(req.payment_id, "complaint_draft", getattr(req, "mobile", None)):
        raise HTTPException(402, "Payment required or not verified")
 
    system = get_system_prompt(req.language, "legal")
    prompt = f"""Generate a formal complaint in Indian legal format.
 
Issue Type: {req.issue_type}
Description: {req.issue_description}
Location: {req.location}
Opponent: {req.opponent_name or 'Not specified'}
Date of Incident: {req.date}
Complainant: {req.user_name}
 
Rules:
- Do NOT assume missing facts
- Keep formal and precise
- No fake legal references
- Use proper complaint structure
 
Structure:
1. To: [Appropriate authority based on issue type]
2. Subject line
3. Factual description
4. Specific request for action
5. Formal close with name"""
 
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
 
# ─── FONT REGISTRATION (fixes ₹ symbol + Devanagari rendering) ──────────────
DEJAVU_REGULAR = "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"
DEJAVU_BOLD    = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
 
def register_fonts():
    try:
        registered = pdfmetrics.getRegisteredFontNames()
        if "DejaVu" not in registered:
            pdfmetrics.registerFont(TTFont("DejaVu", DEJAVU_REGULAR))
        if "DejaVu-Bold" not in registered:
            pdfmetrics.registerFont(TTFont("DejaVu-Bold", DEJAVU_BOLD))
    except Exception:
        pass
 
register_fonts()
 
def clean_for_pdf(text: str) -> str:
    """Convert markdown to ReportLab XML tags, fix special chars."""
    import re as _re
    # Convert **bold** to <b>bold</b>
    text = _re.sub(r'\*\*(.+?)\*\*', r'<b></b>', text)
    # Convert *italic* to <i>italic</i>
    text = _re.sub(r'\*(.+?)\*', r'<i></i>', text)
    # Strip # headers marker (keep text)
    text = _re.sub(r'^#+\s*', '', text, flags=_re.MULTILINE)
    # Remove backticks
    text = text.replace('`', '')
    # Store our <b> <i> tags temporarily
    text = text.replace('<b>', 'B').replace('</b>', '/B')
    text = text.replace('<i>', 'I').replace('</i>', '/I')
    # Escape XML special chars
    text = text.replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')
    # Restore our tags
    text = text.replace('B', '<b>').replace('/B', '</b>')
    text = text.replace('I', '<i>').replace('/I', '</i>')
    return text
 
def safe_para(text: str, style) -> Paragraph:
    """Safe paragraph creation with fallback."""
    try:
        return Paragraph(clean_for_pdf(text), style)
    except Exception:
        plain = re.sub(r'<[^>]+>', '', text)
        plain = plain.replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')
        return Paragraph(plain, style)
 
# ─── PDF GENERATION ───────────────────────────────────────────────────────────
@app.post("/api/generate-pdf")
async def generate_pdf(req: GeneratePDFRequest):
    buffer = io.BytesIO()
    doc = SimpleDocTemplate(
        buffer, pagesize=A4,
        rightMargin=0.85 * inch, leftMargin=0.85 * inch,
        topMargin=0.9 * inch, bottomMargin=0.9 * inch
    )
 
    # Use DejaVu for full Unicode: ₹ symbol, Hindi, Telugu all render correctly
    fn      = "DejaVu"      if "DejaVu"      in pdfmetrics.getRegisteredFontNames() else "Helvetica"
    fn_bold = "DejaVu-Bold" if "DejaVu-Bold" in pdfmetrics.getRegisteredFontNames() else "Helvetica-Bold"
 
    header_style = ParagraphStyle("LDH", fontName=fn_bold, fontSize=10,
        textColor=colors.HexColor("#f59e0b"), spaceAfter=1)
    sub_style = ParagraphStyle("LDS", fontName=fn, fontSize=8,
        textColor=colors.HexColor("#999999"), spaceAfter=12)
    title_style = ParagraphStyle("LDT", fontName=fn_bold, fontSize=15,
        textColor=colors.HexColor("#1a1a2e"), spaceAfter=6)
    section_style = ParagraphStyle("LDSC", fontName=fn_bold, fontSize=11,
        textColor=colors.HexColor("#1a1a2e"), spaceAfter=4, spaceBefore=12)
    body_style = ParagraphStyle("LDB", fontName=fn, fontSize=10.5,
        leading=17, spaceAfter=5, textColor=colors.HexColor("#222222"))
    bullet_style = ParagraphStyle("LDBUL", fontName=fn, fontSize=10.5,
        leading=16, spaceAfter=4, leftIndent=12, textColor=colors.HexColor("#333333"))
    footer_style = ParagraphStyle("LDF", fontName=fn, fontSize=7.5,
        textColor=colors.HexColor("#999999"), spaceBefore=8, alignment=1)
 
    elements = []
 
    # Letterhead
    elements.append(safe_para("LegalDost — lawyaari.com", header_style))
    elements.append(safe_para("Your legal rights, explained.", sub_style))
    elements.append(HRFlowable(width="100%", thickness=1.5,
        color=colors.HexColor("#f59e0b"), spaceAfter=10))
    elements.append(safe_para(req.title, title_style))
    elements.append(HRFlowable(width="100%", thickness=0.5,
        color=colors.HexColor("#dddddd"), spaceAfter=8))
    elements.append(Spacer(1, 0.08 * inch))
 
    # Content: smart line parsing
    for line in req.content.split:
        stripped = line.strip()
        if not stripped:
            elements.append(Spacer(1, 0.05 * inch))
            continue
        # Section headers: A. B. C. D. or short lines ending with colon
        is_section = (
            (len(stripped) < 90 and len(stripped) > 1 and
             stripped[0].upper() in "ABCDEFGH" and stripped[1] == ".") or
            (stripped.endswith(":") and len(stripped) < 80) or
            stripped.startswith("#")
        )
        if is_section:
            elements.append(safe_para(stripped, section_style))
        elif stripped.startswith(("-", "•")) or (stripped.startswith("*") and not stripped.startswith("**")):
            clean = stripped.lstrip("-•* ")
            elements.append(safe_para(f"• {clean}", bullet_style))
        elif len(stripped) > 2 and stripped[0].isdigit() and stripped[1] in ".)":
            elements.append(safe_para(stripped, bullet_style))
        else:
            elements.append(safe_para(stripped, body_style))
 
    # Footer
    elements.append(Spacer(1, 0.25 * inch))
    elements.append(HRFlowable(width="100%", thickness=0.5,
        color=colors.HexColor("#dddddd"), spaceAfter=6))
    footer_text = (
        f"Generated on: {datetime.utcnow().strftime('%d %B %Y')}  |  "
        "LegalDost is not a legal firm. This document is AI-generated. "
        "Consult a qualified lawyer before taking legal action."
    )
    elements.append(safe_para(footer_text, footer_style))
 
    doc.build(elements)
    buffer.seek(0)
 
    filename = f"legaldost_{req.service_type}_{datetime.utcnow().strftime('%Y%m%d%H%M%S')}.pdf"
    return StreamingResponse(
        buffer, media_type="application/pdf",
        headers={"Content-Disposition": f"attachment; filename={filename}"}
    )
 
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