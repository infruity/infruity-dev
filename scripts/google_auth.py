"""Buat secrets/token.json (OAuth Google) — jalankan SEKALI di laptop.

1. Google Cloud Console > APIs & Services:
   - Enable "Google Sheets API" dan "Google Drive API"
   - Credentials > Create OAuth client ID > Desktop app > download JSON
   - Simpan sebagai secrets/credentials.json
2. python scripts/google_auth.py
3. Login dengan akun yang punya akses edit ke sheet & folder Drive Infruity
4. Copy secrets/token.json ke server (folder secrets/ yang sama)
"""
from pathlib import Path

from google_auth_oauthlib.flow import InstalledAppFlow

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]
ROOT = Path(__file__).resolve().parent.parent
secrets = ROOT / "secrets"
flow = InstalledAppFlow.from_client_secrets_file(str(secrets / "credentials.json"), SCOPES)
creds = flow.run_local_server(port=0, prompt="consent", access_type="offline")
(secrets / "token.json").write_text(creds.to_json())
print("✅ Tersimpan:", secrets / "token.json")
