# cape-host.pkr.hcl — Phase 1 (warm) AMI bake.
#
# Produces a `cape-host-warm-<timestamp>` AMI on top of a stock Ubuntu
# 24.04 base. The bake installs cape-core/cape-signatures/cape-qemu/
# cape-suricata from the prod apt channel and stages the win11_seabios
# qcow2 template under /var/lib/libvirt/images/.
#
# This is *not* the final AMI consumed by terraform/nestedvirt-ami. The
# clone+snapshot phase (boot 24 linked clones, 15-min warmup, snapshot1
# capture, create-image with the mandatory tagging policy) requires
# CpuOptions.NestedVirtualization=enabled at RunInstances time, which
# the Packer amazon-ebs builder does not expose. Phase 2 lives in
# .github/workflows/ami-bake.yml and uses the AWS CLI directly.
#
# Trigger: ami-bake.yml after a successful cape-deb-promote-to-prod.
#
# Design rationale: see docs/superpowers/specs/2026-05-06-cape-core-deb-design.md
# § "AMI Bake Automation" and the spec's two-phase implementation note.

packer {
  required_version = ">= 1.10.0"
  required_plugins {
    amazon = {
      version = ">= 1.3.0"
      source  = "github.com/hashicorp/amazon"
    }
  }
}

# ---------------------------------------------------------------------------
# Variables — all of these come from ami-bake.yml's environment.
# ---------------------------------------------------------------------------

variable "region" {
  type    = string
  default = "us-east-1"
}

variable "subnet_id" {
  type        = string
  description = "Subnet to launch the build instance in. Needs internet egress to reach the prod apt repo."
}

variable "vpc_id" {
  type        = string
  description = "VPC of the subnet (required for security group)."
}

variable "instance_type" {
  type    = string
  default = "m8i.8xlarge"
}

variable "ssh_username" {
  type    = string
  default = "ubuntu"
}

# ---- apt repo wiring ------------------------------------------------------

variable "apt_repo_url" {
  type        = string
  description = "Prod apt channel CDN URL (e.g., https://apt.example.com)."
}

variable "apt_keyring_url" {
  type        = string
  description = "Public ASCII-armored signing key URL (typically <apt_repo_url>/keyring.asc)."
}

# ---- version pinning ------------------------------------------------------

variable "cape_core_version"       { type = string }
variable "cape_signatures_version" { type = string }
variable "cape_qemu_version"       { type = string }
variable "cape_suricata_version"   { type = string }

# ---- qcow2 staging --------------------------------------------------------

variable "qcow2_s3_bucket" {
  type        = string
  description = "S3 bucket holding the win11_seabios.qcow2 template."
}

variable "qcow2_s3_key" {
  type    = string
  default = "images/win11_seabios.qcow2"
}

variable "base_vm_name" {
  type    = string
  default = "win11_seabios"
}

# ---- ami metadata ---------------------------------------------------------

variable "build_run_id" {
  type        = string
  description = "GitHub Actions run id (used in ami_name + tags for traceability)."
  default     = "local"
}

variable "instance_profile" {
  type        = string
  description = "IAM instance profile NAME (not ARN) attached to the Packer build host. Needs s3:GetObject on qcow2_s3_bucket so 10-stage-qcow2.sh can fetch the template."
  default     = ""
}

# ---------------------------------------------------------------------------
# Source — stock Ubuntu Noble 24.04 amd64 from Canonical (owner 099720109477).
# ---------------------------------------------------------------------------

