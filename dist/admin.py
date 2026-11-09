"""Local administration, encrypted provider settings, and first-party aggregate analytics."""
import csv
import hmac
import io
import os
import re
import secrets
import time
from collections import Counter
from datetime import datetime, timezone
from functools import wraps
from pathlib import Path
from urllib.parse import urlparse
from flask import jsonify, redirect, request, session, send_from_directory, Response
from werkzeug.security import check_password_hash, generate_password_hash

ADMIN_ID = '__local_admin__'
ADMIN_HASH = generate_password_hash('pass')
FIELDS = {
    'RAZORPAY_HOSTED_LIVE': 'Hosted pages mode: false = test, true = live',
    'RAZORPAY_PAGE_INVESTMENT': 'Plan 1 Payment Page URL (USD, Amount field)',
    'RAZORPAY_PAGE_PREPAID_PLUS': 'Plan 2 Payment Page URL (USD, Amount field)',
    'RAZORPAY_KEY_ID': 'Razorpay Key ID', 'RAZORPAY_KEY_SECRET': 'Razorpay Key Secret',
    'RAZORPAY_WEBHOOK_SECRET': 'Razorpay webhook secret', 'ORACLE_CHECKOUT_ENABLED': 'Enable checkout',
    'GOOGLE_CLIENT_ID': 'Google Client ID', 'GOOGLE_CLIENT_SECRET': 'Google Client Secret', 'GOOGLE_REDIRECT_URI': 'Google callback URL',
    'FACEBOOK_CLIENT_ID': 'Facebook App ID', 'FACEBOOK_CLIENT_SECRET': 'Facebook App Secret',
    'FACEBOOK_GRAPH_VERSION': 'Facebook Graph API version', 'FACEBOOK_REDIRECT_URI': 'Facebook callback URL',
    'LINKEDIN_CLIENT_ID': 'LinkedIn Client ID', 'LINKEDIN_CLIENT_SECRET': 'LinkedIn Client Secret', 'LINKEDIN_REDIRECT_URI': 'LinkedIn callback URL',
}
SECRET_FIELDS = {key for key in FIELDS if 'SECRET' in key}

def local_request():
    return request.remote_addr in {'127.0.0.1', '::1'} and request.host.split(':')[0] in {'127.0.0.1', 'localhost'} and not request.headers.get('X-Forwarded-For') and not os.getenv('VERCEL')

def fallback_user(data):
    profile = data.get('admin_profile', {})
    return {'id': ADMIN_ID, 'name': profile.get('name', 'Administrator' if os.getenv('ORACLE_STORAGE') == 'blob' else 'Local administrator'), 'email': profile.get('email', 'admin@localhost.local'), 'provider': 'local-admin', 'role': 'admin'}

def admin_session_allowed():
    return local_request() or bool(os.getenv('ORACLE_ADMIN_PASSWORD_HASH'))

def fallback_matches(data, identifier, password):
    expected = data.get('admin_profile', {}).get('password') or os.getenv('ORACLE_ADMIN_PASSWORD_HASH') or ADMIN_HASH
    return identifier == 'admin' and admin_session_allowed() and isinstance(password, str) and len(password) <= 128 and check_password_hash(expected, password)

def audit(data, action, target=''):
    rows = data.setdefault('audit', [])
    rows.append({'time': time.time(), 'actor': session.get('uid', ''), 'action': action, 'target': target})
    del rows[:-1000]

def clean_user(user):
    return {key: user.get(key, default) for key, default in [('id',''),('name',''),('email',''),('provider','password'),('role','member'),('disabled',False),('created',None)]}

