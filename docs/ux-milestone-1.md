# UX-Spezifikation — Coop- & Video-Planungsdashboard, Meilenstein 1

**Status:** Umsetzungsbereit zur expliziten Freigabe
**Zielgruppe:** Product Owner, UX/UI, DEV-Agent, QA
**Geltungsbereich:** Meilenstein 1; keine echte Gmail-Anbindung, keine produktive Implementierung in diesem Dokument
**Surface-Entscheidung:** **Operate primär, Monitor sekundär.** Die Oberfläche ist zuerst eine Arbeitsliste zum Finden, Öffnen und Bearbeiten von Kooperationen. Monitoring-Elemente helfen beim Priorisieren und Erkennen von Datenlücken, dominieren aber nicht die Seite.

---

## 1. Zielbild und Scope

Das Dashboard bündelt alle offenen bzw. nicht abgeschlossenen Kooperationen („Coops“) in einer belastbaren Arbeitsoberfläche. Nutzer:innen müssen:

1. den aktuellen Stand jeder Coop schnell erfassen,
2. nach Status, Release-Planung, Kontakt, Agentur und Datenqualität filtern,
3. eine Coop ohne Kontextverlust öffnen und bearbeiten,
4. erkennen, wenn derselbe Kontakt oder dieselbe Agentur in mehreren offenen Coops vorkommt,
5. Herkunft und Synchronisationszustand der Daten nachvollziehen können,
6. Release-Daten und Honorare nur dann sehen, wenn sie tatsächlich vereinbart bzw. erfasst wurden.

### 1.1 In Meilenstein 1 enthalten

- responsive Arbeitsliste aller offenen Coops,
- lokale bzw. gemockte Datengrundlage,
- kanonisches Statusmodell inklusive abgeschlossener Zustände,
- Filter, Suche, Sortierung und gespeicherter URL-Zustand,
- Detailansicht und Bearbeitungsflow,
- Anzeige von Import-Herkunft und Sync-Zustand als vorbereitete UX,
- explizite Hervorhebung wiederkehrender Kontakte und Agenturen,
- Loading-, Empty-, No-results-, Error- und Konfliktzustände,
- Desktop- und Mobile-Spezifikation,
- barrierefreie Interaktions- und Darstellungsregeln.

### 1.2 Nicht in Meilenstein 1 enthalten

- echte Gmail-Authentifizierung, Inbox-Synchronisierung oder Thread-Parsing,
- automatische Statusänderungen durch den Influencer-Agenten,
- Nutzer-, Rollen- oder Rechteverwaltung,
- Umsatz-, Forecast-, Performance- oder andere Business-KPIs,
- Rechnungsstellung, Zahlungsabgleich oder Buchhaltungsintegration,
- komplexe Video-Produktionsworkflows, Asset-Upload oder Freigabeportal,
- Drag-and-drop-Kanban als zweites, konkurrierendes Bedienmodell,
- automatische Dubletten-Zusammenführung.

Die UX bereitet spätere Integrationen vor, simuliert aber keine bereits existierende Automation.

---

## 2. Leitprinzipien

1. **Arbeit vor Überblick:** Der erste Screen beantwortet „Welche Coop muss ich öffnen oder bearbeiten?“ und nicht „Wie sieht das Business aus?“.
2. **Ein Datensatz, eine Wahrheit:** Status, Termin, Honorar und Eckdaten werden pro Coop dargestellt; unbekannte Werte bleiben sichtbar unbekannt und werden nicht geschätzt.
3. **Zusammenhänge statt Dekoration:** Wiederkehrende Kontakte und Agenturen sind klickbare Beziehungen, keine rein farbigen Labels.
4. **Provenienz sichtbar:** Herkunft und Sync-Status stehen nahe an den betroffenen Daten, ohne die Hauptinformation zu überladen.
5. **Progressive Offenlegung:** Die Liste ist scanbar; vollständige Felder und Metadaten liegen im Detailbereich.
6. **Keine Data-Slop:** Keine generischen KPI-Karten, keine Fantasiezahlen, keine dekorativen Diagramme.
7. **Tastatur und Touch gleichwertig:** Jede Kernaktion ist fokussierbar; mobile Ziele sind mindestens 44 × 44 px groß.

---

## 3. Informationsarchitektur

### 3.1 Hauptnavigation

Meilenstein 1 hat genau zwei primäre Ziele:

- **Kooperationen** — Standardroute `/coops`; operative Liste und Arbeitsbereich.
- **Planung** — Route `/planung`; sekundäre Monitor-Ansicht für terminierte Releases.

Weitere Ziele werden nicht vorgetäuscht. Einstellungen, Reports oder Agent-Ansichten sind in M1 nicht Bestandteil der Hauptnavigation.

### 3.2 Seitenstruktur „Kooperationen“

1. App-Kopfzeile
2. Seitentitel und primäre Aktion „Coop anlegen“
3. kompakte Beobachtungsleiste mit handlungsorientierten Ansichten
4. Such-, Filter- und Sortierleiste
5. aktive Filter als entfernbare Chips
6. Ergebnis-Metazeile (Trefferzahl, Sortierung, „Filter zurücksetzen“)
7. Coop-Arbeitsliste
8. Detail-/Bearbeitungspanel bei Auswahl
9. Toast-Region für Speichern/Fehler

### 3.3 Seitenstruktur „Planung“

Die Planungsansicht ist eine chronologische Liste, kein KPI-Dashboard:

- überfällige, aber noch offene Release-Termine,
- Releases der nächsten 14 Tage,
- später terminierte offene Coops,
- „Termin noch offen“ für Coops ohne vereinbartes Release-Datum.

Es werden nur tatsächlich vorhandene Coop-Datensätze aggregiert. Keine Hochrechnung und keine Umsatzsumme.

### 3.4 URL- und Navigationszustand

Folgende Zustände müssen als Query-Parameter abbildbar und nach Reload rekonstruierbar sein:

- `q` — Suche,
- `status` — kommaseparierte Status-IDs,
- `release` — `overdue|14d|later|missing`,
- `contact` — Kontakt-ID,
- `agency` — Agentur-ID,
- `quality` — `missing-release|missing-fee|sync-issue`,
- `sort` — Sortierschlüssel,
- `coop` — ausgewählte Coop-ID.

Beispiel: `/coops?agency=ag_42&status=negotiation,agreed&coop=coop_17`.

Browser-Zurück schließt zuerst den geöffneten Datensatz bzw. stellt den vorherigen Filterzustand wieder her.

---

## 4. Daten- und Statusmodell

### 4.1 Coop-Kernobjekt

Der DEV-Agent soll mindestens folgende Felder typisieren. Werte dürfen `null` sein, wo „noch nicht bekannt/vereinbart“ fachlich korrekt ist.

