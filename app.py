import os
import json
import re
import io
from flask import Flask, render_template, request, jsonify, Response, session
import PyPDF2
import anthropic
from datetime import datetime
import secrets
import requests

app = Flask(__name__)
app.secret_key = os.environ.get('SECRET_KEY', secrets.token_hex(16))

# Configuration
DATA_FOLDER = 'data'
PDF_CONTENT_FILE = os.path.join(DATA_FOLDER, 'pdf_content.json')
PDF_LINKS_FILE = os.path.join(DATA_FOLDER, 'pdf_links.json')
os.makedirs(DATA_FOLDER, exist_ok=True)

# Admin password
ADMIN_PASSWORD = os.environ.get('ADMIN_PASSWORD', 'changeme123')

# Anthropic API key
ANTHROPIC_API_KEY = os.environ.get('ANTHROPIC_API_KEY', '')

def extract_file_id_from_url(url):
    """Extract Google Drive file ID from share URL"""
    patterns = [
        r'/d/([a-zA-Z0-9-_]+)',
        r'id=([a-zA-Z0-9-_]+)',
        r'/file/d/([a-zA-Z0-9-_]+)',
    ]
    for pattern in patterns:
        match = re.search(pattern, url)
        if match:
            return match.group(1)
    return None

def download_pdf_from_drive(file_id, filename="unknown.pdf"):
    """Download PDF from Google Drive using direct download link"""
    try:
        download_url = f"https://drive.google.com/uc?export=download&id={file_id}"
        response = requests.get(download_url, timeout=60)
        
        if response.status_code == 200:
            return response.content, filename
        return None, None
    except Exception as e:
        print(f"Error downloading from Drive: {e}")
        return None, None

def extract_text_from_pdf_bytes(pdf_bytes):
    """Extract text from PDF bytes"""
    text = ""
    try:
        pdf_file = io.BytesIO(pdf_bytes)
        pdf_reader = PyPDF2.PdfReader(pdf_file)
        for page_num, page in enumerate(pdf_reader.pages):
            page_text = page.extract_text()
            text += f"\n--- Page {page_num + 1} ---\n{page_text}"
    except Exception as e:
        print(f"Error extracting PDF text: {e}")
    return text

def load_pdf_content():
    """Load processed PDF content from JSON file"""
    if os.path.exists(PDF_CONTENT_FILE):
        try:
            with open(PDF_CONTENT_FILE, 'r', encoding='utf-8') as f:
                return json.load(f)
        except:
            return {}
    return {}

def save_pdf_content(content):
    """Save processed PDF content to JSON file"""
    with open(PDF_CONTENT_FILE, 'w', encoding='utf-8') as f:
        json.dump(content, f, ensure_ascii=False, indent=2)

def load_pdf_links():
    """Load saved PDF links"""
    if os.path.exists(PDF_LINKS_FILE):
        try:
            with open(PDF_LINKS_FILE, 'r') as f:
                return json.load(f)
        except:
            return []
    return []

def save_pdf_links(links):
    """Save PDF links"""
    with open(PDF_LINKS_FILE, 'w') as f:
        json.dump(links, f, indent=2)

def search_pdfs(query, pdf_content):
    """Simple keyword search in PDFs"""
    results = []
    query_lower = query.lower()
    
    for filename, content in pdf_content.items():
        pages = content.split('--- Page')
        for page in pages:
            if query_lower in page.lower():
                page_num = page.split('---')[0].strip() if '---' in page else "Unknown"
                results.append({
                    'filename': filename,
                    'page': page_num,
                    'content': page[:1000]
                })
    
    return results

@app.route('/')
def index():
    """Main user interface"""
    return render_template('index.html')

@app.route('/admin')
def admin():
    """Admin interface"""
    return render_template('admin_gdrive.html')

@app.route('/admin/login', methods=['POST'])
def admin_login():
    """Verify admin password"""
    data = request.json
    if data.get('password') == ADMIN_PASSWORD:
        session['admin'] = True
        return jsonify({'success': True})
    return jsonify({'success': False, 'error': 'Invalid password'}), 401

