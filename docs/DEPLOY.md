# Deploying Thaqip to production

Target: Oracle Cloud Always Free Arm VM (2 OCPU / 12 GB) in Saudi Arabia
Central (Riyadh) or West (Jeddah), fronted by Cloudflare Free (Tunnel + Access).
The VM opens **no inbound port except SSH**. Visitors reach the app through
Cloudflare; the invite list is a Cloudflare Access policy.

Everything a person must click is in part A. Everything else is one script
(part B), which was rehearsed locally against the production compose file on
14 Sep 2026: fresh volume, restore, migrate, console behind proxy headers,
persistence across recreate.

---

## A. Owner steps (accounts, money, identity)

### A1. Cloudflare (about 15 min)
1. Create a Cloudflare account with the company email and turn on 2FA.
2. **Domain → Register** `thaqip.app` (Cloudflare Registrar, at cost). Turn on
   registrar lock.
3. **Zero Trust** (first visit asks for a team name and the Free plan) →
   **Networks → Tunnels → Create a tunnel** → type *Cloudflared* → name
   `thaqip-prod` → on the install screen choose *Docker* and copy only the
   **token** (the long string after `--token`). Do not run the command shown;
   `bin/prod-secrets.sh` asks for this token.
4. Still in the tunnel, **Public hostnames**, add two:
   | Subdomain | Domain | Service |
   |---|---|---|
   | `app` | thaqip.app | `http://console:8080` |
   | `analytics` | thaqip.app | `http://superset:8088` |
5. **Access → Applications → Add → Self-hosted**:
   - `Thaqip` → `app.thaqip.app` → policy *Allow*, include **Emails** = the
     invite list (up to 50 on Free). Session duration 24h.
   - `Thaqip Analytics` → `analytics.thaqip.app` → policy *Allow*, include
     only the admin emails.
   - Login method: One-time PIN (email).
6. **SSL/TLS → Edge Certificates**: Always Use HTTPS on, minimum TLS 1.2,
   HSTS on.

### A2. Oracle Cloud (about 20 min, may need retries)
1. Sign up at oracle.com/cloud/free. **Home region: Saudi Arabia Central
   (Riyadh)**. It cannot be changed later.
2. Recommended: upgrade the account to *Pay As You Go* (card required, still
   $0 within Always Free limits). This avoids idle-instance reclamation and
   most "out of capacity" errors. Then **Billing → Budgets**: alert at $1.
3. **Compute → Instances → Create**:
   - Image: Canonical Ubuntu 24.04 (aarch64)
   - Shape: `VM.Standard.A1.Flex`, 2 OCPU, 12 GB
   - Boot volume: 150 GB
   - Networking: new VCN with a public subnet; public IPv4 yes
   - SSH key: paste your public key
   - If it says *out of host capacity*: try another availability domain,
     retry later, or upgrade to PAYG (step 2).
4. Security list of the subnet: ingress **only TCP 22** (the default).
   Do not add 80/443; Cloudflare Tunnel is outbound.
5. Send the engineer the instance's public IP. Give SSH access by adding their
   public key to `~/.ssh/authorized_keys`; never share a password.

### A3. GitHub deploy key (5 min)
The repo is private (`Azizsa8/thaqip`). On the VM the engineer runs
`ssh-keygen -t ed25519 -f ~/.ssh/thaqip_deploy -N ""` and sends you the
`.pub`. Add it at **repo → Settings → Deploy keys** with *write access off*.

---

## B. Engineer steps (on the VM)

```bash
# 1. clone with the read-only deploy key
GIT_SSH_COMMAND='ssh -i ~/.ssh/thaqip_deploy' git clone git@github.com:Azizsa8/thaqip.git
cd thaqip

# 2. copy the newest laptop backup over (run on the laptop)
#    scp -r ~/thaqip/var/backups/<newest> ubuntu@<vm-ip>:~/thaqip-backup

# 3. provision (asks for the Cloudflare tunnel token once)
bin/provision-vm.sh ~/thaqip-backup
```

`bin/provision-vm.sh` does, in order: Docker + compose, ufw (SSH only),
timezone Asia/Riyadh, uv, production secrets (`.env`), image builds, Postgres,
restore (roles without the old superuser password, data, then
`db/post-restore.sql` re-applies database-level lockdown), migrations, new
console admin password + service token, Chromium for the Etimad scrapers, the
stack (Superset last, after its secrets exist), boot service, cron, first
backup and restore drill.

**Stop ingestion on the laptop once the VM is scraping**, so two machines do
not write to Etimad at once:
```bash
crontab -l | grep -v thaqip/bin | crontab -    # on the laptop
docker compose stop poller relay alerts indexer pricing-seed prediction-clock ops-health
```

---

## C. Day 1 gate (all must pass)

- [ ] `https://app.thaqip.app` asks for the Cloudflare one-time PIN, then shows the Thaqip login
- [ ] SSL Labs grade A for app.thaqip.app
- [ ] From outside: `nmap -Pn <vm-ip>` shows only 22 open
- [ ] `bin/restore-drill.sh` passes on the VM
- [ ] Test suite green against the VM (tunnel the console to your machine:
      `ssh -L 8091:127.0.0.1:8091 ubuntu@<vm-ip>` after temporarily exposing
      it on loopback, or run the suite on the VM itself)
- [ ] `sudo reboot` → within 5 minutes `docker compose ps` is all healthy with no manual step

## D. Operations

| Task | Command |
|---|---|
| Status | `docker compose ps` |
| Logs | `docker compose logs -f --tail 100 console` |
| Update to latest code | `git pull && docker compose up -d --build --wait && bin/migrate.sh` |
| Backup now | `bin/backup.sh` |
| Prove a backup restores | `bin/restore-drill.sh` |
| Off-machine backups | `rclone config` (e.g. Oracle Object Storage, S3-compatible), then set `BACKUP_RCLONE_REMOTE=remote:bucket` in `.env` |
| New admin password | `rm var/credentials.env && bin/bootstrap-auth.sh` |
| Telegram bot | `bin/set-telegram-token.sh` |

Production refuses to start if any secret is missing from `.env`
(`POSTGRES_PASSWORD`, `MINIO_ROOT_USER`, `MINIO_ROOT_PASSWORD`,
`TYPESENSE_API_KEY`, `CLOUDFLARE_TUNNEL_TOKEN`); that is deliberate.
