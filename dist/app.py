import hashlib
import hmac
import json
import os
import re
import secrets
import time
import uuid
from functools import wraps
from pathlib import Path
from flask import Flask, Response, jsonify, redirect, request, send_from_directory, session, url_for
from werkzeug.security import check_password_hash, generate_password_hash
from authlib.integrations.flask_client import OAuth
from dotenv import load_dotenv
from store import Store
from admin import ADMIN_ID, fallback_matches, fallback_user, local_request, admin_session_allowed, init_admin, audit
from payments import PLANS, configured, razorpay, validate_amount, page_status, payment_page_url
from status_artifact import StatusArtifactError, decrypt_status

ROOT = Path(__file__).parent
load_dotenv(ROOT / '.env.oracle.local', override=False)
CLOUD = os.getenv('ORACLE_STORAGE') == 'blob'
if os.getenv('VERCEL') and not CLOUD:
    raise RuntimeError('Vercel requires encrypted persistent Blob storage.')
if CLOUD:
    from blob_store import BlobStore
    store = BlobStore()
    signing_key = os.environ['FLASK_SECRET_KEY']
else:
    store = Store(os.getenv('ORACLE_DATA_DIR') or str(ROOT / 'instance'))
    secretfile = store.path / '.session-key'
    if not secretfile.exists():
        try:
            fd = os.open(secretfile, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, 'w') as output:
                output.write(secrets.token_hex(32))
        except FileExistsError:
            pass
    signing_key = os.getenv('FLASK_SECRET_KEY') or secretfile.read_text()
PUBLIC_ROOT = ROOT / 'public' if (ROOT / 'public').is_dir() else ROOT
app = Flask(__name__, static_folder=None)
app.config.update(SECRET_KEY=signing_key, SESSION_COOKIE_NAME='oracle691_session', SESSION_COOKIE_HTTPONLY=True,
                  SESSION_COOKIE_SAMESITE='Lax', SESSION_COOKIE_SECURE=CLOUD or os.getenv('ORACLE_HTTPS') == 'true', MAX_CONTENT_LENGTH=16384)
oauth = OAuth(app)
def register_oauth_providers():
    for name, metadata in {
        'google': 'https://accounts.google.com/.well-known/openid-configuration',
        'linkedin': 'https://www.linkedin.com/oauth/.well-known/openid-configuration',
    }.items():
        prefix = name.upper()
        if os.getenv(prefix + '_CLIENT_ID') and os.getenv(prefix + '_CLIENT_SECRET'):
            oauth.register(name=name, client_id=os.environ[prefix + '_CLIENT_ID'], client_secret=os.environ[prefix + '_CLIENT_SECRET'],
                           server_metadata_url=metadata, client_kwargs={'scope': 'openid email profile'})
    version = os.getenv('FACEBOOK_GRAPH_VERSION', '')
    if os.getenv('FACEBOOK_CLIENT_ID') and os.getenv('FACEBOOK_CLIENT_SECRET') and re.fullmatch(r'v[0-9]+\.[0-9]+', version):
        oauth.register(name='facebook', client_id=os.environ['FACEBOOK_CLIENT_ID'], client_secret=os.environ['FACEBOOK_CLIENT_SECRET'],
                       authorize_url=f'https://www.facebook.com/{version}/dialog/oauth',
                       access_token_url=f'https://graph.facebook.com/{version}/oauth/access_token',
                       api_base_url=f'https://graph.facebook.com/{version}/',
                       client_kwargs={'scope': 'email public_profile', 'token_endpoint_auth_method': 'client_secret_post'})

register_oauth_providers()

@app.after_request
def headers(response):
    response.headers['X-Content-Type-Options'] = 'nosniff'
    response.headers['Referrer-Policy'] = 'strict-origin-when-cross-origin'
    response.headers['X-Frame-Options'] = 'DENY'
    if request.path.startswith('/api/'):
        response.headers['Cache-Control'] = 'no-store'
    return response

