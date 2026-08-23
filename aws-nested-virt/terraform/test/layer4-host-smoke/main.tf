data "aws_availability_zones" "available" {
  state = "available"
}

locals {
  prefix = "${var.project_name}-l4-${var.test_run_id}"
  tags = {
    Project   = var.project_name
    Stack     = "test-layer4-host-smoke"
    TestRunId = var.test_run_id
    Ephemeral = "true"
  }
}

# Minimal VPC. Layer 4 only needs outbound HTTPS to the apt repo;
# no peering, no nested-virt, no fanout.
resource "aws_vpc" "test" {
  cidr_block           = "10.99.0.0/24"
  enable_dns_support   = true
  enable_dns_hostnames = true
  tags                 = merge(local.tags, { Name = "${local.prefix}-vpc" })
}

resource "aws_internet_gateway" "test" {
  vpc_id = aws_vpc.test.id
  tags   = merge(local.tags, { Name = "${local.prefix}-igw" })
}

resource "aws_subnet" "test" {
  vpc_id                  = aws_vpc.test.id
  cidr_block              = "10.99.0.0/25"
  availability_zone       = data.aws_availability_zones.available.names[0]
  map_public_ip_on_launch = true
  tags                    = merge(local.tags, { Name = "${local.prefix}-subnet" })
}

resource "aws_route_table" "test" {
  vpc_id = aws_vpc.test.id
  route {
    cidr_block = "0.0.0.0/0"
    gateway_id = aws_internet_gateway.test.id
  }
  tags = local.tags
}

resource "aws_route_table_association" "test" {
  subnet_id      = aws_subnet.test.id
  route_table_id = aws_route_table.test.id
}

resource "aws_security_group" "test" {
  name        = "${local.prefix}-sg"
  description = "Layer 4 host smoke test SG. Outbound any; inbound SSH only from configured CIDRs."
  vpc_id      = aws_vpc.test.id

  dynamic "ingress" {
    for_each = length(var.allowed_admin_cidrs) > 0 ? [1] : []
    content {
      description = "SSH (debugging only; SSM is the primary access path)"
      from_port   = 22
      to_port     = 22
      protocol    = "tcp"
      cidr_blocks = var.allowed_admin_cidrs
    }
  }

  egress {
    description = "Outbound HTTPS (apt repo) and any (clamav freshclam, etc.)"
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }

  tags = local.tags
}

# Instance role: SSM only. No S3 / Secrets Manager access — Layer 4
# tests strictly the package install path, not the full nestedvirt
# integration.
resource "aws_iam_role" "test" {
  name = "${local.prefix}-role"
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "ec2.amazonaws.com" }
      Action    = "sts:AssumeRole"
    }]
  })
  tags = local.tags
}

resource "aws_iam_role_policy_attachment" "test_ssm" {
  role       = aws_iam_role.test.name
  policy_arn = "arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore"
}

resource "aws_iam_instance_profile" "test" {
  name = "${local.prefix}-profile"
  role = aws_iam_role.test.name
  tags = local.tags
}

resource "aws_instance" "test" {
  ami                         = var.ubuntu_ami_id
  instance_type               = var.instance_type
  subnet_id                   = aws_subnet.test.id
  vpc_security_group_ids      = [aws_security_group.test.id]
  iam_instance_profile        = aws_iam_instance_profile.test.name
  associate_public_ip_address = true
  key_name                    = var.key_pair_name != "" ? var.key_pair_name : null

  root_block_device {
    volume_size = 30
    volume_type = "gp3"
    encrypted   = true
  }

  metadata_options {
    http_endpoint               = "enabled"
    http_tokens                 = "required"
    http_put_response_hop_limit = 1
  }

  # base64gzip wraps user_data through gzip + base64, which EC2
  # decodes back to plaintext before cloud-init runs.  Lifts the
  # 16384-byte plaintext limit on aws_instance.user_data (typical
  # gzip ratio gives ~3x headroom for the shell script).  The
  # plaintext userdata.sh crossed 16KB after L4's runtime-probe
  # additions (libvirt-python import, cape-writable, qemu ldd) and
  # the "not found" probe expansion — failure mode:
  #   Error: expected length of user_data to be in the range (0 - 16384)
  user_data_base64 = base64gzip(templatefile("${path.module}/userdata.sh", {
    apt_repo_url            = var.apt_repo_url
    apt_repo_keyring_url    = var.apt_repo_keyring_url
    threat_content_url      = var.threat_content_url
    cape_core_version       = var.cape_core_version
    cape_signatures_version = var.cape_signatures_version
    cape_qemu_version       = var.cape_qemu_version
    cape_suricata_version   = var.cape_suricata_version
  }))

  tags = merge(local.tags, { Name = "${local.prefix}-host" })
}
