from __future__ import annotations

import unittest
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parents[2]


def documents(path: str) -> list[dict[str, Any]]:
    with (ROOT / path).open(encoding="utf-8") as handle:
        return [doc for doc in yaml.safe_load_all(handle) if doc]


class GmailEgressProxyContractTests(unittest.TestCase):
    def test_envoy_allows_only_exact_google_connect_authorities(self) -> None:
        resources = documents("infrastructure/import/gmail-egress-proxy.yaml")
        config_map = next(item for item in resources if item["kind"] == "ConfigMap")
        envoy = yaml.safe_load(config_map["data"]["envoy.yaml"])
        listener = envoy["static_resources"]["listeners"][0]
        hcm = listener["filter_chains"][0]["filters"][0]["typed_config"]
        routes = hcm["route_config"]["virtual_hosts"][0]["routes"]
        self.assertEqual(routes[0]["match"], {"connect_matcher": {}})
        self.assertEqual(routes[0]["route"]["upgrade_configs"][0]["upgrade_type"], "CONNECT")
        rbac = next(
            item for item in hcm["http_filters"] if item["name"] == "envoy.filters.http.rbac"
        )
        permissions = rbac["typed_config"]["rules"]["policies"]["google-api-connect"][
            "permissions"
        ]
        authorities = {
            permission["header"]["string_match"]["exact"] for permission in permissions
        }
        self.assertEqual(
            authorities,
            {"oauth2.googleapis.com:443", "gmail.googleapis.com:443"},
        )
        self.assertNotIn("access_log", hcm)
        serialized = config_map["data"]["envoy.yaml"]
        self.assertNotIn("safe_regex", serialized)

    def test_proxy_is_immutable_rootless_readonly_and_internal_only(self) -> None:
        resources = documents("infrastructure/import/gmail-egress-proxy.yaml")
        deployment = next(item for item in resources if item["kind"] == "Deployment")
        service = next(item for item in resources if item["kind"] == "Service")
        pod = deployment["spec"]["template"]["spec"]
        container = pod["containers"][0]
        self.assertEqual(
            container["image"],
            "docker.io/envoyproxy/envoy:distroless-v1.35.3@"
            "sha256:f9080877fdbaecc788e79e200d912382e57f780b63752160e27dfe34f5d0aa36",
        )
        self.assertEqual(pod["securityContext"]["runAsUser"], 65532)
        self.assertTrue(pod["securityContext"]["runAsNonRoot"])
        self.assertEqual(pod["securityContext"]["seccompProfile"], {"type": "RuntimeDefault"})
        self.assertFalse(pod["automountServiceAccountToken"])
        self.assertTrue(container["securityContext"]["readOnlyRootFilesystem"])
        self.assertFalse(container["securityContext"]["allowPrivilegeEscalation"])
        self.assertEqual(container["securityContext"]["capabilities"], {"drop": ["ALL"]})
        self.assertIn("requests", container["resources"])
        self.assertIn("limits", container["resources"])
        self.assertEqual(service["spec"]["type"], "ClusterIP")
        self.assertEqual(service["spec"]["ports"][0]["port"], 10000)

    def test_network_policies_force_importer_through_proxy(self) -> None:
        policies = {item["metadata"]["name"]: item for item in documents("infrastructure/import/network-policy.yaml")}
        importer = policies["gmail-import-egress"]
        proxy = policies["gmail-egress-proxy"]
        importer_serialized = yaml.safe_dump(importer)
        self.assertNotIn("ipBlock", importer_serialized)
        self.assertNotIn("192.0.2.", importer_serialized)
        proxy_rule = next(
            rule
            for rule in importer["spec"]["egress"]
            if any(port["port"] == 10000 for port in rule.get("ports", []))
        )
        self.assertEqual(
            proxy_rule["to"],
            [{"podSelector": {"matchLabels": {"app.kubernetes.io/name": "gmail-egress-proxy"}}}],
        )
        self.assertIn("Ingress", proxy["spec"]["policyTypes"])
        self.assertIn("Egress", proxy["spec"]["policyTypes"])
        ingress = proxy["spec"]["ingress"][0]
        self.assertEqual(
            ingress["from"],
            [{"podSelector": {"matchLabels": {"coop.example.com/gmail-import": "true"}}}],
        )
        public_443 = next(
            rule
            for rule in proxy["spec"]["egress"]
            if any(port["port"] == 443 for port in rule.get("ports", []))
        )
        self.assertEqual(public_443["to"][0]["ipBlock"]["cidr"], "0.0.0.0/0")
        self.assertIn("169.254.0.0/16", public_443["to"][0]["ipBlock"]["except"])

    def test_jobs_use_http_connect_proxy_query_config_and_stay_suspended(self) -> None:
        cronjobs = documents("infrastructure/import/gmail-import-cronjobs.yaml") + documents(
            "infrastructure/import/gmail-recurring-cronjobs.yaml"
        )
        self.assertEqual(len(cronjobs), 3)
        for cron in cronjobs:
            self.assertTrue(cron["spec"]["suspend"])
            container = cron["spec"]["jobTemplate"]["spec"]["template"]["spec"][
                "containers"
            ][0]
            env = {entry["name"]: entry for entry in container["env"]}
            self.assertEqual(env["GMAIL_IMPORT_ARMED"]["value"], "false")
            self.assertEqual(env["HTTPS_PROXY"]["value"], "http://gmail-egress-proxy:10000")
            self.assertEqual(env["HTTP_PROXY"]["value"], "http://gmail-egress-proxy:10000")
            self.assertIn("gmail-egress-proxy", env["NO_PROXY"]["value"])
            self.assertIn("GMAIL_IMPORT_QUERY", env)
            self.assertNotIn("192.0.2.", yaml.safe_dump(cron))


if __name__ == "__main__":
    unittest.main()