@app.before_request
def csrf():
    if request.method == 'POST' and request.path != '/api/razorpay/webhook':
        if not session.get('csrf') or not hmac.compare_digest(session['csrf'], request.headers.get('X-CSRF-Token', '')):
            return jsonify(error='Your session expired. Reload the page and try again.'), 403

def public_user(user):
    return {**{k: user[k] for k in ('id', 'name', 'email', 'provider')}, 'role': user.get('role','member')}

def signed_in(fn):
    @wraps(fn)
    def wrapped(*args, **kwargs):
        if not session.get('uid'):
            return jsonify(error='Please sign in first.'), 401
        with store.transaction() as data:
            user = data['users'].get(session['uid'])
            if not user or user.get('disabled'):
                session.clear()
                return jsonify(error='Your account is unavailable.'),403
        return fn(*args, **kwargs)
    return wrapped

def body():
    value = request.get_json(silent=True)
    return value if isinstance(value, dict) else {}

def limit_auth(data):
    key = hashlib.sha256((request.remote_addr or 'local').encode()).hexdigest()
    now = time.time()
    attempts = data['attempts'].get(key, [])
    attempts = [t for t in attempts if now-t < 900]
    if len(attempts) >= 20:
        return False
    attempts.append(now)
    data['attempts'][key] = attempts
    return True

@app.get('/api/session')
def current():
    session.setdefault('csrf', secrets.token_urlsafe(32))
    with store.transaction() as data:
        user = fallback_user(data) if session.get('uid') == ADMIN_ID and admin_session_allowed() else data['users'].get(session.get('uid'))
        if user and user.get('disabled'):
            session.clear()
            user = None
        holdings = [p for p in data['payments'].values() if p['user_id'] == session.get('uid')]
    return jsonify(csrf=session['csrf'], user=public_user(user) if user else None, holdings=holdings, access=(user or {}).get('access', {}),
                   analytics_client=CLOUD, google_enabled=bool(oauth._registry.get('google')), oauth_providers={name: bool(oauth._registry.get(name)) for name in ('google', 'facebook', 'linkedin')}, checkout_enabled=configured(), payment_pages=page_status(), plans=PLANS,
                   daily_contract_checkout_enabled=configured() and os.getenv('ORACLE_DAILY_CONTRACT_CHECKOUT_ENABLED') == 'true',
                   daily_contracts=public_daily_contracts(),
                   payment_mode='test' if os.getenv('RAZORPAY_KEY_ID', '').startswith('rzp_test_') else 'live' if configured() else 'unconfigured')

@app.post('/api/register')
def register():
    value = body()
    email = str(value.get('email', '')).strip().lower()
    name = str(value.get('name', '')).strip()
    password = value.get('password', '')
    if not re.fullmatch(r'[^\s@]+@[^\s@]+\.[^\s@]+', email) or len(email) > 254 or not 1 <= len(name) <= 80 or not isinstance(password, str) or not 12 <= len(password) <= 128:
        return jsonify(error='Enter a name, valid email, and a password of 12–128 characters.'), 400
    with store.transaction() as data:
        if not limit_auth(data):
            return jsonify(error='Too many attempts. Try again in 15 minutes.'), 429
        if any(u['email'] == email for u in data['users'].values()):
            return jsonify(error='Unable to create this account. Try signing in.'), 409
        uid = uuid.uuid4().hex
        data['users'][uid] = {'id': uid, 'name': name, 'email': email, 'password': generate_password_hash(password), 'provider': 'password', 'role':'member', 'created':time.time()}
    session.clear()
    session.update(uid=uid, csrf=secrets.token_urlsafe(32))
    return jsonify(ok=True), 201

