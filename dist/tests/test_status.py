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

    def test_plan_three_prices_full_batch_and_keeps_exchange_capital_separate(self):
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
        self.assertEqual([item['predictionCap'] for item in catalogue['contracts']], [30, 100, 300])
        self.assertEqual([item['signalEntitlementCount'] for item in catalogue['contracts']], [2, 2, 2])
        self.assertTrue(all(item['estimatedBotCapitalInr'] >= 2 * 1055 for item in catalogue['contracts']))
        self.assertTrue(all(item['grossBatchCapitalInr'] == 2 * 1055 for item in catalogue['contracts']))
        self.assertTrue(all(item['requiredBatchCapitalInr'] == 2321 for item in catalogue['contracts']))
        self.assertTrue(all(item['estimatedBotCapitalInr'] == item['requiredBatchCapitalInr'] for item in catalogue['contracts']))
        self.assertTrue(all(item['estimatedBotCapitalUsd'] > 0 for item in catalogue['contracts']))
        self.assertTrue(all(item['planNumber'] == 3 for item in catalogue['contracts']))
        self.assertTrue(all(item['contractPriceCents'] == item['accessFeeCents'] for item in catalogue['contracts']))
        self.assertTrue(all(item['contractPriceCents'] == item['requiredExchangeCapitalCents'] for item in catalogue['contracts']))
        self.assertTrue(all(item['knownActualizationCashCents'] == 2 * item['contractPriceCents'] for item in catalogue['contracts']))
        self.assertTrue(all(item['researchAccessFeeCents'] > 0 for item in catalogue['contracts']))
        self.assertTrue(all(item['contractPriceCents'] <= 1_000_000 for item in catalogue['contracts']))
        self.assertTrue(all(item['botDeploymentStatus'] == 'not-defined' for item in catalogue['contracts']))

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

    def test_contract_windows_apply_tiered_prediction_caps(self):
        rows = [
            {'rank': rank, 'market': f'M{rank}/INR', 'targetAtMs': 10_000_000 + rank,
             'forecastHorizonSeconds': 900, 'signalId': f'one-{rank}'}
            for rank in range(1, 321)
        ]
        source = {
            'generatedAtMs': 3_599_000, 'scopeStatus': 'ready', 'scopeId': 'windows',
            'issuanceMode': 'poc-research', 'tradeEligibleNowCount': 0,
            'tradeQuantum': {'investmentInr': 1055},
            'usdInr': {'rate': 84.4, 'observedAtMs': 3_599_000, 'basis': 'test'},
            'rows': rows,
        }
        catalogue = build_daily_contracts(source, now_ms=3_600_000,
                                           prediction_quantum_reference_usd=1.0)
        self.assertEqual(catalogue['catalogueVersion'], 'plan-3-bot-contracts-v5')
        self.assertEqual([item['predictionCap'] for item in catalogue['contracts']],
                         [30, 100, 300])
        self.assertEqual([item['dailyBatchSize'] for item in catalogue['contracts']],
                         [30, 100, 300])
        self.assertEqual([item['signalEntitlementCount'] for item in catalogue['contracts']],
                         [30, 100, 300])
        self.assertEqual([item['grossBatchCapitalInr'] for item in catalogue['contracts']],
                         [31_650, 105_500, 316_500])
        self.assertEqual([item['requiredBatchCapitalInr'] for item in catalogue['contracts']],
                         [34_815, 116_050, 348_150])
        self.assertEqual([len(catalogue['predictionBatches'][item['contractId']])
                          for item in catalogue['contracts']], [30, 100, 300])
        self.assertEqual(catalogue['predictionBatches'][catalogue['contracts'][0]['contractId']][-1]['rank'], 30)
        self.assertEqual(catalogue['predictionBatches'][catalogue['contracts'][1]['contractId']][-1]['rank'], 100)
        self.assertEqual(catalogue['predictionBatches'][catalogue['contracts'][2]['contractId']][-1]['rank'], 300)

    def test_plan_three_holds_contract_above_ten_thousand_dollars(self):
        source = {
            'generatedAtMs': 3_599_000, 'scopeStatus': 'ready', 'scopeId': 'limit',
            'issuanceMode': 'poc-research', 'tradeEligibleNowCount': 0,
            'tradeQuantum': {'investmentInr': 1_000_000},
            'usdInr': {'rate': 80.0, 'observedAtMs': 3_599_000, 'basis': 'test'},
            'rows': [{'rank': 1, 'market': 'BTC/INR', 'targetAtMs': 10_000_000,
                      'forecastHorizonSeconds': 900, 'signalId': 'limit-1'}],
        }
        catalogue = build_daily_contracts(source, now_ms=3_600_000,
                                           prediction_quantum_reference_usd=1.0)
        self.assertTrue(all(item['contractPriceCents'] > 1_000_000
                            for item in catalogue['contracts']))
        self.assertTrue(all(item['status'] == 'held' for item in catalogue['contracts']))
        self.assertTrue(all('10,000' in item['blockedReasons'][0]
                            for item in catalogue['contracts']))

    def test_authenticated_round_trip_and_wrong_key(self):
        payload = build_status(self.instance, now_ms=3_600_000)
        key = Fernet.generate_key()
        artifact = encrypt_status(payload, key)
        self.assertEqual(decrypt_status(artifact, key), payload)
        with self.assertRaises(StatusArtifactError):
            decrypt_status(artifact, Fernet.generate_key())


if __name__ == '__main__':
    unittest.main()