| Feld | Typ | Pflicht | Darstellung/Regel |
|---|---|---:|---|
| `id` | string | ja | stabil, nicht editierbar |
| `title` | string | ja | Arbeitstitel der Coop |
| `brandName` | string | ja | Marke/Auftraggeber |
| `status` | enum | ja | siehe 4.2 |
| `contact` | object/null | nein | `id`, `name`, `email` |
| `agency` | object/null | nein | `id`, `name` |
| `releaseDate` | ISO date/null | nein | nur vereinbarter Termin; kein Schätzwert |
| `fee` | object/null | nein | `amount`, `currency`, `agreementState` |
| `deliverables` | string[] | nein | freie, kurze Eckdaten, z. B. Formatnamen nur wenn erfasst |
| `brief` | string/null | nein | weitere Eckdaten/Notizen |
| `source` | enum | ja | in M1 `manual` oder `gmail_preview` |
| `sourceReference` | string/null | nein | z. B. Thread-ID/-Betreff; in M1 nicht zwingend verlinkt |
| `syncState` | enum | ja | siehe 4.4 |
| `updatedAt` | ISO datetime | ja | absolut im Detail, relativ in der Liste |
| `updatedBy` | object | ja | `human` oder vorbereitet `agent`, plus Anzeigename |
| `version` | integer | ja | Basis für optimistische Konflikterkennung |

### 4.2 Kanonische Status

Die Reihenfolge ist fachlich und für Statusauswahl/Sortierung verbindlich:

| ID | Deutsches Label | Gruppe | Offen? | Bedeutung |
|---|---|---|---:|---|
| `inbox` | Eingang | Anbahnung | ja | erfasst, noch nicht qualifiziert |
| `clarification` | In Klärung | Anbahnung | ja | Eckdaten oder Zuständigkeit werden geklärt |
| `negotiation` | Verhandlung | Vereinbarung | ja | Konditionen sind in Abstimmung |
| `agreed` | Vereinbart | Vereinbarung | ja | Zusammenarbeit ist bestätigt |
| `production` | In Produktion | Umsetzung | ja | Content wird erstellt |
| `approval` | In Freigabe | Umsetzung | ja | externe/interne Freigabe läuft |
| `scheduled` | Eingeplant | Veröffentlichung | ja | Veröffentlichung ist terminiert |
| `published` | Veröffentlicht | Abgeschlossen | nein | Veröffentlichung erfolgt |
| `cancelled` | Abgesagt | Abgeschlossen | nein | Coop findet nicht statt |

**Regeln:**

- Standardlisten zeigen alle Datensätze mit `open = true`.
- Abgeschlossene Datensätze sind in M1 nur über Filter „Abgeschlossen einblenden“ erreichbar.
- Status und Release-Datum sind unabhängig: Ein vereinbarter Termin kann bereits ab `agreed` existieren; `scheduled` setzt ein Release-Datum voraus.
- Wechsel zu `scheduled` bei fehlendem Release-Datum öffnet eine Inline-Validierung: „Für ‚Eingeplant‘ ist ein Release-Datum erforderlich.“
- Wechsel zu `published` erfordert in M1 nur Bestätigung; kein erfundenes Veröffentlichungsdatum wird automatisch gesetzt.
- Wechsel zu `cancelled` erfordert Bestätigung, aber keinen Pflichtgrund.
- Das Statusmodell ist zentral zu definieren, damit Dashboard und späterer Influencer-Agent dieselben IDs verwenden.

### 4.3 Honorarzustand

`fee.agreementState`:

- `unknown` — noch nicht bekannt,
- `proposed` — Betrag im Gespräch, noch nicht vereinbart,
- `agreed` — vereinbart.

Darstellung:

- `fee = null` oder `unknown`: „Honorar offen“; niemals `0 €`.
- `proposed`: Betrag + Zusatz „Vorschlag“.
- `agreed`: Betrag + Zusatz „vereinbart“.
- Währung ist Pflicht, sobald ein Betrag vorhanden ist.

### 4.4 Herkunft und Synchronisationsstatus

`source`:

- `manual` — manuell angelegt,
- `gmail_preview` — für M1 gemockte/vorbereitete Gmail-Herkunft.

`syncState`:

- `local` — nur lokal/manuell,
- `synced` — Stand stimmt mit Quelle überein,
- `pending` — Änderung wartet auf Synchronisierung,
- `conflict` — Quelle und Dashboard-Version widersprechen sich,
- `error` — Synchronisierung fehlgeschlagen.

In M1 sind diese Zustände als Test-Fixtures darstellbar. UI-Texte müssen klar sagen, dass keine Live-Gmail-Verbindung aktiv ist, z. B. Tooltip: „Gmail-Herkunft ist in Meilenstein 1 eine Vorschau; es findet keine Live-Synchronisierung statt.“

---

## 5. Desktop-Layout

### 5.1 Breakpoint und Raster

- Desktop: ab `1024px`.
- App-Shell: linke Navigation `224px`, Inhaltsbereich flexibel.
- Inhaltsbereich: maximal `1600px`, linksbündig; horizontaler Seitenabstand `32px` (ab 1440 px: `40px`).
- Kopfzeile im Inhaltsbereich: mindestens `64px` hoch.
- Listenbereich bei geschlossenem Detailpanel: volle verfügbare Breite.
- Bei geöffnetem Detailpanel ab `1280px`: Split View mit Liste `minmax(620px, 1fr)` und Detailpanel `440px`; dazwischen 1-px-Trenner.
- Zwischen `1024px` und `1279px`: Detailpanel als rechte Overlay-Schublade `min(480px, 90vw)`, Hintergrund bleibt sichtbar, aber nicht interaktiv.

### 5.2 Kopf und Beobachtungsleiste

Oben links:

- H1 „Kooperationen“ (`32px`),
- darunter eine sachliche Zeile: „Offene Kooperationen planen und bearbeiten.“

Oben rechts:

- primärer Button „Coop anlegen“.

Darunter liegt eine **Beobachtungsleiste**, keine Kartenreihe. Sie besteht aus horizontalen Textaktionen mit real berechneten Trefferzahlen:

- „Release überfällig · {n}“
- „Nächste 14 Tage · {n}“
- „Termin fehlt · {n}“
- „Sync prüfen · {n}“

Jede Aktion setzt einen dokumentierten Filter. Ist `n = 0`, bleibt die Aktion sichtbar, ist aber als „0“ neutral und nicht alarmierend. Keine große Zahl, kein Icon-Topper, kein Trendpfeil.

### 5.3 Filter- und Listenbereich

Die Toolbar bleibt beim vertikalen Scrollen unterhalb der App-Kopfzeile sticky.

Reihenfolge:

