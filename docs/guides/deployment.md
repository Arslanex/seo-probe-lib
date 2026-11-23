# Deployment

How to ship an application that uses seoprobe: a Docker image with Chromium, the network policy
the browser features need, configuration, and how to size it.

A term you do not know is probably in the [glossary](../glossary.md).

## A Docker image with Chromium

seoprobe is not on PyPI yet, so the image installs it from its source tree. Put the
`seo-probe-lib` folder next to your Dockerfile (a git submodule or a copy) and start from this:

```dockerfile
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PLAYWRIGHT_BROWSERS_PATH=/ms-playwright

# seoprobe from its source tree, then Chromium with the system libraries and fonts it needs, in
# one layer so the apt lists do not stay in the image.
# libatomic1: Playwright's own Node.js driver needs it before it can install anything else.
COPY seo-probe-lib /opt/seoprobe
RUN apt-get update \
 && apt-get install -y --no-install-recommends libatomic1 \
 && pip install --no-cache-dir "/opt/seoprobe[all]" \
 && python -m playwright install --with-deps chromium \
 && rm -rf /var/lib/apt/lists/*

# Run as an unprivileged user; the browser lives in a shared, readable location.
RUN useradd --create-home --uid 10001 app
USER app
WORKDIR /home/app

COPY --chown=app your_app/ ./your_app/
CMD ["python", "-m", "your_app"]
```

This image was built and run on 24 November 2026 (Docker 29, arm64). As the unprivileged user, it
rendered a page, took desktop and mobile screenshots, measured lab metrics, and refused the cloud
metadata address. It is about 1.4 GB, most of it Chromium and its libraries.

Notes:

- **`libatomic1`** is missing from the `slim` images on arm64. Without it,
  `playwright install --with-deps` fails with `libatomic.so.1: cannot open shared object file`
  before it gets to install anything. It is harmless on amd64.
- **`--with-deps`** installs the libraries and fonts Chromium needs, through apt. Pages in
  Chinese, Japanese or Korean need extra fonts for faithful screenshots (`fonts-noto-cjk`).
- **No sandbox flags are needed.** Playwright starts its headless Chromium in a way that works
  for an unprivileged user in a container.
- **Shared memory.** The image worked with Docker's default 64 MB of `/dev/shm`. If you render
  many pages at once (`browser_concurrency`), give it more: `--shm-size=1g`, or `shm_size: 1gb`
  in Compose.
- **Keep the browser and the driver together.** Each Playwright version expects its own
  Chromium build. Run `playwright install` in the same image build that installs the Python
  package, never in a separate cache.
- **Images that do not need the browser** can drop the extra: `pip install "/opt/seoprobe[dns]"`.
  No Chromium, no apt step, a much smaller image. Browser features then answer `missing-extra`.
  A common split is a light image for the API and the full image for the workers.

## Restrict outgoing network access

**Required for every process that runs the browser features** (`render`, `screenshots`,
`mobile`, `accessibility`, `lab_metrics`, and the audits that use them).

The HTTP client checks every address at connect time, but Chromium resolves names itself. A
hostile DNS server could therefore answer the library's check with a public address and the
browser's own lookup with a private one. The [security model](../architecture/security.md#the-browser)
explains this in full. Close the gap at the network level, so the process cannot reach these
addresses at all:

- private ranges: `10.0.0.0/8`, `172.16.0.0/12`, `192.168.0.0/16`, `fc00::/7`;
- carrier-grade NAT: `100.64.0.0/10`;
- loopback: `127.0.0.0/8`, `::1`;
- link-local, where the cloud metadata services live: `169.254.0.0/16`, `fe80::/10`.

Then allow back the internal services the process really needs (its database, its queue, DNS).

A starting point for **Kubernetes**. It has not been tested here. Adapt the labels, the DNS
selector and the allowed services to your cluster:

```yaml
apiVersion: networking.k8s.io/v1
kind: NetworkPolicy
metadata:
  name: seoprobe-worker-egress
spec:
  podSelector:
    matchLabels: {app: seoprobe-worker}
  policyTypes: [Egress]
  egress:
    - to:                                   # the public internet, minus internal ranges
        - ipBlock:
            cidr: 0.0.0.0/0
            except: [10.0.0.0/8, 172.16.0.0/12, 192.168.0.0/16, 100.64.0.0/10, 169.254.0.0/16, 127.0.0.0/8]
        - ipBlock:
            cidr: ::/0
            except: [fc00::/7, fe80::/10, ::1/128]
    - to:                                   # cluster DNS
        - namespaceSelector: {}
          podSelector:
            matchLabels: {k8s-app: kube-dns}
      ports: [{protocol: UDP, port: 53}, {protocol: TCP, port: 53}]
    - to:                                   # the database and the queue the worker needs
        - podSelector:
            matchLabels: {app: postgres}
        - podSelector:
            matchLabels: {app: redis}
```

