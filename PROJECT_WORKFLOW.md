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


DATABASE BACKUPS

HealthCoach stores persistent SQLite data in:

data/healthcoach_food.db
data/healthcoach_memory.db

scripts/backup_databases.py creates online SQLite snapshots while HealthCoach
continues running. Every new backup must pass SQLite integrity_check and contain
application tables before it is published.

Verified backups are written to:

backups/databases/

The default retention is 14 successful generations per database. Old backups
are pruned only after all databases in the current run were copied and verified.

Run and verify a backup manually:

cd /home/vandal/bots/healthcoach
source /home/vandal/bots/healthcoach/venv/bin/activate
python scripts/backup_databases.py

Run the automated tests:

python -m unittest discover -s tests -v

The daily schedule is defined in:

ops/systemd/healthcoach-database-backup.service
ops/systemd/healthcoach-database-backup.timer

Install or refresh the timer:

sudo cp ops/systemd/healthcoach-database-backup.service /etc/systemd/system/
sudo cp ops/systemd/healthcoach-database-backup.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now healthcoach-database-backup.timer

Check the timer and its latest run:

systemctl status healthcoach-database-backup.timer --no-pager
systemctl list-timers healthcoach-database-backup.timer --no-pager
journalctl -u healthcoach-database-backup.service -n 50 --no-pager

Test the installed service immediately:

sudo systemctl start healthcoach-database-backup.service
systemctl status healthcoach-database-backup.service --no-pager
find backups/databases -maxdepth 1 -type f -name '*.db' -printf '%f\n' | sort


GITHUB BACKUP

After ANY code change:

cd ~/bots/healthcoach
git add .
git commit -m "describe change"
git push
