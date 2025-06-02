from flask import Flask, render_template, jsonify, request, Response, session, redirect, url_for
import requests
import os
import logging
import re
import pandas as pd
from dotenv import load_dotenv
from requests.exceptions import RequestException
from requests.adapters import HTTPAdapter
from requests.packages.urllib3.util.retry import Retry

app = Flask(__name__)
app.secret_key = os.urandom(24)

# Set up logging
logging.basicConfig(level=logging.DEBUG)
logger = logging.getLogger(__name__)

# Load environment variables
load_dotenv()
HS_API_BASE_URL = os.getenv("HS_API_URL", "https://info.dev.3ceonline.com/ccce/apis/tradedata/import/v1/schedule")
PGA_API_BASE_URL = os.getenv("PGA_API_URL", "https://info.dev.3ceonline.com/ccce/apis/pga-flags")
API_TOKEN = os.getenv("API_TOKEN", "your_token_here")
BARCODE_API_KEY = os.getenv("BARCODE_API_KEY", "your_barcode_api_key")
VALID_USER = "admin"
VALID_PASS = "secret123"

# Base paths
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(BASE_DIR, "data")

# Configure session with retries
session_requests = requests.Session()
retries = Retry(total=3, backoff_factor=1, status_forcelist=[502, 503, 504])
session_requests.mount('https://', HTTPAdapter(max_retries=retries))


# Authentication
def auth_required():
    auth = request.authorization
    if not auth or auth.username != VALID_USER or auth.password != VALID_PASS:
        return Response('Unauthorized', 401, {'WWW-Authenticate': 'Basic realm="Login Required"'})
    return None


# Unified landing page with authentication
@app.route('/')
def index():
    auth_response = auth_required()
    if auth_response:
        return auth_response
    session['authenticated'] = True
    return render_template('index.html')


# HS Stacking: Feature page
@app.route('/hs-stacking')
def hs_stacking():
    if not session.get('authenticated'):
        return redirect(url_for('index'))
    return render_template('hs_index.html')


# HS Stacking: API endpoint
@app.route('/fetch-hs-verifications', methods=['POST'])
def fetch_hs_verifications():
    if not session.get('authenticated'):
        return jsonify({"success": False, "error": "Unauthorized"}), 401

    try:
        hs_code = request.json.get('hsCode', '8501512020')
        origin = request.json.get('origin', 'CN')
        destination = request.json.get('destination', 'US')

        if not hs_code or not origin or not destination:
            return jsonify({"success": False, "error": "HS Code, Origin, and Destination are required"}), 400

        api_url = f"{HS_API_BASE_URL}/{hs_code.replace('.', '')}/{origin}/{destination}"
        logger.debug(f"Calling HS API: {api_url}")

        headers = {
            "Authorization": f"Bearer {API_TOKEN}",
            "Content-Type": "application/json"
        }

        response = session_requests.get(api_url, headers=headers, timeout=10)

        if response.status_code == 200:
            logger.debug("GET request successful")
            response_data = response.json()
        elif response.status_code == 405:
            logger.debug("GET failed with 405, attempting POST")
            payload = {}
            response = session_requests.post(api_url, json=payload, headers=headers, timeout=10)
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

        hs_code_duties = find_full_hs_codes_and_duties(response_data)

        if not hs_code_duties:
            logger.error("No full HS codes found in response")
            return jsonify({
                "success": False,
                "error": "No full HS codes found in response"
            }), 404

        all_stackable_codes = []
        tariff_priority_rules = {
            '9903.88.01': 1,
            '9903.01.25': 2,
            '9903.94.05': 3
        }

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

    except RequestException as e:
        logger.error(f"Network error: {str(e)}")
        return jsonify({"success": False, "error": f"Network error: {str(e)}"}), 500
    except Exception as e:
        logger.error(f"Unexpected error: {str(e)}")
        return jsonify({"success": False, "error": f"Unexpected error: {str(e)}"}), 500


def order_stackable_hts_codes(primary_hts, chapter_98_codes, exemption_codes, chapter_99_tariff_codes,
                              tariff_priority_rules, is_auto_part=False):
    ordered_hts_codes = []

    if primary_hts and is_valid_hts_code(primary_hts):
        ordered_hts_codes.append({
            'code': primary_hts,
            'desc': 'Primary HTS Code',
            'dutyRate': ''
        })
    else:
        logger.error("Invalid or missing primary HTS code")
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


def sort_chapter_99_codes(chapter_99_tariff_codes, tariff_priority_rules):
    return sorted(chapter_99_tariff_codes, key=lambda x: tariff_priority_rules.get(x['code'], 999))


