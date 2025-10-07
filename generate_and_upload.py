```python
#!/usr/bin/env python3
"""
generate_and_upload.py

Creates a ~5 minute (300s) video on a trending topic and uploads it to YouTube.
Designed for running in GitHub Actions (Ubuntu runner).

Secrets (set as GitHub Secrets):
 - YT_CLIENT_ID
 - YT_CLIENT_SECRET
 - YT_REFRESH_TOKEN
 - PEXELS_API_KEY

Requires requirements.txt:
 moviepy
 gtts
 pytrends
 requests
 google-api-python-client
 google-auth-oauthlib
 google-auth-httplib2
"""

import os
import io
import math
import json
import time
import random
import tempfile
import logging
from typing import List
from datetime import datetime

import requests
from gtts import gTTS
from pytrends.request import TrendReq
from moviepy.editor import (
    VideoFileClip,
    AudioFileClip,
    concatenate_videoclips,
    CompositeAudioClip,
    CompositeVideoClip,
    TextClip,
)
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload

# Logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("auto_video_bot")

# Config
OUTPUT_DIR = "outputs"
ASSETS_DIR = "assets"
MUSIC_PATH = os.path.join(ASSETS_DIR, "music.mp3")  # put your royalty-free music here
FINAL_VIDEO = os.path.join(OUTPUT_DIR, "final_video.mp4")
TARGET_DURATION = 300  # 5 minutes
VIDEO_RESOLUTION = (1280, 720)
PEXELS_PER_QUERY = 8  # how many videos to request per query page
PEXELS_VIDEO_MIN_DURATION = 2  # seconds
MAX_PEXELS_PAGES = 3

# Secrets (from env in GitHub Actions)
YT_CLIENT_ID = os.environ.get("YT_CLIENT_ID")
YT_CLIENT_SECRET = os.environ.get("YT_CLIENT_SECRET")
YT_REFRESH_TOKEN = os.environ.get("YT_REFRESH_TOKEN")
PEXELS_API_KEY = os.environ.get("PEXELS_API_KEY")

if not os.path.exists(OUTPUT_DIR):
    os.makedirs(OUTPUT_DIR, exist_ok=True)
if not os.path.exists(ASSETS_DIR):
    os.makedirs(ASSETS_DIR, exist_ok=True)


def get_trending_topic(region="india") -> str:
    """Get a trending topic using pytrends trending_searches."""
    try:
        pytrends = TrendReq(hl="en-US", tz=330)
        trends_df = pytrends.trending_searches(pn=region)
        if trends_df is None or trends_df.empty:
            logger.warning("pytrends returned empty; defaulting topic.")
            return "latest tech news"
        # choose a random top trending item
        top = trends_df.iloc[random.randint(0, min(5, len(trends_df)-1)), 0]
        logger.info(f"Selected trending topic: {top}")
        return str(top)
    except Exception as e:
        logger.exception("Error fetching trending topic; fallback used.")
        return "latest trending news"


def generate_script(topic: str) -> str:
    """
    Generate a ~5-minute spoken script with simple templates.
    This avoids paid LLMs; it's a deterministic template-expander to create ~300s of speech.
    """
    # Basic structure: intro (40s), 3 segments (80s each), conclusion (20s) -> ~300s
    intro = (
        f"Welcome! Today we're talking about {topic}. "
        "In the next five minutes you'll get a quick, clear summary, key facts, and what to watch next. "
    )

    # Generate segments with facts + explanations
    def expand_segment(idx):
        bullets = [
            f"Key point {idx+1}. What happened and why it matters.",
            "Important background details to understand context.",
            "A short example or statistic to make it concrete.",
            "What to look for next and possible outcomes.",
        ]
        # Expand bullets into full sentences
        return " ".join(b + " " + ("Here's why that matters." if i % 2 == 0 else "This often leads to more interest.") for i, b in enumerate(bullets))

    segments = [expand_segment(i) for i in range(3)]
    outro = "Thanks for watching. If you found this helpful, like and subscribe for daily five-minute explainers."

    # Combine, attempt to reach ~TARGET_DURATION by repeating details if necessary
    script = intro + "\n\n" + "\n\n".join(segments) + "\n\n" + outro

    # Heuristic to make it long enough: gTTS at natural pace ~150 wpm -> 300s -> 750 words
    words = script.split()
    target_words = 750
    if len(words) < target_words:
        # repeat segments with small paraphrase to reach target
        extra = []
        while len(words) + len(extra) < target_words:
            for s in segments:
                extra.append(s + " Here's another short insight.")
                if len(words) + len(extra) >= target_words:
                    break
        script = script + "\n\n" + "\n\n".join(extra)
    logger.info(f"Script generated ({len(script.split())} words).")
    return script


def text_to_speech(script: str, out_path: str) -> str:
    """Use gTTS to convert script to mp3"""
    logger.info("Generating TTS audio (gTTS)...")
    tts = gTTS(text=script, lang="en", slow=False)
    tts.save(out_path)
    logger.info(f"TTS saved to {out_path}")
    return out_path


def fetch_pexels_videos(query: str, required_seconds: int) -> List[str]:
    """
    Search Pexels videos for the query and download enough clips to cover required_seconds.
    Returns list of local file paths.
    """
    if not PEXELS_API_KEY:
        logger.warning("PEXELS_API_KEY not set. Skipping Pexels fetch and using stock placeholders.")
        return []

    logger.info(f"Searching Pexels for videos: '{query}'")
    headers = {"Authorization": PEXELS_API_KEY}
    downloaded = []
    total_seconds = 0
    page = 1

    while total_seconds < required_seconds and page <= MAX_PEXELS_PAGES:
        params = {"query": query, "per_page": PEXELS_PER_QUERY, "page": page}
        resp = requests.get("https://api.pexels.com/videos/search", headers=headers, params=params, timeout=30)
        if resp.status_code != 200:
            logger.warning(f"Pexels API returned {resp.status_code}: {resp.text}")
            break
        data = resp.json()
        videos = data.get("videos", [])
        if not videos:
            break
        for v in videos:
            video_files = v.get("video_files", [])
            if not video_files:
                continue
            # choose a medium quality file
            file_choice = sorted(video_files, key=lambda x: x.get("width", 0))[0]
            url = file_choice.get("link")
            dur = v.get("duration") or file_choice.get("fps") or 5
            if url is None:
                continue
            # download
            fname = os.path.join(ASSETS_DIR, f"pexels_{v['id']}_{int(time.time())}.mp4")
            try:
                with requests.get(url, stream=True, timeout=60) as r:
                    r.raise_for_status()
                    with open(fname, "wb") as f:
                        for chunk in r.iter_content(chunk_size=8192):
                            if chunk:
                                f.write(chunk)
                downloaded.append(fname)
                total_seconds += max(PEXELS_VIDEO_MIN_DURATION, float(dur))
                logger.info(f"Downloaded {fname} ({dur}s). Total seconds collected: {total_seconds}")
                if total_seconds >= required_seconds:
                    break
            except Exception as e:
                logger.exception("Error downloading pexels video; skipping.")
        page += 1

    logger.info(f"Total downloaded clip count: {len(downloaded)}; total seconds ~{total_seconds}")
    return downloaded


def assemble_video(clips_paths: List[str], narration_path: str, output_path: str, target_duration: int = TARGET_DURATION):
    """Combine clips, narration, and background music to create final video of ~target_duration."""
    logger.info("Assembling video from clips and narration...")

    # Load narration length
    narration_audio = AudioFileClip(narration_path)
    narration_length = narration_audio.duration
    logger.info(f"Narration duration: {narration_length}s")

    # If clips available, load and trim/loop to reach narration length (or target_duration)
    video_clips = []
    for p in clips_paths:
        try:
            clip = VideoFileClip(p).resize(newsize=VIDEO_RESOLUTION)
            if clip.duration < 1:
                continue
            video_clips.append(clip)
        except Exception:
            logger.exception(f"Failed to load clip {p}; skipping.")

    # If no clips downloaded, create simple color background clips with text (fallback)
    if not video_clips:
        logger.info("No video clips found; creating text slides as fallback.")
        # create 6 slides of equal duration til target
        num_slides = 6
        slide_dur = math.ceil(target_duration / num_slides)
        for i in range(num_slides):
            txt = TextClip("Trending Topic", fontsize=60, size=VIDEO_RESOLUTION, method="caption", align="center")
            txt = txt.set_duration(slide_dur)
            video_clips.append(txt.set_fps(24))

    # Concatenate visuals and adjust to narration length
    concatenated = None
    try:
        concatenated = concatenate_videoclips(video_clips, method="compose")
    except Exception:
        # if concatenation fails, try concatenating first n clips
        concatenated = concatenate_videoclips(video_clips[: max(1, len(video_clips))], method="compose")

    # If concatenated shorter than narration, loop or speed adjust
    if concatenated.duration < narration_length:
        loops = math.ceil(narration_length / concatenated.duration)
        logger.info(f"Looping visual clips {loops} times to match narration.")
        concatenated = concatenate_videoclips([concatenated] * loops, method="compose")

    # Trim to narration length
    video_final = concatenated.subclip(0, min(concatenated.duration, narration_length))

    # Add narration as audio (with background music)
    audio_clips = [narration_audio]
    # add bg music if exists
    bg_audio_clip = None
    if os.path.exists(MUSIC_PATH):
        try:
            bg_audio_clip = AudioFileClip(MUSIC_PATH).volumex(0.15)  # low volume
            # If bg shorter than narration, loop it
            if bg_audio_clip.duration < narration_audio.duration:
                loops = math.ceil(narration_audio.duration / bg_audio_clip.duration)
                bg_audio_clip = concatenate_audios([bg_audio_clip] * loops).subclip(0, narration_audio.duration)
            else:
                bg_audio_clip = bg_audio_clip.subclip(0, narration_audio.duration)
            audio_clips.append(bg_audio_clip)
        except Exception:
            logger.exception("Error loading background music. Proceeding without it.")
    else:
        logger.info("No music file at assets/music.mp3; skipping background music.")

    final_audio = CompositeAudioClip(audio_clips).set_duration(narration_audio.duration)
    video_final = video_final.set_audio(final_audio)

    # Add simple captions: split narration into chunks and overlay text at intervals
    # Create text clips overlay
    # Quick heuristic: create one caption every 8-10 seconds
    words = open(narration_path.replace(".mp3", ".txt"), "r", encoding="utf-8").read().split()
    # distribute words into chunks
    approx_words_per_caption = 30
    captions = [" ".join(words[i:i+approx_words_per_caption]) for i in range(0, len(words), approx_words_per_caption)]
    caption_clips = []
    cap_start = 0
    cap_dur = max(3, narration_audio.duration / max(1, len(captions)))
    for cap in captions[:40]:  # limit number to avoid overload
        txt_clip = TextClip(cap, fontsize=28, color="white", method="caption", size=(VIDEO_RESOLUTION[0]-60, None), align="West")
        txt_clip = txt_clip.set_position(("center", VIDEO_RESOLUTION[1]-100)).set_start(cap_start).set_duration(cap_dur)
        caption_clips.append(txt_clip)
        cap_start += cap_dur

    # Composite video with captions
    video_with_captions = CompositeVideoClip([video_final, *caption_clips])
    # write the final file
    logger.info(f"Writing final video to {output_path} ... (this can take a while)")
    video_with_captions.write_videofile(output_path, fps=24, codec="libx264", audio_codec="aac", threads=2, temp_audiofile="temp-audio.m4a", remove_temp=True)
    logger.info("Video assembly complete.")
    # close clips to release resources
    narration_audio.close()
    video_final.close()
    concatenated.close()
    for c in video_clips:
        try:
            c.close()
        except Exception:
            pass
    return output_path


def concatenate_audios(clips: List[AudioFileClip]):
    """Helper to concatenate audio clips using moviepy (used for bg music looping)."""
    from moviepy.editor import concatenate_audioclips
    return concatenate_audioclips(clips)


def generate_metadata(topic: str, script: str):
    """Create title, description, and tags using templates."""
    title = f"{topic} — Explained in 5 Minutes"
    description = (
        f"{title}\n\n"
        f"Quick 5 minute explainer on {topic}. Subscribe for daily 5-minute updates.\n\n"
        f"Generated on {datetime.utcnow().strftime('%Y-%m-%d UTC')}\n\n"
        "Timestamps:\n0:00 Intro\n1:00 Key points\n3:30 Summary\n\n"
        "Music: royalty-free\nClips: Pexels (free)\n"
    )
    tags = [topic.split()[0].lower(), "explainer", "5 minutes", "trending", "news", "shorts"]
    return title[:100], description, tags


def get_youtube_service():
    """Exchange refresh token for access token and create googleapiclient youtube service."""
    if not (YT_CLIENT_ID and YT_CLIENT_SECRET and YT_REFRESH_TOKEN):
        raise EnvironmentError("YouTube credentials not provided in env variables.")
    token_url = "https://oauth2.googleapis.com/token"
    data = {
        "client_id": YT_CLIENT_ID,
        "client_secret": YT_CLIENT_SECRET,
        "refresh_token": YT_REFRESH_TOKEN,
        "grant_type": "refresh_token",
    }
    resp = requests.post(token_url, data=data, timeout=30)
    resp.raise_for_status()
    token_data = resp.json()
    access_token = token_data.get("access_token")
    if not access_token:
        logger.error("Failed to obtain access token from refresh token response.")
        raise RuntimeError("No access token.")

    creds = Credentials(token=access_token)
    youtube = build("youtube", "v3", credentials=creds, cache_discovery=False)
    return youtube


def upload_video_to_youtube(file_path: str, title: str, description: str, tags: List[str]):
    """Uploads the video to YouTube using the YouTube Data API."""
    logger.info("Uploading to YouTube...")
    youtube = get_youtube_service()
    body = {
        "snippet": {
            "title": title,
            "description": description,
            "tags": tags,
            "categoryId": "28",  # Science & Technology as default
        },
        "status": {
            "privacyStatus": "public",
            "selfDeclaredMadeForKids": False,
        },
    }
    media = MediaFileUpload(file_path, chunksize=-1, resumable=True, mimetype="video/*")
    request = youtube.videos().insert(part="snippet,status", body=body, media_body=media)
    response = None
    while response is None:
        status, response = request.next_chunk()
        if status:
            logger.info(f"Upload progress: {int(status.progress() * 100)}%")
    logger.info(f"Upload complete. Video id: {response.get('id')}")
    # set thumbnail could be added here if you want
    return response.get("id")


def save_text_for_captions(script: str, tts_path: str):
    """Save a text copy alongside mp3 for quick caption pulling."""
    txt_path = tts_path.replace(".mp3", ".txt")
    with open(txt_path, "w", encoding="utf-8") as f:
        f.write(script)
    return txt_path


def main():
    logger.info("Starting auto video generation pipeline...")
    topic = get_trending_topic(region="india")
    script = generate_script(topic)
    if not script:
        logger.error("Script generation failed.")
        return

    # TTS
    tts_out = os.path.join(OUTPUT_DIR, "narration.mp3")
    text_to_speech(script, tts_out)
    save_text_for_captions(script, tts_out)

    # Fetch visuals
    # Use topic words as queries
    queries = [topic] + topic.split()[:2]
    clips = []
    for q in queries:
        clips.extend(fetch_pexels_videos(q, required_seconds=TARGET_DURATION // len(queries)))

    # assemble
    final_path = assemble_video(clips_paths=clips, narration_path=tts_out, output_path=FINAL_VIDEO, target_duration=TARGET_DURATION)

    # metadata
    title, description, tags = generate_metadata(topic, script)

    # upload
    try:
        video_id = upload_video_to_youtube(final_path, title, description, tags)
        logger.info(f"Video uploaded successfully: https://youtu.be/{video_id}")
    except Exception:
        logger.exception("Failed to upload video to YouTube.")

    logger.info("Pipeline finished.")


if __name__ == "__main__":
    main()
```
