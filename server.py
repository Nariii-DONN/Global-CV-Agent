import os, re, json, sqlite3, hashlib, hmac, secrets, time, threading, mimetypes, tempfile, csv, io, logging
from pathlib import Path
from urllib.parse import urlparse, urljoin, urlunparse, parse_qsl, urlencode
from urllib import robotparser
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from email.parser import BytesParser
from email.policy import default
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests
from bs4 import BeautifulSoup
from pypdf import PdfReader
try:
    from docx import Document
except Exception:
    Document = None
try:
    import fitz
except Exception:
    fitz = None
from dotenv import load_dotenv

load_dotenv()
logging.basicConfig(level=os.getenv('LOG_LEVEL', 'INFO'), format='%(asctime)s %(levelname)s %(message)s')
log = logging.getLogger('cvagent')
BASE = Path(__file__).resolve().parent
STATIC_DIR = (BASE / 'app' / 'static').resolve()
DATA = Path(os.getenv('DATABASE_PATH', './data/cv_agent.db'))
if not DATA.is_absolute(): DATA = BASE / DATA
if not os.getenv('DATABASE_PATH'):
    _legacy = BASE / './data/cv_finder.db'
    if not DATA.exists() and _legacy.exists(): DATA = _legacy
UPLOAD_DIR = Path(os.getenv('UPLOAD_DIR', './data/uploads'))
if not UPLOAD_DIR.is_absolute(): UPLOAD_DIR = BASE / UPLOAD_DIR
UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
DATA.parent.mkdir(parents=True, exist_ok=True)
MAX_MB = float(os.getenv('MAX_DOWNLOAD_MB', '8'))
MAX_HTML_MB = float(os.getenv('MAX_HTML_MB', '3'))
MIN_SCORE = float(os.getenv('MIN_MATCH_SCORE', '0'))
MAX_QUERIES = int(os.getenv('MAX_QUERIES_PER_JD', '25'))
RESULTS_PER_QUERY = int(os.getenv('RESULTS_PER_QUERY', '10'))
FREE_SEARCH_ENABLED = os.getenv('FREE_SEARCH_ENABLED', '1') == '1'
FREE_SEARCH_DELAY = float(os.getenv('FREE_SEARCH_DELAY', '2.0'))
OLLAMA_BASE_URL = os.getenv('OLLAMA_BASE_URL', 'http://127.0.0.1:11434').rstrip('/')
OLLAMA_MODEL = os.getenv('OLLAMA_MODEL', '')
MAX_CANDIDATES = int(os.getenv('MAX_CANDIDATES_PER_RUN', '60'))
MAX_AI_RERANK = int(os.getenv('MAX_AI_RERANK', '30'))
FETCH_WORKERS = int(os.getenv('FETCH_WORKERS', '6'))
SEARCH_COUNTRY = os.getenv('SEARCH_COUNTRY', 'IN')
SEARCH_LANGUAGE = os.getenv('SEARCH_LANGUAGE', 'en')

# Domains that waste free-mode time (login walls / anti-bot). Still searchable via snippet, skipped for fetch.
SKIP_FETCH_DOMAINS = set((os.getenv('SKIP_FETCH_DOMAINS', 'linkedin.com,indeed.com,glassdoor.com,monster.com,ziprecruiter.com,facebook.com,instagram.com').lower().split(',')))
# Skip binary / media URLs early
SKIP_EXTENSIONS = ('.jpg', '.jpeg', '.png', '.gif', '.svg', '.webp', '.mp4', '.mp3', '.avi', '.mov', '.zip', '.rar', '.exe', '.css', '.js', '.ico', '.woff', '.woff2', '.ttf')

# ---------- Admin-only access ----------
# Credentials live in .env (never committed). Password / secret code are stored
# as SHA-256 hex digests only — the server never sees plaintext at rest.
ADMIN_NAME=os.getenv('ADMIN_NAME', '').strip()
ADMIN_EMAIL=os.getenv('ADMIN_EMAIL', '').strip().lower()
ADMIN_PHONE_DIGITS=re.sub(r'\D', '', os.getenv('ADMIN_PHONE', ''))
ADMIN_PASSWORD_SHA256=os.getenv('ADMIN_PASSWORD_SHA256', '').strip().lower()
ADMIN_SECRET_SHA256=os.getenv('ADMIN_SECRET_SHA256', '').strip().lower()
SESSION_TIMEOUT=int(os.getenv('SESSION_TIMEOUT_SEC', '43200'))
SESSION_SECRET=os.getenv('SESSION_SECRET', '')
if not SESSION_SECRET:
    SESSION_SECRET=secrets.token_hex(32)
    log.warning('SESSION_SECRET not set; using ephemeral secret (sessions reset on restart)')
AUTH_ENABLED=bool(ADMIN_EMAIL and ADMIN_PHONE_DIGITS and ADMIN_PASSWORD_SHA256 and ADMIN_SECRET_SHA256)
SESSIONS={}
SESSION_LOCK=threading.Lock()
FAILED_LOGINS={}
ROBOTS_CACHE = {}
ROBOTS_TTL = 24 * 3600

GLOBAL_SKILL_TERMS = [
 r'python|java\b|javascript|typescript|react|angular|vue|node\.?js|django|fastapi|flask|spring boot|spring|\.net|c#|go\b|golang|rust|php|ruby|rails|swift|kotlin|flutter|dart',
 r'sql|mysql|postgresql|postgres|mongodb|redis|elasticsearch|oracle|sqlite|snowflake|bigquery|redshift|cassandra|dynamodb',
 r'aws|azure|gcp|docker|kubernetes|terraform|jenkins|ci/?cd|git|github|gitlab|linux|bash|ansible|prometheus|grafana',
 r'machine learning|deep learning|nlp|computer vision|tensorflow|pytorch|scikit-learn|pandas|numpy|data science|data analysis|power bi|tableau|excel|etl|spark|hadoop|airflow',
 r'figma|photoshop|illustrator|ui/?ux|product management|agile|scrum|jira|salesforce|sap|hubspot|digital marketing|seo|content writing',
 r'nursing|physiotherapy|accounting|tally|auditing|mechanical|civil|electrical|autocad|solidworks|hr|recruitment|teaching',
]
GLOBAL_CITIES = r'Bangalore|Bengaluru|Hyderabad|Delhi|Noida|Gurugram|Gurgaon|Pune|Mumbai|Chennai|Kolkata|Ahmedabad|Jaipur|Kochi|Coimbatore|Indore|Bhopal|Lucknow|Chandigarh|Dehradun|Remote|Hybrid|London|New York|San Francisco|Toronto|Sydney|Dubai|Singapore|Berlin|Dublin'

DB_LOCK = threading.Lock()

# ---------- Live event bus (SSE) ----------
from collections import deque
LIVE_BUF = deque(maxlen=300)
LIVE_COND = threading.Condition()
LIVE_SEQ = 0
def live_emit(etype, data=None):
    global LIVE_SEQ
    evt={'seq':0,'ts':time.time(),'type':etype,'data':data or {}}
    with LIVE_COND:
        LIVE_SEQ+=1; evt['seq']=LIVE_SEQ
        LIVE_BUF.append(evt)
        LIVE_COND.notify_all()
    return evt

# ---------- Sessions ----------
def _req_cookies(headers):
    out={}
    try:
        for part in (headers.get('Cookie') or '').split(';'):
            if '=' in part:
                k, v=part.strip().split('=', 1); out[k.strip()]=v.strip()
    except Exception: pass
    return out

def _session_valid(headers):
    tok=_req_cookies(headers).get('gca_session', '')
    if not tok: return False
    with SESSION_LOCK:
        s=SESSIONS.get(tok)
        if not s: return False
        if s[0] < time.time():
            SESSIONS.pop(tok, None); return False
        return True

def _verify_admin(name, email, phone, password, secret):
    if not AUTH_ENABLED: return False
    if (email or '').strip().lower() != ADMIN_EMAIL: return False
    if (name or '').strip().lower() != ADMIN_NAME.strip().lower(): return False
    if not ADMIN_PHONE_DIGITS or re.sub(r'\D', '', phone or '') != ADMIN_PHONE_DIGITS: return False
    ph=hashlib.sha256((password or '').encode('utf-8')).hexdigest()
    sh=hashlib.sha256((secret or '').encode('utf-8')).hexdigest()
    return hmac.compare_digest(ph, ADMIN_PASSWORD_SHA256) and hmac.compare_digest(sh, ADMIN_SECRET_SHA256)

