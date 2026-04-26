import os
import re
import time
import yaml
import logging
import requests
from datetime import datetime
from flask import Flask, request, jsonify, abort
from flask_cors import CORS
from rapidfuzz import fuzz, process

app = Flask(__name__)

# Настройка логирования самого микросервиса
logging.basicConfig(filename='service.log', level=logging.INFO,
                    format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# Загрузка конфигурации
def load_config():
    config_filename = f"config.yaml"
    try:
        with open(config_filename, 'r') as f:
            return yaml.safe_load(f)
    except FileNotFoundError:
        logging.error(f"Не найден файл конфигурации '{config_filename}', приложение не может быть запущено")
        raise FileNotFoundError(f"config file {config_filename} missing")
    except yaml.YAMLError as e:
        logging.error(f"Ошибка парсировки YAML: {e}")
        raise e

config = load_config()
app.config.update(config)

CORS(app)

def verify_token():
    token = request.headers.get('Authorization')
    if not token:
        return None
    
    # Убираем префикс "Bearer ", если он есть
    if token.startswith("Bearer "):
        token = token.split(" ")[1]
    
    headers = {"Authorization": token}
    
    try:
        # Отправляем запрос к сервису авторизации для проверки токена
        response = requests.get(
            f"{app.config["AUTH_SERVICE_URL"]}/api/auth/me",
            headers=headers, 
            timeout=2
        )
        
        if response.status_code == 200:
            return response.json()  # Возвращаем данные пользователя
        else:
            logger.warning(f"Token validation failed: {response.status_code}")
            return None
            
    except requests.exceptions.RequestException as e:
        logger.error(f"Failed to connect to auth-service: {e}")
        return None

def get_log_files():
    """Получает список доступных файлов логов"""
    files = []
    for path in app.config['log_paths']:
        if os.path.exists(path):
            for filename in os.listdir(path):
                if filename.endswith('.log') or filename.endswith('.txt'):
                    files.append(os.path.join(path, filename))
    return files

def search_in_file(filepath, query, min_score):
    results = []
    try:
        # Обработка архивов (упрощенно: считаем, что это текстовые файлы)
        # В продакшене здесь нужно использовать gzip.open если файл .gz
        with open(filepath, 'r', errors='ignore') as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue

                score = fuzz.partial_ratio(query.lower(), line.lower())

                if score >= min_score:
                    results.append({
                        "file": filepath,
                        "line": line,
                        "score": score,
                    })
    except Exception as e:
        logger.error(f"Error reading file {filepath}: {e}")

    return results

@app.route('/api/search/healthcheck', methods=['GET'])
def healthcheck():
    return jsonify({
        "message": "search ok",
        "version": app.config.get('service_version', 'unknown')
    })

@app.route('/api/search/files', methods=['GET'])
def list_files():
    if not verify_token():
        abort(401)

    files = get_log_files()
    return jsonify(files)

@app.route('/api/search/files/<path:filepath>', methods=['GET'])
def view_file(filepath):
    if not verify_token():
        abort(401)

    n = request.args.get('n', default=10, type=int)
    # Безопасность: предотвращение выхода за пределы директории логов
    safe_path = None
    for base_path in app.config['log_paths']:
        candidate = os.path.join(base_path, filepath)
        if os.path.commonpath([base_path, candidate]) == base_path and os.path.exists(candidate):
            safe_path = candidate
            break

    if not safe_path:
        abort(404, description="File not found or access denied")

    try:
        with open(safe_path, 'r', errors='ignore') as f:
            lines = f.readlines()
            last_n = lines[-n:] if len(lines) > n else lines

        return jsonify({
            "path": safe_path,
            "size_bytes": os.path.getsize(safe_path),
            "lines": [l.strip() for l in last_n]
        })
    except Exception as e:
        abort(500, description=str(e))

@app.route('/api/search', methods=['GET'])
def search():
    user_data = verify_token()
    if not user_data:
        abort(401)

    start_time = time.time()

    query = request.args.get('query')
    if not query:
        abort(400, description="Query parameter is required")

    fuzziness = request.args.get('fuzziness', default=app.config['default_fuzziness'], type=int)
    page = request.args.get('page', default=1, type=int)
    size = request.args.get('size', default=app.config['default_page_size'], type=int)

    logger.info(f"Search request by {user_data.get('email')}: query='{query}'")

    # Логирование запроса
    user = request.headers.get('X-User', 'anonymous') # Предполагаем, что пользователь передается прокси
    logger.info(f"Search request by {user}: query='{query}', params={request.args}")

    all_results = []
    files_processed = 0

    log_files = get_log_files()

    # Ограничение количества файлов
    if len(log_files) > app.config['max_files_per_request']:
        log_files = log_files[:app.config['max_files_per_request']]

    for file_path in log_files:
        if files_processed >= app.config['max_files_per_request']:
            break
        results = search_in_file(file_path, query, fuzziness)
        all_results.extend(results)
        files_processed += 1

    # Сортировка по проценту совпадения (убывание)
    all_results.sort(key=lambda x: x['score'], reverse=True)

    # Пагинация
    total_count = len(all_results)
    start_idx = (page - 1) * size
    end_idx = start_idx + size
    paginated_results = all_results[start_idx:end_idx]

    execution_time = time.time() - start_time

    response_data = {
        "results": paginated_results,
        "metadata": {
            "total_count": total_count,
            "execution_time_sec": round(execution_time, 4),
            "page": page,
            "size": size
        }
    }

    return jsonify(response_data)

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000, debug=app.config["debug_mode"])
