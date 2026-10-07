import os
import json
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload
from google.oauth2.credentials import Credentials

SCOPES = ["https://www.googleapis.com/auth/youtube.upload"]

def get_creds():
    client_secret = json.loads(os.environ["CLIENT_SECRET_JSON"])
    return Credentials(
        None,
        refresh_token=os.environ["REFRESH_TOKEN"],
        token_uri="https://oauth2.googleapis.com/token",
        client_id=client_secret["installed"]["client_id"],
        client_secret=client_secret["installed"]["client_secret"],
        scopes=SCOPES,
    )

def upload_video(path, title, description, tags):
    creds = get_creds()
    youtube = build("youtube", "v3", credentials=creds)

    body = {
        "snippet": {
            "title": title,
            "description": description,
            "tags": tags,
            "categoryId": "24",
        },
        "status": {
            "privacyStatus": "public",
            "selfDeclaredMadeForKids": False,
        },
    }

    media = MediaFileUpload(path, chunksize=-1, resumable=True)
    request = youtube.videos().insert(
        part="snippet,status",
        body=body,
        media_body=media,
    )
    response = request.execute()
    print("Uploaded video ID:", response.get("id"))