def _login_blocked(ip):
    now=time.time()
    with SESSION_LOCK:
        lst=[t for t in FAILED_LOGINS.get(ip, []) if now - t < 300]
        FAILED_LOGINS[ip]=lst
        return len(lst) >= 5

def _login_failed(ip):
    with SESSION_LOCK:
        FAILED_LOGINS.setdefault(ip, []).append(time.time())

SCHEMA = '''
CREATE TABLE IF NOT EXISTS jobs(
 id INTEGER PRIMARY KEY AUTOINCREMENT,
 title TEXT, jd_filename TEXT, jd_text TEXT, requirements_json TEXT,
 created_at TEXT DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS candidates(
 id INTEGER PRIMARY KEY AUTOINCREMENT,
 name TEXT, email TEXT, phone TEXT, location TEXT, current_title TEXT,
 years_experience REAL, skills_json TEXT, education TEXT, summary TEXT,
 cv_text TEXT, cv_path TEXT, source_url TEXT, source_type TEXT,
 source_title TEXT, source_snippet TEXT, fingerprint TEXT UNIQUE,
 score REAL DEFAULT 0, category TEXT DEFAULT 'GOOD',
 match_json TEXT, created_at TEXT DEFAULT CURRENT_TIMESTAMP, updated_at TEXT DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS job_candidates(
 job_id INTEGER, candidate_id INTEGER, score REAL, category TEXT, match_json TEXT,
 PRIMARY KEY(job_id,candidate_id)
);
CREATE TABLE IF NOT EXISTS search_runs(
 id INTEGER PRIMARY KEY AUTOINCREMENT, job_id INTEGER, status TEXT,
 queries INTEGER DEFAULT 0, results INTEGER DEFAULT 0, downloaded INTEGER DEFAULT 0,
 parsed INTEGER DEFAULT 0, candidates INTEGER DEFAULT 0, started_at TEXT DEFAULT CURRENT_TIMESTAMP,
 finished_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_candidates_score ON candidates(score DESC);
CREATE INDEX IF NOT EXISTS idx_candidates_email ON candidates(email);
CREATE INDEX IF NOT EXISTS idx_jobcand_job_score ON job_candidates(job_id, score DESC);
'''

def db():
    c = sqlite3.connect(DATA, timeout=30)
    c.row_factory = sqlite3.Row
    try:
        c.execute('PRAGMA journal_mode=WAL')
        c.execute('PRAGMA synchronous=NORMAL')
    except Exception:
        pass
    c.executescript(SCHEMA)
    return c

def qone(sql, args=()):
    with DB_LOCK:
        c=db(); r=c.execute(sql,args).fetchone(); c.close(); return r

def qall(sql, args=()):
    with DB_LOCK:
        c=db(); r=c.execute(sql,args).fetchall(); c.close(); return [dict(x) for x in r]

def execute(sql,args=()):
    with DB_LOCK:
        c=db(); cur=c.execute(sql,args); c.commit(); x=cur.lastrowid; c.close(); return x

# ---------- AI ----------

def _json_from_text(txt):
    if not txt: return None
    txt=txt.strip()
    if txt.startswith('```'):
        txt=re.sub(r'^```(?:json)?', '', txt, flags=re.I).strip()
        txt=re.sub(r'```$', '', txt).strip()
    try: return json.loads(txt)
    except Exception:
        m=re.search(r'\{.*\}',txt,re.S)
        return json.loads(m.group(0)) if m else None

def ollama_json(instructions, payload):
    if not OLLAMA_MODEL: return None
    body={
      'model':OLLAMA_MODEL,
      'stream':False,
      'format':'json',
      'options':{'temperature':0}
    }
    body['prompt']=instructions+'\n\nDATA:\n'+payload[:12000]
    try:
        r=requests.post(OLLAMA_BASE_URL+'/api/generate',json=body,timeout=90)
        r.raise_for_status()
        return _json_from_text(r.json().get('response',''))
    except Exception as e:
        log.warning('Ollama error: %s', e)
        return None

def openai_json(instructions, payload):
    key=os.getenv('OPENAI_API_KEY')
    if key:
        base=os.getenv('OPENAI_BASE_URL','https://api.openai.com/v1').rstrip('/')
        model=os.getenv('OPENAI_MODEL','gpt-4o-mini') or 'gpt-4o-mini'
        # Try /responses first, fall back to /chat/completions for compat with gateways
        payload_small = payload[:15000]
        try:
            r=requests.post(base+'/responses',headers={'Authorization':f'Bearer {key}','Content-Type':'application/json'},json={'model':model,'input':[{'role':'system','content':instructions},{'role':'user','content':payload_small}]},timeout=60)
            r.raise_for_status(); data=r.json()
            if data.get('output_text'): txt=data['output_text']
            else:
                parts=[]
                for item in data.get('output',[]):
                    for c in item.get('content',[]) if isinstance(item,dict) else []:
                        if isinstance(c,dict):
                            t=c.get('text')
                            if isinstance(t,str): parts.append(t)
                            elif isinstance(t,dict) and t.get('value'): parts.append(str(t['value']))
                txt='\n'.join(parts)
            parsed=_json_from_text(txt)
            if parsed: return parsed
        except Exception as e:
            log.warning('OpenAI /responses failed (%s), trying /chat/completions', e)
        try:
            r=requests.post(base+'/chat/completions',headers={'Authorization':f'Bearer {key}','Content-Type':'application/json'},json={'model':model,'messages':[{'role':'system','content':instructions},{'role':'user','content':payload_small}],'temperature':0,'response_format':{'type':'json_object'}},timeout=60)
            r.raise_for_status(); data=r.json()
            txt=data['choices'][0]['message']['content'] if data.get('choices') else ''
            return _json_from_text(txt)
        except Exception as e:
            log.warning('OpenAI error: %s', e)
    return ollama_json(instructions, payload)

def extract_jd(text):
    prompt='''Extract a recruitment job description into strict JSON. Return only JSON with keys: title, must_have (array), preferred (array), minimum_years (number or 0), locations (array), education (array), keywords (array). Distinguish mandatory requirements from nice-to-have. Do not infer protected or sensitive traits.'''
    out=openai_json(prompt, text[:15000])
    if out and isinstance(out, dict) and (out.get('must_have') or out.get('title')): return out
    # deterministic fallback
    title='Job Opening'
    for line in text.splitlines():
        s=line.strip()
        if 4 < len(s) < 100 and not re.search(r'@|www\.|https?://|^\W*$',s):
            # skip generic headers
            if not re.match(r'(?i)^(job description|about (us|the role|company)|overview|responsibilities)', s):
                title=s; break
    combined='|'.join(GLOBAL_SKILL_TERMS)
    skill_terms=re.findall(r'\b(?:'+combined+r')\b',text,re.I)
    skills=list(dict.fromkeys(x.lower().strip() for x in skill_terms if x.strip()))
    m=re.search(r'(\d+(?:\.\d+)?)\+?\s*(?:years|yrs)',text,re.I)
    years=float(m.group(1)) if m else 0
    loc=[]
    for x in re.findall(r'\b('+GLOBAL_CITIES+r')\b',text,re.I):
        if x.lower() not in [z.lower() for z in loc]: loc.append(x)
    return {'title':title,'must_have':skills[:10],'preferred':[],'minimum_years':years,'locations':loc,'education':[],'keywords':skills}

