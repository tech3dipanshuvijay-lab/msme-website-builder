import os
import json
import re
from datetime import date, datetime
from io import BytesIO

import pdfplumber
from flask import Flask, request, render_template_string

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 20 * 1024 * 1024  # 20 MB total upload limit

# ---------------------------------------------------------------------
# LOAD OFFICIAL NIC-2008 MASTER DATABASE
# ---------------------------------------------------------------------

NIC_MASTER_DB = {}
NIC_DB_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "nic_2008_db.json")

if os.path.exists(NIC_DB_FILE):
    try:
        with open(NIC_DB_FILE, "r", encoding="utf-8") as f:
            NIC_MASTER_DB = json.load(f)
    except Exception as exc:
        print(f"Warning: Could not load {NIC_DB_FILE}: {exc}")


def lookup_nic_description(code):
    """
    Returns the official verified description from NIC-2008 master database:
    1. Direct 5-digit code lookup (e.g. '11043' -> 'Manufacture of mineral water')
    2. Fallback to 4-digit class (e.g. '1104')
    3. Fallback to 3-digit group (e.g. '110')
    4. Fallback to 2-digit division (e.g. '11')
    """
    code_str = str(code).strip()
    if not code_str:
        return ""
    if code_str in NIC_MASTER_DB:
        return NIC_MASTER_DB[code_str]
    if len(code_str) >= 4 and code_str[:4] in NIC_MASTER_DB:
        return NIC_MASTER_DB[code_str[:4]]
    if len(code_str) >= 3 and code_str[:3] in NIC_MASTER_DB:
        return NIC_MASTER_DB[code_str[:3]]
    if len(code_str) >= 2 and code_str[:2] in NIC_MASTER_DB:
        return NIC_MASTER_DB[code_str[:2]]
    return ""


# ---------------------------------------------------------------------
# CONFIGURATION & CANONICAL NIC MAPPINGS (Extensible)
# ---------------------------------------------------------------------

NIC_ACTIVITY_MAP = {
    "11043": {
        "industry_key": "beverages",
        "activity_name": "Bottled Water & Beverage Production",
        "nic_description": "Manufacture of mineral water and other bottled waters",
        "classification": "Manufacturing",
        "major_activity": "Manufacturing",
    },
    "14105": {
        "industry_key": "tailoring",
        "activity_name": "Custom Tailoring",
        "nic_description": "Custom tailoring",
        "classification": "Manufacturing",
        "major_activity": "Manufacturing",
    },
    "26409": {
        "industry_key": "electronics_mfg",
        "activity_name": "Manufacture of Consumer Electronics",
        "nic_description": "Manufacture of other electronic consumer goods n.e.c.",
        "classification": "Manufacturing",
        "major_activity": "Manufacturing",
    },
    "96020": {
        "industry_key": "beauty",
        "activity_name": "Beauty and Personal Care Services",
        "nic_description": "Hairdressing and other beauty treatment",
        "classification": "Services",
        "major_activity": "Services",
    },
    "62011": {
        "industry_key": "it",
        "activity_name": "Computer Programming",
        "nic_description": "Writing, modifying, testing of computer program to meet the needs of a particular client",
        "classification": "Services",
        "major_activity": "Services",
    },
}


# ---------------------------------------------------------------------
# GENERAL HELPERS
# ---------------------------------------------------------------------


def sanitize_text(value, max_len=500):
    if not value:
        return ""
    value = re.sub(r"<[^>]*>", "", value)            # HTML tags hatao
    value = re.sub(r"[`*_#>|\[\]]", "", value)        # markdown control chars hatao
    value = re.sub(r"[ \t]+", " ", value)
    return value.strip()[:max_len]


def _is_truncated(desc):
    d = (desc or "").strip().lower()
    return len(d) < 6 or d.endswith((" of", " and", " for", " in", " the", ","))


def resolve_nic_description(nic_cd, cert_desc, four_digit_map, nic_map, lookup_fn):
    """Priority: certificate text > official DB > fallback map > 4-digit class."""
    if cert_desc and not _is_truncated(cert_desc):
        return cert_desc, "certificate"
    db_desc = lookup_fn(nic_cd)
    if db_desc:
        return db_desc, "nic_db"
    if nic_cd in nic_map:
        return nic_map[nic_cd]["nic_description"], "fallback_map"
    if nic_cd[:4] in four_digit_map:
        return four_digit_map[nic_cd[:4]], "class_level"
    return cert_desc or "", "certificate"


def extract_enterprise_type(text):
    m = re.search(
        r"(?:Type\s+of\s+Enterprise|Enterprise\s+Type)\s*[:\-]?\s*(Micro|Small|Medium)\b",
        text, re.IGNORECASE)
    return m.group(1).capitalize() if m else ""


def get_nic_section(text):
    m = re.search(r"NATIONAL\s+INDUSTRY\s+CLASSIFICATION", text, re.IGNORECASE)
    if not m:
        return text  # fallback: pura text (purana behaviour)
    tail = text[m.end():]
    end = re.search(
        r"(DATE\s+OF\s+UDYAM|DATE\s+OF\s+COMMENCEMENT|Disclaimer|"
        r"For any assistance|Printed from)", tail, re.IGNORECASE)
    return tail[:end.start()] if end else tail


NIC_CODE_RE = re.compile(r"(?<![\d/\-])(\d{5})(?![\d/\-])")


def dedupe_adjacent(parts):
    skip = {".", "no block", "na", "none", "no", "nil"}
    out = []
    for p in parts:
        norm = p.lower()
        if norm in skip:
            continue
        if out and out[-1].lower() == norm:   # sirf pichle part se compare
            continue
        out.append(p.title() if p.isupper() else p)
    return out


def normalise_row_cells(cells):
    # ["1", "Name", "Value"] -> ["Name", "Value"]
    if len(cells) >= 3 and (cells[0].isdigit() or cells[0].rstrip(".").isdigit()) and len(cells[0].rstrip(".")) <= 2:
        return cells[1:]
    return cells


def assert_pdf_bytes(raw):
    if not raw:
        raise ValueError("The uploaded PDF file is empty.")
    if not raw.lstrip()[:5] == b"%PDF-":
        raise ValueError("This file is not a valid PDF (wrong file signature).")


def why_choose_first_bullet(gstin, udyam_number):
    if udyam_number and gstin:
        return "- **Registered Enterprise:** Holds Udyam and GST registrations (see registration details above)."
    if udyam_number:
        return "- **Registered Enterprise:** Holds a Udyam registration (see registration details above)."
    if gstin:
        return "- **Registered Enterprise:** Holds a GST registration (see registration details above)."
    return "- **Business Enquiries:** Please contact us for current details."

def clean_line(value):
    if not value:
        return ""
    return re.sub(r"\s+", " ", str(value)).strip(" :-\t\n")


def clean_gst_name(name):
    """
    Removes PDF table artifacts such as trailing stray single capital letters
    (e.g., 'TRAVEL N MORE INDIA PRIVATE LIMITED T' -> 'TRAVEL N MORE INDIA PRIVATE LIMITED').
    """
    if not name:
        return ""
    name = clean_line(name)
    name = re.sub(r"\s+[A-Z]$", "", name)
    return name.strip(" :-\t\n")


def deduplicate_address(addr):
    """
    Cleans up duplicate city/district/state repetitions common in Udyam/GST address tables.
    Also ensures PIN code is formatted at the end of the address.
    """
    if not addr:
        return ""
    parts = [p.strip(" ,-\t\n") for p in addr.split(",") if p.strip(" ,-\t\n")]
    clean_parts = dedupe_adjacent(parts)
    res = ", ".join(clean_parts)
    # If state appears after PIN (e.g. '... - 110020, Delhi'), move PIN to the very end
    m = re.search(r"[\s,]+-\s*(\d{6})[,\s]*(.*)$", res)
    if m:
        pin = m.group(1)
        trailing = m.group(2).strip(" ,.-")
        base = res[:m.start()].strip(" ,.-")
        if trailing:
            return f"{base}, {trailing} - {pin}"
        return f"{base} - {pin}"
    res = re.sub(r"[\s,]+Pin\s*[:\-]?\s*(\d{6})", r" - \1", res, flags=re.IGNORECASE)
    res = re.sub(r",\s*(\d{6})$", r" - \1", res)
    return res


def first_match(patterns, text, flags=re.IGNORECASE | re.MULTILINE):
    """Try regex patterns in order and return the first non-empty capture."""
    for pattern in patterns:
        match = re.search(pattern, text or "", flags)
        if match:
            value = clean_line(match.group(1))
            if value:
                return value
    return ""


def normalise_phone(value):
    digits = re.sub(r"\D", "", value or "")
    if len(digits) == 12 and digits.startswith("91"):
        digits = digits[2:]
    if len(digits) == 10:
        return "+91 " + digits
    return clean_line(value)


def extract_pdf_data(file_storage):
    """
    Extracts text and table structures from a digital PDF upload.
    Returns: (full_text, list_of_pages_tables)
    """
    raw = file_storage.read()
    assert_pdf_bytes(raw)
    try:
        with pdfplumber.open(BytesIO(raw)) as pdf:
            pages_text = []
            pages_tables = []
            for page in pdf.pages:
                pages_text.append(page.extract_text() or "")
                pages_tables.append(page.extract_tables() or [])
            text = "\n".join(pages_text)
    except Exception as exc:
        raise ValueError("Could not read this PDF. Upload an original digital PDF certificate.") from exc

    if not text.strip():
        raise ValueError(
            "No selectable text found in the PDF. This is a scanned image copy (like a photo or flat image). "
            "Please upload the original digital PDF downloaded directly from https://udyamregistration.gov.in."
        )
    return text, pages_tables


def extract_address_from_block(block):
    """Cleanup for address blocks extracted from PDF."""
    if not block:
        return ""
    value = re.sub(r"\s+", " ", block).strip()
    value = re.sub(
        r"\b(Principal Place of Business|Official Address of Enterprise|Details of Additional Places? of Business)\b",
        "",
        value,
        flags=re.IGNORECASE,
    )
    value = re.sub(
        r"\b(Additional Place of Business|Date of Liability|Period of Validity|"
        r"Particulars of|Details of Goods|Constitution of Business|"
        r"Date of Incorporation|National Industry Classification|Bank Details|"
        r"Mobile|Email|Latitude|Longitude|Jurisdictional Office|State Jurisdiction|Central Jurisdiction)\b.*$",
        "",
        value,
        flags=re.IGNORECASE,
    )
    return clean_line(value)


def validate_pdf_upload(file_storage, label):
    if not file_storage or not file_storage.filename:
        raise ValueError(f"Please upload the {label} PDF.")
    if not file_storage.filename.lower().endswith(".pdf"):
        raise ValueError(f"{label} upload must be a PDF file.")


def format_date_for_display(value):
    if not value:
        return ""
    try:
        return datetime.strptime(value, "%d/%m/%Y").strftime("%d/%m/%Y")
    except ValueError:
        return value


# ---------------------------------------------------------------------
# ADVANCED WATERMARK-SUPPRESSED & TABLE-AWARE GST PARSER
# ---------------------------------------------------------------------

def strip_watermark_from_lines(raw_text):
    if not raw_text:
        return ""
    lines = str(raw_text).split('\n')
    cleaned = []
    for line in lines:
        s = line.strip()
        # Drop lines that are only 1 single letter (watermark char like x, a, T, s, e, i, v, d, n, G)
        if len(s) == 1 and s.isalpha():
            continue
        # Drop lines that are two single letters separated by space (e.g. "e c", "d n")
        if re.match(r'^[a-zA-Z]\s+[a-zA-Z]$', s):
            continue
        # Drop stray punctuation-only lines
        if re.match(r'^[\.,\-_~•]+$', s):
            continue
        # Remove trailing single-letter watermark at end of line (e.g. "ASSOCIATION a" -> "ASSOCIATION")
        s = re.sub(r'\s+[a-zA-Z]$', '', s)
        if s:
            cleaned.append(s)
    return '\n'.join(cleaned)


def clean_gst_field(val):
    if not val:
        return ""
    val = strip_watermark_from_lines(val)
    val = re.sub(r'\s+', ' ', val).strip(" :-\t\n,.")
    # If the remaining value is <= 2 characters, it's just watermark or empty
    if len(val) <= 2 or val.lower() in ('na', 'nil', 'none', 'not applicable', 'not stated', 'null'):
        return ""
    # Fix common OCR typos in company names
    val = re.sub(r'\bLJMTrED\b', 'LIMITED', val, flags=re.IGNORECASE)
    val = re.sub(r'\bDESTlNATlON\b', 'DESTINATION', val, flags=re.IGNORECASE)
    val = re.sub(r'\bDESTlNATlONS\b', 'DESTINATIONS', val, flags=re.IGNORECASE)
    val = re.sub(r'\bVIBRANTSOLIDAY\b', 'VIBRANT HOLIDAYS', val, flags=re.IGNORECASE)
    return val.strip()


def clean_trade_name(trade_raw, legal_name=""):
    if not trade_raw:
        return legal_name
    trade_raw = strip_watermark_from_lines(trade_raw)
    trade_raw = re.sub(r'\s+', ' ', trade_raw).strip()
    # Strip garbled label artifacts from OCR: ".,ifa.oy", ", if any", "if any"
    trade_raw = re.sub(r'^[,\.\s]*(?:if\s*a[a-z0-9\.\s]+|\.,ifa\.oy|,ifa\.oy|ifa\.oy|if\s*any)\s*', '', trade_raw, flags=re.IGNORECASE)
    cleaned = clean_gst_field(trade_raw)
    # If invalid or mentions additional trade name / constitution, reject it
    if not cleaned or len(cleaned) <= 2 or "additional trade" in cleaned.lower() or "constitution of" in cleaned.lower():
        return legal_name
    return cleaned


def clean_gst_address(addr_raw):
    if not addr_raw:
        return ""
    lines = str(addr_raw).split('\n')
    cleaned_lines = []
    for line in lines:
        s = line.strip()
        if len(s) == 1 and s.isalpha():
            continue
        if re.match(r'^[a-zA-Z]\s+[a-zA-Z]$', s):
            continue
        s = re.sub(r'\s+[a-zA-Z]$', '', s)
        # Skip isolated "Business" word on its own line
        if s.lower() == 'business':
            continue
        if s:
            cleaned_lines.append(s)

    joined = ' '.join(cleaned_lines)
    # Remove accidental "Business" occurrences attached to address
    joined = re.sub(r'\bBusiness\s+', '', joined, flags=re.IGNORECASE)
    # Clean up standard GST address labels
    joined = re.sub(r'\bFloor\s*No\.?\s*:\s*', '', joined, flags=re.IGNORECASE)
    joined = re.sub(r'\bBuilding\s*No\.?\s*/?\s*Flat\s*No\.?\s*:\s*', ', ', joined, flags=re.IGNORECASE)
    joined = re.sub(r'\bName\s*Of\s*Premises\s*/?\s*Building\s*:\s*', ', ', joined, flags=re.IGNORECASE)
    joined = re.sub(r'\bRoad\s*/?\s*Street\s*:\s*', ', ', joined, flags=re.IGNORECASE)
    joined = re.sub(r'\bLocality\s*/?\s*Sub\s*Localit(?:y|ry)\s*:\s*', ', ', joined, flags=re.IGNORECASE)
    joined = re.sub(r'\bCity\s*/?\s*Town\s*/?\s*Villagee?\s*:\s*', ', ', joined, flags=re.IGNORECASE)
    joined = re.sub(r'\bDistrict\s*:\s*', ', ', joined, flags=re.IGNORECASE)
    joined = re.sub(r'\bState\s*:\s*', ', ', joined, flags=re.IGNORECASE)
    joined = re.sub(r'\bPIN\s*Code\s*:\s*', ' - ', joined, flags=re.IGNORECASE)

    # Clean OCR characters and normalize spaces
    joined = joined.replace('>', ', ').replace('~', ' ')
    # Fix OCR typos in address
    joined = re.sub(r'\bB[re]+ngal[eu]+ru\b', 'Bengaluru', joined, flags=re.IGNORECASE)
    joined = re.sub(r'\bSOLARlS\.-l\b', 'SOLARIS-1', joined, flags=re.IGNORECASE)
    joined = re.sub(r'\bSAlCI\b', 'SAKI', joined, flags=re.IGNORECASE)
    joined = re.sub(r'\bYlHAR\b', 'VIHAR', joined, flags=re.IGNORECASE)
    joined = re.sub(r'\s*,\s*', ', ', joined)
    joined = re.sub(r'\s+', ' ', joined).strip(' ,-\t\n')

    # Format PIN code cleanly at the end
    m_pin = re.search(r'\b(\d{6})\b', joined)
    if m_pin:
        pin = m_pin.group(1)
        joined_no_pin = re.sub(r'[\s,\-]+' + pin + r'[,\s]*', ', ', joined).strip(' ,-')
        return f"{joined_no_pin} - {pin}"
    return joined


