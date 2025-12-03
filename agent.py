import streamlit as st
import pandas as pd
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from googleapiclient.discovery import build
import gspread
from oauth2client.service_account import ServiceAccountCredentials
from datetime import datetime
from openai import OpenAI
import json

# --- CONFIGURATION ---
# We use .get() to avoid crashing
GOOGLE_API_KEY = st.secrets.get("GOOGLE_API_KEY")
SEARCH_ENGINE_ID = st.secrets.get("SEARCH_ENGINE_ID")
GMAIL_USER = st.secrets.get("GMAIL_USER")
GMAIL_APP_PASSWORD = st.secrets.get("GMAIL_APP_PASSWORD")
OPENAI_API_KEY = st.secrets.get("OPENAI_API_KEY")

SHEET_SCOPE = ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"]
# Your Master Sheet ID
MASTER_SHEET_ID = "14x4FW2Zsbj9g-j5bGt12l5SsK11fWEf94i0t1HxAnas"

if OPENAI_API_KEY:
    client_ai = OpenAI(api_key=OPENAI_API_KEY)

# --- HELPER FUNCTIONS ---

def get_gspread_client():
    if "SHEET_CREDENTIALS" not in st.secrets:
        st.error("⚠️ Missing Sheet Credentials.")
        st.stop()
    creds_dict = dict(st.secrets["SHEET_CREDENTIALS"])
    creds = ServiceAccountCredentials.from_json_keyfile_dict(creds_dict, SHEET_SCOPE)
    return gspread.authorize(creds)

def generate_search_strategy(jd_text, location, work_style, model_choice):
    # STRATEGY UPDATE: We force the AI to produce strictly LinkedIn Profile searches
    # We ask for synonyms to catch more people (e.g. "M365" OR "Microsoft 365")
    
    loc_prompt = f"AND \"{location}\"" if location.strip() else ""
    if "remote" in work_style.lower():
        loc_prompt = "" # Ignore city if remote

    prompt = f"""
    You are an expert Sourcer. Create 3 "X-Ray" Boolean strings to find candidates on LinkedIn.
    
    JOB: {jd_text[:2000]}
    
    RULES:
    1. BASE: All queries MUST start with: site:linkedin.com/in/
    2. TITLES: Use OR for title variations. Example: ("M365 Admin" OR "Microsoft 365 Administrator" OR "SharePoint Admin")
    3. LOCATION: {loc_prompt} (Only include if provided)
    4. NO JUNK: Do not include "intitle:resume" or "filetype:pdf". Stick to profiles.
    
    Output JSON with keys: 'role_title', 'boolean_strings' (list of 3 strings).
    """
    try:
        response = client_ai.chat.completions.create(
            model=model_choice,
            response_format={"type": "json_object"},
            messages=[{"role": "user", "content": prompt}]
        )
        return json.loads(response.choices[0].message.content)
    except Exception as e:
        st.error(f"AI Strategy Error: {e}")
        return None

def search_google(queries):
    if not GOOGLE_API_KEY or not SEARCH_ENGINE_ID:
        st.error("Missing Google Keys")
        return []
    
    service = build("customsearch", "v1", developerKey=GOOGLE_API_KEY)
    results = []
    
    for q in queries:
        try:
            # We explicitly print the query to the UI so you can see what is happening
            print(f"Running Query: {q}") 
            
            res = service.cse().list(q=q, cx=SEARCH_ENGINE_ID, num=10).execute()
            for item in res.get('items', []):
                link = item['link']
                
                # FILTER: STRICTLY LINKEDIN PROFILES ONLY
                # This removes the "login", "job posting", and "company page" junk
                if "linkedin.com/in/" in link:
                    results.append({
                        'Name': item['title'].split("-")[0].strip(),
                        'Link': link,
                        'Snippet': item['snippet']
                    })
        except Exception:
            continue
    return results

def ai_score_candidate(snippet, role, loc, style, model):
    prompt = f"""
    Role: {role} | Loc: {loc} | Style: {style}
    Candidate Snippet: {snippet}
    
    Task: Score 0-100.
    - If snippet looks like a Job Posting or Recruiter, Score 0.
    - If snippet matches skills, Score high.
    
    Output JSON: 'score' (int), 'reason' (string).
    """
    try:
        response = client_ai.chat.completions.create(
            model=model,
            response_format={"type": "json_object"},
            messages=[{"role": "user", "content": prompt}]
        )
        return json.loads(response.choices[0].message.content)
    except: return {"score": 0, "reason": "AI Error"}