def extract_candidate(text, source_url=''):
    prompt='''Extract a candidate resume/profile into JSON. Return keys: name, email, phone, location, current_title, years_experience, skills (array), education (array), summary. Use null when unknown. Only extract information actually present in the supplied content.'''
    out=openai_json(prompt, text[:12000])
    if out and isinstance(out, dict): return out
    # fallback extraction
    email=re.search(r'[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}',text)
    phone=re.search(r'(?:\+?\d[\d\s().-]{8,}\d)',text)
    lines=[x.strip() for x in text.splitlines() if x.strip() and len(x.strip())>1]
    # skip nav junk for name guess
    junk_pat=re.compile(r'(?i)^(home|menu|search|login|sign in|cookies|privacy|home\s*›|skip to|all rights reserved|copyright)')
    name='Unknown candidate'
    for ln in lines[:15]:
        if junk_pat.search(ln) or len(ln)>120 or '@' in ln or 'http' in ln.lower(): continue
        if re.search(r'(?i)\b(resume|curriculum vitae|work experience|professional summary)\b', ln): continue
        name=ln[:120]; break
    years=re.search(r'(\d+(?:\.\d+)?)\+?\s*(?:years|yrs)',text,re.I)
    combined='|'.join(GLOBAL_SKILL_TERMS)
    fb_skills=list(dict.fromkeys(x.lower() for x in re.findall(r'\b(?:'+combined+r')\b', text, re.I)))[:20]
    # current title heuristic: line with senior/engineer/manager/etc near top
    cur_title=None
    for ln in lines[:20]:
        if re.search(r'(?i)\b(senior|junior|lead|principal|engineer|developer|designer|manager|analyst|scientist|consultant|architect|specialist|executive|accountant|nurse)\b', ln) and len(ln)<120:
            cur_title=ln[:140]; break
    return {'name':name,'email':email.group(0) if email else None,'phone':phone.group(0) if phone else None,'location':None,'current_title':cur_title,'years_experience':float(years.group(1)) if years else 0,'skills':fb_skills,'education':[],'summary':' '.join(lines[1:4])[:500],'_source_url':source_url}

# ---------- Search providers ----------
def normalize_url(u):
    try:
        p = urlparse(u.strip())
        if not p.scheme: return ''
        netloc = p.netloc.lower()
        if netloc.startswith('www.'): netloc = netloc[4:]
        # strip tracking params
        q = [(k, v) for k, v in parse_qsl(p.query) if not k.lower().startswith(('utm_', 'gclid', 'fbclid', 'mc_', 'igsh'))]
        path = p.path.rstrip('/') or '/'
        return urlunparse((p.scheme.lower(), netloc, path, '', urlencode(q), ''))
    except Exception:
        return (u or '').strip()

def is_skipped_domain(url):
    try:
        host = urlparse(url).netloc.lower()
        if host.startswith('www.'): host = host[4:]
        return any(host == d or host.endswith('.' + d) for d in SKIP_FETCH_DOMAINS)
    except Exception:
        return False

def _parse_ddg_html(html_text, limit):
    soup=BeautifulSoup(html_text,'html.parser')
    out=[]
    for a in soup.select('.result__a')[:limit]:
        href=a.get('href','')
        # DDG wraps with //duckduckgo.com/l/?uddg=
        if 'uddg=' in href:
            try:
                from urllib.parse import parse_qs
                qs=parse_qs(urlparse(href).query)
                if qs.get('uddg'): href=qs['uddg'][0]
            except Exception: pass
        title=a.get_text(' ',strip=True)
        parent=a.find_parent(class_='result')
        snippet=''
        if parent:
            sn=parent.select_one('.result__snippet')
            snippet=sn.get_text(' ',strip=True) if sn else ''
        if href.startswith('//'): href='https:'+href
        if href: out.append({'title':title,'url':href,'snippet':snippet,'source':'duckduckgo'})
    return out

def duckduckgo_html(query):
    if not FREE_SEARCH_ENABLED: return []
    h={'User-Agent':'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124 Safari/537.36'}
    # primary + lite fallback (both free, no key)
    for endpoint, is_post in (('https://html.duckduckgo.com/html/', True), ('https://lite.duckduckgo.com/lite/', True)):
        try:
            time.sleep(FREE_SEARCH_DELAY / 2)
            if is_post: r=requests.post(endpoint,data={'q':query},headers=h,timeout=25)
            else: r=requests.get(endpoint,params={'q':query},headers=h,timeout=25)
            r.raise_for_status()
            out=_parse_ddg_html(r.text, RESULTS_PER_QUERY)
            if out: return out
        except Exception as e:
            log.warning('DuckDuckGo %s: %s', endpoint, e); continue
    return []

def mojeek_free(query):
    if not FREE_SEARCH_ENABLED: return []
    try:
        time.sleep(FREE_SEARCH_DELAY / 2)
        h={'User-Agent':'Mozilla/5.0 (compatible; GlobalCVAgent/1.0; +local-public-web-research)'}
        r=requests.get('https://www.mojeek.com/search',params={'q':query},headers=h,timeout=25)
        r.raise_for_status()
        soup=BeautifulSoup(r.text,'html.parser')
        out=[]
        for a in soup.select('a.title')[:RESULTS_PER_QUERY]:
            href=a.get('href',''); title=a.get_text(' ',strip=True)
            parent=a.find_parent('li') or a.parent
            snippet=parent.get_text(' ',strip=True)[:400] if parent else ''
            if href.startswith('/'): href=urljoin('https://www.mojeek.com', href)
            if href.startswith('http'): out.append({'title':title,'url':href,'snippet':snippet,'source':'mojeek'})
        return out
    except Exception as e:
        log.warning('Mojeek: %s', e); return []

def bing_html_free(query):
    if not FREE_SEARCH_ENABLED: return []
    try:
        time.sleep(FREE_SEARCH_DELAY / 2)
        h={'User-Agent':'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'}
        r=requests.get('https://www.bing.com/search',params={'q':query,'count':RESULTS_PER_QUERY},headers=h,timeout=25)
        r.raise_for_status()
        soup=BeautifulSoup(r.text,'html.parser')
        out=[]
        for li in soup.select('li.b_algo')[:RESULTS_PER_QUERY]:
            a=li.select_one('h2 a')
            if not a: continue
            href=a.get('href',''); title=a.get_text(' ',strip=True)
            sn=li.select_one('.b_caption p, p')
            snippet=sn.get_text(' ',strip=True) if sn else ''
            if href.startswith('http'): out.append({'title':title,'url':href,'snippet':snippet,'source':'bing-html'})
        return out
    except Exception as e:
        log.warning('Bing-html: %s', e); return []

def public_web_search(query):
    out=[]
    if FREE_SEARCH_ENABLED:
        out.extend(duckduckgo_html(query))
        if len(out) < RESULTS_PER_QUERY:
            out.extend(bing_html_free(query))
        if len(out) < RESULTS_PER_QUERY:
            out.extend(mojeek_free(query))
    return out

def serper(query):
    key=os.getenv('SERPER_API_KEY')
    if not key: return []
    try:
        r=requests.post('https://google.serper.dev/search',headers={'X-API-KEY':key,'Content-Type':'application/json'},json={'q':query,'num':RESULTS_PER_QUERY,'gl':SEARCH_COUNTRY,'hl':SEARCH_LANGUAGE},timeout=30)
        r.raise_for_status(); data=r.json()
        return [{'title':x.get('title',''),'url':x.get('link',''),'snippet':x.get('snippet',''),'source':'serper'} for x in data.get('organic',[])]
    except Exception as e:
        log.warning('Serper: %s', e); return []

def brave(query):
    key=os.getenv('BRAVE_SEARCH_API_KEY')
    if not key: return []
    try:
        r=requests.get('https://api.search.brave.com/res/v1/web/search',headers={'X-Subscription-Token':key,'Accept':'application/json'},params={'q':query,'count':RESULTS_PER_QUERY,'country':SEARCH_COUNTRY,'search_lang':SEARCH_LANGUAGE},timeout=30)
        r.raise_for_status(); data=r.json()
        return [{'title':x.get('title',''),'url':x.get('url',''),'snippet':x.get('description',''),'source':'brave'} for x in data.get('web',{}).get('results',[])]
    except Exception as e:
        log.warning('Brave: %s', e); return []

def bing(query):
    key=os.getenv('BING_SEARCH_API_KEY')
    if not key: return []
    try:
        r=requests.get('https://api.bing.microsoft.com/v7.0/search',headers={'Ocp-Apim-Subscription-Key':key},params={'q':query,'count':RESULTS_PER_QUERY,'mkt':f'{SEARCH_LANGUAGE}-{SEARCH_COUNTRY}'},timeout=30)
        r.raise_for_status(); data=r.json()
        return [{'title':x.get('name',''),'url':x.get('url',''),'snippet':x.get('snippet',''),'source':'bing'} for x in data.get('webPages',{}).get('value',[])]
    except Exception as e:
        log.warning('Bing: %s', e); return []