1. Suchfeld, Breite `min(420px, 100%)`, Placeholder „Marke, Coop, Kontakt oder Agentur suchen“.
2. Filterbutton „Filter“ mit Anzahl aktiver Filter.
3. Schnellfilter „Status“.
4. Sortiermenü.
5. optional bei Platz: Toggle „Abgeschlossene einblenden“.

Darunter aktive Filterchips. Jeder Chip enthält lesbares Label und Entfernen-Button; „Alle zurücksetzen“ steht am Ende.

### 5.4 Desktop-Arbeitsliste

Eine semantische Datentabelle ist für Desktop verbindlich. Spalten:

1. **Coop** — Titel (erste Zeile), Marke (zweite Zeile), ggf. Beziehungshinweis.
2. **Status** — Statusbutton/Badge.
3. **Release** — lokales Datum oder „Noch nicht vereinbart“.
4. **Honorar** — formatierter Wert plus Vereinbarungszustand oder „Offen“.
5. **Kontakt / Agentur** — Kontakt und Agentur als klickbare Entity-Chips.
6. **Aktualisiert** — relatives Datum, Tooltip/Accessible Name mit absolutem Zeitstempel.
7. **Sync** — kompaktes Statussymbol mit Text in Tooltip und Screenreader-Label.
8. Row-Aktionsmenü.

Zeilenhöhe: mindestens `76px`, bei mehreren Beziehungshinweisen automatisch wachsend. Der komplette Datensatz ist über Titel-Link und Enter auf fokussierter Zeile öffnbar; interaktive Kind-Controls lösen nicht zusätzlich das Öffnen aus.

**Sticky Table Header** unterhalb der Filterleiste. Horizontales Scrollen ist erst unter `1024px` vorgesehen; auf Desktop werden Spalten responsiv gewichtet. Bei enger Breite darf „Aktualisiert“ zuerst ausgeblendet werden, danach „Sync“ als Teil der Coop-Unterzeile erscheinen. Release und Status werden nie ausgeblendet.

---

## 6. Mobile-Layout

### 6.1 Grundlayout

- Mobile: bis `767px`; Tablet: `768–1023px` nutzt dieselbe Kartenlogik mit mehr Breite.
- Seitenabstand `16px`.
- Topbar `56px`: Menütaste, Titel „Kooperationen“, Aktion „Neu“.
- Navigation als öffnbarer Drawer; aktuelle Route ist textlich markiert.
- Beobachtungsleiste horizontal scrollbar mit Scroll-Snap; Elemente sind kompakte Text-Pills, keine Statistik-Karten.
- Suche über volle Breite.
- Darunter eine Zeile: Filterbutton, Sortierung, Trefferzahl.
- Aktive Filterchips horizontal scrollbar; Ende/Fortsetzung visuell durch beschnittene nächste Chipkante, nicht durch Verlauf.

### 6.2 Mobile Coop-Karte

Jeder Datensatz wird als klare Listenkarte dargestellt, nicht als bunte Kachel:

1. Kopfzeile: Statusbadge links, Syncstatus rechts.
2. Coop-Titel, maximal zwei Zeilen.
3. Marke.
4. Release und Honorar als zweispaltige Definition List; bei sehr schmaler Breite untereinander.
5. Kontakt- und Agenturzeile.
6. Beziehungshinweis, falls wiederkehrend.
7. Fußzeile: „Aktualisiert …“ und 44-px-Aktionsbutton.

Die gesamte nicht-interaktive Kartenfläche öffnet die Detailseite. Verschachtelte Chips und Menübuttons bleiben separat bedienbar.

### 6.3 Mobile Detail und Bearbeitung

- Detail öffnet als eigenständige Route/Full-Screen-Layer, nicht als schmaler Drawer.
- Sticky Topbar: Zurück, gekürzter Titel, Overflow-Menü.
- Lesemodus: Sektionen untereinander ohne Akkordeons für Kernfelder.
- „Bearbeiten“ ist sticky am unteren Rand, respektiert Safe Area.
- Bearbeitungsmodus: ebenfalls Vollbild; unten sticky „Abbrechen“ und „Speichern“.
- Die Tastatur darf den fokussierten Input oder die Aktionsleiste nicht verdecken.

---

## 7. Komponenten-Spezifikation

### 7.1 AppShell

- Props: `activeRoute`, `navItems`, `children`.
- Desktop: persistente Seitennavigation.
- Mobile: Drawer mit Fokusfalle, Escape/Backdrop schließt, Fokus kehrt zum Auslöser zurück.

### 7.2 ObservationBar

- Eingabe: Liste `{label, count, filterPreset, tone}`.
- `tone` nur `neutral|attention|danger`; semantischer Text bleibt immer vorhanden.
- Klick ersetzt nur den dazugehörigen Release-/Qualitätsfilter, bewahrt Suche und unabhängige Filter.
- Aktiver Preset hat `aria-pressed="true"`.

### 7.3 CoopSearch

- Suche clientseitig in M1; Debounce `250ms`.
- Durchsucht Titel, Marke, Kontaktname/-E-Mail und Agenturname, case-insensitiv.
- Clear-Button erscheint ab einem Zeichen und hat Label „Suche leeren“.
- Enter ist nicht erforderlich; Treffer aktualisieren automatisch.
- Ergebnisänderung wird verzögert über eine höfliche Live-Region angekündigt: „12 Kooperationen gefunden.“

### 7.4 FilterPopover / FilterSheet

Desktop als Popover (`360–420px`), mobil als Bottom Sheet mit Vollhöhenoption. Gruppen:

- Status (Mehrfachauswahl),
- Release-Planung (Einfachauswahl),
- Beziehungen (Kontakt, Agentur; suchbare Comboboxen),
- Datenqualität (Mehrfachauswahl),
- abgeschlossene Datensätze einblenden (Toggle).

Desktop übernimmt Checkboxänderungen direkt; „Fertig“ schließt. Mobil sammelt temporär und übernimmt mit „{n} Ergebnisse anzeigen“. „Zurücksetzen“ ist sekundär.

### 7.5 StatusBadge und StatusSelect

- Badge enthält immer Text; Farbe ist Zusatzsignal.
- Klick in Tabelle öffnet kompaktes Select für direkte Statusänderung.
- Mobile direkte Statusänderung nur im Detail, um Fehlbedienung zu vermeiden.
- Auswahl zeigt Gruppenüberschriften, nicht auswählbar.
- Kritische Übergänge (`published`, `cancelled`) verlangen Bestätigung.
- Während Speichern: Auswahl gesperrt, Zeile zeigt kleinen Spinner + „Speichert“.
- Fehler: alter Status wird wiederhergestellt, Inline-Hinweis plus Toast.

### 7.6 EntityChip und RelationshipSignal

