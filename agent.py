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
DRIVE_FOLDER_ID = st.secrets["DRIVE_FOLDER_ID"]
SHEET_SCOPE = ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"]

# Initialize OpenAI
client_ai = OpenAI(api_key=OPENAI_API_KEY)

# --- DATABASE MANAGEMENT FUNCTIONS (THE NEW PART) ---

def get_gspread_client():
    creds_dict = dict(st.secrets["SHEET_CREDENTIALS"])
    creds = ServiceAccountCredentials.from_json_keyfile_dict(creds_dict, SHEET_SCOPE)
    return gspread.authorize(creds)

def get_or_create_user_db(user_email):
    """
    Checks the 'User_Registry' to see if this user already has a DB.
    If not, creates a NEW file in the Shared Drive and registers it.
    """
    client = get_gspread_client()
    
    # 1. Connect to the Registry Sheet
    # We look for a file named "User_Registry" inside the specific folder
    try:
        # Note: Ideally, pass the Registry ID directly in secrets for speed, 
        # but searching by name works if it's unique in that folder.
        registry_file = client.open("User_Registry")
        registry_sheet = registry_file.sheet1
    except Exception as e:
        st.error(f"Critical Error: Could not find 'User_Registry' sheet in the folder. Please create it first. ({e})")
        return None

    # 2. Check if user exists
    records = registry_sheet.get_all_records()
    user_map = {row['User Email']: row for row in records}
    
    if user_email in user_map:
        # User Found! Return their existing Sheet
        sheet_id = user_map[user_email]['Sheet ID']
        try:
            return client.open_by_key(sheet_id)
        except:
            st.warning("Found your ID in registry, but couldn't open the file. Creating a new one...")
    
    # 3. User Not Found (or broken) -> Create New One
    st.info(f"🆕 First time for {user_email}? Setting up your private database...")
    
    # We use the Drive API directly to create a file INSIDE the Shared Drive Folder
    # This bypasses the Service Account "0GB Quota" issue (because the Shared Drive owns it)
    drive_service = build('drive', 'v3', developerKey=GOOGLE_API_KEY, credentials=client.auth)
    
    file_metadata = {
        'name': f"Candidate DB - {user_email}",
        'parents': [DRIVE_FOLDER_ID], # Puts it in the Shared Drive
        'mimeType': 'application/vnd.google-apps.spreadsheet'
    }
    
    file = drive_service.files().create(body=file_metadata, fields='id, webViewLink').execute()
    new_sheet_id = file.get('id')
    new_sheet_url = file.get('webViewLink')
    
    # 4. Share it with the User
    # We grant them "Writer" access so they own their data
    try:
        batch = drive_service.permissions().create(
            fileId=new_sheet_id,
            body={'type': 'user', 'role': 'writer', 'emailAddress': user_email},
            fields='id',
        ).execute()
    except Exception as e:
        st.warning(f"Created DB but failed to share: {e}")

    # 5. Initialize the new sheet with headers
    new_sh = client.open_by_key(new_sheet_id)
    # (Optional: Setup a 'Master' tab if you want)

    # 6. Register the new user
    registry_sheet.append_row([user_email, new_sheet_id, new_sheet_url])
    
    return new_sh

# --- AI & SEARCH FUNCTIONS (SAME AS BEFORE) ---

def generate_search_strategy(jd_text, location, work_style, model_choice):
    prompt = f"""
    You are an expert Sourcer. Create a search strategy.
    
    JOB CONTEXT:
    - Role Description: {jd_text[:3000]}
    - Target Location: {location}
    - Work Style: {work_style}
    
    TASK:
    Generate 3 distinct Google X-Ray Boolean strings.
    1. LinkedIn: Include location keywords if 'Onsite' or 'Hybrid'.
    2. Niche (GitHub/StackOverflow): Targeted at domain + Location.
    3. Resume File Search: Uploaded CVs.
    
    Output JSON with keys: 'role_title', 'boolean_strings' (list of 3).
    """
    try:
        response = client_ai.chat.completions.create(
            model=model_choice,
            response_format={"type": "json_object"},
            messages=[{"role": "system", "content": "Output valid JSON only."},
                      {"role": "user", "content": prompt}]
        )
        return json.loads(response.choices[0].message.content)
    except Exception as e:
        st.error(f"Strategy Error: {e}")
        return None

def ai_score_candidate(candidate_snippet, jd_summary, location, work_style, model_choice):
    prompt = f"""
    You are a Hiring Manager. Evaluate this candidate snippet.
    
    ROLE: {jd_summary}
    REQ LOCATION: {location}
    REQ WORK STYLE: {work_style}
    SNIPPET: {candidate_snippet}
    
    RULES:
    1. Role Mismatch: If Role is Engineer and candidate is Recruiter, SCORE 0.
    2. Location: If Onsite/Hybrid, strictly penalize mismatch.
    
    Output JSON:
    {{
        "score": (integer 0-100),
        "reason": (1 short sentence),
        "flag": (string: "Strong Match", "Location Mismatch", "Role Mismatch", "Too Senior/Junior")
    }}
    """
    try:
        response = client_ai.chat.completions.create(
            model=model_choice,
            response_format={"type": "json_object"},
            messages=[{"role": "user", "content": prompt}]
        )
        return json.loads(response.choices[0].message.content)
    except:
        return {"score": 0, "reason": "AI Error", "flag": "Unknown"}

def search_google(queries):
    service = build("customsearch", "v1", developerKey=GOOGLE_API_KEY)
    all_results = []
    for q in queries:
        try:
            res = service.cse().list(q=q, cx=SEARCH_ENGINE_ID, num=10).execute()
            for item in res.get('items', []):
                title_parts = item['title'].split("-")
                name = title_parts[0].strip() if len(title_parts) > 0 else "Unknown"
                if not any(d['Link'] == item['link'] for d in all_results):
                    all_results.append({'Name': name, 'Link': item['link'], 'Snippet': item['snippet']})
        except: pass
    return all_results

