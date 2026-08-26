# mcp-garmin

Read-only MCP-Server für Garmin Connect, mandantenfähig. Portfolio-weite
Konventionen stehen in `msk-core/CONVENTIONS.md`; hier nur das Projektspezifische.

## Herkunft des Codes

`garmin_mcp/client.py` und `garmin_mcp/fit/*` sind **Kopien aus MyFITContainer**
(Commit im Datei-Header). Source of truth bleibt MFC. Änderungen hier sind mit
`mcp-garmin addition` markiert - aktuell `GarminClient.search_activities`,
`get_activity_detail`, `list_adhoc_challenges`, `get_adhoc_challenge` und zwei
umgebogene Modulnamen (`fit_parser` -> `fit.parser`, `fit_devfields` ->
`fit.devfields`).

Wer in MFC am Garmin-Client oder an `fit_parser`/`streams` etwas repariert, muss
hier nachziehen. Umgekehrt genauso.

## Zwei Identitäten, ein Mechanismus

stdio ist Single-User: der Prozess gehört dem, der ihn startet, Tokens kommen
aus der lokalen Datei. HTTP ist immer mandantenfähig.

Die ganze Trennung hängt an **einer** Stelle: `_McpEndpoint` löst den Bearer über
`oauth.access_token_user()` zum Konto auf, setzt `session.CURRENT_USER` und gibt
erst dann an den Session-Manager ab. Tools holen ihre Session pro Aufruf über
`current_session()`. Eine an `register()` gebundene Session würde den Server
sofort wieder einbenutzerfähig machen - deshalb reicht `tools.register` eine
Funktion herum, keine Instanz.

Verifiziert: eine ContextVar, im ASGI-Callable gesetzt, ist im Tool-Coroutine
sichtbar (`test_two_accounts_never_see_each_other`).

**Der FIT-Cache liegt je Konto in einem eigenen Unterverzeichnis.** Das sind
fremde Trainingsdaten; ein flacher Cache wäre ein Datenleck zwischen Konten.

## `db.conn()` committet nicht, wenn eine Exception durchfliegt

Der Kontextmanager macht `yield` und danach `commit()`. Fliegt eine Exception
durch den `yield`, wird der Commit übersprungen und die Verbindung schließt -
Rollback. Der Fehlversuchs-Zähler in `verify_login` war genau deshalb wirkungslos
(die Sperre hätte nie gegriffen). Wer in einem `with conn()`-Block schreibt und
danach eine Exception wirft, muss den Block vorher verlassen.

## Kein statischer Bearer, keine Passphrase

`MCP_PASSPHRASE` und `MCP_TOKEN` gab es im Single-User-Stand; beide sind raus.
Ein statischer Bearer lässt sich keinem Konto zuordnen und wäre in einem
Mehrbenutzer-Server ein Generalschlüssel. claude.ai und ChatGPT haben ohnehin
kein Feld für einen eigenen Header - der OAuth-AS in `oauth.py` ist Pflicht,
nicht Komfort. Identität am Consent-Screen ist das Session-Cookie.

## Garmin verbinden: zwei Wege, und warum beide

`connect.py`. Der Web-Login spricht Garmins SSO **vom Server aus** - genau der
Pfad, den Cloudflare bei Rechenzentrums-IPs zeitweise blockt. Deshalb gibt es
zusätzlich den Import des Blobs aus `garmin-mcp export`, der ohne Server-Login
auskommt. SSO-Logins sind prozessweit über einen Lock serialisiert: mehrere
gleichzeitige Logins sind das Muster, das eine IP bei Garmin einsammelt.

## MCP-SDK: 2.x

`mcp.server.mcpserver.MCPServer`. MFC hängt noch auf `mcp<2`
(`mcp.server.fastmcp`); die Portierung ist klein: Importpfad, und
`stateless_http`/`json_response` wandern vom Konstruktor in
`streamable_http_app()`. `garmin_mcp/server.py` ist die Vorlage dafür.

## Fehler müssen beim Client ankommen

Wirft ein Tool eine beliebige Exception, meldet das SDK nur
„Error executing tool <name>" und verschluckt die Ursache. Deshalb hängt an jedem
Tool `_guard`, das `NotConnected`/`GarminError`/`ValueError` in `ToolError`
übersetzt - nur die kommen im Klartext beim Modell an. Der häufigste Fall im
Mehrbenutzerbetrieb ist „Garmin noch nicht verbunden", und der muss den Nutzer
zur Kontoseite führen.

## Antworten müssen klein sein

Garmins JSON ist gigantisch, `extract_streams` liefert bis zu 1500 Punkte je
Kanal. Alles geht durch `project.py` bzw. `fitview.py`; Streams werden auf
`max_points` (Default 120) nachverdichtet, je Kanal mit min/max/avg. Neue Tools
ohne Projektion sind ein Bug, kein Feature.

## Admin-Oberfläche

`/admin`, sichtbar nur für Konten mit `users.is_admin`. Das **erste** Konto eines
Servers bekommt das Flag automatisch (`create_user`), ältere Datenbanken ohne
Admin bekommen es beim nächsten `db.init()` für das älteste Konto - beides
absichtlich implizit, weil sonst niemand an die Seite käme, ohne SSH zu haben.

Ein Nicht-Admin bekommt **404, nicht 403**: die Existenz der Seite ist selbst
eine Information. Der Admin kann sein eigenes Konto dort nicht ändern (sonst
sperrt man sich aus), und `set_admin` weigert sich, den letzten Admin zu
degradieren.

Die CLI (`garmin-mcp invite|user`) bleibt vollwertig bestehen - sie ist der Weg
zurück, wenn sich niemand mehr einloggen kann.

Bewusst in Kauf genommen: Mit der Seite kann ein gestohlenes Session-Cookie
Konten anlegen und löschen; vorher brauchte das SSH-Zugang. Deshalb das
Admin-Flag statt „jeder eingeloggte Nutzer".

## Betrieb

`/root/mcp-garmin/` auf dem Strato-VPS nach Hausmuster, Vhost
`mcp.garmin.skerwiderski.cloud` mit `flush_interval -1`. Alles Persistente liegt
im Volume unter `/data` (SQLite + FIT-Cache). Kein Admin-Web: Einladungen und
Konten laufen über `docker exec mcp-garmin garmin-mcp invite|user`.

**Backup bewusst nicht.** Das Volume enthält fremde Garmin-Tokens; die sind in
zwei Minuten neu geholt, jede Kopie ist ein zusätzliches Risiko. `APP_SECRET`
gehört dagegen in deine Passwortverwaltung - ohne ihn müssen alle neu verbinden.