def search_all(query):
    out=[]
    # Free path always runs first. Paid APIs are optional enhancements.
    for fn in (public_web_search,serper,brave,bing): out.extend(fn(query))
    seen=set(); clean=[]
    for x in out:
        raw=x.get('url','')
        if not raw: continue
        u=normalize_url(raw) or raw
        if not u or u in seen: continue
        # skip media files early
        if u.lower().split('?')[0].endswith(SKIP_EXTENSIONS): continue
        seen.add(u); x['url']=u; clean.append(x)
    return clean

# ---------- URL/CV handling ----------
def allowed_fetch(url):
    p=urlparse(url)
    if p.scheme not in ('http','https') or not p.netloc: return False
    host=p.netloc.lower()
    now=time.time()
    cached=ROBOTS_CACHE.get(host)
    if cached and now - cached[1] < ROBOTS_TTL:
        rp=cached[0]
        try: return rp.can_fetch('GlobalCVAgentBot/1.0', url)
        except Exception: return True
    try:
        rp=robotparser.RobotFileParser(); rp.set_url(f'{p.scheme}://{p.netloc}/robots.txt'); rp.read()
        ROBOTS_CACHE[host]=(rp, now)
        return rp.can_fetch('GlobalCVAgentBot/1.0', url)
    except Exception:
        return True

def fetch(url):
    if not url or is_skipped_domain(url):
        return None
    if url.lower().split('?')[0].endswith(SKIP_EXTENSIONS):
        return None
    if not allowed_fetch(url): return None
    try:
        h={'User-Agent':'GlobalCVAgentBot/1.0 (+recruitment sourcing; respects robots.txt)'}
        r=requests.get(url,headers=h,timeout=20,stream=True,allow_redirects=True)
        if r.status_code>=400: return None
        ct=(r.headers.get('Content-Type') or '').lower()
        is_html='html' in ct or 'text/' in ct
        limit_mb=MAX_HTML_MB if is_html else MAX_MB
        cl=r.headers.get('Content-Length')
        if cl:
            try:
                if int(cl)>limit_mb*1024*1024: return None
            except ValueError: pass
        chunks=[]; n=0
        for c in r.iter_content(65536):
            if not c: continue
            n+=len(c)
            if n>limit_mb*1024*1024: return None
            chunks.append(c)
        b=b''.join(chunks)
        return {'url':r.url,'bytes':b,'content_type':ct}
    except Exception as e:
        log.debug('fetch failed %s: %s', url, e)
        return None

def parse_document(obj):
    if not obj: return '', 'unknown'
    b=obj['bytes']; ct=(obj.get('content_type') or '').lower(); url=(obj.get('url') or '').lower().split('?')[0]
    if 'pdf' in ct or url.endswith('.pdf'):
        try:
            if fitz:
                doc=fitz.open(stream=b, filetype='pdf')
                text='\n'.join(page.get_text() for page in doc); doc.close()
            else:
                with tempfile.NamedTemporaryFile(suffix='.pdf', delete=False) as tf:
                    tf.write(b); tmp=tf.name
                try:
                    rd=PdfReader(tmp); text='\n'.join((p.extract_text() or '') for p in rd.pages)
                finally:
                    try: os.unlink(tmp)
                    except Exception: pass
        except Exception as e:
            log.debug('pdf parse failed: %s', e); return '', 'pdf'
        return text or '','pdf'
    if 'wordprocessingml.document' in ct or url.endswith('.docx'):
        if not Document: return '', 'docx'
        with tempfile.NamedTemporaryFile(suffix='.docx', delete=False) as tf:
            tf.write(b); tmp=tf.name
        try:
            doc=Document(tmp)
            paras=[p.text for p in doc.paragraphs]
            tables=[cell.text for table in doc.tables for row in table.rows for cell in row.cells]
            text='\n'.join(paras + tables)
        except Exception as e:
            log.debug('docx parse failed: %s', e); return '', 'docx'
        finally:
            try: os.unlink(tmp)
            except Exception: pass
        return text or '','docx'
    if url.endswith('.doc') or 'msword' in ct:
        return '', 'doc'
    if 'html' in ct or 'text/' in ct or b'<html' in b[:800].lower() or b'<!doctype' in b[:800].lower():
        try:
            soup=BeautifulSoup(b,'html.parser')
            for tag in soup(['script','style','noscript','svg','nav','footer','header','aside','form','iframe']): tag.decompose()
            main=soup.select_one('main, article, .resume, .cv, [role=main]')
            root=main or soup.body or soup
            return root.get_text('\n',strip=True),'html'
        except Exception: return '', 'html'
    try: return b.decode('utf-8','ignore'),'text'
    except: return '','unknown'

def is_candidate_like(text,title,snippet,url):
    if len(text)<600: return False
    s=((text[:8000]+' '+title+' '+snippet)).lower()
    has_cv_kw=any(x in s for x in ['resume','curriculum vitae','career objective','professional summary','work experience','work history','employment history'])
    has_contact=bool(re.search(r'[\w.+-]+@[\w.-]+\.[a-z]{2,}|linkedin\.com/in|github\.com/', s))
    has_sections=sum(1 for x in ['experience','education','skills','projects','certifications','summary','objective'] if x in s)
    ul=(url or '').lower()
    is_doc=ul.split('?')[0].endswith(('.pdf','.docx','.doc','.txt')) or 'resume' in ul or 'cv' in ul
    if has_cv_kw and (has_contact or has_sections>=2): return True
    if is_doc and (has_contact or has_sections>=3): return True
    if re.search(r'(?i)\b(apply now|job description|we are hiring|job opening)\b', title+' '+snippet) and not has_cv_kw:
        return False
    return False

def build_queries(jd):
    must=[str(x) for x in jd.get('must_have',[])][:8]
    pref=[str(x) for x in jd.get('preferred',[])][:4]
    title=(jd.get('title') or '').strip().strip('"')
    loc=' '.join(jd.get('locations',[])[:2])
    core=' '.join([f'"{title}"' if title and title.lower()!='job opening' else '', *[f'"{x}"' for x in must[:3]], loc]).strip()
    if not core: core=' '.join(must[:3] + pref[:2]).strip() or 'resume CV'
    queries=[
        f'{core} resume CV',
        f'{core} "curriculum vitae"',
        f'{core} filetype:pdf',
        f'{core} filetype:docx',
        f'"{title}" resume {loc}'.strip() if title else f'resume {" ".join(must[:3])}',
        f'"{title}" CV {" ".join(must[:4])}'.strip() if title else f'CV {" ".join(must[:4])}',
        f'"{title}" portfolio {loc}'.strip() if title else f'portfolio {" ".join(must[:2])}',
        f'{core} site:github.com OR site:gitlab.com OR site:behance.net',
        f'{core} inurl:resume OR inurl:cv',
    ]
    for skill in must[:8]:
        queries.append(f'"{title}" "{skill}" resume'.strip() if title else f'"{skill}" resume CV')
    for p in pref[:3]:
        queries.append(f'"{title}" "{p}" CV'.strip() if title else f'"{p}" CV resume')
    return list(dict.fromkeys(q.strip() for q in queries if q and q.strip()))[:MAX_QUERIES]

# ---------- Matching ----------
def normalize(s): return re.sub(r'[^a-z0-9+#.]',' ',str(s).lower())
def _term_in_text(term_norm, text_norm, skills_set):
    t=(term_norm or '').strip()
    if not t: return False
    if t in skills_set: return True
    try:
        return re.search(r'(?<![a-z0-9+#.])'+re.escape(t)+r'(?![a-z0-9+#.])', text_norm) is not None
    except re.error:
        return t in text_norm