@app.post('/api/login')
def login():
    value = body()
    with store.transaction() as data:
        if not limit_auth(data):
            return jsonify(error='Too many attempts. Try again in 15 minutes.'), 429
        identifier = str(value.get('email','')).strip().lower()
        if fallback_matches(data, identifier, value.get('password')):
            session.clear()
            session.update(uid=ADMIN_ID, csrf=secrets.token_urlsafe(32))
            audit(data,'admin.login',ADMIN_ID)
            return jsonify(ok=True,redirect='/Oracle691/admin')
        user = next((u for u in data['users'].values() if u['email'] == identifier), None)
        password = value.get('password', '')
        if not isinstance(password, str) or len(password) > 128 or not user or user.get('disabled') or not user.get('password') or not check_password_hash(user['password'], password):
            return jsonify(error='Email or password is incorrect.'), 401
        uid = user['id']
    session.clear()
    session.update(uid=uid, csrf=secrets.token_urlsafe(32))
    return jsonify(ok=True,redirect='/Oracle691/admin' if user.get('role') == 'admin' else None)

@app.post('/api/logout')
def logout():
    session.clear()
    return jsonify(ok=True)

@app.post('/api/account')
@signed_in
def account():
    value = body()
    name = str(value.get('name', '')).strip()
    if not 1 <= len(name) <= 80:
        return jsonify(error='Enter a name of 1–80 characters.'), 400
    with store.transaction() as data:
        user = data['users'][session['uid']]
        if value.get('new_password'):
            password = value['new_password']
            if not user.get('password') or not check_password_hash(user['password'], str(value.get('current_password', ''))) or not isinstance(password, str) or not 12 <= len(password) <= 128:
                return jsonify(error='Check your current password and use 12–128 characters for the new password.'), 400
            user['password'] = generate_password_hash(password)
        user['name'] = name
    return jsonify(ok=True)

OAUTH_PROVIDERS = ('google', 'facebook', 'linkedin')

@app.get('/auth/<provider>')
def provider_login(provider):
    if provider not in OAUTH_PROVIDERS:
        return 'Not found', 404
    if not oauth._registry.get(provider):
        return redirect('/Oracle691?auth=unavailable')
    callback = os.getenv(provider.upper() + '_REDIRECT_URI', f'http://127.0.0.1:8000/auth/{provider}/callback')
    return oauth.create_client(provider).authorize_redirect(callback)

def oauth_user(provider, info):
    subject = str(info.get('sub', ''))
    email = str(info.get('email', '')).strip().lower()
    if not subject or len(subject) > 255 or not re.fullmatch(r'[^\s@]+@[^\s@]+\.[^\s@]+', email) or len(email) > 254:
        raise ValueError('Provider did not return the required account details.')
    if provider in ('google', 'linkedin') and info.get('email_verified') is not True:
        raise ValueError('Provider email is not verified.')
    with store.transaction() as data:
        user = next((u for u in data['users'].values() if u.get(provider + '_sub') == subject), None)
        if not user:
            # Do not link accounts based on email alone.
            if any(u['email'] == email for u in data['users'].values()):
                return None
            uid = uuid.uuid4().hex
            user = {'id': uid, 'name': str(info.get('name') or email)[:80], 'email': email, 'provider': provider, provider + '_sub': subject, 'role':'member', 'created':time.time()}
            data['users'][uid] = user
    if user.get('disabled'):
        raise ValueError('Account disabled')
    return user

@app.get('/auth/<provider>/callback')
def provider_callback(provider):
    if provider not in OAUTH_PROVIDERS:
        return 'Not found', 404
    if not oauth._registry.get(provider):
        return redirect('/Oracle691?auth=unavailable')
    try:
        client = oauth.create_client(provider)
        token = client.authorize_access_token()  # Authlib verifies OAuth state; OIDC also verifies the ID token.
        if provider == 'facebook':
            proof = hmac.new(os.environ['FACEBOOK_CLIENT_SECRET'].encode(), token['access_token'].encode(), hashlib.sha256).hexdigest()
            response = client.get('me', params={'fields': 'id,name,email', 'appsecret_proof': proof}, token=token, timeout=15)
            response.raise_for_status()
            info = response.json()
            info['sub'] = info.get('id')
        else:
            info = token.get('userinfo')
            if not info:
                raise ValueError('Missing validated OIDC claims')
        user = oauth_user(provider, info)
        if user is None:
            return redirect('/Oracle691?auth=existing')
        session.clear()
        session.update(uid=user['id'], csrf=secrets.token_urlsafe(32))
        return redirect('/Oracle691/admin' if user.get('role') == 'admin' else '/Oracle691')
    except Exception:
        return redirect('/Oracle691?auth=failed')