`EntityChip` zeigt Kontakt oder Agentur mit Typpräfix in Accessible Name, z. B. sichtbar „Studio Nord“, zugänglich „Agentur: Studio Nord“.

Wenn dieselbe Entity in mindestens zwei **offenen** Coops referenziert wird:

- Chip erhält neben dem Namen ein textliches Multiplicity-Label „3 offene Coops“.
- In der Coop-Zeile erscheint unter dem Titel: „Auch in 2 weiteren offenen Coops“.
- Der Hinweis enthält ein Beziehungssymbol (zwei verbundene Punkte), aber nie nur Farbe/Icon.
- Alle Instanzen derselben Entity verwenden dieselbe neutrale Akzentbehandlung innerhalb der Sitzung; Identität wird primär über Text und ID gesichert.
- Klick setzt `contact=<id>` bzw. `agency=<id>` und zeigt alle passenden Coops.
- Hover/Focus-Preview auf Desktop: kleines Popover mit den anderen Coop-Titeln und Status; maximal fünf, danach „+ n weitere“.
- Im Detail gibt es die Sektion „Weitere offene Coops mit …“ mit klickbaren Zeilen.
- Wenn Kontakt und Agentur beide mehrfach vorkommen, werden beide separat ausgewiesen; nicht zu einem vagen „verknüpft“-Signal zusammenfassen.

Berechnung:

```text
contactOpenCount[id] = Anzahl offener Coops mit contact.id = id
agencyOpenCount[id]  = Anzahl offener Coops mit agency.id = id
isRepeated = count >= 2
otherCount = count - 1
```

Fehlende IDs werden nie nur anhand des angezeigten Namens automatisch zusammengeführt. Optional kann im Detail der nicht-blockierende Hinweis „Mögliche Dublette“ erscheinen; M1 führt nicht automatisch zusammen.

### 7.7 SyncIndicator

- Sichtbare Kurzformen: „Lokal“, „Synchron“, „Ausstehend“, „Konflikt“, „Fehler“ im Detail.
- In der Tabelle darf das Icon kompakt sein, benötigt aber Tooltip und `aria-label`.
- `conflict` und `error` sind klickbar und öffnen die Herkunftssektion im Detail.
- Keine pulsierende Daueranimation.

### 7.8 CoopTable / CoopListCard

- Gemeinsame Datenquelle, Filterlogik und Aktionen.
- Selektierte Tabellenzeile: Hintergrund + linker Fokus-/Auswahlindikator **nur als Zustandsanzeige**, nicht als dekorative Accent Rail; zusätzlich `aria-selected="true"`.
- Tabellenzeilen haben keine individuellen Schatten.
- Mobile Karten haben 1-px-Rahmen; Schatten nur bei aktivem Press-/Drag-Zustand, wobei Drag in M1 nicht vorgesehen ist.

### 7.9 ToastRegion

- `aria-live="polite"`, Fehler bei Bedarf `assertive`.
- Erfolg: „Coop gespeichert.“; automatisches Schließen nach 4 Sekunden.
- Fehler bleibt stehen, bis geschlossen oder erneut versucht.
- Toast ersetzt keine feldnahe Validierung.

---

## 8. Filter, Suche und Sortierung

### 8.1 Standardzustand

- offene Status eingeschlossen,
- abgeschlossene ausgeschlossen,
- keine weiteren Filter,
- Sortierung: „Zuletzt aktualisiert“ absteigend.

### 8.2 Verfügbare Sortierungen

- Zuletzt aktualisiert (neueste zuerst),
- Release-Datum (früheste zuerst; `null` am Ende),
- Status im Workflow,
- Marke A–Z,
- Honorar (höchstes zuerst; unbekannt am Ende).

Sortierung nach Honorar addiert keine Summe und interpretiert „offen“ nicht als null Euro. Bei mehreren Währungen muss M1 diese Option deaktivieren oder auf eine aktive Währung begrenzen; Beträge verschiedener Währungen dürfen nicht direkt verglichen werden.

### 8.3 Filterlogik

- Mehrere Werte innerhalb einer Gruppe: OR.
- Verschiedene Gruppen: AND.
- Suche: AND mit allen Filtern.
- Beobachtungs-Presets setzen nachvollziehbare Filter:
  - `overdue`: `releaseDate < heute`, offen,
  - `14d`: heute bis einschließlich heute + 14 Tage, offen,
  - `missing-release`: Status mindestens `agreed`, aber `releaseDate = null`,
  - `sync-issue`: `syncState in [conflict,error]`.
- „Heute“ wird anhand der konfigurierten lokalen Zeitzone berechnet; Datumswerte sind Kalenderdaten, keine UTC-Zeitpunkte.

### 8.4 Ergebniszustände

- Metazeile: „{n} offene Kooperationen“ bzw. bei eingeblendeten abgeschlossenen „{n} Kooperationen“.
- Bei Suche zusätzlich: „für ‚…‘“.
- Kein paginiertes Verhalten in M1, solange die lokale Fixture-Menge klein ist; Komponenten-API darf spätere serverseitige Pagination nicht verhindern.

---

## 9. Detailansicht

### 9.1 Struktur im Lesemodus

1. **Detailkopf**
   - Titel, Marke, Status,
   - „Bearbeiten“-Button,
   - Schließen/Zurück,
   - letzte Aktualisierung inkl. Urheber.
2. **Planung**
   - Release-Datum,
   - Status,
   - Deliverables/Eckdaten.
3. **Konditionen**
   - Honorar,
   - Vereinbarungszustand.
4. **Kontakt & Agentur**
   - Kontaktname, E-Mail,
   - Agentur,
   - RelationshipSignal und Liste weiterer offener Coops.
5. **Notizen / Briefing**
6. **Herkunft & Sync**
   - Quelle,
   - Quellenreferenz,
   - Syncstatus,
   - Zeitstempel der letzten Synchronisierung nur, wenn vorhanden.
7. **Metadaten**
   - interne ID, Version; visuell zurückhaltend.

Leere Einzelwerte werden als „Noch nicht erfasst“ oder fachlich präziser („Noch nicht vereinbart“) dargestellt. Leere Sektionen dürfen nicht vollständig verschwinden, wenn ihr Fehlen eine relevante Datenlücke ist.

### 9.2 Kontextwechsel

- Klick auf andere Coop in Split View ersetzt den Detailinhalt ohne Seiten-Reload.
- Bei ungespeicherten Änderungen erscheint vorher ein Dialog: „Änderungen verwerfen?“ mit „Weiter bearbeiten“ (primär) und „Verwerfen“ (destruktiv).
- Beziehungseinträge öffnen die referenzierte Coop und bewahren den aktiven Kontakt-/Agenturfilter.

---

## 10. Bearbeitungsflow

### 10.1 Einstieg

