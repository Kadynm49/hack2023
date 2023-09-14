import azure.functions as func
import youtube_dl
import openai
from azure.storage.blob.baseblobservice import BaseBlobService
from datetime import datetime, timedelta
import requests
from azure.storage.blob import BlobServiceClient, BlobClient, ContainerClient, BlobPermissions
import urllib.request

def summarize_text(podcast_transcript):
    openai.api_type = "azure"
    openai.api_base = os.getenv("AZURE_OPENAI_ENDPOINT")
    openai.api_version = "2023-05-15"
    openai.api_key = os.getenv("AZURE_OPENAI_KEY")

    response = openai.ChatCompletion.create(
        engine="gpt-35-turbo-16k",
        messages=[
            {"role": "system", "content": "You are a podcast summarizer. You are going to be given a transcript of a youtube podcast you will summarize the podcast in a list of the most important parts. You will have to assume when different members of the podcast are speaking using context clues. You can also determine if the podcast is entertaining or informative in nature to help you determine what is important."},
            {"role": "user", "content": podcast_transcript}
        ]
    )

def my_hook(d):
    if d['status'] == 'finished':
        print('Done downloading, now converting ...')

def download_video(url):
    ydl_opts = {
        'format': 'bestaudio/best',
        'postprocessors': [{
            'key': 'FFmpegExtractAudio',
            'preferredcodec': 'mp3',
            'preferredquality': '192',
        }],
        'progress_hooks': [my_hook],
        'outtmpl': 'audio/%(title)s.%(ext)s'
    }
    with youtube_dl.YoutubeDL(ydl_opts) as ydl:
        ydl.download([url])

def upload_audio_to_blob(audio_file_name):
    blob_service_client = BlobServiceClient.from_connection_string(STORAGE_CONNECTION_STRING)

    # First check to see if the mp3s container exists
    container = blob_service_client.get_container_client("mp3s")
    if not container.exists():
        # Create the container if it doesn't exist
        container.create()

    # Check to see that the file isn't already in the mp3s container
    blob_client = blob_service_client.get_blob_client(container="mp3s", blob=audio_file_name)
    if not blob_client.exists():
        # Upload the file to the mp3s container
        with open(f'audio/{audio_file_name}', "rb") as data:
            blob_client.upload_blob(data)

def transcribe_audio(audio_file_name):
    blob_connection_string = os.getenv("STORAGE_CONNECTION_STRING")
    container_name = "mp3s"

    blob_service = BaseBlobService(connection_string=blob_connection_string)

    sas_token = blob_service.generate_blob_shared_access_signature(
        container_name,
        audio_file_name,
        permission=BlobPermissions.READ,
        expiry=datetime.utcnow() + timedelta(hours=1)
    )

    url_with_sas = blob_service.make_blob_url(
        container_name,
        audio_file_name,
        sas_token=sas_token
    )

    speech_key = os.getenv("SPEECH_KEY")
    speech_region = os.getenv("SPEECH_REGION")

    # Create a batch transcription job
    transcription_url = f"https://{speech_region}.api.cognitive.microsoft.com/speechtotext/v3.0/transcriptions"
    headers = {
        'Ocp-Apim-Subscription-Key': speech_key,
        'Content-Type': 'application/json'
    }
    body = {
        "contentContainerUrl": [url_with_sas],
        "locale": "en-US",
        "displayName": audio_file_name,
        "properties": {
            "diarizationEnabled": "false",
            "wordLevelTimestampsEnabled": "false",
            "punctuationMode": "DictatedAndAutomatic",
            "profanityFilterMode": "None",
        }
    }

    response = requests.post(transcription_url, headers=headers, json=body)

    # Get the transcription ID
    transcription_id = response.json()['self'].split('/')[-1]

    # Check the status of the transcription job
    # Wait a bit if it's still running
    job_status = response.json()['status']
    while job_status != 'Succeeded':
        response = requests.get(f"{transcription_url}/{transcription_id}", headers=headers)
        job_status = response.json()['status']
        if job_status.lower().contains('fail'):
            print('Transcription job failed')
            return None
        time.sleep(5)

    # Get the transcription results
    results_url = f"{transcription_url}/{transcription_id}/files"
    response = requests.get(results_url, headers=headers)
    results_url = response.json()['values'][0]['links']['contentUrl']

    print(f'Results URL: {results_url}')

    # Download result file
    urllib.request.urlretrieve(results_url, f"audio/{audio_file_name}.json")

    # Grab full transcript from the json file
    with open(f"audio/{audio_file_name}.json") as f:
        transcript = json.load(f)['combinedRecognizedPhrases'][0]['display']

    return transcript

def main(req: func.HttpRequest) -> func.HttpResponse:
    try:
        # Get the YouTube URL from the request body
        req_body = req.get_json()
        youtube_url = req_body.get('url')
    except ValueError:
        return func.HttpResponse(
            "Please pass a url in the request body",
            status_code=400
        )

    # Make sure the link is to a valid singular YouTube video
    if not youtube_url.startswith('https://www.youtube.com/watch?v='):
        return func.HttpResponse(
            "Please pass a valid YouTube video url in the request body",
            status_code=400
        )

    if youtube_url:
        download_video(youtube_url)
        # Upload the audio to Azure Blob Storage
        upload_audio_to_blob(f'{youtube_url}.mp3')
        # Use Azure Speech Services to transcribe the audio
        podcast_transcript = transcribe_audio(f'{youtube_url}.mp3')
        # Use OpenAI to summarize the transcript
        summary = summarize_text(podcast_transcript)
        # Return the summary
        return func.HttpResponse(
            summary,
            status_code=200
        )
    else:
        return func.HttpResponse(
            "Please pass a url in the request body",
            status_code=400
        )