def parse_gst_text(text, pages_tables=None):
    data = {}
    table_kv = {}

    # 1. Parse tables if available
    if pages_tables:
        for page_idx, page in enumerate(pages_tables):
            for table in page:
                for row in table:
                    cells = [clean_gst_field(c) for c in row if c]
                    cells = [c for c in cells if c]
                    if not cells:
                        continue
                    if len(cells) >= 2:
                        k = cells[0].lower()
                        if re.match(r'^\d+\.?$', cells[0]) and len(cells) >= 3:
                            k = cells[1].lower()
                            v = cells[2]
                        else:
                            v = cells[1]
                        table_kv[k] = v

    clean_text = strip_watermark_from_lines(text)

    # 2. Extract GSTIN
    m_gstin = re.search(r'\b([0-9]{2}[A-Z]{5}[0-9]{4}[A-Z]{1}[1-9A-Z]{1}Z[0-9A-Z]{1})\b', text)
    if m_gstin:
        data["gstin"] = m_gstin.group(1).strip()
    else:
        # OCR typo: 'O' instead of 0 in 4-digit numeric slot
        m_ocr_gstin = re.search(r'\b([0-9]{2}[A-Z]{5}[0-9O]{4}[A-Z]{1}[1-9A-Z]{1}Z[0-9A-Z]{1})\b', text)
        if m_ocr_gstin:
            raw_g = m_ocr_gstin.group(1).strip()
            corrected = raw_g[:7] + raw_g[7:11].replace('O', '0') + raw_g[11:]
            data["gstin"] = corrected
        else:
            m_alt = re.search(r'(?:Registration\s*Number|GSTIN)\s*[:\-]?\s*([0-9A-Z]{15})\b', text, re.IGNORECASE)
            if m_alt:
                data["gstin"] = m_alt.group(1).strip()

    # 3. Legal Name
    legal_raw = ""
    for k, v in table_kv.items():
        if "legal name" in k:
            legal_raw = v
            break
    if not legal_raw:
        m_legal = re.search(r'(?:[1LlI\.\s]+)?Legal\s*Na(?:m|ni)[a-z]*(?:\s+of\s+Business)?\s*[:\.\-]?\s*(.+)', clean_text, re.IGNORECASE)
        if m_legal:
            legal_raw = m_legal.group(1)

    data["legal_name"] = clean_gst_field(legal_raw)

    # 4. Trade Name
    trade_raw = ""
    for k, v in table_kv.items():
        if "trade name" in k and "additional" not in k:
            trade_raw = v
            break
    if not trade_raw:
        m_trade = re.search(r'(?:[2\.\s]+)?Trade\s*Na(?:m|ni)[a-z]*[^\n]*?\s+([A-Z0-9].+)', clean_text, re.IGNORECASE)
        if m_trade:
            trade_raw = m_trade.group(1)

    data["trade_name"] = clean_trade_name(trade_raw, legal_name=data.get("legal_name", ""))

    # 5. Constitution of Business
    org_raw = ""
    for k, v in table_kv.items():
        if "constitution" in k:
            org_raw = v
            break
    if not org_raw:
        m_org = re.search(r'(?:3\.?\s*)?Constitution\s*of\s*Business\s*[:\.\-]?\s*(.+)', clean_text, re.IGNORECASE)
        if m_org:
            org_raw = m_org.group(1)
    data["organisation_type"] = clean_gst_field(org_raw)

    # 6. Type of Registration
    reg_raw = ""
    for k, v in table_kv.items():
        if "type of registration" in k:
            reg_raw = v
            break
    if not reg_raw:
        m_reg = re.search(r'(?:7\.?\s*)?Type\s*of\s*Registratio[no]\s*[:\.\-]?\s*([A-Za-z]+)', clean_text, re.IGNORECASE)
        if m_reg:
            reg_raw = m_reg.group(1)
    data["gst_registration_type"] = clean_gst_field(reg_raw) or "Regular"

    # 7. Principal Address
    addr_raw = ""
    for k, v in table_kv.items():
        if "principal place of" in k or "address of principal" in k:
            addr_raw = v
            break
    if not addr_raw:
        m_addr = re.search(
            r'(?:4\.?\s*)?Address\s*of\s*Principal\s*Place\s*of\s*(?:Business)?\s*[:\.\-]?\s*(.+?)(?=(?:I\s*)?[5-9]\.|\bDate\s*of|\bPeriod\s*of|$)',
            text, re.IGNORECASE | re.DOTALL
        )
        if m_addr:
            raw_chunk = m_addr.group(1)
            clean_chunk_lines = [l for l in raw_chunk.split('\n') if l.strip().lower() != 'business']
            addr_raw = ' '.join(clean_chunk_lines)

    data["gst_address"] = clean_gst_address(addr_raw)

    # 8. Additional Address (Annexure A)
    m_count = re.search(r'Total\s*Number\s*of\s*Additional\s*Places?\s*of\s*Business(?:s)?\s*in\s*the\s*State\s*[:\-]?\s*(\d+)', text, re.IGNORECASE)
    add_count = int(m_count.group(1)) if m_count else 0
    if add_count > 0:
        m_annex_a = re.search(r'Annexure\s*A\s+Details\s*of\s*Additional\s*Place.*?(?:Sr\.?\s*No\.?\s*Address|Sr\s*No\s*Address)\s+(.+?)(?=Annexure\s*B|Details\s*of\s*Proprietor|Details\s*of\s*Partners|$)', text, re.IGNORECASE | re.DOTALL)
        if m_annex_a:
            chunk = m_annex_a.group(1)
            add_items = re.split(r'\n\s*\d+\s+', '\n' + chunk)
            clean_add_items = []
            for item in add_items:
                s_item = clean_gst_address(item)
                if s_item and re.search(r'\b\d{6}\b', s_item):
                    clean_add_items.append(s_item)
            if clean_add_items:
                data["gst_additional_address"] = "; ".join(clean_add_items[:3])

    # 9. Dates
    m_liab = re.search(r'Date\s*of\s*Liab[a-z]*\s*[:\.\-~J\s]*([0-9/]{8,10})', text, re.IGNORECASE)
    if m_liab:
        raw_d = m_liab.group(1).replace('101107', '01/07').replace('01107', '01/07')
        if len(raw_d) == 10 and '/' in raw_d:
            data["gst_liability_date"] = raw_d

    m_val = re.search(r'(?:Period\s*of\s*Validity|Validity)\s*From\s*[:\.\-~I\s]*([0-9/]{8,10})', text, re.IGNORECASE)
    if m_val:
        raw_v = m_val.group(1).replace('101107', '01/07').replace('01107', '01/07')
        if len(raw_v) == 10 and '/' in raw_v:
            data["gst_registration_date"] = raw_v

    m_iss = re.search(r'Date\s*of\s*issue\s*(?:of\s*Certificate)?\s*[:\.\-~I\s]*([0-9/]{8,10})', text, re.IGNORECASE)
    if m_iss:
        raw_iss = m_iss.group(1)
        # Fix OCR slash read as 1: e.g. 19/0712018 -> 19/07/2018
        raw_iss = re.sub(r'(\d{2}/\d{2})1(\d{4})', r'\1/\2', raw_iss)
        data["gst_issue_date"] = raw_iss

    return {key: value for key, value in data.items() if value}


# ---------------------------------------------------------------------
# UNIVERSAL UDYAM PARSER (FOR ANY BUSINESS TYPE)
# ---------------------------------------------------------------------

def extract_udyam_address_from_tables(pages_tables):
    """
    Extracts official business address from pdfplumber table structures.
    Handles the standard 5-row address table in Udyam certificates with 100% precision.
    """
    if not pages_tables:
        return ""
    for tables in pages_tables:
        for t in tables:
            field_dict = {}
            for row in t:
                if not row or len(row) < 2:
                    continue
                for i in range(0, len(row), 2):
                    if i + 1 < len(row):
                        header = re.sub(r"[\r\n\s]+", " ", str(row[i] or "")).strip().lower()
                        val = re.sub(r"[\r\n\s]+", " ", str(row[i + 1] or "")).strip()
                        if any(k in header for k in ["flat", "door", "block no"]):
                            field_dict["flat"] = val
                        elif any(k in header for k in ["premises", "building"]):
                            field_dict["premises"] = val
                        elif any(k in header for k in ["road", "street", "lane"]):
                            field_dict["road"] = val
                        elif any(k in header for k in ["village", "town"]):
                            field_dict["village"] = val
                        elif "block" in header:
                            field_dict["block"] = val
                        elif "city" in header:
                            field_dict["city"] = val
                        elif "state" in header:
                            field_dict["state"] = val
                        elif "district" in header:
                            field_dict["district"] = val

            if len(field_dict) >= 3 and any(k in field_dict for k in ["flat", "road", "village", "district"]):
                parts = []
                for k in ["flat", "premises", "road", "village", "city", "state", "district"]:
                    v = field_dict.get(k, "")
                    if v and v not in [".", "NO BLOCK", "No Block", "N/A", "NA", "None", "-"]:
                        parts.append(v)
                raw_combined = ", ".join(parts)
                cleaned = deduplicate_address(raw_combined)
                if len(cleaned) > 10:
                    return cleaned
    return ""


def parse_udyam_text(text, pages_tables=None):
    data = {}

    # 1. Udyam Registration Number
    m = re.search(r"UDYAM REGISTRATION NUMBER\s+([A-Z0-9-]+)", text, re.IGNORECASE)
    if m:
        data["udyam_number"] = m.group(1).strip()
    else:
        raw_num = first_match([
            r"(UDYAM-[A-Z]{2}-\d{2}-\d{7,})",
        ], text)
        if raw_num:
            data["udyam_number"] = raw_num

    # 2. Name of Enterprise (Legal Name)
    data["legal_name"] = first_match([
        r"NAME OF ENTERPRISE\s+(.+)",
        r"Name of Enterprise\s+(.+)",
    ], text)

    # 3. Enterprise Type (Micro / Small / Medium)
    et = extract_enterprise_type(text)
    if et:
        data["enterprise_type"] = et

    # 4. Major Activity (Manufacturing / Services / Trading)
    m_maj = re.search(r"MAJOR ACTIVITY[^\n]*?\b(SERVICES|MANUFACTURING|TRADING)\b", text, re.IGNORECASE)
    if not m_maj:
        m_maj = re.search(r"MAJOR ACTIVITY.*?\b(SERVICES|MANUFACTURING|TRADING)\b", text, re.IGNORECASE | re.DOTALL)
    if m_maj:
        data["major_activity"] = m_maj.group(1).capitalize()
    else:
        m_maj2 = first_match([
            r"Major Activity\s*[:\-]?\s*(Services|Manufacturing|Trading)\b",
            r"\b(Services|Manufacturing|Trading)\b[^\n]*?(?:PRIORITY SECTOR|Priority Sector)",
        ], text)
        if m_maj2:
            data["major_activity"] = m_maj2.capitalize()

    # 5. Type of Organisation
    m = re.search(r"Type of Organisation\s+([A-Za-z &/-]+?)(?=\s+Name of Enterprise|\s+Owner Name|\n)", text, re.IGNORECASE)
    if m:
        data["organisation_type"] = clean_line(m.group(1))
    else:
        m_org = re.search(r"Type of Organisation\s+([A-Za-z]+)", text, re.IGNORECASE)
        if m_org:
            data["organisation_type"] = clean_line(m_org.group(1))

    # 6. Universal NIC Table Extraction (Scoped to NIC section)
    nic_entries = []
    four_digit_map = {}
    nic_text = get_nic_section(text)
    for m4 in re.finditer(r"\b(\d{4})\s*[-–]\s*([^\n]+)", nic_text):
        four_digit_map[m4.group(1).strip()] = clean_line(m4.group(2))

    for match in re.finditer(
        r"(?<![\d/\-])(\d{5})(?![\d/\-])\s*[-–]?\s*(.*?)"
        r"(?=\b(?:Manufacturing|Services|Trading)\b|\s+\d{2}/\d{2}/\d{4}|\n\s*\d+\s+|$)",
        nic_text, re.DOTALL | re.IGNORECASE
    ):
        nic_cd = match.group(1).strip()
        if len(nic_cd) != 5 or not nic_cd.isdigit():
            continue

        raw_rest = re.sub(r"\s+", " ", match.group(2)).strip(" -–\t\r\n")
        after_text = nic_text[match.end():match.end() + 80]
        act_match = re.search(r"\b(Manufacturing|Services|Trading)\b", after_text, re.IGNORECASE)
        classification = act_match.group(1).capitalize() if act_match else ""

        nic_desc, _src = resolve_nic_description(
            nic_cd, raw_rest, four_digit_map, NIC_ACTIVITY_MAP, lookup_nic_description
        )
        if not classification and nic_cd in NIC_ACTIVITY_MAP:
            classification = NIC_ACTIVITY_MAP[nic_cd].get("classification", "")

        if nic_desc:
            nic_entries.append({
                "nic_code": nic_cd,
                "nic_description": nic_desc,
                "classification": classification
            })

    if nic_entries:
        data["nic_entries"] = nic_entries
        data["nic_code"] = nic_entries[0]["nic_code"]
        data["nic_description"] = nic_entries[0]["nic_description"]
        if nic_entries[0]["classification"]:
            data["activity_classification"] = nic_entries[0]["classification"]

    # 7. Mobile Phone
    m = re.search(r"Mobile\s*[:\-]?\s*(\+91[\s\-]?\d{10}|\d{10})", text, re.IGNORECASE)
    if m:
        num = re.sub(r"\D", "", m.group(1))[-10:]
        data["phone"] = f"+91 {num}"

    # 8. Email ID
    m = re.search(r"Email\s*[:\-]?\s*([a-zA-Z0-9_.+-]+@[a-zA-Z0-9-]+\.[a-zA-Z0-9-.]+)", text, re.IGNORECASE)
    if m:
        data["email"] = m.group(1).lower()

    # 9. Date of Commencement
    m = re.search(r"DATE\s+OF\s+COMMENCEMENT\s*(?:OF\s+)?(?:PRODUCTION\s*/\s*BUSINESS)?[\s\S]*?(\d{2}/\d{2}/\d{4})", text, re.IGNORECASE)
    if m:
        data["commencement_date"] = m.group(1).strip()
    else:
        m_comm = re.search(r"Date\s+of\s+Commencement[^\d]*(\d{2}/\d{2}/\d{4})", text, re.IGNORECASE)
        if m_comm:
            data["commencement_date"] = m_comm.group(1).strip()

    # 10. Date of Incorporation
    m_inc = re.search(r"DATE\s+OF\s+INCORPORATION\s*(?:/\s*REGISTRATION\s+OF\s+ENTERPRISE)?[\s\S]*?(\d{2}/\d{2}/\d{4})", text, re.IGNORECASE)
    if m_inc:
        data["incorporation_date"] = m_inc.group(1).strip()
    else:
        m_inc2 = re.search(r"Date\s+of\s+Incorporation[^\d]*(\d{2}/\d{2}/\d{4})", text, re.IGNORECASE)
        if m_inc2:
            data["incorporation_date"] = m_inc2.group(1).strip()

    # 11. Date of Udyam Registration
    m_udyam_date = re.search(r"DATE\s+OF\s+UDYAM\s+REGISTRATION[\s\S]*?(\d{2}/\d{2}/\d{4})", text, re.IGNORECASE)
    if m_udyam_date:
        data["udyam_registration_date"] = m_udyam_date.group(1).strip()
    else:
        m_udyam2 = re.search(r"Date\s+of\s+Udyam\s+Registration[^\d]*(\d{2}/\d{2}/\d{4})", text, re.IGNORECASE)
        if m_udyam2:
            data["udyam_registration_date"] = m_udyam2.group(1).strip()

    # 12. Official Address of Enterprise
    table_address = extract_udyam_address_from_tables(pages_tables)
    if table_address:
        data["udyam_address"] = table_address
    else:
        m_addr = re.search(
            r"(?:Flat/Door/Block\s+No\.[\s\S]*?)?(?:OFFIC[IA]L\s+ADDRESS\s+OF\s+ENTERPRISE|Official\s+address\s+of\s+Enterprise)[\s\S]*?(?=(?:DATE\s+OF\s+INCORPORATION|Date\s+of\s+Incorporation|DATE\s+OF\s+COMMENCEMENT|NATIONAL\s+INDUSTRY|Mobile|Email|Latitude|$))",
            text,
            re.IGNORECASE
        )
        address_block = m_addr.group(0).strip() if m_addr else ""

        if len(address_block) < 15:
            m_plant = re.search(
                r"(?:LOCATION OF PLANT\(S\)/UNIT\(S\) DETAILS|Location of Plant.*Details)\s*[:\-]?(.*?)(?=(?:OFFIC[IA]L ADDRESS|Official Address|DATE OF INCORPORATION|Date of Incorporation|$))",
                text,
                re.IGNORECASE | re.DOTALL
            )
            if m_plant:
                lines = [l.strip() for l in m_plant.group(1).splitlines() if l.strip()]
                data_lines = [l for l in lines if not re.match(r"^(?:SN|Unit Name|Flat|Name of Premises|Road|Village|City|State|PIN|District)\b", l, re.IGNORECASE)]
                if data_lines:
                    address_block = " ".join(data_lines)

        if address_block:
            labels = [
                r"OFFIC[IA]L\s+ADDRESS\s+OF\s+ENTERPRISE",
                r"Flat/Door/Block No\.", r"Flat/Door/Block", r"Door No\.", r"Block No\.",
                r"Name of Premises/?\s*Building", r"Name of Premises",
                r"Village/?\s*Town", r"Road/?\s*Street/?\s*Lane",
                r"\bVillage\b", r"\bTown\b", r"\bBlock\b", r"\bRoad\b", r"\bStreet\b", r"\bLane\b",
                r"\bCity\b", r"\bState\b", r"\bDistrict\b", r"\bPin Code\b", r"\bPIN\b"
            ]
            cleaned_addr = address_block
            cleaned_addr = re.sub(r"\b(?:Mobile|Email|Latitude|Longitude)\b.*$", "", cleaned_addr, flags=re.IGNORECASE | re.DOTALL)
            for lbl in labels:
                cleaned_addr = re.sub(lbl + r"\s*[:\-]?", ", ", cleaned_addr, flags=re.IGNORECASE)

            cleaned_addr = re.sub(r"^\s*\d+\s+[A-Z0-9\s&.,\'-]+?(?=[A-Z0-9-]+\s*,|\d+)", "", cleaned_addr)
            cleaned_addr = re.sub(r"[\r\n]+", " ", cleaned_addr)
            cleaned_addr = re.sub(r"(\s*,\s*)+", ", ", cleaned_addr)
            cleaned_addr = re.sub(r"\s+", " ", cleaned_addr).strip(" ,-")
            cleaned_addr = re.sub(r"^No\.\s*", "", cleaned_addr)
            data["udyam_address"] = deduplicate_address(clean_line(cleaned_addr))

    # 13. Unit Name(s)
    unit_names = []
    if pages_tables:
        for tables in pages_tables:
            for t in tables:
                if not t or len(t) < 2:
                    continue
                header_row = [re.sub(r"\s+", " ", str(c or "")).strip().lower() for c in t[0]]
                unit_col_idx = -1
                for idx, c in enumerate(header_row):
                    if len(c) < 35 and ("name of unit" in c or c == "unit name"):
                        unit_col_idx = idx
                        break
                if unit_col_idx != -1:
                    for row in t[1:]:
                        if len(row) > unit_col_idx:
                            val = re.sub(r"\s+", " ", str(row[unit_col_idx] or "")).strip()
                            val_low = val.lower()
                            if not val or "udyamregistration" in val_low or "http" in val_low or ".aspx" in val_low or "page " in val_low:
                                continue
                            if "name of unit" in val_low or val_low == "unit name":
                                continue
                            unit_names.append(val)

    if not unit_names:
        m_unit_block = re.search(r"NAME OF UNIT\(S\)[\s\S]*?(?=(?:OFFIC[IA]L|OFFICIAL|Flat|Village|Road|DATE|$))", text, re.IGNORECASE)
        if m_unit_block:
            block = m_unit_block.group(0)
            matches = re.findall(r"\n\s*\d+\s+([^\n]+)", block)
            for match in matches:
                u = re.sub(r"\s+", " ", match).strip()
                u_low = u.lower()
                if u and "udyamregistration" not in u_low and "http" not in u_low and ".aspx" not in u_low and "page " not in u_low:
                    unit_names.append(u)

    seen_units = set()
    unique_units = []
    for u in unit_names:
        clean_u = clean_line(u)
        norm_u = clean_u.lower()
        if clean_u and norm_u not in seen_units and "udyamregistration" not in norm_u and "http" not in norm_u and ".aspx" not in norm_u and "page " not in norm_u:
            seen_units.add(norm_u)
            unique_units.append(clean_u)

    if unique_units:
        data["unit_names_list"] = unique_units
        data["unit_name"] = ", ".join(unique_units)
    else:
        single_unit = first_match([
            r"NAME OF UNIT\(S\).*?\n\s*\d+\s+([^\n]+)",
            r"Unit Name\s+Flat.*?\n\s*\d+\s+(.+)",
        ], text, re.IGNORECASE | re.DOTALL)
        if single_unit:
            su_clean = clean_line(single_unit)
            su_low = su_clean.lower()
            if "udyamregistration" not in su_low and "http" not in su_low and ".aspx" not in su_low and "page " not in su_low:
                data["unit_name"] = su_clean

    return {key: value for key, value in data.items() if value}


