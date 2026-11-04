"""Razorpay API client. Credentials stay on the server."""
import base64
import json
import os
from urllib.request import Request, urlopen

PLANS = {
    'investment': {
        'name': 'Investment-linked subscription tokens', 'number': 1,
        'min_amount': 30000, 'max_amount': 1000000,
        'description': 'Proposed investment rights linked to subscription tokens.',
        'features': ['Subscription tokens with defined investment rights', 'Potential profit-sharing, appreciation or redemption rights', 'Rights subject to definitive offering terms'],
        'notice': 'May constitute securities or other regulated investment products. No return or redemption is promised by this preview.'
    },
    'prepaid_plus': {
        'name': 'Tradable prepaid subscriptions Plus', 'number': 2,
        'min_amount': 500000, 'max_amount': 5000000,
        'description': 'Future API access and resale of unused rights, plus Plan 1 investment-linked benefits.',
        'features': ['Prepaid rights to future API access', 'Proposed resale of unused service rights', 'Includes Plan 1 investment-linked benefits, subject to terms'],
        'notice': 'The API component derives value from services. Adding Plan 1 investment rights means this combined plan is not a service-only offering.'
    },
}

def configured():
    key = os.getenv('RAZORPAY_KEY_ID', '')
    return bool(key and os.getenv('RAZORPAY_KEY_SECRET') and os.getenv('ORACLE_CHECKOUT_ENABLED') == 'true')

def validate_amount(plan, amount):
    if not isinstance(plan, str) or plan not in PLANS:
        raise ValueError('Choose a valid plan.')
    limits = PLANS[plan]
    if type(amount) is not int or not limits['min_amount'] <= amount <= limits['max_amount'] or amount % 100:
        raise ValueError('Choose a whole-dollar amount within the plan range.')
    return amount

def razorpay(path, data=None):
    auth = base64.b64encode((os.environ['RAZORPAY_KEY_ID'] + ':' + os.environ['RAZORPAY_KEY_SECRET']).encode()).decode()
    request = Request('https://api.razorpay.com/v1/' + path, data=json.dumps(data).encode() if data is not None else None,
                      headers={'Authorization': 'Basic ' + auth, 'Content-Type': 'application/json'})
    with urlopen(request, timeout=20) as response:
        return json.load(response)


PAGE_KEYS = {'investment': 'RAZORPAY_PAGE_INVESTMENT', 'prepaid_plus': 'RAZORPAY_PAGE_PREPAID_PLUS'}

def valid_page_url(value):
    from urllib.parse import urlsplit
    try:
        url = urlsplit(value)
        return url.scheme == 'https' and url.hostname == 'pages.razorpay.com' and not url.username and not url.password and url.port in (None, 443) and not url.fragment and bool(url.path.strip('/'))
    except ValueError:
        return False

def page_status():
    return {plan: valid_page_url(os.getenv(key, '')) for plan, key in PAGE_KEYS.items()}

def payment_page_url(plan, amount):
    from urllib.parse import urlsplit, urlunsplit, parse_qsl, urlencode
    validate_amount(plan, amount)
    value = os.getenv(PAGE_KEYS[plan], '')
    if not valid_page_url(value):
        raise ValueError('This plan is awaiting its Razorpay Payment Page URL.')
    url = urlsplit(value)
    query = dict(parse_qsl(url.query))
    query['amount'] = str(amount // 100)
    return urlunsplit((url.scheme, url.netloc, url.path, urlencode(query), ''))
