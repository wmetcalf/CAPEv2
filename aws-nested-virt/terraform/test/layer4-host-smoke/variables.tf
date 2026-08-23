variable "aws_region" {
  description = "AWS region for the test instance."
  type        = string
  default     = "us-east-1"
}

variable "project_name" {
  description = "Tag prefix for the ephemeral test resources."
  type        = string
  default     = "capev2"
}

variable "test_run_id" {
  description = <<-EOT
    Identifier for this test run, used in resource names so concurrent
    Layer 4 runs don't collide. CI typically passes the GHA run id.
  EOT
  type        = string
}

variable "ubuntu_ami_id" {
  description = <<-EOT
    Stock Ubuntu 24.04 AMI ID in `aws_region`. Layer 4 doesn't need
    nested-virt, so we use the cheapest Ubuntu base instead of the
    nestedvirt-ami source AMI.
  EOT
  type        = string
}

variable "instance_type" {
  description = "Layer 4 doesn't run guests; t3.medium is enough."
  type        = string
  default     = "t3.medium"
}

variable "key_pair_name" {
  description = "EC2 key pair for SSH access (optional; SSM is the primary access path)."
  type        = string
  default     = ""
}

variable "allowed_admin_cidrs" {
  description = "CIDRs allowed to SSH in for debugging. SSM is the primary path."
  type        = list(string)
  default     = []
}

variable "apt_repo_url" {
  description = "Base URL of the dev-channel apt repo (e.g. https://apt.example.com)."
  type        = string
}

variable "apt_repo_keyring_url" {
  description = "URL serving the dev-channel apt repo's GPG public key."
  type        = string
}

variable "threat_content_url" {
  description = <<-EOT
    Base URL of the threat-content GitHub Release serving the flat,
    bare-named ClamAV/Suricata mirror assets (e.g.
    https://github.com/OWNER/REPO/releases/download/threat-content).
    Layer 4 probes <url>/cape-rules.tar.gz.md5 and <url>/junk.ndb for
    reachability, and greps the installed freshclam.conf/update.yaml
    for these bare-asset URLs.
  EOT
  type        = string
}

variable "cape_core_version" {
  description = "Pinned cape-core .deb version to apt-install (e.g. 2.5.1+cape.3)."
  type        = string
}

variable "cape_signatures_version" {
  description = "Pinned cape-signatures .deb version to apt-install."
  type        = string
}

variable "cape_qemu_version" {
  description = "Pinned cape-qemu .deb version to apt-install."
  type        = string
}

variable "cape_suricata_version" {
  description = "Pinned cape-suricata .deb version to apt-install."
  type        = string
}