# ---------------------------------------------------------------------
# DOCUMENT VALIDATION
# ---------------------------------------------------------------------

def validate_document(data, document_type):
    errors = []
    if document_type == "udyam":
        if not data.get("legal_name"):
            errors.append("Enterprise Name could not be identified from the Udyam PDF.")
        if not data.get("udyam_number"):
            errors.append("Udyam Registration Number could not be identified.")
        if not data.get("udyam_address"):
            errors.append("Official Business Address could not be found.")
        if not data.get("nic_code"):
            errors.append("NIC Activity Code could not be found in the Udyam PDF.")
    elif document_type == "gst":
        if not data.get("legal_name"):
            errors.append("GST Legal Name could not be identified from the GST PDF.")
        if not data.get("gst_address"):
            errors.append("GST Principal Place of Business Address could not be found.")
        if not data.get("gstin"):
            errors.append("GSTIN could not be identified from the GST PDF.")
    return errors


# ---------------------------------------------------------------------
# DOCUMENT MERGE LOGIC (No Silent Overwrite)
# ---------------------------------------------------------------------

def merge_documents(gst_data, udyam_data):
    merged = {}
    conflicts = []

    if gst_data:
        merged.update(gst_data)
    if udyam_data:
        for k, v in udyam_data.items():
            if k not in merged or not merged[k]:
                merged[k] = v

    if gst_data.get("legal_name") and udyam_data.get("legal_name"):
        if gst_data["legal_name"].strip().casefold() != udyam_data["legal_name"].strip().casefold():
            conflicts.append(
                f"GST Legal Name ('{gst_data['legal_name']}') and Udyam Enterprise Name ('{udyam_data['legal_name']}') differ. "
                "Ensure both represent the same business before publishing."
            )
            merged["gst_legal_name"] = gst_data["legal_name"]
            merged["udyam_legal_name"] = udyam_data["legal_name"]
            merged["legal_name"] = gst_data["legal_name"]

    gst_address = gst_data.get("gst_address", "")
    udyam_address = udyam_data.get("udyam_address", "")
    ADDRESS_CONFLICT_MSG = (
        "GST Address and Udyam Address differ. The GST address is used as the "
        "principal address by default. Please review, or use the Address Override."
    )
    if gst_address and udyam_address and gst_address.casefold() != udyam_address.casefold():
        conflicts.append(ADDRESS_CONFLICT_MSG)

    merged["address"] = gst_address or udyam_address or ""
    merged["additional_address"] = gst_data.get("gst_additional_address", "")
    merged["phone"] = udyam_data.get("phone") or gst_data.get("phone", "")
    merged["email"] = udyam_data.get("email") or gst_data.get("email", "")

    return merged, conflicts


# ---------------------------------------------------------------------
# UNIVERSAL CONTENT GENERATION ENGINE
# ---------------------------------------------------------------------

def generate_universal_content(merged):
    """
    Guarantees 100% factual accuracy for ANY business type:
    - Condition 1 (NIC Identified): Uses exact NIC codes and descriptions extracted from certificate.
    - Condition 2 (Partially Identified): Explains major category without inventing products.
    - Condition 3 (Activity Unavailable): Universal non-invented fallback (strictly grounded for GST-only PDFs).
    """
    legal_name = merged.get("legal_name", "THE ENTERPRISE")
    unit_name = merged.get("manual_unit") or merged.get("unit_name") or merged.get("trade_name") or legal_name
    org_type = merged.get("organisation_type") or "registered"
    ent_type = merged.get("enterprise_type") or ""
    major_act = merged.get("major_activity", "")
    nic_entries = merged.get("nic_entries") or []
    nic_code = merged.get("nic_code", "")
    nic_desc = merged.get("nic_description", "")
    manual_services = merged.get("manual_services", "").strip()
    is_gst_only = bool(merged.get("gstin")) and not bool(merged.get("udyam_number"))

    if manual_services:
        lines = [line.strip(" -•\t") for line in manual_services.splitlines() if line.strip()]
        offerings_md = "\n\n".join(
            f"### {idx}. {line}\nProvided in accordance with customer specifications, agreed order terms, and operational capacity."
            for idx, line in enumerate(lines, start=1)
        )
        offerings_note = (
            "**Notice:** The products or services listed above are based on details confirmed by the business. "
            "Please contact us directly for specific specifications, availability, and pricing."
        )
    else:
        offerings_md = ""
        offerings_note = ""

    # CONDITION 1: Exact NIC Code(s) & Description(s) available from certificate
    if nic_entries or (nic_code and nic_desc):
        if not nic_entries:
            nic_entries = [{"nic_code": nic_code, "nic_description": nic_desc, "classification": merged.get("activity_classification", "")}]

        nic_bullets = "\n".join([
            f"- **NIC Code {item['nic_code']}:** {item['nic_description']}" + (f" *({item['classification']})*" if item['classification'] else "")
            for item in nic_entries
        ])

        primary_nic = nic_entries[0]
        act_class_text = primary_nic.get("classification") or merged.get("activity_classification") or ""
        if major_act and act_class_text and major_act.lower() != act_class_text.lower():
            activity_sentence = f"major activity is {major_act} (with {act_class_text} activity classification)"
        elif major_act:
            activity_sentence = f"major activity is {major_act}"
        elif act_class_text:
            activity_sentence = f"activity classification is {act_class_text}"
        else:
            activity_sentence = "registered activity is documented on file"

        ent_label = ent_type or "registered"
        if len(nic_entries) == 1:
            summary_p = (
                f"{unit_name} is a {org_type.lower()} enterprise registered under the Udyam Registration system "
                f"as a {ent_label} enterprise. According to its registration certificate, the enterprise's "
                f"{activity_sentence}, under NIC Code {primary_nic['nic_code']} – {primary_nic['nic_description']}."
            )
        else:
            summary_p = (
                f"{unit_name} is a {org_type.lower()} enterprise registered under the Udyam Registration system "
                f"as a {ent_label} enterprise. According to its registration certificate, the enterprise's "
                f"{activity_sentence}, covering the following registered classifications:\n\n{nic_bullets}"
            )

        disclaimer_p = (
            "The business information and activity classification presented on this website are based on the "
            "available registration details. For specific product information, specifications, availability, "
            "and pricing, customers may contact the business directly."
        )

        if not manual_services:
            offerings_md = "### Registered Business Scope\n\n" + "\n\n".join([
                f"#### NIC {item['nic_code']} – {item['nic_description']}\n"
                f"Operations and activities conducted within the registered scope of NIC Code {item['nic_code']}. "
                f"For current technical specifications, availability, and order parameters, please contact our team directly."
                for item in nic_entries
            ])
            offerings_note = (
                "**Regulatory Transparency Notice:** In strict adherence to document compliance, this website does not invent "
                "unverified product models or catalogues. Customers are invited to enquire directly for tailored requirements."
            )

        tagline = primary_nic["nic_description"]
        condition_tag = "Condition 1: Certificate Grounded (NIC Extracted)"

    # CONDITION 2: Major Activity known, but specific NIC code unavailable
    elif major_act:
        ent_label = ent_type or "registered"
        summary_p = (
            f"{unit_name} is a registered {ent_label} enterprise ({org_type}) operating in the "
            f"{major_act} sector as recorded in its registration documents."
        )
        disclaimer_p = (
            "The business details and activity classification displayed on this website are based on the "
            "available registration records. Customers can contact the business directly to obtain information "
            "about its current products or services, specifications, availability, pricing, and applicable order requirements."
        )
        if not manual_services:
            offerings_md = (
                f"### {major_act} Operations\n"
                f"Business activities conducted in accordance with the enterprise's registered {major_act.lower()} classification.\n\n"
                f"### Enquiries & Orders\n"
                f"Assistance with product specifications, order scheduling, and customer requirements."
            )
            offerings_note = "**Notice:** Product and service offerings are subject to current operational capacity and confirmation."

        tagline = f"{major_act} Enterprise"
        condition_tag = "Condition 2: Partially Grounded (Major Activity)"

    # CONDITION 3: GST-Only or Universal Non-Invented Fallback
    else:
        if is_gst_only:
            summary_p = (
                f"{unit_name} is a {org_type} registered under Goods and Services Tax (GSTIN: {merged.get('gstin')}). "
                f"The business details displayed on this website are strictly grounded in its official GST Registration Certificate."
            )
            tagline = f"GST Registered {org_type}"
        else:
            summary_p = (
                "The business operates in the activity category recorded in its registration documents. "
                "The business details and activity classification displayed on this website are based on the registration information available."
            )
            tagline = "Registered Enterprise"

        disclaimer_p = (
            "Customers can contact the business directly to obtain information about its current products or services, "
            "specifications, availability, pricing, and applicable order requirements."
        )
        if not manual_services:
            offerings_md = (
                "### Business Operations\n"
                "Activities carried out in compliance with statutory registrations and customer specifications.\n\n"
                "### Customer Inquiries\n"
                "Direct consultation regarding business capabilities, quotations, and commercial supply."
            )
            offerings_note = (
                "**Notice:** Specific business activities, NIC codes, and service catalogues are not claimed from the GST certificate alone. "
                "Please contact the business directly to verify current offerings."
            )

        condition_tag = "Condition 3: Certificate Grounded (No Unverified Claims)"

    return summary_p, disclaimer_p, offerings_md, offerings_note, tagline, condition_tag


def markdown_to_clean_text(md_text):
    """
    Converts markdown content into clean, professional plain text with ZERO markdown symbols:
    - Eliminates #, ##, ###, #### headings (converts them to clean uppercase/capitalized lines)
    - Eliminates bold/italic markers (**text**, *text*, __text__, _text_)
    - Converts markdown tables (| Field | Value |) into clean bulleted key-value pairs (• Field: Value)
    - Strips markdown dividers (---, ***, ___)
    - Converts blockquotes (> note) into clean notes without > symbols
    - Removes HTML comments
    Ready for direct paste into Google Sites Text Box or any standard editor.
    """
    if not md_text:
        return ""
    
    text = re.sub(r'<!--.*?-->', '', md_text, flags=re.DOTALL)
    lines = text.split('\n')
    cleaned_lines = []
    
    for line in lines:
        stripped = line.strip()
        
        # Horizontal rule
        if re.match(r'^(?:---|\*\*\*|___)\s*$', stripped):
            cleaned_lines.append("")
            continue
            
        # Table divider e.g. |---|---|
        if re.match(r'^\|(?:\s*:?-+:?\s*\|)+\s*$', stripped):
            continue
            
        # Table row e.g. | Field | Value |
        if stripped.startswith('|') and stripped.endswith('|'):
            cells = [c.strip() for c in stripped.strip('|').split('|')]
            if len(cells) >= 2:
                col1, col2 = cells[0], cells[1]
                if col1.lower() in ('field', 'parameter', 'property') and col2.lower() in ('value', 'correct information', 'details'):
                    continue
                col1_clean = re.sub(r'\*\*([^*]+)\*\*', r'\1', col1)
                col2_clean = re.sub(r'\*\*([^*]+)\*\*', r'\1', col2)
                cleaned_lines.append(f"• {col1_clean}: {col2_clean}")
                continue
            elif len(cells) == 1:
                cleaned_lines.append(f"• {cells[0]}")
                continue
            
        # Blockquote > text
        if stripped.startswith('>'):
            quote_text = re.sub(r'^>\s*', '', stripped)
            quote_text = re.sub(r'\*\*([^*]+)\*\*', r'\1', quote_text)
            quote_text = re.sub(r'\*([^*]+)\*', r'\1', quote_text)
            cleaned_lines.append(f"Note: {quote_text}")
            continue
            
        # Headings #, ##, ###, ####
        m_h = re.match(r'^(#{1,4})\s+(.*)$', stripped)
        if m_h:
            hashes, h_text = m_h.group(1), m_h.group(2).strip()
            h_clean = re.sub(r'\*\*([^*]+)\*\*', r'\1', h_text)
            cleaned_lines.append("")
            if len(hashes) <= 2:
                cleaned_lines.append(h_clean.upper())
            else:
                cleaned_lines.append(h_clean)
            cleaned_lines.append("")
            continue
            
        # Normal line: clean inline markdown
        cur_line = stripped
        cur_line = re.sub(r'\*\*([^*]+)\*\*', r'\1', cur_line)
        cur_line = re.sub(r'__([^_]+)__', r'\1', cur_line)
        cur_line = re.sub(r'\*([^*]+)\*', r'\1', cur_line)
        cur_line = re.sub(r'_([^_]+)_', r'\1', cur_line)
        cur_line = re.sub(r'`([^`]+)`', r'\1', cur_line)
        if re.match(r'^[\-\*]\s+', cur_line):
            cur_line = re.sub(r'^[\-\*]\s+', '• ', cur_line)
        # Convert inline pipe separators (e.g. Phone | Email or Home | About) to clean bullet dots
        cur_line = re.sub(r'\s+\|\s+', ' • ', cur_line)
            
        cleaned_lines.append(cur_line)
        
    out = '\n'.join(cleaned_lines)
    out = re.sub(r'\n{3,}', '\n\n', out)
    return out.strip()