def save_results(df, role_name):
    client = get_gspread_client()
    try:
        sh = client.open_by_key(MASTER_SHEET_ID)
    except:
        st.error("❌ Permission Error. Share the sheet with the bot email.")
        st.stop()

    timestamp = datetime.now().strftime("%m-%d %H:%M")
    title = f"{timestamp} - {role_name[:10]}"
    
    try:
        ws = sh.add_worksheet(title=title, rows=20, cols=10)
    except:
        title = f"{title}-{datetime.now().second}"
        ws = sh.add_worksheet(title=title, rows=20, cols=10)

    ws.append_row(['Score', 'Name', 'Reason', 'Link'])
    ws.append_rows(df[['AI Score', 'Name', 'Reason', 'Link']].values.tolist())
    return f"{sh.url}#gid={ws.id}", title

def send_email(email, df, url, role):
    if not email: return
    msg = MIMEMultipart()
    msg['Subject'] = f"Results: {role}"
    msg['From'] = GMAIL_USER
    msg['To'] = email
    
    html = df.head(5)[['AI Score', 'Name', 'Reason']].to_html(index=False)
    body = f"<h3>Results: {role}</h3><a href='{url}'>Open Database</a><br>{html}"
    msg.attach(MIMEText(body, 'html'))
    
    try:
        with smtplib.SMTP_SSL('smtp.gmail.com', 465) as s:
            s.login(GMAIL_USER, GMAIL_APP_PASSWORD)
            s.send_message(msg)
    except: pass

# --- MAIN UI ---
st.set_page_config(page_title="AI Talent Agent", layout="wide")
st.title("🤖 AI Talent Agent")

with st.sidebar:
    model = st.radio("Model", ["gpt-4o", "gpt-4o-mini"])

with st.form("main"):
    email = st.text_input("Send Report To", "judd@sharphuman.com")
    c1, c2 = st.columns(2)
    with c1: loc = st.text_input("Location", placeholder="Leave blank for Remote/World")
    with c2: style = st.text_input("Work Style", value="Remote")
    jd = st.text_area("Job Description")
    
    submitted = st.form_submit_button("Run Agent")

if submitted and jd:
    status = st.status("Agent is working...", expanded=True)
    
    status.write("🧠 Strategy...")
    strat = generate_search_strategy(jd, loc, style, model)
    
    if strat:
        role = strat['role_title']
        queries = strat['boolean_strings']
        status.write(f"🔎 Role: {role}")
        
        # Show user the actual search logic (Debugging)
        with st.expander("See Boolean Search Strings"):
            st.write(queries)

        res = search_google(queries)
        
        # --- AUTO-RETRY LOGIC ---
        if len(res) == 0:
            status.write("⚠️ 0 Results. Trying a broader search (Removing location constraint)...")
            # Generate a broader strategy by force
            broad_strat = generate_search_strategy(jd, "", "Remote", model) # Force broad
            res = search_google(broad_strat['boolean_strings'])

        if res:
            status.write(f"👀 Scoring {len(res)} candidates...")
            scored = []
            progress = status.progress(0)
            for i, r in enumerate(res):
                progress.progress((i+1)/len(res))
                s = ai_score_candidate(r['Snippet'], role, loc, style, model)
                r['AI Score'] = s.get('score', 0)
                r['Reason'] = s.get('reason', 'N/A')
                scored.append(r)
            
            df = pd.DataFrame(scored)
            df = df[df['AI Score'] > 10].sort_values(by='AI Score', ascending=False)
            
            if len(df) > 0:
                status.write("💾 Saving...")
                url, tab = save_results(df, role)
                send_email(email, df, url, role)
                
                status.update(label="✅ Done", state="complete")
                st.success(f"Saved to tab: {tab}")
                
                st.dataframe(
                    df[['AI Score', 'Name', 'Reason', 'Link']],
                    column_config={
                        "Link": st.column_config.LinkColumn("Profile", display_text="Open Link"),
                        "Reason": st.column_config.TextColumn("Analysis", width="large")
                    },
                    hide_index=True,
                    use_container_width=True
                )
            else:
                status.update(label="⚠️ Low Relevance", state="error")
                st.warning("Found profiles, but none matched the JD high enough (Low AI Scores).")
        else:
            status.update(label="⚠️ No Results", state="error")
            st.error("Google returned 0 results even after broadening the search.")