def create_tab_and_fill(user_sheet, df, role_name):
    try:
        timestamp = datetime.now().strftime("%m-%d %H:%M")
        short_role = (role_name[:15] + '..') if len(role_name) > 15 else role_name
        tab_title = f"{timestamp} - {short_role}"
        
        df = df.sort_values(by='AI Score', ascending=False)
        
        worksheet = user_sheet.add_worksheet(title=tab_title, rows=30, cols=10)
        worksheet.append_row(['AI Score', 'Name', 'Location/Flag', 'Reason', 'Link', 'Snippet'])
        
        data = df[['AI Score', 'Name', 'Flag', 'Reason', 'Link', 'Snippet']].values.tolist()
        worksheet.append_rows(data)
        
        return True, f"{user_sheet.url}#gid={worksheet.id}", tab_title
    except Exception as e:
        st.error(f"Tab Creation Error: {e}")
        return False, None, None

def send_summary_email(user_email, df, sheet_url, role_name, model_used):
    top_candidates = df.sort_values(by='AI Score', ascending=False).head(5)
    
    msg = MIMEMultipart()
    msg['Subject'] = f"AI Agent Report: {role_name}"
    msg['From'] = GMAIL_USER
    msg['To'] = user_email

    html_table = top_candidates[['AI Score', 'Name', 'Reason']].to_html(index=False, border=0, justify="left")
    
    body = f"""
    <h3>Talent Agent Report</h3>
    <p><strong>Role:</strong> {role_name}</p>
    <p><strong>AI Model:</strong> {model_used}</p>
    <p><strong>📂 Your Private Database:</strong> <a href="{sheet_url}">Open Google Sheet</a></p>
    
    <h4>Top 5 Matches:</h4>
    {html_table}
    """
    msg.attach(MIMEText(body, 'html'))

    try:
        with smtplib.SMTP_SSL('smtp.gmail.com', 465) as server:
            server.login(GMAIL_USER, GMAIL_APP_PASSWORD)
            server.send_message(msg)
        return True
    except Exception as e:
        st.error(f"Email Error: {e}")
        return False

# --- MAIN APP UI ---
st.set_page_config(page_title="AI Talent Agent", page_icon="🤖", layout="wide")

with st.sidebar:
    st.header("⚙️ Settings")
    model_option = st.radio("AI Brain:", ["gpt-4o", "gpt-4o-mini"], index=0)

st.title("🤖 AI Talent Agent")
st.markdown("Automated sourcing with your own private database.")

with st.form("agent_form"):
    c1, c2 = st.columns(2)
    with c1:
        # This email is now the KEY to their database
        recipient_email = st.text_input("Your Email (Google Account)", "judd@sharphuman.com")
    with c2:
        # We don't need Sheet Name input anymore! The bot manages it.
        st.info("ℹ️ We will automatically load (or create) your personal candidate database.")
    
    c3, c4 = st.columns(2)
    with c3:
        loc_input = st.text_input("📍 Location", placeholder="e.g. Nashville, USA")
    with c4:
        style_input = st.text_input("🏢 Work Style", placeholder="e.g. Remote, Hybrid")

    jd_input = st.text_area("Paste Job Description Here", height=200)
    
    submitted = st.form_submit_button("Launch Agent")

if submitted and jd_input:
    status = st.status("Agent is starting...", expanded=True)
    
    # 1. SETUP DB
    status.write(f"📂 Accessing database for **{recipient_email}**...")
    user_sheet = get_or_create_user_db(recipient_email)
    
    if user_sheet:
        status.write(f"🧠 Analyzing JD with **{model_option}**...")
        strategy = generate_search_strategy(jd_input, loc_input, style_input, model_option)
        
        if strategy:
            role_title = strategy.get('role_title', 'Candidate Search')
            queries = strategy.get('boolean_strings', [])
            
            status.write(f"🔎 Role: **{role_title}**")
            status.write(f"🌐 Running {len(queries)} searches...")
            
            raw_candidates = search_google(queries)
            
            if raw_candidates:
                status.write(f"👀 Scoring {len(raw_candidates)} profiles...")
                
                scored_data = []
                progress_bar = status.progress(0)
                
                for i, cand in enumerate(raw_candidates):
                    progress_bar.progress((i + 1) / len(raw_candidates))
                    ai_result = ai_score_candidate(cand['Snippet'], role_title, loc_input, style_input, model_option)
                    cand['AI Score'] = ai_result.get('score', 0)
                    cand['Reason'] = ai_result.get('reason', 'N/A')
                    cand['Flag'] = ai_result.get('flag', '')
                    scored_data.append(cand)
                    
                df = pd.DataFrame(scored_data)
                df = df[df['AI Score'] > 10]
                
                status.write("💾 Saving to your Private Sheet...")
                success, tab_url, tab_name = create_tab_and_fill(user_sheet, df, role_title)
                
                if success:
                    send_summary_email(recipient_email, df, tab_url, role_title, model_option)
                    status.update(label="✅ Success!", state="complete", expanded=False)
                    st.success(f"Report sent to {recipient_email}")
                    st.markdown(f"**[Open Your Database]({tab_url})**")
                    st.dataframe(df[['AI Score', 'Name', 'Reason']].head(5))
                else:
                    st.error("Error saving to sheet.")
            else:
                st.warning("No candidates found.")
    else:
        st.error("Could not setup database. Check Shared Drive permissions.")