@app.post('/api/payment-page')
@signed_in
def hosted_payment_page():
    value = body()
    try:
        url = payment_page_url(value.get('plan'), value.get('amount'))
    except ValueError as error:
        return jsonify(error=str(error)), 400
    from urllib.parse import urlsplit, urlunsplit, parse_qsl, urlencode
    reference = 'oracle_' + secrets.token_hex(20)
    with store.transaction() as data:
        data.setdefault('hosted_intents', {})[reference] = {
            'user_id': session['uid'], 'plan': value['plan'], 'amount': value['amount'],
            'created': time.time(), 'environment': 'live' if os.getenv('RAZORPAY_HOSTED_LIVE') == 'true' else 'test'}
    parts = urlsplit(url)
    query = dict(parse_qsl(parts.query))
    query['portal_reference'] = reference
    url = urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), ''))
    return jsonify(url=url, message='Payment will be confirmed by a signed Razorpay notification.')

@app.post('/api/checkout')
@signed_in
def checkout():
    if not configured():
        return jsonify(error='Checkout is awaiting Razorpay setup. Request access to discuss a plan.'), 503
    value = body()
    try:
        if value.get('action') == 'order':
            private_contract = private_daily_contract(value.get('contract_id')) if value.get('contract_id') else None
            contract = private_contract.get('contract') if private_contract else None
            if contract:
                if os.getenv('ORACLE_DAILY_CONTRACT_CHECKOUT_ENABLED') != 'true':
                    return jsonify(error='Daily contract checkout is awaiting commercial and payment approval.'), 503
                if contract.get('status') != 'available' or int(contract.get('purchaseUntilMs') or 0) <= int(time.time() * 1000):
                    return jsonify(error='This daily contract is not currently available.'), 409
                amount = int(contract.get('contractPriceCents') or contract.get('accessFeeCents') or 0)
                if value.get('amount') != amount or amount < 100:
                    return jsonify(error='The contract price changed. Refresh before continuing.'), 409
                plan = 'daily_contract'
                plan_name = str(contract.get('name') or contract['contractId'])
            else:
                plan = value.get('plan')
                try:
                    amount = validate_amount(plan, value.get('amount'))
                except ValueError as error:
                    return jsonify(error=str(error)), 400
                plan_name = PLANS[plan]['name']
            with store.transaction() as data:
                pending = [o for o in data['orders'].values() if o['user_id'] == session['uid'] and time.time()-o['created'] < 3600]
                if len(pending) >= 10:
                    return jsonify(error='Too many checkout attempts. Please try later.'), 429
            order = razorpay('orders', {'amount': amount, 'currency': 'USD', 'receipt': 'oracle_' + uuid.uuid4().hex[:24], 'notes': {'plan': plan, 'contract_id': contract.get('contractId') if contract else ''}})
            with store.transaction() as data:
                data['orders'][order['id']] = {'id': order['id'], 'user_id': session['uid'], 'plan': plan, 'plan_name': plan_name, 'contract_id': contract.get('contractId') if contract else None, 'contract_snapshot': contract, 'prediction_batch': private_contract.get('predictionBatch', []) if private_contract else [], 'amount': amount, 'created': time.time()}
            return jsonify(order_id=order['id'], amount=order['amount'], currency=order['currency'], key=os.environ['RAZORPAY_KEY_ID'], name=plan_name)
        if value.get('action') == 'verify':
            order_id = str(value.get('razorpay_order_id', ''))
            payment_id = str(value.get('razorpay_payment_id', ''))
            with store.transaction() as data:
                order = data['orders'].get(order_id)
            if not order or order['user_id'] != session['uid'] or not re.fullmatch(r'pay_[A-Za-z0-9]+', payment_id):
                return jsonify(error='Unknown order.'), 400
            expected = hmac.new(os.environ['RAZORPAY_KEY_SECRET'].encode(), (order_id + '|' + payment_id).encode(), hashlib.sha256).hexdigest()
            if not hmac.compare_digest(expected, str(value.get('razorpay_signature', ''))):
                return jsonify(error='Payment verification failed.'), 400
            result = record_payment(razorpay('payments/' + payment_id))
            return jsonify(result), 200 if result['status'] == 'captured' else 202
        return jsonify(error='Invalid action.'), 400
    except Exception:
        return jsonify(error='Unable to confirm payment with Razorpay. If debited, keep your payment ID and contact support before retrying.'), 502