- Primär über „Bearbeiten“ im Detail.
- Direkte Statusänderung in Desktop-Liste ist die einzige Inline-Bearbeitung außerhalb des Formulars.
- „Coop anlegen“ nutzt dasselbe Formular ohne Herkunftsdaten; `source=manual`, `syncState=local`.

### 10.2 Formularaufbau

Sektionen und Felder:

1. **Grunddaten**
   - Coop-Titel (Pflicht, max. 120 Zeichen),
   - Marke (Pflicht, max. 100 Zeichen),
   - Status (Pflicht).
2. **Planung**
   - Release-Datum (optional, Pflicht bei `scheduled`),
   - Deliverables als wiederholbare Textzeilen (je max. 120 Zeichen).
3. **Konditionen**
   - Honorarstatus,
   - Betrag (dezimal, `>= 0`; nur sichtbar bei `proposed|agreed`),
   - Währung (Pflicht mit Betrag).
4. **Beziehungen**
   - Kontakt-Combobox,
   - Kontakt-E-Mail,
   - Agentur-Combobox.
5. **Eckdaten / Notizen**
   - Mehrzeilentext, max. 4.000 Zeichen.
6. **Herkunft**
   - read-only in M1.

### 10.3 Beziehungsauswahl

- Combobox sucht bestehende Kontakte/Agenturen nach Name und E-Mail.
- Treffer zeigen „bereits in {n} offenen Coops“, bevor gewählt wird.
- „Neu anlegen“ ist eindeutig von bestehenden Treffern getrennt.
- Gleichnamige Entities zeigen E-Mail bzw. Agentur als Disambiguierung.
- Bei möglicher Dublette: Warnung „Ein ähnlicher Kontakt existiert bereits“ mit Auswahl bestehender Datensätze; Speichern bleibt möglich, da M1 keine automatische Fusion macht.

### 10.4 Validierung

- Feldvalidierung nach Blur und beim Speichern.
- Zusammenfassung oberhalb des Formulars nur nach fehlgeschlagenem Speichern: „Bitte 2 Felder prüfen“, mit Sprunglinks.
- Fehlermeldung beschreibt Lösung, z. B. „Release-Datum wählen, bevor der Status auf ‚Eingeplant‘ gesetzt wird.“
- Datumseingabe nutzt sichtbares lokales Format `TT.MM.JJJJ`, Datenmodell ISO `YYYY-MM-DD`.
- Betrag akzeptiert lokales Dezimalkomma, speichert normalisiert.

### 10.5 Speichern

1. Nutzer:in klickt „Speichern“.
2. Client validiert.
3. Button zeigt „Speichert …“ und ist deaktiviert; Abbrechen bleibt deaktiviert, solange der Request läuft.
4. Erfolg: Lesemodus, Toast „Coop gespeichert.“, `updatedAt`, `updatedBy` und `version` aktualisieren.
5. Netzwerk-/Serverfehler: Formular bleibt mit Eingaben offen; feldunabhängiger Fehler über Aktionsleiste mit „Erneut versuchen“.
6. Versionskonflikt: kein Überschreiben. Konfliktdialog mit:
   - „Neuere Version laden“ (empfohlen),
   - „Meine Eingaben kopieren“ (kopiert als Text/JSON zur Sicherung),
   - „Abbrechen“.

Für M1 darf Persistenz lokal simuliert werden, der UI-Vertrag muss jedoch asynchron (`idle|saving|success|error|conflict`) implementiert sein.

---

## 11. Systemzustände

### 11.1 Initial Loading

- App-Shell und Seitentitel sofort anzeigen.
- Beobachtungsleiste: vier textnahe Skeletons.
- Desktop-Tabelle: Header plus sechs Skeletonzeilen in realistischen Spaltenbreiten.
- Mobile: vier Skeletonkarten.
- Keine Spinner-Vollseite bei initialem Laden.
- `aria-busy="true"` am Ergebnisbereich; einmalige Live-Meldung „Kooperationen werden geladen.“

### 11.2 Refresh / Filter Loading

- Bestehende Ergebnisse bleiben sichtbar, erhalten optional reduzierte Opazität (`0.65`).
- Schlanker Progressindikator direkt über der Liste.
- Filter bleiben bedienbar, außer derselbe Request muss serialisiert werden.
- Nach Abschluss Live-Meldung mit Trefferzahl.

### 11.3 Global Empty

Wenn noch keine Coop existiert:

- Überschrift: „Noch keine Kooperationen“
- Text: „Lege die erste Kooperation manuell an. Der Gmail-Import folgt in einem späteren Meilenstein.“
- Primäraktion: „Coop anlegen“
- Keine Illustration erforderlich.

### 11.4 No Results

Wenn Coops existieren, aber Filter nichts liefern:

- „Keine Kooperationen für diese Auswahl“
- aktive Filter zusammenfassen,
- Aktionen: „Filter zurücksetzen“ und optional „Suche leeren“.
- Niemals denselben Zustand wie globales Empty verwenden.

### 11.5 Listenfehler

- Inhaltlicher Fehlerblock an Position der Liste:
  - „Kooperationen konnten nicht geladen werden.“
  - technisch verständliche, nicht interne Zusatzinfo,
  - Button „Erneut versuchen“.
- App-Navigation und Filter bleiben erhalten.
- Wenn Cache-Daten vorhanden sind: Daten sichtbar lassen und Banner „Stand möglicherweise veraltet“ anzeigen.

### 11.6 Detailfehler

- Panel bleibt offen.
- „Details konnten nicht geladen werden.“ + Retry.
- Coop-Grundtitel aus Listenzeile darf als Kontext stehen bleiben.

### 11.7 Sync-Konflikt und Sync-Fehler

- `conflict`: persistentes, amberfarbenes Statusbanner im Detail mit textlicher Erklärung.
- `error`: persistentes, rotes Statusbanner mit „Erneut synchronisieren“ erst in einem Meilenstein mit echter Integration; in M1 stattdessen deaktivierter Button mit Erklärung „Live-Synchronisierung noch nicht aktiv“.
- Statusfarbe niemals ohne Text/Badge.

---

## 12. Interaktionsregeln

### 12.1 Tastatur

- `Tab` folgt visueller Reihenfolge.
- `Enter` auf fokussiertem Titel/Zeilen-Trigger öffnet Detail.
- `Escape` schließt Popover; Panel nur, wenn kein ungespeicherter Entwurf vorliegt.
- Statusmenü: Pfeiltasten, Home/End, Enter, Escape nach ARIA-Listbox-/Menu-Pattern.
- Comboboxen folgen WAI-ARIA Combobox Pattern.
- Kein globales Ein-Tasten-Shortcut in M1; verhindert Konflikte mit Texteingaben.

### 12.2 Fokusmanagement