source "amazon-ebs" "warm" {
  region        = var.region
  instance_type = var.instance_type
  subnet_id     = var.subnet_id
  vpc_id        = var.vpc_id

  source_ami_filter {
    filters = {
      name                = "ubuntu/images/hvm-ssd-gp3/ubuntu-noble-24.04-amd64-server-*"
      architecture        = "x86_64"
      "root-device-type"  = "ebs"
      "virtualization-type" = "hvm"
      state               = "available"
    }
    owners      = ["099720109477"]
    most_recent = true
  }

  ssh_username = var.ssh_username

  # Larger root volume — qcow2 template is ~30 GB and the apt install
  # leaves /opt/CAPEv2/.venv at ~1 GB. Phase 2 grows the volume further
  # to fit 24 linked clones + their snapshot1 deltas.
  launch_block_device_mappings {
    device_name           = "/dev/sda1"
    volume_type           = "gp3"
    volume_size           = 200
    iops                  = 6000
    throughput            = 400
    delete_on_termination = true
  }

  # AMI names must match `[A-Za-z0-9 .,/()@_'\[\]\-]+`. cape_core_version
  # often carries `~` (Debian's pre-release marker, e.g. `0.1.0~unreleased`)
  # and `+` (the +cape.N suffix) — both rejected by AWS. Sanitize.
  ami_name = "cape-host-warm-${replace(replace(var.cape_core_version, "~", "-"), "+", ".")}-${var.build_run_id}-{{timestamp}}"
  # AMI descriptions must be ASCII-only — AWS rejects non-ASCII chars
  # (em-dashes etc.) with `Character sets beyond ASCII are not supported`.
  ami_description = "CAPEv2 sandbox host (Phase 1 warm) - cape-core ${var.cape_core_version}, signatures ${var.cape_signatures_version}, qemu ${var.cape_qemu_version}, suricata ${var.cape_suricata_version}. NOT a final AMI; consumed by ami-bake Phase 2 to produce cape-host-base."

  # Build-time tags on the *running build instance* and its volumes —
  # makes orphan instances easy to find if Packer crashes.
  run_tags = {
    Owner         = "capev2"
    Purpose       = "cape-host-warm-bake"
    BuildRunId    = var.build_run_id
    Phase         = "1-warm"
  }
  run_volume_tags = {
    Owner      = "capev2"
    Purpose    = "cape-host-warm-bake"
    BuildRunId = var.build_run_id
  }

  # Final AMI tags. Phase 2 inherits + extends these with the mandatory
  # `cape-host-base` policy (CapeCoreVersion, BakeDate, Retention, etc.).
  tags = {
    Owner                  = "capev2"
    Purpose                = "cape-host-warm"
    Phase                  = "1-warm"
    CapeCoreVersion        = var.cape_core_version
    CapeSignaturesVersion  = var.cape_signatures_version
    CapeQemuVersion        = var.cape_qemu_version
    CapeSuricataVersion    = var.cape_suricata_version
    BuildRunId             = var.build_run_id
    Retention              = "keep"
  }
  snapshot_tags = {
    Owner       = "capev2"
    Purpose     = "cape-host-warm"
    Phase       = "1-warm"
    BuildRunId  = var.build_run_id
  }

  # IAM instance profile is set by ami-bake.yml via the Terraform-managed
  # bake_instance role (s3:Get on qcow2 bucket, ssm:* for tunneling).
  # Empty fallback supported for ad-hoc local Packer runs that don't
  # need S3 access (e.g., dry-run builds).
  iam_instance_profile = var.instance_profile
}

# ---------------------------------------------------------------------------
# Build pipeline.
# ---------------------------------------------------------------------------

build {
  name    = "cape-host-warm"
  sources = ["source.amazon-ebs.warm"]

  # 00 — Add apt repo, import GPG key, install pinned cape-* debs.
  provisioner "shell" {
    environment_vars = [
      "APT_REPO_URL=${var.apt_repo_url}",
      "APT_KEYRING_URL=${var.apt_keyring_url}",
      "CAPE_CORE_VERSION=${var.cape_core_version}",
      "CAPE_SIGNATURES_VERSION=${var.cape_signatures_version}",
      "CAPE_QEMU_VERSION=${var.cape_qemu_version}",
      "CAPE_SURICATA_VERSION=${var.cape_suricata_version}",
    ]
    script           = "${path.root}/provisioners/00-install-cape.sh"
    execute_command  = "{{.Vars}} sudo -E bash '{{.Path}}'"
  }

  # 10 — Pull the qcow2 template from S3 into /var/lib/libvirt/images/.
  # Phase 2 will run clone-win11-vms.sh against this; we don't run it here
  # because nested virt is not enabled on the Packer build instance.
  provisioner "shell" {
    environment_vars = [
      "QCOW2_S3_BUCKET=${var.qcow2_s3_bucket}",
      "QCOW2_S3_KEY=${var.qcow2_s3_key}",
      "BASE_VM_NAME=${var.base_vm_name}",
    ]
    script          = "${path.root}/provisioners/10-stage-qcow2.sh"
    execute_command = "{{.Vars}} sudo -E bash '{{.Path}}'"
  }

  # 15a — Upload the win11_seabios libvirt domain XML to /tmp on the
  # build instance. Packer's `shell` provisioner with `script` only
  # uploads that one .sh; sibling files don't auto-upload, hence this
  # explicit `file` provisioner.
  provisioner "file" {
    source      = "${path.root}/provisioners/win11_seabios.xml"
    destination = "/tmp/win11_seabios.xml"
  }

  # 15b — Stage the template XML at /etc/libvirt/qemu/win11_seabios.xml
  # on the warm AMI. We do NOT `virsh define` here: define probes the
  # emulator's KVM support, and the Packer build instance has no
  # nested virtualization. Define happens in Phase 2's SSM clone
  # command (ami-bake-clone-phase.sh) on the m8i.8xlarge instance
  # where /dev/kvm exists.
  provisioner "shell" {
    environment_vars = ["TEMPLATE_XML=/tmp/win11_seabios.xml"]
    script           = "${path.root}/provisioners/15-stage-template.sh"
    execute_command  = "{{.Vars}} sudo -E bash '{{.Path}}'"
  }

  # 90 — Cleanup before snapshot: apt cache, /var/log, machine-id,
  # ssh host keys (regenerated on first boot of derived instance).
  provisioner "shell" {
    script          = "${path.root}/provisioners/90-cleanup.sh"
    execute_command = "{{.Vars}} sudo -E bash '{{.Path}}'"
  }

  # Manifest for downstream workflow consumption — emits the warm AMI id
  # so ami-bake.yml's Phase 2 step can pick it up.
  post-processor "manifest" {
    output     = "packer-manifest.json"
    strip_path = true
  }
}
