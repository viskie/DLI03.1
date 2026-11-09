import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from cryptography.fernet import Fernet

from status_artifact import StatusArtifactError, decrypt_status, encrypt_status
from status_generator import build_status
from contracts import build_daily_contracts


class StatusArtifactTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.instance = Path(self.temporary.name) / 'instance-2'
        results = self.instance / 'results/prediction_scopes'
        results.mkdir(parents=True)
        connection = sqlite3.connect(results / 'outcomes.sqlite')
        connection.execute('''CREATE TABLE outcomes(
            outcome_id TEXT PRIMARY KEY, scope_id TEXT, cycle_at_ms INTEGER, rank INTEGER,
            target_at_ms INTEGER, verified_at_ms INTEGER, origin_at_ms INTEGER,
            market TEXT, horizon_seconds INTEGER, base_price REAL, predicted_price REAL,
            actual_price REAL, deviation_percent REAL, actual_move_percent REAL,
            direction_correct INTEGER, absolute_error REAL, forecast_config_id TEXT,
            model_sha256 TEXT, status TEXT, was_validated INTEGER,
            validation_failures TEXT, actual_observed_at_ms INTEGER, resolution_source TEXT)''')
        values = [
            ('actual','scope',1000,1,2000,2050,1000,'BTC/USDT',1000,100.0,102.0,103.0,-0.9,3.0,1,0.01,'cfg','model','resolved',1,'[]',2050,'live'),
            ('pending','scope',1000,2,3000,None,1000,'ETH/USDT',2000,10.0,11.0,None,None,None,None,None,'cfg','model','pending',0,'[]',None,None),
        ]
        connection.executemany('INSERT INTO outcomes VALUES (' + ','.join('?' * 23) + ')', values)
        connection.commit()
        connection.close()
        (self.instance / 'state.json').write_text(json.dumps({'status':'completed','updatedAtMs':1000}))
        (self.instance / 'shadow.json').write_text(json.dumps({'status':'running','lastProcessedAtMs':3500000}))
        (results / 'scope.json').write_text(json.dumps({'createdAtMs':3500000,'scopeId':'scope','status':'ready','selectedCount':50,'requestedCount':100,'tradeEligibleCount':5}))

    def tearDown(self):
        self.temporary.cleanup()

    def test_deterministic_status_and_split_logs(self):
        first = build_status(self.instance, now_ms=3_600_000, previous_artifact_sha256='abc')
        second = build_status(self.instance, now_ms=3_600_000, previous_artifact_sha256='abc')
        self.assertEqual(first, second)
        self.assertEqual(first['predictionLogs']['actualized']['count'], 1)
        self.assertEqual(first['predictionLogs']['pending']['count'], 1)
        self.assertEqual(first['round']['previousArtifactSha256'], 'abc')
        example = first['valuation']['purchaseExamples'][1]
        self.assertEqual(example['purchaseUsd'], 700)
        self.assertGreater(example['oracle961QUnits'], 0)
        self.assertEqual(len(first['dailyContracts']['contracts']), 3)
        self.assertFalse(first['dailyContracts']['available'])

    def test_three_daily_contracts_have_separate_access_fee_and_bot_capital(self):
        source = {
            'generatedAtMs': 3_599_000, 'scopeStatus': 'ready', 'scopeId': 'scope',
            'sourceInstance': 'o369', 'sourceInstanceKey': 'instance-2',
            'tradeEligibleNowCount': 2, 'blockedReasons': [],
            'tradeQuantum': {'investmentInr': 1055},
            'usdInr': {'rate': 84.4, 'observedAtMs': 3_599_000, 'basis': 'test'},
            'rows': [
                {'rank': 1, 'market': 'BTC/INR', 'forecastHorizonSeconds': 86_400, 'targetAtMs': 10_000_000, 'signalId': 'a'},
                {'rank': 2, 'market': 'ETH/INR', 'forecastHorizonSeconds': 43_200, 'targetAtMs': 20_000_000, 'signalId': 'b'},
            ],
        }
        catalogue = build_daily_contracts(source, now_ms=3_600_000,
                                           prediction_quantum_reference_usd=1.0)
        self.assertTrue(catalogue['available'])
        self.assertEqual(catalogue['sourceInstance'], 'o369')
        self.assertEqual(catalogue['sourceInstanceKey'], 'instance-2')
        self.assertTrue(all(item['sourceInstance'] == 'o369' for item in catalogue['contracts']))
        self.assertEqual([item['accessDays'] for item in catalogue['contracts']], [1, 3, 9])
        self.assertEqual([item['dailyBatchSize'] for item in catalogue['contracts']], [2, 2, 2])
        self.assertTrue(all(item['estimatedBotCapitalInr'] >= 2 * 1055 for item in catalogue['contracts']))
        self.assertTrue(all(item['estimatedBotCapitalUsd'] > 0 for item in catalogue['contracts']))
        self.assertTrue(all(item['accessFeeCents'] > 0 for item in catalogue['contracts']))
        self.assertNotEqual(catalogue['contracts'][0]['accessFeeCents'],
                            catalogue['contracts'][0]['estimatedBotCapitalInr'] * 100)

    def test_poc_contracts_publish_ranked_rows_without_trade_qualification(self):
        source = {
            'generatedAtMs': 3_599_000, 'scopeStatus': 'ready', 'scopeId': 'scope',
            'sourceInstance': 'o369', 'sourceInstanceKey': 'instance-2',
            'issuanceMode': 'poc-research', 'tradeEligibleNowCount': 0,
            'blockedReasons': ['low-confidence'],
            'tradeQuantum': {'investmentInr': 1055},
            'usdInr': {'rate': 84.4, 'observedAtMs': 3_599_000, 'basis': 'test'},
            'rows': [{'rank': 1, 'market': 'BTC/INR', 'forecastHorizonSeconds': 900,
                      'targetAtMs': 10_000_000, 'signalId': 'poc-a',
                      'tradeQualified': False}],
        }
        catalogue = build_daily_contracts(source, now_ms=3_600_000,
                                           prediction_quantum_reference_usd=1.0)
        self.assertTrue(catalogue['available'])
        self.assertTrue(catalogue['proofOfConcept'])
        self.assertEqual(catalogue['contracts'][0]['status'], 'available')
        self.assertFalse(catalogue['contracts'][0]['tradeQualified'])
        self.assertFalse(catalogue['contracts'][0]['executionEligible'])
        self.assertIn('low-confidence', catalogue['contracts'][0]['sourceWarnings'])

    def test_contract_windows_include_every_available_row_without_thirty_row_cap(self):
        day = 86_400_000
        rows = [
            {'rank': rank, 'market': f'M{rank}/INR', 'targetAtMs': 10_000_000 + rank,
             'forecastHorizonSeconds': 900, 'signalId': f'one-{rank}'}
            for rank in range(1, 36)
        ]
        rows.extend([
            {'rank': 36, 'market': 'TWO/INR', 'targetAtMs': 2 * day,
             'forecastHorizonSeconds': day, 'signalId': 'two'},
            {'rank': 37, 'market': 'EIGHT/INR', 'targetAtMs': 8 * day,
             'forecastHorizonSeconds': day, 'signalId': 'eight'},
            {'rank': 38, 'market': 'TEN/INR', 'targetAtMs': 10 * day,
             'forecastHorizonSeconds': day, 'signalId': 'outside'},
        ])
        source = {
            'generatedAtMs': 3_599_000, 'scopeStatus': 'ready', 'scopeId': 'windows',
            'issuanceMode': 'poc-research', 'tradeEligibleNowCount': 0,
            'tradeQuantum': {'investmentInr': 1055},
            'usdInr': {'rate': 84.4, 'observedAtMs': 3_599_000, 'basis': 'test'},
            'rows': rows,
        }
        catalogue = build_daily_contracts(source, now_ms=3_600_000,
                                           prediction_quantum_reference_usd=1.0)
        self.assertEqual(catalogue['catalogueVersion'], 'all-window-targets-v2')
        self.assertEqual([item['dailyBatchSize'] for item in catalogue['contracts']],
                         [35, 36, 37])
        self.assertEqual([len(catalogue['predictionBatches'][item['contractId']])
                          for item in catalogue['contracts']], [35, 36, 37])
        self.assertNotIn('outside', {row['signalId'] for batch in
                         catalogue['predictionBatches'].values() for row in batch})

    def test_authenticated_round_trip_and_wrong_key(self):
        payload = build_status(self.instance, now_ms=3_600_000)
        key = Fernet.generate_key()
        artifact = encrypt_status(payload, key)
        self.assertEqual(decrypt_status(artifact, key), payload)
        with self.assertRaises(StatusArtifactError):
            decrypt_status(artifact, Fernet.generate_key())


if __name__ == '__main__':
    unittest.main()
