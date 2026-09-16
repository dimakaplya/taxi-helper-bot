from flask import Flask
import threading
import subprocess
import sys

app = Flask(__name__)

def run_bot():
    """Запуск бота в отдельном потоке"""
    subprocess.run([sys.executable, "app.py"])

@app.route('/')
def health():
    return {'status': 'ok', 'message': 'Taxi Helper Bot is running'}, 200

if __name__ == '__main__':
    # Запуск бота в фоновом потоке
    bot_thread = threading.Thread(target=run_bot, daemon=True)
    bot_thread.start()
    
    # Flask слушает на порту 5000 (требуется для Railway)
    app.run(host='0.0.0.0', port=5000, debug=False)