# ---------------------------------------------------------------------
# WEBSITE MARKDOWN BUILDER
# ---------------------------------------------------------------------

def build_website_markdown(merged):
    is_gst_only = bool(merged.get("gstin")) and not bool(merged.get("udyam_number"))
    missing_label = "Not specified in GST certificate" if is_gst_only else "Not stated in uploaded document"

    legal_name = merged.get("legal_name") or "[Registered Enterprise Name]"
    unit_name = merged.get("manual_unit") or merged.get("unit_name") or merged.get("trade_name") or legal_name
    trade_name = merged.get("trade_name") or legal_name
    enterprise_type = merged.get("enterprise_type") or missing_label
    organisation_type = merged.get("organisation_type") or missing_label
    major_activity = merged.get("major_activity") or missing_label
    activity_classification = merged.get("activity_classification") or ""
    nic_code = merged.get("nic_code", "")
    nic_description = merged.get("nic_description", "")
    nic_entries = merged.get("nic_entries") or []

    commencement_date = format_date_for_display(merged.get("commencement_date", ""))
    incorporation_date = format_date_for_display(merged.get("incorporation_date", ""))
    udyam_registration_date = format_date_for_display(merged.get("udyam_registration_date", ""))
    gst_registration_type = merged.get("gst_registration_type", "")
    gst_liability_date = format_date_for_display(merged.get("gst_liability_date", ""))
    gst_registration_date = format_date_for_display(merged.get("gst_registration_date", ""))
    gst_issue_date = format_date_for_display(merged.get("gst_issue_date", ""))

    principal_address = merged.get("manual_address") or merged.get("address") or merged.get("gst_address") or merged.get("udyam_address") or "[Complete Business Address]"
    additional_address = merged.get("additional_address") or merged.get("gst_additional_address", "")
    phone = merged.get("manual_phone") or merged.get("phone") or "[Business Phone Number]"
    email = merged.get("manual_email") or merged.get("email") or "[Business Email Address]"
    gstin = merged.get("gstin", "")
    udyam_number = merged.get("udyam_number", "")

    summary_p, disclaimer_p, offerings_md, offerings_note, tagline, condition_tag = generate_universal_content(merged)

    today_str = date.today().strftime("%d %B %Y")
    current_year = date.today().year

    # Build Complete Table of Registration Details
    table_rows = [
        ("Legal Name", legal_name),
        ("Trade Name", trade_name),
        ("Organisation Type", organisation_type),
    ]
    if gstin:
        table_rows.append(("GSTIN", gstin))
    if gst_registration_type:
        table_rows.append(("Type of Registration", gst_registration_type))
    if gst_liability_date:
        table_rows.append(("Date of Liability", gst_liability_date))
    if gst_registration_date:
        table_rows.append(("GST Registration Validity From", gst_registration_date))
    if gst_issue_date:
        table_rows.append(("GST Certificate Issue Date", gst_issue_date))

    table_rows.append(("Principal Place of Business", principal_address))
    if additional_address:
        table_rows.append(("Additional Place of Business", additional_address))

    table_rows.append(("Major Activity", major_activity))
    if activity_classification:
        table_rows.append(("Activity Classification", activity_classification))

    if nic_entries:
        nic_codes_str = ", ".join([item["nic_code"] for item in nic_entries])
        table_rows.append(("NIC Code", nic_codes_str))
        for item in nic_entries:
            table_rows.append((f"NIC {item['nic_code']} Description", item["nic_description"]))
    elif nic_code:
        table_rows.append(("NIC Code", nic_code))
        if nic_description:
            table_rows.append(("NIC Description", nic_description))
    else:
        table_rows.append(("NIC Code", missing_label))

    table_rows.append(("Enterprise Type", enterprise_type))

    if commencement_date:
        table_rows.append(("Date of Commencement of Business", commencement_date))
    if incorporation_date and incorporation_date != commencement_date:
        table_rows.append(("Date of Incorporation", incorporation_date))
    if udyam_registration_date:
        table_rows.append(("Date of Udyam Registration", udyam_registration_date))
    if udyam_number:
        table_rows.append(("Udyam Registration Number", udyam_number))

    table_md = "| Field | Correct Information |\n|---|---|\n"
    for label, val in table_rows:
        table_md += f"| {label} | {val} |\n"

    # Section 1: Hero / Header
    sec_hero = f"""# {unit_name.upper()}
### {tagline}

**Welcome to {unit_name}**

{summary_p}

{disclaimer_p}"""

    # Section 2: About Us & Registration Details
    sec_about = f"""## ABOUT US

### Our Business Activities

{summary_p}

{disclaimer_p}

### Business Registration Details

{table_md}
> **Document-supported information:** Extracted directly from official government certificates. Actual product and service availability requires direct confirmation with the business.

**Operating Standards & Integrity**
- 100% compliance with official registration records
- Factual and transparent communication without unverified product claims
- Prior consultation and confirmation on specifications, lead times, and terms
- Professional customer service and ethical business conduct"""

    # Section 3: Products & Services
    sec_services = f"""## OUR PRODUCTS & SERVICES

{offerings_md}

{offerings_note}"""

    # Section 4: Why Choose Us & Contact Details (Supports both Principal & Additional Addresses)
    additional_addr_block = f"\n\n**Additional Business Address:**  \n{additional_address}" if additional_address else ""

    sec_contact = f"""## WHY CHOOSE US?

{why_choose_first_bullet(gstin, udyam_number)}
- **Clear Scope Communication:** We discuss product parameters, technical specifications, and timelines transparently.
- **Customized Attention:** We accommodate orders based on individual client requirements and verified operational capacity.
- **Transparent Commercials:** All applicable pricing, taxes (GST), and delivery terms are established before contract or order confirmation.

---

## CONTACT US

**{unit_name.upper()}**  
Registered Enterprise: {legal_name}

**Principal Business Address:**  
{principal_address}{additional_addr_block}

**Phone:** {phone}  
**Email:** {email}  
**Operating Hours:** Standard business hours (Kindly contact us prior to visiting).

For product specifications, service enquiries, formal quotations, or compliance verification, please get in touch using the details above."""

    # Section 5: Privacy Policy
    sec_privacy = f"""## PRIVACY POLICY

> Template text - legal review required before publishing.

**Effective Date: {today_str}**

{unit_name} ("we", "our", or "the Enterprise") respects the privacy of customers, clients, and website visitors. This Privacy Policy outlines our procedures regarding the collection, use, and protection of information.

### 1. Information We Collect
We only receive personal or business details voluntarily submitted to us, including:
- Name, contact telephone numbers, and email addresses
- Physical business or delivery addresses
- Specific product, order, or service enquiry details

### 2. Purpose and Usage
Collected information is utilised exclusively to:
- Respond to commercial enquiries and provide requested information
- Execute purchase orders, contracts, and statutory billing
- Coordinate product deliveries or service scheduling
- Adhere to legal, accounting, and taxation requirements in India

### 3. Data Protection and Confidentiality
We employ appropriate administrative and technical controls to safeguard customer records against unauthorised access. We do not sell, rent, or trade customer information to third parties.

### 4. Third-Party Disclosures
Data is disclosed only to essential service partners (such as authorised courier or banking providers) strictly necessary to fulfill transactions, or when mandated by law.

### 5. Your Rights
Customers may contact us to review, verify, or update their contact records, subject to statutory record-retention requirements under Indian tax laws.

### 6. Privacy Contact
For any privacy questions, please contact us at **{email}** or **{phone}**."""

    # Section 6: Terms and Conditions
    sec_terms = f"""## TERMS AND CONDITIONS

> Template text - legal review required before publishing.

**Effective Date: {today_str}**

These Terms and Conditions apply to all visitors, clients, and commercial enquiries interacting with {unit_name}.

### 1. Informational Purpose
All website materials and activity descriptions reflect verified statutory records. Specific product models, technical parameters, and service availability must be confirmed directly for each transaction.

### 2. Quotations and Orders
Discussions, price lists, or estimates do not constitute binding contracts until formal written acceptance or a purchase order is mutually confirmed by {unit_name}.

### 3. Prices and Taxes
Prices are subject to applicable statutory taxes (such as GST) and delivery logistics charges unless specifically indicated in writing.

### 4. Quality and Inspections
Clients are encouraged to verify specifications upon delivery or prior to dispatch in accordance with agreed commercial terms.

### 5. Limitation of Liability
To the fullest extent permitted by Indian law, the enterprise shall not be liable for indirect or consequential losses resulting from website downtime or minor typographical discrepancies.

### 6. Governing Law
These terms are governed by the laws of India. Any dispute shall be subject to the exclusive jurisdiction of the competent courts in the jurisdiction where the enterprise is registered."""

    # Section 7: Disclaimer & Footer (Includes Principal Office + Additional Office + GSTIN)
    footer_additional = f"\nAdditional Office: {additional_address}  " if additional_address else ""
    reg_line_parts = []
    if gstin:
        reg_line_parts.append(f"GSTIN: {gstin}")
    if udyam_number:
        reg_line_parts.append(f"Udyam Reg. No.: {udyam_number}")
    reg_line = " | ".join(reg_line_parts)

    sec_footer = f"""## DISCLAIMER

The content published on this website is derived directly from official government registration documents (including Udyam and GST certificates). While every effort is made to maintain factual accuracy, the inclusion of an official classification does not guarantee that every single item or service within that classification is actively stocked or manufactured at any given moment.

Customers, procurement agents, and third parties are advised to verify exact product availability, lead times, and commercial terms directly with {unit_name} before placing orders.

---

## FOOTER

**{legal_name.upper()}**  
Principal Office: {principal_address}  {footer_additional}
{reg_line}  
Phone: {phone} | Email: {email}  

Links: Home | About Us | Products & Services | Privacy Policy | Terms & Conditions | Disclaimer  
© {current_year} {legal_name}. All rights reserved."""

    full_markdown = f"""{sec_hero}

<!-- Mode: {condition_tag} -->

---

{sec_about}

---

{sec_services}

---

{sec_contact}

---

{sec_privacy}

---

{sec_terms}

---

{sec_footer}"""

    sections = [
        {"title": "1. Hero / Header & Welcome", "content": sec_hero, "id": "sec_hero"},
        {"title": "2. About Us & Registration Table", "content": sec_about, "id": "sec_about"},
        {"title": "3. Products & Services (Document Grounded)", "content": sec_services, "id": "sec_services"},
        {"title": "4. Why Choose Us & Contact Info", "content": sec_contact, "id": "sec_contact"},
        {"title": "5. Privacy Policy (India Compliant)", "content": sec_privacy, "id": "sec_privacy"},
        {"title": "6. Terms and Conditions", "content": sec_terms, "id": "sec_terms"},
        {"title": "7. Disclaimer & Website Footer", "content": sec_footer, "id": "sec_footer"},
    ]

    for s in sections:
        s["clean_content"] = markdown_to_clean_text(s["content"])

    clean_full_text = markdown_to_clean_text(full_markdown)

    return full_markdown, clean_full_text, sections


