# Unraid Inventory

Last synced: 2026-11-11

## Host
- Hostname: girthy
- IP: 192.168.40.69
- Unraid version: 7.2.5
- CPU: Intel i7-8700K
- SSH: `ssh unraid` (alias in `~/.ssh/config`, key `~/.ssh/id_ed25519_unraid`)

## Array
- Parity: 1x (parity2 slot unpopulated)
- Data disks: disk1-disk5, all DISK_OK
- Cache: nvme0n1, DISK_OK
- Last parity check: 2026-08-02, 0 sync errors

## Shares
- Cache
- SHARE
- appdata
- data
- domains
- isos
- system

## Docker containers
| Name | Image | Status |
|---|---|---|
| binhex-plex | binhex/arch-plex | running |
| binhex-radarr | binhex/arch-radarr | running |
| binhex-sonarr | binhex/arch-sonarr | running |
| binhex-sabnzbd | binhex/arch-sabnzbd | running |
| binhex-seerr | ghcr.io/binhex/arch-seerr | running |
| cloudflared | cloudflare/cloudflared:2026.10.1 | running |
| filebrowser | filebrowser/filebrowser | running |
| PhotoStructure | photostructure/server:stable | running |
| speedtest-tracker | henrywhitaker3/speedtest-tracker | running |
| girthy-dash | girthy-dash:1.0 (built locally, see `unraid/docker/girthy-dash`) | running |

## Remote access (Cloudflare Tunnel `girthy`)
Token in `/mnt/user/appdata/cloudflared/token`. Admin apps are behind a Cloudflare Access email policy.

| Hostname | Service | Access login |
|---|---|---|
| sonarr.girthy.org | :8989 | yes |
| radarr.girthy.org | :7878 | yes |
| downloads.girthy.org | SABnzbd :8060 | yes |
| speedtest.girthy.org | :2952 (container currently on :9091) | yes |
| photos.girthy.org | PhotoStructure | yes |
| request.girthy.org | Seerr :5055 | no (Plex sign-in) |
| monitor.girthy.org | girthy-dash :7879 | **not yet** |
