from flask import Flask, render_template, jsonify, request, Response
import requests
import os
import logging
import re
import uuid
from dotenv import load_dotenv
from requests.exceptions import RequestException
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

app = Flask(__name__)

# Set up logging
logging.basicConfig(level=logging.DEBUG)
logger = logging.getLogger(__name__)

# Load environment variables
load_dotenv()
API_BASE_URL = os.getenv("API_URL", "https://info.dev.3ceonline.com/ccce/apis/tradedata/import/v1/schedule")
API_TOKEN = os.getenv("API_TOKEN", "your_token_here")
VALID_USER = os.getenv("AUTH_USER", "admin")
VALID_PASS = os.getenv("AUTH_PASS", "secret123")

# Validate environment variables
if not VALID_USER or not VALID_PASS:
    logger.error("Missing AUTH_USER or AUTH_PASS environment variables")
    raise ValueError("Authentication credentials must be set in environment variables")

# Configure session with retries
session = requests.Session()
retries = Retry(total=3, backoff_factor=1, status_forcelist=[502, 503, 504])
session.mount('https://', HTTPAdapter(max_retries=retries))

# Authentication
def auth_required():
    request_id = str(uuid.uuid4())
    auth = request.authorization
    logger.debug(f"[{request_id}] Authorization header: {auth}")
    if not auth:
        logger.error(f"[{request_id}] No authorization header provided")
        return Response('Unauthorized', 401, {'WWW-Authenticate': 'Basic realm="Login Required"'})
    if auth.username != VALID_USER or auth.password != VALID_PASS:
        logger.error(f"[{request_id}] Invalid credentials: username={auth.username}, expected={VALID_USER}")
        return Response('Unauthorized', 401, {'WWW-Authenticate': 'Basic realm="Login Required"'})
    logger.debug(f"[{request_id}] Authentication successful")
    return None

# Validate HTS code
def is_valid_hts_code(code):
    return bool(re.match(r'^\d{4,10}(\.\d{2})?$|^9903\.\d{2}\.\d{2}$|^98\d{2}\.\d{2}\.\d{2}$', code))

# Algorithm to order stackable HTS codes
def order_stackable_hts_codes(primary_hts, chapter_98_codes, exemption_codes, chapter_99_tariff_codes):
    ordered_hts_codes = []

    if primary_hts and is_valid_hts_code(primary_hts):
        ordered_hts_codes.append({
            'code': primary_hts,
            'desc': 'Primary HTS Code',
            'dutyRate': ''
        })
    else:
        logger.error(f"Invalid or missing primary HTS code: {primary_hts}")
        return []

    for code in chapter_98_codes or []:
        if is_valid_hts_code(code):
            ordered_hts_codes.append({
                'code': code,
                'desc': 'Chapter 98 Special Duty Program',
                'dutyRate': 'Varies'
            })

    for code in exemption_codes or []:
        if is_valid_hts_code(code):
            ordered_hts_codes.append({
                'code': code,
                'desc': 'Tariff Exemption',
                'dutyRate': 'Exempt'
            })

    if chapter_99_tariff_codes:
        sorted_tariff_codes = sort_chapter_99_codes(chapter_99_tariff_codes)
        for item in sorted_tariff_codes:
            if is_valid_hts_code(item['code']):
                ordered_hts_codes.append({
                    'code': item['code'],
                    'desc': item['desc'],
                    'dutyRate': item['rate']
                })

    return ordered_hts_codes

# Sort Chapter 99 codes by priority
def sort_chapter_99_codes(chapter_99_tariff_codes):
    tariff_priority_rules = {
        '9903.88.01': 1,
        '9903.01.25': 2,
        '9903.94.05': 3
    }
    return sorted(chapter_99_tariff_codes, key=lambda x: tariff_priority_rules.get(x['code'], 999))

# Find all full HS codes and their duties
def find_full_hs_codes_and_duties(data):
    full_hs_codes = []

    def traverse(children, parent_duties=None):
        for item in children or []:
            code = item.get('code', '')
            duties = item.get('duties', {})
            if code and len(code) >= 10 and is_valid_hts_code(code):
                general_rate = duties.get('General', {}).get('rate', '0') if parent_duties else '0'
                full_hs_codes.append({
                    'code': code,
                    'duties': parent_duties or {},
                    'generalRate': general_rate
                })
            traverse(item.get('children', []), duties if duties else parent_duties)

    traverse(data.get('children', []))
    return full_hs_codes

