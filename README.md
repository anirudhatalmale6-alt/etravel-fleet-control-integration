# Fleet Control Pack 1.0.0 - proposed integration

Nothing here has been applied. This is the unified diff that
`PATCH-INSTRUCTIONS.md` asked for, plus the two files it needs to work.

Server: 65.21.44.213. Date: 5 September 2026.

## What is in this folder

| file | what it is |
|---|---|
| `01-controller-node-v1-sites.diff` | one new read-only route on the AI Ops controller |
| `02-agent-py-fleet-tools.diff` | the agent.py patch: four tools + the single-site guard |
| `ai1_fleet_tools.py` | corrected replacement for the pack's `server/ai1_fleet_tools.py` |
| `03-etravel-ai-next-fleet.env` | the systemd EnvironmentFile the secrets must arrive through |

## Why the shipped adapter was replaced rather than dropped in

The pack's own instructions say: *"If any expected boundary differs, stop and
produce a proposed diff rather than applying it."* Five boundaries differ.

1. **No authentication.** `fleet_status()` did an unauthenticated GET to
   `127.0.0.1:8787/fleet/status`. That route is behind the controller's browser
   login session and returns **401**. The replacement uses the `/node/v1/`
   HMAC protocol that `app/fleet.py verify_incoming_request()` already
   implements.

2. **`/fleet/status` is not a site list.** `app/fleet.py all_status()` returns
   `{"nodes": {...}}`. The shipped `fleet_list_sites()` looked for `"sites"` or
   `"targets"`, found neither, and **returned an empty list without raising**.
   Asked for the 27 eShops sites it would answer "0" — confidently, and
   wrongly. In the replacement a missing inventory is an error, never an empty
   answer.

3. **Nothing on the controller returned the 126 sites.** Hence patch 01.
   `/etc/etravel-ai-ops/sites.yml` stays the single source of truth.

4. **Tool name collision.** `request_production_action` already exists in the
   live `agent.py` (the Navigator approval tool) with a different schema. The
   replacement exports `fleet_request_production_action`.

5. **Cloudflare, and then a User-Agent rule.** The pack's Navigator URL is the
   public hostname, which from this server hits Cloudflare's bot challenge.
   Going to the origin is necessary but not sufficient — measured at the
   origin on 5 September:

   | request | result |
   |---|---|
   | default curl User-Agent | 401 `rest_forbidden` (WordPress answered) |
   | empty User-Agent | 403 `Access denied.` (blocked before WordPress) |
   | `Python-urllib/3.11` | 403 `Access denied.` (blocked before WordPress) |
   | `eTravel-AI-Fleet/1.0 (+https://ai1.etravel.gr)` | **200, real results** |

   A 403 here is not an authentication result. Nobody should "fix" it by
   weakening the signature.

## What is already verified, live and read-only

Run against the real server with the real secrets, before this was written up:

```
fleet_status()      OK   node primary, role primary, 126 sites,
                         families {etravel 11, eshops 27, unassigned 85,
                                   collector 1, fotovoltaika_hub 1,
                                   solar_panels_hub 1}
fleet_list_sites()  correctly RAISED FleetAdapterError
                    ("/node/v1/sites returned HTTP 404")
                    - the route does not exist until patch 01 is applied,
                      and the adapter refuses to call that zero sites
navigator_search()  OK   3 real results, each carrying site_family and server_id
```

## The single-site guard in patch 02

`agent.py` has a keyword fast path that answers from `_tool_extended_health()`,
which is pinned to one WordPress install — `read_only_bridge.py` line 6,
`WP_ROOT = /var/www/vhosts/etravel.gr/httpdocs`.

On 4 September *"Check all eShops sites for PHP version and WordPress version"*
was answered by that shortcut with **etravel.gr's** PHP 8.4.25 and WordPress
7.0.4. Well-formatted, accurate, and about the wrong subject. The model never
entered the tool loop, so it had no way to notice.

A malformed reply gets reported as a bug in a day. A well-formed reply about
the wrong subject is believed.

Patch 02 does not delete the shortcut — `PATCH-INSTRUCTIONS.md` allows
restricting it, and it is genuinely useful for a single-site question. It adds
`multi_site_request`: anything mentioning *all, every, each, eshops, sister,
family, fleet, inventory, compare, failover, collector, staging, across* goes
to the tool loop, where it can look.

## Order of application

```
1  cp -p /opt/etravel-ai-ops/app/main.py /opt/etravel-ai-ops/app/main.py.bak-$(date +%Y%m%d-%H%M%S)
   patch -p1 < 01-controller-node-v1-sites.diff
   /opt/etravel-ai-ops/venv/bin/python -m py_compile /opt/etravel-ai-ops/app/main.py
   systemctl restart etravel-ai-ops.service
   curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:8787/node/v1/sites   # expect 403

2  install -o root -g root -m 0600 03-etravel-ai-next-fleet.env /etc/etravel-ai-next/fleet.env
   fill in the two secrets by hand, on the server
   add the systemd drop-in described inside that file

3  install -o root -g root -m 0644 ai1_fleet_tools.py /opt/etravel-ai-next/ai1_fleet_tools.py

4  cp -p /opt/etravel-ai-next/agent.py /opt/etravel-ai-next/agent.py.bak-fleet-$(date +%Y%m%d-%H%M%S)
   patch -p1 < 02-agent-py-fleet-tools.diff
   /opt/etravel-ai-next/.venv/bin/python -m py_compile /opt/etravel-ai-next/agent.py
   systemctl restart etravel-ai-next.service
```

Rollback at any step: restore the `.bak` file and restart that one service. The
two services are independent; step 1 alone changes nothing about the agent, and
steps 2 to 4 alone leave the controller untouched.

## Acceptance tests this makes possible

Tests 1 to 8 of `ACCEPTANCE-TESTS.md` become answerable. Two of them will still
report honestly rather than pass:

- **Test 8**, separate checks of primary / failover / collector-source /
  collector-new: `sites.yml` marks **every one of the 126 sites `node: primary`**,
  so a per-node site breakdown returns 126 / 0 / 0 / 0. That is the inventory
  telling the truth about itself, not a bug in this patch.
- **Test 16**, `auto_execute_production` false: it is false, in all three
  places that record it. But `production_writes_enabled` in
  `/etc/etravel-ai-ops/rollout.yml` is **true**, not false. Unchanged here;
  flipping it is the owner's decision.
