#!/usr/bin/env python3
"""Exact rendered, source and Flux contract for the non-deployable semantic writer."""
from __future__ import annotations

import sys
from pathlib import Path

import yaml

SENTINEL = "127.0.0.1:5000/coop-api:git-000000000000@sha256:" + "0" * 64


def docs(path: str) -> list[dict]:
    return [doc for doc in yaml.safe_load_all(Path(path).read_text()) if doc]


def validate(render: list[dict], flux: list[dict], source: dict) -> None:
    assert {(item['kind'], item['metadata']['name']) for item in render} == {
        ('CronJob', 'gmail-semantic-worker'),
        ('NetworkPolicy', 'gmail-semantic-worker-egress'),
    }, 'semantic resource inventory changed'
    owners = [item for item in flux if item.get('metadata', {}).get('name') == 'coop-semantic']
    assert len(owners) == 1 and owners[0]['spec']['suspend'] is True
    assert owners[0]['spec']['path'] == './infrastructure/semantic'
    assert set(source['resources']) == {'semantic-cronjob.yaml', 'network-policy.yaml'}
    job = next(item for item in render if item['kind'] == 'CronJob')
    assert job['spec']['suspend'] is True
    container = job['spec']['jobTemplate']['spec']['template']['spec']['containers']
    assert len(container) == 1 and container[0]['image'] == SENTINEL
    assert container[0]['command'] == ['python', '-m', 'app.ingestion.gmail_semantic_queue']
    assert container[0]['args'] == ['--max-batch', '25']
    env = {entry['name']: entry for entry in container[0]['env']}
    assert env['SEMANTIC_WORKER_ARMED'] == {'name': 'SEMANTIC_WORKER_ARMED', 'value': 'false'}
    assert env['DATABASE_URL']['valueFrom']['secretKeyRef'] == {
        'name': 'gmail-semantic-db', 'key': 'SEMANTIC_DATABASE_URL'
    }
    assert {name for name, entry in env.items() if entry.get('valueFrom')} == {
        'DATABASE_URL', 'GMAIL_CLIENT_ID', 'GMAIL_CLIENT_SECRET',
        'GMAIL_REFRESH_TOKEN', 'GMAIL_ACCOUNT',
    }
    assert all(entry['valueFrom']['secretKeyRef']['name'] == 'gmail-oauth'
               for name, entry in env.items() if name.startswith('GMAIL_'))
    policy = next(item for item in render if item['kind'] == 'NetworkPolicy')
    assert policy['spec']['podSelector']['matchLabels'] == {'coop.example.com/semantic-worker': 'true'}


def main() -> int:
    if len(sys.argv) != 4:
        raise SystemExit('usage: validate-semantic-dormant.py RENDER FLUX SOURCE')
    rendered, flux, source = map(docs, sys.argv[1:])
    assert len(source) == 1
    validate(rendered, flux, source[0])
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