# Shared logic for fetching stackable HS codes
def fetch_stackable_codes(hs_code, origin, destination, request_id):
    if not hs_code or not origin or not destination:
        return {"success": False, "error": "HS Code, Origin, and Destination are required"}, 400

    if not isinstance(hs_code, str) or not isinstance(origin, str) or not isinstance(destination, str):
        return {"success": False, "error": "HS Code, Origin, and Destination must be strings"}, 400

    hs_code = hs_code.strip()
    origin = origin.strip().upper()
    destination = destination.strip().upper()

    if not is_valid_hts_code(hs_code):
        return {"success": False, "error": "Invalid HS Code format"}, 400

    if not re.match(r'^[A-Z]{2}$', origin) or not re.match(r'^[A-Z]{2}$', destination):
        return {"success": False, "error": "Origin and Destination must be 2-letter ISO country codes"}, 400

    api_url = f"{API_BASE_URL}/{hs_code.replace('.', '')}/{origin}/{destination}"
    logger.debug(f"[{request_id}] Calling API: {api_url}")

    headers = {
        "Authorization": f"Bearer {API_TOKEN}",
        "Content-Type": "application/json"
    }

    try:
        response = session.get(api_url, headers=headers, timeout=10)
        if response.status_code == 200:
            logger.debug(f"[{request_id}] GET request successful")
            response_data = response.json()
        elif response.status_code == 405:
            logger.debug(f"[{request_id}] GET failed with 405, attempting POST")
            response = session.post(api_url, json={}, headers=headers, timeout=10)
            if response.status_code == 200:
                logger.debug(f"[{request_id}] POST request successful")
                response_data = response.json()
            else:
                logger.error(f"[{request_id}] POST request failed: {response.status_code} - {response.text}")
                return {"success": False, "error": f"API request failed: {response.text}"}, response.status_code
        elif response.status_code == 429:
            logger.error(f"[{request_id}] Rate limit exceeded")
            return {"success": False, "error": "Rate limit exceeded"}, 429
        else:
            logger.error(f"[{request_id}] GET request failed: {response.status_code} - {response.text}")
            return {"success": False, "error": f"API request failed: {response.text}"}, response.status_code

        hs_code_duties = find_full_hs_codes_and_duties(response_data)
        if not hs_code_duties:
            logger.error(f"[{request_id}] No full HS codes found in response")
            return {"success": False, "error": "No full HS codes found in response"}, 404

        all_stackable_codes = []
        for hs_item in hs_code_duties:
            primary_hts = hs_item['code']
            duties = hs_item['duties']
            general_rate = hs_item['generalRate']

            chapter_99_tariff_codes = []
            seen_codes = set()
            for key in duties:
                if key.startswith('Additional Duty 9903'):
                    primary_code = key.replace('Additional Duty ', '').replace(', Clause 20(a&b)', '')
                    if primary_code not in seen_codes:
                        chapter_99_tariff_codes.append({
                            'code': primary_code,
                            'desc': duties.get(key, {}).get('longName', ''),
                            'rate': duties.get(key, {}).get('rate', '')
                        })
                        seen_codes.add(primary_code)

            chapter_98_codes = []
            exemption_codes = [
                duties[key].get('name', '')
                for key in duties
                if key == 'C' and duties[key].get('rate') == 'Free'
            ]

            ordered_hts_codes = order_stackable_hts_codes(
                primary_hts,
                chapter_98_codes,
                exemption_codes,
                chapter_99_tariff_codes
            )

            all_stackable_codes.append({
                'primaryHTS': primary_hts,
                'stackableCodes': ordered_hts_codes,
                'generalRate': general_rate
            })

        return {
            "success": True,
            "data": response_data,
            "stackableCodeSets": all_stackable_codes
        }, 200

    except RequestException as e:
        logger.error(f"[{request_id}] Network error: {str(e)}")
        return {"success": False, "error": f"Network error: {str(e)}"}, 500
    except Exception as e:
        logger.error(f"[{request_id}] Unexpected error: {str(e)}")
        return {"success": False, "error": f"Unexpected error: {str(e)}"}, 500

@app.route('/')
def index():
    auth_response = auth_required()
    if auth_response:
        return auth_response
    return render_template('index.html')

@app.route('/fetch-verifications', methods=['POST'])
def fetch_verifications():
    request_id = str(uuid.uuid4())
    auth_response = auth_required()
    if auth_response:
        return auth_response

    try:
        if not request.is_json:
            return jsonify({"success": False, "error": "Request must be JSON"}), 400

        data = request.get_json()
        hs_code = data.get('hsCode', '8501512020')
        origin = data.get('origin', 'CN')
        destination = data.get('destination', 'US')

        result, status_code = fetch_stackable_codes(hs_code, origin, destination, request_id)
        return jsonify(result), status_code

    except Exception as e:
        logger.error(f"[{request_id}] Unexpected error in fetch-verifications: {str(e)}")
        return jsonify({"success": False, "error": f"Unexpected error: {str(e)}"}), 500

@app.route('/api/stackable-hs', methods=['POST'])
def stackable_hs_api():
    request_id = str(uuid.uuid4())
    auth_response = auth_required()
    if auth_response:
        return auth_response

    try:
        if not request.is_json:
            return jsonify({"success": False, "error": "Request must be JSON"}), 400

        data = request.get_json()
        hs_code = data.get('hsCode')
        origin = data.get('origin')
        destination = data.get('destination')

        result, status_code = fetch_stackable_codes(hs_code, origin, destination, request_id)
        return jsonify(result), status_code

    except Exception as e:
        logger.error(f"[{request_id}] Unexpected error in stackable-hs-api: {str(e)}")
        return jsonify({"success": False, "error": f"Unexpected error: {str(e)}"}), 500

if __name__ == '__main__':
    port = int(os.getenv('PORT', 5000))
    app.run(host='0.0.0.0', port=port, debug=True)