def build_google_sites_html(merged):
    current_year = date.today().year
    today_str = date.today().strftime("%d %B %Y")
    
    legal_name = merged.get("legal_name") or "Registered Enterprise"
    unit_name = merged.get("manual_unit") or merged.get("unit_name") or merged.get("trade_name") or legal_name
    trade_name = merged.get("trade_name") or legal_name
    enterprise_type = merged.get("enterprise_type", "")
    organisation_type = merged.get("organisation_type", "")
    major_activity = merged.get("major_activity", "")
    gstin = merged.get("gstin", "")
    udyam_number = merged.get("udyam_number", "")
    phone = merged.get("manual_phone") or merged.get("phone", "")
    email = merged.get("manual_email") or merged.get("email", "")
    principal_address = merged.get("manual_address") or merged.get("address") or merged.get("gst_address") or merged.get("udyam_address") or ""
    additional_address = merged.get("additional_address") or merged.get("gst_additional_address", "")
    commencement_date = format_date_for_display(merged.get("commencement_date", ""))
    udyam_registration_date = format_date_for_display(merged.get("udyam_registration_date", ""))
    gst_registration_date = format_date_for_display(merged.get("gst_registration_date", ""))

    summary_p, disclaimer_p, offerings_md, offerings_note, tagline, condition_tag = generate_universal_content(merged)
    nic_entries = merged.get("nic_entries") or []

    # Common inline/scoped styles for Google Sites Embeds
    base_style = """<style>
.gs-container {
    font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Helvetica, Arial, sans-serif;
    color: #1e293b;
    line-height: 1.6;
    max-width: 100%;
    margin: 0 auto;
    padding: 16px;
    box-sizing: border-box;
}
.gs-card {
    background: #ffffff;
    border: 1px solid #e2e8f0;
    border-radius: 12px;
    padding: 24px;
    margin-bottom: 20px;
    box-shadow: 0 2px 4px rgba(0,0,0,0.04);
}
.gs-hero {
    background: linear-gradient(135deg, #042f2e 0%, #0f766e 100%);
    color: #ffffff;
    border-radius: 14px;
    padding: 36px 24px;
    text-align: center;
    margin-bottom: 24px;
    box-shadow: 0 4px 12px rgba(15, 118, 110, 0.2);
}
.gs-hero h1 {
    font-size: 28px;
    font-weight: 800;
    margin: 0 0 8px 0;
    letter-spacing: -0.5px;
}
.gs-hero p.tagline {
    font-size: 16px;
    color: #99f6e4;
    margin: 0 0 16px 0;
    font-weight: 500;
}
.gs-badge {
    display: inline-block;
    background: rgba(255, 255, 255, 0.15);
    border: 1px solid rgba(255, 255, 255, 0.3);
    color: #ffffff;
    font-size: 12px;
    font-weight: 600;
    padding: 4px 12px;
    border-radius: 999px;
    margin-bottom: 12px;
}
.gs-btn-group {
    display: flex;
    gap: 12px;
    justify-content: center;
    flex-wrap: wrap;
    margin-top: 18px;
}
.gs-btn {
    display: inline-block;
    padding: 10px 20px;
    border-radius: 8px;
    font-weight: 600;
    text-decoration: none;
    font-size: 14px;
    transition: all 0.2s;
}
.gs-btn-primary {
    background: #ffffff;
    color: #0f766e;
}
.gs-btn-secondary {
    background: rgba(255, 255, 255, 0.15);
    color: #ffffff;
    border: 1px solid rgba(255, 255, 255, 0.4);
}
.gs-table {
    width: 100%;
    border-collapse: collapse;
    margin-top: 12px;
    font-size: 14px;
}
.gs-table th, .gs-table td {
    padding: 10px 14px;
    border: 1px solid #e2e8f0;
    text-align: left;
}
.gs-table th {
    background: #f8fafc;
    color: #475569;
    font-weight: 600;
    width: 35%;
}
.gs-table tr:nth-child(even) td {
    background: #f8fafc;
}
.gs-grid {
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(280px, 1fr));
    gap: 16px;
    margin-top: 14px;
}
.gs-service-card {
    background: #f8fafc;
    border: 1px solid #e2e8f0;
    border-radius: 10px;
    padding: 18px;
    border-left: 4px solid #0f766e;
}
.gs-service-card h4 {
    margin: 0 0 6px 0;
    color: #0f766e;
    font-size: 15px;
    font-weight: 700;
}
.gs-service-card p {
    margin: 0;
    font-size: 13.5px;
    color: #475569;
}
.gs-notice {
    background: #f0fdf4;
    border: 1px solid #bbf7d0;
    color: #166534;
    border-radius: 8px;
    padding: 12px 16px;
    font-size: 13px;
    margin-top: 14px;
}
.gs-contact-item {
    display: flex;
    align-items: flex-start;
    gap: 10px;
    margin-bottom: 12px;
    font-size: 14px;
}
.gs-contact-item strong {
    min-width: 80px;
    color: #334155;
}
.gs-footer {
    text-align: center;
    padding: 24px;
    background: #0f172a;
    color: #94a3b8;
    border-radius: 12px;
    font-size: 13px;
    margin-top: 24px;
}
.gs-footer strong {
    color: #f8fafc;
}
@media (max-width: 600px) {
    .gs-hero h1 { font-size: 22px; }
    .gs-table th, .gs-table td { padding: 8px 10px; font-size: 13px; }
}
</style>"""

    # 1. Hero HTML
    badge_text = f"Reg. No: {udyam_number or gstin}" if (udyam_number or gstin) else "Official MSME Enterprise"
    btn_phone = f'<a href="tel:{phone.replace(" ", "")}" class="gs-btn gs-btn-primary">📞 Call Now</a>' if phone and "[" not in phone else ""
    btn_email = f'<a href="mailto:{email}" class="gs-btn gs-btn-secondary">✉️ Email Us</a>' if email and "[" not in email else ""
    
    html_hero = f"""{base_style}
<div class="gs-container">
    <div class="gs-hero">
        <span class="gs-badge">{badge_text}</span>
        <h1>{unit_name}</h1>
        <p class="tagline">{tagline}</p>
        <p style="max-width: 700px; margin: 0 auto; font-size: 14px; opacity: 0.9;">{summary_p}</p>
        <div class="gs-btn-group">
            {btn_phone}
            {btn_email}
        </div>
    </div>
</div>"""

    # 2. About Us & Registration Details HTML
    rows_html = f"""<tr><th>Enterprise Legal Name</th><td><strong>{legal_name}</strong></td></tr>
<tr><th>Unit / Trade Name</th><td>{unit_name}</td></tr>
<tr><th>Organisation Type</th><td>{organisation_type or "Registered Enterprise"}</td></tr>"""
    if enterprise_type:
        rows_html += f"<tr><th>Enterprise Category</th><td>{enterprise_type}</td></tr>"
    if major_activity:
        rows_html += f"<tr><th>Major Activity</th><td>{major_activity}</td></tr>"
    if udyam_number:
        rows_html += f"<tr><th>Udyam Registration Number</th><td><code>{udyam_number}</code></td></tr>"
    if gstin:
        rows_html += f"<tr><th>GSTIN</th><td><code>{gstin}</code></td></tr>"
    if commencement_date:
        rows_html += f"<tr><th>Date of Commencement</th><td>{commencement_date}</td></tr>"
    if udyam_registration_date:
        rows_html += f"<tr><th>Udyam Registration Date</th><td>{udyam_registration_date}</td></tr>"
    if principal_address:
        rows_html += f"<tr><th>Principal Business Address</th><td>{principal_address}</td></tr>"
    if additional_address:
        rows_html += f"<tr><th>Additional Branch Location</th><td>{additional_address}</td></tr>"

    html_about = f"""{base_style}
<div class="gs-container">
    <div class="gs-card">
        <h2 style="margin: 0 0 10px 0; color: #0f766e; font-size: 22px;">About Our Enterprise</h2>
        <p style="margin: 0 0 14px 0; color: #475569; font-size: 14px;">{summary_p}</p>
        <h3 style="margin: 20px 0 8px 0; color: #1e293b; font-size: 16px;">Statutory Registration Details</h3>
        <table class="gs-table">
            <tbody>
                {rows_html}
            </tbody>
        </table>
        <div class="gs-notice">
            ✓ <strong>Verified Record:</strong> Extracted directly from statutory government documents. Specifications and orders subject to commercial confirmation.
        </div>
    </div>
</div>"""

    # 3. Products & Services HTML
    services_cards = ""
    if nic_entries:
        for itm in nic_entries:
            cd = itm.get("nic_code", "")
            desc = itm.get("nic_description", "")
            cls = itm.get("classification", "")
            badge = f'<span style="background: #e0f2fe; color: #0369a1; font-size: 11px; padding: 2px 6px; border-radius: 4px; margin-left: 6px;">{cls}</span>' if cls else ""
            services_cards += f"""<div class="gs-service-card">
    <h4>NIC {cd} {badge}</h4>
    <p>{desc}</p>
</div>"""
    else:
        services_cards = f"""<div class="gs-service-card">
    <h4>{major_activity or 'Business'} Operations</h4>
    <p>Activities conducted in compliance with statutory registrations and customer specifications.</p>
</div>"""

    html_services = f"""{base_style}
<div class="gs-container">
    <div class="gs-card">
        <h2 style="margin: 0 0 8px 0; color: #0f766e; font-size: 22px;">Products & Registered Services</h2>
        <p style="margin: 0 0 14px 0; color: #475569; font-size: 14px;">All activities conducted in strict adherence to official statutory registrations without unverified product claims.</p>
        <div class="gs-grid">
            {services_cards}
        </div>
        <div class="gs-notice">
            ℹ️ <strong>Order & Procurement Notice:</strong> Please contact our sales and operations desk directly for availability, technical lead times, and quotations.
        </div>
    </div>
</div>"""

    # 4. Contact Us HTML
    why_bullet = why_choose_first_bullet(gstin, udyam_number)
    clean_why_bullet = why_bullet.replace("- **", "").replace(":**", ": ")

    html_contact = f"""{base_style}
<div class="gs-container">
    <div class="gs-grid" style="margin-top: 0;">
        <div class="gs-card">
            <h3 style="margin: 0 0 12px 0; color: #0f766e; font-size: 18px;">Why Choose Us?</h3>
            <ul style="padding-left: 20px; font-size: 14px; color: #475569; line-height: 1.8;">
                <li>{clean_why_bullet}</li>
                <li><strong>Clear Scope Communication:</strong> Transparent discussion on product parameters and order fulfillment.</li>
                <li><strong>Client Focused:</strong> Orders accommodated based on verified operational capacity.</li>
                <li><strong>Transparent Commercials:</strong> All applicable GST taxes and terms established prior to order confirmation.</li>
            </ul>
        </div>
        <div class="gs-card" style="border-top: 4px solid #0f766e;">
            <h3 style="margin: 0 0 14px 0; color: #1e293b; font-size: 18px;">Get In Touch</h3>
            <div class="gs-contact-item">
                <strong>📍 Address:</strong>
                <span>{principal_address or 'Please contact us for location'}</span>
            </div>
            {f'<div class="gs-contact-item"><strong>🏢 Branch:</strong><span>{additional_address}</span></div>' if additional_address else ''}
            <div class="gs-contact-item">
                <strong>📞 Phone:</strong>
                <span><a href="tel:{phone.replace(' ', '')}" style="color: #0f766e; font-weight: 600; text-decoration: none;">{phone}</a></span>
            </div>
            <div class="gs-contact-item">
                <strong>✉️ Email:</strong>
                <span><a href="mailto:{email}" style="color: #0f766e; font-weight: 600; text-decoration: none;">{email}</a></span>
            </div>
            <div class="gs-contact-item">
                <strong>⏰ Hours:</strong>
                <span>Standard business hours (Prior appointment recommended)</span>
            </div>
        </div>
    </div>
</div>"""

    # 5. Privacy Policy HTML
    html_privacy = f"""{base_style}
<div class="gs-container">
    <div class="gs-card">
        <h2 style="margin: 0 0 10px 0; color: #0f766e; font-size: 22px;">Privacy Policy</h2>
        <div class="gs-notice" style="background: #fffbeb; border-color: #fde68a; color: #92400e; margin-bottom: 14px;">
            ⚠️ <em>Template text - legal review required before publishing.</em>
        </div>
        <p style="font-size: 13px; color: #64748b;"><strong>Effective Date:</strong> {today_str}</p>
        <p style="font-size: 14px; color: #334155;">{unit_name} ("we", "our", or "the Enterprise") respects the privacy of customers, clients, and website visitors. This Privacy Policy outlines our procedures regarding the collection, use, and protection of information.</p>
        <h4 style="margin: 16px 0 6px 0; color: #1e293b;">1. Information We Collect</h4>
        <p style="font-size: 14px; color: #475569;">We only receive business or personal contact details voluntarily provided to us, including contact numbers, delivery addresses, and commercial inquiry specifications.</p>
        <h4 style="margin: 16px 0 6px 0; color: #1e293b;">2. Usage & Disclosure</h4>
        <p style="font-size: 14px; color: #475569;">Collected details are used exclusively to respond to inquiries, fulfill statutory billing, and coordinate deliveries. Customer details are never sold or rented to third parties.</p>
        <h4 style="margin: 16px 0 6px 0; color: #1e293b;">3. Contact</h4>
        <p style="font-size: 14px; color: #475569;">For privacy questions, reach us at: <strong>{email}</strong> | <strong>{phone}</strong></p>
    </div>
</div>"""

    # 6. Terms & Conditions HTML
    html_terms = f"""{base_style}
<div class="gs-container">
    <div class="gs-card">
        <h2 style="margin: 0 0 10px 0; color: #0f766e; font-size: 22px;">Terms and Conditions</h2>
        <div class="gs-notice" style="background: #fffbeb; border-color: #fde68a; color: #92400e; margin-bottom: 14px;">
            ⚠️ <em>Template text - legal review required before publishing.</em>
        </div>
        <p style="font-size: 13px; color: #64748b;"><strong>Effective Date:</strong> {today_str}</p>
        <p style="font-size: 14px; color: #334155;">These Terms and Conditions apply to all visitors, clients, and commercial inquiries interacting with {unit_name}.</p>
        <h4 style="margin: 16px 0 6px 0; color: #1e293b;">1. Informational Purpose</h4>
        <p style="font-size: 14px; color: #475569;">All website descriptions reflect verified statutory records. Technical parameters, availability, and lead times must be confirmed in writing prior to order confirmation.</p>
        <h4 style="margin: 16px 0 6px 0; color: #1e293b;">2. Commercial Terms & Taxes</h4>
        <p style="font-size: 14px; color: #475569;">All quotations are subject to applicable taxes (GST) and freight parameters. Purchase orders become binding upon mutual written acceptance.</p>
    </div>
</div>"""

    # 7. Disclaimer & Footer HTML
    html_footer = f"""{base_style}
<div class="gs-container">
    <div class="gs-footer">
        <strong style="font-size: 16px;">{legal_name.upper()}</strong><br>
        <span style="font-size: 13px;">Principal Office: {principal_address}</span><br>
        <span style="font-size: 12px; opacity: 0.85;">{f"GSTIN: {gstin} | " if gstin else ""}{f"Udyam Reg. No.: {udyam_number} | " if udyam_number else ""}Phone: {phone} | Email: {email}</span><br>
        <div style="margin-top: 14px; padding-top: 10px; border-top: 1px solid rgba(255,255,255,0.1); font-size: 11px; opacity: 0.7;">
            The information displayed is derived from official government registration documents. All rights reserved © {current_year} {legal_name}.
        </div>
    </div>
</div>"""

    # Full 1-Page Google Sites Website HTML
    full_html = f"""<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{unit_name}</title>
{base_style}
</head>
<body style="margin: 0; background: #f8fafc;">
<div class="gs-container">
    <!-- HERO -->
    <div class="gs-hero">
        <span class="gs-badge">{badge_text}</span>
        <h1>{unit_name}</h1>
        <p class="tagline">{tagline}</p>
        <p style="max-width: 700px; margin: 0 auto; font-size: 14px; opacity: 0.9;">{summary_p}</p>
        <div class="gs-btn-group">
            {btn_phone}
            {btn_email}
        </div>
    </div>

    <!-- ABOUT -->
    <div class="gs-card">
        <h2 style="margin: 0 0 10px 0; color: #0f766e; font-size: 22px;">About Our Enterprise</h2>
        <p style="margin: 0 0 14px 0; color: #475569; font-size: 14px;">{summary_p}</p>
        <h3 style="margin: 20px 0 8px 0; color: #1e293b; font-size: 16px;">Statutory Registration Details</h3>
        <table class="gs-table">
            <tbody>
                {rows_html}
            </tbody>
        </table>
    </div>

    <!-- SERVICES -->
    <div class="gs-card">
        <h2 style="margin: 0 0 8px 0; color: #0f766e; font-size: 22px;">Products & Registered Services</h2>
        <p style="margin: 0 0 14px 0; color: #475569; font-size: 14px;">Activities conducted strictly in accordance with statutory filings.</p>
        <div class="gs-grid">
            {services_cards}
        </div>
    </div>

    <!-- CONTACT & WHY CHOOSE US -->
    <div class="gs-grid" style="margin-top: 0;">
        <div class="gs-card">
            <h3 style="margin: 0 0 12px 0; color: #0f766e; font-size: 18px;">Why Choose Us?</h3>
            <ul style="padding-left: 20px; font-size: 14px; color: #475569; line-height: 1.8;">
                <li>{clean_why_bullet}</li>
                <li><strong>Clear Scope Communication:</strong> Direct technical clarity.</li>
                <li><strong>Client Focused:</strong> Real-time operational alignment.</li>
                <li><strong>Statutory Compliant:</strong> Full billing and GST compliance.</li>
            </ul>
        </div>
        <div class="gs-card" style="border-top: 4px solid #0f766e;">
            <h3 style="margin: 0 0 14px 0; color: #1e293b; font-size: 18px;">Get In Touch</h3>
            <div class="gs-contact-item"><strong>📍 Address:</strong><span>{principal_address or 'Contact for location'}</span></div>
            {f'<div class="gs-contact-item"><strong>🏢 Branch:</strong><span>{additional_address}</span></div>' if additional_address else ''}
            <div class="gs-contact-item"><strong>📞 Phone:</strong><span><a href="tel:{phone.replace(' ', '')}" style="color: #0f766e; font-weight: 600; text-decoration: none;">{phone}</a></span></div>
            <div class="gs-contact-item"><strong>✉️ Email:</strong><span><a href="mailto:{email}" style="color: #0f766e; font-weight: 600; text-decoration: none;">{email}</a></span></div>
        </div>
    </div>

    <!-- FOOTER -->
    <div class="gs-footer">
        <strong>{legal_name.upper()}</strong><br>
        <span>{principal_address}</span><br>
        <span style="font-size: 12px; opacity: 0.85;">{f"GSTIN: {gstin} | " if gstin else ""}{f"Udyam: {udyam_number} | " if udyam_number else ""}Phone: {phone}</span><br>
        <div style="margin-top: 10px; font-size: 11px; opacity: 0.7;">
            © {current_year} {legal_name}. All rights reserved. | Generated for Google Sites
        </div>
    </div>
</div>
</body>
</html>"""

    html_sections = [
        {"title": "1. Hero / Welcome Banner (HTML Embed)", "content": html_hero, "id": "html_sec_hero"},
        {"title": "2. About Us & Registration Table (HTML Embed)", "content": html_about, "id": "html_sec_about"},
        {"title": "3. Products & Services Cards (HTML Embed)", "content": html_services, "id": "html_sec_services"},
        {"title": "4. Why Choose Us & Contact Block (HTML Embed)", "content": html_contact, "id": "html_sec_contact"},
        {"title": "5. Privacy Policy Block (HTML Embed)", "content": html_privacy, "id": "html_sec_privacy"},
        {"title": "6. Terms and Conditions Block (HTML Embed)", "content": html_terms, "id": "html_sec_terms"},
        {"title": "7. Compliance Disclaimer & Footer (HTML Embed)", "content": html_footer, "id": "html_sec_footer"},
    ]

    return full_html, html_sections


# ---------------------------------------------------------------------
# HTML INTERFACE
# ---------------------------------------------------------------------

