# Öffentliche M1-Demo

- Demo: <https://t-flow-sash.github.io/coop-video-planning-dashboard-m1-demo/>
- Öffentliches Artefaktrepo: <https://github.com/t-flow-sash/coop-video-planning-dashboard-m1-demo>
- Source of Truth für sämtlichen Quellcode ist ausschließlich dieses private Hauptrepo. Das öffentliche Repo enthält nur den erzeugten statischen Export.

Die Demo läuft isoliert im Demo-Modus mit Beispieldaten und Browser-`localStorage`. Sie verwendet keine API, keine Gmail-Verbindung und keine echten Kunden- oder Produktionsdaten.

## Statischen Export reproduzieren und prüfen

Aus dem Root des privaten Hauptrepos:

```bash
corepack pnpm install --frozen-lockfile
NEXT_PUBLIC_DEMO_MODE=true \
PAGES_REPOSITORY_NAME=coop-video-planning-dashboard-m1-demo \
  corepack pnpm --filter web build
touch apps/web/out/.nojekyll
```

Für den Browser-Nachweis wird der Export unter demselben Basispfad wie bei GitHub Pages lokal bereitgestellt:

```bash
rm -rf /tmp/coop-demo-preview
mkdir -p /tmp/coop-demo-preview/coop-video-planning-dashboard-m1-demo
cp -a apps/web/out/. /tmp/coop-demo-preview/coop-video-planning-dashboard-m1-demo/
python3 -m http.server 4173 --directory /tmp/coop-demo-preview
```

In einem zweiten Terminal:

```bash
DEMO_BASE_URL=http://127.0.0.1:4173/coop-video-planning-dashboard-m1-demo/ \
  node scripts/public-demo-evidence.mjs
```

Der Nachweis schlägt bei Browserfehlern, fehlgeschlagenen Requests oder Requests an andere Origins fehl.

## Manueller Fallback-Deploy

GitHub Pages wird wegen der Plan-Einschränkung des privaten Repos nicht von dort deployt. Operativ wird nur der geprüfte Inhalt von `apps/web/out` in das öffentliche Artefaktrepo übernommen. Die Authentifizierung erfolgt interaktiv über die vorhandene Git-/GitHub-CLI-Konfiguration (zum Beispiel einmalig mit `gh auth login`); Tokens gehören weder in Befehle noch in Dateien.

```bash
SOURCE_SHA=$(git rev-parse --short HEAD)
DEPLOY_DIR=$(mktemp -d)
gh repo clone t-flow-sash/coop-video-planning-dashboard-m1-demo "$DEPLOY_DIR/repo"
git -C "$DEPLOY_DIR/repo" rm -r --ignore-unmatch .
cp -a apps/web/out/. "$DEPLOY_DIR/repo/"
touch "$DEPLOY_DIR/repo/.nojekyll"
git -C "$DEPLOY_DIR/repo" add -A
git -C "$DEPLOY_DIR/repo" diff --cached --check
git -C "$DEPLOY_DIR/repo" commit -m "deploy: M1 demo from $SOURCE_SHA"
git -C "$DEPLOY_DIR/repo" push origin main
```

Nach dem Push den Pages-Lauf im öffentlichen Repo abwarten und anschließend Demo-URL sowie `scripts/public-demo-evidence.mjs` gegen die öffentliche URL prüfen. Falls der Export unverändert ist, beendet `git commit` erwartungsgemäß ohne neuen Commit.
