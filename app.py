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

def create_and_fill_sheet(df, user_email):
    """
    Creates a NEW sheet with a timestamp, shares it with the user, 
    and fills it with data. Returns the URL of the new sheet.
    """
    try:
        client = get_sheet_connection()
        
        # 1. Generate unique filename with timestamp
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M")
        filename = f"Search Results - {timestamp}"
        
        # 2. Create the new spreadsheet (It lives in the Bot's Drive initially)
        sh = client.create(filename)
        
        # 3. CRITICAL: Share it with the human user so they can see it
        # We use the email entered in the form
        sh.share(user_email, perm_type='user', role='writer')
        
        # 4. Write Data
        worksheet = sh.sheet1
        # Add Headers first
        worksheet.append_row(['Name', 'Link', 'Snippet'])
        # Add Data
        worksheet.append_rows(df.values.tolist())
        
        return sh.url, filename
        
    except Exception as e:
        st.error(f"Sheet Creation Error: {e}")
        return None, None

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

def send_summary_email(user_email, df, sheet_url):
    msg = MIMEMultipart()
    msg['Subject'] = f"New Candidates Found ({len(df)})"
    msg['From'] = GMAIL_USER
    msg['To'] = user_email

    html_table = df.to_html(index=False, border=0, justify="left")
    
    body = f"""
    <h3>Passive Candidate Report</h3>
    <p>We found {len(df)} profiles.</p>
    
    <p><strong>📂 Access your new Spreadsheet here:</strong><br>
    <a href="{sheet_url}">{sheet_url}</a></p>
    
    <hr>
    <h4>Preview:</h4>
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
    st.write("This will create a brand new Google Sheet for every search.")
    job_keywords = st.text_input("Job Keywords", "Sales Director Chicago")
    recipient_email = st.text_input("Email Report To (Must be a Google Account)", "your_email@gmail.com")
    submitted = st.form_submit_button("Run Search")

if submitted:
    st.info(f"Searching for: {job_keywords}...")
    
    # 1. Search
    xray_query = f'site:linkedin.com/in/ {job_keywords}'
    candidates = search_google(xray_query, num_results=10)
    
    if candidates:
        df = pd.DataFrame(candidates)
        st.success(f"Found {len(candidates)} profiles!")
        st.dataframe(df)
        
        # 2. Create & Save to NEW Sheet
        with st.spinner('Creating new Google Sheet...'):
            sheet_url, sheet_name = create_and_fill_sheet(df, recipient_email)
            
            if sheet_url:
                st.success(f"✅ Created new sheet: '{sheet_name}'")
                st.markdown(f"[Open Google Sheet]({sheet_url})")

                # 3. Email with Link
                with st.spinner('Sending Email...'):
                    if send_summary_email(recipient_email, df, sheet_url):
                        st.success(f"✅ Email sent to {recipient_email}")
                    else:
                        st.error("❌ Email failed.")
            else:
                st.error("Could not create sheet. Check permissions.")
    else:
        st.warning("No results found. Try broader keywords.")