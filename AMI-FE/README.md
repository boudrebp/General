# General

Bulk-send MeterException payloads to an API endpoint using a JSON template and text event list.

Files in this repo:
- Sample MeterException Payload: Template JSON payload with placeholders.
- send_meter_exceptions.py: Script that builds and posts one payload per text line.
- send_meter_exceptions.ps1: PowerShell script that builds and posts one payload per text line.
- meter_events.txt: Sample event input.

Quick start:
1. Dry run (prints generated payloads, no API calls):
	python3 send_meter_exceptions.py --events meter_events.txt --endpoint https://example.com/api/meter-exceptions --dry-run
2. Send for real:
	python3 send_meter_exceptions.py --events meter_events.txt --endpoint https://example.com/api/meter-exceptions

PowerShell quick start (Windows/PowerShell 7+):
1. Dry run:
	pwsh ./send_meter_exceptions.ps1 -EventsPath ./meter_events.txt -TemplatePath "./Sample MeterException Payload" -Endpoint "https://example.com/api/meter-exceptions" -DryRun
2. Send for real:
	pwsh ./send_meter_exceptions.ps1 -EventsPath ./meter_events.txt -TemplatePath "./Sample MeterException Payload" -Endpoint "https://example.com/api/meter-exceptions"

Add headers (repeatable):
python3 send_meter_exceptions.py --events meter_events.txt --endpoint https://example.com/api/meter-exceptions --header "Authorization: Bearer YOUR_TOKEN" --header "x-api-key: YOUR_KEY"

PowerShell with headers:
pwsh ./send_meter_exceptions.ps1 -EventsPath ./meter_events.txt -TemplatePath "./Sample MeterException Payload" -Endpoint "https://example.com/api/meter-exceptions" -Header "Authorization: Bearer YOUR_TOKEN" -Header "x-api-key: YOUR_KEY"

Built-in bearer token request (OAuth client credentials):
Python:
python3 send_meter_exceptions.py --events meter_events.txt --endpoint https://example.com/api/meter-exceptions --token-url https://example.com/oauth/token --token-client-id YOUR_CLIENT_ID --token-client-secret YOUR_CLIENT_SECRET --token-scope "api.read api.write"

PowerShell:
pwsh ./send_meter_exceptions.ps1 -EventsPath ./meter_events.txt -TemplatePath "./Sample MeterException Payload" -Endpoint "https://example.com/api/meter-exceptions" -TokenUrl "https://example.com/oauth/token" -TokenClientId "YOUR_CLIENT_ID" -TokenClientSecret "YOUR_CLIENT_SECRET" -TokenScope "api.read api.write"

Token notes:
- If token options are provided, Authorization is set automatically to Bearer <access_token>.
- Optional grant type override:
	- Python: --token-grant-type
	- PowerShell: -TokenGrantType
- If scope contains {{projectID}}, pass project id with:
	- Python: --token-project-id
	- PowerShell: -TokenProjectId

From your provided cURL (Zitadel) to Python:
python3 send_meter_exceptions.py --events meter_events.txt --endpoint https://example.com/api/meter-exceptions --token-url "https://zitadel.systest2-gridmssceapps.gcsnt.gnscet.com/oauth/v2/token" --token-client-id "YOUR_CLIENT_ID" --token-client-secret "YOUR_CLIENT_SECRET" --token-scope "openid profile email urn:zitadel:iam:org:project:id:{{projectID}}:aud" --token-project-id "YOUR_PROJECT_ID" --token-grant-type client_credentials

From your provided cURL (Zitadel) to PowerShell:
pwsh ./send_meter_exceptions.ps1 -EventsPath ./meter_events.txt -TemplatePath "./Sample MeterException Payload" -Endpoint "https://example.com/api/meter-exceptions" -TokenUrl "https://zitadel.systest2-gridmssceapps.gcsnt.gnscet.com/oauth/v2/token" -TokenClientId "YOUR_CLIENT_ID" -TokenClientSecret "YOUR_CLIENT_SECRET" -TokenScope "openid profile email urn:zitadel:iam:org:project:id:{{projectID}}:aud" -TokenProjectId "YOUR_PROJECT_ID" -TokenGrantType "client_credentials"

Text file format (one event per line):
- meter_id,timestamp,name[,outage_id]
- Name must be either Primary Power Up or Primary Power Down.

Name to ID mapping:
- Primary Power Down -> 18001
- Primary Power Up -> 18002

Notes:
- Use now as timestamp to auto-fill current UTC time in ISO-8601 format for that line.
- Blank lines and lines starting with # are ignored.