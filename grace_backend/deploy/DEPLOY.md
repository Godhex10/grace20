# Deploying Grace to the Oracle VM

Run these in order. Anything in `<angle brackets>` you replace with your value.
`<IP>` = the VM's public IP. Default Ubuntu user = `ubuntu`.

---

## 1. SSH in (from your Windows machine)
```powershell
ssh -i "C:\Users\ugopr\.ssh\grace_oracle.key" ubuntu@<IP>
```
If it complains the key is "unprotected", run this once, then retry the ssh:
```powershell
icacls "C:\Users\ugopr\.ssh\grace_oracle.key" /inheritance:r /grant:r "%USERNAME%:R"
```

## 2. Install the system packages (on the VM)
```bash
sudo apt update && sudo apt -y upgrade
sudo apt -y install python3 python3-venv python3-pip git
# Caddy (reverse proxy + auto HTTPS)
sudo apt -y install debian-keyring debian-archive-keyring apt-transport-https curl
curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/gpg.key' | sudo gpg --dearmor -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt' | sudo tee /etc/apt/sources.list.d/caddy-stable.list
sudo apt update && sudo apt -y install caddy
```

## 3. Open the VM's OWN firewall (the Oracle-Ubuntu gotcha)
Oracle's Ubuntu image blocks ports in local iptables even after you opened them in the cloud Security List:
```bash
sudo iptables -I INPUT 6 -m state --state NEW -p tcp --dport 80 -j ACCEPT
sudo iptables -I INPUT 6 -m state --state NEW -p tcp --dport 443 -j ACCEPT
sudo netfilter-persistent save
```

## 4. Get Grace's code onto the VM
**Option A — git (recommended, makes future updates a `git pull`):**
```bash
cd ~
git clone <your-private-repo-url> gracev2-main
```
**Option B — copy from your PC** (run this in PowerShell on Windows, not the VM).
First zip the project locally (exclude `.venv`), then:
```powershell
scp -i "C:\Users\ugopr\.ssh\grace_oracle.key" grace.zip ubuntu@<IP>:~
```
then on the VM: `unzip grace.zip -d gracev2-main`

## 5. Python venv + dependencies
```bash
cd ~/gracev2-main
python3 -m venv .venv
./.venv/bin/pip install --upgrade pip
./.venv/bin/pip install -r grace_backend/requirements.txt
```

## 6. Create the .env (on the VM)
```bash
nano ~/gracev2-main/grace_backend/.env
```
Paste your keys, and this time **include** these two lines (uncommented):
```
GEMINI_API_KEY=...
TAVILY_API_KEY=...
TOMTOM_API_KEY=...
SHODAN_API_KEY=...
GRACE_MODEL=...
GRACE_CODE_MODEL=...

# Use Supabase now that backend + DB are both in the EU:
DATABASE_URL=postgresql://postgres.hirhqjdqopkdqenlfwgq:<db-password>@aws-1-eu-west-1.pooler.supabase.com:5432/postgres

# Allow the hosted frontend to call the API (set once you have the domain):
ALLOWED_ORIGINS=https://your-domain.duckdns.org
```
Save: `Ctrl+O`, `Enter`, `Ctrl+X`.

## 7. Run Grace as an always-on service (systemd)
```bash
sudo cp ~/gracev2-main/grace_backend/deploy/grace.service /etc/systemd/system/grace.service
sudo systemctl daemon-reload
sudo systemctl enable --now grace
sudo systemctl status grace --no-pager      # should say "active (running)"
curl -s http://127.0.0.1:8000/api/health     # should print an operational status
```
Logs if needed: `journalctl -u grace -f`

## 8. A free domain (DuckDNS) so Caddy can do HTTPS
1. Go to duckdns.org, sign in, create a subdomain (e.g. `grace-ugo`).
2. Set its IP to your VM's `<IP>` and Save.
3. Your domain is now `grace-ugo.duckdns.org`.

## 9. Caddy reverse proxy (auto-HTTPS + SSE)
```bash
sudo cp ~/gracev2-main/grace_backend/deploy/Caddyfile /etc/caddy/Caddyfile
sudo nano /etc/caddy/Caddyfile      # change your-domain.duckdns.org to YOUR subdomain
sudo systemctl restart caddy
sudo systemctl status caddy --no-pager
```
Now visit **https://grace-ugo.duckdns.org/api/health** in your browser — you should see the operational status over HTTPS. 🎉

## 10. Point the frontend at it
In `index.html`, set the API base to `https://grace-ugo.duckdns.org` (there's a `GRACE_API_BASE` / `API_BASE` in the JS), and make sure `ALLOWED_ORIGINS` in the `.env` matches where the frontend is served from. Restart if you changed `.env`: `sudo systemctl restart grace`.

---

## Updating Grace later
With git: `cd ~/gracev2-main && git pull && sudo systemctl restart grace`
(That's the whole "deploy an update" flow we talked about.)
