# Personal Cloud Host User Guide

This guide covers the local-network release for Quectel Pi M2 running Debian 13.

> The default service uses plain HTTP and is intended for a trusted LAN only. Do not expose it directly to the Internet or forward its service ports.

## 1. Before you install

- Connect the board to a network and make sure it can access package repositories.
- Have a user account with `sudo` privileges.
- Connect the phone or computer that will use the portal to the same local network.
- If you need SSH, configure and test a public key first. The installer only disables SSH password authentication when an authorized key is present for the service user.

## 2. Install and open the portal

Clone the repository on the board:

```bash
git clone https://github.com/Quectel-Pi/demo-personal-cloudhost.git
cd demo-personal-cloudhost
sudo ./install.sh
```

Follow the prompt to set the initial portal password. The installer installs and starts the required services. Use `./install.sh --help` to review optional arguments, including `--no-ttyd` to skip the browser terminal and `--no-ssh-harden` to leave SSH configuration unchanged.

Find the board's LAN address:

```bash
hostname -I
```

Open `http://<board-ip>/` in a browser on the same LAN. With PAM authentication available, sign in with the configured service-user account and its system password. If the installer uses password-hash authentication instead, sign in as `admin` with the password supplied during installation.

## 3. Use the portal

- **Overview:** view system resource and network information.
- **Files:** browse the configured file root, upload and download files, and manage directories. The default file root is the service user's home directory.
- **Site:** publish static files from `~/cloud/www`; open `/site/` to view the site.
- **Terminal:** use the browser-based local shell if it was installed. This is not an SSH client.
- **SSH:** use the command shown in the portal to connect from a terminal on your computer. SSH must be configured and reachable on the LAN.

Keep private keys and passwords out of shared folders and public sites. Do not place secrets in the static-site directory.

## 4. SSH access

Generate a key on the client computer if you do not already have one:

```bash
ssh-keygen -t ed25519
```

Install the public key for the board's service user using your usual trusted provisioning method, then verify the connection on the local network:

```bash
ssh -i ~/.ssh/id_ed25519 <user>@<board-ip>
```

Confirm the host-key fingerprint through a trusted channel before accepting it. The installer does not enable password-based SSH hardening if it cannot find an authorized key, to avoid locking out the user.

## 5. Maintenance

The installer supports these common operations:

```bash
sudo ./install.sh --help
sudo ./install.sh --uninstall
```

Uninstall removes the installed application and services but retains user data under `~/cloud`. Back up important files separately before system maintenance or storage changes.

For service status and logs, use standard systemd tools, for example:

```bash
systemctl status caddy cloudhost-portal
journalctl -u cloudhost-portal
```

## 6. Troubleshooting

| Symptom | Check |
|---|---|
| Portal does not open | Confirm the client and board are on the same LAN, check the board IP with `hostname -I`, and verify `caddy` and `cloudhost-portal` are active. |
| Sign-in is rejected | Confirm whether PAM system-account authentication or password-hash fallback was selected; use the corresponding account and password. |
| SSH connection is refused | Check that the SSH service is active, the board is reachable, and the client is using the correct username, key, address, and port. |
| Browser terminal is missing | It may have been skipped with `--no-ttyd`; reinstall with the terminal enabled if it is required. |
| Files are not visible where expected | The default file root is the service user's home directory; the static site directory is `~/cloud/www`. |

Do not solve access problems by exposing the board or its internal services to the public Internet.
