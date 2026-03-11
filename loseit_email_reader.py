import os
import imaplib
import email
from dotenv import load_dotenv

load_dotenv("/home/vandal/.env")

SAVE_PATH = "/home/vandal/bots/healthcoach/data/latest_loseit.csv"

def download_latest_loseit_csv():
    username = os.getenv("GMAIL_USER")
    password = os.getenv("GMAIL_APP_PASSWORD")

    mail = imaplib.IMAP4_SSL("imap.gmail.com")
    mail.login(username, password)
    mail.select("inbox")

    # search for Lose It daily summaries
    status, messages = mail.search(None, '(FROM "Lose It!" SUBJECT "Daily Summary")')

    if status != "OK":
        mail.logout()
        return None

    message_ids = messages[0].split()

    if not message_ids:
        mail.logout()
        return None

    # get newest email
    latest_id = message_ids[-1]

    status, data = mail.fetch(latest_id, "(RFC822)")
    if status != "OK":
        mail.logout()
        return None

    raw_email = data[0][1]
    msg = email.message_from_bytes(raw_email)

    for part in msg.walk():
        content_disposition = str(part.get("Content-Disposition"))

        if "attachment" in content_disposition:
            filename = part.get_filename()

            if filename and filename.lower().endswith(".csv"):
                os.makedirs(os.path.dirname(SAVE_PATH), exist_ok=True)

                with open(SAVE_PATH, "wb") as f:
                    f.write(part.get_payload(decode=True))

                mail.logout()
                return SAVE_PATH

    mail.logout()
    return None