@app.route('/admin/save_links', methods=['POST'])
def save_links():
    """Save Google Drive PDF links"""
    if not session.get('admin'):
        return jsonify({'error': 'Unauthorized'}), 401
    
    data = request.json
    links = data.get('links', [])
    
    save_pdf_links(links)
    return jsonify({'success': True, 'count': len(links)})

@app.route('/admin/get_links', methods=['GET'])
def get_links():
    """Get saved PDF links"""
    if not session.get('admin'):
        return jsonify({'error': 'Unauthorized'}), 401
    
    links = load_pdf_links()
    return jsonify({'links': links})

@app.route('/admin/digest', methods=['POST'])
def digest_pdfs():
    """Process PDFs from saved Google Drive links"""
    if not session.get('admin'):
        return jsonify({'error': 'Unauthorized'}), 401
    
    links = load_pdf_links()
    
    if not links:
        return jsonify({'error': 'No PDF links saved. Please add links first.'}), 400
    
    pdf_content = {}
    processed = 0
    errors = []
    
    for link_obj in links:
        url = link_obj.get('url', '')
        name = link_obj.get('name', 'Unknown')
        
        file_id = extract_file_id_from_url(url)
        if not file_id:
            errors.append(f"Invalid URL for {name}")
            continue
        
        try:
            pdf_bytes, _ = download_pdf_from_drive(file_id, name)
            if pdf_bytes:
                text = extract_text_from_pdf_bytes(pdf_bytes)
                if text:
                    pdf_content[name] = text
                    processed += 1
                else:
                    errors.append(f"Could not extract text from {name}")
            else:
                errors.append(f"Could not download {name} - make sure sharing is set to 'Anyone with the link'")
        except Exception as e:
            errors.append(f"Error processing {name}: {str(e)}")
    
    save_pdf_content(pdf_content)
    
    return jsonify({
        'success': True,
        'processed': processed,
        'total': len(links),
        'errors': errors
    })

@app.route('/admin/status', methods=['GET'])
def get_status():
    """Get current status of processed PDFs"""
    if not session.get('admin'):
        return jsonify({'error': 'Unauthorized'}), 401
    
    pdf_content = load_pdf_content()
    return jsonify({
        'total_files': len(pdf_content),
        'files': list(pdf_content.keys())
    })

@app.route('/ask', methods=['POST'])
def ask_question():
    """Handle user questions with streaming response"""
    data = request.json
    question = data.get('question', '')
    conversation_history = data.get('history', [])
    
    if not question:
        return jsonify({'error': 'No question provided'}), 400
    
    if not ANTHROPIC_API_KEY:
        return jsonify({'error': 'API key not configured'}), 500
    
    pdf_content = load_pdf_content()
    
    if not pdf_content:
        return jsonify({'error': 'No PDFs processed yet. Please contact the administrator.'}), 400
    
    relevant_content = search_pdfs(question, pdf_content)
    
    context = "Available reference materials:\n\n"
    for idx, result in enumerate(relevant_content[:5]):
        context += f"[Source {idx+1}: {result['filename']}, Page {result['page']}]\n"
        context += f"{result['content'][:500]}...\n\n"
    
    messages = []
    for msg in conversation_history:
        messages.append({
            "role": msg["role"],
            "content": msg["content"]
        })
    
    current_message = f"""Based ONLY on the following reference materials from uploaded PDFs, please answer this question. 
If the answer is not in the provided materials, say so clearly.
Always cite your sources using the format [Source X: filename, Page Y].

{context}

Question: {question}"""
    
    messages.append({
        "role": "user",
        "content": current_message
    })
    
    def generate():
        try:
            client = anthropic.Client(api_key=ANTHROPIC_API_KEY)
            
            response = client.messages.create(
                model="claude-sonnet-4-20250514",
                max_tokens=2000,
                messages=messages,
                system="You are a helpful medical education assistant. Answer questions based only on the provided reference materials. Always cite your sources. Be concise but thorough.",
                stream=True
            )
            
            for event in response:
                if hasattr(event, 'delta') and hasattr(event.delta, 'text'):
                    yield f"data: {json.dumps({'text': event.delta.text})}\n\n"
            
            yield f"data: {json.dumps({'done': True})}\n\n"
                
        except Exception as e:
            yield f"data: {json.dumps({'error': str(e)})}\n\n"
    
    return Response(generate(), mimetype='text/event-stream')

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000, debug=True)