def deterministic_match(candidate, jd):
    cskills=set(normalize(x).strip() for x in candidate.get('skills',[]) if x)
    text=normalize(' '.join([candidate.get('summary','') or '',candidate.get('current_title','') or '',candidate.get('cv_text','') or '']))
    must=[str(x) for x in jd.get('must_have',[])]; pref=[str(x) for x in jd.get('preferred',[])]
    hits=[]; miss=[]
    for x in must:
        if _term_in_text(normalize(x), text, cskills): hits.append(x)
        else: miss.append(x)
    ph=[]
    for x in pref:
        if _term_in_text(normalize(x), text, cskills): ph.append(x)
    if must: score=(len(hits)/len(must))*80
    else: score=60
    if pref: score+=(len(ph)/len(pref))*10
    else: score+=5
    cy=float(candidate.get('years_experience') or 0); jy=float(jd.get('minimum_years') or 0)
    if jy: score += 10 if cy>=jy else max(0,10*(cy/max(jy,0.1)))
    else: score += 10
    try:
        jlocs=[str(x).lower() for x in jd.get('locations',[])]
        cloc=str(candidate.get('location') or '').lower()
        if jlocs and cloc and any(j in cloc or cloc in j for j in jlocs if j): score+=2
    except Exception: pass
    score=min(100,round(score,1))
    cat='BEST' if score>=85 and len(miss)==0 else 'BETTER' if score>=70 else 'GOOD'
    return {'score':score,'category':cat,'matched':hits,'missing':miss,'preferred_matched':ph,'reason':f'{len(hits)}/{len(must)} must-have matched; {cy:g}y exp vs {jy:g}y required.'}

def ai_match(candidate, jd):
    cv=(candidate.get('cv_text') or '')[:6000]
    payload=json.dumps({'job':jd,'candidate':{**{k:candidate.get(k) for k in ['name','current_title','years_experience','skills','education','summary','location']},'cv_text':cv}},ensure_ascii=False)
    prompt='''Evaluate candidate fit for a job. Return only JSON: {"score": number 0-100, "category": "BEST"|"BETTER"|"GOOD", "matched": [strings], "missing": [strings], "preferred_matched": [strings], "reason": string}. Treat must-have items as hard requirements, preferred items as secondary. Do not use protected characteristics. Base claims only on supplied candidate evidence.'''
    return openai_json(prompt,payload)

def save_candidate(cand, source, job_id, jd, document_obj=None):
    email_norm=str(cand.get('email') or '').strip().lower()
    phone_digits=re.sub(r'\D','',str(cand.get('phone') or ''))
    name_norm=str(cand.get('name') or '').strip().lower()
    cv_hash=hashlib.sha256((cand.get('cv_text') or '')[:2000].encode('utf-8','ignore')).hexdigest()[:16]
    if email_norm: base_key=f'email:{email_norm}'
    elif phone_digits: base_key=f'phone:{phone_digits}'
    elif name_norm and name_norm!='unknown candidate': base_key=f'name:{name_norm}|cv:{cv_hash}|url:{source.get("url","")}'
    else: base_key=f'url:{source.get("url","")}|cv:{cv_hash}'
    fp=hashlib.sha256(base_key.encode()).hexdigest()
    existing=qone('SELECT id FROM candidates WHERE fingerprint=?',(fp,))
    if not existing and cand.get('email'):
        existing=qone('SELECT id FROM candidates WHERE lower(email)=lower(?) LIMIT 1',(cand.get('email').strip(),))
    if not existing and cand.get('phone'):
        ph=re.sub(r'\D','',str(cand.get('phone')))
        if ph: existing=qone('SELECT id FROM candidates WHERE phone LIKE ? LIMIT 1',('%'+ph[-8:],))
    if existing: cid=existing['id']
    else:
        saved_path=None
        if document_obj and document_obj.get('bytes'):
            ct=(document_obj.get('content_type') or '').lower(); u=document_obj.get('url','').lower().split('?')[0]
            if 'pdf' in ct or u.endswith('.pdf'): ext='.pdf'
            elif 'wordprocessingml' in ct or u.endswith('.docx'): ext='.docx'
            elif 'html' in ct: ext='.html'
            else: ext='.html'
            saved_path=UPLOAD_DIR/(fp+ext)
            if not saved_path.exists():
                try: saved_path.write_bytes(document_obj['bytes'])
                except Exception: saved_path=None
        cid=execute('INSERT INTO candidates(name,email,phone,location,current_title,years_experience,skills_json,education,summary,cv_text,cv_path,source_url,source_type,source_title,source_snippet,fingerprint) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',(
            cand.get('name') or 'Unknown candidate',cand.get('email'),cand.get('phone'),cand.get('location'),cand.get('current_title'),float(cand.get('years_experience') or 0),json.dumps(cand.get('skills') or []),json.dumps(cand.get('education') or []),cand.get('summary') or '',cand.get('cv_text') or '',str(saved_path) if saved_path else None,source.get('url'),source.get('source'),source.get('title'),source.get('snippet'),fp))
    match=deterministic_match(cand,jd)
    # Optional AI re-rank only for promising candidates (saves hours on local models)
    if match.get('score',0) >= 40 and (os.getenv('OPENAI_API_KEY') or OLLAMA_MODEL):
        try:
            ai=ai_match(cand,jd)
            if ai and isinstance(ai.get('score'), (int,float)): match=ai
        except Exception as e: log.debug('ai rerank failed: %s', e)
    score=float(match.get('score',0) or 0)
    cat=match.get('category') or ('BEST' if score>=85 else 'BETTER' if score>=70 else 'GOOD')
    # MIN_SCORE gates job linkage, but candidate row is still kept for audit
    execute('UPDATE candidates SET score=?,category=?,match_json=?,updated_at=CURRENT_TIMESTAMP WHERE id=?',(score,cat,json.dumps(match),cid))
    if score>=MIN_SCORE:
        execute('INSERT OR REPLACE INTO job_candidates(job_id,candidate_id,score,category,match_json) VALUES(?,?,?,?,?)',(job_id,cid,score,cat,json.dumps(match)))
    else:
        execute('DELETE FROM job_candidates WHERE job_id=? AND candidate_id=?',(job_id,cid))
    return cid

# ---------- Pipeline ----------
def _fetch_parse(x):
    try:
        obj=fetch(x['url'])
        if not obj: return None
        text,kind=parse_document(obj)
        if len(text)<600 or not is_candidate_like(text,x.get('title',''),x.get('snippet',''),x.get('url','')): return None
        return (x, obj, text)
    except Exception as e:
        log.debug('fetch_parse failed: %s', e); return None