**Elsewhere:**

- **On cloud VMs:** use a security group or firewall rule with the same ranges, and require
  the token-protected version of the metadata service (IMDSv2 on AWS, or its equivalent).
- **With plain Docker:** run the browser workers on a dedicated network whose host firewall
  (iptables or nftables) drops traffic to those ranges.

The API process needs this only if it runs browser features itself.

## Configuration

| Variable | Purpose |
|---|---|
| `SEOPROBE_GOOGLE_API_KEY` | PageSpeed Insights and Chrome UX Report |
| `SEOPROBE_OPENPAGERANK_KEY` | domain authority (a Keywords Everywhere key; old DomCop keys are rejected) |
| `SEOPROBE_BING_API_KEY` | Bing Webmaster Tools |
| `SEOPROBE_GOOGLE_CLIENT_ID`, `SEOPROBE_GOOGLE_CLIENT_SECRET` | the OAuth client for Search Console and GA4 |
| `SEOPROBE_LANG` | `en` (default) or `tr` |
| `SEOPROBE_<SETTING>` | any other `Config` field: `SEOPROBE_MAX_PAGES`, `SEOPROBE_LINK_CHECK_BUDGET_S`, `SEOPROBE_REQUEST_DELAY_S`, ... |

They are read only when you call `seoprobe.Config.from_env()`. Load secrets from your secret
manager into the environment. A `.env` file is not read on its own.

**At startup, check what is configured,** so a missing key shows up in your logs, not in a
customer's report:

```python
async with seoprobe.Session(seoprobe.Config.from_env()) as s:
    for op in ("pagespeed", "render", "dns_health"):
        print(op, (await seoprobe.preflight(op, session=s)).missing)        # no network
    print("google key:", (await seoprobe.check_access("google", session=s)).code)  # one harmless request
```

These two are helpers, not operations, so they return their own objects instead of a `Result`.
`preflight(op)` returns what the operation still needs (`missing`). `check_access(provider)`
takes `google`, `openpagerank`, `search_console`, `ga4` or `bing`, and returns a `code` such as
`ok`, `missing` or `not-connected`. An unknown provider name raises `KeyError`.

## Sizing

### What an audit costs

Measured on public sites in November 2026:

| Run | Requests | Time |
|---|---|---|
| one page, no time limits | 100-145 | 35-45 s |
| one page, `link_check_budget_s=5, visibility_budget_s=5` | about 50 | about 18 s |
| a site sample of 6 pages | about 190 | about 65 s |

Most of the time goes to checking links one by one and to waiting politely between requests to
the same site (`request_delay_s`, 0.4 s by default). The settings that change the cost, with
their defaults, are in the README's [cost table](../../README.md#what-an-audit-costs). The time
budgets cap the slow, optional parts, and
[ADR 0008](../architecture/adr/0008-dynamic-time-budgets.md) explains why they do not change the
score.

### Workers

- **An audit is mostly waiting on the network.** One worker process can run several audits at
  once (async), and CPU is rarely the limit. The browser is: each rendered page is a Chromium
  tab. `browser_concurrency` (default 2) caps tabs per process. Plan about 200-300 MB of memory
  per tab.
- **Politeness limits are kept per process.** With N worker processes auditing the same site,
  that site can receive N × `per_host_concurrency` parallel requests. The limits are per target
  site, not per customer: two customers auditing the same competitor share it. Route jobs by
  target host to one worker, or lower `per_host_concurrency`, if you audit small sites.
- **Set the job runner's time limit above the `timeout_s`** you give the audit. seoprobe then
  always finishes and reports before the runner kills the job.

### Logging

Loggers are under `seoprobe.*` and silent until you configure them. They log at three levels:

- **WARNING:** something was skipped that you may want to know about, such as a failing event
  hook or an audit module that failed.
- **ERROR:** an unexpected exception inside an operation, the one that became `internal-error`,
  with its traceback.
- **DEBUG:** one line per operation, plus the details that are kept out of results on purpose
  (exception texts, resolved addresses). Keep DEBUG out of logs that customers or support staff
  can read.

## Upgrading

- **Rebuild the image.** That reinstalls Chromium for the new Playwright version.
- **Re-run the examples against a few real sites** before rolling out
  (`python examples/09_audit_and_report.py https://your-site/`).
- **Check `score_version` and `rule_version` in the changelog.** When they change, scores from
  before and after are not directly comparable, and `report.compare` warns about it.
