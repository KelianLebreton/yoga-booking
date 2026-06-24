import gspread
from google.oauth2.service_account import Credentials

scopes = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]

creds = Credentials.from_service_account_file("secrets/service-account.json", scopes=scopes)
client = gspread.authorize(creds)

sheet = client.open_by_key("1lUzrsPeMOS2ruPZkCP8l4b-u3eKFgRs_BecNlyDAuaM")
print(sheet.worksheets())  # doit lister tes onglets : Élèves, Crédits, etc.