def is_valid_hts_code(code):
    import re
    return bool(re.match(r'^\d{8,10}(\.\d{2})?$|^9903\.\d{2}\.\d{2}$|^98\d{2}\.\d{2}\.\d{2}$', code))


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


@app.route('/pga-flags')
def pga_flags():
    if not session.get('authenticated'):
        return redirect(url_for('index'))
    return render_template('pga_index.html')


@app.route('/codes')
@app.route('/codes.html')
def codes_page():
    if not session.get('authenticated'):
        return redirect(url_for('index'))
    return render_template('codes.html')


@app.route('/list-pga-options')
def list_pga_options():
    if not session.get('authenticated'):
        return jsonify({"success": False, "error": "Unauthorized"}), 401
    try:
        if not os.path.exists(os.path.join(DATA_DIR, "PGA_codes.xlsx")):
            return jsonify({"success": False, "error": "PGA_codes.xlsx not found"}), 500
        df = pd.read_excel(os.path.join(DATA_DIR, "PGA_codes.xlsx"), dtype=str).fillna("")
        return {
            "success": True,
            "agencyCode": sorted(df["Agency Code"].dropna().unique()),
            "code": sorted(df["Code"].dropna().unique()),
            "programCode": sorted(df["Program Code"].dropna().unique())
        }
    except Exception as e:
        return jsonify({"success": False, "error": f"Error reading PGA_codes.xlsx: {str(e)}"}), 500


@app.route('/codes-data')
def codes_data():
    if not session.get('authenticated'):
        return jsonify({"success": False, "error": "Unauthorized"}), 401
    try:
        if not os.path.exists(os.path.join(DATA_DIR, "PGA_codes.xlsx")):
            return jsonify({"success": False, "error": "PGA_codes.xlsx not found"}), 500
        agency = request.args.get('agency')
        code = request.args.get('code')
        program = request.args.get('program')

        df = pd.read_excel(os.path.join(DATA_DIR, "PGA_codes.xlsx"), dtype=str)
        df.columns = df.columns.str.strip()

        for col in ["Agency Code", "Code", "Program Code"]:
            df[col] = df[col].astype(str).str.strip()

        if agency:
            df = df[df["Agency Code"].str.strip().str.lower() == agency.strip().lower()]
        if code:
            df = df[df["Code"].str.strip().str.lower() == code.strip().lower()]
        if program:
            df = df[df["Program Code"].str.strip().str.lower() == program.strip().lower()]

        return jsonify({"success": True, "data": df.to_dict(orient="records")})
    except Exception as e:
        return jsonify({"success": False, "error": f"Error processing codes data: {str(e)}"}), 500


@app.route('/lookup-upc', methods=['POST'])
def lookup_upc():
    if not session.get('authenticated'):
        return jsonify({"success": False, "error": "Unauthorized"}), 401

    upc = request.json.get('upc')
    if not upc:
        return jsonify({"success": False, "error": "UPC is required"}), 400

    if not BARCODE_API_KEY:
        return jsonify({"success": False, "error": "Barcode API key not set"}), 500

    url = f"https://api.barcodelookup.com/v3/products?key={BARCODE_API_KEY}&barcode={upc}"
    try:
        r = requests.get(url, timeout=10)
        r.raise_for_status()
        p = r.json().get("products", [{}])[0]
        return {
            "success": True,
            "name": p.get("product_name", ""),
            "brand": p.get("brand", ""),
            "description": p.get("description", ""),
            "image": p.get("images", [""])[0]
        }
    except Exception as e:
        return jsonify({"success": False, "error": f"Error: {str(e)}"}), 502


