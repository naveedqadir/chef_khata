import os
import re
import struct
import time
import requests
import markdown
from io import BytesIO
from concurrent.futures import ThreadPoolExecutor, as_completed
from flask import Flask, render_template, request, jsonify, session, Response
from dotenv import load_dotenv

load_dotenv()

app = Flask(__name__)
app.secret_key = os.urandom(24)

GROQ_API_KEY = os.getenv("GROQ_API_KEY", "")
GROQ_API_URL = "https://api.groq.com/openai/v1/chat/completions"
GROQ_TTS_URL = "https://api.groq.com/openai/v1/audio/speech"
MODEL = "llama-3.3-70b-versatile"
TTS_MODEL = "canopylabs/orpheus-v1-english"
TTS_VOICE = "autumn"  # warm female voice — options: autumn, diana, hannah, austin, daniel, troy

# Shorter token limit for voice — keeps responses concise & TTS fast
MAX_VOICE_TOKENS = 300

SYSTEM_PROMPT = """You are Chef Khata, a warm and enthusiastic voice cooking assistant.
Users talk to you by voice, so keep your responses SHORT and conversational — like chatting in a kitchen.

Your expertise: recipes, cooking techniques, ingredient substitutions, meal planning, and food science.

Rules for voice responses:
- Keep answers to 2-4 sentences for simple questions
- For recipes, give brief numbered steps (max 6-8 steps)
- Never use markdown formatting like **, ##, or bullet points — speak naturally
- Avoid long lists — summarize instead
- If asked about non-cooking topics, gently redirect to cooking
- Be warm, encouraging, and make cooking feel approachable
- Mention both metric and imperial when giving measurements
- Be mindful of allergies and dietary restrictions"""


def get_chat_response(messages: list[dict]) -> str:
    """Send messages to Groq API and return the assistant's response."""
    if not GROQ_API_KEY or GROQ_API_KEY == "your-groq-api-key-here":
        return "Please set your Groq API key in the .env file. Get a free key at console.groq.com"

    headers = {
        "Authorization": f"Bearer {GROQ_API_KEY}",
        "Content-Type": "application/json",
    }

    payload = {
        "model": MODEL,
        "messages": messages,
        "temperature": 0.7,
        "max_completion_tokens": MAX_VOICE_TOKENS,
        "stream": False,
        "user": "chef-khata-voice-agent",
    }

    try:
        resp = requests.post(GROQ_API_URL, headers=headers, json=payload, timeout=30)
        resp.raise_for_status()
        data = resp.json()
        return data["choices"][0]["message"]["content"]
    except requests.exceptions.HTTPError:
        if resp.status_code == 401:
            return "Invalid API key. Please check your Groq API key."
        elif resp.status_code == 429:
            return "Rate limit reached. Please wait a moment and try again."
        return f"API error: {resp.status_code}"
    except requests.exceptions.ConnectionError:
        return "Could not connect to Groq API. Check your internet connection."
    except Exception as e:
        return f"Something went wrong: {e}"


@app.route("/")
def home():
    return render_template("home.html")


@app.route("/agent")
def agent():
    session.pop("history", None)
    return render_template("agent.html")


@app.route("/about")
def about():
    return render_template("about.html")


@app.route("/chat", methods=["POST"])
def chat():
    user_message = request.json.get("message", "").strip()
    if not user_message:
        return jsonify({"error": "Empty message"}), 400

    # Maintain conversation history in session (last 20 messages)
    history = session.get("history", [])

    messages = [{"role": "system", "content": SYSTEM_PROMPT}]
    messages.extend(history)
    messages.append({"role": "user", "content": user_message})

    reply = get_chat_response(messages)

    # Update history
    history.append({"role": "user", "content": user_message})
    history.append({"role": "assistant", "content": reply})
    # Keep last 20 messages to avoid token overflow
    session["history"] = history[-20:]

    # Convert markdown to HTML for display
    reply_html = markdown.markdown(reply, extensions=["fenced_code", "tables", "nl2br"])

    return jsonify({"reply": reply, "reply_html": reply_html})


@app.route("/clear", methods=["POST"])
def clear():
    session.pop("history", None)
    return jsonify({"status": "ok"})


# ── TTS Helpers ─────────────────────────────────────────────

def _clean_for_speech(text: str) -> str:
    """Strip markdown/emoji so TTS reads cleanly."""
    text = re.sub(r'[#*_`~>]', '', text)
    text = re.sub(r'\[([^\]]+)\]\([^)]+\)', r'\1', text)  # [link](url) → link
    text = re.sub(r'⚠️', 'Warning', text)
    text = re.sub(r'[^\x00-\x7F]+', ' ', text)  # remove emojis
    # Normalize numbers for natural reading (Orpheus tip: hyphens for letter-by-letter)
    text = re.sub(r'\s+', ' ', text).strip()
    return text


