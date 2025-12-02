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
# Load all secrets
GOOGLE_API_KEY = st.secrets["GOOGLE_API_KEY"]
SEARCH_ENGINE_ID = st.secrets["SEARCH_ENGINE_ID"]
GMAIL_USER = st.secrets["GMAIL_USER"]
GMAIL_APP_PASSWORD = st.secrets["GMAIL_APP_PASSWORD"]
OPENAI_API_KEY = st.secrets["OPENAI_API_KEY"]
SHEET_SCOPE = ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"]

# Initialize OpenAI Client
client_ai = OpenAI(api_key=OPENAI_API_KEY)

# --- AI AGENT FUNCTIONS ---

def generate_search_strategy(jd_text):
    """
    Uses GPT-4o to analyze the JD and create 3 distinct boolean search strings.
    """
    prompt = f"""
    You are an expert Technical Recruiter / Boolean Search Sourcer. 
    Analyze this Job Description and generate 3 distinct Google X-Ray Boolean strings to find passive candidates.
    
    1. A broad LinkedIn search (site:linkedin.com/in/) focusing on title and key skills.
    2. A niche platform search (site:github.com OR site:stackoverflow.com OR site:behance.net) relevant to the role type.
    3. A 'Resume/CV' file search (filetype:pdf OR filetype:doc) for uploaded resumes.
    
    JOB DESCRIPTION:
    {jd_text[:2000]}
    
    Output strictly valid JSON with keys: 'role_title', 'boolean_strings' (a list of 3 strings).
    """
    
    try:
        response = client_ai.chat.completions.create(
            model="gpt-4o",
            response_format={"type": "json_object"},
            messages=[{"role": "system", "content": "You are a helpful sourcing assistant. Output JSON."},
                      {"role": "user", "content": prompt}]
        )
        data = json.loads(response.choices[0].message.content)
        return data
    except Exception as e:
        st.error(f"AI Strategy Error: {e}")
        return None

def ai_score_candidate(candidate_snippet, jd_summary):
    """
    Uses GPT-4o-mini (Cheaper/Faster) to score a single candidate snippet against the JD.
    """
    prompt = f"""
    You are a Hiring Manager. 
    Evaluate this candidate snippet against the role requirements.
    
    ROLE: {jd_summary}
    CANDIDATE SNIPPET: {candidate_snippet}
    
    Output JSON:
    {{
        "score": (integer 0-100),
        "reason": (1 short sentence explaining the match or mismatch),
        "flag": (string: "Remote", "Senior", "Junior", or "Unknown")
    }}
    """
    try:
        response = client_ai.chat.completions.create(
            model="gpt-4o-mini", # Using mini for speed/cost on loops
            response_format={"type": "json_object"},
            messages=[{"role": "user", "content": prompt}]
        )
        return json.loads(response.choices[0].message.content)
    except:
        return {"score": 0, "reason": "AI Error", "flag": "Unknown"}

# --- GOOGLE & SHEETS FUNCTIONS ---

def get_sheet_connection():
    creds_dict = dict(st.secrets["SHEET_CREDENTIALS"])
    creds = ServiceAccountCredentials.from_json_keyfile_dict(creds_dict, SHEET_SCOPE)
    return gspread.authorize(creds)

def create_tab_and_fill(df, role_name, sheet_name):
    try:
        client = get_sheet_connection()
        sh = client.open(sheet_name)
        
        timestamp = datetime.now().strftime("%m-%d %H:%M")
        short_role = (role_name[:15] + '..') if len(role_name) > 15 else role_name
        tab_title = f"{timestamp} - {short_role}"
        
        # Sort by Score (High to Low)
        df = df.sort_values(by='AI Score', ascending=False)
        
        worksheet = sh.add_worksheet(title=tab_title, rows=30, cols=10)
        worksheet.append_row(['AI Score', 'Name', 'Reason', 'Flag', 'Link', 'Snippet'])
        
        # Select specific columns
        data = df[['AI Score', 'Name', 'Reason', 'Flag', 'Link', 'Snippet']].values.tolist()
        worksheet.append_rows(data)
        
        return True, f"{sh.url}#gid={worksheet.id}", tab_title
    except Exception as e:
        st.error(f"Tab Creation Error: {e}")
        return False, None, None

