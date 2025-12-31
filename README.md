# PQRS

*Est. 2023*

---

## Introduction

This is a monorepo containing the codebases for several applications running on my server "PQRS".

I am currently still in the process of uploading this code, so parts of it may be missing.

PQRS is a server which has the capacity for up to four nodes, which are designated as follows:
- Plasma
- [Quark](#quark)
- Relativity
- Singularity

All will share common network and data transmission/backup infrastructure. At the moment, only Quark is online and operational.

## Quark
- A Dell Optiplex Micro with a 6-core i5-8500T and 16GB of DDR4.
- Has two pools of storage:
	- `/dev/nvme0n1`: a 512GB NVMe SSD which acts as the system's boot drive and stores the boot drives for most VMs.
	- `/dev/sda`: a 4TB SATA SSD which stores a ZFS pool "archive".
- Has been in use as a server since the project's inception in August 2023.

### Directory of Applications
- All VMs with a VMID starting with 1 are LXCs.
- All VMs with a VMID starting with 2 are QEMU-based VMs.

#### [100-109]: Network Infrastructure
- [100] Tailscale: Allows remote access to the PQRS Network without compromising security by port forwarding.
- [101] Mailserv: Allows other local applications to send mail to myself with a REST API.
- [102] DNS: Provides a DNS server for PQRS which resolves subdomains of leoapagano.com.
- [103] [UPS Statistics](./ups-stats/README.md): Monitors the UPS which PQRS is plugged into.

#### [110-129]: Storage Infrastructure & Backups
- [110] Restic: Automatically backs up data created by other applications to the cloud.
- [111] Bitwarden Backups: Automatically backs up my Bitwarden vault to a GPG-encrypted CSV file.
- [112] IMAP Backups: Automatically backs up 
- [113] Git Backups: Automatically clones my Git repos, public and private.

#### [130-139]: Development Environments
- Created and destroyed as needed on an ad-hoc basis.

#### [140-149]: Media Services
- TBA

#### [150-199]: Miscellaneous Services
- TBA

#### [200-255]: QEMU VMs
- TBA