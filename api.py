from flask import Flask, request, jsonify
import logging

app = Flask(__name__)

def run_flask(fuse_fs):
    app.config['FUSE_FS'] = fuse_fs
    app.run(host='0.0.0.0', port=6000, threaded=True)

@app.route('/add_translation', methods=['POST'])
def add_translation():
    data = request.json
    logging.info(f"Received add_translation request: {data}")
    if 'original' in data and 'translated' in data:
        success = app.config['FUSE_FS'].add_translation(data['original'], data['translated'])
        if success:
            return jsonify({"status": "success", "message": "Translation added successfully"}), 200
        else:
            return jsonify({"status": "error", "message": "Failed to add translation"}), 500
    else:
        return jsonify({"status": "error", "message": "Missing 'original' or 'translated' in request"}), 400

@app.route('/remove_translation', methods=['POST'])
def remove_translation():
    data = request.json
    logging.info(f"Received remove_translation request: {data}")
    if 'original' in data:
        success = app.config['FUSE_FS'].remove_translation(data['original'])
        if success:
            return jsonify({"status": "success", "message": "Translation removed successfully"}), 200
        else:
            return jsonify({"status": "error", "message": "Failed to remove translation"}), 500
    else:
        return jsonify({"status": "error", "message": "Missing 'original' in request"}), 400

@app.route('/list_translations', methods=['GET'])
def list_translations():
    logging.info("Received list_translations request")
    translations = app.config['FUSE_FS'].list_translations()
    return jsonify({"translations": translations}), 200
