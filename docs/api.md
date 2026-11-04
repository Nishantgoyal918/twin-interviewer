# API

All endpoints are under `/api`. There are three kinds of caller:

- **Creator:** Supabase login, or the `X-Dev-Creator` header locally.
- **Subject:** a session token from `POST /api/sessions`, sent as `Authorization: Bearer …`,
  or as `?st=` for beacons.
- **Machines:** QStash signature, webhook secret, or cron secret.

## Creator

| Method | Path | What |
|---|---|---|
| GET | `/public-config` | Supabase URL + anon key for the login page, dev mode flag |
| GET / POST | `/twins` | list / create from a free-text brief (compiles a spec draft) |
| GET / PUT | `/twins/{id}/spec` | latest spec + versions / save a new version |
| POST | `/twins/{id}/spec/approve` | approve latest; twin becomes `active` |
| POST / GET | `/twins/{id}/invites` | create (link shown once) / list |
| POST | `/twins/{id}/invites/{iid}/revoke` | revoke |
| GET | `/twins/{id}/coverage` | slots, registers, interests, never-raise, notes |
| GET | `/twins/{id}/sessions` | session list |
| GET | `/twins/{id}/sessions/{sid}/debug` | turns with tags, decisions, talk share |
| POST | `/labels` | human label on a turn (for signal validation) |
| POST | `/twins/{id}/imports` | start a WhatsApp/plain-text import → signed upload URL |
| POST | `/twins/{id}/imports/{iid}/complete` | parse + embed it |
| POST | `/twins/{id}/ask` | ask the twin a question |

## Subject

| Method | Path | What |
|---|---|---|
| GET | `/invites/{token}` | name, audience, modalities (for the consent page) |
| POST | `/sessions` | `{invite_token, modality}` → session token, history, limits (resumes open text sessions) |
| GET | `/sessions/{id}/history` | turns so far |
| POST | `/sessions/{id}/end` | end; `expected_segments` for voice; starts the job chain |
| POST | `/voice/connect` | `{sdp}` → GPT-Live answer SDP (server holds the key) |
| POST | `/turn` | fast lane: `{client_turn_id, subject, interviewer?, features, messages}` |
| POST | `/turn/{id}/analyze` | slow lane: returns guidance, engagement, policy |
| POST | `/delegation` | GPT-Live asked for help → recall + tip |
| POST | `/uploads/sign` | `{seq}` → signed PUT URL for an audio segment |
| POST | `/uploads/complete` | record a segment (`seq, path, start_ms, end_ms, bytes`) |

Errors:

- **409:** "Still working on the previous message…". The browser retries.
- **429:** a cap was hit.
- **503:** a provider key is missing.

## Machine

| Method | Path | Auth |
|---|---|---|
| GET | `/health` | none |
| POST | `/jobs/{kind}` | QStash signature (inline mode: unused) |
| POST | `/webhooks/deepgram?asset=&secret=` | webhook secret |
| GET | `/cron/daily` | `Bearer CRON_SECRET` |
| PUT/GET | `/dev/storage/{path}` | HMAC-signed URL, local storage only |
