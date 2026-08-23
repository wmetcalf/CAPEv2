variable "aws_region" {
  type    = string
  default = "us-east-1"
}

variable "project_name" {
  type    = string
  default = "capev2"
}

variable "test_run_id" {
  type        = string
  description = "Disambiguator for parallel runs (typically the GHA run id)."
}

# ---- Source AMI -----------------------------------------------------------

variable "cape_host_base_ami_id" {
  type        = string
  description = "cape-host-base AMI under test. Should be a recent Phase 2 bake (cape-* installed via apt, Win11 clones + snapshot1 captured)."
}

variable "instance_type" {
  type    = string
  default = "m8i.8xlarge"
}

# ---- Apt channel under test ----------------------------------------------

variable "apt_repo_url" {
  type        = string
  description = "Dev apt channel CDN URL. Layer 5 overlays this on top of the AMI's pinned versions to test the dev-channel install path."
}

variable "apt_repo_keyring_url" {
  type = string
}

variable "cape_core_version" { type = string }
variable "cape_signatures_version" { type = string }
variable "cape_qemu_version" { type = string }
variable "cape_suricata_version" { type = string }

# ---- Networking + access -------------------------------------------------

variable "allowed_admin_cidrs" {
  type        = list(string)
  default     = []
  description = "Optional CIDRs allowed inbound SSH (debugging). Empty = SSM-only access."
}

variable "key_pair_name" {
  type    = string
  default = ""
}

# ---- Optional CAPE admin sync --------------------------------------------

variable "cape_admin_secret_name" {
  type        = string
  default     = ""
  description = "Secrets Manager secret name with the CAPE admin user JSON. Required for the EICAR submission to authenticate against the API."
}
