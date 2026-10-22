#!/usr/bin/env python3
"""Exact rendered, source and Flux contract for the activated semantic writer."""
from __future__ import annotations

import re
import sys
from pathlib import Path

import yaml

SENTINEL = "127.0.0.1:5000/coop-api:git-000000000000@sha256:" + "0" * 64
PROMOTED = re.compile(
    r"127\.0\.0\.1:5000/coop-api:git-(?P<revision>[0-9a-f]{12})@sha256:[0-9a-f]{64}"
)


def docs(path: str) -> list[dict]:
    return [doc for doc in yaml.safe_load_all(Path(path).read_text()) if doc]


def validate(render: list[dict], flux: list[dict], source: dict) -> None:
    assert {(item['kind'], item['metadata']['name']) for item in render} == {
        ('CronJob', 'gmail-semantic-worker'),
        ('NetworkPolicy', 'gmail-semantic-worker-egress'),
    }, 'semantic resource inventory changed'
    owners = [item for item in flux if item.get('metadata', {}).get('name') == 'coop-semantic']
    assert len(owners) == 1, 'semantic Flux owner missing or duplicated'
    assert owners[0]['spec']['suspend'] is False, 'semantic owner must be unsuspended'
    assert owners[0]['spec']['path'] == './infrastructure/semantic'
    assert set(source['resources']) == {'semantic-cronjob.yaml', 'network-policy.yaml'}
    job = next(item for item in render if item['kind'] == 'CronJob')
    assert job['spec']['suspend'] is False, 'semantic scheduler must be unsuspended'
    container = job['spec']['jobTemplate']['spec']['template']['spec']['containers']
    assert len(container) == 1
    image = container[0]['image']
    assert image != SENTINEL, 'the dormant sentinel can never ship as activated state'
    assert PROMOTED.fullmatch(image), image
    assert container[0]['command'] == ['python', '-m', 'app.ingestion.gmail_semantic_queue']
    assert container[0]['args'] == ['--max-batch', '25']
    env = {entry['name']: entry for entry in container[0]['env']}
    assert env['SEMANTIC_WORKER_ARMED'] == {'name': 'SEMANTIC_WORKER_ARMED', 'value': 'true'}
    assert env['DATABASE_URL']['valueFrom']['secretKeyRef'] == {
        'name': 'gmail-semantic-db', 'key': 'SEMANTIC_DATABASE_URL'
    }
    assert {name for name, entry in env.items() if entry.get('valueFrom')} == {
        'DATABASE_URL', 'GMAIL_CLIENT_ID', 'GMAIL_CLIENT_SECRET',
        'GMAIL_REFRESH_TOKEN', 'GMAIL_ACCOUNT',
    }
    assert all(
        entry['valueFrom']['secretKeyRef']['name'] == 'gmail-oauth'
        for name, entry in env.items()
        if name.startswith('GMAIL_') and entry.get('valueFrom')
    )
    # The provider contract requires the pinned account fingerprint; without it the
    # runner fails closed with a retryable configuration error at every run.
    module_source = Path('apps/api/app/ingestion/gmail.py').read_text()
    fingerprint = re.search(r'EXPECTED_ACCOUNT_SHA256 = "([0-9a-f]{64})"', module_source)
    assert fingerprint is not None, 'expected account fingerprint constant missing'
    assert env.get('GMAIL_ACCOUNT_SHA256', {}).get('value') == fingerprint.group(1), (
        'semantic worker must pin the same Gmail account fingerprint as the collector'
    )
    policy = next(item for item in render if item['kind'] == 'NetworkPolicy')
    assert policy['spec']['podSelector']['matchLabels'] == {
        'coop.example.com/semantic-worker': 'true'
    }
    # The credential itself travels with the release migration Job, which sets the
    # writer password and enables LOGIN with the enforced ACL before the first run.
    migration = docs('infrastructure/migration/migration-job.yaml')
    migrate = next(item for item in migration if item['kind'] == 'Job')
    steps = migrate['spec']['template']['spec']['containers'][0]
    assert 'app.semantic_database_access provision' in steps['args'][0]
    assert 'app.semantic_database_access reconcile' in steps['args'][0]
    migrate_env = {entry['name']: entry for entry in steps['env']}
    assert migrate_env['SEMANTIC_DATABASE_URL']['valueFrom']['secretKeyRef'] == {
        'name': 'gmail-semantic-db', 'key': 'SEMANTIC_DATABASE_URL'
    }
    migration_source = docs('infrastructure/migration/kustomization.yaml')[0]
    assert 'gmail-semantic-db.secret.sops.yaml' in migration_source['resources']


def main() -> int:
    if len(sys.argv) != 4:
        raise SystemExit('usage: validate-semantic-activation.py RENDER FLUX SOURCE')
    rendered, flux, source = map(docs, sys.argv[1:])
    assert len(source) == 1
    validate(rendered, flux, source[0])
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
