// ═══════════════════════════════════════════════════════════════
//  Lawyaari — Audio Voice Manager
//  File: audio.js  →  place in /frontend/ folder alongside index.html
//
//  HOW TO USE:
//  1. Record your voice clips on your phone
//  2. Rename them exactly as listed below
//  3. Put them in /frontend/audio/ folder
//  4. This file is already linked in index.html via <script src="audio.js">
//     (add that one line to index.html <head> section)
//  5. Deploy to Netlify — done.
//
//  RECORDING CHECKLIST (6 clips, ~5 sec each, save as .mp3):
//
//  welcome_sir.mp3
//    Say: "Aapka swagath hai sir. Lawyaari mein aapka swagat hai."
//
//  welcome_maam.mp3
//    Say: "Aapka swagath hai ma'am. Lawyaari mein aapka swagat hai."
//
//  last_chat_sir.mp3
//    Say: "Sir, aaj sirf ek hi sawaal bacha hai."
//
//  last_chat_maam.mp3
//    Say: "Ma'am, aaj sirf ek hi sawaal bacha hai."
//
//  quota_over_sir.mp3
//    Say: "Sir, aaj ki muft seema khatam ho gayi hai."
//
//  quota_over_maam.mp3
//    Say: "Ma'am, aaj ki muft seema khatam ho gayi hai."
//
//  pdf_generating.mp3
//    Say: "Aapka dastaavez ban raha hai, kripya prateeksha karein."
//    (one clip covers both sir/maam — gender neutral)
//
//  OPTIONAL Telugu clips (if you speak Telugu):
//  welcome_te_sir.mp3    → "Sir, mee swaagatham. Lawyaari lo mee swaagatham."
//  welcome_te_maam.mp3   → "Ma'am, mee swaagatham. Lawyaari lo mee swaagatham."
//  quota_over_te_sir.mp3 → "Sir, nedu mee uchita kota ayipoyindi."
//  quota_over_te_maam.mp3→ "Ma'am, nedu mee uchita kota ayipoyindi."
//  last_chat_te.mp3      → "Meeru inkaa okkatee free question adagavachu."
// ═══════════════════════════════════════════════════════════════

(function() {

  // ── 1. Audio file map ───────────────────────────────────────────
  // Keys match the event names called throughout the app.
  // Values are paths relative to this file (inside /frontend/).
  const AUDIO_MAP = {
    // Hindi / Hinglish
    welcome_hi_sir:        'audio/welcome_sir.mp3',
    welcome_hi_maam:       'audio/welcome_maam.mp3',
    welcome_hinglish_sir:  'audio/welcome_sir.mp3',
    welcome_hinglish_maam: 'audio/welcome_maam.mp3',

    last_chat_hi_sir:        'audio/last_chat_sir.mp3',
    last_chat_hi_maam:       'audio/last_chat_maam.mp3',
    last_chat_hinglish_sir:  'audio/last_chat_sir.mp3',
    last_chat_hinglish_maam: 'audio/last_chat_maam.mp3',

    quota_over_hi_sir:        'audio/quota_over_sir.mp3',
    quota_over_hi_maam:       'audio/quota_over_maam.mp3',
    quota_over_hinglish_sir:  'audio/quota_over_sir.mp3',
    quota_over_hinglish_maam: 'audio/quota_over_maam.mp3',

    pdf_generating: 'audio/pdf_generating.mp3',

    // Telugu (optional — falls back to Hindi if file missing)
    welcome_te_sir:        'audio/welcome_te_sir.mp3',
    welcome_te_maam:       'audio/welcome_te_maam.mp3',
    last_chat_te_sir:      'audio/last_chat_te.mp3',
    last_chat_te_maam:     'audio/last_chat_te.mp3',
    quota_over_te_sir:     'audio/quota_over_te_sir.mp3',
    quota_over_te_maam:    'audio/quota_over_te_maam.mp3',
  };

  // ── 2. Pre-load all audio objects ──────────────────────────────
  const _cache = {};
  let _ready = false;

  function _preload() {
    Object.entries(AUDIO_MAP).forEach(([key, src]) => {
      const a = new Audio(src);
      a.preload = 'auto';
      _cache[key] = a;
    });
    _ready = true;
  }

  // Pre-load on first user interaction (browsers require this)
  function _initOnInteraction() {
    if (_ready) return;
    _preload();
    document.removeEventListener('click', _initOnInteraction);
    document.removeEventListener('touchstart', _initOnInteraction);
  }
  document.addEventListener('click', _initOnInteraction, { once: true });
  document.addEventListener('touchstart', _initOnInteraction, { once: true });

  // ── 3. Core play function ───────────────────────────────────────
  function playAudio(key) {
    const audio = _cache[key];
    if (!audio) return;   // file not recorded yet — silent fail
    // Stop any currently playing clip
    Object.values(_cache).forEach(a => { try { a.pause(); a.currentTime = 0; } catch(e){} });
    audio.play().catch(() => {});  // silent fail if browser blocks
  }

  // ── 4. Helper to build key from lang + gender ───────────────────
  function _key(event, lang, gender) {
    const sal = (gender === 'female') ? 'maam' : 'sir';
    // Telugu with fallback to Hindi if Telugu clip missing
    if (lang === 'te') {
      const teKey = `${event}_te_${sal}`;
      if (_cache[teKey]) return teKey;
      // fallback to Hindi
      return `${event}_hi_${sal}`;
    }
    const l = (lang === 'hinglish') ? 'hinglish' : 'hi';
    return `${event}_${l}_${sal}`;
  }

  // ── 5. Public API — these replace the old speak() calls ─────────

  // Called after successful login
  window.welcomeVoice = function(name, gender) {
    const lang = (window.state && window.state.lang) || 'hi';
    const key  = _key('welcome', lang, gender);
    setTimeout(() => playAudio(key), 700);
  };

  // Called when 1 free chat remains
  window.notifyLastChat = function(gender) {
    const lang = (window.state && window.state.lang) || 'hi';
    const key  = _key('last_chat', lang, gender);
    playAudio(key);
  };

  // Called when daily quota is fully used up
  window.notifyQuotaOver = function(gender) {
    const lang = (window.state && window.state.lang) || 'hi';
    const key  = _key('quota_over', lang, gender);
    playAudio(key);
  };

  // Called when PDF generation starts
  window.notifyPdfGenerating = function(gender) {
    playAudio('pdf_generating');
  };

  // ── 6. Volume control (optional — call from console or add UI) ──
  window.setLawyaariVolume = function(vol) {  // 0.0 to 1.0
    Object.values(_cache).forEach(a => { a.volume = Math.max(0, Math.min(1, vol)); });
  };

  console.log('[Lawyaari Audio] Loaded. Drop .mp3 files in /frontend/audio/ to activate.');

})();