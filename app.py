from flask import Flask, render_template, jsonify, request
import requests
import os
import logging
import re
from dotenv import load_dotenv
from urllib3.exceptions import NameResolutionError
from requests.adapters import HTTPAdapter
from requests.packages.urllib3.util.retry import Retry

app = Flask(__name__)

# Set up logging
logging.basicConfig(level=logging.DEBUG)
logger = logging.getLogger(__name__)

# Load environment variables
load_dotenv()
API_BASE_URL = os.getenv("API_URL", "https://info.dev.3ceonline.com/ccce/apis/tradedata/import/v1/schedule")
API_TOKEN = os.getenv("API_TOKEN", "your_token_here")  # Replace with actual token in .env

# Configure session with retries
session = requests.Session()
retries = Retry(total=3, backoff_factor=1, status_forcelist=[502, 503, 504])
session.mount('https://', HTTPAdapter(max_retries=retries))


# Algorithm to order stackable HTS codes
def order_stackable_hts_codes(primary_hts, chapter_98_codes, exemption_codes, chapter_99_tariff_codes,
                              tariff_priority_rules, is_auto_part=False):
    ordered_hts_codes = []

    # Step 1: Add primary HTS code
    if primary_hts and is_valid_hts_code(primary_hts):
        ordered_hts_codes.append({
            'code': primary_hts,
            'desc': 'Primary HTS Code',
            'dutyRate': ''
        })
    else:
        logger.error("Invalid or missing primary HTS code")
        return []

    # Step 2: Add Chapter 98 codes
    for code in chapter_98_codes or []:
        if is_valid_hts_code(code):
            ordered_hts_codes.append({
                'code': code,
                'desc': 'Chapter 98 Special Duty Program',
                'dutyRate': 'Varies'
            })

    # Step 3: Add exemption codes
    for code in exemption_codes or []:
        if is_valid_hts_code(code):
            ordered_hts_codes.append({
                'code': code,
                'desc': 'Tariff Exemption',
                'dutyRate': 'Exempt'
            })

    # Step 4: Add Chapter 99 tariff codes in priority order
    if chapter_99_tariff_codes:
        # If auto part, exclude 9903.01.xx codes due to 9903.94.05
        if is_auto_part:
            chapter_99_tariff_codes = [
                item for item in chapter_99_tariff_codes
                if not item['code'].startswith('9903.01')
            ]
        sorted_tariff_codes = sort_chapter_99_codes(chapter_99_tariff_codes, tariff_priority_rules)
        for item in sorted_tariff_codes:
            if is_valid_hts_code(item['code']):
                ordered_hts_codes.append({
                    'code': item['code'],
                    'desc': item['desc'],
                    'dutyRate': item['rate']
                })

    return ordered_hts_codes


# Sort Chapter 99 codes by priority
def sort_chapter_99_codes(chapter_99_tariff_codes, tariff_priority_rules):
    return sorted(chapter_99_tariff_codes, key=lambda x: tariff_priority_rules.get(x['code'], 999))


# Validate HTS code
def is_valid_hts_code(code):
    import re
    return bool(re.match(r'^\d{8,10}(\.\d{2})?$|^9903\.\d{2}\.\d{2}$|^98\d{2}\.\d{2}\.\d{2}$', code))


# Find all full HS codes, their duties, and General rate
def find_full_hs_codes_and_duties(data):
    full_hs_codes = []

    def traverse(children, parent_duties=None):
        for item in children or []:
            code = item.get('code', '')
            duties = item.get('duties', {})
            # Check if this is a full 10-digit HS code
            if code and len(code) >= 10 and is_valid_hts_code(code):
                general_rate = duties.get('General', {}).get('rate', '0') if parent_duties else '0'
                full_hs_codes.append({
                    'code': code,
                    'duties': parent_duties or {},
                    'generalRate': general_rate
                })
            # Recurse with current duties as parent duties if they exist
            traverse(item.get('children', []), duties if duties else parent_duties)

    traverse(data.get('children', []))
    return full_hs_codes