- Nach Öffnen eines Overlays Fokus auf Überschrift/erstes sinnvolles Control.
- Nach Schließen Fokus auf auslösendes Element zurück.
- Nach Speichern Fokus auf „Bearbeiten“ im Detailkopf.
- Nach Löschen eines Filters Fokus auf nächsten Chip, sonst Filterbutton.
- Bei Validation Error Fokus auf Fehlerzusammenfassung; Sprunglink fokussiert das Feld.

### 12.3 Hover, Focus, Active

- Hover darf zusätzliche Information anbieten, aber nie erforderlich sein.
- `:focus-visible`: 2-px-Ring plus 2-px-Offset, nicht durch `outline: none` entfernen.
- Active/Pressed: kleine Farb-/Positionsänderung, keine skalierende Animation, die Layout oder Text verwischt.

### 12.4 Bewegung

- Panel: maximal `180ms`, ease-out.
- Popover/Toast: maximal `120ms`.
- Keine Schleifen, kein Pulsieren.
- Bei `prefers-reduced-motion: reduce`: Übergänge auf `0–1ms`, keine räumliche Slide-Bewegung.

---

## 13. Barrierefreiheit

Ziel: WCAG 2.2 AA.

### 13.1 Semantik

- Ein H1 pro Seite.
- Desktop-Liste als echte `<table>` mit `<caption class="sr-only">Offene Kooperationen</caption>`.
- Spaltenköpfe mit `scope="col"`; sortierbare Spalte mit `aria-sort`.
- Mobile Liste als `<ul>` mit `<article>` je Eintrag.
- Details als `<aside aria-labelledby="coop-detail-title">` in Split View; mobile Vollseite als `<main>`.
- Statusänderungen und Trefferzahl über getrennte, sparsam genutzte Live-Regionen.

### 13.2 Kontrast und Nicht-Farb-Signale

- Normaltext mindestens 4.5:1, großer Text mindestens 3:1.
- UI-Komponenten und Fokusindikatoren mindestens 3:1 gegen angrenzende Farbe.
- Jeder Status enthält Text; Syncstatus enthält Text/Tooltip/Accessible Name.
- Wiederkehrende Beziehungen enthalten Anzahl und Formulierung, nicht nur Akzentfarbe.

### 13.3 Touch und Zoom

- Interaktive Ziele mindestens 44 × 44 CSS px; Ausnahme für textinterne Links nur bei ausreichendem Abstand.
- Seite bleibt bis 400 % Zoom nutzbar; kein zweidimensionales Scrollen der gesamten Seite.
- Bei 320 CSS px kein Abschneiden von Kernaktionen.

### 13.4 Formulare

- Sichtbare Labels, keine Placeholder-only-Felder.
- Pflichtfelder zusätzlich textlich markiert.
- `aria-describedby` verbindet Hilfe und Fehler.
- Fehler sind konkret, persistent und programmatisch zugeordnet.
- Währungs- und Datumsformat werden als Hilfetext genannt.

### 13.5 Sprache und Texte

- Dokument-/App-Sprache `de`.
- Keine internen Status-IDs in der Oberfläche.
- Relative Zeit („vor 2 Std.“) erhält absoluten Zeitstempel im Accessible Name/Tooltip.
- Abkürzungen vermeiden; „Coop“ wird als etablierter Produktbegriff verwendet, die erste leere Ansicht spricht zusätzlich von „Kooperationen“.

---

## 14. Visuelle Tokens

Keine bestehende Markenidentität lag für M1 vor. Die Tokens sind daher ein bewusst ruhiges, arbeitsorientiertes Startsystem und müssen zentral als CSS Custom Properties oder äquivalente Theme-Tokens implementiert werden.

### 14.1 Typografie

Bewusste Wahl: **IBM Plex Sans** für produktive, tabellarische Oberflächen; **IBM Plex Mono** ausschließlich für interne IDs/technische Metadaten. Fonts bevorzugt selbst hosten; bis Assets vorliegen, Fallback ohne Layoutbruch.

```css
--font-sans: "IBM Plex Sans", "Segoe UI", sans-serif;
--font-mono: "IBM Plex Mono", "SFMono-Regular", monospace;

--text-xs: 0.75rem;   /* 12 */
--text-sm: 0.875rem;  /* 14 */
--text-md: 1rem;      /* 16 */
--text-lg: 1.25rem;   /* 20 */
--text-xl: 1.5rem;    /* 24 */
--text-2xl: 2rem;     /* 32 */

--leading-tight: 1.2;
--leading-normal: 1.5;
```

Minimum für reguläre Inhalts- und Formtexte: `14px`, mobil bevorzugt `16px` für Inputs zur Vermeidung von Auto-Zoom.

### 14.2 Farben

Palette ohne Tech-Indigo und ohne Verlauf:

```css
--color-canvas: #F5F4F0;
--color-surface: #FFFFFF;
--color-surface-subtle: #EFEEE9;
--color-ink: #1C211F;
--color-ink-muted: #59615D;
--color-border: #D6D9D5;
--color-border-strong: #AAB1AC;

--color-accent: #0C6B58;        /* Petrolgrün */
--color-accent-hover: #095646;
--color-accent-soft: #E2F1EC;
--color-focus: #087EA4;

--color-info: #245D78;
--color-info-soft: #E7F1F5;
--color-warning: #8A5A00;
--color-warning-soft: #FFF1CE;
--color-danger: #A2382A;
--color-danger-soft: #FBE8E4;
--color-success: #2F6D47;
--color-success-soft: #E4F2E8;
```

Statusbadges nutzen Kombinationen aus neutralen, Info-, Warning- und Success-Tokens. Nicht für jeden Status eine neue Markenfarbe erfinden; Workflow-Gruppen unterscheiden sich zusätzlich durch Text.

### 14.3 Abstand, Größe, Radius

```css
--space-1: 0.25rem;  /* 4 */
--space-2: 0.5rem;   /* 8 */
--space-3: 0.75rem;  /* 12 */
--space-4: 1rem;     /* 16 */
--space-5: 1.25rem;  /* 20 */
--space-6: 1.5rem;   /* 24 */
--space-8: 2rem;     /* 32 */
--space-10: 2.5rem;  /* 40 */
--space-12: 3rem;    /* 48 */

--radius-sm: 4px;
--radius-md: 8px;
--radius-lg: 12px;
--control-h-sm: 36px;
--control-h-md: 44px;
```

Keine pillenförmigen Großcontainer. Pill-Radius nur für kompakte Filter-/Statuschips.

### 14.4 Rahmen und Elevation

```css
--shadow-overlay: 0 16px 40px rgb(28 33 31 / 0.16);
--shadow-popover: 0 8px 24px rgb(28 33 31 / 0.14);
--border-default: 1px solid var(--color-border);
```