def run_search(job_id):
    job=qone('SELECT * FROM jobs WHERE id=?',(job_id,))
    if not job: return
    jd=json.loads(job['requirements_json'])
    run_id=execute('INSERT INTO search_runs(job_id,status) VALUES(?,?)',(job_id,'RUNNING'))
    live_emit('run_started',{'run_id':run_id,'job_id':job_id,'title':job['title']})
    try:
        queries=build_queries(jd)
        live_emit('queries_built',{'run_id':run_id,'job_id':job_id,'queries':len(queries)})
        results=[]; seen=set()
        for qi, query in enumerate(queries):
            for x in search_all(query):
                u=normalize_url(x.get('url','')) or x.get('url','')
                if not u or u in seen: continue
                seen.add(u); x['url']=u; results.append(x)
                if len(results)>=MAX_CANDIDATES*3: break
            if (qi+1) % 5 == 0 or (qi+1)==len(queries):
                execute('UPDATE search_runs SET queries=?,results=? WHERE id=?',(len(queries),len(results),run_id))
                live_emit('progress',{'run_id':run_id,'job_id':job_id,'stage':'search','queries_done':qi+1,'queries':len(queries),'results':len(results),'downloaded':0,'parsed':0,'candidates':0})
            if len(results)>=MAX_CANDIDATES*3: break
            if len(results)>=40: time.sleep(0.3)
        live_emit('results_found',{'run_id':run_id,'job_id':job_id,'queries':len(queries),'results':len(results)})
        downloaded=parsed=cs=0
        # concurrent fetch+parse, sequential AI/DB (sqlite + local LLM are the bottleneck)
        candidates_pre=[]
        with ThreadPoolExecutor(max_workers=max(2, FETCH_WORKERS)) as ex:
            futs={ex.submit(_fetch_parse, x): x for x in results[:MAX_CANDIDATES*2]}
            for f in as_completed(futs):
                r=f.result()
                if not r: continue
                downloaded+=1
                x, obj, text=r
                parsed+=1
                # cheap deterministic pre-score to prioritize AI budget
                tmp_cand={'skills':[],'summary':'','current_title':'','cv_text':text,'years_experience':0}
                try:
                    pre=deterministic_match(tmp_cand, jd)
                except Exception: pre={'score':50}
                candidates_pre.append((pre.get('score',0), x, obj, text))
                if len(candidates_pre)>=MAX_CANDIDATES: break
        candidates_pre.sort(key=lambda t: t[0], reverse=True)
        live_emit('progress',{'run_id':run_id,'job_id':job_id,'stage':'fetch','queries':len(queries),'results':len(results),'downloaded':downloaded,'parsed':parsed,'candidates':0})
        for idx,(pre_score, x, obj, text) in enumerate(candidates_pre[:MAX_CANDIDATES]):
            try:
                cand=extract_candidate(text,x['url']); cand['cv_text']=text[:15000]
                # skip AI for low pre-score beyond budget
                cid=save_candidate(cand,x,job_id,jd,obj); cs+=1
                try:
                    live_emit('candidate_found',{'run_id':run_id,'job_id':job_id,'candidate_id':cid,'name':cand.get('name'),'score':None,'source_url':x.get('url')})
                except Exception: pass
            except Exception as e: log.warning('candidate save: %s', e)
            if idx % 5 == 0:
                execute('UPDATE search_runs SET queries=?,results=?,downloaded=?,parsed=?,candidates=? WHERE id=?',(len(queries),len(results),downloaded,parsed,cs,run_id))
                live_emit('progress',{'run_id':run_id,'job_id':job_id,'stage':'rank','queries':len(queries),'results':len(results),'downloaded':downloaded,'parsed':parsed,'candidates':cs})
        execute('UPDATE search_runs SET status=?,queries=?,results=?,downloaded=?,parsed=?,candidates=?,finished_at=CURRENT_TIMESTAMP WHERE id=?',('DONE',len(queries),len(results),downloaded,parsed,cs,run_id))
        live_emit('run_done',{'run_id':run_id,'job_id':job_id,'queries':len(queries),'results':len(results),'downloaded':downloaded,'parsed':parsed,'candidates':cs})
    except Exception as e:
        log.exception('run search error: %s', e)
        execute('UPDATE search_runs SET status=?,finished_at=CURRENT_TIMESTAMP WHERE id=?',('ERROR',run_id))
        try: live_emit('run_error',{'run_id':run_id,'job_id':job_id,'error':str(e)[:300]})
        except Exception: pass

# ---------- HTTP ----------
HTML_INDEX='''<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Global CV Agent</title><link rel="stylesheet" href="/static/app.css"></head><body><div id="app"></div><script src="/static/app.js"></script></body></html>'''

LOGIN_HTML='''<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Sign in — Global CV Agent</title><style>
*{box-sizing:border-box}body{margin:0;font-family:Inter,system-ui,-apple-system,Segoe UI,Roboto,Arial,sans-serif;background:radial-gradient(circle at 20% 0%,#17213b 0,#080b12 35%),#080b12;color:#f4f7fb;min-height:100vh;display:grid;place-items:center;padding:20px}
.card{width:100%;max-width:420px;background:linear-gradient(180deg,rgba(20,27,40,.95),rgba(12,17,26,.96));border:1px solid #273245;border-radius:18px;padding:28px;box-shadow:0 14px 40px #00000040}
.brand{display:flex;gap:11px;align-items:center;font-weight:800;font-size:19px;margin-bottom:6px}.logo{width:36px;height:36px;border-radius:11px;background:linear-gradient(135deg,#6d7cff,#8e67ff 65%,#4de0bc);display:grid;place-items:center}
.sub{color:#9ba8ba;font-size:13px;margin-bottom:20px}label{display:block;font-size:12px;color:#9ba8ba;margin:12px 0 5px}input{width:100%;background:#0d131d;color:#fff;border:1px solid #273245;border-radius:10px;padding:10px 12px;font:inherit}input:focus{outline:none;border-color:#6d7cff}
button{width:100%;margin-top:20px;border:1px solid #6d7cff77;background:linear-gradient(135deg,#6d7cff,#785cf2);color:#fff;padding:12px;border-radius:11px;cursor:pointer;font-weight:700;font-size:15px}button:disabled{opacity:.6;cursor:wait}
.err{display:none;margin-top:14px;padding:10px 12px;border:1px solid #7a2b36;border-radius:10px;background:#2a1218;color:#ff9aa5;font-size:13px}.lock{margin-top:16px;text-align:center;color:#7f8ca0;font-size:11px}
</style></head><body><div class="card"><div class="brand"><div class="logo">◎</div>Global CV Agent</div>
<div class="sub">Restricted access — administrator sign-in required.</div>
<div class="err" id="err"></div>
<label for="name">Full name</label><input id="name" autocomplete="name" placeholder="Your full name">
<label for="email">Email</label><input id="email" type="email" autocomplete="username" placeholder="you@example.com">
<label for="phone">Phone</label><input id="phone" type="tel" autocomplete="tel" placeholder="+91 ...">
<label for="password">Password</label><input id="password" type="password" autocomplete="current-password" placeholder="••••••••">
<label for="secret">Secret code</label><input id="secret" type="password" placeholder="••••••">
<button id="go" onclick="doLogin()">Sign in</button>
<div class="lock">Single-administrator system. All fields are verified.</div></div>
<script>
async function doLogin(){
var e=document.getElementById('err');e.style.display='none';
var b=document.getElementById('go');b.disabled=true;b.textContent='Verifying…';
var body={name:document.getElementById('name').value,email:document.getElementById('email').value,phone:document.getElementById('phone').value,password:document.getElementById('password').value,secret_code:document.getElementById('secret').value};
try{var r=await fetch('/api/login',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});
var d=await r.json();if(!r.ok)throw new Error(d.error||'Sign-in failed');location.href='/';}
catch(err){e.textContent=err.message;e.style.display='block';b.disabled=false;b.textContent='Sign in';}}
document.addEventListener('keydown',function(ev){if(ev.key==='Enter')doLogin();});
</script></body></html>'''

