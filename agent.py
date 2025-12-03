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

# THE MASTER SHEET ID
MASTER_SHEET_ID = "14x4FW2Zsbj9g-j5bGt12l5SsK11fWEf94i0t1HxAnas"

client_ai = OpenAI(api_key=OPENAI_API_KEY)

# --- GOOGLE SHEETS FUNCTIONS ---

def get_gspread_client():
    creds_dict = dict(st.secrets["SHEET_CREDENTIALS"])
    creds = ServiceAccountCredentials.from_json_keyfile_dict(creds_dict, SHEET_SCOPE)
    return gspread.authorize(creds)

def save_results(df, role_name):
    client = get_gspread_client()
    try:
        sh = client.open_by_key(MASTER_SHEET_ID)
    except Exception as e:
        st.error(f"❌ Permission Error: Please share your sheet with: {st.secrets['SHEET_CREDENTIALS']['client_email']}")
        st.stop()

    timestamp = datetime.now().strftime("%m-%d %H:%M")
    short_role = (role_name[:15] + '..') if len(role_name) > 15 else role_name
    title = f"{timestamp} - {short_role}"
    
    # Create new tab
    try:
        ws = sh.add_worksheet(title=title, rows=20, cols=10)
    except:
        title = f"{title} ({datetime.now().second})"
        ws = sh.add_worksheet(title=title, rows=20, cols=10)

    ws.append_row(['AI Score', 'Name', 'Reason', 'Link'])
    ws.append_rows(df[['AI Score', 'Name', 'Reason', 'Link']].values.tolist())
    
    return f"{sh.url}#gid={ws.id}", title

# --- AI & SEARCH FUNCTIONS ---

def generate_search_strategy(jd_text, location, work_style, model_choice):
    prompt = f"JOB: {jd_text[:3000]} | LOC: {location} | STYLE: {work_style}. Gen 3 Boolean strings. Output JSON: 'role_title', 'boolean_strings'."
    try:
        res = client_ai.chat.completions.create(model=model_choice, response_format={"type": "json_object"}, messages=[{"role": "user", "content": prompt}])
        return json.loads(res.choices[0].message.content)
    except: return None

def ai_score_candidate(snippet, role, loc, style, model):
    prompt = f"ROLE: {role} | LOC: {loc} | STYLE: {style} | CANDIDATE: {snippet}. Score 0-100. Output JSON: 'score', 'reason', 'flag'."
    try:
        res = client_ai.chat.completions.create(model=model, response_format={"type": "json_object"}, messages=[{"role": "user", "content": prompt}])
        return json.loads(res.choices[0].message.content)
    except: return {"score": 0, "reason": "Error", "flag": "Unknown"}

def search_google(queries):
    service = build("customsearch", "v1", developerKey=GOOGLE_API_KEY)
    results = []
    for q in queries:
        try:
            res = service.cse().list(q=q, cx=SEARCH_ENGINE_ID, num=10).execute()
            for item in res.get('items', []):
                results.append({'Name': item['title'], 'Link': item['link'], 'Snippet': item['snippet']})
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
    email = st.text_input("Send Report To", "judd@sharphuman.com")
    c1, c2 = st.columns(2)
    with c1: loc = st.text_input("Location")
    with c2: style = st.text_input("Work Style")
    jd = st.text_area("Job Description")
    submitted = st.form_submit_button("Run Agent")

if submitted and jd:
    status = st.status("Agent is working...", expanded=True)
    status.write("🧠 Strategy...")
    strat = generate_search_strategy(jd, loc, style, model)
    
    if strat:
        status.write(f"🔎 {strat['role_title']}")
        res = search_google(strat['boolean_strings'])
        if res:
            status.write("👀 Scoring...")
            scored = []
            for r in res:
                s = ai_score_candidate(r['Snippet'], strat['role_title'], loc, style, model)
                r['AI Score'] = s.get('score', 0)
                r['Reason'] = s.get('reason', '')
                scored.append(r)
            
            df = pd.DataFrame(scored)
            df = df[df['AI Score'] > 10].sort_values(by='AI Score', ascending=False)
            
            status.write("💾 Saving...")
            url, tab = save_results(df, strat['role_title'])
            send_email(email, df, url, strat['role_title'])
            
            status.update(label="✅ Done!", state="complete")
            st.success(f"Saved to: {tab}")
            st.markdown(f"**[Open Database]({url})**")
            
            # --- FIX FOR "FULL LINK OR NAME" ---
            # This makes the Link column a clickable button so it's not cut off
            st.dataframe(
                df[['AI Score', 'Name', 'Reason', 'Link']],
                column_config={
                    "Link": st.column_config.LinkColumn("Profile Link"),
                },
                hide_index=True
            )
        else:
            st.warning("No results.")