# mcp-garmin

Read-only MCP-Server für Garmin Connect. Portfolio-weite Konventionen stehen in
`msk-core/CONVENTIONS.md`; hier nur das Projektspezifische.

## Herkunft des Codes

`garmin_mcp/client.py` und `garmin_mcp/fit/*` sind **Kopien aus MyFITContainer**
(Commit im Datei-Header). Source of truth bleibt MFC. Änderungen hier sind mit
`mcp-garmin addition` markiert — aktuell nur `GarminClient.search_activities`,
`GarminClient.get_activity_detail` und zwei umgebogene Modulnamen
(`fit_parser` -> `fit.parser`, `fit_devfields` -> `fit.devfields`).

Wer in MFC am Garmin-Client oder an `fit_parser`/`streams` etwas repariert, muss
hier nachziehen. Umgekehrt genauso. Ein geteiltes Package lohnt erst, wenn das
öfter als zweimal passiert.

## Warum der Login nicht auf dem Server läuft

Garmins SSO steht hinter Cloudflare und beantwortet frische Logins von
Rechenzentrums-IPs seit März 2026 mit 429/403. `garmin-mcp login` läuft deshalb
lokal beim Nutzer; auf den Server wandern nur die Tokens. OAuth1 hält ~1 Jahr
und erzeugt OAuth2-Tokens gegen `connectapi.garmin.com` — der Server fasst
`sso.garmin.com` nie an. Das ist kein Komfort-Detail, sondern der Grund, warum
der Connector auf Free-Tier-IPs überhaupt zuverlässig läuft.

## MCP-SDK: 2.x, nicht 1.x

Dieses Projekt nutzt `mcp.server.mcpserver.MCPServer` (SDK 2.x). MFC hängt noch
auf `mcp<2` (`mcp.server.fastmcp`). Die Portierung ist klein: Importpfad plus
`stateless_http`/`json_response` wandern vom Konstruktor in
`streamable_http_app()`. Der Rest — eigener Starlette-Wrapper um
`session_manager.handle_request`, Bearer davor — bleibt gleich. `garmin_mcp/server.py`
ist damit die Vorlage für MFCs Migration.

## Fehler müssen beim Client ankommen

Wirft ein Tool eine beliebige Exception, meldet das SDK nur
„Error executing tool <name>" und verschluckt die Ursache. Der häufigste Fall
(keine Tokens) wäre damit unlesbar. Deshalb hängt an jedem Tool `_guard`, das
`NotConnected`/`GarminError`/`ValueError` in `ToolError` übersetzt — nur die
kommen im Klartext beim Modell an.

## Antworten müssen klein sein

Garmins JSON ist gigantisch, `extract_streams` liefert bis zu 1500 Punkte je
Kanal. Alles geht durch `project.py` bzw. `fitview.py`; Streams werden auf
`max_points` (Default 120) nachverdichtet, je Kanal mit min/max/avg. Neue Tools
ohne Projektion sind ein Bug, kein Feature.

## Zwei Auth-Wege, und warum beide nötig sind

`MCP_TOKEN` (statischer Bearer) reicht für Claude Code und Claude Desktop.
claude.ai und ChatGPT haben **kein Feld für einen eigenen Header** — die gehen
zwingend über den OAuth-AS in `oauth.py` (DCR + PKCE + Consent-Screen mit
`MCP_PASSPHRASE`). Der AS ist damit Pflicht, nicht Komfort.
