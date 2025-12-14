import os
import json
from flask import Flask, render_template, request, jsonify, Response, session
from werkzeug.utils import secure_filename
import PyPDF2
import anthropic
from datetime import datetime
import secrets

app = Flask(__name__)
app.secret_key = os.environ.get('SECRET_KEY', secrets.token_hex(16))

# Configuration
UPLOAD_FOLDER = 'pdfs'
ALLOWED_EXTENSIONS = {'pdf'}
app.config['UPLOAD_FOLDER'] = UPLOAD_FOLDER
app.config['MAX_CONTENT_LENGTH'] = 50 * 1024 * 1024  # 50MB max file size

# Ensure upload folder exists
os.makedirs(UPLOAD_FOLDER, exist_ok=True)

# Admin password (set this in Replit secrets)
ADMIN_PASSWORD = os.environ.get('ADMIN_PASSWORD', 'changeme123')

# Anthropic API key (set this in Replit secrets)
ANTHROPIC_API_KEY = os.environ.get('ANTHROPIC_API_KEY', '')

def allowed_file(filename):
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS

def extract_text_from_pdf(pdf_path):
    """Extract text from PDF file"""
    text = ""
    try:
        with open(pdf_path, 'rb') as file:
            pdf_reader = PyPDF2.PdfReader(file)
            for page_num, page in enumerate(pdf_reader.pages):
                page_text = page.extract_text()
                text += f"\n--- Page {page_num + 1} ---\n{page_text}"
    except Exception as e:
        print(f"Error extracting text from {pdf_path}: {e}")
    return text

def get_all_pdf_content():
    """Get content from all uploaded PDFs"""
    pdf_content = {}
    
    if not os.path.exists(UPLOAD_FOLDER):
        return pdf_content
    
    for filename in os.listdir(UPLOAD_FOLDER):
        if allowed_file(filename):
            filepath = os.path.join(UPLOAD_FOLDER, filename)
            content = extract_text_from_pdf(filepath)
            if content:
                pdf_content[filename] = content
    
    return pdf_content

def search_pdfs(query, pdf_content):
    """Simple keyword search in PDFs"""
    results = []
    query_lower = query.lower()
    
    for filename, content in pdf_content.items():
        # Split content by pages
        pages = content.split('--- Page')
        for page in pages:
            if query_lower in page.lower():
                # Extract page number
                page_num = page.split('---')[0].strip() if '---' in page else "Unknown"
                results.append({
                    'filename': filename,
                    'page': page_num,
                    'content': page[:1000]  # First 1000 chars of relevant page
                })
    
    return results

@app.route('/')
def index():
    """Main user interface"""
    return render_template('index.html')

@app.route('/admin')
def admin():
    """Admin interface for uploading PDFs"""
    return render_template('admin.html')

@app.route('/admin/login', methods=['POST'])
def admin_login():
    """Verify admin password"""
    data = request.json
    if data.get('password') == ADMIN_PASSWORD:
        session['admin'] = True
        return jsonify({'success': True})
    return jsonify({'success': False, 'error': 'Invalid password'}), 401

@app.route('/admin/upload', methods=['POST'])
def upload_file():
    """Handle PDF upload"""
    if not session.get('admin'):
        return jsonify({'error': 'Unauthorized'}), 401
    
    if 'file' not in request.files:
        return jsonify({'error': 'No file provided'}), 400
    
    file = request.files['file']
    if file.filename == '':
        return jsonify({'error': 'No file selected'}), 400
    
    if file and allowed_file(file.filename):
        filename = secure_filename(file.filename)
        filepath = os.path.join(app.config['UPLOAD_FOLDER'], filename)
        file.save(filepath)
        return jsonify({'success': True, 'filename': filename})
    
    return jsonify({'error': 'Invalid file type'}), 400

@app.route('/admin/files', methods=['GET'])
def list_files():
    """List all uploaded PDFs"""
    if not session.get('admin'):
        return jsonify({'error': 'Unauthorized'}), 401
    
    files = []
    if os.path.exists(UPLOAD_FOLDER):
        for filename in os.listdir(UPLOAD_FOLDER):
            if allowed_file(filename):
                filepath = os.path.join(UPLOAD_FOLDER, filename)
                files.append({
                    'name': filename,
                    'size': os.path.getsize(filepath),
                    'modified': datetime.fromtimestamp(os.path.getmtime(filepath)).isoformat()
                })
    return jsonify({'files': files})

@app.route('/admin/delete/<filename>', methods=['DELETE'])
def delete_file(filename):
    """Delete a PDF"""
    if not session.get('admin'):
        return jsonify({'error': 'Unauthorized'}), 401
    
    filepath = os.path.join(app.config['UPLOAD_FOLDER'], secure_filename(filename))
    if os.path.exists(filepath):
        os.remove(filepath)
        return jsonify({'success': True})
    return jsonify({'error': 'File not found'}), 404

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
    
    # Get all PDF content
    pdf_content = get_all_pdf_content()
    
    if not pdf_content:
        return jsonify({'error': 'No PDFs uploaded yet. Please contact the administrator.'}), 400
    
    # Search for relevant content
    relevant_content = search_pdfs(question, pdf_content)
    
    # Build context from PDFs
    context = "Available reference materials:\n\n"
    for idx, result in enumerate(relevant_content[:5]):  # Top 5 results
        context += f"[Source {idx+1}: {result['filename']}, Page {result['page']}]\n"
        context += f"{result['content'][:500]}...\n\n"
    
    # Build conversation messages
    messages = []
    for msg in conversation_history:
        messages.append({
            "role": msg["role"],
            "content": msg["content"]
        })
    
    # Add current question with context
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
            client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
            
            with client.messages.stream(
                model="claude-sonnet-4-20250514",
                max_tokens=2000,
                messages=messages,
                system="You are a helpful medical education assistant. Answer questions based only on the provided reference materials. Always cite your sources. Be concise but thorough."
            ) as stream:
                for text in stream.text_stream:
                    yield f"data: {json.dumps({'text': text})}\n\n"
                
                yield f"data: {json.dumps({'done': True})}\n\n"
        except Exception as e:
            yield f"data: {json.dumps({'error': str(e)})}\n\n"
    
    return Response(generate(), mimetype='text/event-stream')

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000, debug=True)
