import os
import json
import random
import asyncio
import tempfile
import functools
from flask import Flask, render_template, request, Response, jsonify, redirect, url_for, make_response, session
import edge_tts

app = Flask(__name__)
app.secret_key = "spidey_ff_tts_secret_key_999"

# --- CONFIGURATION ---
# Determine where to save the Stats JSON (Vercel requires /tmp)
if os.environ.get('VERCEL') or os.environ.get('AWS_LAMBDA_FUNCTION_NAME'):
    STATS_FILE = "/tmp/server_stats.json"
else:
    STATS_FILE = os.path.join(tempfile.gettempdir(), "server_stats.json")

# --- STATS HELPERS (JSON) ---
def load_stats():
    """Load stats from JSON file safely."""
    try:
        if os.path.exists(STATS_FILE):
            with open(STATS_FILE, 'r') as f:
                return json.load(f)
    except:
        pass
    return {"total": 0, "api": 0, "ui": 0}

def save_stats(mode="UI"):
    """Increment stats and save to JSON."""
    try:
        stats = load_stats()
        stats["total"] += 1
        if mode == "API": 
            stats["api"] += 1
        else: 
            stats["ui"] += 1
        
        with open(STATS_FILE, 'w') as f:
            json.dump(stats, f)
    except:
        # If writing fails (rare permission issue), we ignore it 
        # so the main app doesn't crash.
        pass

# --- GENERAL HELPERS ---
def run_async(coroutine):
    """Helper to run async code in synchronous Flask."""
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    return loop.run_until_complete(coroutine)

def get_flag_emoji(locale):
    """Converts locale code to emoji flag."""
    try:
        if "-" in locale:
            code = locale.split("-")[-1]
            return chr(ord(code[0]) + 127397) + chr(ord(code[1]) + 127397)
        return "🌐"
    except:
        return "🏳️"

def parse_voice(v):
    """Parses raw voice data into a clean dictionary."""
    parts = v['ShortName'].split('-')
    name = parts[2].replace("Neural", "") if len(parts) > 2 else v['ShortName']
    
    # Try to extract a clean language name
    try:
        full_lang = v['FriendlyName'].split(" - ")[-1]
    except:
        full_lang = v['Locale']

    return {
        "id": v['ShortName'],
        "name": name,
        "gender": v['Gender'],
        "locale": v['Locale'],
        "flag": get_flag_emoji(v['Locale']),
        "full_lang": full_lang
    }

# --- ROUTES ---

@app.route('/')
def root():
    # 1. Security Redirect Check
    if not session.get('verified_user'):
        return render_template('security.html')
    
    # 2. Render Main Generator
    try:
        raw_voices = run_async(edge_tts.list_voices())
        # Sort by Locale
        raw_voices = sorted(raw_voices, key=lambda x: x['Locale'])
        voices = [parse_voice(v) for v in raw_voices]
        
        # Note: We do NOT pass 'history' here anymore. 
        # History is now handled by the Browser (LocalStorage) in the HTML template.
        return render_template('generator.html', voices=voices)
    except Exception as e:
        return f"System Error: {e}"

@app.route('/verify-security')
def verify_security():
    """Sets the session cookie to prove security check passed."""
    session['verified_user'] = True
    return redirect(url_for('root'))

@app.route('/voices')
def gallery():
    try:
        raw_voices = run_async(edge_tts.list_voices())
        voices = [parse_voice(v) for v in raw_voices]
        return render_template('voices.html', voices=voices)
    except Exception as e:
        return f"Error loading voices: {e}"

@app.route('/languages')
def languages():
    try:
        raw_voices = run_async(edge_tts.list_voices())
        data = [parse_voice(v) for v in raw_voices]
        unique = {}
        for d in data:
            if d['locale'] not in unique:
                unique[d['locale']] = d
        # Sort by locale code
        sorted_langs = sorted(unique.values(), key=lambda x: x['locale'])
        return render_template('languages.html', languages=sorted_langs)
    except Exception as e:
        return f"Error loading languages: {e}"

