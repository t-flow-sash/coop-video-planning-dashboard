"""Release contract: real rendered worker command remains non-runnable without arming."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import unittest
from pathlib import Path

import yaml

from scripts.validate_semantic_dormant import validate

ROOT = Path(__file__).resolve().parents[2]


class SemanticReleaseContracts(unittest.TestCase):
    def test_real_render_and_cli_refuse_unarmed_with_invalid_db(self) -> None:
        rendered = subprocess.run(
            ['kubectl', 'kustomize', str(ROOT / 'infrastructure/semantic')],
            check=True, capture_output=True, text=True,
        )
        docs = lambda stream: [item for item in yaml.safe_load_all(stream) if item]
        resources = docs(rendered.stdout)
        flux = docs((ROOT / 'clusters/production/flux-kustomizations.yaml').read_text())
        source = docs((ROOT / 'infrastructure/semantic/kustomization.yaml').read_text())
        validate(resources, flux, source[0])
        container = next(item for item in resources if item['kind'] == 'CronJob')\
            ['spec']['jobTemplate']['spec']['template']['spec']['containers'][0]
        interpreter = ROOT / 'apps/api/.venv/bin/python'
        self.assertTrue(interpreter.exists(), f'missing project interpreter: {interpreter}')
        # The URL must be syntactically valid: an unparseable URL makes ``app.db``
        # fail at import time, which is *not* evidence that the arming gate works.
        # Port 1 is never listening, so any attempted database or provider access
        # would surface as a connection error instead of the sanitized outcome.
        environment = os.environ.copy()
        environment.update(DATABASE_URL='postgresql+psycopg://unarmed_probe:probe@127.0.0.1:1/coop_unarmed',
                           GMAIL_CLIENT_ID='not-a-secret',
                           SEMANTIC_WORKER_ARMED='false')
        command = [str(interpreter), '-m', container['command'][2], *container['args']]
        invoked = subprocess.run(command, cwd=ROOT / 'apps/api', env=environment,
                                 capture_output=True, text=True, timeout=20)
        self.assertEqual(invoked.returncode, 2)
        self.assertEqual(invoked.stdout, '')
        self.assertEqual(json.loads(invoked.stderr), {'outcome': 'NOT_ARMED'})
        self.assertNotIn('Traceback', invoked.stderr)
        self.assertNotIn('gmail-semantic-db.secret', source[0]['resources'])

    def test_manifest_sentinel_and_flag_cannot_be_silently_promoted(self) -> None:
        source = ROOT / 'infrastructure/semantic/semantic-cronjob.yaml'
        cronjob = yaml.safe_load(source.read_text())
        flux = [d for d in yaml.safe_load_all((ROOT / 'clusters/production/flux-kustomizations.yaml').read_text()) if d]
        definition = yaml.safe_load((ROOT / 'infrastructure/semantic/kustomization.yaml').read_text())
        policy = yaml.safe_load((ROOT / 'infrastructure/semantic/network-policy.yaml').read_text())
        resources = [cronjob, policy]
        for broken in ('armed', 'unsuspended', 'promoted-image', 'secret-reuse'):
            candidate = json.loads(json.dumps(resources))
            spec = candidate[0]['spec']
            container = spec['jobTemplate']['spec']['template']['spec']['containers'][0]
            if broken == 'armed':
                container['env'][0]['value'] = 'true'
            elif broken == 'unsuspended':
                spec['suspend'] = False
            elif broken == 'promoted-image':
                container['image'] = '127.0.0.1:5000/coop-api:git-abc@sha256:' + 'a' * 64
            else:
                container['env'][4]['valueFrom']['secretKeyRef']['name'] = 'coop-runtime'
            with self.subTest(broken=broken), self.assertRaises(AssertionError):
                validate(candidate, flux, definition)


if __name__ == '__main__':
    unittest.main()
