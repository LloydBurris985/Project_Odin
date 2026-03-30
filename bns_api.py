# bns_api.py - Minimal REST API for BNS lattice (encode/decode)
from flask import Flask, request, jsonify, send_file
import subprocess
import os
import uuid
import tempfile

app = Flask(__name__)

BNS_PATH = "bns.py"
UPLOAD_DIR = "api_temp"
os.makedirs(UPLOAD_DIR, exist_ok=True)

@app.route('/encode', methods=['POST'])
def encode():
    if 'file' not in request.files:
        return jsonify({"error": "No file uploaded"}), 400
    file = request.files['file']
    if not file.filename:
        return jsonify({"error": "Empty filename"}), 400

    temp_in = os.path.join(UPLOAD_DIR, f"in_{uuid.uuid4().hex}")
    temp_out = f"{temp_in}.bin"
    file.save(temp_in)

    try:
        subprocess.run(["python3", BNS_PATH, "--mode", "encode",
                        "--file", temp_in, "--output_file", temp_out], check=True)
        return send_file(temp_out, as_attachment=True, download_name="encoded_lattice.bin")
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    finally:
        for p in [temp_in, temp_out]:
            if os.path.exists(p):
                os.remove(p)

@app.route('/decode', methods=['POST'])
def decode():
    if 'file' not in request.files:
        return jsonify({"error": "No file uploaded"}), 400
    file = request.files['file']
    if not file.filename:
        return jsonify({"error": "Empty filename"}), 400

    temp_in = os.path.join(UPLOAD_DIR, f"in_{uuid.uuid4().hex}")
    temp_out = f"{temp_in}.decoded"
    file.save(temp_in)

    try:
        subprocess.run(["python3", BNS_PATH, "--mode", "decode",
                        "--file", temp_in, "--output_file", temp_out], check=True)
        return send_file(temp_out, as_attachment=True, download_name="decoded.bin")
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    finally:
        for p in [temp_in, temp_out]:
            if os.path.exists(p):
                os.remove(p)

@app.route('/health', methods=['GET'])
def health():
    return jsonify({
        "status": "BNS API live",
        "bns_script": BNS_PATH,
        "note": "Upload binary file to /encode or /decode"
    })

if __name__ == '__main__':
    print("BNS API ready → http://0.0.0.0:5000")
    print("POST to /encode or /decode with a file")
    app.run(host='0.0.0.0', port=5000)