- Tabelle und Karten primär über Linien und Flächen gliedern.
- Schatten ausschließlich für räumlich überlagernde Elemente (Drawer, Dialog, Popover), nicht für jede Zeile/Karte.
- Kein Blur/Glassmorphism.

### 14.5 Dichte

Desktop Standarddichte: Zeile mindestens `76px`; kompakte Dichte ist nicht Teil von M1. Formulare verwenden `12px` Label-zu-Control und `24px` Feldgruppenabstand. Inhaltliche Sektionen werden durch Überschrift + Abstand bzw. 1-px-Trenner strukturiert, nicht durch verschachtelte Karten.

---

## 15. Textuelle Wireframes

### 15.1 Desktop — Liste ohne geöffnetes Detail

```text
┌───────────────┬──────────────────────────────────────────────────────────────────────────────┐
│ Produktname   │ Kooperationen                                      [ Coop anlegen ]          │
│               │ Offene Kooperationen planen und bearbeiten.                                │
│ Kooperationen │                                                                              │
│ Planung       │ Release überfällig · {n} | Nächste 14 Tage · {n} | Termin fehlt · {n}      │
│               │                                                                              │
│               │ [ Suche Marke, Coop, Kontakt, Agentur …        ] [Filter {n}] [Status] [Sort]│
│               │ [Agentur: Studio Nord ×] [Status: Verhandlung ×] Alle zurücksetzen          │
│               │ {n} offene Kooperationen                                                   │
│               │ ┌──────────────────────────────────────────────────────────────────────────┐ │
│               │ │ Coop       Status       Release      Honorar     Kontakt/Agentur   …    │ │
│               │ ├──────────────────────────────────────────────────────────────────────────┤ │
│               │ │ Video A    Verhandlung  Nicht vereinb. Offen     Lea · Studio Nord  ⋮   │ │
│               │ │ Marke X    Auch in 2 weiteren offenen Coops                           │ │
│               │ ├──────────────────────────────────────────────────────────────────────────┤ │
│               │ │ Reel B     Eingeplant   24.10.2026   vereinbart  Lea · Studio Nord  ⋮   │ │
│               │ └──────────────────────────────────────────────────────────────────────────┘ │
└───────────────┴──────────────────────────────────────────────────────────────────────────────┘
```

Die Zahlen sind im Wireframe nur Layoutbeispiele, keine Produktkennzahlen oder Seed-Datenvorgabe.

### 15.2 Desktop — Split View mit Detail

```text
┌───────────────┬───────────────────────────────────────┬──────────────────────────────────────┐
│ Navigation    │ Filter + Coop-Liste                  │ Video A                         [×]  │
│               │                                       │ Marke X                 [Bearbeiten] │
│               │ > Video A  Verhandlung               │ Verhandlung · aktualisiert …          │
│               │   Video B  Eingeplant                │──────────────────────────────────────│
│               │   Video C  In Freigabe               │ Planung                             │
│               │                                       │ Release: Noch nicht vereinbart       │
│               │                                       │ Deliverables: …                      │
│               │                                       │                                      │
│               │                                       │ Konditionen                          │
│               │                                       │ Honorar: Offen                       │
│               │                                       │                                      │
│               │                                       │ Kontakt & Agentur                    │
│               │                                       │ Lea · Studio Nord · 3 offene Coops   │
│               │                                       │ Weitere offene Coops mit Studio Nord │
│               │                                       │ • Reel B — Eingeplant                │
│               │                                       │ • Video C — In Freigabe              │
│               │                                       │                                      │
│               │                                       │ Herkunft & Sync: Gmail-Vorschau      │
└───────────────┴───────────────────────────────────────┴──────────────────────────────────────┘
```

### 15.3 Mobile — Liste

```text
┌──────────────────────────────┐
│ [☰] Kooperationen      [Neu] │
├──────────────────────────────┤
│ Überfällig · {n} | 14 T. · {n}│  ← horizontal scrollbar
│ [ Suche …                  ] │
│ [Filter {n}] [Sortieren] {n}│
│ [Studio Nord ×] [Verhand…]  │
│                              │
│ ┌──────────────────────────┐ │
│ │ Verhandlung      Sync ok │ │
│ │ Video A                  │ │
│ │ Marke X                  │ │
│ │ Release      Honorar     │ │
│ │ Nicht vereinb. Offen     │ │
│ │ Lea · Studio Nord        │ │
│ │ Auch in 2 weiteren Coops │ │
│ │ Aktualisiert …       [⋮] │ │
│ └──────────────────────────┘ │
│ ┌──────────────────────────┐ │
│ │ Eingeplant      Lokal    │ │
│ │ Reel B                   │ │
│ │ …                        │ │
│ └──────────────────────────┘ │
└──────────────────────────────┘
```

### 15.4 Mobile — Bearbeitung

```text
┌──────────────────────────────┐
│ [←] Coop bearbeiten          │
├──────────────────────────────┤
│ Grunddaten                   │
│ Coop-Titel *                 │
│ [ Video A                  ] │
│ Marke *                      │
│ [ Marke X                  ] │
│ Status *                     │
│ [ Verhandlung             ▾] │
│                              │
│ Planung                     │
│ Release-Datum                │
│ [ TT.MM.JJJJ              ] │
│ …                            │
│                              │
├──────────────────────────────┤
│ [Abbrechen]      [Speichern] │  ← sticky + safe area
└──────────────────────────────┘
```

---

## 16. Implementierungs- und Abnahmekriterien

### 16.1 Komponenten-/State-Schnitt

Empfohlene Zustandsgrenzen, unabhängig vom finalen Framework:

- `CoopRepository`: Laden, Erstellen, Aktualisieren, Versionsprüfung.
- `useCoopQueryState`: URL ↔ Suche/Filter/Sortierung/Selektion.
- `deriveRelationshipIndex(coops)`: wiederkehrende Kontakte/Agenturen ausschließlich aus offenen Coops.
- `deriveObservationPresets(coops, today, timezone)`: Beobachtungszahlen und Filter.
- `CoopWorkspace`: orchestration von Liste und Detail.
- Präsentationskomponenten bleiben frei von eigener fachlicher Datenmutation.

### 16.2 Mindest-Fixtures für UX-Tests

Fixture-Daten sollen Fälle, nicht Business-Ergebnisse abbilden:

- mindestens zwei offene Coops mit derselben `contact.id`,
- mindestens zwei offene Coops mit derselben `agency.id`,
- eine Coop ohne Release-Datum ab Status `agreed`,
- eine mit vorgeschlagenem und eine mit vereinbartem Honorar,
- je ein Syncstatus `local`, `synced`, `conflict`,
- eine veröffentlichte und eine abgesagte Coop für den Abschlussfilter,
- zwei gleichnamige Kontakte mit unterschiedlichen IDs/E-Mails.