@app.route('/api-docs')
def apidocs():
    return render_template('apidocs.html')

# --- MAIN API ENDPOINT ---
@app.route('/tts', methods=['GET', 'POST'])
def tts_api():
    # 1. Get Parameters
    text = request.args.get('text') or request.form.get('text')
    voice_param = request.args.get('voice') or request.form.get('voice')
    lang = request.args.get('lang')
    gender = request.args.get('gender')
    country = request.args.get('country')
    rate = request.args.get('rate') or "+0%"
    
    if not text:
        return jsonify({"error": "Missing 'text' parameter"}), 400

    # 2. Determine Usage Mode (For Stats)
    mode = "API"
    # If the request comes from our own website, count as UI
    if request.referrer and request.host in request.referrer:
        mode = "UI"
    
    save_stats(mode)

    # 3. Fetch Available Voices
    try:
        all_voices = run_async(edge_tts.list_voices())
    except:
        return jsonify({"error": "TTS Engine unavailable"}), 500

    selected_voice = "en-US-AriaNeural" # Default Fallback

    # 4. Voice Selection Logic
    if voice_param:
        # Smart Match: "Adri" -> "Microsoft Adri Neural"
        clean = voice_param.lower().replace("neural", "").strip()
        for v in all_voices:
            if clean in v['ShortName'].lower():
                selected_voice = v['ShortName']
                break
    elif lang or gender or country:
        # Filter Logic
        candidates = all_voices
        if lang: 
            candidates = [v for v in candidates if v['Locale'].lower().startswith(lang.lower())]
        if gender: 
            candidates = [v for v in candidates if v['Gender'].lower() == gender.lower()]
        if country: 
            candidates = [v for v in candidates if v['Locale'].split('-')[-1].lower() == country.lower()]
        
        if candidates:
            selected_voice = random.choice(candidates)['ShortName']

    # 5. Generate Audio
    try:
        # Determine temp directory based on environment
        temp_dir = "/tmp" if (os.environ.get('VERCEL') or os.environ.get('AWS_LAMBDA_FUNCTION_NAME')) else tempfile.gettempdir()
        
        with tempfile.NamedTemporaryFile(delete=False, suffix=".mp3", dir=temp_dir) as temp:
            t_name = temp.name

        async def _gen():
            communicate = edge_tts.Communicate(text, selected_voice, rate=rate)
            await communicate.save(t_name)

        run_async(_gen())
        
        # Read the file into memory
        with open(t_name, 'rb') as f:
            data = f.read()
            
        # Delete the temp file immediately to save space
        os.remove(t_name)
        
        # Return the audio stream
        return Response(data, mimetype="audio/mpeg", headers={
            "Content-Disposition": f"attachment; filename=tts_{selected_voice}.mp3",
            "X-Voice-Used": selected_voice
        })
        
    except Exception as e:
        return jsonify({"error": str(e)}), 500

# --- ADMIN PANEL ---

@app.route('/dashboard/734401', methods=['GET', 'POST'])
def admin_login():
    if request.method == 'POST':
        u = request.form.get('username')
        p = request.form.get('password')
        if u == "Spidey" and p == "Admin_734401":
            session['admin_logged_in'] = True
            return redirect(url_for('admin_panel'))
    return render_template('admin_login.html')

@app.route('/dashboard/panel')
def admin_panel():
    if not session.get('admin_logged_in'): 
        return redirect(url_for('admin_login'))
    
    stats = load_stats()
    return render_template('admin_panel.html', stats=stats)

@app.route('/logout')
def logout():
    session.pop('admin_logged_in', None)
    return redirect(url_for('root'))

if __name__ == '__main__':
    # 0.0.0.0 allows access from other devices on the same network
    app.run(host='0.0.0.0', port=8080)