@app.route('/fetch-pga-flags', methods=['POST'])
def fetch_pga_flags():
    if not session.get('authenticated'):
        return jsonify({"success": False, "error": "Unauthorized"}), 401

    try:
        hs_code = request.json.get('hsCode')
        name = request.json.get('name')
        description = request.json.get('description')

        if not hs_code:
            return jsonify({"success": False, "error": "HS Code is required"}), 400

        target = hs_code
        chapter_key = target[:2].zfill(2)

        required_files = [
            "HS_Chapters_lookup.xlsx",
            "PGA_HTS.xlsx",
            "PGA_codes.xlsx",
            "hs_codes.xlsx"
        ]
        for file in required_files:
            if not os.path.exists(os.path.join(DATA_DIR, file)):
                return jsonify({"success": False, "error": f"Missing required file: {file}"}), 500

        df_chapters = pd.read_excel(os.path.join(DATA_DIR, "HS_Chapters_lookup.xlsx"))
        df_chapters["Chapter"] = df_chapters["Chapter"].astype(str).str.zfill(2)
        df_chapters = df_chapters.ffill().bfill()
        chapters = df_chapters[df_chapters["Chapter"] == chapter_key].dropna(axis=1, how="all").to_dict("records")

        df_hts = pd.read_excel(os.path.join(DATA_DIR, "PGA_HTS.xlsx"), dtype=str).rename(
            columns={"HTS Number - Full": "HsCode"})
        df_hts.columns = df_hts.columns.str.strip()

        df_pga = pd.read_excel(os.path.join(DATA_DIR, "PGA_codes.xlsx"), dtype=str).replace("", pd.NA)
        df_pga.columns = df_pga.columns.str.strip()

        key_cols_hts = ["PGA Name Code", "PGA Flag Code", "PGA Program Code"]
        key_cols_pga = ["Agency Code", "Code", "Program Code"]

        for col in key_cols_hts:
            df_hts[col] = df_hts[col].astype(str).str.strip()
        for col in key_cols_pga:
            df_pga[col] = df_pga[col].astype(str).str.strip()

        pga_merged = df_hts.merge(
            df_pga,
            how="left",
            left_on=key_cols_hts,
            right_on=key_cols_pga,
            suffixes=("", "_pga")
        )

        pga_hts_columns = [
            "PGA Name Code", "PGA Flag Code", "PGA Flag", "PGA Program Code",
            "HsCode", "HTS Long Description", "Effective Begin Date",
            "Effective End Date", "HTS Update Date",
            "Change Pending Status Code", "Change Pending Status"
        ]
        pga_hts = (
            pga_merged[pga_merged["HsCode"] == target]
            [pga_hts_columns]
            .dropna(axis=1, how="all")
            .to_dict("records")
        )

        pga_sections_columns = [
            "R= Required\n M = May be required",
            "Tariff Flag Code Definition",
            "PGA Compliance Message (see final in shared google drive) ",
            "Summary of Requirements",
            "Conditions to Disclaim",
            "List of Documents Required",
            "Links to Example Documents",
            "Applicable HTS Codes",
            "Guidance",
            "Link to Disclaimer Form Template",
            "CFR Link",
            "Website Link"
        ]

        matched_pga_info = df_pga.merge(
            df_hts[df_hts["HsCode"] == target][key_cols_hts],
            how="inner",
            left_on=key_cols_pga,
            right_on=key_cols_hts
        )

        pga_sections = {}
        for col in pga_sections_columns:
            items = matched_pga_info[col].dropna().astype(
                str).str.strip().unique().tolist() if col in matched_pga_info else []
            if items:
                pga_sections[col.strip()] = items

        xl = pd.ExcelFile(os.path.join(DATA_DIR, "hs_codes.xlsx"))
        chapter_tab_names = [f"HTS Chapter {int(chapter_key)}", f"Chapter {int(chapter_key)}"]
        sheet_name = next((name for name in chapter_tab_names if name in xl.sheet_names), None)

        if sheet_name:
            df_rules = xl.parse(sheet_name)
            df_rules["HsCode"] = df_rules["HsCode"].astype(str)
            df_rules["Chapter"] = df_rules["HsCode"].str[:2].str.zfill(2)
            df_rules["Header"] = df_rules["HsCode"].str[:4]
            hs_rules = df_rules[df_rules["HsCode"].str.startswith(target)]
            if hs_rules.empty:
                hs_rules = df_rules[df_rules["HsCode"].str.startswith(target[:4])]
            if hs_rules.empty:
                hs_rules = df_rules[df_rules["Chapter"] == chapter_key]
            hs_rules = hs_rules.dropna(axis=1, how="all").to_dict("records")
        else:
            hs_rules = []

        return {
            "success": True,
            "hs_chapters": chapters,
            "pga_hts": pga_hts,
            "pga_sections": pga_sections,
            "hs_rules": hs_rules,
            "pga_requirements": [],
            "disclaimer": "Sources: ACE Agency Tariff Code Reference Guide (March 5, 2024), ACE Appendix PGA (December 12, 2024), Federal Register notices (e.g., CPSC expansion, September 9, 2024)"
        }

    except Exception as e:
        return jsonify({"success": False, "error": f"Unexpected error: {str(e)}"}), 500


if __name__ == '__main__':
    port = int(os.getenv('PORT', 5000))
    app.run(host='0.0.0.0', port=port, debug=True)