import hashlib
import hmac
import importlib
import json
import os
from pathlib import Path
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
TEMP = tempfile.TemporaryDirectory()
os.environ['ORACLE_DATA_DIR'] = TEMP.name
import app as module

class PortalTests(unittest.TestCase):
    def setUp(self):
        with module.store.transaction() as data:
            data.update(users={}, orders={}, payments={}, attempts={})
        self.client = module.app.test_client()
        self.csrf = self.client.get('/api/session').json['csrf']

    def post(self, path, data):
        return self.client.post(path, json=data, headers={'X-CSRF-Token': self.csrf})

    def register(self):
        response = self.post('/api/register', {'name':'Test User','email':'test@example.test','password':'a long test password'})
        self.assertEqual(response.status_code,201)
        self.csrf = self.client.get('/api/session').json['csrf']

    def test_auth_and_encryption(self):
        self.register()
        raw = (module.store.path/'accounts.enc').read_bytes()
        self.assertNotIn(b'test@example.test', raw)
        self.assertNotIn(b'a long test password', raw)
        from store import Store
        reopened = Store(module.store.path)
        with reopened.transaction() as data:
            user = next(iter(data['users'].values()))
            self.assertTrue(user['password'].startswith('scrypt:'))
        self.assertEqual(self.post('/api/account', {'name':'Updated Name'}).status_code,200)
        self.assertEqual(self.post('/api/logout',{}).status_code,200)
        self.csrf = self.client.get('/api/session').json['csrf']
        self.assertEqual(self.post('/api/login', {'email':'test@example.test','password':'wrong'}).status_code,401)
        self.assertEqual(self.post('/api/login', {'email':'test@example.test','password':'a long test password'}).status_code,200)
        self.assertEqual(self.client.get('/api/session').json['user']['name'],'Updated Name')

    def test_csrf_and_private_routes(self):
        self.assertEqual(self.client.post('/api/register',json={}).status_code,403)
        self.assertEqual(self.post('/api/checkout',{}).status_code,401)
        self.assertEqual(self.client.get('/instance/accounts.enc').status_code,404)
        self.assertEqual(self.client.get('/.env.local').status_code,404)
        response = self.client.get('/Oracle691')
        self.assertEqual(response.status_code,200)
        response.close()
        self.assertEqual(self.client.get('/auth/google').status_code,302)

    def test_checkout_server_price_and_ownership(self):
        self.register()
        env={'RAZORPAY_KEY_ID':'rzp_test_fake','RAZORPAY_KEY_SECRET':'secret','ORACLE_CHECKOUT_ENABLED':'true'}
        def provider(path, data=None):
            if path == 'orders':
                self.assertEqual(data['amount'],30000)
                return {'id':'order_123','amount':30000,'currency':'USD'}
            return {'id':'pay_123','order_id':'order_123','amount':30000,'currency':'USD','status':'captured'}
        with patch.dict(os.environ,env), patch.object(module,'razorpay',side_effect=provider):
            response=self.post('/api/checkout',{'action':'order','plan':'investment','amount':30000})
            self.assertEqual(response.json['amount'],30000)
            signature=hmac.new(b'secret',b'order_123|pay_123',hashlib.sha256).hexdigest()
            payment={'action':'verify','razorpay_order_id':'order_123','razorpay_payment_id':'pay_123','razorpay_signature':signature}
            self.assertEqual(self.post('/api/checkout',payment).json['status'],'captured')
            self.assertEqual(self.post('/api/checkout',payment).status_code,200)
            self.assertEqual(len(self.client.get('/api/session').json['holdings']),1)
            payment['razorpay_signature']='tampered'
            self.assertEqual(self.post('/api/checkout',payment).status_code,400)
            stranger=module.app.test_client()
            csrf=stranger.get('/api/session').json['csrf']
            stranger.post('/api/register',json={'name':'Other','email':'other@example.test','password':'a long test password'},headers={'X-CSRF-Token':csrf})
            csrf=stranger.get('/api/session').json['csrf']
            payment['razorpay_signature']=signature
            self.assertEqual(stranger.post('/api/checkout',json=payment,headers={'X-CSRF-Token':csrf}).status_code,400)

    def test_daily_contract_uses_exact_server_price_and_preserves_batch(self):
        self.register()
        contract = {'contractId':'O961-DAILY','name':'Oracle 961 · 1-day bot list',
                    'status':'available','accessFeeCents':2500,
                    'purchaseUntilMs':int(time.time()*1000)+60_000,
                    'accessUntilMs':int(time.time()*1000)+86_400_000,
                    'batchSha256':'abc'}
        private = {'contract':contract,'predictionBatch':[{'market':'BTC/INR'}]}
        env={'RAZORPAY_KEY_ID':'rzp_test_fake','RAZORPAY_KEY_SECRET':'secret',
             'ORACLE_CHECKOUT_ENABLED':'true','ORACLE_DAILY_CONTRACT_CHECKOUT_ENABLED':'true'}
        def provider(path, data=None):
            if path == 'orders':
                self.assertEqual(data['amount'],2500)
                self.assertEqual(data['notes']['contract_id'],'O961-DAILY')
                return {'id':'order_daily','amount':2500,'currency':'USD'}
            return {'id':'pay_daily','order_id':'order_daily','amount':2500,'currency':'USD','status':'captured'}
        with patch.dict(os.environ,env), patch.object(module,'private_daily_contract',return_value=private), patch.object(module,'razorpay',side_effect=provider):
            self.assertEqual(self.post('/api/checkout',{'action':'order','contract_id':'O961-DAILY','amount':2499}).status_code,409)
            order=self.post('/api/checkout',{'action':'order','contract_id':'O961-DAILY','amount':2500}).json
            signature=hmac.new(b'secret',b'order_daily|pay_daily',hashlib.sha256).hexdigest()
            result=self.post('/api/checkout',{'action':'verify','razorpay_order_id':'order_daily','razorpay_payment_id':'pay_daily','razorpay_signature':signature})
            self.assertEqual(result.status_code,200)
            signals=self.client.get('/api/oracle961/contracts/O961-DAILY/signals')
            self.assertEqual(signals.status_code,200)
            self.assertEqual(signals.json['rows'][0]['market'],'BTC/INR')

    def test_social_registration_and_returning_signin(self):
        for provider in ('google','facebook','linkedin'):
            info={'sub':provider+'-123','email':provider+'@example.test','name':'Social User','email_verified':True}
            user=module.oauth_user(provider,info)
            self.assertEqual(user['provider'],provider)
            self.assertEqual(module.oauth_user(provider,info)['id'],user['id'])
            other=dict(info,sub='different-identity')
            self.assertIsNone(module.oauth_user(provider,other))
        with self.assertRaises(ValueError):
            module.oauth_user('google',{'sub':'test','email':'unverified@example.test','email_verified':False})
        for provider in ('google','facebook','linkedin'):
            self.assertIn('auth=unavailable',self.client.get('/auth/'+provider).location)
        self.assertEqual(self.client.get('/auth/unknown').status_code,404)

    def test_oidc_callback_and_failed_state(self):
        from unittest.mock import MagicMock
        client=MagicMock()
        client.authorize_access_token.return_value={'userinfo':{'sub':'oidc-123','email':'oidc@example.test','name':'OIDC User','email_verified':True}}
        with patch.dict(module.oauth._registry,{'google':{}}), patch.object(module.oauth,'create_client',return_value=client):
            # Empty provider registry entry is intentionally unavailable.
            self.assertIn('auth=unavailable',self.client.get('/auth/google/callback').location)
        with patch.dict(module.oauth._registry,{'google':{'configured':True}}), patch.object(module.oauth,'create_client',return_value=client):
            self.assertEqual(self.client.get('/auth/google/callback').location,'/Oracle691')
            self.assertEqual(self.client.get('/api/session').json['user']['provider'],'google')
            client.authorize_access_token.side_effect=ValueError('State mismatch')
            self.assertIn('auth=failed',self.client.get('/auth/google/callback').location)

    def test_amount_boundaries_and_configuration(self):
        from payments import validate_amount, configured
        for plan, low, high in [('investment',30000,1000000),('prepaid_plus',500000,5000000)]:
            for amount in [low,high]: self.assertEqual(validate_amount(plan,amount),amount)
            for amount in [low-100,high+100,True,low+1,str(low),float(low),None]:
                with self.assertRaises(ValueError): validate_amount(plan,amount)
        with patch.dict(os.environ,{'RAZORPAY_KEY_ID':'rzp_live_fake','RAZORPAY_KEY_SECRET':'secret','ORACLE_CHECKOUT_ENABLED':'true'}):
            self.assertTrue(configured())
        self.register()
        with patch.dict(os.environ,{'RAZORPAY_KEY_ID':'rzp_test_fake','RAZORPAY_KEY_SECRET':'secret','ORACLE_CHECKOUT_ENABLED':'true'}), patch.object(module,'razorpay') as provider:
            for amount in [29900,1000100,True,30001,'30000']:
                self.assertEqual(self.post('/api/checkout',{'action':'order','plan':'investment','amount':amount}).status_code,400)
            provider.assert_not_called()

    def test_capture_validation_and_webhook_replay(self):
        self.register()
        user=self.client.get('/api/session').json['user']
        with module.store.transaction() as data:
            data['orders']['order_1']={'id':'order_1','user_id':user['id'],'plan':'prepaid_plus','amount':130000,'created':0}
        payment={'id':'pay_1','order_id':'order_1','amount':130000,'currency':'USD','status':'authorized'}
        self.assertEqual(module.record_payment(payment)['status'],'pending')
        self.assertEqual(self.client.get('/api/session').json['holdings'],[])
        payment.update(status='captured',amount=1)
        with self.assertRaises(ValueError): module.record_payment(payment)
        payment['amount']=130000
        payload=json.dumps({'event':'payment.captured','payload':{'payment':{'entity':payment}}}).encode()
        sig=hmac.new(b'webhook',payload,hashlib.sha256).hexdigest()
        with patch.dict(os.environ,{'RAZORPAY_WEBHOOK_SECRET':'webhook'}):
            self.assertEqual(self.client.post('/api/razorpay/webhook',data=payload,content_type='application/json').status_code,400)
            for _ in range(2):
                self.assertEqual(self.client.post('/api/razorpay/webhook',data=payload,content_type='application/json',headers={'X-Razorpay-Signature':sig}).status_code,200)
        self.assertEqual(len(self.client.get('/api/session').json['holdings']),1)

if __name__=='__main__': unittest.main()