@app.route('/')
def index():
    return render_template('index.html')


@app.route('/fetch-verifications', methods=['POST'])
def fetch_verifications():
    try:
        # Get form data
        hs_code = request.json.get('hsCode', '8501512020')
        origin = request.json.get('origin', 'CN')
        destination = request.json.get('destination', 'US')

        if not hs_code or not origin or not destination:
            return jsonify({"success": False, "error": "HS Code, Origin, and Destination are required"}), 400

        # Construct API URL (remove dots from HS code for URL)
        api_url = f"{API_BASE_URL}/{hs_code.replace('.', '')}/{origin}/{destination}"
        logger.debug(f"Calling API: {api_url}")

        headers = {
            "Authorization": f"Bearer {API_TOKEN}",
            "Content-Type": "application/json"
        }

        # Try GET first
        logger.debug("Attempting GET request")
        response = session.get(api_url, headers=headers, timeout=10)

        if response.status_code == 200:
            logger.debug("GET request successful")
            response_data = response.json()
        elif response.status_code == 405:  # Method Not Allowed
            # Fallback to POST
            logger.debug("GET failed with 405, attempting POST")
            payload = {}  # Adjust based on Swagger spec
            response = session.post(api_url, json=payload, headers=headers, timeout=10)
            if response.status_code == 200:
                logger.debug("POST request successful")
                response_data = response.json()
            else:
                logger.error(f"POST request failed: {response.status_code} - {response.text}")
                return jsonify({
                    "success": False,
                    "error": f"API request failed with status {response.status_code}: {response.text}"
                }), 500
        else:
            logger.error(f"GET request failed: {response.status_code} - {response.text}")
            return jsonify({
                "success": False,
                "error": f"API request failed with status {response.status_code}: {response.text}"
            }), 500

        # Parse response for all full HS codes and their duties
        hs_code_duties = find_full_hs_codes_and_duties(response_data)

        if not hs_code_duties:
            logger.error("No full HS codes found in response")
            return jsonify({
                "success": False,
                "error": "No full HS codes found in response"
            }), 404

        # Process stackable HS codes for each full HS code
        all_stackable_codes = []
        tariff_priority_rules = {
            '9903.88.01': 1,  # Section 301, high priority
            '9903.01.25': 2,  # Section 301, lower priority
            '9903.94.05': 3  # Auto parts, lowest unless auto part
        }

        # Assume non-auto part for 8501512020
        is_auto_part = False

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
                            'rate': duties.get(key, {}).get('rate', '').replace(' + 20% (9903.01.24)', '')
                        })
                        seen_codes.add(primary_code)

            chapter_98_codes = []  # No Chapter 98 codes in response
            exemption_codes = [
                duties[key].get('name', '')
                for key in duties
                if key == 'C' and duties[key].get('rate') == 'Free'
            ]

            ordered_hts_codes = order_stackable_hts_codes(
                primary_hts,
                chapter_98_codes,
                exemption_codes,
                chapter_99_tariff_codes,
                tariff_priority_rules,
                is_auto_part=is_auto_part
            )

            all_stackable_codes.append({
                'primaryHTS': primary_hts,
                'stackableCodes': ordered_hts_codes,
                'generalRate': general_rate
            })

        return jsonify({
            "success": True,
            "data": response_data,
            "stackableCodeSets": all_stackable_codes
        })

    except NameResolutionError as e:
        logger.error(f"DNS resolution error: {str(e)}")
        return jsonify({
            "success": False,
            "error": "DNS resolution failed for info.dev.3ceonline.com. Check API_URL or network settings (e.g., VPN, DNS)."
        }), 500
    except requests.exceptions.RequestException as e:
        logger.error(f"Network error: {str(e)}")
        return jsonify({"success": False, "error": f"Network error: {str(e)}"}), 500
    except Exception as e:
        logger.error(f"Unexpected error: {str(e)}")
        return jsonify({"success": False, "error": f"Unexpected error: {str(e)}"}), 500


if __name__ == '__main__':
    app.run(debug=True, port=5000)