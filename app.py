import streamlit as st
import pandas as pd
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from googleapiclient.discovery import build
import gspread
from oauth2client.service_account import ServiceAccountCredentials
from datetime import datetime

# --- CONFIGURATION ---
GOOGLE_API_KEY = st.secrets["GOOGLE_API_KEY"]
SEARCH_ENGINE_ID = st.secrets["SEARCH_ENGINE_ID"]
GMAIL_USER = st.secrets["GMAIL_USER"]
GMAIL_APP_PASSWORD = st.secrets["GMAIL_APP_PASSWORD"]
SHEET_SCOPE = ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"]

# --- HELPER FUNCTIONS ---

def get_sheet_connection():
    """Connects to Google Sheets using the JSON credentials in secrets."""
    creds_dict = dict(st.secrets["SHEET_CREDENTIALS"])
    creds = ServiceAccountCredentials.from_json_keyfile_dict(creds_dict, SHEET_SCOPE)
    client = gspread.authorize(creds)
    return client

def create_tab_and_fill(df, search_term, sheet_name):
    """
    Creates a NEW TAB (Worksheet) inside the Master Sheet.
    """
    try:
        client = get_sheet_connection()
        # Open the Master Sheet (Owned by YOU, so no storage error)
        sh = client.open(sheet_name)
        
        # 1. Generate a unique name for the Tab (e.g. "12-02 Python Dev")
        # Keep it short (limit is 100 chars, but shorter is better for tabs)
        timestamp = datetime.now().strftime("%m-%d %H:%M")
        # Clean search term to keep tab name valid
        short_term = (search_term[:15] + '..') if len(search_term) > 15 else search_term
        tab_title = f"{timestamp} - {short_term}"
        
        # 2. Create the new Worksheet (Tab)
        # rows=20 is just initial; it expands automatically
        worksheet = sh.add_worksheet(title=tab_title, rows=20, cols=10)
        
        # 3. Add Headers & Data
        worksheet.append_row(['Name', 'Profile Link', 'Snippet'])
        
        # Prepare data (df to list)
        data = df[['Name', 'Link', 'Snippet']].values.tolist()
        worksheet.append_rows(data)
        
        # 4. Generate Link to this specific TAB
        # The URL needs the 'gid' (Grid ID) to open the specific tab
        tab_url = f"{sh.url}#gid={worksheet.id}"
        
        return True, tab_url, tab_title

    except Exception as e:
        st.error(f"Tab Creation Error: {e}")
        return False, None, None

def search_google(query, num_results=10):
    service = build("customsearch", "v1", developerKey=GOOGLE_API_KEY)
    results = []
    try:
        res = service.cse().list(q=query, cx=SEARCH_ENGINE_ID, num=num_results).execute()
        for item in res.get('items', []):
            title_parts = item['title'].split("-")
            name = title_parts[0].strip() if len(title_parts) > 0 else "Unknown"
            
            results.append({
                'Name': name,
                'Link': item['link'],
                'Snippet': item['snippet']
            })
    except Exception as e:
        st.error(f"Google Search Error: {e}")
    return results

def send_summary_email(user_email, df, sheet_url, tab_name):
    msg = MIMEMultipart()
    msg['Subject'] = f"Search Results: {tab_name}"
    msg['From'] = GMAIL_USER
    msg['To'] = user_email

    html_table = df.to_html(index=False, border=0, justify="left")
    
    body = f"""
    <h3>Passive Candidate Report</h3>
    <p>Found {len(df)} candidates.</p>
    
    <p><strong>📂 View Results in Tab: '{tab_name}'</strong><br>
    <a href="{sheet_url}" style="font-size:16px;">Click here to open the specific Tab</a></p>
    
    <hr>
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
st.title("🕵️ Passive Candidate Hunter")

with st.form("search_form"):
    st.info("This will create a NEW TAB in your Master Sheet for every search.")
    
    # Inputs
    job_keywords = st.text_input("Job Keywords", "Python Developer London")
    recipient_email = st.text_input("Email Report To", "judd@sharphuman.com")
    master_sheet_name = st.text_input("Master Sheet Name (Must Exist in Drive)", "Candidate Database")
    
    submitted = st.form_submit_button("Run Search")

if submitted:
    st.write(f"Searching for: **{job_keywords}**...")
    
    # 1. Search
    xray_query = f'site:linkedin.com/in/ {job_keywords}'
    candidates = search_google(xray_query, num_results=10)
    
    if candidates:
        df = pd.DataFrame(candidates)
        st.success(f"Found {len(candidates)} profiles!")
        st.dataframe(df)
        
        # 2. Create Tab
        with st.spinner('Creating new Tab in Sheet...'):
            success, tab_url, tab_name = create_tab_and_fill(df, job_keywords, master_sheet_name)
            
            if success:
                st.success(f"✅ Created Tab: {tab_name}")
                
                # 3. Email
                with st.spinner('Sending Email...'):
                    if send_summary_email(recipient_email, df, tab_url, tab_name):
                        st.success(f"✅ Email sent to {recipient_email}")
                    else:
                        st.error("❌ Email failed.")
            else:
                st.error("Could not write to sheet. Check that 'Candidate Database' exists and is shared with the bot.")
    else:
        st.warning("No results found. Try broader keywords.")