Diese Fixtures sind Testdaten und dürfen nicht als echte Kennzahlen oder Produktversprechen erscheinen.

### 16.3 Akzeptanzkriterien

1. Beim Start sind alle und nur offene Coops sichtbar; abgeschlossene lassen sich gezielt einblenden.
2. Suche findet Titel, Marke, Kontakt und Agentur und ist mit Filtern kombinierbar.
3. URL speichert Filter, Sortierung und ausgewählte Coop; Reload stellt den Zustand wieder her.
4. Derselbe Kontakt/dieselbe Agentur in mehreren offenen Coops ist in Liste und Detail textlich samt Anzahl erkennbar.
5. Klick auf einen wiederkehrenden Kontakt/eine Agentur filtert alle zugehörigen Coops.
6. Desktop bietet Tabelle und Split/Drawer-Detail; Mobile bietet Kartenliste und Vollbilddetail.
7. Statusänderung ist möglich; `scheduled` ohne Release-Datum wird verhindert und erklärt.
8. Honorar `null` erscheint als „offen“, niemals als 0.
9. Quelle und Syncstatus sind nachvollziehbar; Gmail wird in M1 eindeutig als Vorschau ohne Live-Sync bezeichnet.
10. Loading, globales Empty, No-results, Listenfehler, Detailfehler, Save-Fehler und Versionskonflikt sind implementierbar und testbar.
11. Alle Kernaktionen funktionieren per Tastatur; Fokus kehrt nach Overlays korrekt zurück.
12. Status, Sync und Beziehungen sind nicht ausschließlich farbcodiert.
13. Bei 320 px Breite und 400 % Zoom bleiben Kernaktionen erreichbar.
14. Es gibt keine generischen KPI-Karten, erfundenen Business-Kennzahlen, Charts oder Marketing-Heroes.

### 16.4 Empfohlene QA-Szenarien

- Filter „Studio Nord“ öffnen, Beziehungshinweis prüfen, andere Coop im selben Kontext öffnen.
- Status auf „Eingeplant“ setzen, ohne Datum: Validierungsfehler; Datum ergänzen, speichern.
- Bearbeitung starten, andere Coop anklicken: Verwerfen-Dialog.
- Speichern mit simuliertem Serverfehler: Eingaben bleiben erhalten.
- Speichern mit veralteter `version`: Konfliktdialog, kein stilles Überschreiben.
- Suche ohne Treffer vs. komplett leerer Datenbestand unterscheiden.
- Tastaturnavigation durch Suche, Filter, Tabelle, Detail und Formular.
- Screenreader-Ausgabe für wiederkehrenden Kontakt und Sync-Konflikt prüfen.
- `prefers-reduced-motion`, 200 % und 400 % Zoom sowie 320/375/768/1024/1440 px testen.

---

## 17. Anti-Slop-Audit nach claude-design

### 17.1 Diagnose vor Korrektur

Bewertung der Spezifikation gegen die zehn Claude-Design-Tells:

| Tell | Gefeuert? | Begründung |
|---|---:|---|
| 1. Tech gradient | Nein | Keine Verläufe vorgesehen. |
| 2. Generic tech hue | Nein | Bewusstes Petrolgrün statt Default-Indigo/Violett. |
| 3. Feature-tile grid | Nein | Arbeitsliste, Filter und Detail bilden die Komposition; keine Feature-Kacheln. |
| 4. Accent rail | Nein | Kein dekorativer Kartenstreifen; Auswahlindikator ist rein zustandsgebunden und zusätzlich semantisch markiert. |
| 5. Unearned blur | Nein | Kein Glassmorphism/Blur; Schatten nur für echte Overlays. |
| 6. Monument stat | Nein | Beobachtungswerte sind kompakte Filteraktionen, keine großen Zahlen/KPIs. |
| 7. Icon topper | Nein | Icons werden nur funktional und zusammen mit Text eingesetzt. |
| 8. Center stack | Nein | Linksbündige, dichte Operate-Komposition mit Tabelle und Inspector. |
| 9. Default type | Nein | IBM Plex Sans wurde bewusst für tabellarische Produktivität gewählt. |
| 10. Wrong surface | Nein | Primär Operate (Arbeitsliste + Bearbeitung), sekundär Monitor (Beobachtungsleiste + Planung). |

**Diagnose-Score: 0/10 Slop-Punkte.** Es feuern keine Tells; insbesondere keine kompositorischen Tells (3, 8, 10).

### 17.2 Behandlung

Da die Diagnose keine Tells auslöst, ist keine kosmetische Scheinreparatur angebracht. Zur Absicherung wurden folgende Guardrails explizit in die Spezifikation aufgenommen:

- Beobachtung als flache Aktionsleiste statt KPI-Karten,
- Tabelle/Listenkarte statt gleichgewichteter Kachelmatrix,
- Beziehungen als konkrete, filterbare Datenverknüpfungen,
- keine erfundenen Zahlen, Trends oder Charts,
- Schatten ausschließlich bei realer Überlagerung,
- keine Farb- oder Icon-only-Kommunikation.

### 17.3 Re-Score nach Guardrails

**Finaler Score: 0/10.** Die Oberfläche bleibt Operate-first, sachlich, datenwahr und frei von den gelisteten AI-Design-Tells.

---

## 18. Offene Entscheidungen für die Freigabe nach Meilenstein 1

Diese Punkte blockieren die UX-Spezifikation nicht, sollen aber erst nach expliziter Meilenstein-Freigabe in Produktcode bzw. Folgemeilensteine einfließen:

- finaler Produktname/Logo,
- tatsächliche technische Quelle der Gmail-Thread-Referenzen,
- Rollen und Berechtigungen für manuelle vs. Agent-Änderungen,
- fachliche Regeln für erlaubte Statusübergänge jenseits der M1-Validierungen,
- Umgang mit mehreren Währungen,
- Merge-Workflow für Kontakt-/Agentur-Dubletten,
- serverseitige Pagination und Volltextsuche bei wachsender Datenmenge.

---

## 19. Finaler Anti-Slop-Abschlussaudit

Nach vollständiger Spezifikation erneut gegen alle zehn `claude-design`-Tells geprüft: **0/10 Slop-Punkte**. Es feuern weder kompositorische Tells (Feature-Tile-Grid, Center Stack, falsche Surface) noch kosmetische Tells (Tech-Gradient, Default-Indigo, Accent Rail, Blur, Monument-Stat, Icon-Topper, Default-Typografie). Die finale Komposition bleibt **Operate-first**, nutzt Monitoring nur als kompakte, datenwahre Filterhilfe und enthält weder generische KPI-Karten noch erfundene Business-Kennzahlen.
