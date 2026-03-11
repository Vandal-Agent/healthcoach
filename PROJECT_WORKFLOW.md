HealthCoach Bot Workflow

SERVER
DigitalOcean Ubuntu server
User: vandal

PROJECT LOCATION
/home/vandal/bots/healthcoach

SERVICE
systemd service

healthcoach.service

Restart service
sudo systemctl restart healthcoach

Check status
sudo systemctl status healthcoach

View logs
sudo journalctl -u healthcoach -f


MAIN FILES

app.py
Main Flask webhook server and Telegram bot logic

loseit_email_reader.py
Reads Lose It summary emails and downloads CSV attachment

loseit_parser.py
Parses Lose It CSV into totals and meal breakdown

loseit_coaching.py
Creates daily food coaching message

recipe_library.json
Library of recurring foods and recipe context


DATA FLOW

iPhone Shortcut
↓
Webhook POST
↓
Flask server
↓
Google Sheets (Health Tracker)
↓
Telegram messages

Lose It Email
↓
IMAP reader
↓
CSV download
↓
Food parser
↓
Food coaching message


GITHUB BACKUP

After ANY code change:

cd ~/bots/healthcoach
git add .
git commit -m "describe change"
git push
