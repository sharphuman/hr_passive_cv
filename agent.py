import streamlit as st
import pandas as pd
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from email.mime.application import MIMEApplication
from googleapiclient.discovery import build
import gspread
from oauth2client.service_account import ServiceAccountCredentials
from datetime import datetime
from openai import OpenAI
import json
import io

# --- CONFIGURATION ---
GOOGLE_API_KEY = st.secrets["GOOGLE_API_KEY"]
SEARCH_ENGINE_ID = st.secrets["SEARCH_ENGINE_ID"]
GMAIL_USER = st.secrets["GMAIL_USER"]
GMAIL_APP_PASSWORD = st.secrets["GMAIL_APP_PASSWORD"]
OPENAI_API_KEY = st.secrets["OPENAI_API_KEY"]
SHEET_SCOPE = ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"]

# 1. GET THIS ID FROM YOUR SHARED DRIVE URL (Recruiter_Bot_Data folder)
# Example: drive.google.com/drive/folders/1NStKbjKN54Uk9PVA...
DRIVE_FOLDER_ID = "0ANxStKbjKN54Uk9PVA"  # <--- UPDATE THIS IF DIFFERENT

client_ai = OpenAI(api_key=OPENAI_API_KEY)

# --- HELPER: SEARCH & FILTER ---

def generate_search_strategy(jd_text, location, work_style, model_choice):
    prompt = f"""
    JOB: {jd_text[:3000]} | LOC: {location} | STYLE: {work_style}
    
    Task: Generate 3 strict X-Ray Boolean strings to find specific profiles (NOT job posts).
    1. LinkedIn: Must use site:linkedin.com/in/ AND title keywords.
    2. Niche: site:github.com OR site:stackoverflow.com (if tech).
    3. Resume: filetype:pdf OR filetype:doc "Resume" OR "CV".
    
    Output JSON keys: 'role_title', 'boolean_strings' (list).
    """
    try:
        response = client_ai.chat.completions.create(
            model=model_choice, response_format={"type": "json_object"},
            messages=[{"role": "user", "content": prompt}]
        )
        return json.loads(response.choices[0].message.content)
    except: return None

def search_google(queries):
    service = build("customsearch", "v1", developerKey=GOOGLE_API_KEY)
    results = []
    seen_links = set()
    
    # JUNK FILTER: Ignore these words in titles/links
    bad_words = ["log in", "sign up", "login", "signup", "job", "career", "hiring", "directory", "articles", "pulse"]
    
    for q in queries:
        try:
            res = service.cse().list(q=q, cx=SEARCH_ENGINE_ID, num=10).execute()
            for item in res.get('items', []):
                link = item['link']
                title = item['title']
                snippet = item['snippet']
                
                # 1. Deduplicate
                if link in seen_links: continue
                
                # 2. Strict Filter: Must look like a profile
                # If it's linkedin, it MUST have /in/
                if "linkedin.com" in link and "/in/" not in link: continue
                
                # 3. Junk Filter
                if any(bad in title.lower() for bad in bad_words): continue
                if any(bad in link.lower() for bad in bad_words): continue

                seen_links.add(link)
                results.append({'Name': title.split("|")[0].split("-")[0].strip(), 'Link': link, 'Snippet': snippet})
        except: pass
    return results

def ai_score_candidate(snippet, role, loc, style, model):
    prompt = f"ROLE: {role} | LOC: {loc} | STYLE: {style} | CANDIDATE: {snippet}. Score 0-100. Output JSON: 'score', 'reason', 'flag'."
    try:
        res = client_ai.chat.completions.create(model=model, response_format={"type": "json_object"}, messages=[{"role": "user", "content": prompt}])
        return json.loads(res.choices[0].message.content)
    except: return {"score": 0, "reason": "Error", "flag": "Unknown"}

# --- HELPER: SAVE & EMAIL ---

def save_to_shared_drive(df, role_name, user_email):
    """
    Creates a new sheet inside the Shared Drive Folder (DRIVE_FOLDER_ID)
    """
    creds_dict = dict(st.secrets["SHEET_CREDENTIALS"])
    creds = ServiceAccountCredentials.from_json_keyfile_dict(creds_dict, SHEET_SCOPE)
    client = gspread.authorize(creds)
    
    # 1. Use Drive API to create file inside the Folder
    drive_service = build('drive', 'v3', developerKey=GOOGLE_API_KEY, credentials=client.auth)
    
    timestamp = datetime.now().strftime("%Y-%m-%d %H-%M")
    filename = f"{timestamp} - {role_name[:15]} - {user_email}"
    
    file_metadata = {
        'name': filename,
        'parents': [DRIVE_FOLDER_ID], # <--- SAVES TO YOUR SHARED DRIVE
        'mimeType': 'application/vnd.google-apps.spreadsheet'
    }
    
    file = drive_service.files().create(body=file_metadata, fields='id, webViewLink').execute()
    sheet_id = file.get('id')
    sheet_url = file.get('webViewLink')
    
    # 2. Open and Write Data
    sh = client.open_by_key(sheet_id)
    ws = sh.sheet1
    ws.append_row(['AI Score', 'Name', 'Reason', 'Link', 'Snippet'])
    ws.append_rows(df[['AI Score', 'Name', 'Reason', 'Link', 'Snippet']].values.tolist())
    
    return sheet_url

def send_email_with_excel(email, df, sheet_url, role):
    msg = MIMEMultipart()
    msg['Subject'] = f"Candidates: {role}"
    msg['From'] = GMAIL_USER
    msg['To'] = email
    
    # 1. Body Text
    html = df.head(5)[['AI Score', 'Name', 'Reason']].to_html(index=False)
    body = f"""
    <h3>Search Results: {role}</h3>
    <p>Attached is the Excel file with your candidates.</p>
    <p><strong>📂 Archived in Shared Drive:</strong> <a href='{sheet_url}'>View Google Sheet</a></p>
    <hr>
    {html}
    """
    msg.attach(MIMEText(body, 'html'))
    
    # 2. Create Excel Attachment in Memory
    excel_buffer = io.BytesIO()
    with pd.ExcelWriter(excel_buffer, engine='openpyxl') as writer:
        df.to_excel(writer, index=False, sheet_name='Candidates')
    excel_data = excel_buffer.getvalue()

    # 3. Attach File
    part = MIMEApplication(excel_data, Name=f"{role[:10]}_Candidates.xlsx")
    part['Content-Disposition'] = f'attachment; filename="{role[:10]}_Candidates.xlsx"'
    msg.attach(part)
    
    # 4. Send
    with smtplib.SMTP_SSL('smtp.gmail.com', 465) as s:
        s.login(GMAIL_USER, GMAIL_APP_PASSWORD)
        s.send_message(msg)

# --- UI ---
st.set_page_config(page_title="AI Talent Agent", layout="wide")
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
            df = df[df['AI Score'] > 10].sort_values(by='AI Score', ascending=False)
            
            if not df.empty:
                status.write("💾 Saving & Attaching Excel...")
                try:
                    # Save to Shared Drive (Backup)
                    url = save_to_shared_drive(df, strat['role_title'], email)
                    # Email Excel Attachment
                    send_email_with_excel(email, df, url, strat['role_title'])
                    
                    status.update(label="✅ Sent!", state="complete")
                    st.success("Check your email! Excel file attached.")
                    st.dataframe(df.head())
                except Exception as e:
                    st.error(f"Error: {e}")
            else:
                st.warning("Candidates found but scores were too low.")
        else:
            st.warning("No valid profiles found (Junk filter active).")