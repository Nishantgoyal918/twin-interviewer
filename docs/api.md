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
| GET | `/twins/{id}/sessions/{sid}/recording` | whole-call recording as signed URLs (6 h): `source` = `call` (parts), `simulator` (one file; `swap` for old runs with the voices on the other ears) or `null` |
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
| POST | `/uploads/sign` | `{seq, kind}` → signed PUT URL; `kind` = `segment` (mic, for Deepgram) or `call` (whole call, stereo) |
| POST | `/uploads/complete` | record an upload (`seq, kind, path, start_ms, end_ms, bytes`) |

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

## Simulator (`/api/dev`, dev only)

Mounted only when `DEV_AUTH=1` (local) or `SIM_KEY` is set (production). With `SIM_KEY`,
every route needs it as the `x-sim-key` header or `?key=`; a wrong key gets 404. See
[simulator.md](simulator.md).

| Method | Path | What |
|---|---|---|
| GET | `/personas` | persona cards |
| POST | `/sim/setup` | create a simulated twin from the persona's brief, approve it, make an invite |
| POST | `/persona/connect` | `{sdp}` → GPT-Live answer for the persona's own session |
| POST | `/persona/react` | judge one interviewer turn as the persona → new energy, mood and the mood cue |
| GET | `/sim/twins/{twin_id}/latest-session` | the session the voice page just ran |
| POST / GET | `/sim/runs` | save a run's transcript and stats / list saved runs |
| POST | `/sim/runs/{sid}/recording-upload` | signed PUT URL for the stereo recording (production) |
| PUT / GET | `/sim/runs/{sid}/recording` | upload (local) / play the recording |
| GET | `/sim/runs/{sid}/scorecard` | the Markdown scorecard from `scripts/sim_score.py` |