def init_admin(app, store, oauth, reload_providers, contracts_provider=None):
    # Saved provider settings override process defaults; no secret values are returned by the API.
    with store.transaction() as data:
        saved = data.get('provider_settings', {})
    for key, value in saved.items():
        if key in FIELDS:
            os.environ[key] = value
    reload_providers()
    import hashlib
    import json
    import threading
    config_lock = threading.RLock()
    settings_signature = hashlib.sha256(json.dumps(saved, sort_keys=True).encode()).hexdigest()

    @app.before_request
    def refresh_provider_settings():
        nonlocal settings_signature
        if not os.getenv('ORACLE_STORAGE') == 'blob':
            return
        with store.transaction() as data:
            latest = data.get('provider_settings', {})
        signature = hashlib.sha256(json.dumps(latest, sort_keys=True).encode()).hexdigest()
        if signature != settings_signature:
            with config_lock:
                for key, value in latest.items():
                    if key in FIELDS:
                        os.environ[key] = value
                oauth._registry.clear()
                oauth._clients.clear()
                reload_providers()
                settings_signature = signature

    def require_admin(fn):
        @wraps(fn)
        def wrapped(*args, **kwargs):
            with store.transaction() as data:
                uid = session.get('uid')
                user = fallback_user(data) if uid == ADMIN_ID and admin_session_allowed() else data['users'].get(uid)
                allowed = user and not user.get('disabled') and user.get('role') == 'admin'
            if not allowed:
                return jsonify(error='Administrator access required.'), 403
            return fn(*args, **kwargs)
        return wrapped

    @app.get('/Oracle691/admin')
    def admin_page():
        with store.transaction() as data:
            uid = session.get('uid')
            user = fallback_user(data) if uid == ADMIN_ID and admin_session_allowed() else data['users'].get(uid)
            allowed = user and not user.get('disabled') and user.get('role') == 'admin'
        if not allowed:
            return redirect('/Oracle691?admin=signin')
        return send_from_directory(Path(__file__).parent / 'templates','admin.html') if (Path(__file__).parent / 'templates/admin.html').exists() else send_from_directory(Path(__file__).parent / 'Oracle691','admin.html')

    @app.get('/api/admin/dashboard')
    @require_admin
    def dashboard():
        with store.transaction() as data:
            users = [clean_user(u) for u in data['users'].values()]
            payments = list(data['payments'].values())
            orders = list(data['orders'].values())
            analytics = data.get('analytics', {})
            rows = list(data.get('audit', []))[-30:][::-1]
            profile = fallback_user(data) if session['uid'] == ADMIN_ID else clean_user(data['users'][session['uid']])
        totals = Counter()
        live_payments = [p for p in payments if p.get('environment') != 'test']
        for payment in live_payments:
            totals[payment['plan']] += payment['amount']
        catalogue = contracts_provider() if contracts_provider else {'available': False, 'contracts': []}
        return jsonify(users=users, payments=payments, orders=orders, analytics=analytics, audit=rows, profile=profile,
                       daily_contracts=catalogue,
                       summary={'users':len(users), 'active':sum(not u['disabled'] for u in users), 'payments':len(live_payments),
                                'revenue':sum(p['amount'] for p in live_payments), 'orders':len(orders)}, revenue_by_plan=dict(totals))

    @app.post('/api/admin/users')
    @require_admin
    def manage_user():
        value = request.get_json(silent=True) or {}
        if not isinstance(value, dict):
            return jsonify(error='Invalid request.'), 400
        uid = value.get('id')
        if not isinstance(uid, str):
            return jsonify(error='Choose a user.'), 400
        with store.transaction() as data:
            user = data['users'].get(uid)
            if not user:
                return jsonify(error='User not found.'), 404
            name = str(value.get('name', user['name'])).strip()
            email = str(value.get('email', user['email'])).strip().lower()
            role = value.get('role', user.get('role', 'member'))
            disabled = value.get('disabled', user.get('disabled', False))
            if not 1 <= len(name) <= 80 or len(email) > 254 or not re.fullmatch(r'[^\s@]+@[^\s@]+\.[^\s@]+', email) or role not in ('admin','member') or type(disabled) is not bool:
                return jsonify(error='Check the name, email, role and account status.'), 400
            if any(u['id'] != uid and u['email'] == email for u in data['users'].values()):
                return jsonify(error='Email already belongs to another account.'), 409
            if uid == session['uid'] and (disabled or role != 'admin'):
                return jsonify(error='You cannot disable or demote your own administrator account.'), 400
            if user.get('provider') != 'password' and email != user['email']:
                return jsonify(error='Social account email is managed by the sign-in provider.'), 400
            user.update(name=name, email=email, role=role, disabled=disabled)
            audit(data,'user.updated',uid)
        return jsonify(ok=True)

    @app.post('/api/admin/profile')
    @require_admin
    def admin_profile():
        value = request.get_json(silent=True) or {}
        if not isinstance(value, dict):
            return jsonify(error='Invalid request.'), 400
        name = str(value.get('name','')).strip()
        email = str(value.get('email','')).strip().lower()
        if not 1 <= len(name) <= 80 or len(email) > 254 or not re.fullmatch(r'[^\s@]+@[^\s@]+\.[^\s@]+',email):
            return jsonify(error='Enter a name and valid email.'), 400
        with store.transaction() as data:
            user = data.setdefault('admin_profile', {}) if session['uid'] == ADMIN_ID else data['users'][session['uid']]
            password = value.get('new_password')
            if password:
                if not isinstance(password,str) or not 12 <= len(password) <= 128 or not check_password_hash(user.get('password') or (os.getenv('ORACLE_ADMIN_PASSWORD_HASH') or ADMIN_HASH if session['uid'] == ADMIN_ID else ''),str(value.get('current_password',''))):
                    return jsonify(error='Check your current password and use 12–128 characters for the new password.'), 400
                user['password'] = generate_password_hash(password)
            if session['uid'] != ADMIN_ID and email != user['email']:
                return jsonify(error='Change account email in User management.'),400
            user.update(name=name,email=email)
            audit(data,'admin.profile.updated',session['uid'])
        return jsonify(ok=True)

    @app.get('/api/admin/settings')
    @require_admin
    def settings():
        result = {}
        for key, label in FIELDS.items():
            value = os.getenv(key,'')
            result[key] = {'label':label, 'secret':key in SECRET_FIELDS, 'configured':bool(value), 'value': '' if key in SECRET_FIELDS else value}
        return jsonify(fields=result,services={name: bool(oauth._registry.get(name)) for name in ('google','facebook','linkedin')},checkout_enabled=os.getenv('ORACLE_CHECKOUT_ENABLED') == 'true' and bool(os.getenv('RAZORPAY_KEY_ID') and os.getenv('RAZORPAY_KEY_SECRET')))

    @app.post('/api/admin/settings')
    @require_admin
    def save_settings():
        value = request.get_json(silent=True) or {}
        updates = value.get('values', {}) if isinstance(value,dict) else None
        clear = value.get('clear', []) if isinstance(value,dict) else None
        if not isinstance(updates,dict) or not isinstance(clear,list) or any(k not in FIELDS for k in updates) or any(not isinstance(k,str) or k not in FIELDS for k in clear):
            return jsonify(error='Unknown settings.'),400
        validated = {}
        for key, val in updates.items():
            if not isinstance(val,str) or len(val) > 4096 or any(c in val for c in '\r\n\x00'):
                return jsonify(error='Invalid setting value.'),400
            val = val.strip()
            if not val:
                continue # Blank secret fields keep their existing values.
            if key in ('ORACLE_CHECKOUT_ENABLED', 'RAZORPAY_HOSTED_LIVE') and val not in ('true','false'):
                return jsonify(error='Checkout must be true or false.'),400
            if key == 'FACEBOOK_GRAPH_VERSION' and not re.fullmatch(r'v\d+\.\d+',val):
                return jsonify(error='Use a Graph API version such as v25.0 from your Meta app.'),400
            if key.endswith('_REDIRECT_URI'):
                url = urlparse(val)
                if url.scheme not in ('http','https') or not url.hostname or url.username or url.password or url.fragment or url.query or (url.scheme == 'http' and url.hostname not in ('localhost','127.0.0.1')):
                    return jsonify(error='Callback URL must use HTTPS, or HTTP on localhost.'),400
            if key.startswith('RAZORPAY_PAGE_'):
                from payments import valid_page_url
                if not valid_page_url(val):
                    return jsonify(error='Use the full HTTPS pages.razorpay.com Payment Page URL.'),400
            validated[key]=val
        effective = {key: os.getenv(key,'') for key in FIELDS}
        effective.update(validated)
        for key in clear:
            effective[key] = ''
        if 'ORACLE_CHECKOUT_ENABLED' not in updates and 'ORACLE_CHECKOUT_ENABLED' not in clear:
            validated['ORACLE_CHECKOUT_ENABLED'] = 'true' if all(effective.get(key) for key in ('RAZORPAY_KEY_ID','RAZORPAY_KEY_SECRET','RAZORPAY_WEBHOOK_SECRET')) else 'false'
        with store.transaction() as data:
            settings = data.setdefault('provider_settings',{})
            if 'ORACLE_CHECKOUT_ENABLED' in updates and updates['ORACLE_CHECKOUT_ENABLED'] in ('true','false'):
                data['checkout_manually_disabled'] = updates['ORACLE_CHECKOUT_ENABLED'] == 'false'
            elif 'ORACLE_CHECKOUT_ENABLED' in clear:
                data.pop('checkout_manually_disabled', None)
            elif data.get('checkout_manually_disabled'):
                validated['ORACLE_CHECKOUT_ENABLED'] = 'false'
            for key in clear:
                settings[key]=''
            settings.update(validated)
            audit(data,'provider.settings.updated',','.join(sorted(set(validated)|set(clear))))
        for key in clear:
            os.environ[key]=''
        os.environ.update(validated)
        oauth._registry.clear()
        oauth._clients.clear()
        reload_providers()
        return jsonify(ok=True,message='Settings saved. Complete services are active immediately; no restart needed.', services={name: bool(oauth._registry.get(name)) for name in ('google','facebook','linkedin')}, checkout_enabled=os.getenv('ORACLE_CHECKOUT_ENABLED') == 'true' and bool(os.getenv('RAZORPAY_KEY_ID') and os.getenv('RAZORPAY_KEY_SECRET')))

    @app.get('/api/admin/reports.csv')
    @require_admin
    def reports_csv():
        kind = request.args.get('type','payments')
        with store.transaction() as data:
            if kind == 'users':
                rows = [clean_user(u) for u in data['users'].values()]
                fields = ['id','name','email','provider','role','disabled','created']
            elif kind == 'payments':
                rows = list(data['payments'].values())
                fields = ['payment_id','user_id','plan','amount','status','created']
            elif kind == 'audit':
                rows = data.get('audit',[])
                fields = ['time','actor','action','target']
            else:
                return jsonify(error='Unknown report.'),400
        out=io.StringIO(newline='')
        writer=csv.writer(out)
        writer.writerow(fields)
        for row in rows:
            values=[]
            for field in fields:
                val=str(row.get(field,''))
                if val.lstrip().startswith(('=','+','-','@')):
                    val="'"+val
                values.append(val)
            writer.writerow(values)
        return Response(out.getvalue(),mimetype='text/csv',headers={'Content-Disposition':f'attachment; filename="oracle-{kind}.csv"','Cache-Control':'no-store'})

    def track(kind, path=''):
        day = datetime.now(timezone.utc).strftime('%Y-%m-%d')
        session.setdefault('visitor',secrets.token_urlsafe(18))
        with store.transaction() as data:
            analytics=data.setdefault('analytics',{})
            daily=analytics.setdefault(day,{'views':{},'events':{},'visitors':[]})
            if kind == 'pageview':
                daily['views'][path]=daily['views'].get(path,0)+1
            else:
                daily['events'][kind]=daily['events'].get(kind,0)+1
            # Random per-browser session ID, no IP addresses or query strings stored.
            if session['visitor'] not in daily['visitors'] and len(daily['visitors']) < 10000:
                daily['visitors'].append(session['visitor'])
            for old in sorted(analytics)[:-90]:
                del analytics[old]

    @app.after_request
    def analytics(response):
        if os.getenv('ORACLE_STORAGE') != 'blob' and request.method == 'GET' and response.status_code == 200 and request.path in ('/','/Oracle691','/Oracle691/') and not session.get('uid') == ADMIN_ID:
            try:
                track('pageview',request.path)
            except Exception:
                # Analytics are best effort and cannot undo a successful page response.
                app.logger.warning('Page analytics could not be recorded.')
        return response

    @app.post('/api/analytics/event')
    def analytics_event():
        value=request.get_json(silent=True) or {}
        event=value.get('event') if isinstance(value,dict) else None
        if event not in ('pageview','plan.investment','plan.prepaid_plus','registration.opened','checkout.started'):
            return jsonify(error='Unknown event.'),400
        now=time.time()
        attempts=[t for t in session.get('event_times',[]) if now-t < 60]
        if len(attempts) >= 30:
            return jsonify(error='Event limit reached.'),429
        attempts.append(now)
        session['event_times']=attempts
        track(event,'/Oracle691' if event == 'pageview' else '')
        return jsonify(ok=True)