def record_payment(payment):
    with store.transaction() as data:
        order = data['orders'].get(payment.get('order_id'))
        if not order or payment.get('amount') != order['amount'] or payment.get('currency') != 'USD':
            raise ValueError('Payment mismatch')
        if payment.get('status') != 'captured':
            return {'status': 'pending', 'message': 'Awaiting payment capture. Your account will update after confirmation.'}
        data['payments'].setdefault(payment['id'], {'payment_id': payment['id'], 'user_id': order['user_id'], 'plan': order['plan'], 'contract_id': order.get('contract_id'), 'contract_snapshot': order.get('contract_snapshot'), 'prediction_batch': order.get('prediction_batch', []), 'amount': order['amount'], 'created': time.time(), 'status': 'Paid · access active' if order.get('contract_id') else 'Paid · access activation pending', 'plan_name': order.get('plan_name') or PLANS.get(order['plan'], {}).get('name', order['plan'])})
    return {'status': 'captured', 'message': 'Payment confirmed. Your receipt is saved in My Holdings. Allocation and rights remain subject to the final offering terms and team confirmation.'}

def record_hosted_payment(payment):
    if not isinstance(payment, dict) or not re.fullmatch(r'pay_[A-Za-z0-9]+', str(payment.get('id', ''))):
        raise ValueError('Invalid payment')
    notes = payment.get('notes')
    notes = notes if isinstance(notes, dict) else {}
    reference = notes.get('portal_reference', '')
    with store.transaction() as data:
        if payment['id'] in data['payments']:
            return
        intent = data.setdefault('hosted_intents', {}).get(reference)
        user = data['users'].get(intent.get('user_id')) if intent else None
        matches = (intent and user and not user.get('disabled')
            and payment.get('status') == 'captured' and payment.get('captured') is True
            and type(payment.get('amount')) is int and payment['amount'] == intent['amount']
            and payment.get('currency') == 'USD'
            and str(payment.get('email', '')).strip().lower() == user['email'].lower()
            and not intent.get('payment_id'))
        if not matches:
            data.setdefault('payment_reviews', {})[payment['id']] = {
                'payment_id': payment['id'], 'reason': 'Hosted payment could not be matched; check reference, account email, USD amount and capture status.', 'created': time.time()}
            audit(data, 'payment.review.required', payment['id'])
            return
        live = intent['environment'] == 'live'
        plan = intent['plan']
        intent['payment_id'] = payment['id']
        data['payments'][payment['id']] = {
            'payment_id': payment['id'], 'user_id': intent['user_id'], 'plan': plan,
            'plan_name': PLANS[plan]['name'], 'amount': intent['amount'], 'created': time.time(),
            'status': 'Paid · subscription access active' if live else 'Test payment · no live access',
            'environment': intent['environment'], 'source': 'payment_page'}
        if live:
            user.setdefault('access', {})[plan] = {'active': True, 'payment_id': payment['id'], 'activated': time.time()}
        audit(data, 'payment.hosted.confirmed' if live else 'payment.hosted.test', payment['id'])