def search_google(queries):
    service = build("customsearch", "v1", developerKey=GOOGLE_API_KEY)
    all_results = []
    
    for q in queries:
        try:
            # We fetch 10 results per query string (Total 30 potential)
            res = service.cse().list(q=q, cx=SEARCH_ENGINE_ID, num=10).execute()
            for item in res.get('items', []):
                title_parts = item['title'].split("-")
                name = title_parts[0].strip() if len(title_parts) > 0 else "Unknown"
                
                # Deduplicate by link
                if not any(d['Link'] == item['link'] for d in all_results):
                    all_results.append({
                        'Name': name,
                        'Link': item['link'],
                        'Snippet': item['snippet']
                    })
        except Exception as e:
            st.write(f"Search warning for query '{q}': {e}")
            
    return all_results

def send_summary_email(user_email, df, sheet_url, role_name):
    # Filter top 5 for the email body
    top_candidates = df.sort_values(by='AI Score', ascending=False).head(5)
    
    msg = MIMEMultipart()
    msg['Subject'] = f"AI Agent Report: {role_name}"
    msg['From'] = GMAIL_USER
    msg['To'] = user_email

    html_table = top_candidates[['AI Score', 'Name', 'Reason', 'Link']].to_html(index=False, border=0, justify="left")
    
    body = f"""
    <h3>Talent Agent Report</h3>
    <p>I analyzed the web for <strong>{role_name}</strong>.</p>
    <p><strong>📂 Full Analysis (All Candidates):</strong> <a href="{sheet_url}">Open Google Sheet</a></p>
    
    <h4>Top 5 AI-Ranked Matches:</h4>
    {html_table}
    <br>
    <p><i>Scores generated by GPT-4o based on Job Description analysis.</i></p>
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
st.set_page_config(page_title="AI Talent Agent", page_icon="🤖")
st.title("🤖 AI Talent Agent")
st.markdown("Paste a Job Description. I will design the search, find candidates across the web, and score them for you.")

with st.form("agent_form"):
    # Input is now a big text area for the JD
    jd_input = st.text_area("Paste Job Description Here", height=200)
    recipient_email = st.text_input("Email Report To", "judd@sharphuman.com")
    master_sheet_name = st.text_input("Master Sheet Name", "Candidate Database")
    
    submitted = st.form_submit_button("Launch Agent")

if submitted and jd_input:
    status = st.status("Agent is working...", expanded=True)
    
    # 1. AI Strategy
    status.write("🧠 Reading JD and generating search strategy...")
    strategy = generate_search_strategy(jd_input)
    
    if strategy:
        role_title = strategy.get('role_title', 'Candidate Search')
        queries = strategy.get('boolean_strings', [])
        
        status.write(f"🔎 Identified Role: **{role_title}**")
        status.write(f"🌐 Running {len(queries)} unique boolean searches across the web...")
        
        # 2. Search Google
        raw_candidates = search_google(queries)
        status.write(f"👀 Found {len(raw_candidates)} raw profiles. Now reading and scoring...")
        
        # 3. AI Scoring Loop
        scored_data = []
        progress_bar = status.progress(0)
        
        for i, cand in enumerate(raw_candidates):
            # Update progress
            progress_bar.progress((i + 1) / len(raw_candidates))
            
            # AI Scoring
            ai_result = ai_score_candidate(cand['Snippet'], role_title)
            
            cand['AI Score'] = ai_result.get('score', 0)
            cand['Reason'] = ai_result.get('reason', 'N/A')
            cand['Flag'] = ai_result.get('flag', '')
            scored_data.append(cand)
            
        df = pd.DataFrame(scored_data)
        
        # Filter out bad matches (below 40%) to keep list clean
        df = df[df['AI Score'] > 40]
        
        status.write("💾 Saving Top Candidates to Google Drive...")
        
        # 4. Save & Email
        success, tab_url, tab_name = create_tab_and_fill(df, role_title, master_sheet_name)
        
        if success:
            send_summary_email(recipient_email, df, tab_url, role_title)
            status.update(label="✅ Mission Complete!", state="complete", expanded=False)
            
            st.success(f"Report generated for {role_title}!")
            st.markdown(f"**[View Google Sheet Results]({tab_url})**")
            st.dataframe(df[['AI Score', 'Name', 'Reason', 'Link']].head(10))
            
        else:
            status.update(label="❌ Error Saving Data", state="error")
    else:
        st.error("Could not parse JD. Please try again.")