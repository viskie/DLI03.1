import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from cryptography.fernet import Fernet

from status_artifact import StatusArtifactError, decrypt_status, encrypt_status
from status_generator import (
    build_status, _all_actualized_simulation, _matured_contract_simulations,
)
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

    def test_mature_contract_simulation_uses_only_authenticated_realizations(self):
        archive = Path(self.temporary.name) / 'archive'
        archive.mkdir()
        key = Fernet.generate_key()
        batch = [
            {'market':'BTC/USDT','originAtMs':1000,'targetAtMs':2000,
             'spotPrice':100.0,'predictedPrice':110.0,'direction':'up'},
            {'market':'ETH/USDT','originAtMs':1000,'targetAtMs':3000,
             'spotPrice':100.0,'predictedPrice':90.0,'direction':'down'},
            {'market':'MISSING/USDT','originAtMs':1000,'targetAtMs':4000,
             'spotPrice':100.0,'predictedPrice':110.0,'direction':'up'},
        ]
        payload = {
            'schemaVersion': 1,
            'dailyContracts': {
                'issueDate':'2026-11-01',
                'contracts':[{'contractId':'O961-MATURE','name':'Mature test','accessDays':1,
                              'accessUntilMs':5000,'issuedAtMs':100,'perTradeCapitalUsd':100.0,
                              'batchSha256':'batch','modelSha256':'model','contractPriceCents':30000,
                              'requiredExchangeCapitalCents':30000}],
                'predictionBatches':{'O961-MATURE':batch},
            },
            'predictionLogs': {'actualized': {'rows':[
                {'market':'BTC/USDT','originAtMs':1000,'targetAtMs':2000,
                 'basePrice':100.0,'predictedPrice':110.0,'actualPrice':120.0,
                 'resolutionSource':'live'},
                {'market':'ETH/USDT','originAtMs':1000,'targetAtMs':3000,
                 'basePrice':100.0,'predictedPrice':90.0,'actualPrice':80.0,
                 'resolutionSource':'repaired'},
            ]}},
        }
        (archive/'2026-11-02T00-00-00Z.stat.i').write_bytes(encrypt_status(payload,key))
        result = _matured_contract_simulations(archive,key,now_ms=10_000)
        report = result['contracts'][0]
        self.assertEqual(report['simulationStatus'],'partial')
        self.assertEqual(report['realizedCount'],2)
        self.assertEqual(report['unresolvedCount'],1)
        self.assertEqual(report['winningTrades'],2)
        self.assertEqual(report['correctPredictions'],2)
        self.assertEqual(report['wrongPredictions'],0)
        self.assertAlmostEqual(report['grossProfitLossUsd'],40.0)
        self.assertAlmostEqual(report['grossReturnPercent'],20.0)
        self.assertAlmostEqual(report['netProfitLossUsd'],40.0)
        self.assertEqual(report['costBasis'],'zero-configured-costs')

    def test_verified_contract_simulation_subtracts_costs_and_limits_to_nine(self):
        archive = Path(self.temporary.name) / 'verified-archive'
        archive.mkdir()
        key = Fernet.generate_key()
        for index in range(11):
            origin = 10_000 + index
            target = 20_000 + index
            contract_id = f'O961-{index}'
            payload = {
                'schemaVersion': 1,
                'dailyContracts': {
                    'issueDate': f'2026-10-{index + 1:02d}',
                    'contracts': [{
                        'contractId': contract_id, 'accessDays': 1,
                        'issuedAtMs': 1_000 + index, 'accessUntilMs': 99_999,
                        'perTradeCapitalUsd': 100.0, 'assetNotionalUsd': 98.0,
                        'entryFeeBps': 10, 'exitFeeBps': 10,
                        'costBufferUsd': 1.0, 'costsConfigured': True,
                    }],
                    'predictionBatches': {contract_id: [{
                        'market': 'BTC/USDT', 'originAtMs': origin,
                        'targetAtMs': target, 'spotPrice': 100.0,
                        'predictedPrice': 110.0, 'direction': 'up',
                    }]},
                },
                'predictionLogs': {'actualized': {'rows': [{
                    'market': 'BTC/USDT', 'originAtMs': origin,
                    'targetAtMs': target, 'actualPrice': 110.0,
                    'directionCorrect': True, 'resolutionSource': 'live',
                }]}},
            }
            (archive / f'{index:02d}.stat.i').write_bytes(encrypt_status(payload, key))
        result = _matured_contract_simulations(archive, key, now_ms=30_000)
        self.assertEqual(result['verifiedContractCount'], 9)
        self.assertEqual(result['availableVerifiedContractCount'], 11)
        self.assertEqual(result['contracts'][0]['contractId'], 'O961-10')
        report = result['contracts'][0]
        self.assertAlmostEqual(report['grossProfitLossUsd'], 9.8)
        self.assertAlmostEqual(report['executionCostsUsd'], 1.2058)
        self.assertAlmostEqual(report['netProfitLossUsd'], 8.5942)
        self.assertEqual(report['correctPredictions'], 1)
        self.assertEqual(report['wrongPredictions'], 0)

    def test_all_actualized_simulation_values_the_complete_aggregate(self):
        result = _all_actualized_simulation(
            {'totalActualized': 4, 'performance': {
                'validCount': 4, 'correct': 3, 'wrong': 1,
                'directionalReturnSum': 0.2,
                'exitNotionalFactorSum': 4.2,
            }},
            {'usdInr': {'rate': 80}, 'tradeQuantum': {
                'investmentInr': 800, 'assetNotionalInr': 784,
                'entryFeeBps': 10, 'exitFeeBps': 10,
                'costBufferInr': 8,
            }},
        )
        self.assertTrue(result['available'])
        self.assertEqual(result['correctPredictions'], 3)
        self.assertEqual(result['wrongPredictions'], 1)
        self.assertAlmostEqual(result['grossProfitLossUsd'], 1.96)
        self.assertAlmostEqual(result['executionCostsUsd'], 0.48036)
        self.assertAlmostEqual(result['netProfitLossUsd'], 1.47964)

    def test_authenticated_round_trip_and_wrong_key(self):
        payload = build_status(self.instance, now_ms=3_600_000)
        key = Fernet.generate_key()
        artifact = encrypt_status(payload, key)
        self.assertEqual(decrypt_status(artifact, key), payload)
        with self.assertRaises(StatusArtifactError):
            decrypt_status(artifact, Fernet.generate_key())


if __name__ == '__main__':
    unittest.main()