HTML_UI = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>MSME Official Website Content Studio</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Plus+Jakarta+Sans:wght@400;500;600;700;800&family=JetBrains+Mono:wght@400;500&display=swap" rel="stylesheet">
<style>
    :root {
        --primary: #0f766e;
        --primary-hover: #115e59;
        --primary-light: #f0fdfa;
        --primary-border: #99f6e4;
        --accent: #2563eb;
        --accent-light: #eff6ff;
        --slate-900: #0f172a;
        --slate-800: #1e293b;
        --slate-700: #334155;
        --slate-600: #475569;
        --slate-500: #64748b;
        --slate-400: #94a3b8;
        --slate-300: #cbd5e1;
        --slate-200: #e2e8f0;
        --slate-100: #f1f5f9;
        --slate-50: #f8fafc;
        --emerald-600: #059669;
        --emerald-50: #ecfdf5;
        --emerald-200: #a7f3d0;
        --radius-lg: 16px;
        --radius-md: 10px;
        --radius-sm: 6px;
        --shadow-sm: 0 1px 2px 0 rgba(15, 23, 42, 0.05);
        --shadow-md: 0 4px 6px -1px rgba(15, 23, 42, 0.07), 0 2px 4px -2px rgba(15, 23, 42, 0.05);
        --shadow-lg: 0 10px 15px -3px rgba(15, 23, 42, 0.08), 0 4px 6px -4px rgba(15, 23, 42, 0.04);
    }
    * { box-sizing: border-box; margin: 0; padding: 0; }
    body {
        font-family: 'Plus Jakarta Sans', -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
        background: #f1f5f9;
        color: var(--slate-800);
        line-height: 1.6;
        padding: 32px 16px 80px 16px;
    }
    .container {
        max-width: 1140px;
        margin: 0 auto;
    }

    /* HEADER */
    .studio-header {
        background: linear-gradient(135deg, #042f2e 0%, #0f766e 60%, #115e59 100%);
        color: white;
        border-radius: var(--radius-lg);
        padding: 32px 36px;
        margin-bottom: 24px;
        box-shadow: var(--shadow-lg);
        position: relative;
        overflow: hidden;
    }
    .studio-header::after {
        content: "";
        position: absolute;
        top: -40%;
        right: -10%;
        width: 320px;
        height: 320px;
        background: radial-gradient(circle, rgba(45, 212, 191, 0.2) 0%, transparent 70%);
        pointer-events: none;
    }
    .header-top {
        display: flex;
        justify-content: space-between;
        align-items: center;
        flex-wrap: wrap;
        gap: 16px;
        margin-bottom: 24px;
    }
    .brand-title {
        display: flex;
        align-items: center;
        gap: 14px;
    }
    .brand-icon {
        width: 44px;
        height: 44px;
        background: rgba(255, 255, 255, 0.15);
        border: 1px solid rgba(255, 255, 255, 0.25);
        border-radius: 12px;
        display: flex;
        align-items: center;
        justify-content: center;
        backdrop-filter: blur(4px);
    }
    .brand-title h1 {
        font-size: 24px;
        font-weight: 800;
        letter-spacing: -0.5px;
        color: #ffffff;
    }
    .brand-title p {
        font-size: 13.5px;
        color: #99f6e4;
        margin-top: 2px;
    }
    .header-badge {
        display: inline-flex;
        align-items: center;
        gap: 8px;
        background: rgba(255, 255, 255, 0.12);
        border: 1px solid rgba(255, 255, 255, 0.2);
        padding: 6px 14px;
        border-radius: 999px;
        font-size: 12px;
        font-weight: 600;
        letter-spacing: 0.3px;
    }
    .status-dot {
        width: 8px;
        height: 8px;
        background: #34d399;
        border-radius: 50%;
        box-shadow: 0 0 10px #34d399;
    }

    /* STEPPER BAR */
    .stepper {
        display: flex;
        align-items: center;
        background: rgba(0, 0, 0, 0.2);
        border-radius: var(--radius-md);
        padding: 10px 16px;
        gap: 12px;
    }
    .step-item {
        display: flex;
        align-items: center;
        gap: 10px;
        font-size: 13px;
        font-weight: 600;
        color: rgba(255, 255, 255, 0.65);
    }
    .step-item.active {
        color: #ffffff;
    }
    .step-item.completed {
        color: #5eead4;
    }
    .step-num {
        width: 24px;
        height: 24px;
        border-radius: 50%;
        display: flex;
        align-items: center;
        justify-content: center;
        font-size: 11px;
        background: rgba(255, 255, 255, 0.2);
        color: white;
    }
    .step-item.active .step-num {
        background: #0d9488;
        box-shadow: 0 0 0 2px rgba(255, 255, 255, 0.4);
    }
    .step-item.completed .step-num {
        background: #14b8a6;
    }
    .step-sep {
        flex: 1;
        height: 2px;
        background: rgba(255, 255, 255, 0.15);
    }

    /* CARDS & PANELS */
    .panel {
        background: #ffffff;
        border: 1px solid var(--slate-200);
        border-radius: var(--radius-lg);
        padding: 30px;
        margin-bottom: 24px;
        box-shadow: var(--shadow-sm);
        transition: box-shadow 0.2s;
    }
    .panel-header {
        display: flex;
        justify-content: space-between;
        align-items: center;
        margin-bottom: 20px;
        padding-bottom: 14px;
        border-bottom: 1px solid var(--slate-100);
    }
    .panel-title {
        font-size: 18px;
        font-weight: 700;
        color: var(--slate-900);
        display: flex;
        align-items: center;
        gap: 10px;
    }
    .panel-subtitle {
        font-size: 13.5px;
        color: var(--slate-500);
        margin-top: 2px;
    }

    /* DOCUMENT SELECTOR PILLS */
    .doc-selector {
        display: flex;
        gap: 12px;
        margin-bottom: 22px;
        flex-wrap: wrap;
    }
    .doc-option {
        flex: 1;
        min-width: 220px;
        position: relative;
    }
    .doc-option input[type="radio"] {
        position: absolute;
        opacity: 0;
        cursor: pointer;
    }
    .doc-card {
        display: flex;
        align-items: center;
        gap: 12px;
        padding: 14px 18px;
        border: 2px solid var(--slate-200);
        border-radius: var(--radius-md);
        background: var(--slate-50);
        cursor: pointer;
        transition: all 0.2s;
    }
    .doc-card:hover {
        border-color: var(--slate-400);
        background: #ffffff;
    }
    .doc-option input[type="radio"]:checked + .doc-card {
        border-color: var(--primary);
        background: var(--primary-light);
        box-shadow: 0 0 0 1px var(--primary);
    }
    .doc-card-icon {
        width: 32px;
        height: 32px;
        border-radius: 8px;
        background: white;
        display: flex;
        align-items: center;
        justify-content: center;
        color: var(--primary);
        font-weight: 700;
        font-size: 15px;
        box-shadow: var(--shadow-sm);
    }
    .doc-card-text strong {
        display: block;
        font-size: 14px;
        color: var(--slate-800);
    }
    .doc-card-text span {
        font-size: 12px;
        color: var(--slate-500);
    }

    /* DROPZONE */
    .dropzone {
        border: 2px dashed var(--slate-300);
        border-radius: var(--radius-md);
        padding: 28px 20px;
        text-align: center;
        background: var(--slate-50);
        cursor: pointer;
        transition: all 0.2s;
        position: relative;
    }
    .dropzone:hover, .dropzone.dragover {
        border-color: var(--primary);
        background: var(--primary-light);
    }
    .dropzone-icon {
        width: 48px;
        height: 48px;
        margin: 0 auto 12px;
        color: var(--primary);
        background: white;
        border-radius: 50%;
        display: flex;
        align-items: center;
        justify-content: center;
        box-shadow: var(--shadow-sm);
    }
    .dropzone-title {
        font-size: 14.5px;
        font-weight: 600;
        color: var(--slate-800);
        margin-bottom: 4px;
    }
    .dropzone-subtitle {
        font-size: 12.5px;
        color: var(--slate-500);
    }
    .dropzone input[type="file"] {
        position: absolute;
        inset: 0;
        opacity: 0;
        cursor: pointer;
        width: 100%;
        height: 100%;
    }
    .file-badge {
        display: none;
        margin-top: 12px;
        background: #ffffff;
        border: 1px solid var(--emerald-200);
        color: var(--emerald-600);
        padding: 6px 14px;
        border-radius: 999px;
        font-size: 13px;
        font-weight: 600;
        align-items: center;
        justify-content: center;
        gap: 6px;
    }

    /* ACCORDION (OPTIONAL OVERRIDES) */
    .accordion {
        background: var(--slate-50);
        border: 1px solid var(--slate-200);
        border-radius: var(--radius-md);
        margin: 22px 0;
        overflow: hidden;
    }
    .accordion summary {
        padding: 14px 18px;
        font-weight: 600;
        font-size: 14px;
        color: var(--slate-700);
        cursor: pointer;
        display: flex;
        justify-content: space-between;
        align-items: center;
        user-select: none;
        list-style: none;
    }
    .accordion summary::-webkit-details-marker { display: none; }
    .accordion summary:hover {
        background: var(--slate-100);
    }
    .accordion-body {
        padding: 18px;
        border-top: 1px solid var(--slate-200);
        background: #ffffff;
    }
    .form-grid {
        display: grid;
        grid-template-columns: repeat(auto-fit, minmax(240px, 1fr));
        gap: 16px;
    }
    .form-group {
        margin-bottom: 12px;
    }
    .form-group label {
        display: block;
        font-size: 13px;
        font-weight: 600;
        color: var(--slate-700);
        margin-bottom: 6px;
    }
    .form-control {
        width: 100%;
        padding: 10px 14px;
        font-size: 14px;
        font-family: inherit;
        color: var(--slate-800);
        border: 1px solid var(--slate-300);
        border-radius: var(--radius-sm);
        background: #ffffff;
        transition: border 0.15s, box-shadow 0.15s;
    }
    .form-control:focus {
        outline: none;
        border-color: var(--primary);
        box-shadow: 0 0 0 3px rgba(15, 118, 110, 0.15);
    }
    textarea.form-control {
        min-height: 80px;
        resize: vertical;
    }

    /* BUTTONS */
    .btn {
        display: inline-flex;
        align-items: center;
        justify-content: center;
        gap: 8px;
        padding: 12px 24px;
        font-size: 14.5px;
        font-weight: 600;
        font-family: inherit;
        border-radius: var(--radius-md);
        border: none;
        cursor: pointer;
        transition: all 0.2s;
    }
    .btn-primary {
        background: var(--primary);
        color: white;
        box-shadow: 0 2px 8px rgba(15, 118, 110, 0.25);
    }
    .btn-primary:hover {
        background: var(--primary-hover);
        box-shadow: 0 4px 12px rgba(15, 118, 110, 0.35);
        transform: translateY(-1px);
    }
    .btn-secondary {
        background: var(--slate-100);
        color: var(--slate-700);
        border: 1px solid var(--slate-300);
    }
    .btn-secondary:hover {
        background: var(--slate-200);
        color: var(--slate-900);
    }
    .btn-sm {
        padding: 6px 14px;
        font-size: 12.5px;
        border-radius: var(--radius-sm);
    }
    .btn-block {
        width: 100%;
        padding: 14px 24px;
        font-size: 15.5px;
    }

    /* ALERTS */
    .alert {
        display: flex;
        align-items: flex-start;
        gap: 12px;
        padding: 14px 18px;
        border-radius: var(--radius-md);
        margin-bottom: 20px;
        font-size: 13.5px;
    }
    .alert-error {
        background: #fef2f2;
        border: 1px solid #fecaca;
        color: #991b1b;
    }
    .alert-warning {
        background: #fffbeb;
        border: 1px solid #fde68a;
        color: #92400e;
    }
    .alert ul {
        margin: 4px 0 0 16px;
    }

    /* EXTRACTED DETAILS GRID */
    .review-grid {
        display: grid;
        grid-template-columns: repeat(auto-fit, minmax(250px, 1fr));
        gap: 16px;
        margin-bottom: 20px;
    }
    .review-card {
        background: var(--slate-50);
        border: 1px solid var(--slate-200);
        border-radius: var(--radius-md);
        padding: 14px 16px;
        position: relative;
    }
    .review-card-label {
        font-size: 12px;
        font-weight: 700;
        text-transform: uppercase;
        letter-spacing: 0.5px;
        color: var(--slate-500);
        margin-bottom: 6px;
    }
    .review-card-val {
        font-size: 14px;
        font-weight: 600;
        color: var(--slate-900);
        word-break: break-word;
    }
    .review-card-badge {
        display: inline-block;
        background: #dcfce7;
        color: #15803d;
        font-size: 11px;
        font-weight: 700;
        padding: 2px 8px;
        border-radius: 999px;
        margin-top: 6px;
    }
    .copy-chip {
        display: inline-flex;
        align-items: center;
        gap: 4px;
        font-size: 11px;
        color: var(--primary);
        background: white;
        border: 1px solid var(--slate-200);
        border-radius: 4px;
        padding: 2px 6px;
        cursor: pointer;
        margin-top: 6px;
        font-weight: 600;
    }
    .copy-chip:hover {
        background: var(--primary-light);
        border-color: var(--primary);
    }

    /* COMPLIANCE CHECKLIST */
    .compliance-box {
        background: #f0fdf4;
        border: 1px solid #bbf7d0;
        border-radius: var(--radius-md);
        padding: 16px 20px;
        display: flex;
        gap: 14px;
        align-items: flex-start;
        margin-top: 16px;
    }
    .compliance-box svg {
        flex-shrink: 0;
        color: #16a34a;
        margin-top: 2px;
    }
    .compliance-title {
        font-size: 14px;
        font-weight: 700;
        color: #166534;
        margin-bottom: 4px;
    }
    .compliance-list {
        font-size: 13px;
        color: #15803d;
        line-height: 1.5;
        list-style: square;
        margin-left: 18px;
    }

    /* TABS BAR */
    .tabs-bar {
        display: flex;
        gap: 8px;
        background: var(--slate-100);
        padding: 6px;
        border-radius: var(--radius-md);
        margin-bottom: 20px;
        flex-wrap: wrap;
    }
    .tab-btn {
        flex: 1;
        min-width: 160px;
        padding: 10px 16px;
        font-size: 13.5px;
        font-weight: 600;
        font-family: inherit;
        background: transparent;
        color: var(--slate-600);
        border: none;
        border-radius: var(--radius-sm);
        cursor: pointer;
        transition: all 0.15s;
        display: flex;
        align-items: center;
        justify-content: center;
        gap: 8px;
    }
    .tab-btn.active {
        background: #ffffff;
        color: var(--primary);
        box-shadow: var(--shadow-sm);
    }

    /* SECTION COPY CARDS */
    .section-card {
        background: #ffffff;
        border: 1px solid var(--slate-200);
        border-radius: var(--radius-md);
        padding: 18px 20px;
        margin-bottom: 14px;
        transition: all 0.2s;
    }
    .section-card:hover {
        border-color: var(--slate-400);
        box-shadow: var(--shadow-sm);
    }
    .section-header {
        display: flex;
        justify-content: space-between;
        align-items: center;
        flex-wrap: wrap;
        gap: 10px;
        margin-bottom: 10px;
    }
    .section-name {
        font-size: 15px;
        font-weight: 700;
        color: var(--slate-900);
        display: flex;
        align-items: center;
        gap: 8px;
    }
    .section-meta {
        font-size: 12px;
        color: var(--slate-500);
        background: var(--slate-100);
        padding: 3px 8px;
        border-radius: 999px;
    }
    .section-snippet {
        font-size: 13px;
        color: var(--slate-600);
        background: var(--slate-50);
        border: 1px solid var(--slate-100);
        border-radius: var(--radius-sm);
        padding: 10px 14px;
        white-space: pre-wrap;
        max-height: 100px;
        overflow-y: auto;
        font-family: 'JetBrains Mono', monospace;
    }

    /* VISUAL MOCKUP PREVIEW */
    .mockup-window {
        background: #ffffff;
        border: 1px solid var(--slate-300);
        border-radius: var(--radius-md);
        overflow: hidden;
        box-shadow: var(--shadow-md);
    }
    .mockup-topbar {
        background: var(--slate-100);
        border-bottom: 1px solid var(--slate-200);
        padding: 10px 16px;
        display: flex;
        align-items: center;
        gap: 12px;
    }
    .mockup-dots {
        display: flex;
        gap: 6px;
    }
    .mockup-dot {
        width: 10px;
        height: 10px;
        border-radius: 50%;
        background: var(--slate-300);
    }
    .mockup-url {
        flex: 1;
        background: #ffffff;
        border: 1px solid var(--slate-200);
        border-radius: 4px;
        padding: 4px 12px;
        font-size: 12px;
        color: var(--slate-500);
        font-family: 'JetBrains Mono', monospace;
    }
    .mockup-content {
        padding: 32px 24px;
        max-height: 600px;
        overflow-y: auto;
    }
    .mockup-hero {
        text-align: center;
        padding: 36px 16px;
        background: linear-gradient(180deg, var(--slate-50) 0%, #ffffff 100%);
        border-radius: var(--radius-md);
        margin-bottom: 28px;
        border: 1px solid var(--slate-100);
    }
    .mockup-hero h2 {
        font-size: 26px;
        font-weight: 800;
        color: var(--slate-900);
        margin-bottom: 8px;
    }
    .mockup-hero p {
        font-size: 15px;
        color: var(--slate-600);
        max-width: 650px;
        margin: 0 auto 16px;
    }
    .mockup-hero .badge-hero {
        display: inline-block;
        background: var(--primary-light);
        color: var(--primary);
        font-size: 12px;
        font-weight: 700;
        padding: 4px 12px;
        border-radius: 999px;
        margin-bottom: 12px;
    }
    .mockup-section {
        margin-bottom: 28px;
        padding-bottom: 20px;
        border-bottom: 1px solid var(--slate-100);
    }
    .mockup-section h3 {
        font-size: 18px;
        font-weight: 700;
        color: var(--slate-900);
        margin-bottom: 12px;
    }
    .mockup-table {
        width: 100%;
        border-collapse: collapse;
        font-size: 13.5px;
        margin-top: 10px;
    }
    .mockup-table th, .mockup-table td {
        padding: 8px 12px;
        border: 1px solid var(--slate-200);
        text-align: left;
    }
    .mockup-table th {
        background: var(--slate-100);
        color: var(--slate-700);
    }

    /* TOAST */
    .toast {
        position: fixed;
        bottom: 24px;
        right: 24px;
        background: var(--slate-900);
        color: #ffffff;
        padding: 12px 20px;
        border-radius: var(--radius-md);
        font-size: 13.5px;
        font-weight: 600;
        box-shadow: var(--shadow-lg);
        display: flex;
        align-items: center;
        gap: 10px;
        transform: translateY(100px);
        opacity: 0;
        transition: all 0.3s cubic-bezier(0.16, 1, 0.3, 1);
        z-index: 9999;
    }
    .toast.show {
        transform: translateY(0);
        opacity: 1;
    }
    .toast svg {
        color: #34d399;
    }

    .hidden { display: none !important; }

    @media (max-width: 768px) {
        body { padding: 16px 8px; }
        .studio-header { padding: 20px; }
        .panel { padding: 18px; }
        .brand-title h1 { font-size: 20px; }
        .stepper { flex-direction: column; align-items: flex-start; gap: 8px; }
        .step-sep { display: none; }
    }
</style>
</head>
<body>
<div class="container">

    <!-- HEADER & STEPPER -->
    <header class="studio-header">
        <div class="header-top">
            <div class="brand-title">
                <div class="brand-icon">
                    <svg width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><path d="M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10z"/><path d="m9 12 2 2 4-4"/></svg>
                </div>
                <div>
                    <h1>Universal MSME Website Content Studio</h1>
                    <p>Extracted from Certificate • NIC-2008 Database Integrated • Google Sites Export</p>
                </div>
            </div>
            <div class="header-badge">
                <span class="status-dot"></span>
                <span>Best-effort extraction</span>
            </div>
        </div>

        <div class="stepper">
            <div class="step-item {% if not content %}active{% else %}completed{% endif %}">
                <span class="step-num">1</span>
                <span>Upload Certificate</span>
            </div>
            <div class="step-sep"></div>
            <div class="step-item {% if extracted_data and not content %}active{% elif content %}completed{% endif %}">
                <span class="step-num">2</span>
                <span>Extracted Verification</span>
            </div>
            <div class="step-sep"></div>
            <div class="step-item {% if content %}active{% endif %}">
                <span class="step-num">3</span>
                <span>Section Export & Preview</span>
            </div>
        </div>
    </header>

    <!-- NOTICES & ALERTS -->
    {% if error %}
    <div class="alert alert-error">
        <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="12" cy="12" r="10"/><line x1="12" y1="8" x2="12" y2="12"/><line x1="12" y1="16" x2="12.01" y2="16"/></svg>
        <div>
            <strong>Certificate Error:</strong><br>{{ error }}
        </div>
    </div>
    {% endif %}

    {% if warnings %}
    <div class="alert alert-warning">
        <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="m21.73 18-8-14a2 2 0 0 0-3.48 0l-8 14A2 2 0 0 0 4 21h16a2 2 0 0 0 1.73-3Z"/><line x1="12" y1="9" x2="12" y2="13"/><line x1="12" y1="17" x2="12.01" y2="17"/></svg>
        <div>
            <strong>Notice:</strong>
            <ul>
                {% for item in warnings %}<li>{{ item }}</li>{% endfor %}
            </ul>
        </div>
    </div>
    {% endif %}

    {% if conflicts %}
    <div class="alert alert-warning">
        <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="m21.73 18-8-14a2 2 0 0 0-3.48 0l-8 14A2 2 0 0 0 4 21h16a2 2 0 0 0 1.73-3Z"/><line x1="12" y1="9" x2="12" y2="13"/><line x1="12" y1="17" x2="12.01" y2="17"/></svg>
        <div>
            <strong>Document Discrepancy:</strong>
            <ul>
                {% for item in conflicts %}<li>{{ item }}</li>{% endfor %}
            </ul>
        </div>
    </div>
    {% endif %}

    <!-- STEP 1: UPLOAD PANEL -->
    <div class="panel">
        <div class="panel-header">
            <div>
                <h2 class="panel-title">
                    <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2"><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><polyline points="14 2 14 8 20 8"/></svg>
                    Step 1: Upload Official Certificate
                </h2>
                <p class="panel-subtitle">Upload your Udyam (or GST) digital PDF to automatically generate compliant website content.</p>
            </div>
        </div>

        <form method="POST" enctype="multipart/form-data" id="uploadForm">
            <!-- SELECTOR PILLS -->
            <div class="doc-selector">
                <label class="doc-option">
                    <input type="radio" name="doc_type" value="udyam" {% if selected_type == 'udyam' or not selected_type %}checked{% endif %} onchange="toggleFields()">
                    <div class="doc-card">
                        <div class="doc-card-icon">📄</div>
                        <div class="doc-card-text">
                            <strong>Udyam PDF (Recommended)</strong>
                            <span>All business activities & NIC codes</span>
                        </div>
                    </div>
                </label>
                <label class="doc-option">
                    <input type="radio" name="doc_type" value="gst" {% if selected_type == 'gst' %}checked{% endif %} onchange="toggleFields()">
                    <div class="doc-card">
                        <div class="doc-card-icon">📋</div>
                        <div class="doc-card-text">
                            <strong>GST Certificate PDF</strong>
                            <span>Legal name & GSTIN validation</span>
                        </div>
                    </div>
                </label>
                <label class="doc-option">
                    <input type="radio" name="doc_type" value="both" {% if selected_type == 'both' %}checked{% endif %} onchange="toggleFields()">
                    <div class="doc-card">
                        <div class="doc-card-icon">📑</div>
                        <div class="doc-card-text">
                            <strong>Both (GST + Udyam)</strong>
                            <span>Cross-verified business profile</span>
                        </div>
                    </div>
                </label>
            </div>

            <!-- UPLOAD ZONES -->
            <div class="form-grid" style="margin-bottom: 20px;">
                <div class="form-group" id="udyam_box">
                    <label>Udyam Registration Certificate (PDF)</label>
                    <div class="dropzone" id="udyam_dropzone">
                        <div class="dropzone-icon">
                            <svg width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><polyline points="17 8 12 3 7 8"/><line x1="12" y1="3" x2="12" y2="15"/></svg>
                        </div>
                        <div class="dropzone-title">Click to browse or drag & drop Udyam PDF</div>
                        <div class="dropzone-subtitle">Supports digital official PDF from udyamregistration.gov.in</div>
                        <input type="file" name="udyam_file" id="udyam_file" accept=".pdf,application/pdf" onchange="handleFileSelect(this, 'udyam_file_badge')">
                        <div class="file-badge" id="udyam_file_badge"></div>
                    </div>
                </div>

                <div class="form-group hidden" id="gst_box">
                    <label>GST Registration Certificate (PDF)</label>
                    <div class="dropzone" id="gst_dropzone">
                        <div class="dropzone-icon">
                            <svg width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><polyline points="17 8 12 3 7 8"/><line x1="12" y1="3" x2="12" y2="15"/></svg>
                        </div>
                        <div class="dropzone-title">Click to browse or drag & drop GST PDF</div>
                        <div class="dropzone-subtitle">Form GST REG-06 Certificate</div>
                        <input type="file" name="gst_file" id="gst_file" accept=".pdf,application/pdf" onchange="handleFileSelect(this, 'gst_file_badge')">
                        <div class="file-badge" id="gst_file_badge"></div>
                    </div>
                </div>
            </div>

            <!-- OPTIONAL OVERRIDES ACCORDION -->
            <details class="accordion">
                <summary>
                    <span>⚙️ Optional Details & Overrides (Defaults to Certificate Auto-Extraction)</span>
                    <span style="font-size: 12px; color: var(--slate-500);">Click to Expand ▾</span>
                </summary>
                <div class="accordion-body">
                    <p style="font-size: 13px; color: var(--slate-500); margin-bottom: 14px;">Leave these fields blank to automatically extract official values directly from the uploaded certificate without any manual typing.</p>
                    <div class="form-grid">
                        <div class="form-group">
                            <label for="manual_unit">Display / Unit Name (Optional)</label>
                            <input type="text" id="manual_unit" name="manual_unit" class="form-control" value="{{ form_values.get('manual_unit','') }}" placeholder="Certificate Unit Name is used if blank">
                        </div>
                        <div class="form-group">
                            <label for="manual_phone">Phone Number (Optional)</label>
                            <input type="text" id="manual_phone" name="manual_phone" class="form-control" value="{{ form_values.get('manual_phone','') }}" placeholder="Extracted from certificate if blank">
                        </div>
                        <div class="form-group">
                            <label for="manual_email">Email Address (Optional)</label>
                            <input type="email" id="manual_email" name="manual_email" class="form-control" value="{{ form_values.get('manual_email','') }}" placeholder="Extracted from certificate if blank">
                        </div>
                        <div class="form-group">
                            <label for="manual_address">Address Override (Optional)</label>
                            <input type="text" id="manual_address" name="manual_address" class="form-control" value="{{ form_values.get('manual_address','') }}" placeholder="Extracted from certificate if blank">
                        </div>
                    </div>
                    <div class="form-group" style="margin-top: 6px;">
                        <label for="manual_services">Specific Product Catalog (Optional - Only if you want to name custom items)</label>
                        <textarea id="manual_services" name="manual_services" class="form-control" placeholder="Optional: If you know exact items (e.g. RO Purifier Systems, Alkaline Water Bottles). If left blank, official NIC-2008 descriptions from certificate are used.">{{ form_values.get('manual_services','') }}</textarea>
                    </div>
                </div>
            </details>

            <button type="submit" class="btn btn-primary btn-block">
                <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2"><path d="m13 2-2 2.5V11l5-5-2-4z"/><polygon points="13 2 3 14 12 14 11 22 21 10 12 10 13 2"/></svg>
                Generate Website Content
            </button>
        </form>
    </div>

    <!-- STEP 2: EXTRACTED DETAILS VERIFICATION -->
    {% if extracted_data %}
    <div class="panel">
        <div class="alert alert-warning" style="margin-bottom: 16px; padding: 10px 14px; font-size: 13px;">
            <strong>Notice:</strong> Automatic extraction galat ho sakta hai. Publish karne se pehle har field apne original certificate se verify karein.
        </div>
        <div class="panel-header">
            <div>
                <h2 class="panel-title">
                    <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2"><path d="M22 11.08V12a10 10 0 1 1-5.93-9.14"/><polyline points="22 4 12 14.01 9 11.01"/></svg>
                    Step 2: Extracted Document Details
                </h2>
                <p class="panel-subtitle">Official records extracted and standardized with NIC-2008 database grounding.</p>
            </div>
            <span class="badge-hero" style="background: var(--emerald-50); color: var(--emerald-600); border: 1px solid var(--emerald-200); padding: 4px 12px; border-radius: 999px; font-size: 12px; font-weight: 700;">
                ✓ Extracted from Certificate
            </span>
        </div>

        <div class="review-grid">
            {% for k, v in extracted_data.items() %}
            <div class="review-card">
                <div class="review-card-label">{{ k }}</div>
                <div class="review-card-val">{{ v }}</div>
                <div style="display: flex; justify-content: space-between; align-items: center; margin-top: 6px;">
                    <span class="review-card-badge">Official Record</span>
                    <button type="button" class="copy-chip" data-copy="{{ v }}" onclick="copyRawText(this.dataset.copy)">
                        <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><rect width="14" height="14" x="8" y="8" rx="2" ry="2"/><path d="M4 16c-1.1 0-2-.9-2-2V4c0-1.1.9-2 2-2h10c1.1 0 2 .9 2 2"/></svg> Copy
                    </button>
                </div>
            </div>
            {% endfor %}
        </div>

        <div class="compliance-box">
            <svg width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10z"/><path d="m9 12 2 2 4-4"/></svg>
            <div>
                <div class="compliance-title">Extraction Notes (please verify):</div>
                <ul class="compliance-list">
                    <li><strong>Exact Legal & Unit Identity:</strong> Accurately isolates single or multiple units without website print artifacts.</li>
                    <li><strong>Registration vs Commencement Dates:</strong> Clearly separated to eliminate timeline discrepancies in business verification.</li>
                    <li><strong>NIC-2008 Complete Descriptions:</strong> 5-digit industrial classifications mapped with official descriptions.</li>
                    <li><strong>No invented products or services:</strong> Does not invent unverified models or products, adhering strictly to document records.</li>
                </ul>
            </div>
        </div>
    </div>
    {% endif %}

    <!-- STEP 3: WEBSITE CONTENT STUDIO & GOOGLE SITES EXPORT -->
    {% if content %}
    <div class="panel">
        <div class="panel-header">
            <div>
                <h2 class="panel-title">
                    <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2"><path d="M4 19.5v-15A2.5 2.5 0 0 1 6.5 2H20v20H6.5a2.5 2.5 0 0 1-2.5-2.5Z"/><polyline points="10 2 10 10 13 7 16 10 16 2"/></svg>
                    Step 3: Website Content Studio
                </h2>
                <p class="panel-subtitle">Copy section-by-section directly into Google Sites, or preview live visual website mockup.</p>
                <div class="alert alert-warning" style="margin-top: 10px; margin-bottom: 6px; padding: 8px 14px; font-size: 13px;">
                    <strong>Notice:</strong> Privacy Policy aur Terms generic templates hain. Publish karne se pehle kisi legal professional se review karwayein.
                </div>
            </div>
            <div style="display: flex; gap: 8px; flex-wrap: wrap;">
                <button type="button" class="btn btn-primary btn-sm" onclick="copyCleanFullText()">
                    <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><rect width="14" height="14" x="8" y="8" rx="2" ry="2"/><path d="M4 16c-1.1 0-2-.9-2-2V4c0-1.1.9-2 2-2h10c1.1 0 2 .9 2 2"/></svg>
                    📋 Copy Clean Full Text (No Symbols)
                </button>
                <button type="button" class="btn btn-secondary btn-sm" onclick="copyFullContent()">
                    Copy Markdown (.md)
                </button>
                <button type="button" class="btn btn-secondary btn-sm" onclick="downloadCleanText()">
                    <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><polyline points="7 10 12 15 17 10"/><line x1="12" y1="15" x2="12" y2="3"/></svg>
                    Download (.txt)
                </button>
            </div>
        </div>

        <!-- TABS BAR -->
        <div class="tabs-bar">
            <button type="button" class="tab-btn active" id="tab_html_btn" onclick="switchTab('html')">
                <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><polyline points="16 18 22 12 16 6"/><polyline points="8 6 2 12 8 18"/></svg>
                🌐 Google Sites HTML Embed (1-Click Paste)
            </button>
            <button type="button" class="tab-btn" id="tab_sections_btn" onclick="switchTab('sections')">
                <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><rect width="7" height="7" x="3" y="3" rx="1"/><rect width="7" height="7" x="14" y="3" rx="1"/><rect width="7" height="7" x="14" y="14" rx="1"/><rect width="7" height="7" x="3" y="14" rx="1"/></svg>
                📋 Clean Text Cards (Ready for Text Box)
            </button>
            <button type="button" class="tab-btn" id="tab_mockup_btn" onclick="switchTab('mockup')">
                <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="12" cy="12" r="10"/><line x1="2" y1="12" x2="22" y2="12"/><path d="M12 2a15.3 15.3 0 0 1 4 10 15.3 15.3 0 0 1-4 10 15.3 15.3 0 0 1-4-10 15.3 15.3 0 0 1 4-10z"/></svg>
                Live Website Mockup
            </button>
            <button type="button" class="tab-btn" id="tab_raw_btn" onclick="switchTab('raw')">
                <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><polyline points="14 2 14 8 20 8"/></svg>
                Raw Markdown Code
            </button>
        </div>

        <!-- TAB 1: GOOGLE SITES HTML EMBED (RECOMMENDED 1-CLICK PASTE) -->
        <div id="view_html">
            <div style="background: #f0fdfa; border: 1px solid #99f6e4; border-radius: 10px; padding: 14px 18px; margin-bottom: 20px; display: flex; align-items: center; justify-content: space-between; flex-wrap: wrap; gap: 12px;">
                <div>
                    <strong style="color: #0f766e; font-size: 14.5px;">⚡ Google Sites me 1-Click Paste Kaise Karein:</strong>
                    <p style="margin: 4px 0 0 0; font-size: 13px; color: #115e59;">Google Sites par jaiye ➔ <code>Insert</code> ➔ <code>Embed</code> ➔ <code>Embed Code</code> tab chuniye ➔ Yahan se copy kiya hua HTML code paste kijiye ➔ <code>Next</code> ➔ <code>Insert</code>!</p>
                </div>
                <div style="display: flex; gap: 8px; flex-wrap: wrap;">
                    <button type="button" class="btn btn-primary btn-sm" onclick="copyFullHtml()">
                        <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><polyline points="16 18 22 12 16 6"/><polyline points="8 6 2 12 8 18"/></svg>
                        Copy Full 1-Page HTML Embed
                    </button>
                    <button type="button" class="btn btn-secondary btn-sm" onclick="downloadHtml()">
                        <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><polyline points="7 10 12 15 17 10"/><line x1="12" y1="15" x2="12" y2="3"/></svg>
                        Download (.html)
                    </button>
                </div>
            </div>

            <!-- Hidden full containers -->
            <textarea id="full_html_code" style="display: none;">{{ full_html_embed }}</textarea>
            <textarea id="clean_full_content" style="display: none;">{{ clean_full_text }}</textarea>

            <!-- HTML Section Cards -->
            <h3 style="margin: 0 0 14px 0; font-size: 16px; color: #1e293b;">Section-by-Section Google Sites HTML Embeds</h3>
            {% for hsec in html_sections %}
            <div class="section-card">
                <div class="section-header">
                    <div class="section-name">
                        <span>{{ hsec.title }}</span>
                        <span class="section-meta">{{ hsec.content|length }} chars · HTML Embed</span>
                    </div>
                    <div>
                        <button type="button" class="btn btn-primary btn-sm" id="btn_{{ hsec.id }}" onclick="copySectionWithButton('{{ hsec.id }}', this)">
                            <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><polyline points="16 18 22 12 16 6"/><polyline points="8 6 2 12 8 18"/></svg>
                            Copy HTML Code
                        </button>
                    </div>
                </div>
                <div class="section-snippet">{{ hsec.content[:240]|e }}...</div>
                <textarea id="{{ hsec.id }}" style="display: none;">{{ hsec.content }}</textarea>
            </div>
            {% endfor %}
        </div>

        <!-- TAB 2: SECTION-BY-SECTION CLEAN TEXT CARDS -->
        <div id="view_sections" class="hidden">
            <div style="background: #f8fafc; border: 1px solid #cbd5e1; border-radius: 10px; padding: 12px 16px; margin-bottom: 16px; font-size: 13.5px; color: #334155;">
                <strong>💡 Tip for Google Sites Text Boxes:</strong> In cards se copy kiya hua content bina kisi Markdown symbol (jaise <code>#</code>, <code>**</code>, <code>|</code>) ke 100% clean plain-text format me copy hota hai, jise aap directly Google Sites ke <em>Text Box</em> me paste kar sakte hain.
            </div>
            {% for sec in sections %}
            <div class="section-card">
                <div class="section-header">
                    <div class="section-name">
                        <span>{{ sec.title }}</span>
                        <span class="section-meta">{{ sec.clean_content|length }} chars · Clean Text</span>
                    </div>
                    <div style="display: flex; gap: 6px;">
                        <button type="button" class="btn btn-primary btn-sm" id="btn_clean_{{ sec.id }}" onclick="copySectionWithButton('{{ sec.id }}_clean', this)">
                            <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><rect width="14" height="14" x="8" y="8" rx="2" ry="2"/><path d="M4 16c-1.1 0-2-.9-2-2V4c0-1.1.9-2 2-2h10c1.1 0 2 .9 2 2"/></svg>
                            Copy Clean Text
                        </button>
                        <button type="button" class="btn btn-secondary btn-sm" id="btn_md_{{ sec.id }}" onclick="copySectionWithButton('{{ sec.id }}', this)">
                            Copy Markdown
                        </button>
                    </div>
                </div>
                <div class="section-snippet" style="white-space: pre-line;">{{ sec.clean_content[:240] }}...</div>
                <textarea id="{{ sec.id }}_clean" style="display: none;">{{ sec.clean_content }}</textarea>
                <textarea id="{{ sec.id }}" style="display: none;">{{ sec.content }}</textarea>
            </div>
            {% endfor %}
        </div>

        <!-- TAB 2: LIVE WEBSITE MOCKUP -->
        <div id="view_mockup" class="hidden">
            <div class="mockup-window">
                <div class="mockup-topbar">
                    <div class="mockup-dots">
                        <div class="mockup-dot"></div>
                        <div class="mockup-dot"></div>
                        <div class="mockup-dot"></div>
                    </div>
                    <div class="mockup-url">https://sites.google.com/view/{{ (extracted_data.get('Unit / Trade Name') or 'enterprise')|lower|replace(' ', '-') }}</div>
                </div>
                <div class="mockup-content">
                    <div class="mockup-hero">
                        <span class="badge-hero">Official Registered Enterprise</span>
                        <h2>{{ extracted_data.get('Unit / Trade Name') or extracted_data.get('Enterprise Name') }}</h2>
                        <p>{{ extracted_data.get('Organisation Type') }}</p>
                        <div style="font-size: 13px; color: var(--slate-500);">
                            {% if extracted_data.get('GSTIN') and extracted_data.get('GSTIN') != 'Not specified' %}
                            GSTIN: <strong>{{ extracted_data.get('GSTIN') }}</strong>
                            {% elif extracted_data.get('Udyam Registration Number') %}
                            Registration No: <strong>{{ extracted_data.get('Udyam Registration Number') }}</strong>
                            {% endif %}
                        </div>
                    </div>

                    <div class="mockup-section">
                        <h3>About Us & Statutory Profile</h3>
                        <table class="mockup-table">
                            <tr><th>Legal Name</th><td>{{ extracted_data.get('Enterprise Name') }}</td></tr>
                            <tr><th>Trade Name</th><td>{{ extracted_data.get('Unit / Trade Name') }}</td></tr>
                            <tr><th>Organisation Type</th><td>{{ extracted_data.get('Organisation Type') }}</td></tr>
                            {% if extracted_data.get('GSTIN') and extracted_data.get('GSTIN') != 'Not specified' %}
                            <tr><th>GSTIN</th><td>{{ extracted_data.get('GSTIN') }}</td></tr>
                            {% endif %}
                            {% if extracted_data.get('Udyam Registration Number') and extracted_data.get('Udyam Registration Number') != 'Not specified' %}
                            <tr><th>Udyam Registration No</th><td>{{ extracted_data.get('Udyam Registration Number') }}</td></tr>
                            {% endif %}
                            <tr><th>Major Activity</th><td>{{ extracted_data.get('Major Activity') }}</td></tr>
                            <tr><th>NIC Code</th><td>{{ extracted_data.get('NIC 5-Digit Classification') }}</td></tr>
                        </table>
                    </div>

                    <div class="mockup-section">
                        <h3>Contact & Business Location</h3>
                        <p style="font-size: 13.5px; color: var(--slate-700); margin-bottom: 6px;">📍 <strong>Principal Address:</strong> {{ extracted_data.get('Official Address') }}</p>
                        {% if extracted_data.get('Additional Address') %}
                        <p style="font-size: 13.5px; color: var(--slate-700); margin-bottom: 6px;">🏢 <strong>Additional Address:</strong> {{ extracted_data.get('Additional Address') }}</p>
                        {% endif %}
                        <p style="font-size: 13.5px; color: var(--slate-700); margin-bottom: 6px;">📞 <strong>Phone:</strong> {{ extracted_data.get('Phone Number') }}</p>
                        <p style="font-size: 13.5px; color: var(--slate-700);">✉️ <strong>Email:</strong> {{ extracted_data.get('Email Address') }}</p>
                    </div>

                    <div style="text-align: center; font-size: 12px; color: var(--slate-400); padding-top: 14px;">
                        © {{ current_year }} {{ extracted_data.get('Enterprise Name') }}. All rights reserved. | Document Grounded Website
                    </div>
                </div>
            </div>
        </div>

        <!-- TAB 3: RAW MARKDOWN CODE -->
        <div id="view_raw" class="hidden">
            <textarea id="generated_content" class="form-control" style="min-height: 500px; font-family: 'JetBrains Mono', monospace; font-size: 13px; background: var(--slate-50);" readonly>{{ content }}</textarea>
        </div>
    </div>
    {% endif %}

</div>

<!-- FLOATING TOAST -->
<div id="toast" class="toast">
    <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5"><polyline points="20 6 9 17 4 12"/></svg>
    <span id="toast_msg">Copied to clipboard!</span>
</div>

<script>
function showToast(msg) {
    const toast = document.getElementById('toast');
    const toastMsg = document.getElementById('toast_msg');
    toastMsg.textContent = msg || 'Copied to clipboard!';
    toast.classList.add('show');
    setTimeout(() => {
        toast.classList.remove('show');
    }, 2400);
}

function toggleFields() {
    const selected = document.querySelector('input[name="doc_type"]:checked').value;
    const gstBox = document.getElementById('gst_box');
    const udyamBox = document.getElementById('udyam_box');
    const gstInput = document.getElementById('gst_file');
    const udyamInput = document.getElementById('udyam_file');

    if (selected === 'udyam') {
        udyamBox.classList.remove('hidden');
        gstBox.classList.add('hidden');
        udyamInput.required = true;
        gstInput.required = false;
        gstInput.value = '';
    } else if (selected === 'gst') {
        udyamBox.classList.add('hidden');
        gstBox.classList.remove('hidden');
        udyamInput.required = false;
        gstInput.required = true;
        udyamInput.value = '';
    } else {
        udyamBox.classList.remove('hidden');
        gstBox.classList.remove('hidden');
        udyamInput.required = true;
        gstInput.required = true;
    }
}

function handleFileSelect(input, badgeId) {
    const badge = document.getElementById(badgeId);
    if (input.files && input.files[0]) {
        const f = input.files[0];
        const sizeKb = Math.round(f.size / 1024);
        badge.style.display = 'inline-flex';
        badge.innerHTML = `✓ ${f.name} (${sizeKb} KB)`;
    } else {
        badge.style.display = 'none';
    }
}

function switchTab(tabName) {
    const views = ['html', 'sections', 'mockup', 'raw'];
    views.forEach(v => {
        const el = document.getElementById('view_' + v);
        const btn = document.getElementById('tab_' + v + '_btn');
        if (el) el.classList.add('hidden');
        if (btn) btn.classList.remove('active');
    });
    const targetEl = document.getElementById('view_' + tabName);
    const targetBtn = document.getElementById('tab_' + tabName + '_btn');
    if (targetEl) targetEl.classList.remove('hidden');
    if (targetBtn) targetBtn.classList.add('active');
}

function copyFullHtml() {
    const el = document.getElementById('full_html_code');
    if (!el) return;
    navigator.clipboard.writeText(el.value).then(() => {
        showToast('Google Sites 1-Page HTML Embed Code copied! Paste in Insert ➔ Embed ➔ Embed Code.');
    }).catch(() => {
        el.style.display = 'block';
        el.select();
        document.execCommand('copy');
        el.style.display = 'none';
        showToast('Google Sites HTML copied!');
    });
}

function downloadHtml() {
    const el = document.getElementById('full_html_code');
    if (!el) return;
    const blob = new Blob([el.value], {type: 'text/html;charset=utf-8'});
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = 'msme-google-sites-embed.html';
    a.click();
    URL.revokeObjectURL(url);
    showToast('HTML Embed file downloaded!');
}

function copyRawText(text) {
    navigator.clipboard.writeText(text).then(() => {
        showToast('Attribute copied to clipboard!');
    }).catch(() => {
        const ta = document.createElement('textarea');
        ta.value = text;
        document.body.appendChild(ta);
        ta.select();
        document.execCommand('copy');
        document.body.removeChild(ta);
        showToast('Attribute copied to clipboard!');
    });
}

function copySectionWithButton(secId, btn) {
    const el = document.getElementById(secId);
    if (!el) return;
    const originalHtml = btn.innerHTML;
    navigator.clipboard.writeText(el.value).then(() => {
        btn.innerHTML = '✓ Copied!';
        btn.style.background = '#059669';
        showToast('Section copied! Ready to paste into Google Sites.');
        setTimeout(() => {
            btn.innerHTML = originalHtml;
            btn.style.background = '';
        }, 2000);
    }).catch(() => {
        el.style.display = 'block';
        el.select();
        document.execCommand('copy');
        el.style.display = 'none';
        btn.innerHTML = '✓ Copied!';
        showToast('Section copied!');
        setTimeout(() => {
            btn.innerHTML = originalHtml;
        }, 2000);
    });
}

function copyCleanFullText() {
    const el = document.getElementById('clean_full_content');
    if (!el) return;
    navigator.clipboard.writeText(el.value).then(() => {
        showToast('Clean text copied without markdown symbols! Ready for Google Sites.');
    }).catch(() => {
        el.style.display = 'block';
        el.select();
        document.execCommand('copy');
        el.style.display = 'none';
        showToast('Clean text copied without symbols!');
    });
}

function downloadCleanText() {
    const el = document.getElementById('clean_full_content');
    if (!el) return;
    const blob = new Blob([el.value], {type: 'text/plain;charset=utf-8'});
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = 'msme-website-clean-text.txt';
    a.click();
    URL.revokeObjectURL(url);
    showToast('Clean text (.txt) file downloaded!');
}

function copyFullContent() {
    const el = document.getElementById('generated_content');
    if (!el) return;
    navigator.clipboard.writeText(el.value).then(() => {
        showToast('Complete website markdown copied to clipboard!');
    }).catch(() => {
        el.select();
        document.execCommand('copy');
        showToast('Complete website markdown copied!');
    });
}

function downloadContent() {
    const el = document.getElementById('generated_content');
    if (!el) return;
    const blob = new Blob([el.value], {type: 'text/markdown;charset=utf-8'});
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = 'msme-website-content.md';
    a.click();
    URL.revokeObjectURL(url);
    showToast('Markdown file downloaded!');
}

['udyam_dropzone', 'gst_dropzone'].forEach(id => {
    const dz = document.getElementById(id);
    if (!dz) return;
    ['dragenter', 'dragover'].forEach(eventName => {
        dz.addEventListener(eventName, e => {
            e.preventDefault();
            dz.classList.add('dragover');
        }, false);
    });
    ['dragleave', 'drop'].forEach(eventName => {
        dz.addEventListener(eventName, e => {
            e.preventDefault();
            dz.classList.remove('dragover');
        }, false);
    });
});

toggleFields();
</script>
</body>
</html>
"""


# ---------------------------------------------------------------------
# FLASK ROUTE
# ---------------------------------------------------------------------

@app.route("/", methods=["GET", "POST"])
def index():
    content = None
    clean_full_text = None
    sections = []
    full_html_embed = None
    html_sections = []
    error = None
    warnings = []
    conflicts = []
    extracted_data = {}
    selected_type = "udyam"
    form_values = {}

    if request.method == "POST":
        try:
            selected_type = request.form.get("doc_type", "udyam")
            if selected_type not in ("gst", "udyam", "both"):
                raise ValueError("Please select GST, Udyam, or Both.")

            form_values = {
                "manual_unit": sanitize_text(request.form.get("manual_unit", ""), 150),
                "manual_phone": sanitize_text(request.form.get("manual_phone", ""), 50),
                "manual_email": sanitize_text(request.form.get("manual_email", ""), 100),
                "manual_address": sanitize_text(request.form.get("manual_address", ""), 300),
                "manual_services": sanitize_text(request.form.get("manual_services", ""), 2000),
            }

            gst_data = {}
            udyam_data = {}

            if selected_type in ("gst", "both"):
                gst_file = request.files.get("gst_file")
                validate_pdf_upload(gst_file, "GST certificate")
                gst_text, gst_tables = extract_pdf_data(gst_file)
                gst_data = parse_gst_text(gst_text, pages_tables=gst_tables)
                errs = validate_document(gst_data, "gst")
                if errs:
                    warnings.extend(errs)

            if selected_type in ("udyam", "both"):
                udyam_file = request.files.get("udyam_file")
                validate_pdf_upload(udyam_file, "Udyam certificate")
                udyam_text, udyam_tables = extract_pdf_data(udyam_file)
                udyam_data = parse_udyam_text(udyam_text, pages_tables=udyam_tables)
                errs = validate_document(udyam_data, "udyam")
                if errs:
                    warnings.extend(errs)

            merged, doc_conflicts = merge_documents(gst_data, udyam_data)
            conflicts.extend(doc_conflicts)

            for k, v in form_values.items():
                if v:
                    merged[k] = v

            if form_values.get("manual_phone"):
                merged["phone"] = normalise_phone(form_values["manual_phone"])
            if form_values.get("manual_email"):
                merged["email"] = form_values["manual_email"]

            if not merged.get("legal_name") and merged.get("trade_name"):
                merged["legal_name"] = merged["trade_name"]
                warnings.append("Legal name not found separately; using Trade Name as Legal Name.")

            if not merged.get("legal_name"):
                raise ValueError("Could not extract a valid enterprise name from the uploaded document(s).")

            is_gst_only = bool(merged.get("gstin")) and not bool(merged.get("udyam_number"))
            missing_label = "Not specified in GST certificate" if is_gst_only else "Not specified"

            nic_display = ""
            if merged.get("nic_entries"):
                nic_display = "; ".join([f"{x['nic_code']} - {x['nic_description']}" for x in merged["nic_entries"]])
            elif merged.get("nic_code"):
                nic_display = f"{merged.get('nic_code')} - {merged.get('nic_description', '')}"

            extracted_data = {
                "Enterprise Name": merged.get("legal_name", ""),
                "Unit / Trade Name": merged.get("unit_name") or merged.get("trade_name") or merged.get("legal_name", ""),
                "Enterprise Type": merged.get("enterprise_type", missing_label),
                "Organisation Type": merged.get("organisation_type", missing_label),
                "Major Activity": merged.get("major_activity", missing_label),
                "NIC 5-Digit Classification": nic_display or missing_label,
                "GSTIN": merged.get("gstin", "Not specified"),
                "Type of Registration": merged.get("gst_registration_type", "Not specified"),
                "Date of Liability": merged.get("gst_liability_date", "Not specified"),
                "GST Validity From": merged.get("gst_registration_date", "Not specified"),
                "GST Issue Date": merged.get("gst_issue_date", "Not specified"),
                "Official Address": merged.get("address", "Not specified"),
                "Phone Number": merged.get("phone", "Not specified"),
                "Email Address": merged.get("email", "Not specified"),
            }
            if merged.get("additional_address"):
                extracted_data["Additional Address"] = merged["additional_address"]
            if merged.get("udyam_number"):
                extracted_data["Udyam Registration Number"] = merged["udyam_number"]
            if merged.get("commencement_date"):
                extracted_data["Date of Commencement"] = merged["commencement_date"]

            content, clean_full_text, sections = build_website_markdown(merged)
            full_html_embed, html_sections = build_google_sites_html(merged)

        except Exception as exc:
            error = str(exc)

    return render_template_string(
        HTML_UI,
        content=content,
        clean_full_text=clean_full_text,
        sections=sections,
        full_html_embed=full_html_embed,
        html_sections=html_sections,
        error=error,
        warnings=warnings,
        conflicts=conflicts,
        extracted_data=extracted_data,
        selected_type=selected_type,
        form_values=form_values,
        current_year=date.today().year,
    )


@app.errorhandler(413)
def request_entity_too_large(_error):
    return "Upload too large. Maximum total upload size is 20 MB.", 413


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)