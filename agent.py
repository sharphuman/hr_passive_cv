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
GOOGLE_API_KEY = st.secrets["GOOGLE_API_KEY"]
SEARCH_ENGINE_ID = st.secrets["SEARCH_ENGINE_ID"]
GMAIL_USER = st.secrets["GMAIL_USER"]
GMAIL_APP_PASSWORD = st.secrets["GMAIL_APP_PASSWORD"]
OPENAI_API_KEY = st.secrets["OPENAI_API_KEY"]
SHEET_SCOPE = ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"]

# The specific sheet you want to use
MASTER_SHEET_ID = "14x4FW2Zsbj9g-j5bGt12l5SsK11fWEf94i0t1HxAnas"

client_ai = OpenAI(api_key=OPENAI_API_KEY)

# --- GOOGLE SHEETS FUNCTIONS ---

def get_gspread_client():
    """Authenticates with Google Sheets."""
    creds_dict = dict(st.secrets["SHEET_CREDENTIALS"])
    creds = ServiceAccountCredentials.from_json_keyfile_dict(creds_dict, SHEET_SCOPE)
    return gspread.authorize(creds)

def save_results(df, role_name):
    """
    Saves directly to the hardcoded MASTER_SHEET_ID.
    """
    client = get_gspread_client()
    try:
        sh = client.open_by_key(MASTER_SHEET_ID)
    except Exception as e:
        st.error(f"❌ Permission Error: The bot cannot access your sheet. Please share '{MASTER_SHEET_ID}' with {st.secrets['SHEET_CREDENTIALS']['client_email']} as Editor.")
        st.stop()

    timestamp = datetime.now().strftime("%m-%d %H:%M")
    short_role = (role_name[:15] + '..') if len(role_name) > 15 else role_name
    title = f"{timestamp} - {short_role}"
    
    # Sort Candidates
    df = df.sort_values(by='AI Score', ascending=False)
    
    # Create new tab for this search
    try:
        ws = sh.add_worksheet(title=title, rows=20, cols=10)
    except Exception as e:
        # If tab name exists, append a random number
        title = f"{title} ({datetime.now().second})"
        ws = sh.add_worksheet(title=title, rows=20, cols=10)

    ws.append_row(['AI Score', 'Name', 'Reason', 'Link'])
    ws.append_rows(df[['AI Score', 'Name', 'Reason', 'Link']].values.tolist())
    
    return f"{sh.url}#gid={ws.id}", title

# --- AI & SEARCH FUNCTIONS ---

def generate_search_strategy(jd_text, location, work_style, model_choice):
    prompt = f"""
    JOB: {jd_text[:3000]} | LOC: {location} | STYLE: {work_style}
    Generate 3 Boolean strings (LinkedIn, Niche, Resume). Output JSON keys: 'role_title', 'boolean_strings'.
    """
    try:
        response = client_ai.chat.completions.create(
            model=model_choice, response_format={"type": "json_object"},
            messages=[{"role": "user", "content": prompt}]
        )
        return json.loads(response.choices[0].message.content)
    except: return None

def ai_score_candidate(snippet, role, loc, style, model):
    prompt = f"""
    ROLE: {role} | LOC: {loc} | STYLE: {style} | CANDIDATE: {snippet}
    Score 0-100. Check for role mismatch. Output JSON keys: 'score', 'reason', 'flag'.
    """
    try:
        response = client_ai.chat.completions.create(
            model=model, response_format={"type": "json_object"},
            messages=[{"role": "user", "content": prompt}]
        )
        return json.loads(response.choices[0].message.content)
    except: return {"score": 0, "reason": "Error", "flag": "Unknown"}

def search_google(queries):
    service = build("customsearch", "v1", developerKey=GOOGLE_API_KEY)
    results = []
    for q in queries:
        try:
            res = service.cse().list(q=q, cx=SEARCH_ENGINE_ID, num=10).execute()
            for item in res.get('items', []):
                title_parts = item['title'].split("-")
                name = title_parts[0].strip() if len(title_parts) > 0 else "Unknown"
                if not any(d['Link'] == item['link'] for d in results):
                    results.append({'Name': name, 'Link': item['link'], 'Snippet': item['snippet']})
        except: pass
    return results

def send_email(email, df, url, role):
    msg = MIMEMultipart()
    msg['Subject'] = f"Results: {role}"
    msg['From'] = GMAIL_USER
    msg['To'] = email
    
    html = df.head(5)[['AI Score', 'Name', 'Reason']].to_html(index=False)
    body = f"""
    <h3>AI Agent Results: {role}</h3>
    <p><strong>📂 Database:</strong> <a href='{url}'>Click to Open Sheet</a></p>
    <hr>
    {html}
    """
    msg.attach(MIMEText(body, 'html'))
    
    with smtplib.SMTP_SSL('smtp.gmail.com', 465) as s:
        s.login(GMAIL_USER, GMAIL_APP_PASSWORD)
        s.send_message(msg)

# --- MAIN UI ---
st.set_page_config(page_title="AI Talent Agent", page_icon="🤖", layout="wide")
st.title("🤖 AI Talent Agent")

with st.sidebar:
    model = st.radio("Model", ["gpt-4o", "gpt-4o-mini"])

with st.form("main"):
    # Simplified Inputs
    email = st.text_input("Send Report To", "judd@sharphuman.com")
    
    c1, c2 = st.columns(2)
    with c1: loc = st.text_input("Location (Optional)")
    with c2: style = st.text_input("Work Style (Optional)")
    
    jd = st.text_area("Job Description")
    
    submitted = st.form_submit_button("Run Agent")

if submitted and jd:
    status = st.status("Agent is working...", expanded=True)
    status.write("🧠 Building strategy...")
    
    strat = generate_search_strategy(jd, loc, style, model)
    if strat:
        status.write(f"🔎 Role: {strat['role_title']}")
        res = search_google(strat['boolean_strings'])
        
        if res:
            status.write(f"👀 Scoring {len(res)} profiles...")
            scored = []
            progress_bar = status.progress(0)
            
            for i, r in enumerate(res):
                progress_bar.progress((i + 1) / len(res))
                s = ai_score_candidate(r['Snippet'], strat['role_title'], loc, style, model)
                r['AI Score'] = s.get('score', 0)
                r['Reason'] = s.get('reason', '')
                scored.append(r)
            
            df = pd.DataFrame(scored)
            df = df[df['AI Score'] > 10] 
            
            status.write("💾 Saving to Master Database...")
            
            try:
                url, tab_name = save_results(df, strat['role_title'])
                send_email(email, df, url, strat['role_title'])
                
                status.update(label="✅ Done!", state="complete", expanded=False)
                st.success(f"Results saved to tab: {tab_name}")
                st.markdown(f"**[Open Database]({url})**")
                st.dataframe(df[['AI Score', 'Name', 'Reason']].head())
                
            except Exception as e:
                status.update(label="❌ Error", state="error")
                st.error(f"Error saving data: {e}")
        else:
            st.warning("No results found.")