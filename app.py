from flask import Flask, render_template, request, jsonify, make_response, copy_current_request_context
import os
import subprocess
import gc
import logging
import traceback
from functools import wraps
import time
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeoutError

# Configure logging
logging.basicConfig(level=logging.INFO,
                   format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

app = Flask(__name__, static_url_path='/static')
app.config['UPLOAD_FOLDER'] = 'static/audio'
app.config['MAX_CONTENT_LENGTH'] = 16 * 1024 * 1024  # 16MB max file size

# Ensure the upload folder exists
os.makedirs(app.config['UPLOAD_FOLDER'], exist_ok=True)

# Create a thread pool for running TTS tasks
executor = ThreadPoolExecutor(max_workers=2)

def process_tts(text, language, gender, alpha, output_file, inference_dir):
    """Process TTS in a separate function that doesn't need request context"""
    try:
        cmd = [
            'python',
            'inference.py',
            '--sample_text', text,
            '--language', language,
            '--gender', gender,
            '--alpha', str(alpha),
            '--output_file', output_file
        ]
        
        process = subprocess.run(
            cmd,
            check=True,
            cwd=inference_dir,
            capture_output=True,
            text=True,
            timeout=20  # Reduced timeout to ensure we respond within Render's limit
        )
        
        if process.returncode != 0:
            logger.error(f"TTS process failed with output: {process.stdout}\nError: {process.stderr}")
            raise subprocess.CalledProcessError(process.returncode, cmd, process.stdout, process.stderr)
            
        if not os.path.exists(output_file):
            logger.error("Output file was not generated")
            raise FileNotFoundError("Audio file was not generated")
            
        return True
            
    except subprocess.TimeoutExpired:
        logger.error("TTS process timed out")
        raise TimeoutError("Speech generation took too long. Please try with shorter text.")
    except Exception as e:
        logger.error(f"TTS processing error: {str(e)}")
        raise

def with_timeout(seconds):
    def decorator(func):
        @wraps(func)
        def wrapper(*args, **kwargs):
            try:
                # Get all the data we need from the request context
                data = {
                    'text': request.form.get('text'),
                    'language': request.form.get('language'),
                    'gender': request.form.get('gender'),
                    'alpha': float(request.form.get('alpha', 1.0))
                }
                
                # Validate input
                if not all([data['text'], data['language'], data['gender']]):
                    return jsonify({
                        'status': 'error',
                        'message': 'Missing required parameters'
                    }), 400
                
                # Limit text length more aggressively for Render deployment
                if len(data['text']) > 300:  # Reduced from 500 to 300 for Render
                    return jsonify({
                        'status': 'error',
                        'message': 'Text length exceeds maximum limit of 300 characters for free tier'
                    }), 400
                
                # Generate output filename with timestamp
                timestamp = int(time.time())
                filename = f'output_{data["language"]}_{data["gender"]}_{timestamp}.wav'
                output_file = os.path.join(os.path.abspath(app.config['UPLOAD_FOLDER']), filename)
                
                # Clean up old files
                cleanup_old_files(app.config['UPLOAD_FOLDER'])
                
                # Get the inference directory path
                current_dir = os.path.dirname(os.path.abspath(__file__))
                inference_dir = os.path.join(current_dir, 'Fastspeech2_HS')
                
                # Submit the task to the thread pool with a shorter timeout
                future = executor.submit(
                    process_tts,
                    data['text'],
                    data['language'],
                    data['gender'],
                    data['alpha'],
                    output_file,
                    inference_dir
                )
                
                try:
                    # Wait for the result with a shorter timeout
                    success = future.result(timeout=20)  # Reduced internal timeout
                    
                    if success:
                        response = jsonify({
                            'status': 'success',
                            'audio_path': f'/static/audio/{filename}'
                        })
                        response.headers['Cache-Control'] = 'no-cache, no-store, must-revalidate'
                        response.headers['Pragma'] = 'no-cache'
                        response.headers['Expires'] = '0'
                        return response
                    else:
                        return jsonify({
                            'status': 'error',
                            'message': 'TTS generation failed'
                        }), 500
                        
                except FuturesTimeoutError:
                    logger.error("Request timed out")
                    return jsonify({
                        'status': 'error',
                        'message': 'Request timed out. Please try with shorter text or try again.'
                    }), 504
                    
            except Exception as e:
                logger.error(f"Error in request processing: {str(e)}")
                return jsonify({
                    'status': 'error',
                    'message': str(e)
                }), 500
            finally:
                gc.collect()
        return wrapper
    return decorator

# --- Routes ---

@app.route('/')
def home():
    response = make_response(render_template('index.html'))
    response.headers['Cache-Control'] = 'no-cache, no-store, must-revalidate'
    response.headers['Pragma'] = 'no-cache'
    response.headers['Expires'] = '0'
    return response

@app.route('/synthesize', methods=['POST'])
@with_timeout(25)  # Increased to 25 seconds but with internal safeguards
def synthesize():
    # The actual processing is handled in the decorator
    pass

def cleanup_old_files(directory, max_age=3600):  # Clean files older than 1 hour
    try:
        current_time = time.time()
        for filename in os.listdir(directory):
            filepath = os.path.join(directory, filename)
            if os.path.isfile(filepath):
                file_age = current_time - os.path.getmtime(filepath)
                if file_age > max_age:
                    os.remove(filepath)
    except Exception as e:
        logger.error(f"Error cleaning up files: {str(e)}")

# --- Startup Logging ---
logger.info("Starting Flask application...")
logger.info("Checking for ML models...")
if os.path.exists('Fastspeech2_HS'):
    logger.info("Fastspeech2_HS directory found")
else:
    logger.warning("Fastspeech2_HS directory not found - models need to be uploaded")

@app.errorhandler(Exception)
def handle_exception(e):
    # Log the error for debugging
    logger.error(f"Unhandled Exception: {str(e)}", exc_info=True)
    # Return a JSON response with error details
    return jsonify({
        'status': 'error',
        'message': 'Internal Server Error: ' + str(e)
    }), 500

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 4005))
    host = os.environ.get('HOST', '0.0.0.0')
    debug = os.environ.get('FLASK_DEBUG', 'False').lower() == 'true'
    app.run(host=host, port=port, debug=debug)