class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        log.info('%s %s', self.address_string(), fmt % args)
    def _send(self,code,body,ctype='application/json'):
        b=body.encode() if isinstance(body,str) else body
        self.send_response(code); self.send_header('Content-Type',ctype); self.send_header('Content-Length',str(len(b))); self.end_headers(); self.wfile.write(b)
    def _json(self,obj,code=200): self._send(code,json.dumps(obj,ensure_ascii=False),'application/json; charset=utf-8')
    def _authed(self):
        return _session_valid(self.headers)
    def do_HEAD(self):
        # Health/port scanners (Render, load balancers) probe with HEAD.
        p=self.path
        known=(p=='/' or p=='/index.html' or p=='/dashboard' or p.startswith('/dashboard?')
               or p.startswith('/static/') or p.startswith('/api/'))
        self.send_response(200 if known else 404)
        self.send_header('Content-Length','0'); self.end_headers()
    def do_GET(self):
        if self.path=='/' or self.path=='/index.html' or self.path=='/dashboard' or self.path.startswith('/dashboard?'):
            if self._authed(): return self._send(200,HTML_INDEX,'text/html; charset=utf-8')
            return self._send(200,LOGIN_HTML,'text/html; charset=utf-8')
        if self.path=='/api/login': return self._json({'ok':False,'error':'use POST'},405)
        if self.path.startswith('/api/') and not self._authed():
            return self._json({'error':'login required'},401)
        if self.path=='/api/me': return self._json({'authenticated':True,'name':ADMIN_NAME})
        if self.path=='/api/live' or self.path.startswith('/api/live?'): return self.sse_live()
        if self.path.startswith('/static/'):
            try:
                rel=self.path[len('/static/'):].split('?')[0].split('#')[0]
                p=(STATIC_DIR / rel).resolve()
                if not str(p).startswith(str(STATIC_DIR)): return self._send(403,'forbidden','text/plain')
                if p.exists() and p.is_file(): return self._send(200,p.read_bytes(),mimetypes.guess_type(str(p))[0] or 'application/octet-stream')
            except Exception: pass
            return self._send(404,'not found','text/plain')
        if self.path.startswith('/api/candidates/') and self.path.endswith('/file'):
            try:
                cid=int(self.path.split('/')[3])
                r=qone('SELECT cv_path FROM candidates WHERE id=?',(cid,))
                if not r: return self._json({'error':'not found'},404)
                fp=Path(r['cv_path']) if r['cv_path'] else None
                if not fp or not fp.exists() or fp.parent.resolve()!=UPLOAD_DIR.resolve(): return self._json({'error':'file unavailable'},404)
                ctype=mimetypes.guess_type(str(fp))[0] or 'application/octet-stream'
                b=fp.read_bytes()
                self.send_response(200); self.send_header('Content-Type',ctype); self.send_header('Content-Disposition',f'attachment; filename="{fp.name}"'); self.send_header('Content-Length',str(len(b))); self.end_headers(); self.wfile.write(b); return
            except Exception as e:
                return self._json({'error':str(e)},400)
        if self.path.startswith('/api/candidates/'):
            try:
                cid=int(self.path.split('/')[-1]); r=qone('SELECT * FROM candidates WHERE id=?',(cid,))
                if not r: return self._json({'error':'not found'},404)
                d=dict(r); d['skills']=json.loads(d.pop('skills_json') or '[]'); d['education']=json.loads(d.pop('education') or '[]'); d['match']=json.loads(d.pop('match_json') or '{}'); return self._json(d)
            except: pass
        if self.path=='/api/jobs': return self._json(qall('SELECT id,title,jd_filename,created_at FROM jobs ORDER BY id DESC'))
        if self.path.startswith('/api/candidates'):
            from urllib.parse import urlparse as _up, parse_qs as _pqs
            # /api/candidates?job_id=&category=&q=&min_score=&limit=&offset=
            if self.path.startswith('/api/candidates?') or self.path=='/api/candidates':
                qs=_pqs(_up(self.path).query)
                job_id=qs.get('job_id',[None])[0]
                cat=qs.get('category',[None])[0]
                q=qs.get('q',[''])[0].strip().lower()
                try: min_score=float(qs.get('min_score',[MIN_SCORE])[0] or MIN_SCORE)
                except ValueError: min_score=MIN_SCORE
                try: limit=min(500, max(1, int(qs.get('limit',['100'])[0])))
                except ValueError: limit=100
                try: offset=max(0, int(qs.get('offset',['0'])[0]))
                except ValueError: offset=0
                if job_id:
                    rows=qall('SELECT c.id,c.name,c.email,c.phone,c.location,c.current_title,c.years_experience,c.skills_json,c.source_url,c.source_type,jc.score,jc.category,jc.match_json FROM candidates c JOIN job_candidates jc ON jc.candidate_id=c.id WHERE jc.job_id=? ORDER BY jc.score DESC, c.id DESC LIMIT 2000',(int(job_id),))
                else:
                    rows=qall('SELECT id,name,email,phone,location,current_title,years_experience,skills_json,source_url,source_type,score,category,match_json FROM candidates ORDER BY score DESC, id DESC LIMIT 2000')
                if cat and cat!='ALL': rows=[r for r in rows if r.get('category')==cat]
                if min_score: rows=[r for r in rows if float(r.get('score') or 0)>=min_score]
                if q: rows=[r for r in rows if q in ((r.get('name') or '')+' '+(r.get('current_title') or '')+' '+(r.get('skills_json') or '')+' '+(r.get('location') or '')).lower()]
                return self._json(rows[offset:offset+limit])
        if self.path=='/api/stats' or self.path.startswith('/api/stats?'):
            from urllib.parse import urlparse as _up2, parse_qs as _pqs2
            qs=_pqs2(_up2(self.path).query)
            job_id=qs.get('job_id',[None])[0]
            if job_id:
                counts={x['category']:x['n'] for x in qall('SELECT jc.category as category,COUNT(*) n FROM job_candidates jc WHERE jc.job_id=? GROUP BY jc.category',(int(job_id),))}
                total=qone('SELECT COUNT(*) n FROM job_candidates WHERE job_id=?',(int(job_id),))['n']
                run=qone('SELECT * FROM search_runs WHERE job_id=? ORDER BY id DESC LIMIT 1',(int(job_id),))
            else:
                counts={x['category']:x['n'] for x in qall('SELECT category,COUNT(*) n FROM candidates GROUP BY category')}; total=qone('SELECT COUNT(*) n FROM candidates')['n']; run=qone('SELECT * FROM search_runs ORDER BY id DESC LIMIT 1')
            jobs=qone('SELECT COUNT(*) n FROM jobs')['n']; return self._json({'jobs':jobs,'candidates':total,'best':counts.get('BEST',0),'better':counts.get('BETTER',0),'good':counts.get('GOOD',0),'run':dict(run) if run else None})
        if self.path=='/api/config': return self._json({'openai':bool(os.getenv('OPENAI_API_KEY')),'ollama':bool(OLLAMA_MODEL),'ollama_model':OLLAMA_MODEL,'free_web':FREE_SEARCH_ENABLED,'serper':bool(os.getenv('SERPER_API_KEY')),'brave':bool(os.getenv('BRAVE_SEARCH_API_KEY')),'bing':bool(os.getenv('BING_SEARCH_API_KEY')),'min_score':MIN_SCORE,'max_candidates':MAX_CANDIDATES})
        if self.path.startswith('/api/runs'):
            from urllib.parse import urlparse as _upr, parse_qs as _pqsr
            qs=_pqsr(_upr(self.path).query)
            job_id=qs.get('job_id',[None])[0]
            try: limit=min(100, max(1, int(qs.get('limit',['20'])[0])))
            except ValueError: limit=20
            if job_id:
                rows=qall('SELECT sr.*, j.title as job_title FROM search_runs sr LEFT JOIN jobs j ON j.id=sr.job_id WHERE sr.job_id=? ORDER BY sr.id DESC LIMIT ?',(int(job_id), limit))
            else:
                rows=qall('SELECT sr.*, j.title as job_title FROM search_runs sr LEFT JOIN jobs j ON j.id=sr.job_id ORDER BY sr.id DESC LIMIT ?',(limit,))
            return self._json(rows)
        if self.path.startswith('/api/activity'):
            from urllib.parse import urlparse as _upa, parse_qs as _pqsa
            qs=_pqsa(_upa(self.path).query)
            try: limit=min(50, max(1, int(qs.get('limit',['15'])[0])))
            except ValueError: limit=15
            cands=qall('SELECT id,name,current_title,score,category,source_url,created_at FROM candidates ORDER BY id DESC LIMIT ?',(limit,))
            runs=qall('SELECT sr.*, j.title as job_title FROM search_runs sr LEFT JOIN jobs j ON j.id=sr.job_id ORDER BY sr.id DESC LIMIT 10')
            with LIVE_COND:
                evts=list(LIVE_BUF)[-30:]
            return self._json({'candidates':cands,'runs':runs,'events':evts})
        if self.path.startswith('/api/export'):
            from urllib.parse import urlparse as _up3, parse_qs as _pqs3
            qs=_pqs3(_up3(self.path).query)
            job_id=qs.get('job_id',[None])[0]
            fmt=qs.get('format',['csv'])[0]
            if job_id:
                rows=qall('SELECT c.name,c.email,c.phone,c.location,c.current_title,c.years_experience,c.skills_json,c.source_url,jc.score,jc.category,jc.match_json FROM candidates c JOIN job_candidates jc ON jc.candidate_id=c.id WHERE jc.job_id=? ORDER BY jc.score DESC',(int(job_id),))
            else:
                rows=qall('SELECT name,email,phone,location,current_title,years_experience,skills_json,source_url,score,category,match_json FROM candidates ORDER BY score DESC')
            out=io.StringIO(); w=csv.writer(out)
            w.writerow(['name','email','phone','location','title','years','skills','source_url','score','category','reason'])
            for r in rows:
                try: reason=json.loads(r.get('match_json') or '{}').get('reason','')
                except Exception: reason=''
                w.writerow([r.get('name'),r.get('email'),r.get('phone'),r.get('location'),r.get('current_title'),r.get('years_experience'),r.get('skills_json'),r.get('source_url'),r.get('score'),r.get('category'),reason])
            data=out.getvalue().encode('utf-8')
            self.send_response(200); self.send_header('Content-Type','text/csv; charset=utf-8'); self.send_header('Content-Disposition','attachment; filename="candidates.csv"'); self.send_header('Content-Length',str(len(data))); self.end_headers(); self.wfile.write(data); return
        return self._send(404,'not found','text/plain')
    def sse_live(self):
        from urllib.parse import urlparse as _upl, parse_qs as _pqsl
        try: last=int((_pqsl(_upl(self.path).query).get('last',['0'])[0] or 0))
        except ValueError: last=0
        try:
            self.send_response(200)
            self.send_header('Content-Type','text/event-stream')
            self.send_header('Cache-Control','no-cache')
            self.send_header('Connection','keep-alive')
            self.send_header('X-Accel-Buffering','no')
            self.end_headers()
        except (BrokenPipeError, ConnectionResetError):
            return
        # replay missed buffer
        try:
            with LIVE_COND:
                backlog=[e for e in list(LIVE_BUF) if e['seq']>last]
            for e in backlog[-100:]:
                line=f"data: {json.dumps(e, ensure_ascii=False)}\n\n".encode('utf-8')
                self.wfile.write(line)
            self.wfile.flush()
            last=backlog[-1]['seq'] if backlog else last
            # stream ~55s with heartbeat every 15s (proxies/clients time out otherwise)
            end=time.time()+55
            while time.time()<end:
                with LIVE_COND:
                    LIVE_COND.wait(timeout=15)
                    fresh=[e for e in list(LIVE_BUF) if e['seq']>last]
                    if fresh: last=fresh[-1]['seq']
                for e in fresh:
                    self.wfile.write(f"data: {json.dumps(e, ensure_ascii=False)}\n\n".encode('utf-8'))
                # heartbeat comment keeps connection alive
                self.wfile.write(b': ping\n\n')
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as e:
            log.debug('sse closed: %s', e)
        return
    def do_POST(self):
        if self.path=='/api/login': return self.login()
        if self.path=='/api/logout': return self.logout()
        if not self._authed(): return self._json({'error':'login required'},401)
        if self.path=='/api/jobs': return self.create_job()
        if self.path=='/api/search':
            try:
                ln=int(self.headers.get('Content-Length','0')); data=json.loads(self.rfile.read(ln) or b'{}'); job_id=int(data['job_id'])
                threading.Thread(target=run_search,args=(job_id,),daemon=True).start()
                live_emit('search_queued',{'job_id':job_id})
                return self._json({'ok':True,'job_id':job_id})
            except Exception as e: return self._json({'error':str(e)},400)
        if self.path=='/api/candidates/delete':
            try:
                ln=int(self.headers.get('Content-Length','0')); data=json.loads(self.rfile.read(ln)); cid=int(data['id'])
                r=qone('SELECT cv_path FROM candidates WHERE id=?',(cid,))
                execute('DELETE FROM job_candidates WHERE candidate_id=?',(cid,)); execute('DELETE FROM candidates WHERE id=?',(cid,))
                try:
                    if r and r['cv_path']:
                        fp=Path(r['cv_path'])
                        if fp.exists() and fp.resolve().parent==UPLOAD_DIR.resolve(): fp.unlink()
                except Exception: pass
                return self._json({'ok':True})
            except Exception as e: return self._json({'error':str(e)},400)
        return self._json({'error':'not found'},404)
    def login(self):
        try:
            ln=int(self.headers.get('Content-Length', '0')); data=json.loads(self.rfile.read(ln) or b'{}')
        except Exception:
            return self._json({'error': 'invalid request'}, 400)
        ip=self.client_address[0] if self.client_address else 'unknown'
        if _login_blocked(ip):
            return self._json({'error': 'too many attempts, try again in a few minutes'}, 429)
        if not AUTH_ENABLED:
            return self._json({'error': 'admin account is not configured on the server'}, 503)
        ok=_verify_admin(data.get('name'), data.get('email'), data.get('phone'), data.get('password'), data.get('secret_code') or data.get('secretCode'))
        if not ok:
            _login_failed(ip); time.sleep(0.5)
            return self._json({'error': 'invalid credentials'}, 401)
        tok=secrets.token_urlsafe(32)
        with SESSION_LOCK:
            SESSIONS[tok]=(time.time() + SESSION_TIMEOUT, ADMIN_NAME)
            FAILED_LOGINS.pop(ip, None)
            # opportunistic cleanup
            try:
                now=time.time()
                for k in [k for k, v in SESSIONS.items() if v[0] < now]: SESSIONS.pop(k, None)
            except Exception: pass
        body=json.dumps({'ok': True, 'name': ADMIN_NAME}).encode('utf-8')
        self.send_response(200)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Set-Cookie', f'gca_session={tok}; HttpOnly; Path=/; SameSite=Lax; Max-Age={SESSION_TIMEOUT}')
        self.send_header('Content-Length', str(len(body))); self.end_headers(); self.wfile.write(body)
    def logout(self):
        tok=_req_cookies(self.headers).get('gca_session', '')
        with SESSION_LOCK: SESSIONS.pop(tok, None)
        body=json.dumps({'ok': True}).encode('utf-8')
        self.send_response(200)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Set-Cookie', 'gca_session=; HttpOnly; Path=/; SameSite=Lax; Max-Age=0')
        self.send_header('Content-Length', str(len(body))); self.end_headers(); self.wfile.write(body)
    def create_job(self):
        ctype=self.headers.get('Content-Type','')
        ln=int(self.headers.get('Content-Length','0')); body=self.rfile.read(ln)
        # Multipart parser using email package
        if 'multipart/form-data' not in ctype: return self._json({'error':'multipart/form-data required'},400)
        raw=b'Content-Type: '+ctype.encode()+b'\r\nMIME-Version: 1.0\r\n\r\n'+body
        msg=BytesParser(policy=default).parsebytes(raw)
        jd_bytes=None; filename='jd.pdf'
        for part in msg.iter_attachments():
            if part.get_param('name',header='Content-Disposition')=='file':
                jd_bytes=part.get_payload(decode=True); filename=part.get_filename() or filename; break
        if not jd_bytes: return self._json({'error':'attach a PDF/DOCX/TXT as field file'},400)
        if len(jd_bytes)>20*1024*1024: return self._json({'error':'JD file too large (20MB max)'},400)
        safe=re.sub(r'[^A-Za-z0-9._-]','_',filename or 'jd.pdf')
        p=UPLOAD_DIR/(hashlib.sha1(jd_bytes).hexdigest()+'_'+safe); p.write_bytes(jd_bytes)
        low=safe.lower()
        if low.endswith('.docx'): ctype_guess='application/vnd.openxmlformats-officedocument.wordprocessingml.document'
        elif low.endswith('.txt'): ctype_guess='text/plain'
        elif low.endswith('.doc'): ctype_guess='application/msword'
        else: ctype_guess='application/pdf'
        try:
            text,kind=parse_document({'bytes':jd_bytes,'content_type':ctype_guess,'url':safe})
            if kind=='doc' or (not text.strip() and low.endswith('.doc')):
                return self._json({'error':'Legacy .doc needs conversion to .docx. Re-save as .docx or text PDF and retry.'},400)
        except Exception as e: return self._json({'error':f'JD parse failed: {e}'},400)
        if not text.strip(): return self._json({'error':'No extractable text found. Use a text PDF/DOCX/TXT (scanned images need OCR).'},400)
        jd=extract_jd(text)
        jid=execute('INSERT INTO jobs(title,jd_filename,jd_text,requirements_json) VALUES(?,?,?,?)',(jd.get('title') or 'Job Opening',safe,text[:50000],json.dumps(jd,ensure_ascii=False)))
        live_emit('job_created',{'job_id':jid,'title':jd.get('title') or 'Job Opening'})
        return self._json({'ok':True,'job_id':jid,'requirements':jd})

if __name__=='__main__':
    host=os.getenv('APP_HOST','127.0.0.1'); port=int(os.getenv('PORT', os.getenv('APP_PORT','8787')))
    print(f'Global CV Agent: http://{host}:{port}')
    ThreadingHTTPServer((host,port),Handler).serve_forever()
