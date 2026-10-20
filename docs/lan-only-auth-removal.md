# LAN-only Betrieb ohne Authentifizierung

**Status:** bewusste Risikoakzeptanz, 2026-10-20.

## Entscheidung

Die HTTP-Basic-Authentifizierung wurde für das Coop-Dashboard vollständig entfernt:

- keine `basicAuth`-Middleware mehr in `infrastructure/ingress/middleware.yaml`
- keine `middlewares`-Referenzen mehr auf den fachlichen Routen in `infrastructure/ingress/ingressroute.yaml`
- API: `AUTH_MODE=disabled` mit `ALLOW_UNAUTHENTICATED=true` in `infrastructure/apps/api.yaml`

Der Dienst ist damit für jedes Gerät im LAN `192.168.2.94` ohne Login erreichbar. Es gibt keinen
Internet-Ingress, keinen Portforward und keine öffentliche DNS-Auflösung für diesen Host.

## Was weiterhin gilt

- **TLS bleibt aktiv.** HTTPS mit dem lokalen CA-Zertifikat, `TLSStore/default` unverändert.
- **HTTP wird auf HTTPS umgeleitet.** Die `redirect-https`-Middleware bleibt bestehen.
- **Netzwerkgrenze bleibt die eigentliche Kontrolle.** Default-deny NetworkPolicies,
  ClusterIP-Services und die Host-Firewall sind unverändert. Wer keinen Zugang zum LAN
  `192.168.2.94` hat, erreicht den Dienst nicht.
- **CORS bleibt auf `https://192.168.2.94` beschränkt**, kein Wildcard.

## Fail-closed bleibt erhalten

`AUTH_MODE=disabled` außerhalb von `development`/`test` startet nur, wenn
`ALLOW_UNAUTHENTICATED=true` explizit gesetzt ist. Ohne dieses Opt-in verweigert die API
den Start. Damit kann ein Deployment nicht versehentlich unauthentifiziert hochkommen.

`AUTH_MODE=basic` bleibt vollständig implementiert und getestet. Ein Rückbau ist ein
einzelner Commit: Middleware wieder einbauen, Routen referenzieren, `AUTH_MODE=basic`
plus `AUTH_USERNAME`/`AUTH_PASSWORD` setzen.

## Bewusst aufbewahrt

Das SOPS-verschlüsselte Secret `coop-basic-auth` und die Schlüssel `AUTH_USERNAME` /
`AUTH_PASSWORD` in `coop-runtime` bleiben bestehen. Sie werden aktuell nicht mehr
referenziert und dienen ausschließlich dem schnellen Rollback.

## Risiken, die daraus folgen

| Risiko                                     | Wirkung                                                                      |
| ------------------------------------------ | ---------------------------------------------------------------------------- |
| Jedes LAN-Gerät hat vollen Schreibzugriff  | Statusänderungen, Notizen und Importe sind ohne Login möglich                |
| Keine Audit-Zuordnung zu einer Person      | `actor_type`/`source` bleiben technisch gesetzt, aber nicht personengebunden |
| Gäste-WLAN oder kompromittiertes LAN-Gerät | erhält denselben Zugriff wie der Betreiber                                   |

Diese Risiken sind für ein privates Einzelnutzer-LAN akzeptiert. Sobald der Dienst von
außen erreichbar werden soll, ist Authentifizierung vorher wieder zu aktivieren.