def _split_into_chunks(text: str, max_len: int = 195) -> list[str]:
    """Split text into chunks of ≤max_len chars at sentence boundaries.
    Orpheus limit is 200 chars — we use 195 for safety margin."""
    sentences = re.split(r'(?<=[.!?])\s+', text)
    chunks = []
    current = ""
    for sent in sentences:
        if len(sent) > max_len:
            parts = re.split(r'(?<=,)\s+', sent)
            for part in parts:
                if len(part) > max_len:
                    words = part.split()
                    for word in words:
                        if len(current) + len(word) + 1 > max_len:
                            if current:
                                chunks.append(current.strip())
                            current = word
                        else:
                            current = f"{current} {word}" if current else word
                elif len(current) + len(part) + 1 > max_len:
                    if current:
                        chunks.append(current.strip())
                    current = part
                else:
                    current = f"{current} {part}" if current else part
        elif len(current) + len(sent) + 1 > max_len:
            if current:
                chunks.append(current.strip())
            current = sent
        else:
            current = f"{current} {sent}" if current else sent
    if current:
        chunks.append(current.strip())
    return chunks


def _generate_tts_chunk(text: str, retries: int = 2) -> bytes | None:
    """Call Groq Orpheus TTS for a single chunk with retry on rate-limit.
    Orpheus free tier: 10 RPM, so we respect 429 with exponential backoff."""
    for attempt in range(retries + 1):
        try:
            resp = requests.post(
                GROQ_TTS_URL,
                headers={
                    "Authorization": f"Bearer {GROQ_API_KEY}",
                    "Content-Type": "application/json",
                },
                json={
                    "model": TTS_MODEL,
                    "input": text,
                    "voice": TTS_VOICE,
                    "response_format": "wav",
                },
                timeout=20,
            )
            if resp.status_code == 429 and attempt < retries:
                wait = float(resp.headers.get("retry-after", 2 ** (attempt + 1)))
                time.sleep(min(wait, 10))
                continue
            resp.raise_for_status()
            return resp.content
        except Exception:
            if attempt < retries:
                time.sleep(1)
                continue
            return None
    return None


def _concatenate_wavs(wav_list: list[bytes]) -> bytes:
    """Concatenate multiple WAV files (same format) into one."""
    if not wav_list:
        return b""
    if len(wav_list) == 1:
        return wav_list[0]

    # Parse first WAV header to get format info
    first = wav_list[0]
    # Standard WAV header is 44 bytes
    # Collect all PCM data
    pcm_data = BytesIO()
    for i, wav in enumerate(wav_list):
        if i == 0:
            # Find "data" chunk
            data_pos = wav.find(b'data')
            if data_pos == -1:
                pcm_data.write(wav[44:])
                header_size = 44
            else:
                header_size = data_pos + 8  # skip "data" + 4-byte size
                pcm_data.write(wav[header_size:])
        else:
            data_pos = wav.find(b'data')
            if data_pos == -1:
                pcm_data.write(wav[44:])
            else:
                pcm_data.write(wav[data_pos + 8:])

    raw_pcm = pcm_data.getvalue()
    # Rebuild WAV with correct sizes
    header = bytearray(first[:header_size] if 'header_size' in dir() else first[:44])

    # Update RIFF chunk size (file size - 8)
    total_size = len(header) + len(raw_pcm) - 8
    struct.pack_into('<I', header, 4, total_size)

    # Update data chunk size
    data_pos_h = bytes(header).find(b'data')
    if data_pos_h != -1:
        struct.pack_into('<I', header, data_pos_h + 4, len(raw_pcm))

    return bytes(header) + raw_pcm


@app.route("/speak", methods=["POST"])
def speak():
    """Convert text to human-like speech using Groq Orpheus TTS.
    Uses concurrent chunk generation for faster audio assembly."""
    text = request.json.get("text", "").strip()
    if not text:
        return jsonify({"error": "No text"}), 400

    clean = _clean_for_speech(text)
    if not clean:
        return jsonify({"error": "Empty after cleaning"}), 400

    chunks = _split_into_chunks(clean)

    # Generate TTS chunks concurrently (but respect Orpheus 10 RPM rate limit)
    # Use max 3 workers to avoid hammering the API
    wav_parts = [None] * len(chunks)
    max_workers = min(len(chunks), 3)

    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = {executor.submit(_generate_tts_chunk, chunk): i for i, chunk in enumerate(chunks)}
        for future in as_completed(futures):
            idx = futures[future]
            wav_parts[idx] = future.result()

    # Filter out failures while preserving order
    wav_parts = [w for w in wav_parts if w]

    if not wav_parts:
        return jsonify({"error": "TTS generation failed"}), 500

    combined = _concatenate_wavs(wav_parts)
    return Response(combined, mimetype="audio/wav",
                    headers={"Cache-Control": "no-store"})


if __name__ == "__main__":
    app.run(debug=True, port=5000)