@app.post('/api/razorpay/webhook')
def webhook():
    secret = os.getenv('RAZORPAY_WEBHOOK_SECRET')
    if not secret:
        return jsonify(error='Webhook not configured.'), 503
    expected = hmac.new(secret.encode(), request.get_data(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, request.headers.get('X-Razorpay-Signature', '')):
        return jsonify(error='Invalid signature.'), 400
    value = body()
    try:
        if value.get('event') == 'payment.captured':
            payment = value['payload']['payment']['entity']
            with store.transaction() as data:
                known_order = payment.get('order_id') in data['orders']
            if known_order:
                record_payment(payment)
            else:
                record_hosted_payment(payment)
        return jsonify(ok=True)
    except (KeyError, ValueError):
        return jsonify(error='Unknown or mismatched payment.'), 400

@app.get('/Oracle691')
@app.get('/Oracle691/')
def oracle():
    return send_from_directory(ROOT / 'templates', 'oracle.html') if (ROOT / 'templates/oracle.html').exists() else send_from_directory(PUBLIC_ROOT / 'Oracle691', 'index.html')

def oracle_status_bytes():
    if CLOUD:
        from blob_store import BlobHTTPClient
        result = BlobHTTPClient().get(os.getenv('ORACLE_STATUS_BLOB_PATH', 'oracle691/status/stat.i'))
        if result is None:
            raise FileNotFoundError('Oracle 961 status has not been published.')
        return b''.join(result.stream)
    path = Path(os.getenv('ORACLE_STATUS_LOCAL_FILE') or ROOT / 'instance/oracle961-status/stat.i')
    return path.read_bytes()

def oracle_status_payload():
    key = os.getenv('ORACLE_STATUS_KEY')
    if not key and not CLOUD:
        try:
            key = (ROOT / 'instance/oracle961-status/status.key').read_text().strip()
        except OSError:
            pass
    if not key:
        raise RuntimeError('Oracle 961 status decryption is not configured.')
    return decrypt_status(oracle_status_bytes(), key)

def public_daily_contracts():
    try:
        catalogue = oracle_status_payload().get('dailyContracts', {})
    except (FileNotFoundError, OSError, RuntimeError, StatusArtifactError):
        return {'schemaVersion': 1, 'available': False, 'contracts': [], 'message': 'Awaiting the next authenticated production round.'}
    return {key: value for key, value in catalogue.items()
            if key not in {'predictionBatch', 'predictionBatches'}}

def daily_contract(contract_id):
    if not isinstance(contract_id, str):
        return None
    return next((item for item in public_daily_contracts().get('contracts', []) if item.get('contractId') == contract_id), None)

def private_daily_contract(contract_id):
    if not isinstance(contract_id, str):
        return None
    try:
        catalogue = oracle_status_payload().get('dailyContracts', {})
    except (FileNotFoundError, OSError, RuntimeError, StatusArtifactError):
        return None
    contract = next((item for item in catalogue.get('contracts', []) if item.get('contractId') == contract_id), None)
    batches = catalogue.get('predictionBatches', {})
    batch = batches.get(contract_id, []) if isinstance(batches, dict) else []
    if not batch:
        batch = catalogue.get('predictionBatch', [])
    return {'contract': contract, 'predictionBatch': batch} if contract else None

@app.get('/Oracle691/stat.i')
def oracle_status_artifact():
    try:
        content = oracle_status_bytes()
    except (FileNotFoundError, OSError, RuntimeError):
        return 'Oracle 961 status is not available.', 503
    etag = hashlib.sha256(content).hexdigest()
    return Response(content, mimetype='application/octet-stream', headers={
        'Cache-Control': 'public, max-age=300, must-revalidate',
        'Content-Disposition': 'inline; filename="stat.i"',
        'ETag': '"' + etag + '"',
    })

@app.get('/api/oracle961/status')
def oracle_status_json():
    """Server-side decryption boundary used by the forthcoming game UI."""
    try:
        payload = oracle_status_payload()
    except (FileNotFoundError, OSError, RuntimeError, StatusArtifactError):
        return jsonify(error='Oracle 961 status is unavailable or failed authentication.'), 503
    if isinstance(payload.get('dailyContracts'), dict):
        payload['dailyContracts'] = {
            key: value for key, value in payload['dailyContracts'].items()
            if key not in {'predictionBatch', 'predictionBatches'}
        }
    return jsonify(payload)

@app.get('/api/oracle961/contracts')
def oracle_daily_contracts():
    return jsonify(public_daily_contracts())

@app.get('/api/oracle961/contracts/<contract_id>/signals')
@signed_in
def oracle_contract_signals(contract_id):
    now_ms = int(time.time() * 1000)
    with store.transaction() as data:
        holding = next((payment for payment in data['payments'].values()
                        if payment.get('user_id') == session['uid']
                        and payment.get('contract_id') == contract_id), None)
    snapshot = (holding or {}).get('contract_snapshot') or {}
    if not holding or int(snapshot.get('accessUntilMs') or 0) <= now_ms:
        return jsonify(error='An active purchase of this contract is required.'), 403
    return jsonify(schemaVersion=1, contract=snapshot,
                   batchSha256=snapshot.get('batchSha256'), rows=holding.get('prediction_batch', []))

@app.get('/api/oracle961/outcomes')
def oracle_actualized_outcomes():
    """Public, presentation-safe view of the authenticated hourly outcome log."""
    key = os.getenv('ORACLE_STATUS_KEY')
    if not key and not CLOUD:
        try:
            key = (ROOT / 'instance/oracle961-status/status.key').read_text().strip()
        except OSError:
            pass
    if not key:
        return jsonify(error='Oracle 961 outcomes are not configured.'), 503
    try:
        payload = decrypt_status(oracle_status_bytes(), key)
    except (FileNotFoundError, OSError, RuntimeError, StatusArtifactError):
        return jsonify(error='Oracle 961 outcomes are unavailable or failed authentication.'), 503
    log = payload.get('predictionLogs', {}).get('actualized', {})
    safe = []
    for row in log.get('rows', [])[:1000]:
        safe.append({key: row.get(key) for key in (
            'market', 'horizonSeconds', 'originAtMs', 'targetAtMs', 'verifiedAtMs',
            'basePrice', 'predictedPrice', 'actualPrice', 'deviationPercent',
            'actualMovePercent', 'directionCorrect', 'status', 'resolutionSource')})
    return jsonify(schemaVersion=1, generatedAtMs=payload.get('round', {}).get('generatedAtMs'),
        roundId=payload.get('round', {}).get('id'), totalActualized=int(log.get('count') or 0),
        publishedCount=len(safe), rows=safe)

@app.get('/Oracle691/<path:filename>')
def oracle_asset(filename):
    return send_from_directory(PUBLIC_ROOT / 'Oracle691', filename)

@app.get('/')
def home():
    return send_from_directory(ROOT / 'templates', 'home.html') if (ROOT / 'templates/home.html').exists() else send_from_directory(PUBLIC_ROOT, 'index.html')

@app.get('/<path:filename>')
def static_file(filename):
    if filename not in {'index.html', 'product.html', 'product.css', 'style.css', 'script.js', 'logo-spiral.png', 'logo-spiral.svg', 'sandbox-login.html', 'sandbox-register.html', 'sandbox-dashboard.html', 'sandbox.css', 'sandbox.js'}:
        return 'Not found', 404
    return send_from_directory(PUBLIC_ROOT, filename)

if CLOUD:
    from blob_store import StoreConflict
    @app.errorhandler(StoreConflict)
    def concurrent_write(error):
        return jsonify(error='Another request updated this account. Please retry.'), 409

init_admin(app, store, oauth, register_oauth_providers, public_daily_contracts)

if __name__ == '__main__':
    app.run(host='127.0.0.1', port=8000, debug=False)
